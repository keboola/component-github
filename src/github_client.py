"""
GitHub API Client for interacting with GitHub REST API v2022-11-28
"""

import logging
from typing import Dict, List, Optional, Any
import requests
from keboola.component.exceptions import UserException


class GitHubClient:
    """
    Client for interacting with GitHub REST API
    """

    API_VERSION = "2022-11-28"
    BASE_URL = "https://api.github.com"

    def __init__(self, access_token: str):
        """
        Initialize GitHub client

        Args:
            access_token: GitHub personal access token
        """
        self.access_token = access_token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.API_VERSION,
            }
        )

    def _make_request(
        self, method: str, endpoint: str, params: Optional[Dict] = None, data: Optional[Dict] = None
    ) -> Any:
        """
        Make HTTP request to GitHub API with error handling

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (without base URL)
            params: Query parameters
            data: Request body data

        Returns:
            Response data

        Raises:
            UserException: On API errors
        """
        url = f"{self.BASE_URL}{endpoint}"

        try:
            response = self.session.request(method=method, url=url, params=params, json=data, timeout=60)

            # Handle rate limiting
            if response.status_code == 403 and "rate limit" in response.text.lower():
                raise UserException(
                    "GitHub API rate limit exceeded. Please try again later or use a token with higher limits."
                )

            # Handle authentication errors
            if response.status_code == 401:
                raise UserException("GitHub authentication failed. Please check your access token.")

            # Handle not found
            if response.status_code == 404:
                raise UserException(f"GitHub resource not found: {endpoint}. Please check your configuration.")

            # Raise for other errors
            response.raise_for_status()

            # Some endpoints return 204 No Content
            if response.status_code == 204:
                return None

            return response.json()

        except requests.exceptions.Timeout:
            raise UserException(f"Request to GitHub API timed out: {endpoint}")
        except requests.exceptions.ConnectionError:
            raise UserException(f"Connection error to GitHub API: {endpoint}")
        except requests.exceptions.RequestException as e:
            if not isinstance(e, UserException):
                raise UserException(f"GitHub API request failed: {str(e)}")
            raise

    def _paginate(self, endpoint: str, params: Optional[Dict] = None, max_pages: int = 100) -> List[Dict]:
        """
        Paginate through GitHub API results

        Args:
            endpoint: API endpoint
            params: Query parameters
            max_pages: Maximum number of pages to fetch

        Returns:
            List of all results
        """
        all_results = []
        page = 1
        per_page = 100  # GitHub's max per page

        if params is None:
            params = {}

        params["per_page"] = per_page

        while page <= max_pages:
            params["page"] = page
            results = self._make_request("GET", endpoint, params=params)

            if not results:
                break

            all_results.extend(results)

            # If we got fewer results than per_page, we've reached the end
            if len(results) < per_page:
                break

            page += 1
            logging.info(f"Fetched page {page} from {endpoint}")

        return all_results

    def test_connection(self) -> Dict:
        """
        Test the GitHub API connection

        Returns:
            User information if successful

        Raises:
            UserException: If connection fails
        """
        return self._make_request("GET", "/user")

    def get_user_organizations(self) -> List[Dict]:
        """
        Get organizations for the authenticated user

        Returns:
            List of organizations
        """
        logging.info("Fetching user organizations")
        return self._paginate("/user/orgs")

    def get_organization_members(self, org_name: str) -> List[Dict]:
        """
        Get members of an organization

        Args:
            org_name: Organization name

        Returns:
            List of organization members
        """
        logging.info(f"Fetching members for organization: {org_name}")
        return self._paginate(f"/orgs/{org_name}/members")

    def get_organization_teams(self, org_name: str) -> List[Dict]:
        """
        Get teams in an organization

        Args:
            org_name: Organization name

        Returns:
            List of teams
        """
        logging.info(f"Fetching teams for organization: {org_name}")
        return self._paginate(f"/orgs/{org_name}/teams")

    def get_organization_repositories(self, org_name: str) -> List[Dict]:
        """
        Get repositories in an organization

        Args:
            org_name: Organization name

        Returns:
            List of repositories
        """
        logging.info(f"Fetching repositories for organization: {org_name}")
        return self._paginate(f"/orgs/{org_name}/repos")

    def get_repository_issues(self, owner: str, repo: str, state: str = "all") -> List[Dict]:
        """
        Get issues for a repository

        Args:
            owner: Repository owner (user or organization)
            repo: Repository name
            state: Issue state (open, closed, all)

        Returns:
            List of issues
        """
        logging.info(f"Fetching issues for repository: {owner}/{repo}")
        params = {"state": state}
        return self._paginate(f"/repos/{owner}/{repo}/issues", params=params)

    def get_repository_commits(self, owner: str, repo: str, since: Optional[str] = None) -> List[Dict]:
        """
        Get commits for a repository

        Args:
            owner: Repository owner (user or organization)
            repo: Repository name
            since: Only commits after this date (ISO 8601 format)

        Returns:
            List of commits
        """
        logging.info(f"Fetching commits for repository: {owner}/{repo}")
        params = {}
        if since:
            params["since"] = since
        return self._paginate(f"/repos/{owner}/{repo}/commits", params=params)
