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
    data_dir, _comp = _run_component(tmp_path, parameters, fetch_returns, state, config_data_extra)
    return data_dir


def _run_component(tmp_path, parameters, fetch_returns, state=None, config_data_extra=None):
    """Run a Component with fetch stubbed; return ``(data_dir, comp)`` so callers can inspect fetch."""
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
    return data_dir, comp


def _fetch_params(comp) -> dict:
    """The query params passed to the stubbed client.fetch (3rd positional arg of run())."""
    args, kwargs = comp.client.fetch.call_args
    # component.run() calls client.fetch(endpoint, path, params, stop_paging=...)
    return args[2] if len(args) > 2 else kwargs.get("params", {})


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


def test_repository_commits_pk_is_repository_scoped(tmp_path):
    """repository_commits shares a table across repos, so its PK must be ['repository', 'sha'].

    A commit SHA is not globally unique (a fork shares its base repo's SHAs); without the
    `repository` scope, incremental upserts collide across repos extracted into the same table.
    The synthetic `repository` column must be present in the schema and populated per row.
    """
    params = {**PAT, "endpoint": "repository_commits", "repository": "octocat/Hello", "load_type": "full_load"}
    payload = [{"sha": "abc", "commit": {"message": "init"}}]
    data_dir = _make_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "repository_commits")
    # _pk reflects schema column order, not PK declaration order; assert as a set.
    assert set(_pk(manifest)) == {"repository", "sha"}
    header = [c["name"] for c in manifest["schema"]]
    assert "repository" in header
    # the repository column is populated with owner/repo on the written row
    repo_idx = header.index("repository")
    assert rows[0][repo_idx] == "octocat/Hello"


def test_org_envelope_members(tmp_path):
    params = {**PAT, "endpoint": "organization_members", "organization": "octo-org", "load_type": "full_load"}
    payload = [{"id": 1, "login": "octocat", "site_admin": False}]
    data_dir = _make_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "organization_members")
    assert len(rows) == 1
    header = [c["name"] for c in manifest["schema"]]
    assert "org" in header


def test_incremental_endpoint_writes_upsert_manifest_and_no_state(tmp_path):
    """incremental_load → upsert manifest; the windowing model writes NO state file."""
    params = {**PAT, "endpoint": "repository_issues", "repository": "octocat/Hello", "load_type": "incremental_load"}
    payload = [{"id": 5, "number": 5, "title": "x", "user": {"login": "a", "id": 1}}]
    data_dir = _make_component(tmp_path, params, payload)
    _rows, manifest = _read_table(data_dir, "repository_issues")
    assert manifest["incremental"] is True
    state_file = data_dir / "out" / "state.json"
    assert not state_file.exists() or json.loads(state_file.read_text()) == {}


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


def test_empty_repository_means_all_no_longer_raises(tmp_path):
    """Empty repository now means 'all in scope' — it enumerates rather than erroring."""
    params = {
        **PAT,
        "endpoint": "repository_issues",
        "repository": [],
        "organization": ["acme"],
        "load_type": "full_load",
    }
    data_dir = _write_datadir(tmp_path, params)
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        from component import Component

        with mock.patch("component.GitHubClient"):
            comp = Component()
        comp.client.get_json_array_all.return_value = []  # ty: ignore[unresolved-attribute]  # no repos in scope
        comp.client.fetch.return_value = iter([])  # ty: ignore[unresolved-attribute]
        comp.run()  # must not raise
    rows, _manifest = _read_table(data_dir, "repository_issues")
    assert rows == []


# --------------------------------------------------- since/until window -> server params


def test_workflow_runs_since_uses_created_date_range(tmp_path):
    """repository_workflow_runs sends `created=>=YYYY-MM-DD` (date-range grammar) from `since`."""
    params = {
        **PAT,
        "endpoint": "repository_workflow_runs",
        "repository": "octocat/Hello",
        "since": "2024-03-15T08:30:00Z",
    }
    payload = [{"id": 1, "name": "CI", "status": "completed"}]
    _data_dir, comp = _run_component(tmp_path, params, payload)
    assert _fetch_params(comp)["created"] == ">=2024-03-15"


def test_commits_since_passes_iso_instant(tmp_path):
    """A plain `since` endpoint injects the resolved ISO instant as the server `since` param."""
    params = {
        **PAT,
        "endpoint": "repository_commits",
        "repository": "octocat/Hello",
        "since": "2024-03-15T08:30:00Z",
    }
    payload = [{"sha": "abc"}]
    _data_dir, comp = _run_component(tmp_path, params, payload)
    assert _fetch_params(comp)["since"] == "2024-03-15T08:30:00+00:00"


# --------------------------------------------------- fan-out over many targets (§4)


def test_repo_subresource_endpoints_emit_context_columns():
    """Every repo SUB-resource endpoint must emit owner+repo so fanned multi-repo output is joinable.

    The bare ``repository`` endpoint is exempt: it returns the repo object itself, which already
    carries ``full_name`` + ``owner_login`` and is therefore self-identifying across repos.
    """
    from endpoints import ENDPOINTS, Target, output_columns

    self_identifying = {"repository"}
    for ep in ENDPOINTS.values():
        if ep.target in (Target.OWNER_REPO, Target.OWNER_REPO_PULL) and ep.name not in self_identifying:
            cols = output_columns(ep)
            assert "owner" in cols and "repo" in cols, ep.name


def _run_multi(tmp_path, params, fetch_side_effect, state=None):
    """Run a Component whose client.fetch returns a different iterator per call (one per target)."""
    data_dir = _write_datadir(tmp_path, params, state)
    env = {"KBC_DATADIR": str(data_dir), "KBC_DATA_TYPE_SUPPORT": "authoritative"}
    with mock.patch.dict(os.environ, env):
        from component import Component

        with mock.patch("component.GitHubClient"):
            comp = Component()
        comp.client.fetch.side_effect = fetch_side_effect  # ty: ignore[unresolved-attribute]
        comp.run()
    return data_dir, comp


def test_fanout_two_repos_into_one_table(tmp_path):
    params = {**PAT, "endpoint": "repository_commits", "repository": ["a/x", "a/y"], "load_type": "full_load"}
    side_effect = [iter([{"sha": "s1", "commit": {"message": "m"}}]), iter([{"sha": "s2", "commit": {"message": "m"}}])]
    data_dir, _comp = _run_multi(tmp_path, params, side_effect)
    rows, manifest = _read_table(data_dir, "repository_commits")
    header = [c["name"] for c in manifest["schema"]]
    repo_idx = header.index("repository")
    assert sorted(r[repo_idx] for r in rows) == ["a/x", "a/y"]


def test_since_applies_in_full_load_too(tmp_path):
    """`since` bounds the fetch in full_load as well (it's the window, not a first-run-only floor)."""
    params = {
        **PAT,
        "endpoint": "repository_commits",
        "repository": ["a/x"],
        "load_type": "full_load",
        "since": "2024-01-01T00:00:00Z",
    }
    _data_dir, comp = _run_component(tmp_path, params, [{"sha": "s1"}])
    assert _fetch_params(comp)["since"] == "2024-01-01T00:00:00+00:00"


def test_no_state_file_written(tmp_path):
    """The windowing model is stateless — an incremental run writes no watermark/state."""
    params = {**PAT, "endpoint": "repository_commits", "repository": ["a/x"], "load_type": "incremental_load"}
    data_dir, _comp = _run_component(tmp_path, params, [{"sha": "s1"}])
    state_file = data_dir / "out" / "state.json"
    assert not state_file.exists() or json.loads(state_file.read_text()) == {}


def test_until_drops_records_past_upper_bound(tmp_path):
    """`until` is enforced client-side: records whose time_field is after Until are dropped."""
    params = {
        **PAT,
        "endpoint": "repository_commits",
        "repository": ["a/x"],
        "since": "2024-01-01T00:00:00Z",
        "until": "2024-06-01T00:00:00Z",
    }
    payload = [
        {"sha": "in", "commit": {"author": {"date": "2024-03-01T00:00:00Z"}}},
        {"sha": "past", "commit": {"author": {"date": "2024-09-01T00:00:00Z"}}},  # after until -> dropped
    ]
    data_dir, _comp = _run_component(tmp_path, params, payload)
    rows, manifest = _read_table(data_dir, "repository_commits")
    header = [c["name"] for c in manifest["schema"]]
    sha_idx = header.index("sha")
    assert sorted(r[sha_idx] for r in rows) == ["in"]


def test_pull_reviews_nested_fanout_over_prs(tmp_path):
    params = {**PAT, "endpoint": "repository_pull_reviews", "repository": ["a/x"], "load_type": "full_load"}
    # 1st fetch = the PR listing (2 PRs); next 2 = each PR's reviews
    side_effect = [
        iter(
            [
                {"number": 11, "updated_at": "2024-01-02T00:00:00Z"},
                {"number": 12, "updated_at": "2024-01-01T00:00:00Z"},
            ]
        ),
        iter([{"id": 101, "state": "APPROVED", "user": {"login": "u", "id": 1}}]),
        iter([{"id": 102, "state": "COMMENTED", "user": {"login": "v", "id": 2}}]),
    ]
    data_dir, _comp = _run_multi(tmp_path, params, side_effect)
    rows, manifest = _read_table(data_dir, "repository_pull_reviews")
    header = [c["name"] for c in manifest["schema"]]
    assert len(rows) == 2
    assert "pull_number" in header
    pn_idx = header.index("pull_number")
    assert sorted(r[pn_idx] for r in rows) == ["11", "12"]
