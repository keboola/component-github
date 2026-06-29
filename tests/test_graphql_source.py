"""Unit tests for the GraphQL flatten / detection / template helpers."""

import pytest
from keboola.component.exceptions import UserException

from graphql_source import (
    TEMPLATES,
    apply_date_placeholders,
    collect_columns,
    connection_path,
    detect_rows_path,
    flatten_graphql_node,
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
