"""Unit tests for the GraphQL flatten / detection / template helpers."""

from graphql_source import (
    collect_columns,
    flatten_graphql_node,
    sanitize_alias,
)


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
