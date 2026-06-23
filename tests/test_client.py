"""Unit tests for the auth-agnostic GitHub client (mocked HTTP via ``responses``)."""

import time

import pytest
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


def test_session_headers_set():
    c = GitHubClient(token="ghp_abc")
    assert c.session.headers["Authorization"] == "Bearer ghp_abc"
    assert c.session.headers["Accept"] == "application/vnd.github+json"
    assert c.session.headers["X-GitHub-Api-Version"] == "2022-11-28"
