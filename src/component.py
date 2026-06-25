"""GitHub extractor — thin, registry-driven orchestrator.

``run()`` is identical for all 33 endpoints: every per-endpoint difference (path, target,
primary key, envelope shape, incremental strategy, output columns + native types) is data in
the :mod:`endpoints` registry. The component resolves a token (PAT or OAuth) in ``__init__``,
builds the auth-agnostic :class:`GitHubClient`, then for the row's chosen endpoint fetches,
flattens to the declared schema, writes a headerless typed CSV + manifest, and advances the
``last_run`` watermark only on success.
"""

import csv
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition, SupportedDataTypes
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement

from client import GitHubClient
from configuration import AuthType, Configuration
from credentials import resolve_token
from endpoints import ENDPOINTS, Endpoint, Target, expand_records, flatten_record, output_columns

STATE_LAST_RUN = "last_run"  # legacy single-watermark key (read-only, for migration)
STATE_TARGETS = "targets"  # per-target watermark map: {target_key: iso_instant}


def _redact_authorization(request: Any) -> Any:
    """VCR request hook: force the recorded ``Authorization`` header to ``Bearer ***``.

    The recorder's built-in sanitizer already whitelist-strips ``Authorization`` from
    recorded requests (it is not a safe header), so no real token can leak. This hook runs
    afterwards and unconditionally writes back a redacted placeholder so every cassette
    *explicitly* carries ``Authorization: Bearer ***`` — the sanitization is visible, not
    merely absent. It mutates only the recorded request; the live request is untouched (§7.2).
    """
    if getattr(request, "headers", None) is not None:
        request.headers["Authorization"] = ["Bearer ***"]
    return request


def _build_vcr_sanitizers() -> list[Any]:
    """Build the VCR sanitizer chain, or ``[]`` when the test-only ``keboola.vcr`` is absent.

    ``keboola.vcr`` ships with the ``keboola.datadirtest`` *dev* dependency and is not present
    in the production image (built with ``uv sync --no-dev``). Guarding the import keeps the
    component importable in production while still exposing ``VCR_SANITIZERS`` to the recorder,
    which probes this module in a context where the dev deps are installed.
    """
    try:
        from keboola.vcr import CallbackSanitizer, DefaultSanitizer
    except ImportError:
        return []
    return [
        DefaultSanitizer(
            additional_sensitive_fields=["access_token", "github_token"],
            # Keep the non-secret GitHub protocol headers the client depends on at replay:
            # Link drives pagination; the rate-limit headers + Retry-After drive wait-and-retry.
            additional_safe_headers=["link", "x-ratelimit-remaining", "x-ratelimit-reset", "retry-after"],
        ),
        CallbackSanitizer(before_request=_redact_authorization),
    ]


#: Picked up automatically by ``keboola.datadirtest`` during recording (module-level global).
#: ``DefaultSanitizer`` redacts ``#``-prefixed config secrets + token-shaped body/URL fields;
#: the callback guarantees the recorded request ``Authorization`` header reads ``Bearer ***``.
VCR_SANITIZERS = _build_vcr_sanitizers()

# Maps a registry BaseType name to the keboola.component SupportedDataTypes enum.
_BASE_TYPE_MAP = {
    "STRING": SupportedDataTypes.STRING,
    "INTEGER": SupportedDataTypes.INTEGER,
    "NUMERIC": SupportedDataTypes.NUMERIC,
    "BOOLEAN": SupportedDataTypes.BOOLEAN,
    "TIMESTAMP": SupportedDataTypes.TIMESTAMP,
}


class Component(ComponentBase):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Configuration(**self.configuration.parameters)
        self.client = GitHubClient(token=self._resolve_token())

    # ----------------------------------------------------------------------------- run
    def run(self) -> None:
        """Fan out over every resolved target into one table, advancing per-target watermarks.

        A row's target fields are multi-select with empty = "all in scope" (§4). ``_resolve_targets``
        turns them into a list of concrete context dicts; each is fetched and merged into one output
        table. A single inaccessible target (404 / not-found) is skipped with a warning so an
        "all" run is not derailed by one bad repo; token/rate-limit errors still abort.
        """
        endpoint = self._endpoint()
        incremental = self._incremental(endpoint)
        run_started_at = datetime.now(UTC).isoformat()
        state = self.get_state_file() or {}

        targets = self._resolve_targets(endpoint)
        logging.info("Fanning out over %d target(s) for '%s'.", len(targets), endpoint.name)

        all_rows: list[dict[str, Any]] = []
        succeeded: list[str] = []
        for context in targets:
            key = self._target_key(endpoint, context)
            since = self._since(key, state) if incremental else self._full_load_since()
            try:
                all_rows.extend(self._fetch_target(endpoint, context, since))
            except UserException as exc:
                if self._fans_out(endpoint) and self._is_single_target_access_error(exc):
                    logging.warning("Skipping target '%s': %s", key or "(default)", exc)
                    continue
                raise
            succeeded.append(key)

        self._write_table(endpoint, all_rows, incremental)
        if incremental:
            self._advance_state(state, succeeded, run_started_at)

    def _fetch_target(self, endpoint: Endpoint, context: dict[str, str], since: str | None) -> list[dict[str, Any]]:
        """Fetch one concrete target's rows (the nested PR scan for empty-pull_number reviews)."""
        if endpoint.name == "repository_pull_reviews" and self.cfg.pull_number is None:
            return self._fetch_pull_reviews_nested(context, since)
        enriched = self._with_pull_number(context)
        path = self._build_path(endpoint, enriched)
        params, stop_paging = self._fetch_params(endpoint, since)
        return self._collect_rows(endpoint, path, enriched, params, stop_paging)

    # ------------------------------------------------------------------- run() helpers
    def _incremental(self, endpoint: Endpoint) -> bool:
        """Effective incremental-load flag for this row.

        Incremental upsert is used ONLY when the endpoint is incremental-capable (it carries a
        server-side ``since``/``created`` param or sorts by ``updated`` for client-side stop-paging)
        AND the row's ``load_type`` is ``incremental_load``. Endpoints with no incremental signal
        (branches, tags, members, languages, …) always write a full load (``incremental=False`` →
        full replace) so rows deleted on GitHub are purged rather than lingering forever. See spec §2.3.
        """
        capable = bool(endpoint.incremental_param) or endpoint.incremental_sort
        return capable and self.cfg.incremental_load

    def _resolve_token(self) -> str:
        oauth_credentials = None
        if self.cfg.auth_type == AuthType.oauth:
            oauth_credentials = self._oauth_credentials()
        return resolve_token(self.cfg, oauth_credentials)

    def _oauth_credentials(self) -> dict[str, Any] | None:
        """The broker-injected ``authorization.oauth_api.credentials`` (guarded for absence).

        Each hop falls back to ``{}`` so a present-but-null ``authorization`` / ``oauth_api`` in
        config_data resolves to no credentials (clean UserException via the resolver, exit 1)
        rather than an AttributeError on ``None`` (uncaught, exit 2).
        """
        config_data = self.configuration.config_data or {}
        return ((config_data.get("authorization") or {}).get("oauth_api") or {}).get("credentials")

    def _endpoint(self) -> Endpoint:
        endpoint = ENDPOINTS.get(self.cfg.endpoint)
        if endpoint is None:
            raise UserException(
                f"Unknown endpoint '{self.cfg.endpoint}'. Choose one of: {', '.join(sorted(ENDPOINTS))}."
            )
        return endpoint

    def _build_path(self, endpoint: Endpoint, context: dict[str, str]) -> str:
        """Substitute a resolved target's context into the endpoint path template."""
        values = {
            "org": context.get("org", ""),
            "team_slug": context.get("team_slug", ""),
            "owner": context.get("owner", ""),
            "repo": context.get("repo", ""),
            "pull_number": context.get("pull_number", ""),
        }
        try:
            return endpoint.path_template.format(**values)
        except KeyError as exc:
            raise UserException(f"Missing path parameter {exc} for endpoint '{endpoint.name}'.") from exc

    # ------------------------------------------------------------------- target resolution
    def _resolve_targets(self, endpoint: Endpoint) -> list[dict[str, str]]:
        """One context dict per concrete target the row expands to (empty selection = all in scope)."""
        if endpoint.target == Target.NONE:
            return [{}]
        if endpoint.target == Target.ORG:
            return [{"org": org} for org in (self.cfg.organization or self._list_org_logins())]
        if endpoint.target in (Target.OWNER_REPO, Target.OWNER_REPO_PULL):
            repos = self.cfg.repository or self._all_repos_in_scope()
            return [self._repo_context(name) for name in repos]
        if endpoint.target == Target.ORG_TEAM:
            return self._resolve_team_targets()
        return [{}]

    def _all_repos_in_scope(self) -> list[str]:
        """All repos for the selected org(s), or all of the user's repos when no org is selected."""
        names: list[str] = []
        for org in self.cfg.organization or [""]:
            names.extend(self._list_repo_fullnames(org))
        return names

    def _resolve_team_targets(self) -> list[dict[str, str]]:
        """team values are qualified ``org/team_slug``; empty = all teams in the selected org(s).

        Back-compat: a bare slug (legacy rows, where ``organization`` carried the org separately) is
        paired with each selected org.
        """
        if self.cfg.team_slug:
            targets = []
            for value in self.cfg.team_slug:
                if "/" in value:
                    org, _, slug = value.partition("/")
                    targets.append({"org": org, "team_slug": slug})
                else:
                    targets.extend({"org": org, "team_slug": value} for org in self.cfg.organization or [""])
            return targets
        targets = []
        for org in self.cfg.organization or self._list_org_logins():
            targets.extend({"org": org, "team_slug": slug} for slug in self._list_team_slugs(org))
        return targets

    @staticmethod
    def _repo_context(full_name: str) -> dict[str, str]:
        owner, _, repo = full_name.partition("/")
        return {"owner": owner, "repo": repo, "repository": full_name}

    @staticmethod
    def _target_key(endpoint: Endpoint, context: dict[str, str]) -> str:
        """Stable per-target state key (the watermark map key)."""
        if endpoint.target in (Target.OWNER_REPO, Target.OWNER_REPO_PULL):
            return context.get("repository", "")
        if endpoint.target == Target.ORG_TEAM:
            return f"{context.get('org', '')}/{context.get('team_slug', '')}"
        return context.get("org", "")

    def _fans_out(self, endpoint: Endpoint) -> bool:
        """True when the row targets more than one concrete resource (so one bad target is skippable)."""
        if endpoint.target == Target.NONE:
            return False
        return not (len(self.cfg.repository) == 1 and len(self.cfg.organization) <= 1)

    @staticmethod
    def _is_single_target_access_error(exc: UserException) -> bool:
        """A 404/not-found on one target — skippable; token/rate-limit errors are not."""
        message = str(exc).lower()
        return "http 404" in message or "not found" in message

    def _with_pull_number(self, context: dict[str, str]) -> dict[str, str]:
        """For pull_reviews targeted mode, inject the configured single pull_number into the path/context."""
        if self.cfg.pull_number is not None:
            return {**context, "pull_number": str(self.cfg.pull_number)}
        return context

    def _fetch_pull_reviews_nested(self, context: dict[str, str], since: str | None) -> list[dict[str, Any]]:
        """repo -> list its PRs (sorted updated desc, bounded by ``since``) -> each PR's reviews."""
        reviews_ep = ENDPOINTS["repository_pull_reviews"]
        pulls_ep = ENDPOINTS["repository_pulls"]
        owner, repo = context["owner"], context["repo"]
        pulls_path = f"/repos/{owner}/{repo}/pulls"
        pulls_params, pulls_stop = self._fetch_params(pulls_ep, since)
        rows: list[dict[str, Any]] = []
        for pr in self.client.fetch(pulls_ep, pulls_path, pulls_params, stop_paging=pulls_stop):
            number = pr.get("number")
            if number is None:
                continue
            pr_context = {**context, "pull_number": str(number)}
            reviews_path = f"/repos/{owner}/{repo}/pulls/{number}/reviews"
            for payload in self.client.fetch(reviews_ep, reviews_path):
                rows.append(flatten_record(reviews_ep, payload, pr_context))
        return rows

    def _since(self, key: str, state: dict[str, Any]) -> str | None:
        """Incremental lower bound for one target: per-target watermark, then legacy ``last_run``,
        then the row's parsed ``Fetch Since`` (relative phrases resolved). ``None`` = full history.

        The legacy fallback migrates rows whose state predates per-target watermarks: their single
        ``last_run`` seeds every target on the first per-target run, so no history is re-fetched.
        """
        targets = state.get(STATE_TARGETS) or {}
        return targets.get(key) or state.get(STATE_LAST_RUN) or self.cfg.parsed_initial_since()

    def _full_load_since(self) -> str | None:
        """Full Load honours ``Fetch Since`` on EVERY run (rolling window); no watermark, no state."""
        return self.cfg.parsed_initial_since()

    def _advance_state(self, state: dict[str, Any], succeeded: list[str], run_started_at: str) -> None:
        """Set each successfully-fetched target's watermark to this run's start; leave others as-is."""
        targets = dict(state.get(STATE_TARGETS) or {})
        for key in succeeded:
            targets[key] = run_started_at
        self.write_state_file({STATE_TARGETS: targets})

    def _fetch_params(
        self, endpoint: Endpoint, since: str | None
    ) -> tuple[dict[str, Any], Callable[[dict[str, Any]], bool] | None]:
        """Build query params + the optional client-side stop-paging predicate."""
        params: dict[str, Any] = dict(endpoint.default_params)
        for row_field, query_param in endpoint.filter_params.items():
            params[query_param] = getattr(self.cfg, row_field)

        stop_paging = None
        if since and endpoint.incremental_param:
            # The watermark is rendered per-endpoint: most `since` params take a bare ISO instant,
            # but /actions/runs `created` needs date-range grammar (>=YYYY-MM-DD) — see endpoints.
            params[endpoint.incremental_param] = endpoint.incremental_param_format(since)
        elif since and endpoint.incremental_sort:
            # No server `since` (e.g. /pulls) — sort desc and stop once we cross the watermark.
            stop_paging = self._stop_paging_predicate(since)
        return params, stop_paging

    @staticmethod
    def _stop_paging_predicate(since: str) -> Callable[[dict[str, Any]], bool]:
        # `since` is normally pre-validated (Configuration._validate_initial_since) or our own
        # ISO `last_run` watermark, but guard the build-time parse too: a malformed watermark
        # (e.g. a hand-edited state file) must surface as a clean UserException (exit 1), never an
        # uncaught ValueError (exit 2).
        try:
            watermark = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise UserException(
                "Incremental watermark must be an ISO-8601 timestamp, e.g. 2024-01-31T00:00:00Z"
            ) from exc

        def stop(last_record: dict[str, Any]) -> bool:
            updated = last_record.get("updated_at")
            if not isinstance(updated, str):
                return False
            try:
                return datetime.fromisoformat(updated.replace("Z", "+00:00")) < watermark
            except ValueError:
                return False

        return stop

    def _collect_rows(
        self,
        endpoint: Endpoint,
        path: str,
        context: dict[str, str],
        params: dict[str, Any],
        stop_paging: Callable[[dict[str, Any]], bool] | None,
    ) -> list[dict[str, Any]]:
        """Fetch, expand object-maps, drop PR rows, and flatten to the declared columns."""
        rows: list[dict[str, Any]] = []
        for payload in self.client.fetch(endpoint, path, params, stop_paging=stop_paging):
            for record in expand_records(endpoint, payload, context):
                if endpoint.drop_predicate and record.get(endpoint.drop_predicate) is not None:
                    continue  # /issues PR-split: drop pull-request rows (§4.4)
                rows.append(flatten_record(endpoint, record, context))
        return rows

    def _write_table(self, endpoint: Endpoint, rows: list[dict[str, Any]], incremental: bool) -> None:
        columns = output_columns(endpoint)
        table = self.create_out_table_definition(
            f"{endpoint.name}.csv",
            primary_key=endpoint.primary_key,
            incremental=incremental,
            schema=self._build_schema(endpoint),
            # Headerless invariant (§6.5): the schema names the columns; CSV carries no header.
            has_header=False,
        )
        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writerows(rows)  # no writeheader() — headerless
        self.write_manifest(table)
        logging.info("Wrote %d rows to table '%s'.", len(rows), endpoint.name)

    @staticmethod
    def _build_schema(endpoint: Endpoint) -> dict[str, ColumnDefinition]:
        """Authoritative native-type schema (§6.5): one ColumnDefinition per output column."""
        typed = {col.name: col.base_type.value for col in endpoint.schema}
        pk = set(endpoint.primary_key)
        schema: dict[str, ColumnDefinition] = {}
        for name in output_columns(endpoint):
            base = _BASE_TYPE_MAP.get(typed.get(name, "STRING"), SupportedDataTypes.STRING)
            schema[name] = ColumnDefinition(
                data_types=BaseType(dtype=base),
                primary_key=name in pk,
            )
        return schema

    # --------------------------------------------------------------------- sync actions
    @sync_action("testConnection")
    def test_connection(self) -> None:
        """Validate the token against the free ``GET /rate_limit`` endpoint."""
        self._call_rate_limit()

    @sync_action("listOrganizations")
    def list_organizations(self) -> list[SelectElement]:
        return [SelectElement(value=org, label=org) for org in self._list_org_logins()]

    @sync_action("listRepositories")
    def list_repositories(self) -> list[SelectElement]:
        # The first selected org scopes the picker (the UI reloads it as `organization` changes);
        # no org selected → the authenticated user's repos.
        org = self.cfg.organization[0] if self.cfg.organization else ""
        return [SelectElement(value=name, label=name) for name in self._list_repo_fullnames(org)]

    @sync_action("listTeams")
    def list_teams(self) -> list[SelectElement]:
        if not self.cfg.organization:
            raise UserException("Select an organization before loading teams.")
        org = self.cfg.organization[0]
        teams = self._fetch_json_array(f"/orgs/{org}/teams", params={"per_page": "100"})
        # Value is the qualified ``org/team_slug`` (self-contained across multiple selected orgs).
        return [
            SelectElement(value=f"{org}/{slug}", label=f"{org} / {team.get('name') or slug}")
            for team in teams
            if (slug := team.get("slug"))
        ]

    # --------------------------------------------- shared enumeration (sync actions + run fan-out)
    def _list_org_logins(self) -> list[str]:
        return [login for o in self._fetch_json_array("/user/orgs") if (login := o.get("login"))]

    def _list_repo_fullnames(self, org: str) -> list[str]:
        path = f"/orgs/{org}/repos" if org else "/user/repos"
        repos = self._fetch_json_array(path, params={"per_page": "100"})
        return [name for r in repos if (name := r.get("full_name"))]

    def _list_team_slugs(self, org: str) -> list[str]:
        teams = self._fetch_json_array(f"/orgs/{org}/teams", params={"per_page": "100"})
        return [slug for t in teams if (slug := t.get("slug"))]

    # ------------------------------------------------------------- sync-action helpers
    def _call_rate_limit(self) -> None:
        """Single guarded GET /rate_limit; the client classifies status (401/404/rate-limit/network)."""
        self.client.get_response("/rate_limit")

    def _fetch_json_array(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """Guarded GET for the dropdown loaders.

        Status routing (401 token / 404 not-found / rate-limit / network) is handled inside the
        client by the SAME classifier the run path uses, so a rate-limited dropdown no longer reads
        as a token problem — it surfaces the shared rate-limit message.
        """
        return self.client.get_json_array(path, params)


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
