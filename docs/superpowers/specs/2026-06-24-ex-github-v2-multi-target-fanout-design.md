# ex-github-v2 — Multi-target rows, fan-out & relative dates

**Date:** 2026-06-24
**Status:** Approved design — pending implementation plan
**Builds on:** `2026-06-23-ex-github-v2-design.md` (the single-target row model this revises)

## 1. Motivation

The shipped model is **one row = one endpoint + exactly one target**. In practice that means:

- A user who wants "commits for every repo in an org" must create one row per repo — dozens of rows.
- An empty target field is a hard error (the 29 failures observed in job `48673238` were all empty
  `organization`/`repository`/`team` targets), even though "all of them" is the obvious intent.
- The target pickers are single-select, so even a handful of repos is tedious to configure.

This revision makes a **single row cover many targets**: the `organization`, `repository`, and `team`
fields become **multi-select**, an **empty** field means **"all in scope"**, and `run()` **fans out**
over the resolved target set into one output table. It also adds relative-date support to
`initial_since` and tidies the form's optional fields.

## 2. Approved decisions

| Decision | Choice |
|---|---|
| Which fields become multi-select | `organization`, `repository`, `team` (Pull Request Number stays a single integer) |
| Empty multi-select means | "all in scope" (see §4 fan-out table) |
| Incremental fetch cursor | **Per-target watermark** in `state.json` (state bloat is acceptable) |
| Incremental column picker | **None** — GitHub fixes the `since`/`created` field per endpoint; user only chooses full vs incremental via `load_type` |
| Relative dates on `initial_since` | **In scope** — accept "5 days ago"/"yesterday" + ISO-8601, via `dateparser` |
| Optional fields | Grouped under a collapsed **"Optional settings"** section |

## 3. Schema changes (`configRowSchema.json`)

### 3.1 Multi-select async targets

`organization`, `repository`, `team_slug` change from single async `select` (`type: string`) to an
**async multi-select** (`type: array`). Shape (per `component-build-ui` references — `examples.md:179`):

```json
"repository": {
  "type": "array",
  "title": "Repositories",
  "format": "select",
  "uniqueItems": true,
  "items": { "type": "string", "enum": [] },
  "description": "Leave empty to extract all repositories in the selected organization(s).",
  "options": {
    "async": { "label": "Load Repositories", "action": "listRepositories", "autoload": ["organization", "endpoint"] },
    "tooltip": "Pick one or more repositories. Empty = all repos in the selected organization(s), or all your repos if no organization is set.",
    "dependencies": { "endpoint": [ <repo endpoints> ] }
  }
}
```

- `organization` (PO 20) — async multi-select; shown for **org-scoped *and* repo-scoped** endpoints
  (today it is hidden for repo endpoints — that gap is why repo listing fell back to `/user/repos`).
  It is the **defining scope field**: it both fans out org endpoints and scopes the repo/team pickers.
- `repository` (PO 40) — async multi-select; value items are `owner/repo`.
- `team_slug` → keep key, retitled **"Teams"** (PO 30) — async multi-select; shown for `team_members`.
  Because teams are org-scoped, item **values are qualified `org/team_slug`** (not a bare slug, which
  would be ambiguous across multiple selected orgs); `listTeams` enumerates across the selected org(s)
  and emits qualified values with a humanized `org / Team Name` label.
- `pull_number` (PO 50) — unchanged single integer.

The `endpoint` (Data Source) field is unchanged. Empty is now **valid** for the target fields, so the
"requires org/repo/team" error disappears for the normal case.

### 3.2 "Optional settings" section

`load_type`, `initial_since`, `issue_state`, `pull_state`, `milestone_state` move into a named
`type: object` section `optional_settings` with `"format": "grid"`, PO 60. Their `options.dependencies`
(keyed on `endpoint`) are preserved on the inner fields. This is purely a visual grouping — **no
`oneOf`/`allOf`** (conditional logic stays `options.dependencies`).

> The exact "collapsed by default" mechanism (`options.collapsed` vs the section's default render) is
> **verified in the Ctrl+D sandbox during implementation** — do not assume the key name. If a reliable
> collapse isn't available, the section header alone (clearly labelled "Optional settings") is the
> fallback.

> Config-model note: nesting in the schema does **not** change the flat `parameters` shape the model
> reads — Keboola flattens section objects. If the platform nests `optional_settings.*`, the model adds
> an alias/`model_validator(mode="before")` to hoist them. Confirmed during implementation against the
> Ctrl+D sandbox before committing.

### 3.3 `initial_since` copy

Retitle to **"Fetch Since"**; `description`: "Lower bound for the first run." `tooltip`: accepts ISO-8601
(`2024-01-01T00:00:00Z`) **or** a relative phrase (`5 days ago`, `yesterday`, `last monday`); empty =
full history on the first run.

## 4. Fan-out semantics (`run()`)

`run()` stops resolving a single target and instead resolves a **target list** from the endpoint's
`Target` kind crossed with the row's (possibly empty) multi-select values:

| `Target` | Endpoints | Selected → targets | Empty → resolves to |
|---|---|---|---|
| `NONE` | user, user_repos, user_orgs | n/a (single call) | n/a |
| `ORG` | organizations, organization_* | each selected org | all orgs via `GET /user/orgs` |
| `OWNER_REPO` | repository, repository_* | each selected `owner/repo` | all repos in each selected org (`GET /orgs/{org}/repos`), or all user repos (`GET /user/repos`) if no org selected |
| `ORG_TEAM` | team_members | each selected `org/team_slug` | all teams in each selected org (`GET /orgs/{org}/teams`) |
| `OWNER_REPO_PULL` | repository_pull_reviews | each selected `owner/repo` + the single `pull_number` | **not fanned on repos blindly** — see §7.1 |

Enumeration reuses the exact calls the sync actions already make (`list_organizations`,
`list_repositories`, `list_teams` in `component.py:289-307`), refactored so both the sync action and
`run()` call one shared helper.

### 4.1 Orchestration shape

```
endpoint = self._endpoint()
targets  = self._resolve_targets(endpoint)        # list[Target context dicts]; [{}] for NONE
log "Fanning out over N target(s) for '<endpoint>'."
all_rows = []
for t in targets:
    path, context = self._build_path(endpoint, t)
    since   = self._since(endpoint, t) if incremental else None   # per-target watermark
    params, stop = self._fetch_params(endpoint, since)
    try:
        all_rows += self._collect_rows(endpoint, path, context, params, stop)
    except <single-target not-found/no-access>:   # 404 / 403 on this target only
        log WARNING "Skipping <target>: not found or no access."; continue
    record t as succeeded (for watermark advance)
self._write_table(endpoint, all_rows, incremental)   # ONE table, upsert or full-replace
if incremental: persist per-target watermarks for the succeeded targets
```

- One output table per endpoint, all targets merged, disambiguated by context columns (§6).
- Token-auth and rate-limit errors still **abort** the row (they are not per-target conditions).
- A single inaccessible target (deleted repo, private team) is logged and skipped so an `empty = all`
  run is not derailed by one bad target.

## 5. Incremental state (per-target watermark)

State shape changes from a single mark to a per-target map:

```json
{ "targets": { "keboola/component-github": "2026-06-01T10:00:00+00:00", "keboola/other": "2026-05-20T…" } }
```

- Target key: `owner/repo` (OWNER_REPO), `org` (ORG), `org/team_slug` (ORG_TEAM).
- `_since(endpoint, target)` returns `state["targets"][key]` → else the row's parsed `initial_since`
  (§8) → else `None` (full history).
- After a successful fetch of a target, its watermark is set to this run's start time; **only succeeded
  targets advance**, so a skipped/failed target retries its range next run.
- **Legacy migration:** if the old `{"last_run": "…"}` shape is present and `targets` is absent, seed
  every resolved target with that legacy value on first read — no re-fetch of history for existing rows.

## 6. Output

One table per endpoint (unchanged). Multi-target rows are disambiguated by the registry's context
columns (`owner`, `repo`, `org`, `team_slug`). Implementation must **ensure every fanned-out endpoint
declares the context columns it needs** — audit the registry so each `OWNER_REPO` endpoint emits
`owner`+`repo` (some only emit `("owner","repo")`, some add `"repository"`; standardize). Primary keys
that are not globally unique already include `repository` (e.g. commits `["repository","sha"]`); verify
the rest during implementation so multi-target upsert can't collide.

## 7. Edge cases

### 7.1 `repository_pull_reviews`
Needs a specific `pull_number`; "all repos × one PR number" is meaningless (PR #5 differs per repo).
Decision: this endpoint requires **exactly one** selected repository plus `pull_number`; if multiple
repos (or empty = all) are given, raise a clear `UserException`. Documented in its tooltip. (A future
"all reviews for all open PRs" mode is out of scope.)

### 7.2 Cost of `empty = all`
`empty org + empty repo` on a repo endpoint enumerates every accessible repo → potentially hundreds of
calls. No artificial cap, but `run()` logs the resolved target count up front
("Fanning out over N target(s)…") so the scale is visible in the job log.

### 7.3 Removed targets
If a target is removed from the row, its stale per-target watermark key is harmless (ignored). No
pruning needed; state stays small relative to API volume.

## 8. Config model (`configuration.py`)

- `organization: list[str]`, `repository: list[str]`, `team_slug: list[str]` (default `[]`).
- **Back-compat coercion** (`model_validator(mode="before")`): a bare string (old rows + the 33 rows
  already created in project 5922) becomes a one-element list; `""`/absent becomes `[]`. So no existing
  row breaks.
- `initial_since` validation (`_validate_initial_since`) extends to accept relative phrases: parse with
  `dateparser.parse(value, settings={"RETURN_AS_TIMEZONE_AWARE": True})`; on `None` raise the existing
  clean `UserException`. A `parsed_initial_since` computed/helper returns the absolute ISO instant fed to
  GitHub and to the watermark seed (so downstream only ever sees ISO).
- `_validate_endpoint` unchanged. The runtime target-presence check in `_require_targets` is relaxed:
  empty is allowed (means "all"); only `repository_pull_reviews` enforces single-repo + `pull_number`.

## 9. Dependencies

Add `dateparser` to `pyproject.toml` (runtime dep). It is pure-Python and well-maintained; it parses
both relative phrases and absolute formats, so it replaces the bespoke `datetime.fromisoformat` path for
`initial_since` (the internal `last_run` watermark stays strict ISO).

## 10. Backward compatibility

- Existing rows (single-string targets) and the 33 scaffolded rows keep working via §8 coercion.
- Existing incremental state (`last_run`) is migrated per §5.
- Output table names, schemas, and PKs are unchanged — already-loaded Storage tables continue to upsert.

## 11. Testing

- **datadir**: update fixtures for list-typed targets; add a fan-out case (2 repos → merged table with
  distinct `owner/repo` context), an `empty = all` case (mock enumeration), and the back-compat case
  (string target coerces to list).
- **Config model unit tests**: string→list coercion; relative-date parsing (`"5 days ago"` → instant,
  `"not a date"` → `UserException`); `repository_pull_reviews` multi-repo rejection.
- **State**: per-target read/write; legacy `last_run` migration seeds all targets.
- **VCR**: re-record one incremental fan-out endpoint (e.g. `repository_commits` over 2 repos) to lock
  per-target `since` behaviour.

## 12. Out of scope

- Auto-discovering PR numbers for `repository_pull_reviews`.
- A global org/repo scope in root config.
- Any change to authentication, the endpoint catalog, or output column definitions beyond the §6
  context-column audit.
