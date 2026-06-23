# GitHub Extractor

Extracts data from the [GitHub REST API](https://docs.github.com/en/rest) (version `2022-11-28`) into Keboola Storage for analytics. The component is **row-based**: each configuration row defines a single extraction — one GitHub endpoint applied to one target (the authenticated user, an organization, or an `owner/repo`) — and produces one output table. Adding more data is a matter of adding rows, not reconfiguring a single shared run.

## Authentication

Two methods are supported:

- **Personal Access Token (PAT)** — paste a classic (`ghp_…`) or fine-grained (`github_pat_…`) token. Required scopes: `read:org`, `repo`. SAML-SSO organizations require the token to be SSO-authorized.
- **OAuth** — authorize the component through Keboola's OAuth broker (no token handling required).

## Supported endpoints

Around 33 endpoints across three target types:

- **Authenticated user** — user profile, the user's repositories, the user's organizations.
- **Organizations** — organization details, members, teams, team members, repositories, events, outside collaborators.
- **Repositories** — repository details, branches, tags, commits, contributors, collaborators, languages, topics, releases, issues, issue comments, pull requests, pull request reviews and review comments, labels, milestones, stargazers, subscribers, forks, workflows, workflow runs, deployments, and webhooks.

## Incremental loads

Endpoints that expose a server-side cursor (`since` or sort-by-`updated`) — issues, commits, issue comments, pull requests, pull review comments, workflow runs, and user/org repositories — support incremental loading. The component stores a watermark per row and fetches only records changed since the last successful run. An optional **Initial Since** lower bound seeds the first run. Endpoints without an incremental signal upsert on their primary key so tables never grow unbounded.

## Configuration UI

Organization, repository, and team fields are populated by sync-action dropdowns (no free-text typos), and the row form shows only the fields the chosen endpoint actually needs.

## Output

Each row writes one table to the component's default bucket (`in.c-keboola-ex-github-v2-<configId>`) with native data types, a meaningful primary key, and headerless CSV. Pull requests are extracted separately from issues, so the issues table contains only true issues.

## Rate limits

The component respects GitHub's authenticated rate limit (5,000 requests/hour, shared per token). When a limit is hit it waits and retries within a bounded budget; if the budget is exceeded it fails with an actionable message rather than crashing, and a re-run resumes from the last successful watermark.

For full documentation see the [component repository](https://github.com/keboola/component-github).
