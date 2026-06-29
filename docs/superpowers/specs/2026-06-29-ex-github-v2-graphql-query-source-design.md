# ex-github-v2 — GraphQL Query data source (flattened to tables)

**Date:** 2026-06-29
**Status:** Approved design — pending implementation plan
**Builds on:** `2026-06-23-ex-github-v2-design.md` (the typed REST registry) and
`2026-06-24-ex-github-v2-multi-target-fanout-design.md` (the fan-out / window model). This adds a
**parallel** data source; the 33 curated REST endpoints are untouched.

## 1. Motivation

The concrete need: **"which of my repositories have an `AGENTS.md` / `CLAUDE.md`?"** — file *detection*
across many repos, with the file content as a useful extra. The broader want: a way to ask GitHub for
data the curated catalog doesn't cover, **without hard-coding a new endpoint each time**.

Two GitHub "query surfaces" were evaluated against this:

| Surface | Verdict |
|---|---|
| **Search API** (`/search/code`, `q=filename:AGENTS.md`) | **Rejected as the engine.** Returns *matches, not content*; **default branch only**; only indexes files **< 384 KB**; requires a search term; **10 req/min**; **1000 results max** per query. Good for discovery, unfit as a content extractor. |
| **GraphQL API** (`POST /graphql`) | **Chosen.** The real query language: exactly the fields/files you want, any branch, in one round-trip, with `... on Blob { text }` returning content. Cost: the response shape is arbitrary — handled by §4. |

The plain **Contents API** (per-path) was the other candidate but is one request per `repo × path` and
does not generalise beyond files. GraphQL covers the file use case *and* arbitrary future needs, so it is
the foundation; the file use case ships as a **template** (§7) on top of it.

## 2. Approved decisions

| Decision | Choice |
|---|---|
| Mechanism | **GraphQL passthrough** — user-authored query, run by the component |
| Output | **One flattened table** (real columns), never a raw-JSON blob |
| Column types | **All `STRING`** in v1; int/bool/float inference is a follow-up |
| Row grain ("what is a row") | **Auto-detected** from the response, with an **optional manual override** (§5) |
| Flatten rule | objects → `parent_child` columns; nested arrays → JSON-encoded cell; columns = union across rows (§4) |
| Pagination | **Automatic** when the query follows the GraphQL cursor convention (§6); else single page |
| Templates | **In scope for v1** — a flagship AGENTS.md/CLAUDE.md detector + one more (§7) |
| Time window (`since`/`until`) | **Not applicable** — filtering is the query's job |
| Incremental | `load_type` controls Storage write mode only (upsert by user-supplied PK vs full replace) |
| Auth & rate limits | Reuse the existing token (PAT/OAuth) and HTTP-level handling (§11) |

## 3. The data source

A new reserved Data Source value **`graphql_query`** ("GraphQL Query") is added to the `endpoint` enum.
It is **not** an `endpoints.ENDPOINTS` registry entry (those are typed REST descriptors). Instead:

- `configuration._validate_endpoint` accepts `graphql_query` as a valid value alongside the registry keys.
- `component.run()` branches: `if cfg.endpoint == GRAPHQL_ENDPOINT: self._run_graphql()` else the existing
  REST fan-out path. The two paths share only the client object (token + network/rate-limit handling).

This keeps the curated catalog and its declarative guarantees completely intact.

## 4. Auto-flatten to a table (no raw-JSON blob)

For each element under the resolved rows path (§5), `flatten_graphql_node(node)` produces one output row:

- **Scalars** → a column (`name`, `byteSize` → values).
- **Nested objects** → flattened with a `_` separator: `defaultBranchRef { name }` → `defaultBranchRef_name`;
  `agents: object { byteSize text }` → `agents_byteSize`, `agents_text`.
- **Nested arrays** → JSON-encoded into a single cell (we do **not** explode them into child tables in v1).
- **`null`** (e.g. a `Blob` field for a file that doesn't exist) → empty cell. *Presence is derivable*: a
  repo "has AGENTS.md" iff `agents_byteSize`/`agents_text` is non-empty.

**Columns** = the union of keys seen across all rows, in **first-seen order** (later-seen columns appended).
Every column is declared `STRING`. Output is written **headerless + manifest** (the component's invariant
from §6.5 of the base spec): the manifest names the columns and the primary key; the CSV carries no header.
A row missing a column writes an empty value for it.

## 5. Rows path — auto-detected, override available

GraphQL responses are arbitrarily nested, so the component must know which array is the row set. It is
**auto-detected from the first page's `data` object**:

> Descend from `data` through object hops. At each object: if exactly **one** child is an **array of
> objects** (after preferring a child named `nodes`, then `edges`, when several tie), that is the rows path.
> If there is no array child but exactly **one** object child, descend into it (`pageInfo` is an object, so
> it never competes — but `nodes`/`edges` win over it anyway). A response that bottoms out in a single
> object with no array → **one row** (flatten the whole object). Anything genuinely ambiguous — two sibling
> arrays of objects, or multiple object children with no array to follow — raises a clean `UserException`
> telling the user to set the override.

Example: `data → organization → repositories → { nodes[], pageInfo }` resolves to `organization.repositories.nodes`.

**Override field — "Row path (advanced)"**: a dotted path under `data`; empty = auto-detect. Tooltip:
> *"Dotted path under `data` to the list whose items become your table rows, e.g.
> `organization.repositories.nodes`. Leave empty to detect it automatically; set it only when the query
> returns more than one list and detection can't choose."*

When set, detection is skipped and the override is used verbatim (also used to locate the paginated
connection in §6).

## 6. Pagination — convention-based, automatic

GitHub GraphQL uses cursor pagination per connection. The component paginates the **rows-path connection**
automatically when the query opts in:

- The connection (rows path minus its trailing `nodes`/`edges` segment — e.g. `organization.repositories`)
  must select `pageInfo { hasNextPage endCursor }`.
- The query must declare a `$cursor: String` variable, used as `after: $cursor`.

Loop: POST page 1 → read `pageInfo.endCursor` at the connection path → set `variables["cursor"]` → POST
next → repeat while `hasNextPage`. If `pageInfo`/`$cursor` is absent, a single page is fetched. **Multiple
independently-paginated connections are out of scope** (documented); the user paginates the row connection
only. The total wait budget and retry handling are inherited from the existing client.

## 7. Templates (v1)

Templates make the common cases zero-GraphQL. Implemented as a **mode**, not text injection (Keboola sync
actions can't write into a textarea): a `query_template` enum selects a built-in query held in code; the
user fills only the template's parameters. `custom` reveals the raw `gql_query` / `gql_variables` /
`gql_row_path` fields instead.

A template is a small descriptor in code:

```python
@dataclass(frozen=True)
class GraphQLTemplate:
    query: str | Callable[[Configuration], str]  # static, or built from params (filenames)
    rows_path: str
    output_table: str          # default output table name
    primary_key: list[str]     # default PK
    variables: Callable[[Configuration], dict]  # config fields -> GraphQL variables
```

### 7.1 Flagship — `org_ai_config_files` ("Org repos: AI config files")

Answers the motivating question. Parameters: **`gql_org`** (organization login) and **`gql_filenames`**
(list, default `["AGENTS.md", "CLAUDE.md"]`). The query is **built from the filenames** — one aliased
`Blob` field per file; the alias is the filename sanitised to a GraphQL identifier (`AGENTS.md` →
`agents_md`, `.github/workflows/ci.yml` → `github_workflows_ci_yml`):

```graphql
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        url
        defaultBranchRef { name }
        agents_md: object(expression: "HEAD:AGENTS.md")  { ... on Blob { byteSize isBinary text } }
        claude_md: object(expression: "HEAD:CLAUDE.md")  { ... on Blob { byteSize isBinary text } }
      }
    }
  }
}
```

- `rows_path` = `organization.repositories.nodes`; PK default = `nameWithOwner`; table = `repo_ai_config_files`.
- `HEAD:` resolves to the default branch (any ref works via `branch:path`).
- Output (presence derivable from non-empty `*_byteSize`):

```
nameWithOwner | defaultBranchRef_name | agents_md_byteSize | agents_md_text | claude_md_byteSize | claude_md_text
octo/web      | main                  | 840                | # Agents...    |                    |
octo/api      | main                  | 1200               | # Agents...    | 512                | # Claude...
```

### 7.2 Second — `org_pull_request_reviews` ("Open PRs with review decision")

Illustrative, and shows GraphQL value beyond REST: `reviewDecision`
(`APPROVED`/`CHANGES_REQUESTED`/`REVIEW_REQUIRED`) has no REST equivalent. Parameters: `gql_org`, `gql_repo`.

```graphql
query($org: String!, $repo: String!, $cursor: String) {
  repository(owner: $org, name: $repo) {
    pullRequests(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url author { login } reviewDecision createdAt updatedAt }
    }
  }
}
```

`rows_path` = `repository.pullRequests.nodes`; PK default = `number`; table = `repo_open_pull_requests`.
This template is illustrative and may be swapped during implementation if a more useful second example
emerges; the flagship (§7.1) is the committed one.

> **v1 template scope:** a single `gql_org` (with the existing `listOrganizations` async select for
> convenience). Multi-org / "all orgs" fan-out for templates is a documented follow-up — for now, one row
> per org. `custom` mode already covers any shape.

## 8. Client path (`client.py`)

A new method on `GitHubClient`, isolated from the REST `fetch`:

```python
def graphql(self, query: str, variables: dict, rows_path: str | None) -> tuple[list[dict], str]:
    """POST /graphql, paginate the rows-path connection, return (flattened rows, resolved rows_path)."""
```

- POST `{base_url}/graphql` with JSON body `{"query": query, "variables": variables}`, same
  `Authorization`/timeout as the session; no `Accept: vnd.github+json` override needed.
- HTTP status routing reuses `_classify_response` (401 → token, 5xx/network → retry, 403/429 rate-limit →
  the existing wait-and-retry budget).
- **Body-level GraphQL errors** (GitHub returns HTTP 200 with `{data, errors}`):
  - `data is null` + `errors` → `UserException` joining the `errors[].message` strings (clean exit 1).
  - partial (`data` present **and** `errors`) → `logging.warning` the messages, proceed with `data`.
  - a `RATE_LIMITED` error type with no usable `data` → `UserException` advising to reduce query cost /
    retry later (GraphQL's secondary/points budget is not the REST header budget).
- Rows-path detection/override (§5) runs on page 1's `data`; the connection path (rows_path minus
  `nodes`/`edges`) locates `pageInfo` for §6.

`expand_records`/`flatten_record` in `endpoints.py` are REST-specific; GraphQL flattening
(`flatten_graphql_node`) lives alongside the GraphQL path (its own small module or a section of
`component.py`), since its rules (recursive object flattening, array→JSON, dynamic column union) differ.

## 9. Config model (`configuration.py`)

New fields (all optional; only meaningful when `endpoint == "graphql_query"`):

- `query_template: str = "custom"` — enum: `custom`, `org_ai_config_files`, `org_pull_request_reviews`.
- `gql_query: str = ""` — raw query (custom mode).
- `gql_variables: dict = {}` — parsed from a JSON object string; `{}` if blank. Invalid JSON → `UserException`.
- `gql_row_path: str = ""` — override (custom mode).
- `output_table: str = ""` — required (non-empty, safe table slug) in custom mode; templates default it.
- `primary_key: list[str] = []` — coerced from a comma-separated string or array (reuse the list-coercion
  validator pattern); empty = full-load only.
- `gql_org: str = ""`, `gql_repo: str = ""`, `gql_filenames: list[str] = ["AGENTS.md", "CLAUDE.md"]` —
  template parameters.

Validation (`model_validator(mode="after")`, only when `endpoint == "graphql_query"`):
- `custom` → `gql_query` and `output_table` non-empty.
- `org_ai_config_files` → `gql_org` non-empty; `gql_filenames` non-empty.
- `org_pull_request_reviews` → `gql_org` and `gql_repo` non-empty.
- `output_table` matches a safe table-name pattern.

All other (REST) validators are gated so they don't fire for the GraphQL source.

## 10. Output & incremental

- One table named by `output_table`, headerless + manifest, all columns `STRING` (§4).
- `primary_key` (post-flatten column names) drives **incremental upsert** when `load_type =
  incremental_load`; **full replace** otherwise. No `since`/`until` window (the query filters).
- Stateless — no `state.json` (consistent with the REST path post-fan-out spec §5).

## 11. Errors & rate limits

- HTTP-level: reuse `_classify_response` + the tenacity wait-and-retry (token/5xx/network/REST rate-limit).
- GraphQL body-level errors: §8.
- GraphQL points budget: surfaced as a clean `UserException` when the response is `RATE_LIMITED` with no
  data; v1 does not implement points-aware pre-throttling (documented follow-up).

## 12. Schema changes (`configRowSchema.json`)

- `endpoint` enum gains `graphql_query` (title "GraphQL Query"), placed last with a clear "advanced" hint.
- New fields, shown via `options.dependencies` keyed on `endpoint == "graphql_query"`:
  `query_template`, `gql_query` (textarea), `gql_variables` (textarea), `gql_row_path`, `output_table`,
  `primary_key`, `gql_org`, `gql_repo`, `gql_filenames`, and `load_type` (add `graphql_query` to its
  existing dependency list).
- Within the GraphQL group, fields are further conditioned on `query_template` (`custom` reveals
  `gql_query`/`gql_variables`/`gql_row_path`; templates reveal their parameter fields).
- The "Row path (advanced)" tooltip is §5's text.

> **Two-level conditional caveat (per the repo's established practice):** a field that depends on **both**
> `endpoint == graphql_query` **and** `query_template == X` cannot be expressed by a single
> `options.dependencies` map. The exact mechanism (nested dependencies, `oneOf`, or accepting that a
> template field shows whenever the group shows) is **verified in the Ctrl+D sandbox during
> implementation**, with the documented fallback: keep all GraphQL fields under the single
> `endpoint`-dependency and let template-vs-custom relevance be a soft convention (unused fields ignored by
> the model), rather than a hard hide. Mirrors the `optional_settings` collapse caveat in the fan-out spec.

## 13. Testing

- **Unit — flatten** (`flatten_graphql_node`): nested object → `parent_child`; array → JSON cell; null →
  empty; column union across heterogeneous rows in first-seen order.
- **Unit — rows-path detection**: `organization.repositories.nodes` resolves; `nodes` preferred over
  `edges`; single-object → one row; two sibling arrays → `UserException`; override skips detection.
- **Unit — config model**: custom requires `gql_query`+`output_table`; template param validation; bad
  `gql_variables` JSON → `UserException`; `primary_key` string→list coercion.
- **Unit — client**: `data:null + errors` → `UserException` (joined messages); partial data → warn +
  proceed; pagination loop follows `endCursor` until `hasNextPage=false`; no `pageInfo` → single page.
- **VCR / datadir functional**: record the flagship `org_ai_config_files` template over a small org (≥2
  repos, one with `AGENTS.md`, one without) → flattened `repo_ai_config_files.csv` with presence derivable;
  a custom paginated query (two pages) → merged table. Reuse the `VCR_SANITIZERS` chain (the GraphQL POST
  body carries the query, not secrets; `Authorization` is already redacted).

## 14. Backward compatibility

- Additive only: a new `endpoint` enum value + new optional config fields. Existing rows and the REST path
  are unchanged.
- No change to existing table names, schemas, PKs, or auth.

## 15. Out of scope (documented follow-ups)

- Type inference (non-STRING columns).
- Exploding nested arrays into child tables.
- Multiple independently-paginated connections in one query.
- Multi-org / all-orgs fan-out for templates.
- GraphQL points-budget-aware pre-throttling.
- More templates beyond the two in §7.
