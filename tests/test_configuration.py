"""Unit tests for the Configuration validators — every bad config is a clean UserException.

These guard the exit-1 boundary: a malformed config must surface as a UserException (which the
``__main__`` guard maps to exit 1), never an uncaught error (exit 2 — "internal error" in the UI).
Pydantic enum/type failures are re-wrapped with the "Configuration validation error" prefix by the
model's ``__init__``; the custom ``model_validator`` rules raise their own UserException directly.
"""

import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration


def test_pat_auth_empty_token_raises():
    """PAT auth with an empty #github_token is rejected (the _validate_auth rule)."""
    with pytest.raises(UserException, match="Personal Access Token"):
        Configuration(auth_type="pat", **{"#github_token": ""})


def test_unknown_endpoint_raises():
    """A non-empty endpoint that is not a registry key is rejected (the _validate_endpoint rule)."""
    with pytest.raises(UserException, match="Unknown endpoint 'not_a_real_endpoint'"):
        Configuration(auth_type="pat", endpoint="not_a_real_endpoint", **{"#github_token": "ghp_x"})


def test_invalid_state_enum_raises_with_validation_prefix():
    """A bogus state enum is a Pydantic ValidationError re-wrapped with the standard prefix."""
    with pytest.raises(UserException, match="Configuration validation error"):
        Configuration(auth_type="pat", issue_state="bogus", **{"#github_token": "ghp_x"})


def test_unparseable_since_raises():
    """A Since that is neither ISO nor a relative phrase is rejected (clean exit 1)."""
    with pytest.raises(UserException, match="Since must be ISO-8601 or a relative phrase"):
        Configuration(auth_type="pat", since="qwerty", **{"#github_token": "ghp_x"})


def test_unparseable_until_raises():
    """A bad Until is rejected too."""
    with pytest.raises(UserException, match="Until must be ISO-8601 or a relative phrase"):
        Configuration(auth_type="pat", until="not a date", **{"#github_token": "ghp_x"})


def test_valid_iso_since_parses_to_datetime():
    """A well-formed ISO instant resolves to a tz-aware datetime."""
    cfg = Configuration(auth_type="pat", since="2024-01-31T00:00:00Z", **{"#github_token": "ghp_x"})
    assert cfg.since == "2024-01-31T00:00:00Z"
    parsed = cfg.parsed_since()
    assert parsed is not None and parsed.isoformat().startswith("2024-01-31T00:00:00")


def test_empty_window_parses_to_none():
    """Empty since/until parse to None (unbounded that end)."""
    cfg = Configuration(auth_type="pat", **{"#github_token": "ghp_x"})
    assert cfg.parsed_since() is None and cfg.parsed_until() is None


def test_relative_window_parses_to_instants():
    """Relative phrases ('90 days ago', 'now') resolve to absolute tz-aware datetimes."""
    cfg = Configuration(auth_type="pat", since="90 days ago", until="now", **{"#github_token": "ghp_x"})
    s, u = cfg.parsed_since(), cfg.parsed_until()
    assert s is not None and u is not None and s < u
    assert s.tzinfo is not None and u.tzinfo is not None


# --------------------------------------------------- multi-select targets (string back-compat)


def test_string_target_coerces_to_list():
    """A legacy single-string target becomes a one-element list."""
    cfg = Configuration(
        auth_type="pat", endpoint="repository", repository="octocat/Hello", **{"#github_token": "ghp_x"}
    )
    assert cfg.repository == ["octocat/Hello"]


def test_empty_string_target_coerces_to_empty_list():
    """An empty-string target becomes [] (means 'all in scope')."""
    cfg = Configuration(auth_type="pat", endpoint="repository_issues", repository="", **{"#github_token": "ghp_x"})
    assert cfg.repository == []


def test_absent_target_defaults_to_empty_list():
    """An absent target field defaults to []."""
    cfg = Configuration(auth_type="pat", endpoint="user", **{"#github_token": "ghp_x"})
    assert cfg.organization == [] and cfg.repository == [] and cfg.team_slug == []


def test_list_target_passthrough():
    """An already-list target is preserved verbatim."""
    cfg = Configuration(
        auth_type="pat", endpoint="repository_issues", repository=["a/x", "a/y"], **{"#github_token": "ghp_x"}
    )
    assert cfg.repository == ["a/x", "a/y"]


def test_pull_reviews_with_pull_number_rejects_multiple_repos():
    """pull_number set + >1 repo is ambiguous and rejected."""
    with pytest.raises(UserException, match="exactly one repository"):
        Configuration(
            auth_type="pat",
            endpoint="repository_pull_reviews",
            repository=["a/x", "a/y"],
            pull_number=5,
            **{"#github_token": "ghp_x"},
        )


def test_pull_reviews_without_pull_number_allows_many_repos():
    """Empty pull_number permits many repos (nested fan-out over PRs)."""
    cfg = Configuration(
        auth_type="pat", endpoint="repository_pull_reviews", repository=["a/x", "a/y"], **{"#github_token": "ghp_x"}
    )
    assert cfg.repository == ["a/x", "a/y"]


# --------------------------------------------------- GraphQL query data source

GQL = {"#github_token": "ghp_x", "endpoint": "graphql_query"}


def test_graphql_endpoint_is_accepted():
    """The reserved graphql_query endpoint is valid even though it is not in ENDPOINTS."""
    cfg = Configuration(
        auth_type="pat", **GQL, query_template="custom", gql_query="{ viewer { login } }", output_table="t"
    )
    assert cfg.endpoint == "graphql_query"


def test_graphql_custom_requires_query_and_output_table():
    with pytest.raises(UserException, match="GraphQL query"):
        Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="", output_table="t")
    with pytest.raises(UserException, match="Output table"):
        Configuration(
            auth_type="pat", **GQL, query_template="custom", gql_query="{ viewer { login } }", output_table=""
        )


def test_graphql_variables_parsed_from_json_string():
    cfg = Configuration(
        auth_type="pat",
        **GQL,
        query_template="custom",
        gql_query="q",
        output_table="t",
        gql_variables='{"org": "acme"}',
    )
    assert cfg.gql_variables == {"org": "acme"}


def test_graphql_variables_blank_is_empty_dict():
    cfg = Configuration(
        auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="t", gql_variables=""
    )
    assert cfg.gql_variables == {}


def test_graphql_variables_bad_json_raises():
    with pytest.raises(UserException, match="GraphQL Variables"):
        Configuration(
            auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="t", gql_variables="{not json"
        )


def test_graphql_primary_key_string_coerces_to_list():
    cfg = Configuration(
        auth_type="pat",
        **GQL,
        query_template="custom",
        gql_query="q",
        output_table="t",
        primary_key="nameWithOwner, number",
    )
    assert cfg.primary_key == ["nameWithOwner", "number"]


def test_graphql_output_table_rejects_unsafe_name():
    with pytest.raises(UserException, match="Output table"):
        Configuration(auth_type="pat", **GQL, query_template="custom", gql_query="q", output_table="bad name!")


def test_graphql_ai_config_template_requires_org():
    with pytest.raises(UserException, match="Organization"):
        Configuration(auth_type="pat", **GQL, query_template="org_ai_config_files", gql_org="")


def test_graphql_ai_config_template_rejects_filename_with_quote():
    with pytest.raises(UserException, match="file name"):
        Configuration(
            auth_type="pat",
            **GQL,
            query_template="org_ai_config_files",
            gql_org="acme",
            gql_filenames=["AGENTS.md", 'bad"name'],
        )


def test_graphql_ai_config_template_rejects_filename_with_backslash():
    """A backslash in a file name would escape the closing quote in the interpolated GraphQL string."""
    with pytest.raises(UserException, match="file name"):
        Configuration(
            auth_type="pat",
            **GQL,
            query_template="org_ai_config_files",
            gql_org="acme",
            gql_filenames=["AGENTS.md", "bad\\name"],
        )


def test_graphql_pr_reviews_template_requires_org_and_repo():
    with pytest.raises(UserException, match="Repository"):
        Configuration(auth_type="pat", **GQL, query_template="org_pull_request_reviews", gql_org="acme", gql_repo="")


def test_gql_filenames_absent_falls_back_to_default():
    """An absent gql_filenames keeps the field default (the coercion must not force [])."""
    cfg = Configuration(auth_type="pat", **GQL, query_template="org_ai_config_files", gql_org="acme")
    assert cfg.gql_filenames == ["AGENTS.md", "CLAUDE.md"]


def test_gql_filenames_none_falls_back_to_default():
    """An explicit None gql_filenames falls back to the default."""
    cfg = Configuration(
        auth_type="pat", **GQL, query_template="org_ai_config_files", gql_org="acme", gql_filenames=None
    )
    assert cfg.gql_filenames == ["AGENTS.md", "CLAUDE.md"]


def test_gql_filenames_comma_string_splits():
    """A non-empty comma string still splits to a list."""
    cfg = Configuration(
        auth_type="pat",
        **GQL,
        query_template="org_ai_config_files",
        gql_org="acme",
        gql_filenames="AGENTS.md, CLAUDE.md",
    )
    assert cfg.gql_filenames == ["AGENTS.md", "CLAUDE.md"]


def test_gql_filenames_blank_string_falls_back_to_default():
    """A blank/whitespace string coerces to the default, not []."""
    cfg = Configuration(
        auth_type="pat", **GQL, query_template="org_ai_config_files", gql_org="acme", gql_filenames="  "
    )
    assert cfg.gql_filenames == ["AGENTS.md", "CLAUDE.md"]


# --------------------------------------------------- new org-wide templates


def test_graphql_org_pull_requests_requires_org():
    with pytest.raises(UserException, match="Organization"):
        Configuration(auth_type="pat", **GQL, query_template="org_pull_requests", gql_org="")


def test_graphql_org_pull_requests_with_org_constructs():
    cfg = Configuration(auth_type="pat", **GQL, query_template="org_pull_requests", gql_org="acme")
    assert cfg.query_template == "org_pull_requests"
    assert cfg.gql_org == "acme"


def test_graphql_org_repository_languages_requires_org():
    with pytest.raises(UserException, match="Organization"):
        Configuration(auth_type="pat", **GQL, query_template="org_repository_languages", gql_org="")


def test_graphql_org_repository_languages_with_org_constructs():
    cfg = Configuration(auth_type="pat", **GQL, query_template="org_repository_languages", gql_org="acme")
    assert cfg.query_template == "org_repository_languages"


def test_graphql_org_commits_requires_org():
    with pytest.raises(UserException, match="Organization"):
        Configuration(auth_type="pat", **GQL, query_template="org_commits", gql_org="")


def test_graphql_org_commits_with_org_constructs():
    cfg = Configuration(auth_type="pat", **GQL, query_template="org_commits", gql_org="acme")
    assert cfg.query_template == "org_commits"


# --------------------------------------------------------------- Enterprise API base URL (CFTL-858)


def test_api_base_url_defaults_to_empty():
    """An unset API Base URL stays empty — the component then falls back to github.com."""
    cfg = Configuration(auth_type="pat", **{"#github_token": "ghp_x"})
    assert cfg.api_base_url == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.example.ghe.com", "https://api.example.ghe.com"),
        ("https://api.example.ghe.com/", "https://api.example.ghe.com"),
        ("  https://api.example.ghe.com///  ", "https://api.example.ghe.com"),
        ("https://ghe.example.com/api/v3", "https://ghe.example.com/api/v3"),
    ],
)
def test_api_base_url_is_cleaned(raw, expected):
    """Whitespace and trailing slashes are stripped off a pasted URL."""
    cfg = Configuration(auth_type="pat", api_base_url=raw, **{"#github_token": "ghp_x"})
    assert cfg.api_base_url == expected


@pytest.mark.parametrize(
    "bad",
    [
        "api.example.ghe.com",  # bare host, no scheme
        "ftp://api.example.ghe.com",  # wrong scheme
        "not a url",
        "https://",  # no host
        "http://ghe.example.com/api/v3",  # cleartext would send the PAT unencrypted
    ],
)
def test_api_base_url_must_be_absolute_https_url(bad):
    """A bare host, a wrong scheme, or a typo is a clean UserException, not a late network error."""
    with pytest.raises(UserException, match="must be an absolute https URL"):
        Configuration(auth_type="pat", api_base_url=bad, **{"#github_token": "ghp_x"})


@pytest.mark.parametrize(
    "bad",
    [
        "https://user:ghp_secret@ghe.example.com/api/v3",
        "http://user:ghp_secret@ghe.example.com/api/v3",  # bad scheme too: userinfo must win
        "https://ghe.example.com/api/v3?access_token=ghp_secret",  # '@'-free, caught by the ? rule
    ],
)
def test_api_base_url_never_echoes_a_credential(bad):
    """A rejected URL must not be echoed: the exception text lands in the job log."""
    with pytest.raises(UserException) as excinfo:
        Configuration(auth_type="pat", api_base_url=bad, **{"#github_token": "ghp_x"})
    assert "ghp_secret" not in str(excinfo.value)


def test_api_base_url_rejects_embedded_credentials():
    """Userinfo would reach the client's network-error message and land in the job log."""
    with pytest.raises(UserException, match="must not contain a username or password"):
        Configuration(
            auth_type="pat",
            api_base_url="https://user:ghp_secret@ghe.example.com/api/v3",
            **{"#github_token": "ghp_x"},
        )


@pytest.mark.parametrize(
    "bad",
    [
        "https://ghe.example.com/api/v3?x=1",
        "https://ghe.example.com/api/v3#frag",
        # Bare '?'/'#' parse to an EMPTY query/fragment, so a urlparse-field check lets them
        # through — but they still truncate every request path to '/api/v3'.
        "https://ghe.example.com/api/v3?",
        "https://ghe.example.com/api/v3#",
        "https://ghe.example.com/api/v3;p=1",
    ],
)
def test_api_base_url_rejects_query_and_fragment(bad):
    """A query/fragment would truncate or mangle every request path."""
    with pytest.raises(UserException, match="no query string"):
        Configuration(auth_type="pat", api_base_url=bad, **{"#github_token": "ghp_x"})


def test_api_base_url_with_oauth_raises():
    """OAuth + an Enterprise host is rejected: Keboola's OAuth app only exists on github.com."""
    with pytest.raises(UserException, match="OAuth cannot authenticate to a GitHub Enterprise host"):
        Configuration(auth_type="oauth", api_base_url="https://api.example.ghe.com")


def test_oauth_without_api_base_url_is_unaffected():
    """The OAuth guard fires only when an Enterprise host is set — github.com OAuth still loads."""
    cfg = Configuration(auth_type="oauth")
    assert cfg.api_base_url == ""


def test_api_base_url_null_is_treated_as_empty():
    """A field cleared in the UI can arrive as null — it means github.com, not a type error."""
    cfg = Configuration(auth_type="pat", api_base_url=None, **{"#github_token": "ghp_x"})
    assert cfg.api_base_url == ""
