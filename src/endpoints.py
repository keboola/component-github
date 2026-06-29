"""The declarative endpoint registry — the data-driven core of the extractor.

Every supported GitHub endpoint is described as data (an :class:`Endpoint` descriptor), not
code. ``run()`` is identical for all 33 endpoints; the per-endpoint differences (path, target,
primary key, envelope shape, incremental strategy, output columns + native types) live here.
Adding an endpoint is one registry entry + one ``enum`` value in the row schema — never a new
method.

See spec §4.1 (catalog), §4.5 (response shapes), §6.3 (registry shape), §6.5 (native types).
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, auto
from typing import Any


class Target(StrEnum):
    """What concrete path parameters a row must supply."""

    NONE = auto()  # /user, /user/repos — no target
    ORG = auto()  # /orgs/{org}/...
    ORG_TEAM = auto()  # /orgs/{org}/teams/{team_slug}/...
    OWNER_REPO = auto()  # /repos/{owner}/{repo}/...
    OWNER_REPO_PULL = auto()  # /repos/{owner}/{repo}/pulls/{pull_number}/...


class Envelope(StrEnum):
    """The JSON response shape the client must unwrap into records."""

    ARRAY = auto()  # top-level JSON array -> one record each
    OBJECT = auto()  # single JSON object -> one record
    WRAPPED = auto()  # {total_count, <key>: [...]} -> the named array (see envelope_key)
    OBJECT_MAP_VALUE = auto()  # {"Python": 1234, ...} -> one record per key/value (languages)
    OBJECT_MAP_LIST = auto()  # {"names": ["a", "b"]} -> one record per list item (topics)


class BaseType(StrEnum):
    """Keboola native base types emitted in the authoritative ``schema`` manifest (§6.5)."""

    STRING = "STRING"
    INTEGER = "INTEGER"
    TIMESTAMP = "TIMESTAMP"
    BOOLEAN = "BOOLEAN"
    NUMERIC = "NUMERIC"


@dataclass(frozen=True)
class ColumnDef:
    """One output column: its name, native type, and dotted source path in the record.

    ``source`` is a dotted path read from the (possibly nested) GitHub record, e.g.
    ``"user.login"`` reads ``record["user"]["login"]``. When ``source`` is ``None`` the column
    is synthetic (filled by the orchestrator — e.g. the ``owner``/``repo``/``org`` context
    columns, or the object-map key/value columns). ``json`` marks a column whose value is a
    variable-length sub-object/array dumped as a JSON string (e.g. issue ``labels``).
    """

    name: str
    base_type: BaseType = BaseType.STRING
    source: str | None = None
    json: bool = False

    @property
    def source_path(self) -> str:
        """The dotted source path; defaults to the column name when not overridden."""
        return self.source if self.source is not None else self.name


def _iso_since(watermark: str) -> str:
    """Default incremental-param formatter: pass the watermark through verbatim.

    Most ``since``-style filters (e.g. ``/commits?since=``, ``/issues?since=``) accept a bare
    ISO-8601 instant, so the stored watermark is used as-is.
    """
    return watermark


def _created_date_range(watermark: str) -> str:
    """Format the watermark for GitHub Actions' ``created`` filter (date-range grammar).

    ``GET /actions/runs`` does NOT accept a bare ISO instant on ``created`` — it wants the
    range grammar ``>=YYYY-MM-DD`` (a date, not a timestamp). Injecting the raw watermark
    silently under-fetches, so map it to ``>=`` + the date portion. See spec §4.x / Phase 8 TODO 2.
    """
    return f">={watermark[:10]}"


@dataclass(frozen=True)
class Endpoint:
    """A single endpoint descriptor — see spec §6.3."""

    name: str  # registry key == output table name
    path_template: str  # e.g. "/repos/{owner}/{repo}/issues"
    target: Target
    primary_key: list[str]
    envelope: Envelope
    schema: list[ColumnDef]
    envelope_key: str | None = None  # the inner array key when envelope == WRAPPED
    incremental_param: str | None = None  # server-side lower-bound query param (e.g. "since")
    # How the `since` lower bound is rendered into incremental_param (defaults to a bare ISO instant;
    # /actions/runs `created` needs date-range grammar — see _created_date_range).
    incremental_param_format: Callable[[str], str] = _iso_since
    incremental_sort: bool = False  # sort=updated&direction=desc + client-side stop-paging
    # The record field carrying the row's timestamp, used for the client-side fetch-window filter
    # ([since, until]) and stop-paging. Dotted path (e.g. "commit.author.date"). None = not windowable.
    time_field: str | None = None
    default_params: dict[str, str] = field(default_factory=lambda: {"per_page": "100"})
    filter_params: dict[str, str] = field(default_factory=dict)  # row field -> query param name
    drop_predicate: str | None = None  # drop records carrying this top-level key (issues PR-split)
    context_columns: tuple[str, ...] = ()  # synthetic target columns added per record

    def __post_init__(self) -> None:
        # An incremental_sort endpoint relies on the server returning newest-first so the
        # client-side stop-paging predicate (component._stop_paging_predicate) can short-circuit;
        # that only holds when it requests sort=updated & direction=desc. Guard future endpoints.
        if self.incremental_sort:
            assert self.default_params.get("sort") == "updated", (
                f"Endpoint '{self.name}' sets incremental_sort but default_params lacks sort=updated."
            )
            assert self.default_params.get("direction") == "desc", (
                f"Endpoint '{self.name}' sets incremental_sort but default_params lacks direction=desc."
            )


# --- shared column fragments (kept terse; native types per §6.5) ---


def _ts(name: str, source: str | None = None) -> ColumnDef:
    return ColumnDef(name, BaseType.TIMESTAMP, source)


def _int(name: str, source: str | None = None) -> ColumnDef:
    return ColumnDef(name, BaseType.INTEGER, source)


def _bool(name: str, source: str | None = None) -> ColumnDef:
    return ColumnDef(name, BaseType.BOOLEAN, source)


def _str(name: str, source: str | None = None) -> ColumnDef:
    return ColumnDef(name, BaseType.STRING, source)


def _json(name: str, source: str | None = None) -> ColumnDef:
    return ColumnDef(name, BaseType.STRING, source, json=True)


# A minimal GitHub "simple user" fragment, flattened under a dotted prefix.
def _user_cols(prefix: str) -> list[ColumnDef]:
    return [
        _int(f"{prefix}_id", f"{prefix}.id"),
        _str(f"{prefix}_login", f"{prefix}.login"),
        _str(f"{prefix}_type", f"{prefix}.type"),
        _str(f"{prefix}_html_url", f"{prefix}.html_url"),
    ]


# Common repository columns (used by the repo `object` table and shared repo lists).
def _repo_object_cols() -> list[ColumnDef]:
    return [
        _int("id"),
        _str("node_id"),
        _str("name"),
        _str("full_name"),
        _bool("private"),
        _str("owner_login", "owner.login"),
        _int("owner_id", "owner.id"),
        _str("html_url"),
        _str("description"),
        _bool("fork"),
        _str("language"),
        _int("forks_count"),
        _int("stargazers_count"),
        _int("watchers_count"),
        _int("open_issues_count"),
        _int("size"),
        _str("default_branch"),
        _bool("archived"),
        _bool("disabled"),
        _str("visibility"),
        _ts("pushed_at"),
        _ts("created_at"),
        _ts("updated_at"),
    ]


ENDPOINTS: dict[str, Endpoint] = {}


def _register(endpoint: Endpoint) -> None:
    ENDPOINTS[endpoint.name] = endpoint


# =====================================================================================
# A. Authenticated user
# =====================================================================================

_register(
    Endpoint(
        name="user",
        path_template="/user",
        target=Target.NONE,
        primary_key=["id"],
        envelope=Envelope.OBJECT,
        schema=[
            _int("id"),
            _str("login"),
            _str("node_id"),
            _str("type"),
            _str("name"),
            _str("company"),
            _str("email"),
            _str("html_url"),
            _int("public_repos"),
            _int("followers"),
            _int("following"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)

_register(
    Endpoint(
        name="user_repos",
        path_template="/user/repos",
        target=Target.NONE,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        incremental_sort=True,
        time_field="updated_at",
        default_params={"per_page": "100", "sort": "updated", "direction": "desc"},
        schema=_repo_object_cols(),
    )
)

_register(
    Endpoint(
        name="user_orgs",
        path_template="/user/orgs",
        target=Target.NONE,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        schema=[
            _int("id"),
            _str("login"),
            _str("node_id"),
            _str("url"),
            _str("description"),
        ],
    )
)


# =====================================================================================
# B. Organizations
# =====================================================================================

_register(
    Endpoint(
        name="organizations",
        path_template="/orgs/{org}",
        target=Target.ORG,
        primary_key=["id"],
        envelope=Envelope.OBJECT,
        schema=[
            _int("id"),
            _str("login"),
            _str("node_id"),
            _str("name"),
            _str("company"),
            _str("description"),
            _str("html_url"),
            _str("type"),
            _int("public_repos"),
            _int("followers"),
            _int("following"),
            _bool("is_verified"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)

_register(
    Endpoint(
        name="organization_members",
        path_template="/orgs/{org}/members",
        target=Target.ORG,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("org",),
        schema=[
            _int("id"),
            _str("login"),
            _str("node_id"),
            _str("type"),
            _str("html_url"),
            _bool("site_admin"),
        ],
    )
)

_register(
    Endpoint(
        name="organization_teams",
        path_template="/orgs/{org}/teams",
        target=Target.ORG,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("org",),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("name"),
            _str("slug"),
            _str("description"),
            _str("privacy"),
            _str("permission"),
            _str("html_url"),
            _int("parent_id", "parent.id"),
        ],
    )
)

_register(
    Endpoint(
        name="team_members",
        path_template="/orgs/{org}/teams/{team_slug}/members",
        target=Target.ORG_TEAM,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("org", "team_slug"),
        schema=[
            _int("id"),
            _str("login"),
            _str("node_id"),
            _str("type"),
            _str("html_url"),
            _bool("site_admin"),
        ],
    )
)

_register(
    Endpoint(
        name="organization_repositories",
        path_template="/orgs/{org}/repos",
        target=Target.ORG,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        incremental_sort=True,
        time_field="updated_at",
        default_params={"per_page": "100", "sort": "updated", "direction": "desc"},
        context_columns=("org",),
        schema=_repo_object_cols(),
    )
)

_register(
    Endpoint(
        name="organization_events",
        path_template="/orgs/{org}/events",
        target=Target.ORG,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("org",),
        schema=[
            _str("id"),
            _str("type"),
            _str("actor_login", "actor.login"),
            _int("actor_id", "actor.id"),
            _str("repo_name", "repo.name"),
            _int("repo_id", "repo.id"),
            _bool("public"),
            _ts("created_at"),
            _json("payload"),
        ],
    )
)

_register(
    Endpoint(
        name="organization_outside_collaborators",
        path_template="/orgs/{org}/outside_collaborators",
        target=Target.ORG,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("org",),
        schema=[
            _int("id"),
            _str("login"),
            _str("node_id"),
            _str("type"),
            _str("html_url"),
            _bool("site_admin"),
        ],
    )
)


# =====================================================================================
# C. Repositories (owner + repo)
# =====================================================================================

_register(
    Endpoint(
        name="repository",
        path_template="/repos/{owner}/{repo}",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.OBJECT,
        # No synthetic owner/repo: the repo object already carries `full_name` + `owner_login`, so a
        # multi-repo fan-out is self-identifying (see the context-column audit exemption in tests).
        schema=_repo_object_cols(),
    )
)

_register(
    Endpoint(
        name="repository_branches",
        path_template="/repos/{owner}/{repo}/branches",
        target=Target.OWNER_REPO,
        primary_key=["repository", "name"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo", "repository"),
        schema=[
            _str("name"),
            _str("commit_sha", "commit.sha"),
            _str("commit_url", "commit.url"),
            _bool("protected"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_tags",
        path_template="/repos/{owner}/{repo}/tags",
        target=Target.OWNER_REPO,
        primary_key=["repository", "name"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo", "repository"),
        schema=[
            _str("name"),
            _str("commit_sha", "commit.sha"),
            _str("zipball_url"),
            _str("tarball_url"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_commits",
        path_template="/repos/{owner}/{repo}/commits",
        target=Target.OWNER_REPO,
        # A commit SHA is not globally unique (a fork shares its base repo's SHAs), and this is an
        # incremental, shared OWNER_REPO table — so scope the PK by `repository` like the sibling
        # shared tables (branches/tags/contributors/...) to avoid cross-repo upsert collisions.
        primary_key=["repository", "sha"],
        envelope=Envelope.ARRAY,
        incremental_param="since",
        time_field="commit.author.date",
        context_columns=("owner", "repo", "repository"),
        schema=[
            _str("sha"),
            _str("node_id"),
            _str("message", "commit.message"),
            _str("author_name", "commit.author.name"),
            _str("author_email", "commit.author.email"),
            _ts("author_date", "commit.author.date"),
            _str("committer_name", "commit.committer.name"),
            _ts("committer_date", "commit.committer.date"),
            _str("author_login", "author.login"),
            _int("author_id", "author.id"),
            _str("committer_login", "committer.login"),
            _str("html_url"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_contributors",
        path_template="/repos/{owner}/{repo}/contributors",
        target=Target.OWNER_REPO,
        primary_key=["repository", "id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo", "repository"),
        schema=[
            _int("id"),
            _str("login"),
            _str("type"),
            _str("html_url"),
            _int("contributions"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_collaborators",
        path_template="/repos/{owner}/{repo}/collaborators",
        target=Target.OWNER_REPO,
        primary_key=["repository", "id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo", "repository"),
        schema=[
            _int("id"),
            _str("login"),
            _str("type"),
            _str("html_url"),
            _str("role_name"),
            _bool("site_admin"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_languages",
        path_template="/repos/{owner}/{repo}/languages",
        target=Target.OWNER_REPO,
        primary_key=["repository", "language"],
        envelope=Envelope.OBJECT_MAP_VALUE,
        context_columns=("owner", "repo", "repository"),
        schema=[
            _str("language"),  # synthetic: the map key
            _int("bytes"),  # synthetic: the map value
        ],
    )
)

_register(
    Endpoint(
        name="repository_topics",
        path_template="/repos/{owner}/{repo}/topics",
        target=Target.OWNER_REPO,
        primary_key=["repository", "topic"],
        envelope=Envelope.OBJECT_MAP_LIST,
        envelope_key="names",
        context_columns=("owner", "repo", "repository"),
        schema=[
            _str("topic"),  # synthetic: each item of names[]
        ],
    )
)

_register(
    Endpoint(
        name="repository_releases",
        path_template="/repos/{owner}/{repo}/releases",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("tag_name"),
            _str("name"),
            _bool("draft"),
            _bool("prerelease"),
            _str("author_login", "author.login"),
            _int("author_id", "author.id"),
            _str("html_url"),
            _ts("created_at"),
            _ts("published_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_issues",
        path_template="/repos/{owner}/{repo}/issues",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        incremental_param="since",
        time_field="updated_at",
        drop_predicate="pull_request",  # /issues returns PRs too — drop them (§4.4)
        filter_params={"issue_state": "state"},
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _int("number"),
            _str("title"),
            _str("state"),
            _bool("locked"),
            _int("comments"),
            _str("user_login", "user.login"),
            _int("user_id", "user.id"),
            _str("assignee_login", "assignee.login"),
            _int("milestone_id", "milestone.id"),
            _str("html_url"),
            _json("labels"),
            _ts("created_at"),
            _ts("updated_at"),
            _ts("closed_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_issue_comments",
        path_template="/repos/{owner}/{repo}/issues/comments",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        incremental_param="since",
        time_field="updated_at",
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("body"),
            _str("user_login", "user.login"),
            _int("user_id", "user.id"),
            _str("html_url"),
            _str("issue_url"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_pulls",
        path_template="/repos/{owner}/{repo}/pulls",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        incremental_sort=True,  # no `since` on /pulls — sort=updated desc + client-side stop (R6)
        time_field="updated_at",
        default_params={"per_page": "100", "sort": "updated", "direction": "desc"},
        filter_params={"pull_state": "state"},
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _int("number"),
            _str("title"),
            _str("state"),
            _bool("locked"),
            _bool("draft"),
            _str("user_login", "user.login"),
            _int("user_id", "user.id"),
            _str("head_ref", "head.ref"),
            _str("head_sha", "head.sha"),
            _str("base_ref", "base.ref"),
            _str("merge_commit_sha"),
            _str("html_url"),
            _ts("created_at"),
            _ts("updated_at"),
            _ts("closed_at"),
            _ts("merged_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_pull_reviews",
        path_template="/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
        target=Target.OWNER_REPO_PULL,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        # pull_number is a context column so the nested fan-out (empty pull_number = all PRs) tags
        # each review row with the PR it came from; in targeted mode it carries the configured number.
        context_columns=("owner", "repo", "pull_number"),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("user_login", "user.login"),
            _int("user_id", "user.id"),
            _str("body"),
            _str("state"),
            _str("commit_id"),
            _str("html_url"),
            _ts("submitted_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_pull_review_comments",
        path_template="/repos/{owner}/{repo}/pulls/comments",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        incremental_param="since",
        time_field="updated_at",
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _int("pull_request_review_id"),
            _str("body"),
            _str("user_login", "user.login"),
            _int("user_id", "user.id"),
            _str("path"),
            _str("commit_id"),
            _str("html_url"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_labels",
        path_template="/repos/{owner}/{repo}/labels",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("name"),
            _str("color"),
            _bool("default"),
            _str("description"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_milestones",
        path_template="/repos/{owner}/{repo}/milestones",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        filter_params={"milestone_state": "state"},
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _int("number"),
            _str("title"),
            _str("description"),
            _str("state"),
            _int("open_issues"),
            _int("closed_issues"),
            _str("creator_login", "creator.login"),
            _ts("created_at"),
            _ts("updated_at"),
            _ts("due_on"),
            _ts("closed_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_stargazers",
        path_template="/repos/{owner}/{repo}/stargazers",
        target=Target.OWNER_REPO,
        primary_key=["repository", "id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo", "repository"),
        schema=[
            _int("id"),
            _str("login"),
            _str("type"),
            _str("html_url"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_subscribers",
        path_template="/repos/{owner}/{repo}/subscribers",
        target=Target.OWNER_REPO,
        primary_key=["repository", "id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo", "repository"),
        schema=[
            _int("id"),
            _str("login"),
            _str("type"),
            _str("html_url"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_forks",
        path_template="/repos/{owner}/{repo}/forks",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("name"),
            _str("full_name"),
            _str("owner_login", "owner.login"),
            _int("owner_id", "owner.id"),
            _str("html_url"),
            _int("forks_count"),
            _int("stargazers_count"),
            _ts("created_at"),
            _ts("updated_at"),
            _ts("pushed_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_workflows",
        path_template="/repos/{owner}/{repo}/actions/workflows",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.WRAPPED,
        envelope_key="workflows",
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("name"),
            _str("path"),
            _str("state"),
            _str("html_url"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_workflow_runs",
        path_template="/repos/{owner}/{repo}/actions/runs",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.WRAPPED,
        envelope_key="workflow_runs",
        incremental_param="created",  # /actions/runs filters by `created`, not `since`
        incremental_param_format=_created_date_range,  # `created` wants >=YYYY-MM-DD, not a bare instant
        time_field="created_at",
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("name"),
            _str("head_branch"),
            _str("head_sha"),
            _int("run_number"),
            _str("event"),
            _str("status"),
            _str("conclusion"),
            _int("workflow_id"),
            _str("html_url"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_deployments",
        path_template="/repos/{owner}/{repo}/deployments",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("node_id"),
            _str("sha"),
            _str("ref"),
            _str("task"),
            _str("environment"),
            _str("description"),
            _str("creator_login", "creator.login"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)

_register(
    Endpoint(
        name="repository_webhooks",
        path_template="/repos/{owner}/{repo}/hooks",
        target=Target.OWNER_REPO,
        primary_key=["id"],
        envelope=Envelope.ARRAY,
        context_columns=("owner", "repo"),
        schema=[
            _int("id"),
            _str("name"),
            _bool("active"),
            _json("events"),
            _str("config_url", "config.url"),
            _str("config_content_type", "config.content_type"),
            _ts("created_at"),
            _ts("updated_at"),
        ],
    )
)


# =====================================================================================
# Flattening / record transformation
# =====================================================================================


def _dig(record: dict[str, Any], dotted: str) -> Any:
    """Read a dotted path from a (possibly nested) record; ``None`` if any hop is missing."""
    value: Any = record
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
        if value is None:
            return None
    return value


def record_in_window(
    endpoint: Endpoint, record: dict[str, Any], since: datetime | None, until: datetime | None
) -> bool:
    """True if the record's ``time_field`` falls within ``[since, until]`` (open where a bound is None).

    The client-side guarantee for the fetch window: GitHub's server-side ``since``/``created`` bound
    volume where it exists, but ``until`` (and any endpoint without a native lower bound) is enforced
    here so the window is honoured uniformly. A record with no parseable timestamp is KEPT — we never
    silently drop data we can't classify.
    """
    if endpoint.time_field is None or (since is None and until is None):
        return True
    raw = _dig(record, endpoint.time_field)
    if not isinstance(raw, str):
        return True
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def expand_records(endpoint: Endpoint, payload: Any, context: dict[str, str]) -> Iterator[dict[str, Any]]:
    """Expand the object-map envelopes into per-row dicts before flattening.

    The client already unwraps ARRAY / OBJECT / WRAPPED into a stream of plain dicts. The two
    object-map shapes (languages, topics) are not dict records, so the client yields their raw
    payload and the orchestrator expands them here against the synthetic key/value columns.
    """
    if endpoint.envelope == Envelope.OBJECT_MAP_VALUE:
        # {"Python": 1234, "Go": 567} -> {"language": "Python", "bytes": 1234}
        for key, val in (payload or {}).items():
            yield {"language": key, "bytes": val}
    elif endpoint.envelope == Envelope.OBJECT_MAP_LIST:
        # {"names": ["a", "b"]} -> {"topic": "a"}, {"topic": "b"}
        for item in (payload or {}).get(endpoint.envelope_key, []):
            yield {"topic": item}
    else:
        yield payload


def flatten_record(endpoint: Endpoint, record: dict[str, Any], context: dict[str, str]) -> dict[str, Any]:
    """Flatten a raw record to the endpoint's declared output columns.

    Dotted source paths are dug out of nested objects; ``json`` columns are serialized to a
    compact JSON string; synthetic context columns (``owner``/``repo``/``org``/``repository``)
    are filled from ``context``; object-map synthetic columns are read straight from the record.
    """
    out: dict[str, Any] = {}
    for col in endpoint.schema:
        if col.name in context:  # synthetic context column
            out[col.name] = context[col.name]
            continue
        value = _dig(record, col.source_path)
        if col.json and value is not None:
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        out[col.name] = value
    # Always emit explicit context columns even if not in the declared schema order.
    for ctx_col in endpoint.context_columns:
        out.setdefault(ctx_col, context.get(ctx_col, ""))
    return out


def output_columns(endpoint: Endpoint) -> list[str]:
    """The full ordered list of output column names (schema columns + any extra context)."""
    names = [col.name for col in endpoint.schema]
    for ctx_col in endpoint.context_columns:
        if ctx_col not in names:
            names.append(ctx_col)
    return names
