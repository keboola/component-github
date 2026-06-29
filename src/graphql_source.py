"""GraphQL passthrough helpers — pure transforms + the template registry.

Mirrors how `endpoints.py` holds REST data/transforms. Knows nothing about the client, the config
model, or the component, so it is import-cycle-free and trivially unit-testable. See spec
`2026-06-29-ex-github-v2-graphql-query-source-design.md`.
"""

import json
import re
from typing import Any

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
