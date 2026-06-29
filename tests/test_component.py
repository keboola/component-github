import json
import os
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

from component import Component
from endpoints import ENDPOINTS


class TestComponent(unittest.TestCase):
    # set global time to 2010-10-10 - affects functions like datetime.now()
    @freeze_time("2010-10-10")
    # set KBC_DATADIR env to non-existing dir
    @mock.patch.dict(os.environ, {"KBC_DATADIR": "./non-existing-dir"})
    def test_run_no_cfg_fails(self):
        with self.assertRaises(ValueError):
            comp = Component()
            comp.run()


# --------------------------------------------------- _resolve_targets (fan-out resolver, Task 3)

PAT = {"#github_token": "ghp_x"}


def _component(tmp_path: Path, parameters: dict) -> Component:
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": parameters}))
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        with mock.patch("component.GitHubClient"):
            return Component()


def test_resolve_targets_selected_repos(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "repository_commits", "repository": ["a/x", "a/y"]})
    targets = comp._resolve_targets(ENDPOINTS["repository_commits"])
    assert [t["repository"] for t in targets] == ["a/x", "a/y"]
    assert targets[0]["owner"] == "a" and targets[0]["repo"] == "x"


def test_resolve_targets_empty_repo_fans_over_org_repos(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "repository_commits", "organization": ["acme"], "repository": []})
    comp.client.get_json_array_all.return_value = [  # ty: ignore[unresolved-attribute]
        {"full_name": "acme/one"},
        {"full_name": "acme/two"},
    ]
    targets = comp._resolve_targets(ENDPOINTS["repository_commits"])
    assert sorted(t["repository"] for t in targets) == ["acme/one", "acme/two"]


def test_resolve_targets_org_empty_fans_all_orgs(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "organization_members", "organization": []})
    comp.client.get_json_array_all.return_value = [{"login": "org1"}, {"login": "org2"}]  # ty: ignore[unresolved-attribute]
    targets = comp._resolve_targets(ENDPOINTS["organization_members"])
    assert sorted(t["org"] for t in targets) == ["org1", "org2"]


def test_resolve_targets_none_is_single_empty_context(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "user"})
    assert comp._resolve_targets(ENDPOINTS["user"]) == [{}]


if __name__ == "__main__":
    unittest.main()


def _read_csv(path):
    import csv as _csv

    with open(path, newline="") as fh:
        return list(_csv.reader(fh))


def test_run_graphql_custom_writes_dynamic_table(tmp_path):
    comp = _component(
        tmp_path,
        {
            **PAT,
            "endpoint": "graphql_query",
            "query_template": "custom",
            "gql_query": "{ viewer { login } }",
            "output_table": "gql_out",
            "load_type": "full_load",
        },
    )
    comp.client.graphql = MagicMock(
        return_value=(
            [{"nameWithOwner": "o/a", "defaultBranchRef": {"name": "main"}}, {"nameWithOwner": "o/b"}],
            "organization.repositories.nodes",
        )
    )
    comp.run()
    out = comp.tables_out_path
    rows = _read_csv(f"{out}/gql_out.csv")
    # union columns, first-seen order: nameWithOwner, defaultBranchRef_name
    assert rows[0] == ["o/a", "main"]
    assert rows[1] == ["o/b", ""]


def test_run_graphql_template_uses_registry(tmp_path):
    comp = _component(
        tmp_path,
        {
            **PAT,
            "endpoint": "graphql_query",
            "query_template": "org_ai_config_files",
            "gql_org": "acme",
            "load_type": "full_load",
        },
    )
    captured = {}

    def _fake_graphql(query, variables, rows_path):
        captured["query"] = query
        captured["rows_path"] = rows_path
        return ([{"nameWithOwner": "acme/x"}], rows_path)

    comp.client.graphql = MagicMock(side_effect=_fake_graphql)
    comp.run()
    assert 'agents_md: object(expression: "HEAD:AGENTS.md")' in captured["query"]
    assert captured["rows_path"] == "organization.repositories.nodes"
    # template default table name used when output_table is empty
    assert (tmp_path / "data" / "out" / "tables" / "repo_ai_config_files.csv").exists()


def test_run_graphql_incremental_without_pk_falls_back_to_full(tmp_path):
    comp = _component(
        tmp_path,
        {
            **PAT,
            "endpoint": "graphql_query",
            "query_template": "custom",
            "gql_query": "{ viewer { login } }",
            "output_table": "gql_out",
            "load_type": "incremental_load",
        },
    )
    comp.client.graphql = MagicMock(return_value=([{"login": "octocat"}], "viewer"))
    comp.run()  # no primary_key set -> must not raise; writes a full load
    manifest = json.loads((tmp_path / "data" / "out" / "tables" / "gql_out.csv.manifest").read_text())
    assert manifest["incremental"] is False


def test_run_graphql_substitutes_since_placeholder(tmp_path):
    import re as _re

    comp = _component(
        tmp_path,
        {
            **PAT,
            "endpoint": "graphql_query",
            "query_template": "custom",
            "gql_query": "query($q:String!){ search(query:$q,type:ISSUE,first:1){ nodes{ ... on PullRequest { id } } } }",
            "gql_variables": '{"q": "org:keboola is:pr updated:>={{since}}"}',
            "gql_row_path": "search.nodes",
            "output_table": "prs",
            "primary_key": "id",
            "since": "60 days ago",
            "load_type": "full_load",
        },
    )
    captured = {}

    def _fake_graphql(query, variables, rows_path):
        captured["variables"] = variables
        return ([{"id": "x"}], rows_path)

    comp.client.graphql = MagicMock(side_effect=_fake_graphql)
    comp.run()
    # {{since}} resolved to a concrete YYYY-MM-DD date before the query was sent
    assert "{{since}}" not in captured["variables"]["q"]
    assert _re.search(r"updated:>=\d{4}-\d{2}-\d{2}", captured["variables"]["q"])


def test_run_graphql_primary_key_missing_column_raises(tmp_path):
    from keboola.component.exceptions import UserException

    comp = _component(
        tmp_path,
        {
            **PAT,
            "endpoint": "graphql_query",
            "query_template": "custom",
            "gql_query": "{ viewer { login } }",
            "output_table": "gql_out",
            "primary_key": "does_not_exist",
            "load_type": "incremental_load",
        },
    )
    comp.client.graphql = MagicMock(return_value=([{"login": "octocat"}], "viewer"))
    with pytest.raises(UserException, match="Primary key"):
        comp.run()
