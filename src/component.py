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

from keboola.component.base import ComponentBase, sync_action
from keboola.component.dao import BaseType, ColumnDefinition, SupportedDataTypes
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement

from client import DEFAULT_TIMEOUT, GitHubClient
from configuration import AuthType, Configuration
from credentials import resolve_token
from endpoints import ENDPOINTS, Endpoint, Target, expand_records, flatten_record, output_columns

STATE_LAST_RUN = "last_run"


def _redact_authorization(request):
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


def _build_vcr_sanitizers() -> list:
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
        endpoint = self._endpoint()
        path, context = self._build_path(endpoint)
        run_started_at = datetime.now(UTC)
        since = self._since() if self.cfg.incremental_load else None
        params, stop_paging = self._fetch_params(endpoint, since)

        rows = self._collect_rows(endpoint, path, context, params, stop_paging)
        self._write_table(endpoint, rows)

        if self.cfg.incremental_load:
            self.write_state_file({STATE_LAST_RUN: run_started_at.isoformat()})

    # ------------------------------------------------------------------- run() helpers
    def _resolve_token(self) -> str:
        oauth_credentials = None
        if self.cfg.auth_type == AuthType.oauth:
            oauth_credentials = self._oauth_credentials()
        return resolve_token(self.cfg, oauth_credentials)

    def _oauth_credentials(self) -> dict | None:
        """The broker-injected ``authorization.oauth_api.credentials`` (guarded for absence)."""
        config_data = self.configuration.config_data or {}
        return config_data.get("authorization", {}).get("oauth_api", {}).get("credentials")

    def _endpoint(self) -> Endpoint:
        endpoint = ENDPOINTS.get(self.cfg.endpoint)
        if endpoint is None:
            raise UserException(
                f"Unknown endpoint '{self.cfg.endpoint}'. Choose one of: {', '.join(sorted(ENDPOINTS))}."
            )
        return endpoint

    def _build_path(self, endpoint: Endpoint) -> tuple[str, dict[str, str]]:
        """Substitute the row's target params into the path; return (path, context columns)."""
        owner, repo = self._split_repository()
        values = {
            "org": self.cfg.organization,
            "team_slug": self.cfg.team_slug,
            "owner": owner,
            "repo": repo,
            "pull_number": self.cfg.pull_number,
        }
        self._require_targets(endpoint, values)
        try:
            path = endpoint.path_template.format(**values)
        except KeyError as exc:
            raise UserException(f"Missing path parameter {exc} for endpoint '{endpoint.name}'.") from exc

        context = {
            "org": self.cfg.organization,
            "owner": owner,
            "repo": repo,
            "repository": f"{owner}/{repo}" if owner and repo else "",
            "team_slug": self.cfg.team_slug,
        }
        return path, context

    def _split_repository(self) -> tuple[str, str]:
        """The async ``repository`` select carries ``owner/repo``; split it."""
        if "/" in self.cfg.repository:
            owner, repo = self.cfg.repository.split("/", 1)
            return owner, repo
        return "", ""

    @staticmethod
    def _require_targets(endpoint: Endpoint, values: dict) -> None:
        """Belt-and-braces validation behind the schema's conditional fields."""
        required: dict[Target, list[str]] = {
            Target.NONE: [],
            Target.ORG: ["org"],
            Target.ORG_TEAM: ["org", "team_slug"],
            Target.OWNER_REPO: ["owner", "repo"],
            Target.OWNER_REPO_PULL: ["owner", "repo", "pull_number"],
        }
        missing = [field for field in required[endpoint.target] if not values.get(field)]
        if missing:
            raise UserException(
                f"Endpoint '{endpoint.name}' requires {', '.join(missing)} "
                "— set the organization / repository / team / pull request number on the row."
            )

    def _since(self) -> str | None:
        """Lower bound for incremental: stored watermark, else the row's ``initial_since``."""
        state = self.get_state_file() or {}
        return state.get(STATE_LAST_RUN) or self.cfg.initial_since or None

    def _fetch_params(self, endpoint: Endpoint, since: str | None):
        """Build query params + the optional client-side stop-paging predicate."""
        params = dict(endpoint.default_params)
        for row_field, query_param in endpoint.filter_params.items():
            params[query_param] = getattr(self.cfg, row_field)

        stop_paging = None
        if since and endpoint.incremental_param:
            params[endpoint.incremental_param] = since
        elif since and endpoint.incremental_sort:
            # No server `since` (e.g. /pulls) — sort desc and stop once we cross the watermark.
            stop_paging = self._stop_paging_predicate(since)
        return params, stop_paging

    @staticmethod
    def _stop_paging_predicate(since: str):
        watermark = datetime.fromisoformat(since)

        def stop(last_record: dict) -> bool:
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
        params: dict,
        stop_paging: Callable[[dict], bool] | None,
    ) -> list[dict]:
        """Fetch, expand object-maps, drop PR rows, and flatten to the declared columns."""
        rows: list[dict] = []
        for payload in self.client.fetch(endpoint, path, params, stop_paging=stop_paging):
            for record in expand_records(endpoint, payload, context):
                if endpoint.drop_predicate and record.get(endpoint.drop_predicate) is not None:
                    continue  # /issues PR-split: drop pull-request rows (§4.4)
                rows.append(flatten_record(endpoint, record, context))
        return rows

    def _write_table(self, endpoint: Endpoint, rows: list[dict]) -> None:
        columns = output_columns(endpoint)
        table = self.create_out_table_definition(
            f"{endpoint.name}.csv",
            primary_key=endpoint.primary_key,
            incremental=self.cfg.incremental_load,
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
        orgs = self._get_json_array("/user/orgs")
        return [SelectElement(value=o["login"], label=o["login"]) for o in orgs]

    @sync_action("listRepositories")
    def list_repositories(self) -> list[SelectElement]:
        path = f"/orgs/{self.cfg.organization}/repos" if self.cfg.organization else "/user/repos"
        repos = self._get_json_array(path, params={"per_page": "100"})
        return [SelectElement(value=r["full_name"], label=r["full_name"]) for r in repos]

    @sync_action("listTeams")
    def list_teams(self) -> list[SelectElement]:
        if not self.cfg.organization:
            raise UserException("Select an organization before loading teams.")
        teams = self._get_json_array(f"/orgs/{self.cfg.organization}/teams", params={"per_page": "100"})
        return [SelectElement(value=t["slug"], label=t["name"]) for t in teams]

    # ------------------------------------------------------------- sync-action helpers
    def _call_rate_limit(self) -> dict:
        response = self.client.session.get(f"{self.client.base_url}/rate_limit", timeout=DEFAULT_TIMEOUT)
        if response.status_code == 401:
            raise UserException("Invalid or expired token (HTTP 401).")
        if response.status_code >= 400:
            raise UserException(f"GitHub connection check failed (HTTP {response.status_code}).")
        return self._parse_json(response, "/rate_limit")

    def _get_json_array(self, path: str, params: dict | None = None) -> list[dict]:
        response = self.client.session.get(f"{self.client.base_url}{path}", params=params, timeout=DEFAULT_TIMEOUT)
        if response.status_code == 401:
            raise UserException("Invalid or expired token (HTTP 401).")
        if response.status_code >= 400:
            raise UserException(f"GitHub request to {path} failed (HTTP {response.status_code}).")
        body = self._parse_json(response, path)
        return body if isinstance(body, list) else []

    @staticmethod
    def _parse_json(response, path: str):
        """Guard a sync-action JSON parse: a malformed/empty body becomes a clean UserException (exit 1)."""
        try:
            return response.json()
        except ValueError as exc:
            raise UserException(
                f"GitHub returned a malformed or empty response for {path} (HTTP {response.status_code})."
            ) from exc


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
