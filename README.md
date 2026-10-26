ex-github-v2
=============

A row-based Keboola extractor for the [GitHub REST API](https://docs.github.com/en/rest)
(`https://api.github.com`, version `2022-11-28`). Each configuration **row** defines one
extraction — a single GitHub endpoint applied to a single target (the authenticated user, an
organization, or an `owner/repo`) — and produces one output table. The component is a thin
orchestrator over a declarative endpoint registry, so 33 endpoints share one code path.

**Table of Contents:**

[TOC]

Features
========

| **Feature**             | **Description**                                              |
|-------------------------|-------------------------------------------------------------|
| Row-Based Configuration | One row = one endpoint + target; its own state & schedule.  |
| PAT & OAuth auth        | Personal Access Token or broker OAuth — one HTTP path.      |
| Incremental Loading     | `since` cursor or sort-by-`updated` with per-row watermark. |
| Native data types       | Typed output (`INTEGER`/`TIMESTAMP`/`BOOLEAN`/`STRING`).     |
| Rate-limit handling     | Bounded wait-and-retry on the 5000 req/hr core budget.      |
| Sync-action dropdowns   | Org / repository / team pickers; connection test.           |
| GitHub Enterprise       | Optional `api_base_url` targets a GHE.com / GHES host.      |

Authentication
==============

The root config field `auth_type` chooses the credential source. Both reach GitHub as
`Authorization: Bearer <token>`.

Personal Access Token (PAT)
---------------------------

Set `auth_type = "pat"` and paste a token into the encrypted `#github_token` field. Mint a
classic (`ghp_…`) or fine-grained (`github_pat_…`) PAT in the GitHub UI with at least the
`read:org` and `repo` scopes (`repo` covers private-repo reads; `read:org` covers
org/team/member data). For SAML-SSO organizations the PAT must be **SSO-authorized**, or
org/repo endpoints return 403/404.

GitHub Enterprise
-----------------

The optional root field `api_base_url` points the component at a GitHub Enterprise host. Leave
it empty (the default) for github.com — every existing configuration keeps that behaviour.

| Flavour                                 | `api_base_url`                  | GraphQL endpoint used          |
| --------------------------------------- | ------------------------------- | ------------------------------ |
| github.com (default)                    | *(empty)*                       | `https://api.github.com/graphql` |
| Enterprise Cloud with data residency    | `https://api.SUBDOMAIN.ghe.com` | `<base>/graphql`                 |
| Enterprise Server (self-hosted)         | `https://HOSTNAME/api/v3`       | `https://HOSTNAME/api/graphql`   |

Enterprise Server is the reason GraphQL is derived rather than appended: its REST root is
`/api/v3` but its GraphQL endpoint is `/api/graphql` (see `GitHubClient._derive_graphql_url`).

Enterprise hosts require **`auth_type = "pat"`** with a token created on that host. Keboola's
OAuth broker holds one consumer per component id, registered against github.com, so OAuth can
only ever return a github.com token; the config model rejects `oauth` + `api_base_url` with that
reason instead of letting it fail later as an opaque 401. If the Enterprise tenant restricts
access by IP address, the Keboola stack's outbound IPs must be allowed.

OAuth
-----

Set `auth_type = "oauth"`. The Keboola OAuth broker injects
`authorization.oauth_api.credentials` at runtime; the component parses the provider's raw
token response from `#data` (`json.loads(#data)["access_token"]`). The recommended broker
registration is a **standard OAuth App with token expiration disabled** (non-expiring `gho_…`
token, no refresh logic). Read-only scopes: `read:org`, `repo`, `read:user`, `user:email`.

### Local / dev OAuth testing — the credentials-injection bypass

Production OAuth requires Keboola support to register the GitHub OAuth App in the broker (not
self-serve). Until that lands, the OAuth code path is exercised **without the live broker** by
injecting the credentials directly via the Storage configuration API. The `#`-prefixed fields
encrypt/decrypt normally, so the runtime resolver runs exactly as it would in production:

```json
{
  "authorization": {
    "oauth_api": {
      "credentials": {
        "appKey": "<client_id>",
        "#appSecret": "<client_secret>",
        "#data": "{\"access_token\":\"gho_xxx\",\"token_type\":\"bearer\",\"scope\":\"repo,read:org\"}"
      }
    }
  }
}
```

The component only ever **reads** `#data` and pulls `access_token` out of it — it never drives
the OAuth dance itself.

Supported Endpoints
===================

33 endpoints across three target groups, each producing one table named after the registry
key (e.g. `repository_issues`, `organization_members`, `repository_languages`):

- **Authenticated user** — `user`, `user_repos`, `user_orgs`.
- **Organizations** — `organizations`, `organization_members`, `organization_teams`,
  `team_members`, `organization_repositories`, `organization_events`,
  `organization_outside_collaborators`.
- **Repositories** — `repository`, `repository_branches`, `repository_tags`,
  `repository_commits`, `repository_contributors`, `repository_collaborators`,
  `repository_languages`, `repository_topics`, `repository_releases`, `repository_issues`,
  `repository_issue_comments`, `repository_pulls`, `repository_pull_reviews`,
  `repository_pull_review_comments`, `repository_labels`, `repository_milestones`,
  `repository_stargazers`, `repository_subscribers`, `repository_forks`,
  `repository_workflows`, `repository_workflow_runs`, `repository_deployments`,
  `repository_webhooks`.

If you need additional endpoints, please submit your request to
[ideas.keboola.com](https://ideas.keboola.com/).

GraphQL Query (advanced)
------------------------

In addition to the REST endpoints above, you can write custom GraphQL queries to fetch any GitHub data
not covered by the preset endpoints. Select **GraphQL Query** as the Data Source and choose a
**Query Template** or write a **Custom** query. Output is one flattened all-STRING table; nested
objects become `parent_child` columns and nested lists become JSON cells.

- **Query Templates** — built-in queries you can pick instead of writing GraphQL:
  - **Org repos: AI config files** — scans every repository in an organization for `AGENTS.md` and
    `CLAUDE.md` files, reporting whether they exist and their content. One row per repository.
  - **Repository: Open PRs with review decision** — fetches open pull requests of a repository,
    including the GraphQL-only `reviewDecision` field.
  - **Org: Pull Requests + reviews + comments** — all PRs in an organization (within the **Since**
    window) with reviews and review-thread comments — a budget-friendly replacement for the
    `repository_pulls` / `_pull_reviews` / `_pull_review_comments` REST data sources.
  - **Org: Repository languages** — language breakdown per repo in an organization (replaces
    `repository_languages`).
  - **Org: Recent commits** — recent commit history per repo in an organization within the **Since**
    window (replaces `repository_commits`; up to 100 commits per repo per page).
- **Custom** — paste a GraphQL query (and optional JSON variables), name an output table, and optionally
  set **Row path** (dotted path to the data list, e.g. `organization.repositories.nodes`) if
  auto-detection fails. For pagination, select `pageInfo { hasNextPage endCursor }` on the connection
  and use a `$cursor` variable with `after: $cursor`.
- **Primary key** — comma-separated column(s) for incremental upsert (empty = full load each run).
- **Rolling window** — set **Since**/**Until** (relative phrases like `60 days ago` are supported) and write `{{since}}`/`{{until}}` in the query or variables; the component substitutes the resolved date (`YYYY-MM-DD`) at runtime, so a window like `updated:>={{since}}` rolls automatically each run.
- **Null nested objects** — when a probed file is absent, the API returns `null` for that object; the flattener collapses it to a single empty column named after the file (e.g. `agents_md`) rather than emitting the full set of `agents_md_*` sub-columns.

Configuration
=============

Row fields appear conditionally based on the chosen `endpoint`:

- **endpoint** — the data source (selects the registry entry).
- **organization** / **repository** / **team_slug** — the target (async dropdowns populated by
  sync actions). `repository` is `owner/repo`.
- **pull_number** — required only for `repository_pull_reviews`.
- **load_type** — `full_load` or `incremental_load` (default incremental).
- **initial_since** — first-run lower bound (ISO-8601, e.g. `2024-01-01T00:00:00Z`) for
  cursor-based endpoints.
- **issue_state** / **pull_state** / **milestone_state** — `all` / `open` / `closed` filters.

Incremental loading
--------------------

State stores a single `last_run` (ISO-8601 UTC) captured before the fetch and written only on
success. Endpoints with a server-side `since` filter (issues, commits, issue comments, PR
review comments) send it directly; `pulls` / `*_repos` sort by `updated` descending and stop
paging once a record predates the watermark. First run uses `initial_since` if set, otherwise
fetches full history. Incremental output upserts on the table's primary key.

The `repository_issues` caveat
------------------------------

GitHub's `/issues` endpoint returns pull requests mixed in (every PR is also an issue). The
`repository_issues` extraction **drops** rows carrying a `pull_request` object, so the issues
table contains only true issues. Pull requests are covered by the dedicated `repository_pulls`
extraction.

Output
======

One table per row, written headerless with an authoritative `schema` manifest (native types).
Default bucket is on, so tables land in `in.c-keboola-ex-github-v2-<configId>`; do not set a
hardcoded destination (the default bucket silently overrides it).

Development
-----------

Run locally with [uv](https://docs.astral.sh/uv/):

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
uv sync
uv run ruff check .
uv run pytest
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
