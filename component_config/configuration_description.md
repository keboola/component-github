### Authentication

Choose how the extractor authenticates to GitHub:

- **Personal Access Token (PAT)** — paste a classic (`ghp_…`) or fine-grained (`github_pat_…`)
  token into the **GitHub Personal Access Token** field. Required scopes: `read:org`, `repo`.
  If your organization enforces SAML SSO, SSO-authorize the token first. Use **Test Connection**
  to verify it before saving.
- **OAuth** — authorize the component with your GitHub account; Keboola stores and refreshes the
  token for you. No token field is shown in this mode. OAuth works on **github.com only**.

### GitHub Enterprise

Leave **GitHub API Base URL** empty to use github.com. Set it to your Enterprise REST API root to
extract from a GitHub Enterprise host:

- **Enterprise Cloud with data residency** (`SUBDOMAIN.ghe.com`) — `https://api.SUBDOMAIN.ghe.com`
- **Enterprise Server** (self-hosted) — `https://HOSTNAME/api/v3`

Enterprise hosts need a **Personal Access Token created on that host**. Keboola's OAuth app is
registered on github.com, so the OAuth method cannot authenticate to an Enterprise host. If your
Enterprise tenant restricts access by IP address, allow your Keboola stack's outbound IP addresses.

### Endpoint selection (Data Source)

Each configuration row extracts one GitHub data source into one table. Pick the source from the
**Data Source** dropdown (e.g. *Repository Issues*, *Organization Members*, *Repository Commits*).
The output table is named after the selected source.

### Target (organization / repository / team)

Depending on the chosen source, the row asks for a target:

- **Organization** sources need an **Organization** (loaded via the Load button).
- **Repository** sources need a **Repository** in `owner/repo` form (loaded via the Load button).
- **Team Members** needs both an organization and a **Team**.
- **Pull Request Reviews** also needs a **Pull Request Number**.
- A few sources (e.g. *Authenticated User*, *User Repositories*) need no target.

### Load type & Initial Since

For sources that support incremental fetching (issues, commits, issue comments, PR review
comments, pulls, workflow runs, user/org repositories):

- **Load Type** — *Incremental Load* upserts new/changed records on each run and stores a
  `last_run` watermark; *Full Load* replaces the whole table every run.
- **Initial Since (ISO-8601)** — an optional lower bound for the very first run when no watermark
  exists yet, e.g. `2024-01-01T00:00:00Z`. Leave empty to fetch full history on the first run.

Sources without an incremental signal (branches, tags, members, languages, releases, …) always
do a full replace so records deleted on GitHub are removed from the table.

### GraphQL Query (advanced)

Select **GraphQL Query** as the Data Source to run a GitHub GraphQL query and load its result as one
table. Every column is loaded as text; nested objects become `parent_child` columns and nested lists
become a JSON-encoded cell.

- **Query Template** — pick a built-in query or **Custom**.
  - **Org repos: AI config files** — for every repository in an **Organization**, reports whether each
    **File name** (default `AGENTS.md`, `CLAUDE.md`) exists and its content. One row per repository.
  - **Repository: Open PRs with review decision** — open pull requests of one **Organization** /
    **Repository**, including the GraphQL-only `reviewDecision`.
  - **Org: Pull Requests + reviews + comments** — every pull request in an **Organization** (within the
    **Since** window) with its reviews and review-thread comments, in a few paginated queries instead of
    per-PR REST calls. Replaces the `repository_pulls` / `_pull_reviews` / `_pull_review_comments` REST sources.
  - **Org: Repository languages** — language breakdown for every repository in an **Organization** (one
    paginated query). Replaces the `repository_languages` REST source.
  - **Org: Recent commits** — recent commit history per repository in an **Organization** (within the
    **Since** window). Replaces the `repository_commits` REST source. Note: up to 100 commits per repo per page.
- **Custom** — paste a **GraphQL Query** and optional **GraphQL Variables** (a JSON object) and set an
  **Output table** name. To page through large results, select `pageInfo { hasNextPage endCursor }` on the
  connection and declare a `$cursor` variable used as `after: $cursor`.
- **Row path (advanced)** — dotted path under `data` to the list that becomes your rows (e.g.
  `organization.repositories.nodes`). Leave empty to auto-detect; set it only when the query returns more
  than one list.
- **Primary key** — comma-separated result column name(s) for incremental upsert (empty = full load).
- **Rolling window** — set **Since** / **Until** (relative phrases like `60 days ago` work) and reference
  `{{since}}` / `{{until}}` in your query or variables; the component substitutes the resolved date
  (`YYYY-MM-DD`) at runtime. Example variable: `{"q": "org:acme is:pr updated:>={{since}}"}`.
