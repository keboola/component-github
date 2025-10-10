# GitHub Extractor

This extractor component allows you to extract data from GitHub using the GitHub REST API (v2022-11-28).

## Features

The component supports extraction from the following GitHub endpoints:

- **Organizations** - List all organizations the authenticated user has access to
- **Organization Members** - Extract members from a specified organization
- **Organization Teams** - Extract teams from a specified organization  
- **Organization Repositories** - Extract repositories from a specified organization
- **Repository Issues** - Extract issues from a specific repository
- **Repository Commits** - Extract commits from a specific repository

## Configuration

### Authentication

You need to provide a GitHub Personal Access Token with appropriate permissions:

1. Go to [GitHub Settings > Developer settings > Personal access tokens](https://github.com/settings/tokens)
2. Generate a new token (classic) or fine-grained personal access token
3. Required scopes:
   - `read:org` - for organization data
   - `repo` - for repository data

### Endpoint Selection

Select which endpoints you want to extract data from. Based on your selection, you may need to provide additional parameters:

- **Organization endpoints** (members, teams, repositories) require an organization name
- **Repository endpoints** (issues, commits) require repository owner and name

### Output

Each endpoint creates a separate output table in the `out.c-github` bucket:

- `organizations` - Organization details
- `organization_members` - Member information per organization
- `organization_teams` - Team information per organization
- `organization_repositories` - Repository details per organization
- `repository_issues` - Issue details per repository
- `repository_commits` - Commit history per repository

All tables are created with appropriate schemas and primary keys for incremental loading.

## Rate Limits

The component respects GitHub API rate limits. For authenticated requests, you typically get:
- 5,000 requests per hour for standard tokens
- Higher limits for GitHub Enterprise

If you hit rate limits, the component will report an error. Consider:
- Using a token with higher limits
- Reducing the amount of data being extracted
- Running the extraction less frequently