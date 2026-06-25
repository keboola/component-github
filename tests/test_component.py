import json
import os
import unittest
from pathlib import Path
from unittest import mock

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
    comp.client.get_json_array.return_value = [  # ty: ignore[unresolved-attribute]
        {"full_name": "acme/one"},
        {"full_name": "acme/two"},
    ]
    targets = comp._resolve_targets(ENDPOINTS["repository_commits"])
    assert sorted(t["repository"] for t in targets) == ["acme/one", "acme/two"]


def test_resolve_targets_org_empty_fans_all_orgs(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "organization_members", "organization": []})
    comp.client.get_json_array.return_value = [{"login": "org1"}, {"login": "org2"}]  # ty: ignore[unresolved-attribute]
    targets = comp._resolve_targets(ENDPOINTS["organization_members"])
    assert sorted(t["org"] for t in targets) == ["org1", "org2"]


def test_resolve_targets_none_is_single_empty_context(tmp_path):
    comp = _component(tmp_path, {**PAT, "endpoint": "user"})
    assert comp._resolve_targets(ENDPOINTS["user"]) == [{}]


if __name__ == "__main__":
    unittest.main()
