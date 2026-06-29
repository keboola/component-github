"""GraphQL passthrough helpers — pure transforms + the template registry.

Mirrors how `endpoints.py` holds REST data/transforms. Knows nothing about the client, the config
model, or the component, so it is import-cycle-free and trivially unit-testable. See spec
`2026-06-29-ex-github-v2-graphql-query-source-design.md`.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keboola.component.exceptions import UserException

_NON_IDENT = re.compile(r"[^0-9a-zA-Z]+")


def sanitize_alias(filename: str) -> str:
    """Turn a file path into a GraphQL-safe alias / column prefix: 'AGENTS.md' -> 'agents_md'."""
    slug = _NON_IDENT.sub("_", filename).strip("_").lower()
    if not slug:
        slug = "file"
    if slug[0].isdigit():
        slug = f"f_{slug}"
    return slug


def flatten_graphql_node(node: dict[str, Any], sep: str = "_") -> dict[str, Any]:
    """Flatten one row node: nested objects -> `parent_child` columns; arrays -> JSON cell; scalars/None kept.

    A null object (e.g. a `Blob` field for an absent file) becomes a single empty cell under its own key.
    """
    out: dict[str, Any] = {}

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, val in value.items():
                walk(f"{prefix}{sep}{key}" if prefix else key, val)
        elif isinstance(value, list):
            out[prefix] = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        else:
            out[prefix] = value

    if not isinstance(node, dict):
        return {"value": node}
    for key, val in node.items():
        walk(key, val)
    return out


_DATE_PLACEHOLDER = re.compile(r"\{\{\s*(since|until)\s*\}\}")


def apply_date_placeholders(text: str, since: str | None, until: str | None) -> str:
    """Substitute ``{{since}}`` / ``{{until}}`` in a query/variable string with resolved date strings.

    Lets a GraphQL row reuse the dateparser-backed ``since``/``until`` fields for a rolling window
    (e.g. ``updated:>={{since}}`` with Since = "60 days ago"). No placeholder → returned unchanged. A
    placeholder whose field is empty is a config error — raise a clean UserException rather than emit a
    malformed query.
    """

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = since if key == "since" else until
        if not value:
            placeholder = "{{" + key + "}}"
            raise UserException(
                f"The GraphQL query references {placeholder} but the '{key.capitalize()}' field is empty — "
                f"set {key.capitalize()} (e.g. '60 days ago') or remove the placeholder."
            )
        return value

    return _DATE_PLACEHOLDER.sub(_repl, text)


def collect_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Union of all row keys, in first-seen order (stable column ordering for a given query)."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen_set:
                seen_set.add(key)
                seen.append(key)
    return seen


def read_path(data: Any, dotted: str) -> Any:
    """Read a dotted path; empty path returns `data` unchanged; missing hop returns None."""
    if dotted == "":
        return data
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def connection_path(rows_path: str) -> str | None:
    """The paginated connection (rows path minus a trailing `nodes`/`edges`), or None."""
    parts = rows_path.split(".")
    if parts and parts[-1] in ("nodes", "edges"):
        return ".".join(parts[:-1])
    return None


def _is_array_of_objects(value: Any) -> bool:
    return isinstance(value, list) and (not value or isinstance(value[0], dict))


_AMBIGUOUS = (
    "Could not determine the rows path automatically (the query returns more than one list). "
    "Set the 'Row path (advanced)' field, e.g. organization.repositories.nodes."
)


def detect_rows_path(data: dict[str, Any]) -> str:
    """Descend from `data` to the first array-of-objects (preferring `nodes`/`edges`); '' = whole data is one row."""
    path: list[str] = []
    cur: Any = data
    while isinstance(cur, dict):
        array_children = [k for k, v in cur.items() if _is_array_of_objects(v)]
        chosen: str | None = None
        if len(array_children) == 1:
            chosen = array_children[0]
        elif len(array_children) > 1:
            for pref in ("nodes", "edges"):
                if pref in array_children:
                    chosen = pref
                    break
            if chosen is None:
                raise UserException(_AMBIGUOUS)
        if chosen is not None:
            path.append(chosen)
            return ".".join(path)
        object_children = [k for k, v in cur.items() if isinstance(v, dict)]
        if len(object_children) == 1:
            path.append(object_children[0])
            cur = cur[object_children[0]]
            continue
        if not object_children:
            return ".".join(path)  # bottomed out at an object -> one row ('' when path empty)
        raise UserException(_AMBIGUOUS)
    raise UserException(_AMBIGUOUS)


@dataclass(frozen=True)
class GraphQLTemplate:
    """A built-in query a non-GraphQL user picks; params come from dedicated config fields."""

    build_query: Callable[[dict[str, Any]], str]
    build_variables: Callable[[dict[str, Any]], dict[str, Any]]
    rows_path: str
    output_table: str
    primary_key: list[str]


def _ai_config_query(params: dict[str, Any]) -> str:
    filenames = params["filenames"] or ["AGENTS.md", "CLAUDE.md"]
    blob_fields = "\n        ".join(
        f'{sanitize_alias(fn)}: object(expression: "HEAD:{fn}") {{ ... on Blob {{ byteSize isBinary text }} }}'
        for fn in filenames
    )
    return f"""query($org: String!, $cursor: String) {{
  organization(login: $org) {{
    repositories(first: 100, after: $cursor) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        nameWithOwner
        url
        defaultBranchRef {{ name }}
        {blob_fields}
      }}
    }}
  }}
}}"""


def _pull_request_reviews_query(params: dict[str, Any]) -> str:
    return """query($org: String!, $repo: String!, $cursor: String) {
  repository(owner: $org, name: $repo) {
    pullRequests(states: OPEN, first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { number title url author { login } reviewDecision createdAt updatedAt }
    }
  }
}"""


TEMPLATES: dict[str, GraphQLTemplate] = {
    "org_ai_config_files": GraphQLTemplate(
        build_query=_ai_config_query,
        build_variables=lambda p: {"org": p["org"]},
        rows_path="organization.repositories.nodes",
        output_table="repo_ai_config_files",
        primary_key=["nameWithOwner"],
    ),
    "org_pull_request_reviews": GraphQLTemplate(
        build_query=_pull_request_reviews_query,
        build_variables=lambda p: {"org": p["org"], "repo": p["repo"]},
        rows_path="repository.pullRequests.nodes",
        output_table="repo_open_pull_requests",
        primary_key=["number"],
    ),
}
