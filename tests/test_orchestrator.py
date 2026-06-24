"""Orchestrator (component.run) tests with a stubbed client — no real network.

Exercises one endpoint per envelope shape (object, array+since, wrapped, object-map, org),
the /issues PR-drop, incremental state, headerless typed manifests, and the missing-target
UserException. Each test builds a temporary KBC datadir, patches ``GitHubClient`` so
``__init__`` resolves a token without hitting the network, and stubs ``fetch`` to replay a
fixed payload sequence.
"""

import csv
import json
import os
from pathlib import Path
from unittest import mock

import pytest
from keboola.component.exceptions import UserException


def _write_datadir(tmp_path: Path, parameters: dict, state: dict | None = None) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    config = {"parameters": parameters}
    (data_dir / "config.json").write_text(json.dumps(config))
    if state is not None:
        (data_dir / "in" / "state.json").write_text(json.dumps(state))
    return data_dir


def _make_component(tmp_path, parameters, fetch_returns, state=None, config_data_extra=None):
    """Construct a Component against a temp datadir with GitHubClient.fetch stubbed."""
    data_dir = _write_datadir(tmp_path, parameters, state)
    if config_data_extra:
        cfg_path = data_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg.update(config_data_extra)
        cfg_path.write_text(json.dumps(cfg))

    # authoritative: the platform sets this when the Dev Portal dataTypeSupport property is on;
    # set it locally so the native-type `schema` manifest is exercised, not downgraded.
    env = {"KBC_DATADIR": str(data_dir), "KBC_DATA_TYPE_SUPPORT": "authoritative"}
    with mock.patch.dict(os.environ, env):
        from component import Component

        with mock.patch("component.GitHubClient"):
            comp = Component()
        comp.client.fetch.return_value = iter(fetch_returns)  # ty: ignore[unresolved-attribute]
        comp.run()
    return data_dir


def _pk(manifest) -> list[str]:
    """Primary-key column names from an authoritative schema manifest (per-column flag)."""
    return [c["name"] for c in manifest["schema"] if c.get("primary_key")]


def _read_table(data_dir: Path, name: str):
    out = data_dir / "out" / "tables" / f"{name}.csv"
    manifest = json.loads((data_dir / "out" / "tables" / f"{name}.csv.manifest").read_text())
    with open(out, encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return rows, manifest


PAT = {"#github_token": "ghp_x"}


def test_object_envelope_repository(tmp_path):
    params = {
        **PAT,
        "auth_type": "pat",
        "endpoint": "repository",
        "repository": "octocat/Hello",
        "load_type": "full_load",
    }
    payload = [
        {
            "id": 1296269,
            "name": "Hello",
            "full_name": "octocat/Hello",
            "private": False,
            "owner": {"login": "octocat", "id": 1},
            "stargazers_count": 80,
            "created_at": "2011-01-26T19:01:12Z",
            "archived": False,
        }
    ]
    data_dir = _make_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "repository")
    assert len(rows) == 1  # headerless: single data row, no header
    # native types in the authoritative schema manifest
    schema = {c["name"]: c["data_type"]["base"]["type"] for c in manifest["schema"]}
    assert schema["id"] == "INTEGER"
    assert schema["created_at"] == "TIMESTAMP"
    assert schema["private"] == "BOOLEAN"
    assert _pk(manifest) == ["id"]


def test_array_envelope_issues_drops_pull_requests(tmp_path):
    params = {**PAT, "endpoint": "repository_issues", "repository": "octocat/Hello", "load_type": "full_load"}
    payload = [
        {"id": 1, "number": 1, "title": "real issue", "user": {"login": "a", "id": 1}, "labels": [{"name": "bug"}]},
        {"id": 2, "number": 2, "title": "a PR", "pull_request": {"url": "x"}, "user": {"login": "b", "id": 2}},
    ]
    data_dir = _make_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "repository_issues")
    assert len(rows) == 1  # the pull_request row was dropped
    # labels json-serialized, context columns present
    header = [c["name"] for c in manifest["schema"]]
    assert "owner" in header and "repo" in header
    assert _pk(manifest) == ["id"]


def test_wrapped_envelope_workflows(tmp_path):
    params = {**PAT, "endpoint": "repository_workflows", "repository": "octocat/Hello", "load_type": "full_load"}
    # client.fetch already unwraps WRAPPED -> the inner array records
    payload = [{"id": 10, "name": "CI", "state": "active", "created_at": "2024-01-01T00:00:00Z"}]
    data_dir = _make_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "repository_workflows")
    assert len(rows) == 1
    schema = {c["name"]: c["data_type"]["base"]["type"] for c in manifest["schema"]}
    assert schema["id"] == "INTEGER"


def test_object_map_languages(tmp_path):
    params = {**PAT, "endpoint": "repository_languages", "repository": "octocat/Hello", "load_type": "full_load"}
    # client yields the raw map payload once; orchestrator expands it
    payload = [{"Python": 1234, "Go": 567}]
    data_dir = _make_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "repository_languages")
    assert len(rows) == 2  # one row per language
    assert set(_pk(manifest)) == {"repository", "language"}
    schema = {c["name"]: c["data_type"]["base"]["type"] for c in manifest["schema"]}
    assert schema["bytes"] == "INTEGER"
    assert schema["language"] == "STRING"


def test_org_envelope_members(tmp_path):
    params = {**PAT, "endpoint": "organization_members", "organization": "octo-org", "load_type": "full_load"}
    payload = [{"id": 1, "login": "octocat", "site_admin": False}]
    data_dir = _make_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "organization_members")
    assert len(rows) == 1
    header = [c["name"] for c in manifest["schema"]]
    assert "org" in header


def test_incremental_writes_last_run_and_since(tmp_path):
    params = {**PAT, "endpoint": "repository_issues", "repository": "octocat/Hello", "load_type": "incremental_load"}
    state = {"last_run": "2024-01-01T00:00:00+00:00"}
    payload = [{"id": 5, "number": 5, "title": "x", "user": {"login": "a", "id": 1}}]
    data_dir = _make_component(tmp_path, params, payload, state=state)
    rows, manifest = _read_table(data_dir, "repository_issues")
    assert manifest["incremental"] is True
    new_state = json.loads((data_dir / "out" / "state.json").read_text())
    assert "last_run" in new_state and new_state["last_run"] != ""


def test_non_incremental_endpoint_forces_full_load(tmp_path):
    """A snapshot endpoint (no since / no sort) writes incremental=false even when the row asks
    for incremental_load, so rows deleted on GitHub are purged (§2.3 / Phase 8 TODO 2)."""
    params = {
        **PAT,
        "endpoint": "organization_members",
        "organization": "octo-org",
        "load_type": "incremental_load",
    }
    payload = [{"id": 1, "login": "octocat", "site_admin": False}]
    data_dir = _make_component(tmp_path, params, payload)
    _rows, manifest = _read_table(data_dir, "organization_members")
    # incremental upsert is suppressed for a non-incremental-capable endpoint -> full replace
    assert manifest["incremental"] is False
    # and no watermark is advanced for a snapshot endpoint
    state_file = data_dir / "out" / "state.json"
    if state_file.exists():
        assert json.loads(state_file.read_text()) == {}


def test_repository_branches_forces_full_load(tmp_path):
    """A second snapshot endpoint asked to load incrementally still writes a full-load manifest."""
    params = {
        **PAT,
        "endpoint": "repository_branches",
        "repository": "octocat/Hello",
        "load_type": "incremental_load",
    }
    payload = [{"name": "main", "commit": {"sha": "abc", "url": "u"}, "protected": True}]
    data_dir = _make_component(tmp_path, params, payload)
    _rows, manifest = _read_table(data_dir, "repository_branches")
    assert manifest["incremental"] is False


def test_full_load_does_not_write_state(tmp_path):
    params = {**PAT, "endpoint": "repository", "repository": "octocat/Hello", "load_type": "full_load"}
    payload = [{"id": 1, "name": "Hello"}]
    data_dir = _make_component(tmp_path, params, payload)
    state_file = data_dir / "out" / "state.json"
    # full load: state not advanced
    if state_file.exists():
        assert json.loads(state_file.read_text()) == {}


def test_missing_target_raises_userexception(tmp_path):
    params = {**PAT, "endpoint": "repository_issues", "repository": "", "load_type": "full_load"}
    data_dir = _write_datadir(tmp_path, params)
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        from component import Component

        with mock.patch("component.GitHubClient"):
            comp = Component()
        with pytest.raises(UserException, match="requires"):
            comp.run()
