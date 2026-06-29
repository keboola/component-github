"""OAuth credential path + sync-action tests (mocked HTTP / stubbed client)."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest
import requests
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


def test_oauth_null_authorization_blocks_raises_userexception_not_attributeerror(tmp_path):
    """Present-but-null authorization/oauth_api must resolve to no credentials (clean exit 1).

    A `.get(...)` on an explicit null would raise AttributeError (uncaught -> exit 2); the
    `or {}` guard at each hop turns it into the same OAuth UserException as the absent case.
    """
    config = {
        "parameters": {"auth_type": "oauth", "endpoint": "repository", "repository": "o/r"},
        "authorization": {"oauth_api": None},
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
def test_list_repositories_uses_gql_org_when_rest_org_empty(tmp_path):
    """listRepositories falls back to gql_org for the GraphQL template picker."""
    responses.add(
        responses.GET,
        "https://api.github.com/orgs/acme/repos",
        json=[{"full_name": "acme/web"}, {"full_name": "acme/api"}],
        status=200,
    )
    config = {
        "parameters": {
            "#github_token": "ghp_x",
            "endpoint": "graphql_query",
            "query_template": "org_pull_request_reviews",
            "gql_org": "acme",
            "gql_repo": "acme/web",
        }
    }
    comp = _component(_datadir(tmp_path, config), patch_client=False)
    result = comp.list_repositories()
    assert [e.value for e in result] == ["acme/web", "acme/api"]


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
    # team values are qualified org/team_slug; labels are humanized "org / Team Name"
    assert [(e.value, e.label) for e in result] == [("octo-org/core-team", "octo-org / Core Team")]


def test_list_teams_requires_org(tmp_path):
    # UserException("Select an organization...") is converted to exit(1) by the wrapper.
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    with pytest.raises(SystemExit):
        comp.list_teams()


# --------------------------------------------------- network-error guard (exit-2 regression)


@responses.activate
def test_test_connection_network_error_raises_userexception(tmp_path):
    """A connection error during testConnection must be a clean UserException, never escape to exit 2.

    The sync action routes through GitHubClient.get_response, whose try/except requests.RequestException
    converts the failure to UserException. The handler raises it directly (here we call the private
    _call_rate_limit to assert on the exception type, before the @sync_action wrapper maps it).
    """
    responses.add(
        responses.GET,
        "https://api.github.com/rate_limit",
        body=requests.exceptions.ConnectionError("connection reset by peer"),
    )
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    with pytest.raises(UserException, match="Could not reach GitHub"):
        comp._call_rate_limit()


@responses.activate
def test_list_organizations_network_error_userexception_via_enumeration(tmp_path):
    """A connection error during a dropdown loader is a clean UserException (asserted pre-wrapper).

    Calling the paginating enumeration helper shows the underlying raise is UserException with the
    network-guard message — i.e. it maps to exit 1, never escaping uncaught to exit 2.
    """
    responses.add(
        responses.GET,
        "https://api.github.com/user/orgs",
        body=requests.exceptions.ConnectTimeout("timed out"),
    )
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    with pytest.raises(UserException, match="Could not reach GitHub"):
        comp._list_org_logins()


@responses.activate
def test_list_organizations_network_error_exits_one_not_two(tmp_path):
    """End-to-end through the @sync_action wrapper: a network error becomes exit 1, never exit 2."""
    responses.add(
        responses.GET,
        "https://api.github.com/user/orgs",
        body=requests.exceptions.ConnectionError("connection reset by peer"),
    )
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    with pytest.raises(SystemExit) as exit_info:
        comp.list_organizations()
    assert exit_info.value.code == 1


@responses.activate
def test_list_organizations_skips_entries_missing_login(tmp_path):
    """A renamed/missing 'login' field must be skipped, never raise KeyError (exit-2 path)."""
    responses.add(
        responses.GET,
        "https://api.github.com/user/orgs",
        json=[{"login": "octo-org"}, {"id": 2}],  # second entry has no 'login'
        status=200,
    )
    comp = _component(_datadir(tmp_path, _SYNC_CONFIG), patch_client=False)
    result = comp.list_organizations()
    assert [e.value for e in result] == ["octo-org"]


# --------------------------------------------------- row schema: graphql_query data source


def test_row_schema_has_graphql_data_source():
    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "component_config" / "configRowSchema.json").read_text()
    )
    endpoint = schema["properties"]["endpoint"]
    assert "graphql_query" in endpoint["enum"]

    # gql_filenames must NOT be in the top-level required array (only endpoint should be)
    assert schema.get("required") == ["endpoint"]

    # query_template gated on endpoint only (shows for all graphql_query rows)
    qt_deps = schema["properties"]["query_template"]["options"]["dependencies"]
    assert qt_deps == {"endpoint": ["graphql_query"]}

    # Custom-only fields: gated on endpoint AND query_template=custom
    for field in ("gql_query", "gql_variables", "gql_row_path"):
        deps = schema["properties"][field]["options"]["dependencies"]
        assert "graphql_query" in deps["endpoint"], field
        assert deps["query_template"] == ["custom"], field

    # gql_org: gated on endpoint AND both template values (not custom)
    gql_org_deps = schema["properties"]["gql_org"]["options"]["dependencies"]
    assert "graphql_query" in gql_org_deps["endpoint"]
    assert set(gql_org_deps["query_template"]) == {"org_ai_config_files", "org_pull_request_reviews"}

    # gql_repo: gated on endpoint AND org_pull_request_reviews only
    gql_repo_deps = schema["properties"]["gql_repo"]["options"]["dependencies"]
    assert "graphql_query" in gql_repo_deps["endpoint"]
    assert gql_repo_deps["query_template"] == ["org_pull_request_reviews"]

    # gql_filenames: gated on endpoint AND org_ai_config_files only
    gql_fn_deps = schema["properties"]["gql_filenames"]["options"]["dependencies"]
    assert "graphql_query" in gql_fn_deps["endpoint"]
    assert gql_fn_deps["query_template"] == ["org_ai_config_files"]

    # output_table, primary_key, since, until, load_type: gated on endpoint only (all graphql_query rows)
    for field in ("output_table", "primary_key"):
        deps = schema["properties"][field]["options"]["dependencies"]
        assert deps == {"endpoint": ["graphql_query"]}, field
