"""Auth-agnostic GitHub REST client.

One ``requests.Session`` with the standard GitHub headers (``Authorization: Bearer`` +
``Accept`` + ``X-GitHub-Api-Version``). The client knows nothing about where the token came
from (PAT vs OAuth — that is :mod:`credentials`). It owns three concerns:

* **Envelope unwrapping** — turns each response shape (object / array / wrapped / object-map)
  into a stream of records (spec §4.5).
* **Pagination** — follows the ``Link`` header ``rel="next"`` URL *verbatim* until absent;
  never hand-builds ``page=N`` URLs (some endpoints return opaque ``after`` cursors — §4.2).
* **Rate-limit / transient handling** — bounded wait-and-retry; a blown wait budget raises a
  clean :class:`UserException` (exit 1), never an unhandled crash (§4.3).
"""

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests
from keboola.component.exceptions import UserException

from endpoints import Endpoint, Envelope

BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"

#: Hard ceiling on the *total* time the client will wait across all rate-limit/backoff sleeps
#: in a single ``fetch``. Exceeding it raises UserException. Injectable for tests.
MAX_RATE_LIMIT_WAIT_SECONDS = 3600

#: Max retry attempts for a single request before giving up (rate-limit or transient).
MAX_RETRIES = 8

#: Base for exponential backoff on transient 5xx / network errors (seconds).
BACKOFF_BASE_SECONDS = 60

#: Small buffer added when sleeping until the rate-limit reset epoch.
RESET_BUFFER_SECONDS = 5

#: Per-request socket timeout (seconds). Without it a hung connection blocks the job forever.
DEFAULT_TIMEOUT = 60


class GitHubClient:
    def __init__(
        self,
        token: str,
        base_url: str = BASE_URL,
        max_rate_limit_wait_seconds: int = MAX_RATE_LIMIT_WAIT_SECONDS,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_rate_limit_wait_seconds = max_rate_limit_wait_seconds
        self.max_retries = max_retries
        self._sleep = sleep  # injectable so tests don't actually block
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
            }
        )

    # ----------------------------------------------------------------------------- fetch
    def fetch(
        self,
        endpoint: Endpoint,
        path: str,
        params: dict[str, Any] | None = None,
        stop_paging: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Iterator[Any]:
        """Yield unwrapped records for an endpoint, following pagination to the end.

        :param endpoint: the registry descriptor (drives envelope handling).
        :param path: the concrete path (targets already substituted), e.g.
            ``/repos/octocat/Hello/issues``.
        :param params: query params (per_page, since, sort, filters, …).
        :param stop_paging: optional predicate for ``incremental_sort`` endpoints — given the
            last record on a page, returns True to stop fetching further pages (records on/after
            the watermark are still yielded; the next page is simply not requested). See §2.3/R6.

        For OBJECT / ARRAY / WRAPPED the yielded items are plain ``dict`` records. For the two
        object-map envelopes the *raw payload* is yielded once (single page) and expanded by the
        orchestrator (``endpoints.expand_records``), since those shapes are not record streams.
        """
        url: str | None = f"{self.base_url}{path}"
        first = True
        while url:
            # Query params apply only to the first request; subsequent pages use the Link URL
            # verbatim (it already carries the right cursor/per_page).
            response = self._request(url, params if first else None)
            first = False
            payload = self._parse_json(response)

            if endpoint.envelope in (Envelope.OBJECT_MAP_VALUE, Envelope.OBJECT_MAP_LIST):
                # Single-shot map payloads — never paginate.
                yield payload
                return

            page_records = self._unwrap(endpoint, payload)
            last_record: dict[str, Any] | None = None
            for record in page_records:
                last_record = record
                yield record

            # Client-side stop-paging for sort=updated endpoints with no server `since`.
            if stop_paging is not None and last_record is not None and stop_paging(last_record):
                return

            url = self._next_link(response)

    @staticmethod
    def _unwrap(endpoint: Endpoint, payload: Any) -> list[dict[str, Any]]:
        if endpoint.envelope == Envelope.OBJECT:
            return [payload] if isinstance(payload, dict) else []
        if endpoint.envelope == Envelope.ARRAY:
            return payload if isinstance(payload, list) else []
        if endpoint.envelope == Envelope.WRAPPED:
            inner = payload.get(endpoint.envelope_key, []) if isinstance(payload, dict) else []
            return inner if isinstance(inner, list) else []
        return []

    @staticmethod
    def _next_link(response: requests.Response) -> str | None:
        """The ``rel="next"`` URL from the ``Link`` header, or ``None`` (explicit stop)."""
        return response.links.get("next", {}).get("url")

    @staticmethod
    def _parse_json(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise UserException(f"GitHub returned a non-JSON response (HTTP {response.status_code}): {exc}") from exc

    # --------------------------------------------------------------- request + retry loop
    def _request(self, url: str, params: dict[str, Any] | None) -> requests.Response:
        """Perform one GET with bounded rate-limit/transient wait-and-retry.

        Terminal failures raise :class:`UserException` (exit 1) with an actionable message.
        """
        waited = 0.0
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
            except requests.RequestException as exc:
                # Network error — treat as transient, bounded by the same budget.
                waited = self._wait(self._backoff(attempt), waited, reason=f"network error: {exc}")
                if attempt > self.max_retries:
                    raise UserException(f"GitHub request failed after {self.max_retries} retries: {exc}") from exc
                continue

            status = response.status_code

            if status < 400:
                return response

            if status == 401:
                raise UserException(
                    "GitHub rejected the token (HTTP 401). The token is invalid, expired, or "
                    "lacks the required scopes (read:org, repo)."
                )

            if status == 404:
                raise UserException(
                    f"GitHub resource not found (HTTP 404): {url}. Check the organization/repository "
                    "name and that the token has access to it."
                )

            if status in (403, 429) and self._is_rate_limited(response):
                wait_seconds = self._rate_limit_wait_seconds(response)
                waited = self._wait(wait_seconds, waited, reason=f"rate limit (HTTP {status})")
                if attempt > self.max_retries:
                    raise UserException(
                        "GitHub rate limit not recovered within the retry budget; reduce scope or re-run later."
                    )
                continue

            if status >= 500:
                waited = self._wait(self._backoff(attempt), waited, reason=f"transient HTTP {status}")
                if attempt > self.max_retries:
                    raise UserException(f"GitHub returned HTTP {status} repeatedly (after {self.max_retries} retries).")
                continue

            # Any other 4xx (e.g. 403 that is NOT a rate limit — insufficient scope / SSO) is terminal.
            raise UserException(
                f"GitHub request to {url} failed with HTTP {status}: {self._error_message(response)}. "
                "Check the token scopes/SSO authorization and the target's access."
            )

    def _wait(self, seconds: float, already_waited: float, reason: str) -> float:
        """Sleep ``seconds`` if the total wait budget allows; else raise UserException."""
        if already_waited + seconds > self.max_rate_limit_wait_seconds:
            raise UserException(
                "GitHub rate limit not recovered within the wait budget "
                f"({self.max_rate_limit_wait_seconds}s); reduce scope or re-run later."
            )
        logging.warning("Waiting %.0fs before retry (%s).", seconds, reason)
        self._sleep(seconds)
        return already_waited + seconds

    def _backoff(self, attempt: int) -> float:
        return float(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        """A 403/429 is a rate limit if a Retry-After is present or the remaining quota is 0."""
        if response.headers.get("Retry-After") is not None:
            return True
        return response.headers.get("x-ratelimit-remaining") == "0"

    def _rate_limit_wait_seconds(self, response: requests.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                return float(BACKOFF_BASE_SECONDS)

        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                delta = float(reset) - time.time() + RESET_BUFFER_SECONDS
                return max(delta, 0.0)
            except ValueError:
                pass
        return float(BACKOFF_BASE_SECONDS)

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        try:
            body = response.json()
            if isinstance(body, dict) and "message" in body:
                return str(body["message"])
        except ValueError:
            pass
        return response.text[:200]
