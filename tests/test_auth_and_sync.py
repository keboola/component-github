"""OAuth credential path + sync-action tests (mocked HTTP / stubbed client)."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest
import responses
from keboola.component.exceptions import UserException


def _datadir(tmp_path: Path, config: dict) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps(config))
    return data_dir


def _component(data_dir: Path, patch_client: bool = True):
    """Build a real Component (real GitHubClient unless patched) against a datadir."""
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        from component import Component

        if patch_client:
            with mock.patch("component.GitHubClient"):
                return Component()
        return Component()


def test_oauth_path_resolves_injected_token_and_runs(tmp_path):
    """auth_type=oauth reads authorization.oauth_api.credentials.#data and runs the same fetch."""
    config = {
        "parameters": {
            "auth_type": "oauth",
            "endpoint": "repository",
            "repository": "octocat/Hello",
            "load_type": "full_load",
        },
        "authorization": {
            "oauth_api": {
                "credentials": {
                    "appKey": "client_id",
                    "#appSecret": "secret",
                    "#data": '{"access_token":"gho_injected","token_type":"bearer"}',
                }
            }
        },
    }
    data_dir = _datadir(tmp_path, config)
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir), "KBC_DATA_TYPE_SUPPORT": "authoritative"}):
        from component import Component

        with mock.patch("component.GitHubClient") as client_cls:
            comp = Component()
            # token resolved from #data was handed to the client
            assert client_cls.call_args.kwargs["token"] == "gho_injected"
        comp.client.fetch.return_value = iter([{"id": 1, "name": "Hello"}])  # ty: ignore[unresolved-attribute]
        comp.run()
    assert (data_dir / "out" / "tables" / "repository.csv").exists()


def test_oauth_missing_credentials_raises(tmp_path):
    config = {
        "parameters": {"auth_type": "oauth", "endpoint": "repository", "repository": "o/r"},
    }
    data_dir = _datadir(tmp_path, config)
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        from component import Component

        with pytest.raises(UserException, match="OAuth"):
            Component()


# --------------------------------------------------------------------- sync actions

_SYNC_CONFIG = {"parameters": {"#github_token": "ghp_x", "auth_type": "pat", "endpoint": ""}}


@responses.activate
def test_test_connection_success(tmp_path):
    responses.add(responses.GET, "https://api.github.com/rate_limit", json={"rate": {"remaining": 5000}}, status=200)
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    comp.test_connection()  # no exception == success


@responses.activate
def test_test_connection_401_raises(tmp_path):
    # The @sync_action wrapper catches UserException and converts it to exit(1); the
    # underlying handler raises UserException, which the wrapper turns into SystemExit.
    responses.add(responses.GET, "https://api.github.com/rate_limit", json={"message": "Bad creds"}, status=401)
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    with pytest.raises(SystemExit):
        comp.test_connection()


@responses.activate
def test_list_organizations_shape(tmp_path):
    responses.add(
        responses.GET,
        "https://api.github.com/user/orgs",
        json=[{"login": "octo-org", "id": 1}, {"login": "acme", "id": 2}],
        status=200,
    )
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    result = comp.list_organizations()
    assert [(e.value, e.label) for e in result] == [("octo-org", "octo-org"), ("acme", "acme")]


@responses.activate
def test_list_repositories_uses_org_when_set(tmp_path):
    responses.add(
        responses.GET,
        "https://api.github.com/orgs/octo-org/repos",
        json=[{"full_name": "octo-org/repo1"}, {"full_name": "octo-org/repo2"}],
        status=200,
    )
    config = {"parameters": {"#github_token": "ghp_x", "endpoint": "", "organization": "octo-org"}}
    comp = _component(_datadir(tmp_path, config), patch_client=False)
    result = comp.list_repositories()
    assert [e.value for e in result] == ["octo-org/repo1", "octo-org/repo2"]


@responses.activate
def test_list_repositories_falls_back_to_user(tmp_path):
    responses.add(
        responses.GET,
        "https://api.github.com/user/repos",
        json=[{"full_name": "octocat/Hello"}],
        status=200,
    )
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    result = comp.list_repositories()
    assert [e.value for e in result] == ["octocat/Hello"]


@responses.activate
def test_list_teams_shape(tmp_path):
    responses.add(
        responses.GET,
        "https://api.github.com/orgs/octo-org/teams",
        json=[{"name": "Core Team", "slug": "core-team"}],
        status=200,
    )
    config = {"parameters": {"#github_token": "ghp_x", "endpoint": "", "organization": "octo-org"}}
    comp = _component(_datadir(tmp_path, config), patch_client=False)
    result = comp.list_teams()
    assert [(e.value, e.label) for e in result] == [("core-team", "Core Team")]


def test_list_teams_requires_org(tmp_path):
    # UserException("Select an organization...") is converted to exit(1) by the wrapper.
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    with pytest.raises(SystemExit):
        comp.list_teams()
