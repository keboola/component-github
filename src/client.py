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
from urllib.parse import urlsplit

import requests
from keboola.component.exceptions import UserException
from tenacity import RetryCallState, Retrying, retry_if_exception_type

from endpoints import Endpoint, Envelope
from graphql_source import (
    apply_window_placeholder,
    connection_path,
    detect_rows_path,
    has_window_placeholder,
    iso_range,
    read_path,
)

BASE_URL = "https://api.github.com"

#: GitHub search hard-caps results at this value per query.
GRAPHQL_SEARCH_CAP = 1000

#: Below this many seconds a shard cannot be bisected further; fetch capped + warn.
MIN_SHARD_SECONDS = 1
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"

#: Hard ceiling on the *total* time the client will wait across all rate-limit/backoff sleeps
#: in a single ``fetch``. Exceeding it raises UserException. Injectable for tests. Set to 2h so a
#: fetch can wait through more than one hourly rate-limit reset before giving up (backfills on a
#: single 5k/hr PAT routinely span multiple resets).
MAX_RATE_LIMIT_WAIT_SECONDS = 7200

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
    def get_response(self, path: str, params: dict[str, str] | None = None) -> requests.Response:
        """One guarded GET for sync actions: same auth/headers/timeout, fully classified.

        Unlike :meth:`fetch` this performs no pagination or retry — it backs the sync actions
        (testConnection / the dropdown loaders), which need a single call with the SAME network
        guard AND the SAME status classification as the run path. A connection reset / DNS /
        timeout / SSL error becomes a clean :class:`UserException` (exit 1) instead of escaping
        uncaught (which the ``__main__`` guard would map to exit 2 — "internal error" in the
        config UI). Status routing (401 → token, 404 → not-found, 403/429 rate-limit → wait-later)
        is delegated to the SAME :meth:`_classify_response` the run path uses, so the two entry
        points share identical messaging. Sync actions don't retry, so a rate-limit raises a clean
        UserException here rather than the internal ``_RetryableError``.
        """
        return self._guarded_get(f"{self.base_url}{path}", params)

    def _guarded_get(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        """One guarded GET of a full URL: network-guarded + status-classified, no retry."""
        try:
            response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise UserException(f"Could not reach GitHub at {url} ({exc}). Check connectivity and try again.") from exc
        self._classify_response(response, retryable=False)
        return response

    def get_json_array(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Guarded GET returning the parsed record list (``[]`` for a non-array body).

        Wraps :meth:`get_response` (so the network guard AND status classification apply), then
        parses the body safely. A 401/404/rate-limit has already been turned into a clean
        :class:`UserException` by :meth:`_classify_response`, so reaching the parse means a 2xx.
        SINGLE page only — for enumeration that may exceed one page use :meth:`get_json_array_all`.
        """
        response = self.get_response(path, params)
        body = self._parse_json(response)
        return body if isinstance(body, list) else []

    def get_json_array_all(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Like :meth:`get_json_array` but follows ``Link`` pagination — returns ALL pages.

        Enumeration (the sync-action dropdowns AND the run-path fan-out target lists) must not cap at
        one page: an org with >100 repos/teams would otherwise silently lose every target past the
        first 100 (the per-page max). Same single-request guard + status classification per page as
        :meth:`get_response`; query params apply to the first request only (the ``Link`` URL already
        carries the cursor), mirroring :meth:`fetch`.
        """
        url: str | None = f"{self.base_url}{path}"
        records: list[dict[str, Any]] = []
        first = True
        while url:
            response = self._guarded_get(url, params if first else None)
            first = False
            body = self._parse_json(response)
            if isinstance(body, list):
                records.extend(body)
            url = self._next_link(response)
        return records

    # ----------------------------------------------------------------------------- graphql
    def graphql(
        self, query: str, variables: dict[str, Any], rows_path: str | None = None
    ) -> tuple[list[dict[str, Any]], str]:
        """Run a GraphQL query, paginate the rows-path connection, return (raw row nodes, resolved rows_path).

        Rows path is the caller's override or auto-detected from page 1 (graphql_source.detect_rows_path).
        Pagination follows the GraphQL cursor convention: it advances only when the query declares a
        `$cursor` variable AND the connection exposes `pageInfo {hasNextPage endCursor}`.
        """
        return self._graphql_fetch_all(query, variables, rows_path)

    def _graphql_fetch_all(
        self, query: str, variables: dict[str, Any], rows_path: str | None
    ) -> tuple[list[dict[str, Any]], str]:
        """Cursor-paginate one GraphQL query and return (all raw row nodes, resolved rows_path).

        Extracted from graphql() so graphql_sharded() can reuse it per shard without changing
        graphql()'s public signature or behaviour.
        """
        vars_state: dict[str, Any] = dict(variables or {})
        supports_cursor = "$cursor" in query
        all_rows: list[dict[str, Any]] = []
        resolved = rows_path
        while True:
            data = self._graphql_post(query, vars_state)
            if resolved is None:
                resolved = detect_rows_path(data)
            node_or_list = read_path(data, resolved)
            if isinstance(node_or_list, list):
                all_rows.extend(item for item in node_or_list if isinstance(item, dict))
            elif isinstance(node_or_list, dict):
                all_rows.append(node_or_list)
            conn = connection_path(resolved)
            page_info = read_path(data, f"{conn}.pageInfo") if conn else None
            has_next = isinstance(page_info, dict) and page_info.get("hasNextPage") and page_info.get("endCursor")
            if not (supports_cursor and has_next):
                return all_rows, (resolved or "")
            vars_state["cursor"] = page_info["endCursor"]

    def graphql_sharded(
        self,
        query: str,
        variables: dict[str, Any],
        rows_path: str | None,
        since: Any,
        until: Any,
    ) -> tuple[list[dict[str, Any]], str]:
        """Adaptively shard a search query by updated:<range> so each shard is <= the 1000 cap, union rows.

        Per range [lo, hi]: substitute {{window}} -> iso_range(lo, hi) in query + variable string values,
        POST once to read search.issueCount; if issueCount > cap and the range is splittable, bisect at the
        midpoint and recurse; otherwise fetch the range in full (cursor pagination) and union rows, deduped
        by node 'id' (boundary instants can land a PR in two adjacent ranges). issueCount is read at
        connection_path(rows_path) + '.issueCount'.
        """

        def _apply_window(q: str, vs: dict[str, Any], range_str: str) -> tuple[str, dict[str, Any]]:
            wq = apply_window_placeholder(q, range_str) if has_window_placeholder(q) else q
            wvs = {
                k: (apply_window_placeholder(v, range_str) if isinstance(v, str) and has_window_placeholder(v) else v)
                for k, v in vs.items()
            }
            return wq, wvs

        # FIFO queue of (lo, hi) datetime ranges to process.
        queue: list[tuple[Any, Any]] = [(since, until)]
        resolved_path: str | None = rows_path

        # Dedup-by-id union: dict keyed by row["id"] for rows that have one; ordered list for all.
        seen_ids: dict[Any, None] = {}  # ordered dict used as ordered set
        ordered: list[dict[str, Any]] = []

        def _union_rows(new_rows: list[dict[str, Any]]) -> None:
            for row in new_rows:
                rid = row.get("id")
                if rid is not None:
                    if rid in seen_ids:
                        continue
                    seen_ids[rid] = None
                ordered.append(row)

        while queue:
            lo, hi = queue.pop(0)  # FIFO — deterministic order
            range_str = iso_range(lo, hi)
            wq, wvs = _apply_window(query, variables, range_str)

            # Probe: one POST to read issueCount (no cursor pagination needed).
            probe_data = self._graphql_post(wq, wvs)

            # Resolve rows_path on the first probe when the caller passed None.
            if resolved_path is None:
                resolved_path = detect_rows_path(probe_data)

            conn = connection_path(resolved_path) if resolved_path else None
            count_path = f"{conn}.issueCount" if conn else "issueCount"
            count = read_path(probe_data, count_path)

            if count is None:
                # issueCount not present — can't shard; fall back to one full fetch + warn.
                logging.warning(
                    "graphql_sharded: issueCount not found at '%s'; sharding requires issueCount in the query. "
                    "Fetching the whole window once (results may be truncated at %d).",
                    count_path,
                    GRAPHQL_SEARCH_CAP,
                )
                rows, resolved_path = self._graphql_fetch_all(wq, wvs, resolved_path)
                _union_rows(rows)
                break

            logging.info(
                "graphql_sharded: range=%s issueCount=%d running_total=%d",
                range_str,
                count,
                len(ordered),
            )

            if count > GRAPHQL_SEARCH_CAP and (hi - lo).total_seconds() > MIN_SHARD_SECONDS:
                # Bisect: discard probe nodes, enqueue two halves (FIFO → left half first).
                mid = lo + (hi - lo) / 2
                queue.insert(0, (mid, hi))
                queue.insert(0, (lo, mid))
                continue

            if count > GRAPHQL_SEARCH_CAP:
                logging.warning(
                    "graphql_sharded: minimal range %s still has issueCount=%d > %d; "
                    "results will be truncated by GitHub.",
                    range_str,
                    count,
                    GRAPHQL_SEARCH_CAP,
                )

            # Fetch this shard in full (cursor pagination).
            rows, resolved_path = self._graphql_fetch_all(wq, wvs, resolved_path)
            _union_rows(rows)

        return ordered, resolved_path or ""

    def _graphql_post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """One POST /graphql with the shared retry policy; returns the GraphQL `data` object."""
        return self._run_with_retry(self._graphql_attempt, query, variables)

    def _graphql_attempt(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                f"{self.base_url}/graphql", json={"query": query, "variables": variables}, timeout=DEFAULT_TIMEOUT
            )
        except requests.RequestException as exc:
            raise _RetryableError(
                BACKOFF_BASE_SECONDS,
                f"GitHub GraphQL request failed after {self.max_retries} retries: {exc}",
                scale_with_attempt=True,
            ) from exc
        self._classify_response(response, retryable=True)
        return self._graphql_extract_data(self._parse_json(response))

    @staticmethod
    def _graphql_extract_data(parsed: Any) -> dict[str, Any]:
        """GraphQL returns HTTP 200 with {data, errors}: fail on data:null+errors; warn on partial data."""
        if not isinstance(parsed, dict):
            raise UserException("GitHub GraphQL returned an unexpected (non-object) response.")
        errors = parsed.get("errors")
        data = parsed.get("data")
        if errors:
            messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors)
            if data is None:
                if any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in errors):
                    raise UserException(
                        "GitHub GraphQL rate limit (points budget) exhausted; reduce the query cost or retry later."
                    )
                raise UserException(f"GitHub GraphQL query failed: {messages}")
            logging.warning("GitHub GraphQL returned partial data with errors: %s", messages)
        if data is None:
            raise UserException("GitHub GraphQL returned no data.")
        return data

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
        """Perform one GET with bounded rate-limit/transient wait-and-retry (see _run_with_retry)."""
        return self._run_with_retry(self._attempt, url, params)

    def _retrying(self) -> Retrying:
        """The shared tenacity policy used by both the REST GET path and the GraphQL POST path."""
        return Retrying(
            retry=retry_if_exception_type(_RetryableError),
            wait=self._tenacity_wait,
            stop=self._tenacity_stop,
            sleep=self._sleep,
            retry_error_callback=self._tenacity_giveup,
            reraise=True,
        )

    def _run_with_retry(self, fn: Callable[..., Any], *args: Any) -> Any:
        return self._retrying()(fn, *args)

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

        # Single source of truth for status routing, shared with the sync-action path.
        self._classify_response(response, retryable=True)
        return response

    def _classify_response(self, response: requests.Response, *, retryable: bool) -> None:
        """Route an HTTP response by status — the ONE place both entry points classify.

        Shared by :meth:`_attempt` (run path) and :meth:`get_response` (sync-action path) so the
        401 / 404 / rate-limit messaging never drifts between them. A 2xx/3xx returns (the caller
        keeps the response). Terminal failures (401, 404, non-rate-limit 4xx) always raise a clean
        :class:`UserException`. Transient failures (403/429 rate-limit, 5xx) depend on ``retryable``:
        the run path (``retryable=True``) raises the internal :class:`_RetryableError` so tenacity
        waits-and-retries; the sync-action path (``retryable=False``) — which never retries —
        raises a clean :class:`UserException` instead.
        """
        status = response.status_code

        if status < 400:
            return

        if status == 401:
            raise UserException(
                "GitHub rejected the token (HTTP 401). The token is invalid, expired, or "
                "lacks the required scopes (read:org, repo)."
            )

        if status == 404:
            raise UserException(
                f"GitHub resource not found (HTTP 404) for {urlsplit(response.url).path}. Check the "
                "organization/repository name and that the token has access to it."
            )

        if status in (403, 429) and self._is_rate_limited(response):
            if retryable:
                raise _RetryableError(
                    self._rate_limit_wait_seconds(response),
                    "GitHub rate limit not recovered within the retry budget; reduce scope or re-run later.",
                )
            raise UserException("GitHub rate limit hit, try again shortly.")

        if status >= 500:
            if retryable:
                raise _RetryableError(
                    BACKOFF_BASE_SECONDS,
                    f"GitHub returned HTTP {status} repeatedly (after {self.max_retries} retries).",
                    scale_with_attempt=True,
                )
            raise UserException(f"GitHub is unavailable (HTTP {status}); try again shortly.")

        # Any other 4xx (e.g. 403 that is NOT a rate limit — insufficient scope / SSO) is terminal.
        raise UserException(
            f"GitHub returned HTTP {status} for {urlsplit(response.url).path}: {self._error_message(response)}. "
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
        """The GitHub-provided ``message`` field, or a generic phrase — never the raw body.

        Prefer the structured ``message`` GitHub returns on errors; if absent (or the body is not
        JSON) fall back to a generic phrase rather than echoing the raw response body, so response
        internals are never surfaced in the user-facing error.
        """
        try:
            body = response.json()
            if isinstance(body, dict) and "message" in body:
                return str(body["message"])
        except ValueError:
            pass
        return f"HTTP {response.status_code}"
