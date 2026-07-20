"""Unit tests for the auth-agnostic GitHub client (mocked HTTP via ``responses``)."""

import logging
import time
from datetime import UTC, datetime

import pytest
import requests
import responses
from keboola.component.exceptions import UserException

from client import GitHubClient
from endpoints import ENDPOINTS


@pytest.fixture
def client():
    # sleep is a no-op so rate-limit/backoff waits don't actually block the test.
    return GitHubClient(token="ghp_test", sleep=lambda _s: None)


@responses.activate
def test_array_two_page_link_traversal_stops_when_next_absent(client):
    """fetch follows Link rel=next across two pages and stops when no next link is present."""
    page2 = "https://api.github.com/repos/o/r/issues?page=2"
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        json=[{"id": 1}, {"id": 2}],
        status=200,
        headers={"Link": f'<{page2}>; rel="next"'},
        match=[responses.matchers.query_param_matcher({"per_page": "100"})],
    )
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        json=[{"id": 3}],
        status=200,  # no Link header -> explicit stop
        match=[responses.matchers.query_param_matcher({"page": "2"})],
    )

    ep = ENDPOINTS["repository_issues"]
    out = list(client.fetch(ep, "/repos/o/r/issues", {"per_page": "100"}))
    assert [r["id"] for r in out] == [1, 2, 3]
    # exactly two requests were made (stopped after page 2 had no next link)
    assert len(responses.calls) == 2


@responses.activate
def test_get_json_array_all_follows_link_pagination(client):
    """Enumeration must concatenate ALL pages (an org with >100 repos must not lose targets)."""
    page2 = "https://api.github.com/orgs/acme/repos?page=2"
    responses.add(
        responses.GET,
        "https://api.github.com/orgs/acme/repos",
        json=[{"full_name": "acme/a"}],
        status=200,
        headers={"Link": f'<{page2}>; rel="next"'},
        match=[responses.matchers.query_param_matcher({"per_page": "100"})],
    )
    responses.add(
        responses.GET,
        "https://api.github.com/orgs/acme/repos",
        json=[{"full_name": "acme/b"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"page": "2"})],
    )
    result = client.get_json_array_all("/orgs/acme/repos", params={"per_page": "100"})
    assert [r["full_name"] for r in result] == ["acme/a", "acme/b"]
    assert len(responses.calls) == 2


@responses.activate
def test_get_json_array_single_page_does_not_paginate(client):
    """The single-page get_json_array variant returns just the first page."""
    responses.add(
        responses.GET,
        "https://api.github.com/x",
        json=[{"id": 1}],
        status=200,
        headers={"Link": '<https://api.github.com/x?page=2>; rel="next"'},
    )
    assert client.get_json_array("/x") == [{"id": 1}]
    assert len(responses.calls) == 1


@responses.activate
def test_wrapped_envelope_yields_inner_array_not_total_count(client):
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/actions/workflows",
        json={"total_count": 2, "workflows": [{"id": 10}, {"id": 11}]},
        status=200,
    )
    ep = ENDPOINTS["repository_workflows"]
    out = list(client.fetch(ep, "/repos/o/r/actions/workflows", {"per_page": "100"}))
    assert [r["id"] for r in out] == [10, 11]


@responses.activate
def test_object_map_payload_yielded_once(client):
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/languages",
        json={"Python": 100, "Go": 50},
        status=200,
    )
    ep = ENDPOINTS["repository_languages"]
    out = list(client.fetch(ep, "/repos/o/r/languages"))
    assert out == [{"Python": 100, "Go": 50}]
    assert len(responses.calls) == 1  # never paginates


@responses.activate
def test_stop_paging_predicate_prevents_next_page(client):
    page2 = "https://api.github.com/repos/o/r/pulls?page=2"
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/pulls",
        json=[{"id": 1, "updated_at": "2024-05-01T00:00:00Z"}],
        status=200,
        headers={"Link": f'<{page2}>; rel="next"'},
    )
    ep = ENDPOINTS["repository_pulls"]
    # stop after the first page regardless of next link
    out = list(client.fetch(ep, "/repos/o/r/pulls", {"per_page": "100"}, stop_paging=lambda _r: True))
    assert [r["id"] for r in out] == [1]
    assert len(responses.calls) == 1  # second page never requested


@responses.activate
def test_rate_limit_exhausts_budget_raises_userexception():
    """403 + x-ratelimit-remaining:0 + near-future reset, with a tiny budget -> UserException."""
    reset = str(int(time.time()) + 10)
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        json={"message": "API rate limit exceeded"},
        status=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
    )
    # budget of 1s < the ~15s wait needed -> raises rather than crashing
    c = GitHubClient(token="ghp_test", max_rate_limit_wait_seconds=1, sleep=lambda _s: None)
    ep = ENDPOINTS["repository_issues"]
    with pytest.raises(UserException, match="rate limit not recovered within the wait budget"):
        list(c.fetch(ep, "/repos/o/r/issues", {"per_page": "100"}))


@responses.activate
def test_rate_limit_recovers_within_budget(client):
    """First call 403 rate-limited, then succeeds — fetch transparently retries."""
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        json={"message": "secondary rate limit"},
        status=403,
        headers={"Retry-After": "1"},
    )
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        json=[{"id": 99}],
        status=200,
    )
    ep = ENDPOINTS["repository_issues"]
    out = list(client.fetch(ep, "/repos/o/r/issues", {"per_page": "100"}))
    assert [r["id"] for r in out] == [99]


@responses.activate
def test_401_raises_userexception(client):
    responses.add(
        responses.GET,
        "https://api.github.com/user",
        json={"message": "Bad credentials"},
        status=401,
    )
    ep = ENDPOINTS["user"]
    with pytest.raises(UserException, match="HTTP 401"):
        list(client.fetch(ep, "/user"))


@responses.activate
def test_404_on_target_raises_userexception(client):
    responses.add(
        responses.GET,
        "https://api.github.com/orgs/nope",
        json={"message": "Not Found"},
        status=404,
    )
    ep = ENDPOINTS["organizations"]
    with pytest.raises(UserException, match="HTTP 404"):
        list(client.fetch(ep, "/orgs/nope"))


@responses.activate
def test_non_rate_limit_403_is_terminal(client):
    """A 403 with quota remaining (e.g. insufficient scope / SSO) is terminal, not retried."""
    responses.add(
        responses.GET,
        "https://api.github.com/orgs/acme/members",
        json={"message": "Must have admin rights"},
        status=403,
        headers={"x-ratelimit-remaining": "4999"},
    )
    ep = ENDPOINTS["organization_members"]
    with pytest.raises(UserException, match="HTTP 403"):
        list(client.fetch(ep, "/orgs/acme/members", {"per_page": "100"}))
    assert len(responses.calls) == 1  # not retried


@responses.activate
def test_5xx_then_200_returns_records_after_retry(client):
    """A transient 500 is retried; the subsequent 200 is returned (exercises the status>=500 branch)."""
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        json={"message": "server error"},
        status=500,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        json=[{"id": 7}],
        status=200,
    )
    ep = ENDPOINTS["repository_issues"]
    out = list(client.fetch(ep, "/repos/o/r/issues", {"per_page": "100"}))
    assert [r["id"] for r in out] == [7]
    assert len(responses.calls) == 2  # one failed attempt, one success


@responses.activate
def test_repeated_connection_error_exhausts_budget_raises_userexception():
    """Repeated network errors give up after max_retries with a clean UserException (network branch)."""
    responses.add(
        responses.GET,
        "https://api.github.com/repos/o/r/issues",
        body=requests.exceptions.ConnectionError("connection reset by peer"),
    )
    # tiny retry budget + no-op sleep -> never blocks, gives up quickly on the network _RetryableError
    c = GitHubClient(token="ghp_test", max_retries=2, sleep=lambda _s: None)
    ep = ENDPOINTS["repository_issues"]
    with pytest.raises(UserException, match="GitHub request failed after"):
        list(c.fetch(ep, "/repos/o/r/issues", {"per_page": "100"}))
    # initial attempt + max_retries retries, all ConnectionError
    assert len(responses.calls) == 3


def test_session_headers_set():
    c = GitHubClient(token="ghp_abc")
    assert c.session.headers["Authorization"] == "Bearer ghp_abc"
    assert c.session.headers["Accept"] == "application/vnd.github+json"
    assert c.session.headers["X-GitHub-Api-Version"] == "2022-11-28"


@responses.activate
def test_graphql_single_page_detects_rows_path(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={
            "data": {
                "organization": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"nameWithOwner": "o/a"}, {"nameWithOwner": "o/b"}],
                    }
                }
            }
        },
        status=200,
    )
    rows, resolved = client.graphql("query($cursor:String){ x }", {"org": "o"})
    assert resolved == "organization.repositories.nodes"
    assert [r["nameWithOwner"] for r in rows] == ["o/a", "o/b"]
    assert len(responses.calls) == 1


@responses.activate
def test_graphql_errors_with_null_data_raises(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": None, "errors": [{"message": "Field 'bogus' doesn't exist"}]},
        status=200,
    )
    with pytest.raises(UserException, match="bogus"):
        client.graphql("{ bogus }", {})


@responses.activate
def test_graphql_partial_data_warns_and_returns(client, caplog):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"viewer": {"login": "octocat"}}, "errors": [{"message": "timeout on field x"}]},
        status=200,
    )
    with caplog.at_level(logging.WARNING):
        rows, resolved = client.graphql("{ viewer { login } }", {})
    assert resolved == "viewer" and rows == [{"login": "octocat"}]
    assert "timeout on field x" in caplog.text


@responses.activate
def test_graphql_rate_limited_type_raises(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
        status=200,
    )
    with pytest.raises(UserException, match="rate limit"):
        client.graphql("{ x }", {})


@responses.activate
def test_graphql_explicit_rows_path_overrides_detection(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"a": {"items": [{"x": 1}], "others": [{"y": 2}]}}},
        status=200,
    )
    rows, resolved = client.graphql("{ a { items others } }", {}, rows_path="a.items")
    assert resolved == "a.items" and rows == [{"x": 1}]


@responses.activate
def test_graphql_follows_cursor_across_two_pages(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={
            "data": {
                "organization": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
                        "nodes": [{"nameWithOwner": "o/a"}],
                    }
                }
            }
        },
        status=200,
    )
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={
            "data": {
                "organization": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [{"nameWithOwner": "o/b"}],
                    }
                }
            }
        },
        status=200,
    )
    rows, _ = client.graphql("query($cursor:String){ x }", {"org": "o"})
    assert [r["nameWithOwner"] for r in rows] == ["o/a", "o/b"]
    assert len(responses.calls) == 2


@responses.activate
def test_graphql_no_cursor_variable_single_page(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={
            "data": {
                "organization": {
                    "repositories": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
                        "nodes": [{"nameWithOwner": "o/a"}],
                    }
                }
            }
        },
        status=200,
    )
    # query has no $cursor -> we do NOT paginate even though hasNextPage is true
    rows, _ = client.graphql("{ organization { repositories { nodes { nameWithOwner } } } }", {})
    assert [r["nameWithOwner"] for r in rows] == ["o/a"]
    assert len(responses.calls) == 1


@responses.activate
def test_graphql_single_object_is_one_row(client):
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={"data": {"viewer": {"login": "octocat", "name": "Octo"}}},
        status=200,
    )
    rows, resolved = client.graphql("{ viewer { login name } }", {})
    assert resolved == "viewer" and rows == [{"login": "octocat", "name": "Octo"}]


# --------------------------------------------------- graphql_sharded


def _search_response(issue_count: int, nodes: list, *, has_next: bool = False, cursor: str | None = None) -> dict:
    """Build a minimal GitHub search response for sharding tests."""
    return {
        "data": {
            "search": {
                "issueCount": issue_count,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


SHARDED_QUERY = "query($q:String!, $cursor:String){ search(query:$q, type:ISSUE, first:50, after:$cursor){ issueCount pageInfo{hasNextPage endCursor} nodes{ ... on PullRequest { id title } } } }"
SHARDED_VARS = {"q": "org:acme is:pr updated:{{window}}"}
SHARDED_PATH = "search.nodes"

SINCE = datetime(2026, 1, 1, tzinfo=UTC)
UNTIL = datetime(2026, 6, 30, tzinfo=UTC)


@responses.activate
def test_graphql_sharded_single_shard_no_bisection(client):
    """issueCount <= 1000 on the first probe: no bisection, just one full fetch (single page)."""
    # Probe response (first POST) — also used as the full fetch since it has hasNextPage: false.
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(10, [{"id": "PR_1", "title": "fix: one"}, {"id": "PR_2", "title": "feat: two"}]),
        status=200,
    )
    # Full fetch (second POST, same shard, no cursor).
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(10, [{"id": "PR_1", "title": "fix: one"}, {"id": "PR_2", "title": "feat: two"}]),
        status=200,
    )
    rows, resolved = client.graphql_sharded(SHARDED_QUERY, SHARDED_VARS, SHARDED_PATH, SINCE, UNTIL)
    assert resolved == SHARDED_PATH
    assert [r["id"] for r in rows] == ["PR_1", "PR_2"]
    # probe + one full-fetch = 2 POSTs
    assert len(responses.calls) == 2


@responses.activate
def test_graphql_sharded_bisects_on_large_issue_count(client):
    """issueCount > 1000 on the root probe → bisect into two sub-ranges, each fetched fully, rows unioned."""
    # 1. Root probe: issueCount=1500 → bisect
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(1500, []),
        status=200,
    )
    # 2. Left-half probe: issueCount=600 → fetch
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(600, []),
        status=200,
    )
    # 3. Left-half full fetch (single page)
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(600, [{"id": "PR_A1"}, {"id": "PR_A2"}]),
        status=200,
    )
    # 4. Right-half probe: issueCount=900 → fetch
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(900, []),
        status=200,
    )
    # 5. Right-half full fetch (single page)
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(900, [{"id": "PR_B1"}, {"id": "PR_B2"}]),
        status=200,
    )
    rows, resolved = client.graphql_sharded(SHARDED_QUERY, SHARDED_VARS, SHARDED_PATH, SINCE, UNTIL)
    assert resolved == SHARDED_PATH
    assert [r["id"] for r in rows] == ["PR_A1", "PR_A2", "PR_B1", "PR_B2"]
    assert len(responses.calls) == 5


@responses.activate
def test_graphql_sharded_deduplicates_boundary_ids(client):
    """A PR id appearing in both halves (boundary overlap) must appear only once in the output."""
    # Root probe → bisect
    responses.add(responses.POST, "https://api.github.com/graphql", json=_search_response(1500, []), status=200)
    # Left probe → fetch
    responses.add(responses.POST, "https://api.github.com/graphql", json=_search_response(800, []), status=200)
    # Left full fetch — PR_BOUNDARY appears here
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(800, [{"id": "PR_BOUNDARY"}, {"id": "PR_LEFT"}]),
        status=200,
    )
    # Right probe → fetch
    responses.add(responses.POST, "https://api.github.com/graphql", json=_search_response(700, []), status=200)
    # Right full fetch — PR_BOUNDARY appears again
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json=_search_response(700, [{"id": "PR_BOUNDARY"}, {"id": "PR_RIGHT"}]),
        status=200,
    )
    rows, _ = client.graphql_sharded(SHARDED_QUERY, SHARDED_VARS, SHARDED_PATH, SINCE, UNTIL)
    ids = [r["id"] for r in rows]
    assert ids.count("PR_BOUNDARY") == 1
    assert "PR_LEFT" in ids and "PR_RIGHT" in ids
    assert len(ids) == 3


@responses.activate
def test_graphql_sharded_no_issue_count_falls_back_with_warning(client, caplog):
    """When issueCount is absent, fall back to a single full fetch and emit a warning."""
    # First POST: no issueCount field
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"id": "PR_X"}],
                }
            }
        },
        status=200,
    )
    # Second POST: full fetch (no pagination needed)
    responses.add(
        responses.POST,
        "https://api.github.com/graphql",
        json={
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"id": "PR_X"}],
                }
            }
        },
        status=200,
    )
    with caplog.at_level(logging.WARNING):
        rows, _ = client.graphql_sharded(SHARDED_QUERY, SHARDED_VARS, SHARDED_PATH, SINCE, UNTIL)
    assert [r["id"] for r in rows] == ["PR_X"]
    assert "issueCount" in caplog.text
