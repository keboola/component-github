# GraphQL Query Data Source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `graphql_query` data source that runs a user-authored (or templated) GitHub GraphQL query and auto-flattens the result into one Keboola table — without touching the 33 typed REST endpoints.

**Architecture:** A parallel path. `component.run()` branches on a reserved `endpoint == "graphql_query"` to `_run_graphql()`, which builds a query (custom or from a template), calls a new `GitHubClient.graphql()` (POST `/graphql`, cursor pagination, GraphQL-error handling), flattens each row node via pure helpers in a new `graphql_source.py`, and writes a dynamic all-`STRING` headerless table. The REST registry, client `fetch()`, config, schema, and tests are otherwise untouched.

**Tech Stack:** Python 3.14 (run via `uv run python`), `requests`, `tenacity`, `pydantic` v2, `keboola.component`; tests with `pytest`, `responses` (HTTP mock), `keboola.datadirtest` VCR (functional). Lint/format/type via `ruff` + `ty` (pre-commit).

## Global Constraints

_Every task's requirements implicitly include this section._

- **Run/verify with `uv run python` / `uv run pytest`** — the project Python is 3.14; do not use system `python3`.
- **All GraphQL output columns are `STRING`** in v1 (no type inference).
- **Headerless invariant:** output CSV carries no header; the manifest names columns + primary key. `has_header=False`.
- **Stateless:** no `state.json` read/write for the GraphQL path.
- **Reuse auth + HTTP handling:** same Bearer token (PAT/OAuth) and the existing `_classify_response` + tenacity retry; do not build a second rate-limit mechanism.
- **Additive / back-compat:** a new `endpoint` enum value + new optional config fields only. No existing table name, schema, PK, validator, or REST behavior changes.
- **Clean exit-1 boundary:** every bad config / GraphQL error surfaces as `keboola.component.exceptions.UserException` (never an uncaught exception).
- **New module name is `graphql_source.py`** (NOT `graphql.py`/`gql.py`) to avoid clashing with the `graphql-core`/`gql` PyPI packages.
- **Lint clean:** `uv run ruff format` + `uv run ruff check` pass before each commit (pre-commit enforces this).

---

## File Structure

- **Create `src/graphql_source.py`** — GraphQL-specific pure logic + template registry: `sanitize_alias`, `flatten_graphql_node`, `collect_columns`, `read_path`, `connection_path`, `detect_rows_path`, `GraphQLTemplate`, `TEMPLATES`. No imports of `configuration`/`client`/`component` (avoids cycles). Mirrors how `endpoints.py` holds REST data/transforms.
- **Create `src/component_graphql_notes` → N/A** (no extra file; orchestration lives in `component.py`).
- **Modify `src/endpoints.py`** — add `GRAPHQL_ENDPOINT = "graphql_query"` constant (single source of truth, imported by both `configuration.py` and `component.py`).
- **Modify `src/configuration.py`** — GraphQL config fields, coercion + validation; allow the reserved endpoint.
- **Modify `src/client.py`** — `graphql()` + `_graphql_post`/`_graphql_attempt`/`_graphql_extract_data`; extract a shared `_retrying()`/`_run_with_retry()` from `_request`.
- **Modify `src/component.py`** — `GRAPHQL_ENDPOINT` dispatch in `run()`; `_run_graphql`, `_graphql_template_params`, `_write_dynamic_table`.
- **Modify `component_config/configRowSchema.json`** — `graphql_query` enum value + GraphQL fields with `options.dependencies`.
- **Modify `component_config/configuration_description.md` + `README.md`** — document the new data source.
- **Create tests:** `tests/test_graphql_source.py` (new), additions to `tests/test_client.py`, `tests/test_configuration.py`, `tests/test_component.py`, and a functional case `tests/functional/40_graphql_ai_config_files/`.

---

## Task 1: Reserved endpoint constant + config model fields & validation

**Files:**
- Modify: `src/endpoints.py` (add constant near `ENDPOINTS = {}`)
- Modify: `src/configuration.py`
- Test: `tests/test_configuration.py`

**Interfaces:**
- Consumes: existing `Configuration` model, `ENDPOINTS`.
- Produces:
  - `endpoints.GRAPHQL_ENDPOINT: str = "graphql_query"`
  - `configuration.QueryTemplate` StrEnum: `custom`, `org_ai_config_files`, `org_pull_request_reviews`
  - New `Configuration` fields: `query_template: QueryTemplate`, `gql_query: str`, `gql_variables: dict[str, Any]`, `gql_row_path: str`, `output_table: str`, `primary_key: list[str]`, `gql_org: str`, `gql_repo: str`, `gql_filenames: list[str]`.

- [ ] **Step 1: Add the constant**

In `src/endpoints.py`, immediately after `ENDPOINTS: dict[str, Endpoint] = {}`:

```python
#: Reserved Data Source value for the GraphQL passthrough — NOT a typed REST registry entry.
#: component.run() dispatches on it; configuration accepts it; it is intentionally absent from ENDPOINTS.
GRAPHQL_ENDPOINT = "graphql_query"
```

- [ ] **Step 2: Write failing config tests**

Append to `tests/test_configuration.py`:

```python
# --------------------------------------------------- GraphQL query data source

GQL = {"#github_token": "ghp_x", "endpoint": "graphql_query"}


def test_graphql_endpoint_is_accepted():
    """The reserved graphql_query endpoint is valid even though it is not in ENDPOINTS."""
    cfg = Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="{ viewer { login } }", output_table="t")
    assert cfg.endpoint == "graphql_query"


def test_graphql_custom_requires_query_and_output_table():
    with pytest.raises(UserException, match="GraphQL query"):
        Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="", output_table="t")
    with pytest.raises(UserException, match="Output table"):
        Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="{ viewer { login } }", output_table="")


def test_graphql_variables_parsed_from_json_string():
    cfg = Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="t",
                        gql_variables='{"org": "acme"}')
    assert cfg.gql_variables == {"org": "acme"}


def test_graphql_variables_blank_is_empty_dict():
    cfg = Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="t", gql_variables="")
    assert cfg.gql_variables == {}


def test_graphql_variables_bad_json_raises():
    with pytest.raises(UserException, match="GraphQL Variables"):
        Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="t", gql_variables="{not json")


def test_graphql_primary_key_string_coerces_to_list():
    cfg = Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="t",
                        primary_key="nameWithOwner, number")
    assert cfg.primary_key == ["nameWithOwner", "number"]


def test_graphql_output_table_rejects_unsafe_name():
    with pytest.raises(UserException, match="Output table"):
        Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="bad name!")


def test_graphql_ai_config_template_requires_org():
    with pytest.raises(UserException, match="Organization"):
        Configuration(auth_type="pat", **GQL, query_template="org_ai_config_files", gql_org="")


def test_graphql_ai_config_template_rejects_filename_with_quote():
    with pytest.raises(UserException, match="file name"):
        Configuration(auth_type="pat", **GQL, query_template="org_ai_config_files", gql_org="acme",
                       gql_filenames=['AGENTS.md', 'bad"name'])


def test_graphql_pr_reviews_template_requires_org_and_repo():
    with pytest.raises(UserException, match="Repository"):
        Configuration(auth_type="pat", **GQL, query_template="org_pull_request_reviews", gql_org="acme", gql_repo="")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_configuration.py -k graphql -v`
Expected: FAIL (fields/validators not defined; `graphql_query` rejected by `_validate_endpoint`).

- [ ] **Step 4: Implement the model changes**

In `src/configuration.py`:

Add imports / enum (near the other `StrEnum`s, and import the constant + `json`/`re`):

```python
import json
import re
...
from endpoints import ENDPOINTS, GRAPHQL_ENDPOINT


class QueryTemplate(StrEnum):
    custom = "custom"
    org_ai_config_files = "org_ai_config_files"
    org_pull_request_reviews = "org_pull_request_reviews"


_SAFE_TABLE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
```

Add fields to `Configuration` (after the existing row fields):

```python
    # --- GraphQL query data source (only meaningful when endpoint == GRAPHQL_ENDPOINT) ---
    query_template: QueryTemplate = QueryTemplate.custom
    gql_query: str = ""
    gql_variables: dict[str, Any] = Field(default_factory=dict)
    gql_row_path: str = ""
    output_table: str = ""
    primary_key: list[str] = Field(default_factory=list)
    gql_org: str = ""
    gql_repo: str = ""
    gql_filenames: list[str] = Field(default_factory=lambda: ["AGENTS.md", "CLAUDE.md"])
```

Add a before-validator (after `_coerce_target_lists`):

```python
    @model_validator(mode="before")
    @classmethod
    def _coerce_graphql_inputs(cls, data: Any) -> Any:
        """Coerce GraphQL string inputs: variables JSON-string -> dict; comma lists -> list."""
        if not isinstance(data, dict):
            return data
        raw_vars = data.get("gql_variables")
        if isinstance(raw_vars, str):
            text = raw_vars.strip()
            if not text:
                data["gql_variables"] = {}
            else:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise UserException(f"GraphQL Variables must be a JSON object: {exc}.") from exc
                if not isinstance(parsed, dict):
                    raise UserException("GraphQL Variables must be a JSON object (e.g. {\"org\": \"acme\"}).")
                data["gql_variables"] = parsed
        for key in ("primary_key", "gql_filenames"):
            value = data.get(key)
            if isinstance(value, str):
                data[key] = [part.strip() for part in value.split(",") if part.strip()]
            elif value is None:
                data[key] = []
        return data
```

Relax `_validate_endpoint` to accept the reserved value:

```python
    @model_validator(mode="after")
    def _validate_endpoint(self) -> Self:
        if self.endpoint and self.endpoint != GRAPHQL_ENDPOINT and self.endpoint not in ENDPOINTS:
            raise UserException(f"Unknown endpoint '{self.endpoint}'. Choose one of: {', '.join(sorted(ENDPOINTS))}.")
        return self
```

Add the GraphQL validator (after `_validate_dates`):

```python
    @model_validator(mode="after")
    def _validate_graphql(self) -> Self:
        if self.endpoint != GRAPHQL_ENDPOINT:
            return self
        if self.query_template == QueryTemplate.custom:
            if not self.gql_query.strip():
                raise UserException("A GraphQL query is required in Custom mode.")
            if not self.output_table:
                raise UserException("An Output table name is required in Custom mode.")
        elif self.query_template == QueryTemplate.org_ai_config_files:
            if not self.gql_org:
                raise UserException("Organization is required for the 'AI config files' template.")
            if not self.gql_filenames:
                raise UserException("At least one file name is required for the 'AI config files' template.")
            for name in self.gql_filenames:
                if '"' in name or "\n" in name:
                    raise UserException(f"Invalid file name '{name}': must not contain a quote or newline.")
        elif self.query_template == QueryTemplate.org_pull_request_reviews:
            if not self.gql_org:
                raise UserException("Organization is required for the 'Open PRs' template.")
            if not self.gql_repo:
                raise UserException("Repository is required for the 'Open PRs' template.")
        if self.output_table and not _SAFE_TABLE_NAME.match(self.output_table):
            raise UserException("Output table name may contain only letters, digits, underscores and hyphens.")
        return self
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_configuration.py -v`
Expected: PASS (new + existing).

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff format src/configuration.py src/endpoints.py tests/test_configuration.py
uv run ruff check src/configuration.py src/endpoints.py tests/test_configuration.py
git add src/configuration.py src/endpoints.py tests/test_configuration.py
git commit -m "feat(graphql): config model for the graphql_query data source"
```

---

## Task 2: `graphql_source.py` — flatten, columns, alias

**Files:**
- Create: `src/graphql_source.py`
- Test: `tests/test_graphql_source.py`

**Interfaces:**
- Produces:
  - `sanitize_alias(filename: str) -> str`
  - `flatten_graphql_node(node: dict[str, Any], sep: str = "_") -> dict[str, Any]`
  - `collect_columns(rows: list[dict[str, Any]]) -> list[str]`

- [ ] **Step 1: Write failing tests**

Create `tests/test_graphql_source.py`:

```python
"""Unit tests for the GraphQL flatten / detection / template helpers."""

import pytest
from keboola.component.exceptions import UserException

from graphql_source import (
    collect_columns,
    flatten_graphql_node,
    sanitize_alias,
)


def test_sanitize_alias():
    assert sanitize_alias("AGENTS.md") == "agents_md"
    assert sanitize_alias("CLAUDE.md") == "claude_md"
    assert sanitize_alias(".github/workflows/ci.yml") == "github_workflows_ci_yml"


def test_flatten_scalars_and_nested_object():
    node = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}
    assert flatten_graphql_node(node) == {"nameWithOwner": "o/r", "defaultBranchRef_name": "main"}


def test_flatten_null_object_becomes_empty_cell():
    assert flatten_graphql_node({"agents_md": None}) == {"agents_md": None}


def test_flatten_array_is_json_encoded():
    out = flatten_graphql_node({"labels": [{"name": "bug"}, {"name": "p1"}]})
    assert out == {"labels": '[{"name":"bug"},{"name":"p1"}]'}


def test_collect_columns_first_seen_union():
    rows = [{"a": 1, "b": 2}, {"a": 3, "c": 4}]
    assert collect_columns(rows) == ["a", "b", "c"]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_graphql_source.py -v`
Expected: FAIL with `ModuleNotFoundError: graphql_source`.

- [ ] **Step 3: Implement**

Create `src/graphql_source.py`:

```python
"""GraphQL passthrough helpers — pure transforms + the template registry.

Mirrors how `endpoints.py` holds REST data/transforms. Knows nothing about the client, the config
model, or the component, so it is import-cycle-free and trivially unit-testable. See spec
`2026-06-29-ex-github-v2-graphql-query-source-design.md`.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keboola.component.exceptions import UserException

_NON_IDENT = re.compile(r"[^0-9a-zA-Z]+")


def sanitize_alias(filename: str) -> str:
    """Turn a file path into a GraphQL-safe alias / column prefix: 'AGENTS.md' -> 'agents_md'."""
    slug = _NON_IDENT.sub("_", filename).strip("_").lower()
    if not slug:
        slug = "file"
    if slug[0].isdigit():
        slug = f"f_{slug}"
    return slug


def flatten_graphql_node(node: dict[str, Any], sep: str = "_") -> dict[str, Any]:
    """Flatten one row node: nested objects -> `parent_child` columns; arrays -> JSON cell; scalars/None kept.

    A null object (e.g. a `Blob` field for an absent file) becomes a single empty cell under its own key.
    """
    out: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, val in value.items():
                walk(f"{prefix}{sep}{key}" if prefix else key, val)
        elif isinstance(value, list):
            out[prefix] = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        else:
            out[prefix] = value

    if not isinstance(node, dict):
        return {"value": node}
    for key, val in node.items():
        walk(key, val)
    return out


def collect_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union of all row keys, in first-seen order (stable column ordering for a given query)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen_set:
                seen_set.add(key)
                seen.append(key)
    return seen
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_graphql_source.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/graphql_source.py tests/test_graphql_source.py
uv run ruff check src/graphql_source.py tests/test_graphql_source.py
git add src/graphql_source.py tests/test_graphql_source.py
git commit -m "feat(graphql): flatten/columns/alias helpers"
```

---

## Task 3: `graphql_source.py` — path reading, rows-path detection

**Files:**
- Modify: `src/graphql_source.py`
- Test: `tests/test_graphql_source.py`

**Interfaces:**
- Produces:
  - `read_path(data: Any, dotted: str) -> Any` (empty `dotted` returns `data`)
  - `connection_path(rows_path: str) -> str | None` (strips a trailing `nodes`/`edges`)
  - `detect_rows_path(data: dict[str, Any]) -> str` (raises `UserException` when ambiguous)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_graphql_source.py`:

```python
from graphql_source import connection_path, detect_rows_path, read_path


def test_read_path_navigates_and_empty_returns_data():
    data = {"organization": {"repositories": {"nodes": [1, 2]}}}
    assert read_path(data, "organization.repositories.nodes") == [1, 2]
    assert read_path(data, "") == data
    assert read_path(data, "organization.missing") is None


def test_connection_path_strips_nodes_or_edges():
    assert connection_path("organization.repositories.nodes") == "organization.repositories"
    assert connection_path("repository.pullRequests.edges") == "repository.pullRequests"
    assert connection_path("viewer") is None


def test_detect_rows_path_nested_connection():
    data = {"organization": {"repositories": {"pageInfo": {"hasNextPage": False}, "nodes": [{"x": 1}]}}}
    assert detect_rows_path(data) == "organization.repositories.nodes"


def test_detect_rows_path_prefers_nodes_over_edges():
    data = {"search": {"nodes": [{"x": 1}], "edges": [{"y": 2}]}}
    assert detect_rows_path(data) == "search.nodes"


def test_detect_rows_path_single_object_is_one_row():
    data = {"viewer": {"login": "octocat"}}
    assert detect_rows_path(data) == "viewer"


def test_detect_rows_path_ambiguous_sibling_arrays_raises():
    data = {"a": {"items": [{"x": 1}], "others": [{"y": 2}]}}
    with pytest.raises(UserException, match="Row path"):
        detect_rows_path(data)


def test_detect_rows_path_ambiguous_object_children_raises():
    data = {"a": {"login": "x"}, "b": {"login": "y"}}
    with pytest.raises(UserException, match="Row path"):
        detect_rows_path(data)
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_graphql_source.py -k "path" -v`
Expected: FAIL (functions undefined).

- [ ] **Step 3: Implement**

Append to `src/graphql_source.py`:

```python
def read_path(data: Any, dotted: str) -> Any:
    """Read a dotted path; empty path returns `data` unchanged; missing hop returns None."""
    if dotted == "":
        return data
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def connection_path(rows_path: str) -> str | None:
    """The paginated connection (rows path minus a trailing `nodes`/`edges`), or None."""
    parts = rows_path.split(".")
    if parts and parts[-1] in ("nodes", "edges"):
        return ".".join(parts[:-1])
    return None


def _is_array_of_objects(value: Any) -> bool:
    return isinstance(value, list) and (not value or isinstance(value[0], dict))


_AMBIGUOUS = (
    "Could not determine the rows path automatically (the query returns more than one list). "
    "Set the 'Row path (advanced)' field, e.g. organization.repositories.nodes."
)


def detect_rows_path(data: dict[str, Any]) -> str:
    """Descend from `data` to the first array-of-objects (preferring `nodes`/`edges`); '' = whole data is one row."""
    path: list[str] = []
    cur: Any = data
    while isinstance(cur, dict):
        array_children = [k for k, v in cur.items() if _is_array_of_objects(v)]
        chosen: str | None = None
        if len(array_children) == 1:
            chosen = array_children[0]
        elif len(array_children) > 1:
            for pref in ("nodes", "edges"):
                if pref in array_children:
                    chosen = pref
                    break
            if chosen is None:
                raise UserException(_AMBIGUOUS)
        if chosen is not None:
            path.append(chosen)
            return ".".join(path)
        object_children = [k for k, v in cur.items() if isinstance(v, dict)]
        if len(object_children) == 1:
            path.append(object_children[0])
            cur = cur[object_children[0]]
            continue
        if not object_children:
            return ".".join(path)  # bottomed out at an object -> one row ('' when path empty)
        raise UserException(_AMBIGUOUS)
    raise UserException(_AMBIGUOUS)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_graphql_source.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/graphql_source.py tests/test_graphql_source.py
uv run ruff check src/graphql_source.py tests/test_graphql_source.py
git add src/graphql_source.py tests/test_graphql_source.py
git commit -m "feat(graphql): rows-path detection + path helpers"
```

---

## Task 4: `graphql_source.py` — template registry

**Files:**
- Modify: `src/graphql_source.py`
- Test: `tests/test_graphql_source.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) GraphQLTemplate` with fields `build_query: Callable[[dict], str]`, `build_variables: Callable[[dict], dict]`, `rows_path: str`, `output_table: str`, `primary_key: list[str]`.
  - `TEMPLATES: dict[str, GraphQLTemplate]` keyed by `"org_ai_config_files"`, `"org_pull_request_reviews"`.
- Consumes (by the component, Task 7): a params dict `{"org": str, "repo": str, "filenames": list[str]}`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_graphql_source.py`:

```python
from graphql_source import TEMPLATES


def test_ai_config_template_builds_aliased_blob_fields():
    tpl = TEMPLATES["org_ai_config_files"]
    query = tpl.build_query({"org": "acme", "filenames": ["AGENTS.md", "CLAUDE.md"]})
    assert 'agents_md: object(expression: "HEAD:AGENTS.md")' in query
    assert 'claude_md: object(expression: "HEAD:CLAUDE.md")' in query
    assert "$cursor" in query and "pageInfo" in query
    assert tpl.build_variables({"org": "acme", "filenames": []}) == {"org": "acme"}
    assert tpl.rows_path == "organization.repositories.nodes"
    assert tpl.primary_key == ["nameWithOwner"]


def test_pr_reviews_template_uses_org_and_repo():
    tpl = TEMPLATES["org_pull_request_reviews"]
    query = tpl.build_query({"org": "acme", "repo": "web"})
    assert "reviewDecision" in query and "pullRequests" in query
    assert tpl.build_variables({"org": "acme", "repo": "web"}) == {"org": "acme", "repo": "web"}
    assert tpl.rows_path == "repository.pullRequests.nodes"
    assert tpl.primary_key == ["number"]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_graphql_source.py -k template -v`
Expected: FAIL (`TEMPLATES` undefined).

- [ ] **Step 3: Implement**

Append to `src/graphql_source.py`:

```python
@dataclass(frozen=True)
class GraphQLTemplate:
    """A built-in query a non-GraphQL user picks; params come from dedicated config fields."""

    build_query: Callable[[dict[str, Any]], str]
    build_variables: Callable[[dict[str, Any]], dict[str, Any]]
    rows_path: str
    output_table: str
    primary_key: list[str]


def _ai_config_query(params: dict[str, Any]) -> str:
    filenames = params["filenames"] or ["AGENTS.md", "CLAUDE.md"]
    blob_fields = "\n        ".join(
        f'{sanitize_alias(fn)}: object(expression: "HEAD:{fn}") {{ ... on Blob {{ byteSize isBinary text }} }}'
        for fn in filenames
    )
    return f"""query($org: String!, $cursor: String) {{
  organization(login: $org) {{
    repositories(first: 100, after: $cursor) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        nameWithOwner
        url
        defaultBranchRef {{ name }}
        {blob_fields}
      }}
    }}
  }}
}}"""


def _pull_request_reviews_query(params: dict[str, Any]) -> str:
    return """query($org: String!, $repo: String!, $cursor: String) {
  repository(owner: $org, name: $repo) {
    pullRequests(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url author { login } reviewDecision createdAt updatedAt }
    }
  }
}"""


TEMPLATES: dict[str, GraphQLTemplate] = {
    "org_ai_config_files": GraphQLTemplate(
        build_query=_ai_config_query,
        build_variables=lambda p: {"org": p["org"]},
        rows_path="organization.repositories.nodes",
        output_table="repo_ai_config_files",
        primary_key=["nameWithOwner"],
    ),
    "org_pull_request_reviews": GraphQLTemplate(
        build_query=_pull_request_reviews_query,
        build_variables=lambda p: {"org": p["org"], "repo": p["repo"]},
        rows_path="repository.pullRequests.nodes",
        output_table="repo_open_pull_requests",
        primary_key=["number"],
    ),
}
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_graphql_source.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/graphql_source.py tests/test_graphql_source.py
uv run ruff check src/graphql_source.py tests/test_graphql_source.py
git add src/graphql_source.py tests/test_graphql_source.py
git commit -m "feat(graphql): template registry (AI config files + open PRs)"
```

---

## Task 5: `client.graphql()` — POST + GraphQL error handling (single page)

**Files:**
- Modify: `src/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `graphql_source.detect_rows_path`, `read_path`, `connection_path`; existing `_classify_response`, `_parse_json`, tenacity callables, `_RetryableError`.
- Produces:
  - `GitHubClient.graphql(query: str, variables: dict[str, Any], rows_path: str | None = None) -> tuple[list[dict[str, Any]], str]`
  - internal: `_retrying()`, `_run_with_retry(fn, *args)`, `_graphql_post`, `_graphql_attempt`, `_graphql_extract_data`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_client.py`:

```python
@responses.activate
def test_graphql_single_page_detects_rows_path(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"organization": {"repositories": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"nameWithOwner": "o/a"}, {"nameWithOwner": "o/b"}],
        }}}},
        status=200,
    )
    rows, resolved = client.graphql("query($cursor:String){ x }", {"org": "o"})
    assert resolved == "organization.repositories.nodes"
    assert [r["nameWithOwner"] for r in rows] == ["o/a", "o/b"]
    assert len(responses.calls) == 1


@responses.activate
def test_graphql_errors_with_null_data_raises(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": None, "errors": [{"message": "Field 'bogus' doesn't exist"}]},
        status=200,
    )
    with pytest.raises(UserException, match="bogus"):
        client.graphql("{ bogus }", {})


@responses.activate
def test_graphql_partial_data_warns_and_returns(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"viewer": {"login": "octocat"}}, "errors": [{"message": "timeout on field x"}]},
        status=200,
    )
    rows, resolved = client.graphql("{ viewer { login } }", {})
    assert resolved == "viewer" and rows == [{"login": "octocat"}]


@responses.activate
def test_graphql_rate_limited_type_raises(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
        status=200,
    )
    with pytest.raises(UserException, match="rate limit"):
        client.graphql("{ x }", {})


@responses.activate
def test_graphql_explicit_rows_path_overrides_detection(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"a": {"items": [{"x": 1}], "others": [{"y": 2}]}}},
        status=200,
    )
    rows, resolved = client.graphql("{ a { items others } }", {}, rows_path="a.items")
    assert resolved == "a.items" and rows == [{"x": 1}]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_client.py -k graphql -v`
Expected: FAIL (`graphql` undefined).

- [ ] **Step 3: Implement**

In `src/client.py`, add the import at the top:

```python
from graphql_source import connection_path, detect_rows_path, read_path
```

Refactor `_request` to share retry plumbing. Replace the body of `_request` with:

```python
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
```

(Keep the original docstring intent; remove the inline `Retrying(...)` construction now living in `_retrying`.)

Add the GraphQL methods (e.g. after `get_json_array_all`):

```python
    # ----------------------------------------------------------------------------- graphql
    def graphql(
        self, query: str, variables: dict[str, Any], rows_path: str | None = None
    ) -> tuple[list[dict[str, Any]], str]:
        """Run a GraphQL query, paginate the rows-path connection, return (raw row nodes, resolved rows_path).

        Rows path is the caller's override or auto-detected from page 1 (graphql_source.detect_rows_path).
        Pagination follows the GraphQL cursor convention: it advances only when the query declares a
        `$cursor` variable AND the connection exposes `pageInfo {hasNextPage endCursor}`.
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_client.py -v`
Expected: PASS (new graphql tests + all existing REST tests, proving the `_retrying` refactor is behavior-preserving).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff format src/client.py tests/test_client.py
uv run ruff check src/client.py tests/test_client.py
git add src/client.py tests/test_client.py
git commit -m "feat(graphql): client.graphql() POST + GraphQL error handling"
```

---

## Task 6: `client.graphql()` — cursor pagination

**Files:**
- Modify: `src/client.py` (no code change expected — the loop from Task 5 already paginates; this task proves it and adds the single-object case)
- Test: `tests/test_client.py`

**Interfaces:** unchanged from Task 5.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_client.py`:

```python
@responses.activate
def test_graphql_follows_cursor_across_two_pages(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"organization": {"repositories": {
            "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
            "nodes": [{"nameWithOwner": "o/a"}],
        }}}},
        status=200,
    )
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"organization": {"repositories": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"nameWithOwner": "o/b"}],
        }}}},
        status=200,
    )
    rows, _ = client.graphql("query($cursor:String){ x }", {"org": "o"})
    assert [r["nameWithOwner"] for r in rows] == ["o/a", "o/b"]
    assert len(responses.calls) == 2


@responses.activate
def test_graphql_no_cursor_variable_single_page(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"organization": {"repositories": {
            "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
            "nodes": [{"nameWithOwner": "o/a"}],
        }}}},
        status=200,
    )
    # query has no $cursor -> we do NOT paginate even though hasNextPage is true
    rows, _ = client.graphql("{ organization { repositories { nodes { nameWithOwner } } } }", {})
    assert [r["nameWithOwner"] for r in rows] == ["o/a"]
    assert len(responses.calls) == 1


@responses.activate
def test_graphql_single_object_is_one_row(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"viewer": {"login": "octocat", "name": "Octo"}}},
        status=200,
    )
    rows, resolved = client.graphql("{ viewer { login name } }", {})
    assert resolved == "viewer" and rows == [{"login": "octocat", "name": "Octo"}]
```

- [ ] **Step 2: Run to verify result**

Run: `uv run pytest tests/test_client.py -k "graphql" -v`
Expected: PASS (the Task 5 loop already implements this). If `test_graphql_follows_cursor_across_two_pages` fails, fix the loop so `vars_state["cursor"]` advances and the second page is requested.

- [ ] **Step 3: Commit**

```bash
git add tests/test_client.py
git commit -m "test(graphql): cursor pagination + single-object row coverage"
```

---

## Task 7: `component._run_graphql()` dispatch + dynamic table write

**Files:**
- Modify: `src/component.py`
- Test: `tests/test_component.py`

**Interfaces:**
- Consumes: `endpoints.GRAPHQL_ENDPOINT`; `graphql_source.flatten_graphql_node`, `collect_columns`, `TEMPLATES`; `client.graphql`; existing `create_out_table_definition`, `write_manifest`.
- Produces: `Component._run_graphql()`, `Component._graphql_template_params()`, `Component._write_dynamic_table(name, columns, rows, primary_key, incremental)`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_component.py`:

```python
from unittest.mock import MagicMock


def _read_csv(path):
    import csv as _csv
    with open(path, newline="") as fh:
        return list(_csv.reader(fh))


def test_run_graphql_custom_writes_dynamic_table(tmp_path):
    comp = _component(tmp_path, {
        **PAT, "endpoint": "graphql_query", "query_template": "custom",
        "gql_query": "{ viewer { login } }", "output_table": "gql_out", "load_type": "full_load",
    })
    comp.client.graphql = MagicMock(return_value=([{"nameWithOwner": "o/a", "defaultBranchRef": {"name": "main"}},
                                                   {"nameWithOwner": "o/b"}], "organization.repositories.nodes"))
    comp.run()
    out = comp.tables_out_path
    rows = _read_csv(f"{out}/gql_out.csv")
    # union columns, first-seen order: nameWithOwner, defaultBranchRef_name
    assert rows[0] == ["o/a", "main"]
    assert rows[1] == ["o/b", ""]


def test_run_graphql_template_uses_registry(tmp_path):
    comp = _component(tmp_path, {
        **PAT, "endpoint": "graphql_query", "query_template": "org_ai_config_files",
        "gql_org": "acme", "load_type": "full_load",
    })
    captured = {}
    def _fake_graphql(query, variables, rows_path):
        captured["query"] = query
        captured["rows_path"] = rows_path
        return ([{"nameWithOwner": "acme/x"}], rows_path)
    comp.client.graphql = MagicMock(side_effect=_fake_graphql)
    comp.run()
    assert 'agents_md: object(expression: "HEAD:AGENTS.md")' in captured["query"]
    assert captured["rows_path"] == "organization.repositories.nodes"
    # template default table name used when output_table is empty
    assert (tmp_path / "data" / "out" / "tables" / "repo_ai_config_files.csv").exists()


def test_run_graphql_incremental_without_pk_falls_back_to_full(tmp_path):
    comp = _component(tmp_path, {
        **PAT, "endpoint": "graphql_query", "query_template": "custom",
        "gql_query": "{ viewer { login } }", "output_table": "gql_out", "load_type": "incremental_load",
    })
    comp.client.graphql = MagicMock(return_value=([{"login": "octocat"}], "viewer"))
    comp.run()  # no primary_key set -> must not raise; writes a full load
    manifest = json.loads((tmp_path / "data" / "out" / "tables" / "gql_out.csv.manifest").read_text())
    assert manifest["incremental"] is False


def test_run_graphql_primary_key_missing_column_raises(tmp_path):
    from keboola.component.exceptions import UserException
    comp = _component(tmp_path, {
        **PAT, "endpoint": "graphql_query", "query_template": "custom",
        "gql_query": "{ viewer { login } }", "output_table": "gql_out",
        "primary_key": "does_not_exist", "load_type": "incremental_load",
    })
    comp.client.graphql = MagicMock(return_value=([{"login": "octocat"}], "viewer"))
    with pytest.raises(UserException, match="Primary key"):
        comp.run()
```

Add `import pytest` to the top of `tests/test_component.py` if not already present.

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_component.py -k graphql -v`
Expected: FAIL (dispatch/methods not implemented; `run()` falls into the REST path and errors on the unknown endpoint).

- [ ] **Step 3: Implement**

In `src/component.py`, extend imports:

```python
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
from graphql_source import TEMPLATES, collect_columns, flatten_graphql_node
```

Add the dispatch at the very top of `run()`:

```python
    def run(self) -> None:
        if self.cfg.endpoint == GRAPHQL_ENDPOINT:
            return self._run_graphql()
        endpoint = self._endpoint()
        ...  # unchanged REST body
```

Add the GraphQL methods (e.g. after `run`/`_fetch_target`):

```python
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

        logging.info("Running GraphQL query (template=%s) into table '%s'.", self.cfg.query_template, output_table)
        raw_rows, _resolved = self.client.graphql(query, variables, rows_path)
        rows = [flatten_graphql_node(node) for node in raw_rows]

        incremental = self.cfg.incremental_load and bool(primary_key)
        if self.cfg.incremental_load and not primary_key:
            logging.warning("Incremental load requested but no primary key set; writing a full load instead.")
        columns = collect_columns(rows) or list(primary_key)
        self._write_dynamic_table(output_table, columns, rows, primary_key, incremental)

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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_component.py -v`
Expected: PASS.

- [ ] **Step 5: Full unit suite + lint + commit**

```bash
uv run pytest tests/test_component.py tests/test_client.py tests/test_configuration.py tests/test_graphql_source.py -v
uv run ruff format src/component.py tests/test_component.py
uv run ruff check src/component.py tests/test_component.py
git add src/component.py tests/test_component.py
git commit -m "feat(graphql): component dispatch + dynamic table writer"
```

---

## Task 8: Row schema — `graphql_query` data source + conditional fields

**Files:**
- Modify: `component_config/configRowSchema.json`
- Test: `tests/test_auth_and_sync.py` (add a JSON-validity + enum-alignment assertion)

**Interfaces:** none (UI schema). The config-model already accepts every field (Task 1).

- [ ] **Step 1: Write a failing schema-shape test**

Append to `tests/test_auth_and_sync.py`:

```python
import json as _json
from pathlib import Path


def test_row_schema_has_graphql_data_source():
    schema = _json.loads((Path(__file__).resolve().parent.parent / "component_config" / "configRowSchema.json").read_text())
    endpoint = schema["properties"]["endpoint"]
    assert "graphql_query" in endpoint["enum"]
    # the GraphQL fields exist and are gated on the graphql_query data source
    for field in ("query_template", "gql_query", "output_table", "gql_org"):
        assert field in schema["properties"], field
        deps = schema["properties"][field]["options"]["dependencies"]
        assert "graphql_query" in deps["endpoint"]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_auth_and_sync.py -k graphql -v`
Expected: FAIL (`graphql_query` not in enum / fields absent).

- [ ] **Step 3: Implement schema changes**

In `component_config/configRowSchema.json`:

1. Append `"graphql_query"` to `properties.endpoint.enum` (last) and `"GraphQL Query (advanced)"` to `properties.endpoint.options.enum_titles` (same position).
2. Add these properties (PO 90+). Each is gated on `endpoint == "graphql_query"`; the template-parameter fields additionally note the `query_template` they belong to (see the caveat below). Exact JSON:

```json
"query_template": {
  "type": "string",
  "title": "Query Template",
  "enum": ["custom", "org_ai_config_files", "org_pull_request_reviews"],
  "default": "custom",
  "propertyOrder": 90,
  "options": {
    "enum_titles": ["Custom (write your own)", "Org repos: AI config files (AGENTS.md, CLAUDE.md)", "Repository: Open PRs with review decision"],
    "tooltip": "Pick a built-in query, or Custom to write your own GraphQL.",
    "dependencies": { "endpoint": ["graphql_query"] }
  }
},
"gql_query": {
  "type": "string",
  "format": "textarea",
  "title": "GraphQL Query",
  "default": "",
  "description": "A GitHub GraphQL query. For pagination, select pageInfo { hasNextPage endCursor } and declare a $cursor variable used as after: $cursor.",
  "propertyOrder": 91,
  "options": {
    "tooltip": "Runs against https://api.github.com/graphql with your token. The result is flattened into one table (objects become parent_child columns; nested lists become a JSON cell).",
    "dependencies": { "endpoint": ["graphql_query"] }
  }
},
"gql_variables": {
  "type": "string",
  "format": "textarea",
  "title": "GraphQL Variables (JSON)",
  "default": "",
  "description": "A JSON object of variables for your query, e.g. {\"org\": \"acme\"}. Leave blank for none.",
  "propertyOrder": 92,
  "options": { "dependencies": { "endpoint": ["graphql_query"] } }
},
"gql_row_path": {
  "type": "string",
  "title": "Row path (advanced)",
  "default": "",
  "propertyOrder": 93,
  "options": {
    "tooltip": "Dotted path under data to the list whose items become your table rows, e.g. organization.repositories.nodes. Leave empty to detect it automatically; set it only when the query returns more than one list and detection can't choose.",
    "dependencies": { "endpoint": ["graphql_query"] }
  }
},
"output_table": {
  "type": "string",
  "title": "Output table name",
  "default": "",
  "description": "Name of the output table (letters, digits, underscore, hyphen). Required in Custom mode; templates default it.",
  "propertyOrder": 94,
  "options": { "dependencies": { "endpoint": ["graphql_query"] } }
},
"primary_key": {
  "type": "string",
  "title": "Primary key (optional)",
  "default": "",
  "description": "Comma-separated result column name(s) for incremental upsert. Empty = full load only.",
  "propertyOrder": 95,
  "options": {
    "tooltip": "Column names AFTER flattening (e.g. nameWithOwner). Used only when Load Type is Incremental.",
    "dependencies": { "endpoint": ["graphql_query"] }
  }
},
"gql_org": {
  "type": "string",
  "title": "Organization",
  "default": "",
  "propertyOrder": 96,
  "options": {
    "tooltip": "Organization login the template runs against.",
    "dependencies": { "endpoint": ["graphql_query"] }
  }
},
"gql_repo": {
  "type": "string",
  "title": "Repository name",
  "default": "",
  "propertyOrder": 97,
  "options": {
    "tooltip": "Repository name (without owner) for the Open PRs template.",
    "dependencies": { "endpoint": ["graphql_query"] }
  }
},
"gql_filenames": {
  "type": "array",
  "title": "File names",
  "format": "select",
  "uniqueItems": true,
  "default": ["AGENTS.md", "CLAUDE.md"],
  "items": { "type": "string" },
  "options": {
    "tooltip": "Files to probe per repo (one content column group each). Default AGENTS.md + CLAUDE.md.",
    "dependencies": { "endpoint": ["graphql_query"] }
  }
},
```

3. Add `"graphql_query"` to the `load_type` field's `options.dependencies.endpoint` array so Load Type shows for the GraphQL source.

> **Two-level conditional caveat (per spec §12 and the fan-out spec's `optional_settings` precedent):** a field that should depend on **both** `endpoint == graphql_query` **and** `query_template == X` cannot be expressed by one `options.dependencies` map. **Verify in the Ctrl+D sandbox during implementation** whether nested dependencies render; if not, the documented fallback (used above) is to gate every GraphQL field on `endpoint` only and let template-vs-custom relevance be a soft convention — unused fields are simply ignored by the model. Do not block the task on perfect field-hiding.

- [ ] **Step 4: Run to verify pass + JSON validity**

Run: `uv run pytest tests/test_auth_and_sync.py -k graphql -v`
Run: `uv run python -c "import json,pathlib; json.loads(pathlib.Path('component_config/configRowSchema.json').read_text()); print('schema OK')"`
Expected: test PASS; `schema OK` printed.

- [ ] **Step 5: Commit**

```bash
git add component_config/configRowSchema.json tests/test_auth_and_sync.py
git commit -m "feat(graphql): row schema data source + conditional fields"
```

---

## Task 9: Functional VCR case — `org_ai_config_files` template

**Files:**
- Create: `tests/functional/40_graphql_ai_config_files/source/data/config.json`
- Create: `tests/functional/40_graphql_ai_config_files/source/data/cassettes/requests.json`
- Create: `tests/functional/40_graphql_ai_config_files/expected/data/out/tables/repo_ai_config_files.csv`
- Create: `tests/functional/40_graphql_ai_config_files/expected/data/out/tables/repo_ai_config_files.csv.manifest`

**Interfaces:** exercises the whole path end-to-end via `keboola.datadirtest` VCR replay (already wired by `tests/test_functional.py`).

- [ ] **Step 1: Author the config**

`source/data/config.json`:

```json
{
  "action": "run",
  "parameters": {
    "auth_type": "pat",
    "#github_token": "DUMMY_PAT_NOT_REAL",
    "endpoint": "graphql_query",
    "query_template": "org_ai_config_files",
    "gql_org": "acme",
    "gql_filenames": ["AGENTS.md", "CLAUDE.md"],
    "load_type": "full_load"
  }
}
```

- [ ] **Step 2: Author the cassette (single POST, single page)**

`source/data/cassettes/requests.json` — one interaction; `hasNextPage: false` so no second page (avoids POST-body matching across pages). The response body is the JSON-encoded GraphQL payload: two repos, one with both files, one with neither.

```json
{
  "_metadata": { "freeze_time": "2026-06-29T12:00:00+00:00", "recorded_at": "2026-06-29T12:00:00+00:00" },
  "interactions": [
    {
      "request": {
        "headers": { "Authorization": ["Bearer ***"] },
        "method": "POST",
        "uri": "https://api.github.com/graphql"
      },
      "response": {
        "body": { "string": "{\"data\":{\"organization\":{\"repositories\":{\"pageInfo\":{\"hasNextPage\":false,\"endCursor\":null},\"nodes\":[{\"nameWithOwner\":\"acme/web\",\"url\":\"https://github.com/acme/web\",\"defaultBranchRef\":{\"name\":\"main\"},\"agents_md\":{\"byteSize\":12,\"isBinary\":false,\"text\":\"# Agents\\n\"},\"claude_md\":{\"byteSize\":11,\"isBinary\":false,\"text\":\"# Claude\\n\"}},{\"nameWithOwner\":\"acme/legacy\",\"url\":\"https://github.com/acme/legacy\",\"defaultBranchRef\":{\"name\":\"master\"},\"agents_md\":null,\"claude_md\":null}]}}}}" },
        "headers": { "Content-Type": ["application/json"] },
        "status": { "code": 200, "message": "OK" }
      }
    }
  ],
  "version": 1
}
```

- [ ] **Step 3: Determine expected columns by reasoning about the flatten**

Row `acme/web` flattens to: `nameWithOwner, url, defaultBranchRef_name, agents_md_byteSize, agents_md_isBinary, agents_md_text, claude_md_byteSize, claude_md_isBinary, claude_md_text`.
Row `acme/legacy` flattens to: `nameWithOwner, url, defaultBranchRef_name, agents_md, claude_md` (null objects → single empty cells).
First-seen union column order (web first): `nameWithOwner, url, defaultBranchRef_name, agents_md_byteSize, agents_md_isBinary, agents_md_text, claude_md_byteSize, claude_md_isBinary, claude_md_text, agents_md, claude_md`.

- [ ] **Step 4: Author expected CSV (headerless)**

`expected/data/out/tables/repo_ai_config_files.csv` — values stringified by `csv.DictWriter` (bool → `True`/`False`, missing cell → empty). Note `text` cells contain a trailing newline so they are quoted:

```
acme/web,https://github.com/acme/web,main,12,False,"# Agents
",11,False,"# Claude
",,
acme/legacy,https://github.com/acme/legacy,master,,,,,,,,
```

> If exact stringification (e.g. `False` vs `false`, or quoting) differs at runtime, regenerate the expected file from the actual run output once and inspect it for correctness before committing — the runtime output is the source of truth for formatting, the columns/values above are the source of truth for content.

- [ ] **Step 5: Author expected manifest (all STRING, PK = nameWithOwner)**

`expected/data/out/tables/repo_ai_config_files.csv.manifest`:

```json
{"incremental": false, "write_always": false, "delimiter": ",", "enclosure": "\"", "manifest_type": "out", "has_header": false, "schema": [{"name": "nameWithOwner", "data_type": {"base": {"type": "STRING"}}, "nullable": true, "primary_key": true}, {"name": "url", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "defaultBranchRef_name", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "agents_md_byteSize", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "agents_md_isBinary", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "agents_md_text", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "claude_md_byteSize", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "claude_md_isBinary", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "claude_md_text", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "agents_md", "data_type": {"base": {"type": "STRING"}}, "nullable": true}, {"name": "claude_md", "data_type": {"base": {"type": "STRING"}}, "nullable": true}]}
```

- [ ] **Step 6: Run the functional case**

Run: `uv run pytest tests/test_functional.py -k 40_graphql -v`
Expected: PASS. If the VCR matcher rejects the POST (it defaults differ from the GET cases), set the case to match on method+URI only — verify the `VCRTestDataDir`/`vcr_mode="replay"` match config and adjust the cassette (drop body matching for POST). This mirrors the repo's "verify matcher behavior during implementation" practice; keep the case single-interaction.

- [ ] **Step 7: Commit**

```bash
git add tests/functional/40_graphql_ai_config_files
git commit -m "test(graphql): functional VCR case for the AI-config-files template"
```

---

## Task 10: Documentation

**Files:**
- Modify: `component_config/configuration_description.md`
- Modify: `README.md`

**Interfaces:** none.

- [ ] **Step 1: Document the data source**

Add a section to `component_config/configuration_description.md` (and a matching short section in `README.md`) describing:
- The **GraphQL Query** data source: pick it from Data Source; choose a **Query Template** or **Custom**.
- **Custom**: paste a GraphQL query + optional JSON Variables; set an **Output table**; for pagination select `pageInfo { hasNextPage endCursor }` and use a `$cursor` variable; set **Row path (advanced)** only when auto-detection can't choose; set **Primary key** for incremental upsert.
- **Templates**: `Org repos: AI config files` (detects AGENTS.md/CLAUDE.md across an org's repos — set **Organization**, optionally edit **File names**) and `Repository: Open PRs with review decision`.
- Output is one flattened table, **all columns STRING**; nested objects become `parent_child` columns; nested lists become a JSON cell.

Use the exact copy below as the `configuration_description.md` block:

```markdown
### GraphQL Query (advanced)

Select **GraphQL Query** as the Data Source to run a GitHub GraphQL query and load its result as one
table. Every column is loaded as text; nested objects become `parent_child` columns and nested lists
become a JSON-encoded cell.

- **Query Template** — pick a built-in query or **Custom**.
  - **Org repos: AI config files** — for every repository in an **Organization**, reports whether each
    **File name** (default `AGENTS.md`, `CLAUDE.md`) exists and its content. One row per repository.
  - **Repository: Open PRs with review decision** — open pull requests of one **Organization** /
    **Repository**, including the GraphQL-only `reviewDecision`.
- **Custom** — paste a **GraphQL Query** and optional **GraphQL Variables** (a JSON object) and set an
  **Output table** name. To page through large results, select `pageInfo { hasNextPage endCursor }` on the
  connection and declare a `$cursor` variable used as `after: $cursor`.
- **Row path (advanced)** — dotted path under `data` to the list that becomes your rows (e.g.
  `organization.repositories.nodes`). Leave empty to auto-detect; set it only when the query returns more
  than one list.
- **Primary key** — comma-separated result column name(s) for incremental upsert (empty = full load).
```

- [ ] **Step 2: Sanity-check rendering**

Run: `uv run python -c "import pathlib; print('ok' if pathlib.Path('component_config/configuration_description.md').read_text() else 'empty')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add component_config/configuration_description.md README.md
git commit -m "docs(graphql): document the GraphQL Query data source"
```

---

## Task 11: Full suite + smoke import

**Files:** none (verification only).

- [ ] **Step 1: Run the whole test suite**

Run: `uv run pytest -v`
Expected: ALL PASS (unit + functional + existing smoke/import tests).

- [ ] **Step 2: Lint/format/type gate (pre-commit parity)**

Run: `uv run ruff format --check src tests`
Run: `uv run ruff check src tests`
Run: `uv run ty check src` (warnings only — must not error)
Expected: clean.

- [ ] **Step 3: Smoke-import the component**

Run: `uv run python -c "import sys; sys.path.insert(0, 'src'); import component, client, configuration, graphql_source, endpoints; print('import OK')"`
Expected: `import OK`.

- [ ] **Step 4: Final commit (if any formatting changed)**

```bash
git add -A
git commit -m "chore(graphql): final lint/format pass" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:**
- §3 reserved `graphql_query` + dispatch → Tasks 1, 7. ✅
- §4 auto-flatten (objects/arrays/null, column union) → Task 2; written by Task 7. ✅
- §5 rows-path detection + override → Task 3 (detect), Task 5 (override wired). ✅
- §6 cursor pagination convention → Tasks 5–6. ✅
- §7 templates (flagship AI config + PRs) → Task 4; functional flagship → Task 9. ✅
- §8 client path (POST, body errors, RATE_LIMITED, partial) → Task 5. ✅
- §9 config model (fields, JSON variables, PK coercion, validation, filename guard) → Task 1. ✅
- §10 output/incremental (STRING, headerless+manifest, PK-gated incremental) → Task 7. ✅
- §11 errors/rate limits → Task 5 (body) + reused HTTP handling (Task 5 refactor). ✅
- §12 schema + two-level caveat → Task 8. ✅
- §13 testing → Tasks 1–9 (unit) + 9 (functional). ✅
- §14 backward compat → additive only; existing suites re-run green in Tasks 5, 7, 11. ✅

**Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". The two "regenerate/verify at runtime" notes (Task 8 conditional rendering, Task 9 CSV stringification + POST matcher) are explicit verification steps with a documented fallback, matching the repo's established practice — not vague placeholders.

**Type consistency:** `graphql()` signature `(query, variables, rows_path=None) -> tuple[list[dict], str]` is identical across Tasks 5, 6, 7. `_write_dynamic_table(name, columns, rows, primary_key, incremental)` consistent in Task 7. `TEMPLATES` keys (`org_ai_config_files`, `org_pull_request_reviews`) match `QueryTemplate` values (Task 1) and the schema enum (Task 8). `detect_rows_path`/`read_path`/`connection_path` signatures consistent across Tasks 3, 5.

---

## Known scope notes (carried from the spec / this session)

- **The 1000-repo PR request-limit win** the user raised: GraphQL collapses `repos × PRs × reviews`
  REST calls into a few paginated queries. The shipped `org_pull_request_reviews` template is
  **single-repo** (clean one-row-per-PR). The cross-all-repos batching is achievable today via a **custom**
  query that returns repo rows with PRs in a nested JSON cell; a clean per-PR table across all org repos
  needs nested-array→child-table explosion, which is an explicit **out-of-scope follow-up** (spec §15).
  Flag to the user during execution whether they want the all-repos PR variant promoted.
- Out of scope (spec §15): type inference, child-table explosion, multi-connection pagination, multi-org
  template fan-out, GraphQL points-budget pre-throttling, more templates.
