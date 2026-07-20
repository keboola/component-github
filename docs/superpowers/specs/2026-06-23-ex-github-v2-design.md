# keboola.ex-github-v2 — Design Spec

> Type: extractor
> Component ID: `keboola.ex-github-v2`
> Vendor: `keboola` · Visibility: PRIVATE / UNPUBLISHED
> Status: draft
> Date: 2026-06-23
> Build branch: `initial-implementation`
> Research input: `scratchpad/phase2-research.md` (Phase 2, live-verified against `api.github.com`)

---

## 1. Overview & source system

A row-based Keboola extractor for the **GitHub REST API** (`https://api.github.com`, version
`2022-11-28`). Each config row defines **one extraction** — a single GitHub endpoint applied to a
single target (the authenticated user, an organization, or an `owner/repo`) — and produces one
output table. The component is a thin orchestrator over a **declarative endpoint registry**: 30+
endpoints are described as data, not code, so breadth scales without new methods.

- **Source system:** GitHub REST API. Docs: https://docs.github.com/en/rest
- **Primary use case:** pull GitHub organizational, repository, and activity data (orgs, members,
  teams, repos, issues, PRs, commits, releases, workflows, etc.) into Keboola Storage for analytics,
  with incremental loads where the API supports a cursor.
- **Auth:** BOTH a Personal Access Token (PAT) and OAuth (broker-injected). At the HTTP layer both
  are `Authorization: Bearer <token>` — one auth-agnostic client, two credential resolvers.

This is a **rebuild** of the legacy `github-v2` (6 endpoints, PAT-only, root-multiselect config).
The rebuild expands to the full catalog and replaces the non-scaling config model (see §5).

---

## 2. Keboola mapping

How GitHub concepts map onto how Keboola actually runs a component.

### 2.1 Config rows — one row = one extraction

**Decision (Tier A convention, applied):** multiple independent endpoints → **config rows, one row
per extraction**. Root config holds auth + global options; each row holds one endpoint selection +
its target + endpoint-specific filters. Rows run **sequentially** by default (parallelism is opt-in
and not enabled here — GitHub's 5000 req/hr is a shared per-token budget, so parallel rows would race
the same rate-limit bucket; see §9). Each row gets its own `state.json`, so incremental watermarks
fall out per-extraction for free.

This deliberately **breaks from the legacy model** (root `endpoints` multiselect + per-row
`organization_name`/`repository_owner`/`repository_name`). That model couples a global endpoint list
to per-row targets, doesn't say which endpoint uses which row, and collapses to one shared run. It
does not scale to 30+ endpoints. See §5.2 for the justification in full.

### 2.2 Endpoint → output table mapping

Each row writes exactly one table. Table name = the registry's `table` for the chosen endpoint
(stable, not runtime-generated). Default bucket is ON (the Dev Portal `default_bucket` property — set
it in Phase 6 and confirm the exact property casing the portal accepts), so tables land in
`in.c-keboola-ex-github-v2-<configId>` and the component must **not** hardcode a `destination` (a
hardcoded destination is silently overridden by the default bucket). Full endpoint catalog with PK /
pagination / incremental / required params is in §4.

### 2.3 Incremental strategy → output mapping + state

- Endpoints with a server-side `since` filter (issues, commits, issue comments, PR review comments)
  or sort-by-`updated` (pulls, org/user repos): row-level `load_type` defaults to `incremental_load`.
  Incremental output mapping (`incremental=True`) + the registry's primary key → **upsert**.
- `state.json` stores a single key `last_run` = ISO-8601 UTC timestamp captured **before** fetch,
  written **after** a successful write. First run: `last_run` absent → seed from row's optional
  `initial_since`, else full history.
- Endpoints with no incremental signal (branches, tags, teams, members, languages, releases, …)
  always use **full_load (replace), not upsert** — even if the row's `load_type` is
  `incremental_load`, the output mapping is forced to `incremental=False` so rows deleted on GitHub
  are purged each run instead of lingering forever. No `since`/state watermark applies to these
  snapshot endpoints. (Intentional correction to the original draft of this section, which had them
  upsert; see Phase 8 audit TODO 2.)

### 2.4 Secrets → `#`-prefixed

PAT is `parameters.#github_token` (encrypted `KBC::ProjectSecure`). OAuth secrets are never in
`parameters` — they arrive under `authorization.oauth_api.credentials` (`#data`, `#appSecret`),
decrypted by the Job Queue at runtime. See §3.

### 2.5 Sync actions

`testConnection`, `listOrganizations`, `listRepositories`, `listTeams` (§5.4). Used to validate the
token and to populate the org/repo/team dropdowns instead of free-text.

### 2.6 Output bucket / naming

Dev Portal `default_bucket` ON, stage `in` (`defaultBucketStage: in`). Tables resolve to
`in.c-keboola-ex-github-v2-<configId>`. No hardcoded destinations (they are silently overridden).
Confirm the exact `default_bucket` property casing at Phase 6 registration so the flag takes effect.

---

## 3. Authentication & connection

### 3.1 Two credential sources, one HTTP path

GitHub accepts every token type as `Authorization: Bearer <token>`. The component therefore has ONE
auth-agnostic HTTP client and TWO credential resolvers that produce a token string:

| Path | Where token comes from | Resolver |
|------|------------------------|----------|
| **PAT** | `parameters.#github_token` (user-pasted secret, decrypted at runtime) | read the field directly |
| **OAuth** | `authorization.oauth_api.credentials.#data` (broker-injected, decrypted at runtime) | parse `#data` as JSON, read `access_token` |

The active path is chosen by a root config field `auth_type` ∈ {`pat`, `oauth`} (default `pat`). The
schema uses `options.dependencies` to show the `#github_token` field only when `auth_type == "pat"`,
and an `authorization` (OAuth widget) section only when `auth_type == "oauth"`.

### 3.2 OAuth mechanics (Keboola broker)

- Config declares the broker reference (no secrets in plaintext):
  `"authorization": { "oauth_api": { "id": "{OAUTH_API_ID}", "version": 3 } }`.
- At runtime the Job Queue injects `authorization.oauth_api.credentials` with `appKey` (client_id,
  plaintext), `#appSecret` (client_secret), and `#data` (the provider's **raw token response as a
  string**). The broker has no knowledge of GitHub's response shape, so the component parses `#data`
  itself: `json.loads(#data)["access_token"]`.
- GitHub OAuth provider endpoints (for the broker registration, not the component): authorize
  `https://github.com/login/oauth/authorize`, token `https://github.com/login/oauth/access_token`.
- **Standard OAuth App with token expiration DISABLED** → non-expiring `gho_…` token, no refresh
  logic in the component. This is the recommended registration mode.

### 3.3 OAuth scopes (read-only, "maximum visibility")

`read:org`, `repo`, `read:user`, `user:email`. (`read:discussion` is **not** a valid OAuth scope —
dropped. `repo` covers private-repo reads; `read:org` covers org/team/member data.)

### 3.4 Provisioning

- **PAT path — buildable & shippable NOW, zero external dependency.** User self-mints a classic
  (`ghp_…`) or fine-grained (`github_pat_…`) PAT in the GitHub UI; pastes it as `#github_token`.
  Required scopes documented in the schema/README: `read:org`, `repo`. SAML-SSO orgs require the PAT
  to be SSO-authorized.
- **OAuth path — code built & tested NOW via the credentials-injection bypass.** A real `gho_` token
  response is pasted directly into `#data`/`appKey`/`#appSecret` via the Storage configuration API
  (the documented bypass); `#`-fields encrypt/decrypt normally, so the runtime OAuth resolver path is
  exercised without the live broker. The **production** OAuth dance needs Keboola support to register
  the GitHub OAuth App in the broker (NOT self-serve) — see §9 (risk R1). This dependency does **not**
  block the PAT path or local OAuth testing.

### 3.5 Connection method

REST/JSON only. Base `https://api.github.com`. Headers on every request: `Accept:
application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Authorization: Bearer <token>`.
GitHub Enterprise Server (`https://HOST/api/v3`) is **out of scope** (no configurable base URL in v1).

---

## 4. Data model & endpoints

### 4.1 The endpoint catalog (full — 33 endpoints)

The registry is the single source of truth. Columns: **Table** (output table name = registry key),
**Path** (template; `{org}`, `{owner}`, `{repo}`, `{team_slug}`, `{pull_number}` from row params),
**Target** (what the row must supply), **PK**, **Pag.** (`link` = follow Link rel=next; `none` =
single object/array), **Incr.** (`since` / `updated` / none), **Envelope** (`array` = top-level JSON
array; `object` = single JSON object → one row; `wrapped:<key>` = `{total_count, <key>:[…]}`).

#### A. Authenticated user

| Table | Path | Target | PK | Pag. | Incr. | Envelope |
|---|---|---|---|---|---|---|
| `user` | `GET /user` | none | `id` | none | none | object |
| `user_repos` | `GET /user/repos` | none | `id` | link | `updated` (sort) | array |
| `user_orgs` | `GET /user/orgs` | none | `id` | link | none | array |

#### B. Organizations

| Table | Path | Target | PK | Pag. | Incr. | Envelope |
|---|---|---|---|---|---|---|
| `organizations` | `GET /orgs/{org}` | org | `id` | none | none | object |
| `organization_members` | `GET /orgs/{org}/members` | org | `id` | link | none | array |
| `organization_teams` | `GET /orgs/{org}/teams` | org | `id` | link | none | array |
| `team_members` | `GET /orgs/{org}/teams/{team_slug}/members` | org+team_slug | `id` | link | none | array |
| `organization_repositories` | `GET /orgs/{org}/repos` | org | `id` | link | `updated` (sort) | array |
| `organization_events` | `GET /orgs/{org}/events` | org | `id` | link | none (≈90d retention) | array |
| `organization_outside_collaborators` | `GET /orgs/{org}/outside_collaborators` | org | `id` | link | none | array |

#### C. Repositories (owner + repo)

| Table | Path | Target | PK | Pag. | Incr. | Envelope |
|---|---|---|---|---|---|---|
| `repository` | `GET /repos/{owner}/{repo}` | owner+repo | `id` | none | none | object |
| `repository_branches` | `GET /repos/{owner}/{repo}/branches` | owner+repo | `name` | link | none | array |
| `repository_tags` | `GET /repos/{owner}/{repo}/tags` | owner+repo | `name` | link | none | array |
| `repository_commits` | `GET /repos/{owner}/{repo}/commits` | owner+repo | `sha` | link | `since` | array |
| `repository_contributors` | `GET /repos/{owner}/{repo}/contributors` | owner+repo | `id` | link | none | array |
| `repository_collaborators` | `GET /repos/{owner}/{repo}/collaborators` | owner+repo | `id` | link | none | array |
| `repository_languages` | `GET /repos/{owner}/{repo}/languages` | owner+repo | `repository,language` | none | none | object (map → rows) |
| `repository_topics` | `GET /repos/{owner}/{repo}/topics` | owner+repo | `repository,topic` | none | none | object (`names[]` → rows) |
| `repository_releases` | `GET /repos/{owner}/{repo}/releases` | owner+repo | `id` | link | none | array |
| `repository_issues` | `GET /repos/{owner}/{repo}/issues` | owner+repo | `id` | link | `since` | array (PR-mixing, see 4.4) |
| `repository_issue_comments` | `GET /repos/{owner}/{repo}/issues/comments` | owner+repo | `id` | link | `since` | array |
| `repository_pulls` | `GET /repos/{owner}/{repo}/pulls` | owner+repo | `id` | link | `updated` (sort, no `since`) | array |
| `repository_pull_reviews` | `GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews` | owner+repo+pull_number | `id` | link | none | array |
| `repository_pull_review_comments` | `GET /repos/{owner}/{repo}/pulls/comments` | owner+repo | `id` | link | `since` | array |
| `repository_labels` | `GET /repos/{owner}/{repo}/labels` | owner+repo | `id` | link | none | array |
| `repository_milestones` | `GET /repos/{owner}/{repo}/milestones` | owner+repo | `id` | link | none | array |
| `repository_stargazers` | `GET /repos/{owner}/{repo}/stargazers` | owner+repo | `id` | link | none | array |
| `repository_subscribers` | `GET /repos/{owner}/{repo}/subscribers` | owner+repo | `id` | link | none | array |
| `repository_forks` | `GET /repos/{owner}/{repo}/forks` | owner+repo | `id` | link | none | array |
| `repository_workflows` | `GET /repos/{owner}/{repo}/actions/workflows` | owner+repo | `id` | link | none | wrapped:`workflows` |
| `repository_workflow_runs` | `GET /repos/{owner}/{repo}/actions/runs` | owner+repo | `id` | link | `created` (filter) | wrapped:`workflow_runs` |
| `repository_deployments` | `GET /repos/{owner}/{repo}/deployments` | owner+repo | `id` | link | none | array |
| `repository_webhooks` | `GET /repos/{owner}/{repo}/hooks` | owner+repo | `id` | link | none | array |

**Count: 33 tables** (3 user + 7 org + 23 repo). PK is the table's natural key — GitHub numeric `id`
for most resources; `name` for branches/tags; `sha` for commits; composite synthetic keys for the two
object-map endpoints (languages, topics — see 4.5).

### 4.2 Pagination

Follow the **`Link` header `rel="next"` URL verbatim** until absent. Always send `per_page=100`. Do
**not** hand-construct `page=N` URLs — some endpoints (e.g. issues) return opaque `after` cursors
(live-verified). Single-object endpoints (`object` envelope) and the object-map endpoints don't
paginate. Stopping condition is explicit: loop ends when no `rel="next"` link is present (satisfies
the architecture checklist "pagination has an explicit stopping condition").

### 4.3 Rate limits

- Authenticated `core` budget: **5000 req/hr**. Every response carries `x-ratelimit-remaining`,
  `x-ratelimit-reset` (epoch seconds), `x-ratelimit-limit`, `x-ratelimit-resource`.
- **Handling (wait-and-retry, bounded):**
  1. `Retry-After` header present (secondary/abuse limit) → sleep that many seconds.
  2. Else 403/429 with `x-ratelimit-remaining: 0` → sleep until `x-ratelimit-reset` (+small buffer).
  3. Else transient 5xx/network → exponential backoff, base 60 s.
  - All waits are bounded by a **total wait budget** (`MAX_RATE_LIMIT_WAIT_SECONDS`, default 3600 s)
    and a **max retry count**. Exceeding the budget raises `UserException` (exit 1) with a clear
    message ("GitHub rate limit not recovered within the wait budget; reduce scope or re-run later"),
    never an unhandled crash (exit 2).
- The watermark-after-success rule (§2.3) means a budget-blown run keeps the old `last_run` and a
  re-run resumes rather than re-pulling.

### 4.4 The `/issues` PR-mixing caveat (handled, not just documented)

`GET /repos/{owner}/{repo}/issues` returns **pull requests mixed in** — every PR is also an issue, and
PR rows carry a `pull_request` sub-object that pure issues lack. The `repository_issues` extraction
**drops rows where `pull_request` is present**, so the issues table contains only true issues. PRs are
covered by the dedicated `repository_pulls` extraction. This split prevents double-counting. (Behaviour
documented in the row's tooltip and README.)

### 4.5 Notable response shapes → flattening

- **Object-map endpoints.** `repository_languages` returns `{"Python": 1234, "Go": 567}` → emit one
  row per language `{repository, language, bytes}`, PK `(repository, language)`. `repository_topics`
  returns `{"names": ["a","b"]}` → one row per topic `{repository, topic}`, PK `(repository, topic)`.
  The synthetic `repository` column is the row's `owner/repo` so the table is meaningful across rows.
- **Wrapped envelopes.** `/actions/workflows` and `/actions/runs` return
  `{total_count, workflows:[…]}` / `{…, workflow_runs:[…]}`. The registry's `envelope: wrapped:<key>`
  tells the parser to read the named array and paginate it. `total_count` is **not** emitted as a
  column.
- **Nested objects → flatten to scalar columns.** GitHub objects nest heavily (`user.login`,
  `repository.owner.login`, `commit.author.date`). The extractor flattens selected nested fields to
  dotted/underscored scalar columns per the registry's declared output schema (§6.3); it does **not**
  dump whole sub-objects as JSON blobs, except variable-length arrays (e.g. `labels` on an issue),
  which may stay a single JSON column. Internal/pagination keys (e.g. `_links`) are stripped.
- **A repo context column.** Repo- and org-scoped tables carry the row's target as explicit columns
  (`owner`, `repo` / `org`) so multi-row outputs into one table are disambiguated and joinable.

---

## 5. Configuration & schema

The actual `configSchema.json` / `configRowSchema.json` JSON is authored by **`component-build-ui`**
in Phase 6. This section specifies every field so that work is mechanical.

### 5.1 Root config (`configSchema.json`) — authentication + global options

| Field | Type | Title | Secret | Default | Notes / `propertyOrder` |
|---|---|---|---|---|---|
| `auth_type` | string enum `pat`\|`oauth` | "Authentication Method" | no | `pat` | `enum_titles` ["Personal Access Token", "OAuth"]; gates the fields below. PO 10 |
| `#github_token` | string (password) | "GitHub Personal Access Token" | **yes** | — | Shown only when `auth_type=="pat"` (`options.dependencies.auth_type: ["pat"]`). Tooltip: how to mint + scopes `read:org, repo`. PO 20 |
| `authorization` | object (OAuth widget) | "GitHub OAuth" | n/a | — | Shown only when `auth_type=="oauth"`. Standard Keboola OAuth section (`oauth_api`). PO 30 |
| `incremental` | boolean | "Incremental Output" | no | `true` | Global default for `load_type`; a row may still be effectively full-load where no cursor exists. PO 100 |
| `debug` | boolean | "Debug Mode" | no | `false` | Verbose logging. PO 1000. **Not** a Pydantic field — consumed by the component base (see §6.4). |

`required`: `["auth_type"]` (the token requirement is conditional and validated in code, since OAuth
rows have no `#github_token`). Root has 5 fields across two concerns (auth, global options) — within
the flat-list threshold, but auth fields are visually grouped by the `auth_type` dependency.

### 5.2 Config row (`configRowSchema.json`) — one extraction

**THE key design decision.** A row = one endpoint + its target + endpoint-specific filters. Fields:

| Field | Type | Title | Default | Notes / `propertyOrder` |
|---|---|---|---|---|
| `endpoint` | string enum (33 registry keys) | "Data Source" | — | `enum_titles` = human labels ("Repository Issues", …). The single field that selects the registry entry. `autoload` not needed (fixed enum). PO 10 |
| `organization` | string (async select) | "Organization" | "" | `"enum": []`; `options.async.action: listOrganizations`, `autoload: true`. Shown when the chosen endpoint's target needs an org (`options.dependencies.endpoint: [<org endpoints>]`). PO 20 |
| `team_slug` | string (async select) | "Team" | "" | `"enum": []`; `options.async.action: listTeams` (depends on `organization`), `autoload: ["organization"]`. Shown for `team_members`. PO 30 |
| `repository` | string (async select) | "Repository" | "" | `"enum": []`; `options.async.action: listRepositories` (depends on `organization`), `autoload: ["organization"]`. Value = `owner/repo`; the component splits it. Shown for repo endpoints. PO 40 |
| `pull_number` | integer | "Pull Request Number" | null | Shown only for `repository_pull_reviews`. PO 50 |
| `load_type` | string enum `full_load`\|`incremental_load` | "Load Type" | `incremental_load` | `enum_titles` ["Full Load", "Incremental Load"]. Shown for incremental-capable endpoints. PO 60 |
| `initial_since` | string | "Initial Since (ISO-8601)" | "" | First-run lower bound for `since`/`updated` endpoints. Tooltip example `2024-01-01T00:00:00Z`. Shown for incremental-capable endpoints. PO 70 |
| `issue_state` | string enum `all`\|`open`\|`closed` | "Issue State" | `all` | Shown for `repository_issues`. `enum_titles` ["All","Open","Closed"]. PO 80 |
| `pull_state` | string enum `all`\|`open`\|`closed` | "Pull Request State" | `all` | Shown for `repository_pulls`. PO 81 |
| `milestone_state` | string enum `all`\|`open`\|`closed` | "Milestone State" | `all` | Shown for `repository_milestones`. PO 82 |

All target/filter fields are **conditional via `options.dependencies`** keyed on `endpoint`, so the row
form only shows what the chosen endpoint needs. `repository`/`organization`/`team_slug` are async
selects with `"enum": []` (required for async fields) populated by sync actions.

**Why this model (justification).** The legacy model put a global `endpoints` multiselect in the root
and per-row `organization_name`/`repository_owner`/`repository_name`. That is ambiguous (which row
feeds which endpoint?), collapses every endpoint into one run with shared state, and forces every row
to carry every possible target field. It does not scale to 33 endpoints. The chosen model makes the
**row the unit of extraction**: one endpoint, one target, its own state and incremental cursor, its
own enable/disable/schedule/retry. Adding an endpoint is a registry entry + an `enum` value, not a
schema redesign. Targets are async dropdowns (valid values, not free-text typos). This is the
canonical Keboola "one row per object" convention applied at endpoint granularity.

### 5.3 Pydantic config models (`src/configuration.py`)

Mirrors the schema; the platform delivers a single merged `config.json` (root params + the row's
params flattened together). Two models — root and row — composed into one `Configuration` the
component validates early. (See §6.4 for the `auth_type`/cross-field validation and the `debug`
exclusion.)

### 5.4 Sync actions

| Action | Endpoint | Returns | Notes |
|---|---|---|---|
| `testConnection` | `GET /rate_limit` | success / `UserException` | **Free** (doesn't consume quota). 200 → ok; 401 → "Invalid or expired token". Resolves token via the same resolver as `run()`. |
| `listOrganizations` | `GET /user/orgs` | `[{label: login, value: login}]` | Only orgs the token's user can see. |
| `listRepositories` | `GET /orgs/{org}/repos` (org set) else `GET /user/repos` | `[{label: "owner/repo", value: "owner/repo"}]` | Depends on the row's `organization`. |
| `listTeams` | `GET /orgs/{org}/teams` | `[{label: name, value: slug}]` | Depends on `organization`. |

All sync actions: read-only, no state writes, no output tables; use `self.client` from `__init__`;
`testConnection` raises `UserException` on failure, dropdowns return the list. Every action must be
registered in the Dev Portal `actions` list (Phase 6) or it 404s on-platform.

---

## 6. Code architecture

Self-documenting, registry-driven. `run()` is a thin orchestrator; logic in private methods.

### 6.1 Files

| File | Responsibility |
|---|---|
| `src/component.py` | `Component(ComponentBase)`. `__init__` resolves credentials + builds the `GitHubClient`. `run()` (<30 lines) selects the registry entry, fetches, writes, advances state. Sync-action handlers (`@sync_action`). |
| `src/configuration.py` | Pydantic `Configuration` (root + row), `LoadType` enum, `AuthType` enum, cross-field validation. Raises `UserException` on `ValidationError`. |
| `src/client.py` | `GitHubClient` — **auth-agnostic** HTTP: session with `Authorization: Bearer <token>` + standard headers, pagination via Link rel=next, rate-limit wait-and-retry, response-envelope unwrapping. Knows nothing about where the token came from. |
| `src/credentials.py` | `resolve_token(configuration, oauth_credentials) -> str` — the two resolvers (PAT field vs OAuth `#data` JSON parse). The only auth-aware code. |
| `src/endpoints.py` | The **endpoint registry**: `ENDPOINTS: dict[str, Endpoint]`. Each `Endpoint` is a frozen dataclass descriptor (see 6.3). Also the flattening/schema definition per endpoint. |

### 6.2 `run()` orchestration (the thin path)

1. `cfg = Configuration(**self.configuration.parameters)` — validate early.
2. Look up `endpoint = ENDPOINTS[cfg.row.endpoint]`; build the concrete path from row target params
   (raise `UserException` if a required target is missing — belt-and-braces behind the schema).
3. Compute `since` (incremental + state `last_run` or `initial_since`); capture `run_started_at`.
4. `rows = self.client.fetch(endpoint, path, params, since=...)` — client handles pagination,
   rate limits, envelope unwrapping; yields already-unwrapped records.
5. Transform via the endpoint's flattener; build `create_out_table_definition(table, primary_key,
   incremental, schema=...)`; write CSV; `write_manifest`.
6. On success: `write_state_file({"last_run": run_started_at.isoformat()})`.

The per-endpoint differences (path, params, PK, envelope, incremental field, flattener, schema) are
**data in the registry**, so `run()` is identical for all 33 endpoints — no per-endpoint methods.

### 6.3 The endpoint registry shape

```python
@dataclass(frozen=True)
class Endpoint:
    name: str                      # registry key == output table name
    path_template: str             # "/repos/{owner}/{repo}/issues"
    target: Target                 # enum: NONE | ORG | ORG_TEAM | OWNER_REPO | OWNER_REPO_PULL
    primary_key: list[str]         # ["id"] | ["name"] | ["sha"] | ["repository","language"]
    envelope: Envelope             # ARRAY | OBJECT | wrapped key name
    incremental_param: str | None  # "since" | None  (server-side filter param name)
    incremental_sort: bool         # True for sort=updated endpoints with no `since`
    default_params: dict[str, str] # {"per_page": "100", "sort": "updated", "direction": "desc"}
    filter_params: tuple[str, ...] # row fields injected as query params (issue_state→state, …)
    schema: list[ColumnDef]        # explicit output columns + native types + flatten source path
    drop_predicate: str | None     # "pull_request" → drop rows having this key (issues PR-split)
```

`ENDPOINTS` is a `dict[str, Endpoint]` of 33 entries. Adding an endpoint = one entry + one `enum`
value in the row schema. The schema's `ColumnDef` carries the dotted source path for flattening and
the Keboola native type (§6.5), so output columns are explicit, not inferred.

### 6.4 Configuration model details

- `AuthType(str, Enum)`: `pat`, `oauth`. `LoadType(str, Enum)`: `full_load`, `incremental_load`.
- Root model: `auth_type: AuthType = AuthType.pat`, `github_token: str = Field(alias="#github_token",
  default="")`, `incremental: bool = True`. Row model: `endpoint`, target fields (all defaulting to
  `""`/`None` so sync-action-populated fields don't fail validation), `load_type`, `initial_since`,
  filters. `model_config = ConfigDict(populate_by_name=True, extra="ignore")`.
- **Cross-field validation:** a `model_validator` raises `UserException` if `auth_type=="pat"` and
  `github_token` is empty (OAuth tokens validated by presence of injected credentials at runtime).
- **No `debug` field in the model.** The platform `debug` parameter is consumed by `ComponentBase`
  (it switches the root logger to DEBUG automatically). Adding a model `debug` field is redundant and
  flagged by the configuration checklist — so it is intentionally omitted. `debug` stays in the JSON
  schema only.

### 6.5 Native data types

`dataTypeSupport: authoritative` (set in Dev Portal, Phase 6 — verify the live value after the flip;
until it is flipped the platform silently downgrades a correct `schema` manifest to legacy hints). The
component emits a `schema` manifest (`data_type.base.type`) with meaningful types: `INTEGER` for
ids/counts, `TIMESTAMP` for `created_at`/`updated_at`/`pushed_at`, `BOOLEAN` for flags, `STRING` for
the rest. The `ColumnDef` in the registry carries the base type, so output columns carry real types
(an all-STRING authoritative schema is a finding).

**Headerless invariant (hard rule).** The `schema`/authoritative manifest defaults to
`has_header: false`. CSVs are written **headerless** and never with a header row, so the `schema`
alone names the columns and there is no header/`has_header` mismatch. If a header is ever added (e.g.
for debugging), `has_header=True` MUST be passed to `create_out_table_definition`, or Storage ingests
the header text as a data row and the load fails (a literal `"id"` rejected into an INTEGER column).
This cannot be caught by local file-diff tests — it is verified only on the real Storage smoke run
(§8).

### 6.6 Error handling (exit codes)

- `UserException` (exit 1): validation errors, missing required target params, 401/403-auth ("token
  invalid/insufficient scope"), 404 on a user-supplied org/repo ("not found or no access"), rate-limit
  wait budget exceeded. All carry actionable messages.
- Unexpected (exit 2): programming errors, unhandled 5xx after retries exhausted on a non-rate-limit
  path. The `__main__` guard (already scaffolded) maps `UserException`→1, `Exception`→2.

### 6.7 Dependencies

- `keboola.component` (base, manifests, state, `@sync_action`) — already present.
- `requests` for HTTP (session, headers, Link parsing) — explicit, well-understood, easy to VCR. (No
  GitHub SDK: the catalog is data-driven and the SDK would obscure pagination/rate-limit control.)
- `pydantic` (config) — already present.
- Scratch files (if any) go to `/tmp`, never `data/out/tables/`.

---

## 7. Testing

### 7.1 Datadir tests (`keboola.datadirtest`)

- Happy path per representative envelope shape (not all 33): one `array`+`since` endpoint
  (`repository_issues`), one `object` endpoint (`repository`), one `wrapped` endpoint
  (`repository_workflows`), one object-map endpoint (`repository_languages`), one org endpoint
  (`organization_members`). Each asserts the produced CSV (a real row in `expected/`), the manifest
  (PK, incremental, schema/native types), and headerless output.
- Incremental: a test with seeded `in/state.json` `last_run` asserts `since` is applied and the new
  `last_run` is written; a first-run test (no state) asserts `initial_since`/full behaviour.
- Failure cases → exit codes: missing required target (`UserException`, exit 1); 401 unauthorized
  (exit 1); rate-limit budget exceeded (exit 1, with the wait bounded low in the fixture). Fixtures
  carry **no real credentials** (placeholder token; OAuth `#data` a placeholder JSON).
- The PR-mixing split: an issues cassette containing a `pull_request` row asserts that row is dropped.

### 7.2 VCR functional tests (`keboola.datadirtest` + `vcrpy`)

- Cassettes for the breadth of endpoints, not one happy path: at minimum one per envelope shape plus
  the legacy-parity CORE set (organizations, organization_members, organization_teams,
  organization_repositories, repository_issues, repository_commits) and the new CORE (repository,
  repository_branches, repository_pulls). Multi-endpoint breadth is a checklist requirement.
- Success cassettes record a real `2xx` with real rows (not empty/error snapshots). Failure cassettes:
  one **401** (bad token), one **rate-limit** (403 + `x-ratelimit-remaining: 0` + near-future
  `x-ratelimit-reset`) to exercise the wait-and-retry → bounded `UserException`.
- **Token sanitization for BOTH paths.** A VCR `before_record_request` scrubber replaces the
  `Authorization` header with `Bearer ***`. For OAuth, the recorded request never contains `#data`
  (the token is already resolved to a `Bearer` header), but the test config fixture's `#data` is a
  placeholder, and any response body field that could leak a token is scrubbed. Cassettes are
  committed (not gitignored).
- Pagination: a cassette with a `Link: rel="next"` across two pages asserts both pages are followed
  and concatenated, and that traversal stops when `rel="next"` is absent.

### 7.3 Sync action tests

Each sync action has at least a basic test against a cassette: `testConnection` success (200
`/rate_limit`) and failure (401 → `UserException`); `listOrganizations`/`listRepositories`/`listTeams`
return `[{label, value}]` of the right shape.

### 7.4 Seed payloads

Phase 2 already captured live shapes from `octocat/Spoon-Knife` (issues with `pull_request`, Link
`after` cursor, rate-limit headers). Those seed the issues + pagination cassettes. Maintainer provides
a real PAT at Phase 5/7 to record the remaining cassettes.

---

## 8. Deployment & validation (cf-dev project)

- **Image:** the `initial-implementation` branch build (CI builds a branch-tagged image). **No
  promotion, no merge** for the smoke test.
- **Config:** created via `kbagent` in the **cf-dev** project, with `runtime.tag` overridden to the
  `initial-implementation` build tag (not by promoting the default tag). One config, a few rows
  spanning envelope shapes (e.g. `organizations` for a test org, `repository_issues` +
  `repository_commits` + `repository` for a test repo).
- **Auth:** smoke the **PAT** path with the maintainer-provided token; smoke **OAuth** only if the
  broker registration (R1) has landed by then — otherwise OAuth is validated locally via the
  credentials-injection bypass and the platform OAuth smoke is deferred.
- **Success looks like:** a job with `status: success`, exit 0, the expected output tables present in
  `in.c-keboola-ex-github-v2-<configId>` with non-zero row counts (issues > 0, commits > 0), and the
  resolved image tag matching the branch build. The native-type/`has_header` correctness can only be
  confirmed on this real Storage import (local tests diff files; they don't run a Storage load).

---

## 9. Open risks & blockers

| # | Risk | Severity | Owner | Mitigation |
|---|---|---|---|---|
| R1 | **OAuth broker provisioning.** Production OAuth needs Keboola support to register the GitHub OAuth App (client id/secret, authorize/token URLs, scopes) in the OAuth broker for `keboola.ex-github-v2` — **not self-serve**. Already in motion. | High (for prod OAuth only) | Maintainer + Keboola support | Build & test OAuth NOW via the credentials-injection bypass; PAT path ships independently; document the dependency, don't block. |
| R2 | **Rate-limit exhaustion on large orgs.** 5000 req/hr is a shared per-token budget; a config with many repo rows + deep pagination can exhaust it. | Medium | Component | Bounded wait-and-retry (§4.3); `UserException` (exit 1) with guidance on budget blow; watermark-after-success lets a re-run resume; rows run sequentially (no parallel contention on the same token). |
| R3 | **`/issues` PR-mixing.** Issues endpoint returns PRs. | Low (handled) | Component | Drop rows with a `pull_request` key; PRs via `repository_pulls`. Documented + tested (§4.4, §7.1). |
| R4 | **Opaque pagination cursors.** Some endpoints return `after` cursors, not page numbers. | Low (handled) | Component | Follow `Link` rel=next verbatim; never hand-build URLs (§4.2). |
| R5 | **Fine-grained vs classic PAT scope differences.** A fine-grained PAT scoped to selected repos/orgs may 403/404 on endpoints a classic-PAT user expects to work. | Low | User | Document required scopes; surface 403/404 as `UserException` with "check token scope/access". |
| R6 | **`pulls` has no `since`.** Incremental PRs must sort by `updated` desc and stop paging once `updated_at < last_run`. | Low (handled) | Component | `incremental_sort` registry flag drives client-side stop-paging (§4.1, §2.3). |
| R7 | **SAML-SSO orgs.** A classic PAT must be SSO-authorized to see SSO-protected orgs. | Low | User | Documented in schema tooltip + README. |
```
