"""
GitHub Extractor Component main class.
"""

import csv
import logging
from typing import List, Dict

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException
from keboola.component.dao import ColumnDefinition, BaseType
from keboola.component.sync_actions import SelectElement

from configuration import Configuration
from github_client import GitHubClient


class Component(ComponentBase):
    """
    GitHub Extractor Component

    Extracts data from GitHub REST API including:
    - Organizations
    - Organization Members
    - Organization Teams
    - Organization Repositories
    - Repository Issues
    - Repository Commits
    """

    def __init__(self):
        super().__init__()
        self.params = None
        self.github_client = None

    def run(self):
        """
        Main execution code
        """
        # Validate and load configuration
        self.params = Configuration(**self.configuration.parameters)

        # Initialize GitHub client
        self.github_client = GitHubClient(self.params.github_token)

        logging.info(f"Starting GitHub data extraction for endpoints: {', '.join(self.params.endpoints)}")

        # Process each selected endpoint
        for endpoint in self.params.endpoints:
            try:
                self._process_endpoint(endpoint)
            except Exception as e:
                logging.error(f"Error processing endpoint '{endpoint}': {str(e)}")
                raise UserException(f"Failed to process endpoint '{endpoint}': {str(e)}")

        logging.info("GitHub data extraction completed successfully")

    def _process_endpoint(self, endpoint: str):
        """Process a single endpoint"""
        logging.info(f"Processing endpoint: {endpoint}")

        if endpoint == "organizations":
            self._extract_organizations()
        elif endpoint == "organization_members":
            self._extract_organization_members()
        elif endpoint == "organization_teams":
            self._extract_organization_teams()
        elif endpoint == "organization_repositories":
            self._extract_organization_repositories()
        elif endpoint == "repository_issues":
            self._extract_repository_issues()
        elif endpoint == "repository_commits":
            self._extract_repository_commits()
        else:
            raise UserException(f"Unknown endpoint: {endpoint}")

    def _extract_organizations(self):
        """Extract user organizations"""
        data = self.github_client.get_user_organizations()

        if not data:
            logging.warning("No organizations found")
            return

        # Define schema
        schema = {
            "id": ColumnDefinition(data_types=BaseType.integer(), primary_key=True),
            "login": ColumnDefinition(data_types=BaseType.string()),
            "description": ColumnDefinition(data_types=BaseType.string()),
            "url": ColumnDefinition(data_types=BaseType.string()),
            "node_id": ColumnDefinition(data_types=BaseType.string()),
        }

        table = self.create_out_table_definition(
            name="organizations.csv", destination="out.c-github.organizations", schema=schema, incremental=False
        )

        self._write_data_to_table(table, data, ["id", "login", "description", "url", "node_id"])
        logging.info(f"Extracted {len(data)} organizations")

    def _extract_organization_members(self):
        """Extract organization members"""
        data = self.github_client.get_organization_members(self.params.organization_name)

        if not data:
            logging.warning(f"No members found for organization: {self.params.organization_name}")
            return

        schema = {
            "id": ColumnDefinition(data_types=BaseType.integer(), primary_key=True),
            "login": ColumnDefinition(data_types=BaseType.string()),
            "type": ColumnDefinition(data_types=BaseType.string()),
            "site_admin": ColumnDefinition(data_types=BaseType.string()),
            "url": ColumnDefinition(data_types=BaseType.string()),
            "organization": ColumnDefinition(data_types=BaseType.string()),
        }

        table = self.create_out_table_definition(
            name="organization_members.csv",
            destination="out.c-github.organization_members",
            schema=schema,
            incremental=True,
        )

        # Add organization name to each record
        for record in data:
            record["organization"] = self.params.organization_name

        self._write_data_to_table(table, data, ["id", "login", "type", "site_admin", "url", "organization"])
        logging.info(f"Extracted {len(data)} members from organization: {self.params.organization_name}")

    def _extract_organization_teams(self):
        """Extract organization teams"""
        data = self.github_client.get_organization_teams(self.params.organization_name)

        if not data:
            logging.warning(f"No teams found for organization: {self.params.organization_name}")
            return

        schema = {
            "id": ColumnDefinition(data_types=BaseType.integer(), primary_key=True),
            "name": ColumnDefinition(data_types=BaseType.string()),
            "slug": ColumnDefinition(data_types=BaseType.string()),
            "description": ColumnDefinition(data_types=BaseType.string()),
            "privacy": ColumnDefinition(data_types=BaseType.string()),
            "url": ColumnDefinition(data_types=BaseType.string()),
            "organization": ColumnDefinition(data_types=BaseType.string()),
        }

        table = self.create_out_table_definition(
            name="organization_teams.csv",
            destination="out.c-github.organization_teams",
            schema=schema,
            incremental=True,
        )

        # Add organization name to each record
        for record in data:
            record["organization"] = self.params.organization_name

        self._write_data_to_table(table, data, ["id", "name", "slug", "description", "privacy", "url", "organization"])
        logging.info(f"Extracted {len(data)} teams from organization: {self.params.organization_name}")

    def _extract_organization_repositories(self):
        """Extract organization repositories"""
        data = self.github_client.get_organization_repositories(self.params.organization_name)

        if not data:
            logging.warning(f"No repositories found for organization: {self.params.organization_name}")
            return

        schema = {
            "id": ColumnDefinition(data_types=BaseType.integer(), primary_key=True),
            "name": ColumnDefinition(data_types=BaseType.string()),
            "full_name": ColumnDefinition(data_types=BaseType.string()),
            "description": ColumnDefinition(data_types=BaseType.string()),
            "private": ColumnDefinition(data_types=BaseType.string()),
            "fork": ColumnDefinition(data_types=BaseType.string()),
            "created_at": ColumnDefinition(data_types=BaseType.timestamp()),
            "updated_at": ColumnDefinition(data_types=BaseType.timestamp()),
            "pushed_at": ColumnDefinition(data_types=BaseType.timestamp()),
            "size": ColumnDefinition(data_types=BaseType.integer()),
            "stargazers_count": ColumnDefinition(data_types=BaseType.integer()),
            "watchers_count": ColumnDefinition(data_types=BaseType.integer()),
            "language": ColumnDefinition(data_types=BaseType.string()),
            "forks_count": ColumnDefinition(data_types=BaseType.integer()),
            "open_issues_count": ColumnDefinition(data_types=BaseType.integer()),
            "default_branch": ColumnDefinition(data_types=BaseType.string()),
            "url": ColumnDefinition(data_types=BaseType.string()),
            "organization": ColumnDefinition(data_types=BaseType.string()),
        }

        table = self.create_out_table_definition(
            name="organization_repositories.csv",
            destination="out.c-github.organization_repositories",
            schema=schema,
            incremental=True,
        )

        # Add organization name to each record
        for record in data:
            record["organization"] = self.params.organization_name

        columns = [
            "id",
            "name",
            "full_name",
            "description",
            "private",
            "fork",
            "created_at",
            "updated_at",
            "pushed_at",
            "size",
            "stargazers_count",
            "watchers_count",
            "language",
            "forks_count",
            "open_issues_count",
            "default_branch",
            "url",
            "organization",
        ]

        self._write_data_to_table(table, data, columns)
        logging.info(f"Extracted {len(data)} repositories from organization: {self.params.organization_name}")

    def _extract_repository_issues(self):
        """Extract repository issues"""
        data = self.github_client.get_repository_issues(
            self.params.repository_owner, self.params.repository_name, self.params.issue_state
        )

        if not data:
            logging.warning(
                f"No issues found for repository: {self.params.repository_owner}/{self.params.repository_name}"
            )
            return

        schema = {
            "id": ColumnDefinition(data_types=BaseType.integer(), primary_key=True),
            "number": ColumnDefinition(data_types=BaseType.integer()),
            "title": ColumnDefinition(data_types=BaseType.string()),
            "state": ColumnDefinition(data_types=BaseType.string()),
            "created_at": ColumnDefinition(data_types=BaseType.timestamp()),
            "updated_at": ColumnDefinition(data_types=BaseType.timestamp()),
            "closed_at": ColumnDefinition(data_types=BaseType.timestamp()),
            "user_login": ColumnDefinition(data_types=BaseType.string()),
            "labels": ColumnDefinition(data_types=BaseType.string()),
            "comments": ColumnDefinition(data_types=BaseType.integer()),
            "url": ColumnDefinition(data_types=BaseType.string()),
            "repository_owner": ColumnDefinition(data_types=BaseType.string()),
            "repository_name": ColumnDefinition(data_types=BaseType.string()),
        }

        table = self.create_out_table_definition(
            name="repository_issues.csv", destination="out.c-github.repository_issues", schema=schema, incremental=True
        )

        # Transform data
        for record in data:
            record["user_login"] = record.get("user", {}).get("login", "") if record.get("user") else ""
            record["labels"] = ",".join([label.get("name", "") for label in record.get("labels", [])])
            record["repository_owner"] = self.params.repository_owner
            record["repository_name"] = self.params.repository_name

        columns = [
            "id",
            "number",
            "title",
            "state",
            "created_at",
            "updated_at",
            "closed_at",
            "user_login",
            "labels",
            "comments",
            "url",
            "repository_owner",
            "repository_name",
        ]

        self._write_data_to_table(table, data, columns)
        logging.info(
            f"Extracted {len(data)} issues from"
            f"repository: {self.params.repository_owner}/{self.params.repository_name}"
        )

    def _extract_repository_commits(self):
        """Extract repository commits"""
        data = self.github_client.get_repository_commits(
            self.params.repository_owner, self.params.repository_name, self.params.commits_since
        )

        if not data:
            logging.warning(
                f"No commits found for repository: {self.params.repository_owner}/{self.params.repository_name}"
            )
            return

        schema = {
            "sha": ColumnDefinition(data_types=BaseType.string(), primary_key=True),
            "author_name": ColumnDefinition(data_types=BaseType.string()),
            "author_email": ColumnDefinition(data_types=BaseType.string()),
            "author_date": ColumnDefinition(data_types=BaseType.timestamp()),
            "committer_name": ColumnDefinition(data_types=BaseType.string()),
            "committer_email": ColumnDefinition(data_types=BaseType.string()),
            "committer_date": ColumnDefinition(data_types=BaseType.timestamp()),
            "message": ColumnDefinition(data_types=BaseType.string()),
            "url": ColumnDefinition(data_types=BaseType.string()),
            "repository_owner": ColumnDefinition(data_types=BaseType.string()),
            "repository_name": ColumnDefinition(data_types=BaseType.string()),
        }

        table = self.create_out_table_definition(
            name="repository_commits.csv",
            destination="out.c-github.repository_commits",
            schema=schema,
            incremental=True,
        )

        # Transform data
        for record in data:
            commit = record.get("commit", {})
            author = commit.get("author", {})
            committer = commit.get("committer", {})

            record["author_name"] = author.get("name", "")
            record["author_email"] = author.get("email", "")
            record["author_date"] = author.get("date", "")
            record["committer_name"] = committer.get("name", "")
            record["committer_email"] = committer.get("email", "")
            record["committer_date"] = committer.get("date", "")
            record["message"] = commit.get("message", "")
            record["repository_owner"] = self.params.repository_owner
            record["repository_name"] = self.params.repository_name

        columns = [
            "sha",
            "author_name",
            "author_email",
            "author_date",
            "committer_name",
            "committer_email",
            "committer_date",
            "message",
            "url",
            "repository_owner",
            "repository_name",
        ]

        self._write_data_to_table(table, data, columns)
        logging.info(
            f"Extracted {len(data)} commits from"
            f"repository: {self.params.repository_owner}/{self.params.repository_name}"
        )

    def _write_data_to_table(self, table, data: List[Dict], columns: List[str]):
        """Write data to output table"""
        with open(table.full_path, mode="w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()

            for row in data:
                # Convert boolean values to strings
                for key, value in row.items():
                    if isinstance(value, bool):
                        row[key] = str(value)
                    elif value is None:
                        row[key] = ""

                writer.writerow(row)

        self.write_manifest(table)

    @sync_action("testConnection")
    def test_connection(self):
        """Test GitHub API connection"""
        logging.info("Testing GitHub connection")

        params = Configuration(**self.configuration.parameters)
        client = GitHubClient(params.github_token)

        try:
            user_info = client.test_connection()
            logging.info(f"Connection successful. Authenticated as: {user_info.get('login', 'Unknown')}")
        except Exception as e:
            raise UserException(f"Connection test failed: {str(e)}")

    @sync_action("listOrganizations")
    def list_organizations(self):
        """List available organizations for dropdown"""
        logging.info("Fetching organizations for dropdown")

        params = Configuration(**self.configuration.parameters)
        client = GitHubClient(params.github_token)

        try:
            orgs = client.get_user_organizations()
            return [SelectElement(value=org["login"], label=org["login"]) for org in orgs]
        except Exception as e:
            raise UserException(f"Failed to fetch organizations: {str(e)}")


"""
        Main entrypoint
"""
if __name__ == "__main__":
    try:
        comp = Component()
        # this triggers the run method by default and is controlled by the configuration.action parameter
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
