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
for a real token.

Coverage produced here:
* the special cases — failures (missing target / 401 / rate-limit), pagination, the /issues
  PR-drop, incremental (seeded + first-run), the OAuth path, and the four sync actions;
* a ``coverage_<NN>_<endpoint>`` success case for ALL 33 registry endpoints (``ENDPOINT_COVERAGE``),
  against PUBLIC targets with small, public-safe shapes.

``coverage_manifest.json`` records which coverage cases are live-recordable against public data
with a read-scoped PAT vs which need org-membership/private access and stay synthetic. When a
valid PAT is available, the live-recordable subset can be re-recorded for real; the synthetic-only
subset (teams, team members, org events, outside/repo collaborators, deployments, webhooks) stays
authored from the documented GitHub schemas so no private data is ever committed.
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


# =====================================================================================
# Full per-endpoint coverage table (all 33 endpoints).
#
# Each entry drives one `coverage_<name>` run case: the row's target params + a minimal
# public-safe seed payload shaped per the endpoint's documented GitHub response. Public
# targets only (octocat/Spoon-Knife, the public `github` org); a few rows trimmed small so
# committed cassettes stay tiny and carry no private/proprietary data.
#
# `recordable` flags whether the endpoint can be recorded LIVE against public data with a
# read-scoped PAT (True) or needs org-membership / private access and therefore stays
# SYNTHETIC even with a token (False — e.g. team/collaborator/webhook/deployment/private-event
# endpoints). Stage 2 re-records only the `recordable` ones live; the report lists which.
# =====================================================================================

_PUBLIC_REPO = "octocat/Spoon-Knife"
_PUBLIC_ORG = "github"

_SIMPLE_USER = {
    "id": 583231,
    "login": "octocat",
    "node_id": "MDQ6VXNlcjU4MzIzMQ==",
    "type": "User",
    "html_url": "https://github.com/octocat",
    "site_admin": False,
}


def _row(endpoint: str, **params) -> dict:
    """A minimal merged-config `parameters` block for one endpoint coverage case (full load)."""
    base = {"auth_type": "pat", "#github_token": "DUMMY_PAT_NOT_REAL", "endpoint": endpoint, "load_type": "full_load"}
    base.update(params)
    return base


# endpoint -> (row params, seed response payload, recordable-live-against-public-data)
ENDPOINT_COVERAGE: dict[str, tuple[dict, Any, bool]] = {
    # A. Authenticated user --------------------------------------------------------------
    "user": (
        _row("user"),
        {
            "id": 583231,
            "login": "octocat",
            "node_id": "MDQ6VXNlcjU4MzIzMQ==",
            "type": "User",
            "name": "The Octocat",
            "company": "@github",
            "email": None,
            "html_url": "https://github.com/octocat",
            "public_repos": 8,
            "followers": 10000,
            "following": 9,
            "created_at": "2011-01-25T18:44:36Z",
            "updated_at": "2024-01-01T00:00:00Z",
        },
        True,
    ),
    "user_repos": (
        _row("user_repos"),
        [
            {
                "id": 1300192,
                "name": "Spoon-Knife",
                "full_name": _PUBLIC_REPO,
                "private": False,
                "owner": {"login": "octocat", "id": 583231},
                "html_url": "https://github.com/octocat/Spoon-Knife",
                "fork": False,
                "stargazers_count": 12000,
                "default_branch": "main",
                "archived": False,
                "disabled": False,
                "visibility": "public",
                "created_at": "2011-01-27T19:30:43Z",
                "updated_at": "2024-06-01T08:00:00Z",
                "pushed_at": "2024-02-01T10:00:00Z",
            }
        ],
        True,
    ),
    "user_orgs": (
        _row("user_orgs"),
        [
            {
                "id": 9919,
                "login": "github",
                "node_id": "MDEyOk9yZ2FuaXphdGlvbjk5MTk=",
                "url": "https://api.github.com/orgs/github",
                "description": "How people build software.",
            }
        ],
        True,
    ),
    # B. Organizations -------------------------------------------------------------------
    "organizations": (
        _row("organizations", organization=_PUBLIC_ORG),
        {
            "id": 9919,
            "login": "github",
            "node_id": "MDEyOk9yZ2FuaXphdGlvbjk5MTk=",
            "name": "GitHub",
            "company": None,
            "description": "How people build software.",
            "html_url": "https://github.com/github",
            "type": "Organization",
            "public_repos": 500,
            "followers": 40000,
            "following": 0,
            "is_verified": True,
            "created_at": "2008-05-11T04:37:31Z",
            "updated_at": "2024-01-01T00:00:00Z",
        },
        True,
    ),
    "organization_members": (
        _row("organization_members", organization=_PUBLIC_ORG),
        [_SIMPLE_USER],
        True,
    ),  # public members are visible; live-recordable
    "organization_teams": (
        _row("organization_teams", organization=_PUBLIC_ORG),
        [
            {
                "id": 1,
                "node_id": "MDQ6VGVhbTE=",
                "name": "Justice League",
                "slug": "justice-league",
                "description": "A great team",
                "privacy": "closed",
                "permission": "admin",
                "html_url": "https://github.com/orgs/github/teams/justice-league",
                "parent": None,
            }
        ],
        False,
    ),  # listing teams requires org membership — synthetic
    "team_members": (
        _row("team_members", organization=_PUBLIC_ORG, team_slug="justice-league"),
        [_SIMPLE_USER],
        False,
    ),  # requires team/org membership — synthetic
    "organization_repositories": (
        _row("organization_repositories", organization=_PUBLIC_ORG),
        [
            {
                "id": 1300192,
                "name": "Spoon-Knife",
                "full_name": _PUBLIC_REPO,
                "private": False,
                "owner": {"login": "github", "id": 9919},
                "html_url": "https://github.com/octocat/Spoon-Knife",
                "fork": False,
                "stargazers_count": 12000,
                "default_branch": "main",
                "archived": False,
                "disabled": False,
                "visibility": "public",
                "created_at": "2011-01-27T19:30:43Z",
                "updated_at": "2024-06-01T08:00:00Z",
                "pushed_at": "2024-02-01T10:00:00Z",
            }
        ],
        True,
    ),
    "organization_events": (
        _row("organization_events", organization=_PUBLIC_ORG),
        [
            {
                "id": "22249084964",
                "type": "PushEvent",
                "actor": {"login": "octocat", "id": 583231},
                "repo": {"name": "github/Spoon-Knife", "id": 1300192},
                "public": True,
                "created_at": "2024-05-01T10:00:00Z",
                "payload": {"size": 1, "ref": "refs/heads/main"},
            }
        ],
        False,
    ),  # org events need org access — synthetic
    "organization_outside_collaborators": (
        _row("organization_outside_collaborators", organization=_PUBLIC_ORG),
        [_SIMPLE_USER],
        False,
    ),  # admin-only — synthetic
    # C. Repositories --------------------------------------------------------------------
    "repository": (_row("repository", repository=_PUBLIC_REPO), REPO_OBJECT, True),
    "repository_branches": (
        _row("repository_branches", repository=_PUBLIC_REPO),
        [
            {
                "name": "main",
                "commit": {
                    "sha": "d0dd1f61b33d64e29d8bc1372a94ef6a2fee76a9",
                    "url": "https://api.github.com/repos/octocat/Spoon-Knife/commits/d0dd1f6",
                },
                "protected": False,
            }
        ],
        True,
    ),
    "repository_tags": (
        _row("repository_tags", repository=_PUBLIC_REPO),
        [
            {
                "name": "v1.0",
                "commit": {"sha": "aaa111"},
                "zipball_url": "https://api.github.com/repos/octocat/Spoon-Knife/zipball/refs/tags/v1.0",
                "tarball_url": "https://api.github.com/repos/octocat/Spoon-Knife/tarball/refs/tags/v1.0",
            }
        ],
        True,
    ),
    "repository_commits": (_row("repository_commits", repository=_PUBLIC_REPO), COMMITS_PAGE_1, True),
    "repository_contributors": (
        _row("repository_contributors", repository=_PUBLIC_REPO),
        [
            {
                "id": 583231,
                "login": "octocat",
                "type": "User",
                "html_url": "https://github.com/octocat",
                "contributions": 42,
            }
        ],
        True,
    ),
    "repository_collaborators": (
        _row("repository_collaborators", repository=_PUBLIC_REPO),
        [
            {
                "id": 583231,
                "login": "octocat",
                "type": "User",
                "html_url": "https://github.com/octocat",
                "role_name": "admin",
                "site_admin": False,
            }
        ],
        False,
    ),  # push access required — synthetic
    "repository_languages": (_row("repository_languages", repository=_PUBLIC_REPO), LANGUAGES_MAP, True),
    "repository_topics": (
        _row("repository_topics", repository=_PUBLIC_REPO),
        {"names": ["demo", "example", "octocat"]},
        True,
    ),
    "repository_releases": (
        _row("repository_releases", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "node_id": "MDc6UmVsZWFzZTE=",
                "tag_name": "v1.0.0",
                "name": "v1.0.0",
                "draft": False,
                "prerelease": False,
                "author": {"login": "octocat", "id": 583231},
                "html_url": "https://github.com/octocat/Spoon-Knife/releases/tag/v1.0.0",
                "created_at": "2013-02-27T19:35:32Z",
                "published_at": "2013-02-27T19:35:32Z",
            }
        ],
        True,
    ),
    "repository_issues": (_row("repository_issues", repository=_PUBLIC_REPO), ISSUES_WITH_PR, True),
    "repository_issue_comments": (
        _row("repository_issue_comments", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "node_id": "IC_1",
                "body": "Great issue!",
                "user": {"login": "alice", "id": 11},
                "html_url": "https://github.com/octocat/Spoon-Knife/issues/1#issuecomment-1",
                "issue_url": "https://api.github.com/repos/octocat/Spoon-Knife/issues/1",
                "created_at": "2024-03-01T09:00:00Z",
                "updated_at": "2024-03-01T09:00:00Z",
            }
        ],
        True,
    ),
    "repository_pulls": (
        _row("repository_pulls", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "node_id": "PR_1",
                "number": 1,
                "title": "Update README",
                "state": "open",
                "locked": False,
                "draft": False,
                "user": {"login": "alice", "id": 11},
                "head": {"ref": "patch-1", "sha": "ccc333"},
                "base": {"ref": "main"},
                "merge_commit_sha": None,
                "html_url": "https://github.com/octocat/Spoon-Knife/pull/1",
                "created_at": "2024-03-01T09:00:00Z",
                "updated_at": "2024-03-02T09:00:00Z",
                "closed_at": None,
                "merged_at": None,
            }
        ],
        True,
    ),
    "repository_pull_reviews": (
        _row("repository_pull_reviews", repository=_PUBLIC_REPO, pull_number=1),
        [
            {
                "id": 1,
                "node_id": "PRR_1",
                "user": {"login": "bob", "id": 12},
                "body": "LGTM",
                "state": "APPROVED",
                "commit_id": "ccc333",
                "html_url": "https://github.com/octocat/Spoon-Knife/pull/1#pullrequestreview-1",
                "submitted_at": "2024-03-02T10:00:00Z",
            }
        ],
        True,
    ),
    "repository_pull_review_comments": (
        _row("repository_pull_review_comments", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "node_id": "PRRC_1",
                "pull_request_review_id": 1,
                "body": "nit",
                "user": {"login": "bob", "id": 12},
                "path": "README.md",
                "commit_id": "ccc333",
                "html_url": "https://github.com/octocat/Spoon-Knife/pull/1#discussion_r1",
                "created_at": "2024-03-02T10:00:00Z",
                "updated_at": "2024-03-02T10:00:00Z",
            }
        ],
        True,
    ),
    "repository_labels": (
        _row("repository_labels", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "node_id": "LA_1",
                "name": "bug",
                "color": "d73a4a",
                "default": True,
                "description": "Something isn't working",
            }
        ],
        True,
    ),
    "repository_milestones": (
        _row("repository_milestones", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "node_id": "MI_1",
                "number": 1,
                "title": "v1.0",
                "description": "First release",
                "state": "open",
                "open_issues": 4,
                "closed_issues": 8,
                "creator": {"login": "octocat"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-02-01T00:00:00Z",
                "due_on": "2024-12-31T00:00:00Z",
                "closed_at": None,
            }
        ],
        True,
    ),
    "repository_stargazers": (
        _row("repository_stargazers", repository=_PUBLIC_REPO),
        [{"id": 583231, "login": "octocat", "type": "User", "html_url": "https://github.com/octocat"}],
        True,
    ),
    "repository_subscribers": (
        _row("repository_subscribers", repository=_PUBLIC_REPO),
        [{"id": 583231, "login": "octocat", "type": "User", "html_url": "https://github.com/octocat"}],
        True,
    ),
    "repository_forks": (
        _row("repository_forks", repository=_PUBLIC_REPO),
        [
            {
                "id": 2,
                "name": "Spoon-Knife",
                "full_name": "alice/Spoon-Knife",
                "owner": {"login": "alice", "id": 11},
                "html_url": "https://github.com/alice/Spoon-Knife",
                "forks_count": 0,
                "stargazers_count": 0,
                "created_at": "2024-03-01T00:00:00Z",
                "updated_at": "2024-03-01T00:00:00Z",
                "pushed_at": "2024-03-01T00:00:00Z",
            }
        ],
        True,
    ),
    "repository_workflows": (_row("repository_workflows", repository=_PUBLIC_REPO), WORKFLOWS_WRAPPED, True),
    "repository_workflow_runs": (
        _row("repository_workflow_runs", repository=_PUBLIC_REPO),
        {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 30433642,
                    "node_id": "WFR_1",
                    "name": "CI",
                    "head_branch": "main",
                    "head_sha": "aaa111",
                    "run_number": 562,
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "workflow_id": 161335,
                    "html_url": "https://github.com/octocat/Spoon-Knife/actions/runs/30433642",
                    "created_at": "2024-05-01T10:00:00Z",
                    "updated_at": "2024-05-01T10:05:00Z",
                }
            ],
        },
        True,
    ),
    "repository_deployments": (
        _row("repository_deployments", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "node_id": "DE_1",
                "sha": "aaa111",
                "ref": "main",
                "task": "deploy",
                "environment": "production",
                "description": None,
                "creator": {"login": "octocat"},
                "created_at": "2024-05-01T10:00:00Z",
                "updated_at": "2024-05-01T10:00:00Z",
            }
        ],
        False,
    ),  # deployments commonly need repo access — synthetic
    "repository_webhooks": (
        _row("repository_webhooks", repository=_PUBLIC_REPO),
        [
            {
                "id": 1,
                "name": "web",
                "active": True,
                "events": ["push", "pull_request"],
                "config": {"url": "https://example.com/hook", "content_type": "json"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        ],
        False,
    ),  # admin-only — synthetic
}


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


def _endpoint_path(endpoint_name: str, params: dict) -> str:
    """Build the concrete request path the component will hit for this endpoint + row params.

    Mirrors Component._build_path: substitute org/team_slug/owner/repo/pull_number into the
    registry path template (owner/repo come from splitting the ``repository`` "owner/repo" value).
    """
    import endpoints as ep_mod

    ep = ep_mod.ENDPOINTS[endpoint_name]
    repo = params.get("repository", "")
    owner, repo_name = repo.split("/", 1) if "/" in repo else ("", "")
    return ep.path_template.format(
        org=params.get("organization", ""),
        team_slug=params.get("team_slug", ""),
        owner=owner,
        repo=repo_name,
        pull_number=params.get("pull_number", ""),
    )


def _coverage_cases() -> list[tuple[str, dict, list[dict], bool]]:
    """Build (case_name, config, routes, recordable) for all 33 endpoints from ENDPOINT_COVERAGE."""
    import endpoints as ep_mod

    cases = []
    for i, name in enumerate(ep_mod.ENDPOINTS, start=1):
        params, payload, recordable = ENDPOINT_COVERAGE[name]
        path = _endpoint_path(name, params)
        config = {"action": "run", "parameters": params}
        routes = [{"match": path, "status": 200, "json": payload}]
        cases.append((f"coverage_{i:02d}_{name}", config, routes, recordable))
    return cases


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

    # --- Full per-endpoint coverage (all 33 endpoints) --------------------------------------
    recordable_map: dict[str, bool] = {}
    for case_name, config, routes, recordable in _coverage_cases():
        _build(case_name, config, routes, state=None, has_output=True)
        recordable_map[case_name] = recordable
    _write_coverage_manifest(recordable_map)

    print("Done. Generated cassettes under tests/functional/")


def _write_coverage_manifest(recordable_map: dict[str, bool]) -> None:
    """Record which coverage cases are live-recordable-against-public-data vs synthetic-only.

    Read by the report (and by a future Stage-2 live re-record) to know which cassettes a valid
    public-scoped PAT can replace with real recordings, and which must stay synthetic."""
    manifest = {
        "live_recordable": sorted(k for k, v in recordable_map.items() if v),
        "synthetic_only": sorted(k for k, v in recordable_map.items() if not v),
    }
    (_HERE / "coverage_manifest.json").write_text(json.dumps(manifest, indent=2))


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
