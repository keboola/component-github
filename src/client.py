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
from tenacity import RetryCallState, Retrying, retry_if_exception_type

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


class _RetryableError(Exception):
    """Internal signal that an attempt failed transiently and may be retried.

    Carries the header-driven ``wait_seconds`` tenacity should sleep before the next attempt and
    the ``giveup_message`` to surface (as a :class:`UserException`) if the retry budget is blown.
    Never escapes the client: tenacity either retries or the give-up callback converts it.
    """

    def __init__(self, wait_seconds: float, giveup_message: str, *, scale_with_attempt: bool = False):
        super().__init__(giveup_message)
        self.wait_seconds = wait_seconds
        self.giveup_message = giveup_message
        # Transient (network/5xx) backoff grows exponentially with the attempt number; a
        # header-driven rate-limit wait is an absolute value and must NOT be scaled.
        self.scale_with_attempt = scale_with_attempt


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

    # --------------------------------------------------------- single-request GET (sync actions)
    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        """One guarded GET for sync actions: same auth/headers/timeout, network errors guarded.

        Unlike :meth:`fetch` this performs no pagination or retry — it backs the sync actions
        (testConnection / the dropdown loaders), which need a single call with the SAME network
        guard as the run path. A connection reset / DNS / timeout / SSL error becomes a clean
        :class:`UserException` (exit 1) instead of escaping uncaught (which the ``__main__`` guard
        would map to exit 2 — "internal error" in the config UI). The 401 / other-4xx mapping is
        left to the caller, since sync actions return endpoint-specific messages.
        """
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise UserException(f"Could not reach GitHub at {path} ({exc}). Check connectivity and try again.") from exc
        return response

    def get_json_array(self, path: str, params: dict[str, str] | None = None) -> tuple[Any, list[dict[str, Any]]]:
        """Guarded GET returning ``(response, records)`` where records is ``[]`` for non-array bodies.

        Wraps :meth:`get_json` (so the network guard applies) and parses the body safely; the raw
        response is returned alongside so the caller can branch on the status code (401 → token,
        other 4xx/5xx → generic) before reading the list.
        """
        response = self.get_json(path, params)
        if response.status_code >= 400:
            return response, []
        body = self._parse_json(response)
        return response, body if isinstance(body, list) else []

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

    # --------------------------------------------------------------- request + retry control
    def _request(self, url: str, params: dict[str, Any] | None) -> requests.Response:
        """Perform one GET with bounded rate-limit/transient wait-and-retry, driven by tenacity.

        The single-attempt logic (:meth:`_attempt`) classifies the outcome: success returns the
        response, terminal failures (401/404/non-rate-limit 4xx) raise :class:`UserException`
        immediately, and transient failures (network, rate-limit 403/429, 5xx) raise the internal
        :class:`_RetryableError` carrying the header-driven wait and the give-up message.

        Tenacity orchestrates the attempts: the ``wait`` callable reads that header-driven wait,
        the ``stop`` callable enforces the SAME bounds as before — cumulative wait budget first
        (``max_rate_limit_wait_seconds``), then attempt count (``max_retries``) — and on give-up
        the ``retry_error_callback`` re-raises the carried message as a :class:`UserException`.
        Only ``_RetryableError`` is retried; terminal ``UserException`` propagates untouched.
        """
        retrying = Retrying(
            retry=retry_if_exception_type(_RetryableError),
            wait=self._tenacity_wait,
            stop=self._tenacity_stop,
            sleep=self._sleep,
            retry_error_callback=self._tenacity_giveup,
            reraise=True,
        )
        return retrying(self._attempt, url, params)

    def _attempt(self, url: str, params: dict[str, Any] | None) -> requests.Response:
        """One GET; success → response, terminal → UserException, transient → ``_RetryableError``."""
        try:
            response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            # Network error — treat as transient, bounded by the same budget.
            raise _RetryableError(
                BACKOFF_BASE_SECONDS,
                f"GitHub request failed after {self.max_retries} retries: {exc}",
                scale_with_attempt=True,
            ) from exc

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
            raise _RetryableError(
                self._rate_limit_wait_seconds(response),
                "GitHub rate limit not recovered within the retry budget; reduce scope or re-run later.",
            )

        if status >= 500:
            raise _RetryableError(
                BACKOFF_BASE_SECONDS,
                f"GitHub returned HTTP {status} repeatedly (after {self.max_retries} retries).",
                scale_with_attempt=True,
            )

        # Any other 4xx (e.g. 403 that is NOT a rate limit — insufficient scope / SSO) is terminal.
        raise UserException(
            f"GitHub request to {url} failed with HTTP {status}: {self._error_message(response)}. "
            "Check the token scopes/SSO authorization and the target's access."
        )

    # ---------------------------------------------------------------- tenacity callables
    def _tenacity_wait(self, retry_state: RetryCallState) -> float:
        """Header-driven wait for the failed attempt: the value carried by ``_RetryableError``.

        Backoff for network/5xx grows exponentially with the attempt number to match the previous
        hand-rolled loop; rate-limit waits use the exact value computed from the response headers.
        """
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if not isinstance(exc, _RetryableError):
            return 0.0
        seconds = exc.wait_seconds
        if exc.scale_with_attempt:  # transient backoff scales with attempt; header waits don't
            seconds = float(BACKOFF_BASE_SECONDS * (2 ** (retry_state.attempt_number - 1)))
        logging.warning("Waiting %.0fs before retry (%s).", seconds, exc.giveup_message)
        return seconds

    def _tenacity_stop(self, retry_state: RetryCallState) -> bool:
        """Give up when the next sleep would blow the cumulative wait budget, or after max_retries.

        ``wait`` runs before ``stop`` in tenacity, so ``upcoming_sleep`` already holds this round's
        sleep — letting us enforce the budget BEFORE sleeping, exactly like the old ``_wait``.
        """
        if retry_state.idle_for + retry_state.upcoming_sleep > self.max_rate_limit_wait_seconds:
            return True
        return retry_state.attempt_number > self.max_retries

    def _tenacity_giveup(self, retry_state: RetryCallState) -> requests.Response:
        """Convert an exhausted retry into a UserException (budget message takes precedence)."""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if retry_state.idle_for + retry_state.upcoming_sleep > self.max_rate_limit_wait_seconds:
            raise UserException(
                "GitHub rate limit not recovered within the wait budget "
                f"({self.max_rate_limit_wait_seconds}s); reduce scope or re-run later."
            ) from exc
        if isinstance(exc, _RetryableError):
            raise UserException(exc.giveup_message) from exc
        raise UserException("GitHub request failed after exhausting the retry budget.")  # defensive

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        """A 403/429 is a rate limit if a Retry-After is present or the remaining quota is 0."""
        if response.headers.get("Retry-After") is not None:
            return True
        return response.headers.get("x-ratelimit-remaining") == "0"

    @staticmethod
    def _rate_limit_wait_seconds(response: requests.Response) -> float:
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
