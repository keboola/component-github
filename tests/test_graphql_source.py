"""Unit tests for the GraphQL flatten / detection / template helpers."""

from datetime import UTC, datetime

import pytest
from keboola.component.exceptions import UserException

from graphql_source import (
    TEMPLATES,
    apply_date_placeholders,
    apply_window_placeholder,
    collect_columns,
    connection_path,
    detect_rows_path,
    flatten_graphql_node,
    has_window_placeholder,
    iso_range,
    read_path,
    sanitize_alias,
)


def test_apply_date_placeholders_substitutes_since_and_until():
    out = apply_date_placeholders("updated:>={{since}} updated:<={{until}}", "2026-04-30", "2026-06-29")
    assert out == "updated:>=2026-04-30 updated:<=2026-06-29"


def test_apply_date_placeholders_no_placeholder_is_noop():
    assert apply_date_placeholders("org:keboola is:pr", "2026-04-30", None) == "org:keboola is:pr"


def test_apply_date_placeholders_missing_value_raises():
    with pytest.raises(UserException, match="Since"):
        apply_date_placeholders("updated:>={{since}}", None, None)


def test_sanitize_alias():
    assert sanitize_alias("AGENTS.md") == "agents_md"
    assert sanitize_alias("CLAUDE.md") == "claude_md"
    assert sanitize_alias(".github/workflows/ci.yml") == "github_workflows_ci_yml"


def test_flatten_scalars_and_nested_object():
    node = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}
    assert flatten_graphql_node(node) == {"nameWithOwner": "o/r", "defaultBranchRef_name": "main"}


def test_flatten_null_object_becomes_empty_cell():
    assert flatten_graphql_node({"agents_md": None}) == {"agents_md": None}


def test_flatten_array_is_json_encoded():
    out = flatten_graphql_node({"labels": [{"name": "bug"}, {"name": "p1"}]})
    assert out == {"labels": '[{"name":"bug"},{"name":"p1"}]'}


def test_collect_columns_first_seen_union():
    rows = [{"a": 1, "b": 2}, {"a": 3, "c": 4}]
    assert collect_columns(rows) == ["a", "b", "c"]


def test_read_path_navigates_and_empty_returns_data():
    data = {"organization": {"repositories": {"nodes": [1, 2]}}}
    assert read_path(data, "organization.repositories.nodes") == [1, 2]
    assert read_path(data, "") == data
    assert read_path(data, "organization.missing") is None


def test_connection_path_strips_nodes_or_edges():
    assert connection_path("organization.repositories.nodes") == "organization.repositories"
    assert connection_path("repository.pullRequests.edges") == "repository.pullRequests"
    assert connection_path("viewer") is None


def test_detect_rows_path_nested_connection():
    data = {"organization": {"repositories": {"pageInfo": {"hasNextPage": False}, "nodes": [{"x": 1}]}}}
    assert detect_rows_path(data) == "organization.repositories.nodes"


def test_detect_rows_path_prefers_nodes_over_edges():
    data = {"search": {"nodes": [{"x": 1}], "edges": [{"y": 2}]}}
    assert detect_rows_path(data) == "search.nodes"


def test_detect_rows_path_single_object_is_one_row():
    data = {"viewer": {"login": "octocat"}}
    assert detect_rows_path(data) == "viewer"


def test_detect_rows_path_ambiguous_sibling_arrays_raises():
    data = {"a": {"items": [{"x": 1}], "others": [{"y": 2}]}}
    with pytest.raises(UserException, match="Row path"):
        detect_rows_path(data)


def test_detect_rows_path_ambiguous_object_children_raises():
    data = {"a": {"login": "x"}, "b": {"login": "y"}}
    with pytest.raises(UserException, match="Row path"):
        detect_rows_path(data)


def test_ai_config_template_builds_aliased_blob_fields():
    tpl = TEMPLATES["org_ai_config_files"]
    query = tpl.build_query({"org": "acme", "filenames": ["AGENTS.md", "CLAUDE.md"]})
    assert 'agents_md: object(expression: "HEAD:AGENTS.md")' in query
    assert 'claude_md: object(expression: "HEAD:CLAUDE.md")' in query
    assert "$cursor" in query and "pageInfo" in query
    assert tpl.build_variables({"org": "acme", "filenames": []}) == {"org": "acme"}
    assert tpl.rows_path == "organization.repositories.nodes"
    assert tpl.primary_key == ["nameWithOwner"]


def test_pr_reviews_template_uses_org_and_repo():
    tpl = TEMPLATES["org_pull_request_reviews"]
    query = tpl.build_query({"org": "acme", "repo": "web"})
    assert "reviewDecision" in query and "pullRequests" in query
    assert tpl.build_variables({"org": "acme", "repo": "web"}) == {"org": "acme", "repo": "web"}
    assert tpl.rows_path == "repository.pullRequests.nodes"
    assert tpl.primary_key == ["number"]


def test_pr_reviews_build_variables_strips_owner_prefix():
    """gql_repo stored as owner/repo (from listRepositories) → bare name in $repo variable."""
    tpl = TEMPLATES["org_pull_request_reviews"]
    assert tpl.build_variables({"org": "acme", "repo": "acme/web"}) == {"org": "acme", "repo": "web"}


def test_pr_reviews_build_variables_bare_name_unchanged():
    """gql_repo already a bare name (no slash) → passed through unchanged."""
    tpl = TEMPLATES["org_pull_request_reviews"]
    assert tpl.build_variables({"org": "acme", "repo": "web"}) == {"org": "acme", "repo": "web"}


# --------------------------------------------------- new org-wide templates


def test_org_pull_requests_template_query_content():
    tpl = TEMPLATES["org_pull_requests"]
    query = tpl.build_query({"org": "acme"})
    assert "reviewThreads" in query
    assert "reviews" in query
    assert "$cursor" in query
    assert "pageInfo" in query


def test_org_pull_requests_template_metadata():
    tpl = TEMPLATES["org_pull_requests"]
    assert tpl.rows_path == "search.nodes"
    assert tpl.output_table == "pull_requests_graphql"
    assert tpl.primary_key == ["id"]


def test_org_pull_requests_build_variables_preserves_window_placeholder():
    """build_variables must embed the literal {{window}} so the component substitutes it per shard."""
    tpl = TEMPLATES["org_pull_requests"]
    variables = tpl.build_variables({"org": "acme"})
    assert variables["q"] == "org:acme is:pr updated:{{window}} sort:updated-desc"


def test_org_repository_languages_template_query_content():
    tpl = TEMPLATES["org_repository_languages"]
    query = tpl.build_query({"org": "acme"})
    assert "languages(" in query
    assert "$cursor" in query
    assert "pageInfo" in query


def test_org_repository_languages_template_metadata():
    tpl = TEMPLATES["org_repository_languages"]
    assert tpl.rows_path == "organization.repositories.nodes"
    assert tpl.output_table == "repository_languages_graphql"
    assert tpl.primary_key == ["nameWithOwner"]


def test_org_repository_languages_build_variables():
    tpl = TEMPLATES["org_repository_languages"]
    assert tpl.build_variables({"org": "acme"}) == {"org": "acme"}


def test_org_commits_template_query_content():
    tpl = TEMPLATES["org_commits"]
    query = tpl.build_query({"org": "acme"})
    assert "history(" in query
    assert "{{since}}" in query
    assert "$cursor" in query
    assert "pageInfo" in query


def test_org_commits_template_metadata():
    tpl = TEMPLATES["org_commits"]
    assert tpl.rows_path == "organization.repositories.nodes"
    assert tpl.output_table == "repository_commits_graphql"
    assert tpl.primary_key == ["nameWithOwner"]


def test_org_commits_build_variables():
    tpl = TEMPLATES["org_commits"]
    assert tpl.build_variables({"org": "acme"}) == {"org": "acme"}


# --------------------------------------------------- window placeholder helpers


def test_has_window_placeholder_true():
    assert has_window_placeholder("updated:{{window}} sort:updated-desc") is True
    assert has_window_placeholder("updated:{{ window }} sort:updated-desc") is True


def test_has_window_placeholder_false():
    assert has_window_placeholder("updated:>={{since}} sort:updated-desc") is False
    assert has_window_placeholder("no placeholder here") is False


def test_apply_window_placeholder_substitutes():
    result = apply_window_placeholder(
        "updated:{{window}} sort:updated-desc", "2026-01-01T00:00:00+00:00..2026-06-29T00:00:00+00:00"
    )
    assert result == "updated:2026-01-01T00:00:00+00:00..2026-06-29T00:00:00+00:00 sort:updated-desc"


def test_apply_window_placeholder_spaces_substitutes():
    result = apply_window_placeholder("updated:{{ window }}", "A..B")
    assert result == "updated:A..B"


def test_iso_range_format():
    lo = datetime(2026, 1, 1, tzinfo=UTC)
    hi = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
    result = iso_range(lo, hi)
    assert result == "2026-01-01T00:00:00+00:00..2026-06-29T12:00:00+00:00"
    assert ".." in result
    assert result.startswith("2026-01-01")


def test_org_pull_requests_template_uses_window_placeholder():
    """The org_pull_requests template must use {{window}} (not {{since}}) for sharded mode."""
    tpl = TEMPLATES["org_pull_requests"]
    variables = tpl.build_variables({"org": "keboola"})
    assert "{{window}}" in variables["q"]
    assert "{{since}}" not in variables["q"]
    assert has_window_placeholder(variables["q"])
