# Multi-target Rows, Fan-out & Relative Dates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a single config row cover many targets — `organization`/`repository`/`team` become multi-select with empty = "all in scope", `run()` fans out into one table per data source with per-target incremental state, and `Fetch Since` accepts relative dates as both a first-run floor and a rolling window.

**Architecture:** The `Configuration` model gains list-typed targets (back-compat-coerced from strings) and a relative-date parser. `component.run()` resolves a **target list** from the endpoint's `Target` kind, loops it (collecting into one table), and tracks a **per-target watermark**. `repository_pull_reviews` does a nested fan-out (repo → PRs → reviews). The row schema turns the three target fields into async multi-selects and groups optional fields.

**Tech Stack:** Python 3.14, Pydantic v2, `keboola.component`, `requests`, `tenacity`, `dateparser` (new), pytest + `keboola.datadirtest` (VCR).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-24-ex-github-v2-multi-target-fanout-design.md`.
- All user-facing failures are `UserException` (exit 1); never let an uncaught error reach exit 2.
- The endpoint registry stays the data-driven core — no per-endpoint methods.
- Output tables stay **headerless**; the authoritative `schema` manifest names columns + PKs.
- Verify Python with `uv run` (project Python 3.14), not system `python3`.
- Lint/format gate: `uv run ruff format` + `uv run ruff check` must pass before each commit (pre-commit runs them).
- Back-compat is mandatory: the 33 existing rows in project 5922 (single-string targets) and existing `last_run` state must keep working.

---

## File structure

- `pyproject.toml` — add `dateparser` runtime dep.
- `src/configuration.py` — list targets + coercion; relative-date parse; pull_reviews constraint.
- `src/component.py` — target enumeration helpers, fan-out `run()`, per-target state, pull_reviews nesting.
- `src/endpoints.py` — context-column audit for fanned endpoints (data only).
- `component_config/configRowSchema.json` — multi-select async fields + Optional section.
- `tests/test_configuration.py`, `tests/test_orchestrator.py`, `tests/test_component.py` — unit/orchestrator tests.
- `tests/functional/*` — datadir fixtures updated for list targets + a fan-out case.

---

### Task 1: Relative dates on `Fetch Since`

**Files:**
- Modify: `pyproject.toml:6-13` (dependencies)
- Modify: `src/configuration.py` (`_validate_initial_since`, add `parsed_initial_since`)
- Test: `tests/test_configuration.py`

**Interfaces:**
- Produces: `Configuration.parsed_initial_since() -> str | None` — the absolute ISO-8601 instant for `initial_since` ("5 days ago" → an instant), or `None` when empty. Used by `component._since()` (Task 4).

- [ ] **Step 1: Add the dependency.** In `pyproject.toml`, add to `dependencies` (keep sorted-ish):

```toml
    "dateparser>=1.2.0",
```

Run: `uv sync` — expected: resolves and installs `dateparser`.

- [ ] **Step 2: Write failing tests** in `tests/test_configuration.py`:

```python
def test_relative_initial_since_parses_to_instant():
    cfg = Configuration(auth_type="pat", initial_since="5 days ago", **{"#github_token": "ghp_x"})
    assert cfg.initial_since == "5 days ago"          # raw value preserved
    assert cfg.parsed_initial_since() is not None      # resolves to an absolute instant
    assert cfg.parsed_initial_since().endswith("+00:00") or cfg.parsed_initial_since().endswith("Z") is False

def test_iso_initial_since_still_parses():
    cfg = Configuration(auth_type="pat", initial_since="2024-01-31T00:00:00Z", **{"#github_token": "ghp_x"})
    assert cfg.parsed_initial_since().startswith("2024-01-31T00:00:00")

def test_unparseable_initial_since_raises():
    with pytest.raises(UserException, match="ISO-8601 or a relative phrase"):
        Configuration(auth_type="pat", initial_since="qwerty", **{"#github_token": "ghp_x"})

def test_empty_initial_since_parses_to_none():
    cfg = Configuration(auth_type="pat", initial_since="", **{"#github_token": "ghp_x"})
    assert cfg.parsed_initial_since() is None
```

Run: `uv run pytest tests/test_configuration.py -k initial_since -v` — expected: the new tests FAIL.

> Note: `test_malformed_initial_since_raises` (existing) uses `"yesterday"`, which is now **valid**. Update that test to a genuinely unparseable value (`"qwerty"`) and the match string to `"ISO-8601 or a relative phrase"`; keep `test_malformed_initial_since_out_of_range_month_raises` ("2024-13-01" is still rejected).

- [ ] **Step 3: Implement.** In `src/configuration.py`:

Replace the `datetime`/import block top and the `_validate_initial_since` body:

```python
import dateparser

_DATEPARSER_SETTINGS = {"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "UTC"}


def _parse_since(value: str) -> datetime | None:
    """Parse an ISO-8601 or relative ('5 days ago') string to a tz-aware datetime, else None."""
    if not value:
        return None
    return dateparser.parse(value, settings=_DATEPARSER_SETTINGS)
```

Change the validator to use it:

```python
    @model_validator(mode="after")
    def _validate_initial_since(self) -> Self:
        if self.initial_since and _parse_since(self.initial_since) is None:
            raise UserException(
                "Fetch Since must be ISO-8601 or a relative phrase (e.g. '2024-01-31T00:00:00Z' or '5 days ago')."
            )
        return self

    def parsed_initial_since(self) -> str | None:
        """The absolute ISO-8601 instant for ``initial_since`` (relative phrases resolved), or None."""
        parsed = _parse_since(self.initial_since)
        return parsed.isoformat() if parsed else None
```

- [ ] **Step 4: Run tests.** `uv run pytest tests/test_configuration.py -v` — expected: PASS (including the updated existing tests).
- [ ] **Step 5: Commit.**

```bash
uv run ruff format src/configuration.py && uv run ruff check src/configuration.py
git add pyproject.toml uv.lock src/configuration.py tests/test_configuration.py
git commit -m "feat: relative-date support for Fetch Since (dateparser)"
```

---

### Task 2: List-typed multi-select targets + back-compat coercion + pull_reviews constraint

**Files:**
- Modify: `src/configuration.py` (target fields, a `before` coercion validator, a pull_reviews `after` validator)
- Test: `tests/test_configuration.py`

**Interfaces:**
- Produces: `Configuration.organization: list[str]`, `Configuration.repository: list[str]`, `Configuration.team_slug: list[str]` (each default `[]`). A bare string coerces to a one-element list; `""`/absent → `[]`. Consumed by `component._resolve_targets()` (Task 3).

- [ ] **Step 1: Write failing tests** in `tests/test_configuration.py`:

```python
def test_string_target_coerces_to_list():
    cfg = Configuration(auth_type="pat", endpoint="repository", repository="octocat/Hello", **{"#github_token": "ghp_x"})
    assert cfg.repository == ["octocat/Hello"]

def test_empty_string_target_coerces_to_empty_list():
    cfg = Configuration(auth_type="pat", endpoint="repository_issues", repository="", **{"#github_token": "ghp_x"})
    assert cfg.repository == []

def test_list_target_passthrough():
    cfg = Configuration(auth_type="pat", endpoint="repository_issues",
                        repository=["a/x", "a/y"], **{"#github_token": "ghp_x"})
    assert cfg.repository == ["a/x", "a/y"]

def test_pull_reviews_with_pull_number_rejects_multiple_repos():
    with pytest.raises(UserException, match="exactly one repository"):
        Configuration(auth_type="pat", endpoint="repository_pull_reviews",
                      repository=["a/x", "a/y"], pull_number=5, **{"#github_token": "ghp_x"})

def test_pull_reviews_without_pull_number_allows_many_repos():
    cfg = Configuration(auth_type="pat", endpoint="repository_pull_reviews",
                        repository=["a/x", "a/y"], **{"#github_token": "ghp_x"})
    assert cfg.repository == ["a/x", "a/y"]
```

Run: `uv run pytest tests/test_configuration.py -k "target or pull_reviews" -v` — expected: FAIL.

- [ ] **Step 2: Implement.** In `src/configuration.py`, change the three target fields:

```python
    organization: list[str] = Field(default_factory=list)
    team_slug: list[str] = Field(default_factory=list)
    repository: list[str] = Field(default_factory=list)
```

Add a `before` validator that coerces scalars/blank to lists (runs before type validation):

```python
    @model_validator(mode="before")
    @classmethod
    def _coerce_target_lists(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in ("organization", "team_slug", "repository"):
            val = data.get(key)
            if val is None or val == "":
                data[key] = []
            elif isinstance(val, str):
                data[key] = [val]
        return data
```

Add the pull_reviews constraint (`after` validator):

```python
    @model_validator(mode="after")
    def _validate_pull_reviews(self) -> Self:
        if self.endpoint == "repository_pull_reviews" and self.pull_number is not None and len(self.repository) > 1:
            raise UserException(
                "Pull Request Reviews with a Pull Request Number set targets exactly one repository; "
                "select a single repository or clear the number to fetch reviews for all pull requests."
            )
        return self
```

> The existing `_validate_endpoint` is unchanged. Empty targets are now valid everywhere (they mean "all"); `component._resolve_targets` (Task 3) enforces what each endpoint needs at run time.

- [ ] **Step 3: Run tests.** `uv run pytest tests/test_configuration.py -v` — expected: PASS.

> Other suites reference string targets (`test_orchestrator.py` passes `repository="octocat/Hello"`). The `before` coercion keeps those working — they'll still pass after Task 4. Do not change them here.

- [ ] **Step 4: Commit.**

```bash
uv run ruff format src/configuration.py && uv run ruff check src/configuration.py
git add src/configuration.py tests/test_configuration.py
git commit -m "feat: list-typed multi-select targets with string back-compat coercion"
```

---

### Task 3: Target enumeration helpers + `_resolve_targets`

**Files:**
- Modify: `src/component.py` (refactor sync actions to share listers; add `_resolve_targets`)
- Test: `tests/test_component.py`

**Interfaces:**
- Consumes: `Configuration.organization/repository/team_slug` (lists, Task 2).
- Produces:
  - `Component._list_org_logins() -> list[str]` (all orgs via `/user/orgs`)
  - `Component._list_repo_fullnames(org: str) -> list[str]` (`/orgs/{org}/repos` or `/user/repos` when `org==""`)
  - `Component._list_team_slugs(org: str) -> list[str]` (`/orgs/{org}/teams`)
  - `Component._resolve_targets(endpoint: Endpoint) -> list[dict[str, str]]` — one **context dict** per target. Keys per `Target`: `NONE` → `[{}]`; `ORG` → `{"org": ...}`; `OWNER_REPO` → `{"owner","repo","repository"}`; `ORG_TEAM` → `{"org","team_slug"}`. Consumed by `run()` (Task 4).

- [ ] **Step 1: Write failing tests** in `tests/test_component.py` (mirror the `mock.patch("component.GitHubClient")` pattern from `test_orchestrator.py`). Add a helper that builds a Component with stubbed listers:

```python
import os, json
from pathlib import Path
from unittest import mock

def _component(tmp_path: Path, parameters: dict):
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": parameters}))
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        from component import Component
        with mock.patch("component.GitHubClient"):
            return Component()

PAT = {"#github_token": "ghp_x"}

def test_resolve_targets_selected_repos(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "repository_commits", "repository": ["a/x", "a/y"]})
    from endpoints import ENDPOINTS
    targets = comp._resolve_targets(ENDPOINTS["repository_commits"])
    assert [t["repository"] for t in targets] == ["a/x", "a/y"]
    assert targets[0]["owner"] == "a" and targets[0]["repo"] == "x"

def test_resolve_targets_empty_repo_fans_over_org_repos(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "repository_commits", "organization": ["acme"], "repository": []})
    comp.client.get_json_array.return_value = [{"full_name": "acme/one"}, {"full_name": "acme/two"}]
    targets = comp._resolve_targets(ENDPOINTS["repository_commits"])  # noqa: F821
    assert sorted(t["repository"] for t in targets) == ["acme/one", "acme/two"]

def test_resolve_targets_org_empty_fans_all_orgs(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "organization_members", "organization": []})
    comp.client.get_json_array.return_value = [{"login": "org1"}, {"login": "org2"}]
    targets = comp._resolve_targets(ENDPOINTS["organization_members"])  # noqa: F821
    assert sorted(t["org"] for t in targets) == ["org1", "org2"]

def test_resolve_targets_none_is_single_empty_context(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "user"})
    assert comp._resolve_targets(ENDPOINTS["user"]) == [{}]  # noqa: F821
```

Run: `uv run pytest tests/test_component.py -v` — expected: FAIL (`_resolve_targets` undefined).

- [ ] **Step 2: Implement** in `src/component.py`. Refactor the existing sync actions to call shared listers, then add `_resolve_targets`:

```python
    # --- shared enumeration (used by sync actions AND run() fan-out) ---
    def _list_org_logins(self) -> list[str]:
        return [login for o in self.client.get_json_array("/user/orgs") if (login := o.get("login"))]

    def _list_repo_fullnames(self, org: str) -> list[str]:
        path = f"/orgs/{org}/repos" if org else "/user/repos"
        repos = self.client.get_json_array(path, params={"per_page": "100"})
        return [name for r in repos if (name := r.get("full_name"))]

    def _list_team_slugs(self, org: str) -> list[str]:
        teams = self.client.get_json_array(f"/orgs/{org}/teams", params={"per_page": "100"})
        return [slug for t in teams if (slug := t.get("slug"))]

    @staticmethod
    def _repo_context(full_name: str) -> dict[str, str]:
        owner, _, repo = full_name.partition("/")
        return {"owner": owner, "repo": repo, "repository": full_name}

    def _resolve_targets(self, endpoint: Endpoint) -> list[dict[str, str]]:
        """One context dict per concrete target the row expands to (empty = all in scope)."""
        if endpoint.target == Target.NONE:
            return [{}]
        if endpoint.target == Target.ORG:
            orgs = self.cfg.organization or self._list_org_logins()
            return [{"org": org} for org in orgs]
        if endpoint.target in (Target.OWNER_REPO, Target.OWNER_REPO_PULL):
            repos = self.cfg.repository or self._all_repos_in_scope()
            return [self._repo_context(r) for r in repos]
        if endpoint.target == Target.ORG_TEAM:
            return self._resolve_team_targets()
        return [{}]

    def _all_repos_in_scope(self) -> list[str]:
        """All repos for the selected org(s), or all of the user's repos when no org is set."""
        orgs = self.cfg.organization or [""]
        seen: list[str] = []
        for org in orgs:
            seen.extend(self._list_repo_fullnames(org))
        return seen

    def _resolve_team_targets(self) -> list[dict[str, str]]:
        """team values are qualified 'org/team_slug'; empty = all teams in the selected org(s)."""
        if self.cfg.team_slug:
            out = []
            for qualified in self.cfg.team_slug:
                org, _, slug = qualified.partition("/")
                out.append({"org": org, "team_slug": slug})
            return out
        targets = []
        for org in self.cfg.organization or self._list_org_logins():
            targets.extend({"org": org, "team_slug": slug} for slug in self._list_team_slugs(org))
        return targets
```

Refactor the three sync actions to reuse the listers (keeps one source of truth):

```python
    @sync_action("listOrganizations")
    def list_organizations(self) -> list[SelectElement]:
        return [SelectElement(value=o, label=o) for o in self._list_org_logins()]

    @sync_action("listRepositories")
    def list_repositories(self) -> list[SelectElement]:
        org = self.cfg.organization[0] if self.cfg.organization else ""
        return [SelectElement(value=n, label=n) for n in self._list_repo_fullnames(org)]

    @sync_action("listTeams")
    def list_teams(self) -> list[SelectElement]:
        if not self.cfg.organization:
            raise UserException("Select an organization before loading teams.")
        org = self.cfg.organization[0]
        teams = self.client.get_json_array(f"/orgs/{org}/teams", params={"per_page": "100"})
        return [
            SelectElement(value=f"{org}/{slug}", label=f"{org} / {t.get('name') or slug}")
            for t in teams if (slug := t.get("slug"))
        ]
```

> `listRepositories`/`listTeams` use the first selected org to scope the picker (the UI reloads them as the org multi-select changes). The qualified `org/team_slug` value matches `_resolve_team_targets`.

- [ ] **Step 3: Run tests.** `uv run pytest tests/test_component.py tests/test_auth_and_sync.py -v` — expected: PASS. Fix `test_auth_and_sync.py` references to the old single-string `cfg.organization` if any (now a list).
- [ ] **Step 4: Commit.**

```bash
uv run ruff format src/component.py && uv run ruff check src/component.py
git add src/component.py tests/test_component.py
git commit -m "feat: target enumeration helpers and _resolve_targets fan-out resolver"
```

---

### Task 4: Fan-out `run()` + per-target since + per-target state + legacy migration

**Files:**
- Modify: `src/component.py` (`run`, `_build_path`, `_since`, state helpers)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `_resolve_targets` (Task 3), `Configuration.parsed_initial_since` (Task 1).
- Produces: state shape `{"targets": {<key>: <iso>}}`; `_target_key(endpoint, context) -> str`.

- [ ] **Step 1: Write failing tests** in `tests/test_orchestrator.py` (reuse `_run_component`/`_read_table`). Extend `_run_component` so `fetch` can vary per call:

```python
def test_fanout_two_repos_into_one_table(tmp_path):
    params = {**PAT, "endpoint": "repository_commits", "repository": ["a/x", "a/y"], "load_type": "full_load"}
    # fetch is called once per target; return one commit each, tagged later by context
    payloads = [[{"sha": "s1", "commit": {"message": "m"}}], [{"sha": "s2", "commit": {"message": "m"}}]]
    data_dir = _write_datadir(tmp_path, params)
    env = {"KBC_DATADIR": str(data_dir), "KBC_DATA_TYPE_SUPPORT": "authoritative"}
    with mock.patch.dict(os.environ, env):
        from component import Component
        with mock.patch("component.GitHubClient"):
            comp = Component()
        comp.client.fetch.side_effect = [iter(p) for p in payloads]
        comp.run()
    rows, manifest = _read_table(data_dir, "repository_commits")
    header = [c["name"] for c in manifest["schema"]]
    repo_idx = header.index("repository")
    assert sorted(r[repo_idx] for r in rows) == ["a/x", "a/y"]

def test_full_load_applies_fetch_since_every_run(tmp_path):
    params = {**PAT, "endpoint": "repository_commits", "repository": ["a/x"],
              "load_type": "full_load", "initial_since": "2024-01-01T00:00:00Z"}
    payload = [{"sha": "s1"}]
    _data_dir, comp = _run_component(tmp_path, params, payload)
    assert _fetch_params(comp)["since"] == "2024-01-01T00:00:00+00:00"

def test_per_target_watermark_written(tmp_path):
    params = {**PAT, "endpoint": "repository_commits", "repository": ["a/x"], "load_type": "incremental_load"}
    payload = [{"sha": "s1"}]
    data_dir, _comp = _run_component(tmp_path, params, payload)
    state = json.loads((data_dir / "out" / "state.json").read_text())
    assert "a/x" in state["targets"]

def test_legacy_last_run_state_seeds_target_since(tmp_path):
    params = {**PAT, "endpoint": "repository_commits", "repository": ["a/x"], "load_type": "incremental_load"}
    payload = [{"sha": "s1"}]
    _data_dir, comp = _run_component(tmp_path, params, payload, state={"last_run": "2024-05-01T00:00:00+00:00"})
    assert _fetch_params(comp)["since"] == "2024-05-01T00:00:00+00:00"
```

Run: `uv run pytest tests/test_orchestrator.py -k "fanout or fetch_since or watermark or legacy" -v` — expected: FAIL.

- [ ] **Step 2: Implement** the new `run()` and helpers in `src/component.py`:

```python
    def run(self) -> None:
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
            try:
                since = self._since(endpoint, key, state) if incremental else self._full_load_since(endpoint)
                all_rows.extend(self._fetch_target(endpoint, context, since))
            except _TargetSkipped as skip:
                logging.warning("Skipping target '%s': %s", key, skip)
                continue
            succeeded.append(key)

        self._write_table(endpoint, all_rows, incremental)
        if incremental:
            self._advance_state(state, succeeded, run_started_at)

    def _fetch_target(self, endpoint: Endpoint, context: dict[str, str], since: str | None) -> list[dict[str, Any]]:
        path = self._build_path(endpoint, context)
        params, stop_paging = self._fetch_params(endpoint, since)
        return self._collect_rows(endpoint, path, context, params, stop_paging)
```

Replace `_build_path` to take the resolved `context` (drop the old single-target `_split_repository`/`_require_targets`; presence is guaranteed by `_resolve_targets`):

```python
    def _build_path(self, endpoint: Endpoint, context: dict[str, str]) -> str:
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
```

> `_collect_rows` already takes `context` and fills synthetic columns; it is unchanged. The `repository` context column is set by `_repo_context` (Task 3), so commits/branches/etc. tag correctly.

Add since/state helpers and the skip signal:

```python
    @staticmethod
    def _target_key(endpoint: Endpoint, context: dict[str, str]) -> str:
        if endpoint.target in (Target.OWNER_REPO, Target.OWNER_REPO_PULL):
            return context.get("repository", "")
        if endpoint.target == Target.ORG_TEAM:
            return f"{context.get('org', '')}/{context.get('team_slug', '')}"
        return context.get("org", "")

    def _since(self, endpoint: Endpoint, key: str, state: dict[str, Any]) -> str | None:
        """Incremental lower bound for one target: per-target watermark, legacy last_run, then Fetch Since."""
        targets = state.get("targets") or {}
        return targets.get(key) or state.get("last_run") or self.cfg.parsed_initial_since()

    def _full_load_since(self, endpoint: Endpoint) -> str | None:
        """Full Load honours Fetch Since on EVERY run (rolling window); no watermark, no state."""
        return self.cfg.parsed_initial_since()

    def _advance_state(self, state: dict[str, Any], succeeded: list[str], run_started_at: str) -> None:
        targets = dict(state.get("targets") or {})
        for key in succeeded:
            targets[key] = run_started_at
        self.write_state_file({"targets": targets})
```

Add the skip signal near the top of the module:

```python
class _TargetSkipped(Exception):
    """A single fan-out target was inaccessible (404/403) — skip it, keep the rest of the row."""
```

Make a single-target 404/403 raise it. In `client._classify_response` the terminal 404/non-rate 403 raise `UserException`; to skip just one target, catch those in `_fetch_target` ONLY when the row fans out over more than one target OR the field was empty (all). Simplest: catch in `run()` loop:

```python
            except UserException as exc:
                if self._is_single_target_access_error(exc) and self._fans_out(endpoint):
                    logging.warning("Skipping target '%s': %s", key, exc)
                    continue
                raise
```

with helpers:

```python
    def _fans_out(self, endpoint: Endpoint) -> bool:
        return endpoint.target != Target.NONE and not (self.cfg.repository and len(self.cfg.repository) == 1)

    @staticmethod
    def _is_single_target_access_error(exc: UserException) -> bool:
        msg = str(exc)
        return "HTTP 404" in msg or "not found" in msg.lower()
```

> Remove the now-unused `_TargetSkipped` if you take the `UserException`-catch route — keep ONE mechanism. Recommended: the `UserException`-catch route above; delete `_TargetSkipped` and the `except _TargetSkipped` line. Token/rate-limit `UserException`s do not contain "404"/"not found", so they still abort.

- [ ] **Step 3: Run tests.** `uv run pytest tests/test_orchestrator.py -v` — expected: PASS. The legacy single-string fixtures still pass via coercion; `test_missing_target_raises_userexception` must change: an empty `repository` now means "all", so it no longer raises. Replace it with a test that an empty repo on a repo endpoint triggers enumeration (stub `get_json_array` → `[]` → zero targets → empty table, no exception). Update it:

```python
def test_empty_repository_means_all_no_longer_raises(tmp_path):
    params = {**PAT, "endpoint": "repository_issues", "repository": [], "organization": ["acme"], "load_type": "full_load"}
    data_dir = _write_datadir(tmp_path, params)
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        from component import Component
        with mock.patch("component.GitHubClient"):
            comp = Component()
        comp.client.get_json_array.return_value = []   # no repos in scope
        comp.client.fetch.return_value = iter([])
        comp.run()  # must not raise
    rows, _ = _read_table(data_dir, "repository_issues")
    assert rows == []
```

- [ ] **Step 4: Run the full suite.** `uv run pytest -v` — expected: PASS (fix any remaining string-target assumptions).
- [ ] **Step 5: Commit.**

```bash
uv run ruff format src/component.py && uv run ruff check src/component.py
git add src/component.py tests/test_orchestrator.py
git commit -m "feat: fan-out run() with per-target watermark and rolling Fetch Since"
```

---

### Task 5: `repository_pull_reviews` nested fan-out

**Files:**
- Modify: `src/component.py` (`run`/`_fetch_target` special path for pull reviews)
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: resolved repo targets (Task 3), per-repo PR-scan since (Task 4).
- Produces: per-PR review rows tagged with `owner`/`repo`/`pull_number`.

- [ ] **Step 1: Write failing test** in `tests/test_orchestrator.py`:

```python
def test_pull_reviews_nested_fanout_over_prs(tmp_path):
    params = {**PAT, "endpoint": "repository_pull_reviews", "repository": ["a/x"], "load_type": "full_load"}
    data_dir = _write_datadir(tmp_path, params)
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir), "KBC_DATA_TYPE_SUPPORT": "authoritative"}):
        from component import Component
        with mock.patch("component.GitHubClient"):
            comp = Component()
        # 1st fetch = the PR listing for a/x (2 PRs); next 2 = each PR's reviews
        comp.client.fetch.side_effect = [
            iter([{"number": 11, "updated_at": "2024-01-02T00:00:00Z"},
                  {"number": 12, "updated_at": "2024-01-01T00:00:00Z"}]),
            iter([{"id": 101, "state": "APPROVED", "user": {"login": "u", "id": 1}}]),
            iter([{"id": 102, "state": "COMMENTED", "user": {"login": "v", "id": 2}}]),
        ]
        comp.run()
    rows, manifest = _read_table(data_dir, "repository_pull_reviews")
    header = [c["name"] for c in manifest["schema"]]
    assert len(rows) == 2
    assert "pull_number" in header
```

Run: `uv run pytest tests/test_orchestrator.py -k pull_reviews_nested -v` — expected: FAIL.

- [ ] **Step 2: Implement.** Add the nested path to `_fetch_target` (dispatch on the endpoint name). Add a `pulls` descriptor lookup to scan PRs:

```python
    def _fetch_target(self, endpoint: Endpoint, context: dict[str, str], since: str | None) -> list[dict[str, Any]]:
        if endpoint.name == "repository_pull_reviews" and self.cfg.pull_number is None:
            return self._fetch_pull_reviews_nested(context, since)
        path = self._build_path(endpoint, self._with_pull_number(context))
        params, stop_paging = self._fetch_params(endpoint, since)
        return self._collect_rows(endpoint, path, context, params, stop_paging)

    def _with_pull_number(self, context: dict[str, str]) -> dict[str, str]:
        if self.cfg.pull_number is not None:
            return {**context, "pull_number": str(self.cfg.pull_number)}
        return context

    def _fetch_pull_reviews_nested(self, context: dict[str, str], since: str | None) -> list[dict[str, Any]]:
        """repo -> list its PRs (sorted updated desc, bounded by `since`) -> each PR's reviews."""
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
```

Add `pull_number` to the `repository_pull_reviews` context columns in `endpoints.py` so it lands in the table (Task 7 covers the audit; add it here if not already):

```python
        context_columns=("owner", "repo", "pull_number"),
```

- [ ] **Step 3: Run tests.** `uv run pytest tests/test_orchestrator.py -k pull_reviews -v` — expected: PASS.
- [ ] **Step 4: Commit.**

```bash
uv run ruff format src/component.py src/endpoints.py && uv run ruff check src/component.py src/endpoints.py
git add src/component.py src/endpoints.py tests/test_orchestrator.py
git commit -m "feat: nested fan-out for repository_pull_reviews (empty pull_number = all PRs)"
```

---

### Task 6: Row schema — multi-select async fields + Optional section

**Files:**
- Modify: `component_config/configRowSchema.json`
- Test: schema JSON validity + Ctrl+D sandbox (manual)

- [ ] **Step 1:** Convert `organization`, `team_slug`, `repository` to async multi-selects (`type: array`, `uniqueItems`, `items.enum: []`, `options.async` preserved). Set `organization`'s `dependencies.endpoint` to the **union** of org-scoped AND repo-scoped endpoints (so it shows as the scope field for repo endpoints too). Retitle `team_slug` → "Teams" with qualified values. Example for `repository`:

```json
"repository": {
  "type": "array",
  "title": "Repositories",
  "format": "select",
  "uniqueItems": true,
  "items": { "type": "string", "enum": [] },
  "description": "Leave empty to extract all repositories in the selected organization(s).",
  "propertyOrder": 40,
  "options": {
    "async": { "label": "Load Repositories", "action": "listRepositories", "autoload": ["organization", "endpoint"] },
    "tooltip": "Pick one or more repositories (empty = all in the selected organization(s), or all your repos if no org).",
    "dependencies": { "endpoint": [ "repository", "repository_branches", "repository_tags", "repository_commits", "repository_contributors", "repository_collaborators", "repository_languages", "repository_topics", "repository_releases", "repository_issues", "repository_issue_comments", "repository_pulls", "repository_pull_reviews", "repository_pull_review_comments", "repository_labels", "repository_milestones", "repository_stargazers", "repository_subscribers", "repository_forks", "repository_workflows", "repository_workflow_runs", "repository_deployments", "repository_webhooks" ] }
  }
}
```

- [ ] **Step 2:** Move `load_type`, `initial_since`, `issue_state`, `pull_state`, `milestone_state` into a section object `optional_settings` (`"type": "object"`, `"title": "Optional settings"`, `"format": "grid"`, `propertyOrder: 60`), keeping each inner field's `options.dependencies`. Add `repository_pull_reviews` to the `endpoint` dependency arrays of `load_type` and `initial_since`. Retitle `initial_since` → "Fetch Since"; update its `description`/`tooltip` per spec §3.3.

- [ ] **Step 3: Validate JSON.** Run: `uv run python -c "import json; json.load(open('component_config/configRowSchema.json'))"` — expected: no error.
- [ ] **Step 4: Manual sandbox check.** Paste into the Keboola Ctrl+D schema sandbox: confirm the multi-selects render, `organization` shows for repo endpoints, the Optional section collapses, and (if the model flattens `optional_settings.*`) the params land flat. If the platform nests them, add a `before` hoist to `Configuration` (`data |= data.pop("optional_settings", {})`) and a test.
- [ ] **Step 5: Commit.**

```bash
git add component_config/configRowSchema.json
git commit -m "feat: multi-select async target fields + Optional settings section"
```

---

### Task 7: Registry context-column audit

**Files:**
- Modify: `src/endpoints.py` (add missing `owner`/`repo` context columns; verify PKs)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test** asserting every `OWNER_REPO` endpoint emits `owner` + `repo`:

```python
def test_all_owner_repo_endpoints_emit_context_columns():
    from endpoints import ENDPOINTS, Target, output_columns
    for ep in ENDPOINTS.values():
        if ep.target in (Target.OWNER_REPO, Target.OWNER_REPO_PULL):
            cols = output_columns(ep)
            assert "owner" in cols and "repo" in cols, ep.name
```

Run: `uv run pytest tests/test_orchestrator.py -k context_columns -v` — expected: FAIL for any endpoint missing them.

- [ ] **Step 2: Implement.** For each failing endpoint, add `owner`/`repo` to its `context_columns` tuple (e.g. those currently `("owner", "repo")` already pass; add to any that lack it). Confirm non-globally-unique PKs include `repository`. Make only data edits.
- [ ] **Step 3: Run tests.** `uv run pytest tests/test_orchestrator.py -v` — expected: PASS.
- [ ] **Step 4: Commit.**

```bash
uv run ruff format src/endpoints.py && uv run ruff check src/endpoints.py
git add src/endpoints.py tests/test_orchestrator.py
git commit -m "fix: ensure every fanned repo endpoint emits owner/repo context columns"
```

---

### Task 8: Functional fixtures + smoke

**Files:**
- Modify: `tests/functional/*/source/data/config.json` (list-shaped targets), `tests/setup/configs.json`
- Test: `uv run pytest tests/test_functional.py`

- [ ] **Step 1:** The back-compat coercion keeps string fixtures green, so existing cassette tests should still pass unchanged — run `uv run pytest tests/test_functional.py -v` first to confirm. Only update fixtures that assert the NEW behaviour.
- [ ] **Step 2:** Add one datadir fixture `tests/functional/40_fanout_two_repos/` exercising two repos into one table (stub-free is not possible for VCR; use a recorded cassette with two repos, or assert via the orchestrator unit test already added in Task 4 and skip a new VCR case if no live token is available — note the gap in the commit message).
- [ ] **Step 3:** Run: `uv run pytest -v` — expected: PASS.
- [ ] **Step 4: Commit.**

```bash
git add tests/
git commit -m "test: functional fixtures for list-shaped targets and fan-out"
```

---

## Smoke test (after implementation)

1. **Local datadir run** of a no-target endpoint (proves end-to-end without targets): build a `data/` with `{"parameters": {"#github_token": "<PAT>", "endpoint": "user"}}` and run `KBC_DATADIR=data uv run python src/component.py`; confirm `data/out/tables/user.csv` + manifest.
2. **Local fan-out run**: `{"endpoint": "repository_commits", "organization": ["<org>"], "repository": [], "load_type": "full_load", "initial_since": "10 days ago"}` against a small org; confirm one table with a populated `repository` column and the resolved-target-count log line.
3. **Platform**: push the branch (CI builds the image + deploys to `keboola.ex-github-v2` on tag), open a row in project 5922, confirm the multi-selects render and an empty-repo incremental run succeeds where it previously errored.

## Self-review notes

- Spec coverage: §3 schema → Task 6; §3.3 relative dates → Task 1; §4 fan-out → Tasks 3-4; §4.2 since matrix → Task 4 (`_since`/`_full_load_since`); §5 state → Task 4; §6 context columns → Task 7; §7.1 pull_reviews → Task 5; §8 model → Tasks 1-2; §9 dep → Task 1; §10 back-compat → Task 2 + Task 8; §11 testing → spread across tasks.
- One skip mechanism only (`UserException`-catch in `run()`; drop `_TargetSkipped`).
- Type consistency: `_resolve_targets` returns `list[dict[str,str]]`; `_build_path`/`_target_key`/`_fetch_target` all consume that dict.
