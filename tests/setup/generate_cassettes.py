"""Deterministic generator for the Stage-1 (synthetic) VCR functional cassettes.

Run with: ``uv run python tests/setup/generate_cassettes.py``

Why a generator (not hand-typed cassettes): hand-authoring vcrpy ``requests.json`` files — and
the matching ``expected/data/out/`` CSV+manifest — is error-prone and drifts from what the
component actually emits. Instead we let the component generate everything:

1. Stand up a tiny local HTTP server that serves seed GitHub payloads (captured from the real
   API shapes in Phase 2: octocat/Spoon-Knife issues with a ``pull_request`` row, Link
   ``rel=next`` pagination, rate-limit headers).
2. Run the REAL component against it under plain ``vcrpy`` (record mode) with the component's own
   ``VCR_SANITIZERS`` applied — so the recorded request ``Authorization`` header is already
   ``Bearer ***``. (We use vcrpy directly rather than ``keboola.vcr.VCRRecorder.record`` because
   that recorder's zero-copy response patch is incompatible with the installed vcrpy and crashes
   mid-response; the on-disk cassette format is identical — same ``JsonIndentedSerializer`` plus an
   injected ``_metadata.freeze_time`` — so the ``keboola.datadirtest`` replay path reads it natively.)
3. Rewrite the recorded cassette host ``127.0.0.1:<port>`` -> ``api.github.com`` (https), because
   replay matches on scheme/host/port and the component calls ``api.github.com`` at replay time.
4. Snapshot the output that recording run produced into ``expected/data/out/`` as the committed
   expected output, and (for successful sync actions) the stdout result into
   ``sync_action_result.json``.

The result is a real, correctly-formatted, sanitized cassette whose expected output is exactly
what the component produces — the same artifacts a live recording would yield, minus the need
for a real token. (The breadth SUCCESS set is recorded live in Stage 2; these synthetic cases
cover failures, pagination, the PR-drop, the OAuth path, and one case per envelope shape.)
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import sys
import threading
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_FUNCTIONAL = _REPO / "tests" / "functional"
_COMPONENT = _REPO / "src" / "component.py"
sys.path.insert(0, str(_REPO / "src"))

FREEZE_TIME = "2026-06-23T12:00:00+00:00"

# ----------------------------------------------------------------------------- seed payloads
# Minimal-but-real GitHub response shapes (trimmed to the fields the registry reads).

REPO_OBJECT: dict[str, Any] = {
    "id": 1300192,
    "node_id": "MDEwOlJlcG9zaXRvcnkxMzAwMTky",
    "name": "Spoon-Knife",
    "full_name": "octocat/Spoon-Knife",
    "private": False,
    "owner": {"login": "octocat", "id": 583231, "type": "User"},
    "html_url": "https://github.com/octocat/Spoon-Knife",
    "description": "This repo is for demonstration purposes only.",
    "fork": False,
    "language": None,
    "forks_count": 150000,
    "stargazers_count": 12000,
    "watchers_count": 12000,
    "open_issues_count": 1500,
    "size": 3,
    "default_branch": "main",
    "archived": False,
    "disabled": False,
    "visibility": "public",
    "pushed_at": "2024-02-01T10:00:00Z",
    "created_at": "2011-01-27T19:30:43Z",
    "updated_at": "2024-06-01T08:00:00Z",
}

ISSUES_WITH_PR = [
    {
        "id": 100001,
        "node_id": "I_kwDO",
        "number": 42,
        "title": "A real issue",
        "state": "open",
        "locked": False,
        "comments": 3,
        "user": {"login": "alice", "id": 11},
        "assignee": {"login": "bob"},
        "milestone": {"id": 7},
        "html_url": "https://github.com/octocat/Spoon-Knife/issues/42",
        "labels": [{"name": "bug"}, {"name": "help wanted"}],
        "created_at": "2024-03-01T09:00:00Z",
        "updated_at": "2024-03-02T09:00:00Z",
        "closed_at": None,
    },
    {
        # This is a pull request masquerading as an issue — MUST be dropped (§4.4).
        "id": 100002,
        "node_id": "PR_kwDO",
        "number": 43,
        "title": "A pull request returned by /issues",
        "state": "open",
        "pull_request": {"url": "https://api.github.com/repos/octocat/Spoon-Knife/pulls/43"},
        "user": {"login": "carol", "id": 12},
        "html_url": "https://github.com/octocat/Spoon-Knife/pull/43",
        "labels": [],
        "created_at": "2024-03-03T09:00:00Z",
        "updated_at": "2024-03-03T09:00:00Z",
        "closed_at": None,
    },
]

WORKFLOWS_WRAPPED = {
    "total_count": 2,
    "workflows": [
        {
            "id": 161335,
            "node_id": "MDg6V29ya2Zsb3cxNjEzMzU=",
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "state": "active",
            "html_url": "https://github.com/octocat/Spoon-Knife/blob/main/.github/workflows/ci.yml",
            "created_at": "2020-01-08T23:48:37Z",
            "updated_at": "2020-01-08T23:50:21Z",
        },
        {
            "id": 161336,
            "node_id": "MDg6V29ya2Zsb3cxNjEzMzY=",
            "name": "Release",
            "path": ".github/workflows/release.yml",
            "state": "active",
            "html_url": "https://github.com/octocat/Spoon-Knife/blob/main/.github/workflows/release.yml",
            "created_at": "2020-02-08T23:48:37Z",
            "updated_at": "2020-02-08T23:50:21Z",
        },
    ],
}

LANGUAGES_MAP = {"Python": 12345, "Shell": 678, "Dockerfile": 90}

ORG_MEMBERS = [
    {
        "id": 583231,
        "login": "octocat",
        "node_id": "MDQ6VXNlcjU4MzIzMQ==",
        "type": "User",
        "html_url": "https://github.com/octocat",
        "site_admin": False,
    },
    {
        "id": 1024025,
        "login": "hubot",
        "node_id": "MDQ6VXNlcjEwMjQwMjU=",
        "type": "User",
        "html_url": "https://github.com/hubot",
        "site_admin": False,
    },
]

COMMITS_PAGE_1 = [
    {
        "sha": "aaa111",
        "node_id": "C_aaa",
        "commit": {
            "message": "first",
            "author": {"name": "Alice", "email": "a@x.io", "date": "2024-05-01T10:00:00Z"},
            "committer": {"name": "Alice", "date": "2024-05-01T10:00:00Z"},
        },
        "author": {"login": "alice", "id": 11},
        "committer": {"login": "alice"},
        "html_url": "https://github.com/octocat/Spoon-Knife/commit/aaa111",
    },
]
COMMITS_PAGE_2 = [
    {
        "sha": "bbb222",
        "node_id": "C_bbb",
        "commit": {
            "message": "second",
            "author": {"name": "Bob", "email": "b@x.io", "date": "2024-04-01T10:00:00Z"},
            "committer": {"name": "Bob", "date": "2024-04-01T10:00:00Z"},
        },
        "author": {"login": "bob", "id": 12},
        "committer": {"login": "bob"},
        "html_url": "https://github.com/octocat/Spoon-Knife/commit/bbb222",
    },
]

RATE_LIMIT_OK = {
    "resources": {"core": {"limit": 5000, "remaining": 4999, "reset": 9999999999}},
    "rate": {"limit": 5000, "remaining": 4999, "reset": 9999999999},
}

USER_ORGS = [{"login": "octo-org", "id": 1}, {"login": "acme", "id": 2}]
ORG_REPOS = [{"full_name": "octo-org/repo1"}, {"full_name": "octo-org/repo2"}]
ORG_TEAMS = [{"name": "Core Team", "slug": "core-team"}, {"name": "QA", "slug": "qa"}]


# ----------------------------------------------------------------------------- local server


def _make_handler(routes: list[dict[str, Any]]):
    """routes: ordered list of {match: path-prefix, status, json, headers}. First match per path wins,
    consumed once so paginated routes serve page 1 then page 2 for the same path."""
    state = {"routes": list(routes)}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            chosen = None
            for i, r in enumerate(state["routes"]):
                if path == r["match"] and not r.get("_used"):
                    chosen = r
                    state["routes"][i] = {**r, "_used": True}
                    break
            if chosen is None:
                # fall back to last matching (non-consumed) route for the path
                for r in state["routes"]:
                    if path == r["match"]:
                        chosen = r
                        break
            if chosen is None:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"message":"Not Found"}')
                return
            body = json.dumps(chosen["json"]).encode()
            self.send_response(chosen["status"])
            self.send_header("Content-Type", "application/json")
            for k, v in chosen.get("headers", {}).items():
                self.send_header(k, v.replace("%PORT%", str(self.server.server_address[1])))  # ty: ignore[not-subscriptable]
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence  # ty: ignore[invalid-method-override]
            pass

    return Handler


def _run_server(routes):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _make_handler(routes))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ----------------------------------------------------------------------------- recording


def _write_config(case_dir: Path, config: dict, state: dict | None) -> None:
    data = case_dir / "source" / "data"
    (data / "in" / "tables").mkdir(parents=True, exist_ok=True)
    (data / "out" / "tables").mkdir(parents=True, exist_ok=True)
    (data / "cassettes").mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps(config, indent=2))
    if state is not None:
        (data / "in" / "state.json").write_text(json.dumps(state, indent=2))


def _sanitized_before_record_request(request):
    """Apply the component's own VCR_SANITIZERS to a vcrpy request during recording."""
    import component as component_module

    for sanitizer in component_module.VCR_SANITIZERS:
        request = sanitizer.before_record_request(request)
    return request


def _sanitized_before_record_response(response):
    import component as component_module

    for sanitizer in component_module.VCR_SANITIZERS:
        response = sanitizer.before_record_response(response)
    return response


def _record_case(
    case_dir: Path,
    config: dict,
    routes: list[dict],
    state: dict | None,
    base_url_port: int,
    *,
    has_output: bool,
    sync_action: bool = False,
) -> None:
    """Record a sanitized cassette with plain vcrpy, then snapshot expected output via replay.

    We record with vcrpy directly (not ``VCRRecorder.record``) because that recorder's zero-copy
    response patch is incompatible with the installed vcrpy and crashes mid-response. Plain vcrpy
    records the same on-disk format (we reuse the recorder's ``JsonIndentedSerializer`` and add the
    ``_metadata.freeze_time`` the replay path reads), with the component's VCR_SANITIZERS applied.

    For a successful sync action we also capture its stdout JSON into ``sync_action_result.json`` —
    the tester compares the replayed sync-action return value against it, so the ``[{label,value}]``
    shape is actually asserted (not just exit code 0).
    """
    import contextlib
    import io

    import vcr
    from keboola.vcr.recorder import JsonIndentedSerializer

    import component as component_module

    _write_config(case_dir, config, state)
    data_dir = case_dir / "source" / "data"
    cassette_dir = data_dir / "cassettes"
    cassette_dir.mkdir(parents=True, exist_ok=True)
    cassette_path = cassette_dir / "requests.json"

    # Point the client at the local server only for this recording run.
    orig_base = component_module.GitHubClient.__init__

    def patched_init(self, token, base_url=f"http://127.0.0.1:{base_url_port}", **kw):
        orig_base(self, token, base_url=base_url, **kw)

    component_module.GitHubClient.__init__ = patched_init  # ty: ignore[invalid-assignment]

    my_vcr = vcr.VCR(
        serializer="json",
        cassette_library_dir=str(cassette_dir),
        record_mode="all",
        match_on=["method", "scheme", "host", "port", "path", "query"],
        before_record_request=_sanitized_before_record_request,
        before_record_response=_sanitized_before_record_response,
    )
    my_vcr.register_serializer("json", JsonIndentedSerializer())

    os.environ["KBC_DATADIR"] = str(data_dir)
    os.environ["KBC_DATA_TYPE_SUPPORT"] = "authoritative"
    stdout_buf = io.StringIO()
    exited = False
    try:
        with my_vcr.use_cassette("requests.json"):
            from runpy import run_path

            try:
                with contextlib.redirect_stdout(stdout_buf):
                    run_path(str(_COMPONENT), run_name="__main__")
            except SystemExit:
                # Failure cases intentionally exit non-zero; the interaction is still recorded.
                exited = True
    finally:
        component_module.GitHubClient.__init__ = orig_base  # ty: ignore[invalid-assignment]

    if sync_action and not exited:
        result = stdout_buf.getvalue().strip()
        if result:
            (cassette_dir / "sync_action_result.json").write_text(result)

    _rewrite_cassette_host(cassette_path, base_url_port)
    _inject_metadata(cassette_path)

    # Snapshot the output this recording produced as the committed expected output.
    produced = data_dir / "out"
    expected = case_dir / "expected" / "data" / "out"
    if expected.exists():
        shutil.rmtree(expected)
    expected.mkdir(parents=True)
    if has_output and (produced / "tables").exists() and any((produced / "tables").iterdir()):
        shutil.copytree(produced / "tables", expected / "tables")
    else:
        (expected / "tables").mkdir(parents=True, exist_ok=True)


def _inject_metadata(cassette_path: Path) -> None:
    """Add ``_metadata.freeze_time`` so the replay path resolves a deterministic frozen clock."""
    if not cassette_path.exists():
        return
    data = json.loads(cassette_path.read_text())
    data["_metadata"] = {"freeze_time": FREEZE_TIME, "recorded_at": FREEZE_TIME}
    cassette_path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _rewrite_cassette_host(cassette_path: Path, port: int) -> None:
    """Replace the local recording host with the real GitHub host so replay (which calls
    api.github.com) matches on scheme/host/port."""
    if not cassette_path.exists():
        return
    text = cassette_path.read_text()
    text = text.replace(f"http://127.0.0.1:{port}", "https://api.github.com")
    cassette_path.write_text(text)


def _write_expected_status(case_dir: Path, exit_code: int) -> None:
    (case_dir / "source" / "data" / "cassettes" / "expected_status.json").write_text(
        json.dumps({"exit_code": exit_code})
    )


def _load_configs() -> dict[str, dict]:
    raw = json.loads((_HERE / "configs.json").read_text())
    return {c["name"]: c for c in raw}


# ----------------------------------------------------------------------------- case builders


def main() -> None:
    configs = _load_configs()

    def cfg(name: str) -> dict:
        return configs[name]["config"]

    # --- Success / single-page run cases -----------------------------------------------------
    single_page = {
        "01_run_repository": ([{"match": "/repos/octocat/Spoon-Knife", "status": 200, "json": REPO_OBJECT}], None),
        "02_run_repository_issues_drops_pr": (
            [{"match": "/repos/octocat/Spoon-Knife/issues", "status": 200, "json": ISSUES_WITH_PR}],
            None,
        ),
        "03_run_repository_workflows": (
            [{"match": "/repos/octocat/Spoon-Knife/actions/workflows", "status": 200, "json": WORKFLOWS_WRAPPED}],
            None,
        ),
        "04_run_repository_languages": (
            [{"match": "/repos/octocat/Spoon-Knife/languages", "status": 200, "json": LANGUAGES_MAP}],
            None,
        ),
        "05_run_organization_members": (
            [{"match": "/orgs/octo-org/members", "status": 200, "json": ORG_MEMBERS}],
            None,
        ),
        "07_run_issues_incremental_firstrun": (
            [{"match": "/repos/octocat/Spoon-Knife/issues", "status": 200, "json": ISSUES_WITH_PR}],
            None,
        ),
        "09_run_oauth_path": ([{"match": "/repos/octocat/Spoon-Knife", "status": 200, "json": REPO_OBJECT}], None),
    }
    for name, (routes, state) in single_page.items():
        _build(name, cfg(name), routes, state, has_output=True)

    # Incremental seeded: in/state.json carries the prior watermark -> `since` applied.
    _build(
        "06_run_issues_incremental_seeded",
        cfg("06_run_issues_incremental_seeded"),
        [{"match": "/repos/octocat/Spoon-Knife/issues", "status": 200, "json": ISSUES_WITH_PR}],
        state={"last_run": "2024-02-01T00:00:00+00:00"},
        has_output=True,
    )

    # Pagination: page 1 with Link rel=next -> page 2 (no Link) -> stop.
    _build(
        "08_run_pagination_two_pages",
        cfg("08_run_pagination_two_pages"),
        [
            {
                "match": "/repos/octocat/Spoon-Knife/commits",
                "status": 200,
                "json": COMMITS_PAGE_1,
                "headers": {"Link": '<http://127.0.0.1:%PORT%/repos/octocat/Spoon-Knife/commits?page=2>; rel="next"'},
            },
            {"match": "/repos/octocat/Spoon-Knife/commits", "status": 200, "json": COMMITS_PAGE_2},
        ],
        state=None,
        has_output=True,
    )

    # --- Failure cases ----------------------------------------------------------------------
    # Missing target: no HTTP at all — UserException before any request -> exit 1.
    _build(
        "20_run_missing_target_fails",
        cfg("20_run_missing_target_fails"),
        routes=[],
        state=None,
        has_output=False,
        expected_exit=1,
    )

    # 401 unauthorized -> exit 1.
    _build(
        "21_run_unauthorized_fails",
        cfg("21_run_unauthorized_fails"),
        [{"match": "/repos/octocat/Spoon-Knife/issues", "status": 401, "json": {"message": "Bad credentials"}}],
        state=None,
        has_output=False,
        expected_exit=1,
    )

    # Rate-limit budget exceeded: 403 + remaining:0 + far-future reset -> exit 1.
    _build(
        "22_run_rate_limit_budget_exceeded_fails",
        cfg("22_run_rate_limit_budget_exceeded_fails"),
        [
            {
                "match": "/repos/octocat/Spoon-Knife/issues",
                "status": 403,
                "json": {"message": "API rate limit exceeded"},
                "headers": {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
            }
        ],
        state=None,
        has_output=False,
        expected_exit=1,
    )

    # --- Sync action cases ------------------------------------------------------------------
    _build(
        "30_testConnection",
        cfg("30_testConnection"),
        [{"match": "/rate_limit", "status": 200, "json": RATE_LIMIT_OK}],
        state=None,
        has_output=False,
        sync_action=True,
    )
    _build(
        "31_testConnection_unauthorized_fails",
        cfg("31_testConnection_unauthorized_fails"),
        [{"match": "/rate_limit", "status": 401, "json": {"message": "Bad credentials"}}],
        state=None,
        has_output=False,
        sync_action=True,
        expected_exit=1,
    )
    _build(
        "32_listOrganizations",
        cfg("32_listOrganizations"),
        [{"match": "/user/orgs", "status": 200, "json": USER_ORGS}],
        state=None,
        has_output=False,
        sync_action=True,
    )
    _build(
        "33_listRepositories",
        cfg("33_listRepositories"),
        [{"match": "/orgs/octo-org/repos", "status": 200, "json": ORG_REPOS}],
        state=None,
        has_output=False,
        sync_action=True,
    )
    _build(
        "34_listTeams",
        cfg("34_listTeams"),
        [{"match": "/orgs/octo-org/teams", "status": 200, "json": ORG_TEAMS}],
        state=None,
        has_output=False,
        sync_action=True,
    )

    print("Done. Generated cassettes under tests/functional/")


def _build(
    name: str,
    config: dict,
    routes: list[dict],
    state: dict | None,
    *,
    has_output: bool,
    expected_exit: int = 0,
    sync_action: bool = False,
) -> None:
    case_dir = _FUNCTIONAL / name
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)

    if routes:
        httpd, port = _run_server(routes)
        try:
            _record_case(case_dir, config, routes, state, port, has_output=has_output, sync_action=sync_action)
        finally:
            httpd.shutdown()
    else:
        # No HTTP expected (e.g. missing-target failure). Still need an (empty) cassette so the
        # tester runs in replay mode rather than hitting the network.
        _write_config(case_dir, config, state)
        _write_empty_cassette(case_dir)
        (case_dir / "expected" / "data" / "out" / "tables").mkdir(parents=True, exist_ok=True)

    cassettes = case_dir / "source" / "data" / "cassettes"
    # Drop recorded logs.json — committed log snapshots make replay brittle (timestamps/paths);
    # the contract we assert is exit code + output diff + sync-action result, all deterministic.
    (cassettes / "logs.json").unlink(missing_ok=True)
    # Control the expected exit code explicitly (the recorder auto-writes it on non-zero exit;
    # normalize it here so it is present exactly when we declared a failure).
    (cassettes / "expected_status.json").unlink(missing_ok=True)
    if expected_exit != 0:
        _write_expected_status(case_dir, expected_exit)
    # Clean the working out/ produced during recording (gitignored, but keep the tree tidy).
    out_dir = case_dir / "source" / "data" / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)

    print(f"  built {name}")


def _write_empty_cassette(case_dir: Path) -> None:
    cassette = case_dir / "source" / "data" / "cassettes" / "requests.json"
    cassette.parent.mkdir(parents=True, exist_ok=True)
    cassette.write_text(
        json.dumps(
            {
                "_metadata": {"freeze_time": FREEZE_TIME, "recorded_at": FREEZE_TIME},
                "interactions": [],
                "version": 1,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
