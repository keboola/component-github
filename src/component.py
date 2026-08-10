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
from endpoints import (
    ENDPOINTS,
    GRAPHQL_ENDPOINT,
    Endpoint,
    Target,
    _dig,
    expand_records,
    flatten_record,
    output_columns,
    record_in_window,
)
from graphql_source import (
    TEMPLATES,
    apply_date_placeholders,
    collect_columns,
    flatten_graphql_node,
    has_window_placeholder,
)


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
        """Fan out over every resolved target into one table, fetching the ``[since, until]`` window.

        A row's target fields are multi-select with empty = "all in scope" (§4); ``_resolve_targets``
        turns them into concrete context dicts, each fetched and merged into one output table. The
        fetch window is the explicit ``since``/``until`` config — there is NO hidden watermark/state,
        so widening ``since`` backfills (it always applies). A single inaccessible target (404) is
        skipped with a warning; token/rate-limit errors still abort.
        """
        if self.cfg.endpoint == GRAPHQL_ENDPOINT:
            return self._run_graphql()
        endpoint = self._endpoint()
        incremental = self._incremental(endpoint)
        since, until = self.cfg.parsed_since(), self.cfg.parsed_until()

        targets = self._resolve_targets(endpoint)
        logging.info(
            "Fanning out over %d target(s) for '%s'%s.",
            len(targets),
            endpoint.name,
            self._window_label(since, until),
        )

        all_rows: list[dict[str, Any]] = []
        for context in targets:
            try:
                all_rows.extend(self._fetch_target(endpoint, context, since, until))
            except UserException as exc:
                if self._fans_out(endpoint) and self._is_single_target_access_error(exc):
                    logging.warning("Skipping target '%s': %s", self._target_label(context) or "(default)", exc)
                    continue
                raise

        self._write_table(endpoint, all_rows, incremental)

    def _fetch_target(
        self, endpoint: Endpoint, context: dict[str, str], since: datetime | None, until: datetime | None
    ) -> list[dict[str, Any]]:
        """Fetch one concrete target's rows (the nested PR scan for empty-pull_number reviews)."""
        if endpoint.name == "repository_pull_reviews" and self.cfg.pull_number is None:
            return self._fetch_pull_reviews_nested(context, since, until)
        enriched = self._with_pull_number(context)
        path = self._build_path(endpoint, enriched)
        params, stop_paging = self._fetch_params(endpoint, since)
        return self._collect_rows(endpoint, path, enriched, params, stop_paging, since, until)

    # ------------------------------------------------------------------- graphql data source
    def _run_graphql(self) -> None:
        """Run a (custom or templated) GraphQL query and write its flattened result to one table."""
        template = TEMPLATES.get(self.cfg.query_template.value)
        if template is not None:
            params = self._graphql_template_params()
            query = template.build_query(params)
            variables = template.build_variables(params)
            rows_path: str | None = template.rows_path
            output_table = self.cfg.output_table or template.output_table
            primary_key = self.cfg.primary_key or list(template.primary_key)
        else:  # custom
            query = self.cfg.gql_query
            variables = self.cfg.gql_variables
            rows_path = self.cfg.gql_row_path or None
            output_table = self.cfg.output_table
            primary_key = self.cfg.primary_key

        since_dt, until_dt = self.cfg.parsed_since(), self.cfg.parsed_until()

        # Detect {{window}} placeholder: sharded mode (each shard gets a concrete date-range substituted).
        windowed = has_window_placeholder(query) or any(
            isinstance(v, str) and has_window_placeholder(v) for v in variables.values()
        )

        logging.info("Running GraphQL query (template=%s) into table '%s'.", self.cfg.query_template, output_table)

        if windowed:
            if since_dt is None:
                raise UserException(
                    "{{window}} requires a Since value, e.g. '90 days ago'. Set the Since field on this row."
                )
            until_eff = until_dt or datetime.now(UTC)
            raw_rows, resolved = self.client.graphql_sharded(query, variables, rows_path, since_dt, until_eff)
        else:
            # Substitute {{since}}/{{until}} with the resolved (dateparser) window dates so a GraphQL row can
            # roll its own window — e.g. `updated:>={{since}}` with Since = "60 days ago" (§ rolling window).
            since_s = since_dt.isoformat() if since_dt else None
            until_s = until_dt.isoformat() if until_dt else None
            query = apply_date_placeholders(query, since_s, until_s)
            variables = {
                key: (apply_date_placeholders(val, since_s, until_s) if isinstance(val, str) else val)
                for key, val in variables.items()
            }
            raw_rows, resolved = self.client.graphql(query, variables, rows_path)
        rows = [flatten_graphql_node(node) for node in raw_rows]

        if not rows:
            # Deliberately do NOT fall back to the primary key as the column set. That declares a
            # one-column schema, and Storage then rejects the load against the real table with a
            # "does not contain the same number of columns as the schema" error that points nowhere
            # near the cause. Leaving the table untouched keeps its contents; the warning (plus the
            # client's own diagnostic) says what actually happened.
            logging.warning(
                "GraphQL query (template=%s, rows path='%s') returned no rows, so nothing was written: "
                "table '%s' still holds the PREVIOUS run's rows and is now stale. A green job here does "
                "NOT mean the source is empty — see the client's warning above for what the API reported.",
                self.cfg.query_template,
                resolved,
                output_table,
            )
            return

        incremental = self.cfg.incremental_load and bool(primary_key)
        if self.cfg.incremental_load and not primary_key:
            logging.warning("Incremental load requested but no primary key set; writing a full load instead.")
        self._write_dynamic_table(output_table, collect_columns(rows), rows, primary_key, incremental)

    def _graphql_template_params(self) -> dict[str, Any]:
        return {"org": self.cfg.gql_org, "repo": self.cfg.gql_repo, "filenames": self.cfg.gql_filenames}

    def _write_dynamic_table(
        self,
        name: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        primary_key: list[str],
        incremental: bool,
    ) -> None:
        """Write a dynamic all-STRING headerless table (columns discovered from the flattened rows)."""
        missing = [pk for pk in primary_key if pk not in columns]
        if missing:
            raise UserException(
                f"Primary key column(s) {missing} are not present in the query result columns {columns}. "
                "Adjust the Primary key or the query."
            )
        if not columns:
            logging.warning("GraphQL query for table '%s' produced no rows and no columns; nothing written.", name)
            return
        pk_set = set(primary_key)
        schema = {
            col: ColumnDefinition(
                data_types=BaseType(dtype=SupportedDataTypes.STRING),
                primary_key=col in pk_set,
            )
            for col in columns
        }
        table = self.create_out_table_definition(
            f"{name}.csv",
            primary_key=primary_key,
            incremental=incremental,
            schema=schema,
            has_header=False,
        )
        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writerows(rows)
        self.write_manifest(table)
        logging.info("Wrote %d rows to table '%s'.", len(rows), name)

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
    def _target_label(context: dict[str, str]) -> str:
        """Human label for a target, used only in the skip warning."""
        return context.get("repository") or context.get("team_slug") or context.get("org") or ""

    @staticmethod
    def _window_label(since: datetime | None, until: datetime | None) -> str:
        """Render the fetch window for the run log (empty when fully unbounded)."""
        if since is None and until is None:
            return ""
        lo = since.isoformat() if since else "beginning"
        hi = until.isoformat() if until else "now"
        return f" (window {lo} .. {hi})"

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

    def _fetch_pull_reviews_nested(
        self, context: dict[str, str], since: datetime | None, until: datetime | None
    ) -> list[dict[str, Any]]:
        """repo -> list its PRs (sorted updated desc, bounded by the window) -> each PR's reviews."""
        reviews_ep = ENDPOINTS["repository_pull_reviews"]
        pulls_ep = ENDPOINTS["repository_pulls"]
        owner, repo = context["owner"], context["repo"]
        pulls_path = f"/repos/{owner}/{repo}/pulls"
        pulls_params, pulls_stop = self._fetch_params(pulls_ep, since)
        rows: list[dict[str, Any]] = []
        for pr in self.client.fetch(pulls_ep, pulls_path, pulls_params, stop_paging=pulls_stop):
            if not record_in_window(pulls_ep, pr, since, until):
                continue  # PR outside the window — skip its reviews
            number = pr.get("number")
            if number is None:
                continue
            pr_context = {**context, "pull_number": str(number)}
            reviews_path = f"/repos/{owner}/{repo}/pulls/{number}/reviews"
            for payload in self.client.fetch(reviews_ep, reviews_path):
                rows.append(flatten_record(reviews_ep, payload, pr_context))
        return rows

    def _fetch_params(
        self, endpoint: Endpoint, since: datetime | None
    ) -> tuple[dict[str, Any], Callable[[dict[str, Any]], bool] | None]:
        """Build query params + the optional client-side stop-paging predicate.

        The ``since`` lower bound is pushed to GitHub server-side where the endpoint supports it
        (``incremental_param`` — e.g. /commits ``since``, /actions/runs ``created``), or enforced by
        sorting ``updated`` desc and stopping once a page crosses ``since`` (``incremental_sort``).
        The ``until`` upper bound is applied client-side in ``_collect_rows`` (``record_in_window``).
        """
        params: dict[str, Any] = dict(endpoint.default_params)
        for row_field, query_param in endpoint.filter_params.items():
            params[query_param] = getattr(self.cfg, row_field)

        stop_paging = None
        if since is not None and endpoint.incremental_param:
            params[endpoint.incremental_param] = endpoint.incremental_param_format(since.isoformat())
        elif since is not None and endpoint.incremental_sort:
            stop_paging = self._stop_paging_predicate(endpoint, since)
        return params, stop_paging

    @staticmethod
    def _stop_paging_predicate(endpoint: Endpoint, since: datetime) -> Callable[[dict[str, Any]], bool]:
        """Stop paging a ``sort=updated desc`` endpoint once a page's last record predates ``since``."""
        field = endpoint.time_field or "updated_at"

        def stop(last_record: dict[str, Any]) -> bool:
            raw = _dig(last_record, field)
            if not isinstance(raw, str):
                return False
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")) < since
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
        since: datetime | None,
        until: datetime | None,
    ) -> list[dict[str, Any]]:
        """Fetch, expand object-maps, drop PR rows, apply the [since, until] window, and flatten."""
        rows: list[dict[str, Any]] = []
        for payload in self.client.fetch(endpoint, path, params, stop_paging=stop_paging):
            for record in expand_records(endpoint, payload, context):
                if endpoint.drop_predicate and record.get(endpoint.drop_predicate) is not None:
                    continue  # /issues PR-split: drop pull-request rows (§4.4)
                if not record_in_window(endpoint, record, since, until):
                    continue  # outside the explicit fetch window
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
        # The first selected org scopes the picker (the UI reloads it as `organization` changes).
        # For the GraphQL template, fall back to gql_org when the REST organization field is empty.
        # No org at all → the authenticated user's repos.
        org = self.cfg.organization[0] if self.cfg.organization else (self.cfg.gql_org or "")
        return [SelectElement(value=name, label=name) for name in self._list_repo_fullnames(org)]

    @sync_action("listTeams")
    def list_teams(self) -> list[SelectElement]:
        if not self.cfg.organization:
            raise UserException("Select an organization before loading teams.")
        org = self.cfg.organization[0]
        teams = self.client.get_json_array_all(f"/orgs/{org}/teams", params={"per_page": "100"})
        # Value is the qualified ``org/team_slug`` (self-contained across multiple selected orgs).
        return [
            SelectElement(value=f"{org}/{slug}", label=f"{org} / {team.get('name') or slug}")
            for team in teams
            if (slug := team.get("slug"))
        ]

    # --------------------------------------------- shared enumeration (sync actions + run fan-out)
    # These paginate (get_json_array_all): the fan-out target list MUST cover every repo/team/org,
    # not just the first page, or an org with >100 repos silently loses every target past 100.
    def _list_org_logins(self) -> list[str]:
        orgs = self.client.get_json_array_all("/user/orgs", params={"per_page": "100"})
        return [login for o in orgs if (login := o.get("login"))]

    def _list_repo_fullnames(self, org: str) -> list[str]:
        path = f"/orgs/{org}/repos" if org else "/user/repos"
        repos = self.client.get_json_array_all(path, params={"per_page": "100"})
        return [name for r in repos if (name := r.get("full_name"))]

    def _list_team_slugs(self, org: str) -> list[str]:
        teams = self.client.get_json_array_all(f"/orgs/{org}/teams", params={"per_page": "100"})
        return [slug for t in teams if (slug := t.get("slug"))]

    # ------------------------------------------------------------- sync-action helpers
    def _call_rate_limit(self) -> None:
        """Single guarded GET /rate_limit; the client classifies status (401/404/rate-limit/network)."""
        self.client.get_response("/rate_limit")


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
