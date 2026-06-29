"""Configuration models for the GitHub extractor.

The Keboola platform delivers a single merged ``config.json`` whose ``parameters`` are the
root config (auth + global options) and the row's params flattened together. One
``Configuration`` model therefore mirrors both concerns. ``debug`` is intentionally NOT a
field here — ``ComponentBase`` consumes the platform ``debug`` parameter and switches the
root logger to DEBUG on its own, so a model field would be redundant (see spec §6.4).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

import dateparser
from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, model_validator

from endpoints import ENDPOINTS

# dateparser settings: resolve relative phrases ("5 days ago") against UTC and always return a
# timezone-aware datetime so the rendered ISO instant carries an explicit offset.
_DATEPARSER_SETTINGS = {"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "UTC"}


def _parse_date(value: str) -> datetime | None:
    """Parse an ISO-8601 or relative ('5 days ago', 'now') string to a tz-aware datetime, else None.

    dateparser handles both absolute ISO instants and relative phrases, so it is the single parser
    for the user-facing ``Since`` / ``Until`` window fields. Returns ``None`` for an empty or
    unparseable value (the caller decides whether that is an error or "no bound").
    """
    if not value:
        return None
    return dateparser.parse(value, settings=_DATEPARSER_SETTINGS)


class AuthType(StrEnum):
    pat = "pat"
    oauth = "oauth"


class LoadType(StrEnum):
    full_load = "full_load"
    incremental_load = "incremental_load"


class State(StrEnum):
    """Closed set for the issue/pull/milestone ``state`` filters."""

    all = "all"
    open = "open"
    closed = "closed"


class Configuration(BaseModel):
    """Merged root + row configuration for one extraction.

    The async-loaded target fields (``organization``, ``team_slug``, ``repository``) and the
    ``endpoint`` default to empty so sync actions — which instantiate this before the form is
    complete — don't trip *target-presence* validation; presence of a required target is
    enforced in ``run()`` against the registry, not here. The PAT, by contrast, IS validated
    here: ``_validate_auth`` raises when ``auth_type=pat`` (the default) and ``#github_token``
    is empty, so testConnection on an empty form fails with a clear "token required" message
    rather than a confusing downstream error. See the sync-action gotcha in component-develop's
    configuration model.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # --- root: authentication + global options ---
    auth_type: AuthType = AuthType.pat
    github_token: str = Field(alias="#github_token", default="")

    # --- row: endpoint selection + target + filters ---
    # Targets are multi-select: empty list = "all in scope" (resolved in component._resolve_targets).
    # A legacy single-string value (old rows / scaffolded rows) is coerced to a one-element list by
    # _coerce_target_lists below, so existing configs keep working.
    endpoint: str = ""
    organization: list[str] = Field(default_factory=list)
    team_slug: list[str] = Field(default_factory=list)
    repository: list[str] = Field(default_factory=list)
    pull_number: int | None = None
    load_type: LoadType = LoadType.incremental_load
    # Explicit fetch window. Each run pulls records whose timestamp is in [since, until] — no hidden
    # watermark/state. Both accept ISO or relative phrases ("90 days ago", "now"). Empty since = no
    # lower bound (all history); empty until = up to now. Widen `since` to backfill — it always applies.
    since: str = ""
    until: str = ""
    issue_state: State = State.all
    pull_state: State = State.all
    milestone_state: State = State.all

    @computed_field
    @property
    def incremental_load(self) -> bool:
        """True when this row should upsert incrementally (vs full overwrite)."""
        return self.load_type == LoadType.incremental_load

    @model_validator(mode="before")
    @classmethod
    def _coerce_target_lists(cls, data: Any) -> Any:
        """Back-compat: coerce a legacy single-string target to a one-element list; blank → [].

        The multi-select schema sends arrays, but existing rows (and the scaffolded rows in the
        test project) still carry a single string. Runs before type validation so both shapes load.
        """
        if not isinstance(data, dict):
            return data
        for key in ("organization", "team_slug", "repository"):
            value = data.get(key)
            if value is None or value == "":
                data[key] = []
            elif isinstance(value, str):
                data[key] = [value]
        return data

    @model_validator(mode="after")
    def _validate_auth(self) -> Self:
        # OAuth tokens are validated at runtime by the presence of broker-injected
        # credentials, so only the PAT path can be checked here.
        if self.auth_type == AuthType.pat and not self.github_token:
            raise UserException(
                "A GitHub Personal Access Token (#github_token) is required when "
                "the authentication method is 'Personal Access Token'."
            )
        return self

    @model_validator(mode="after")
    def _validate_endpoint(self) -> Self:
        # Empty endpoint is allowed: sync actions instantiate the model before the form is
        # complete. A non-empty endpoint must be a known registry key (run() also re-checks).
        if self.endpoint and self.endpoint not in ENDPOINTS:
            raise UserException(f"Unknown endpoint '{self.endpoint}'. Choose one of: {', '.join(sorted(ENDPOINTS))}.")
        return self

    @model_validator(mode="after")
    def _validate_pull_reviews(self) -> Self:
        # `repository_pull_reviews` needs a specific pull_number; "all repos × one PR number" is
        # meaningless. With a number set it targets exactly one repo (targeted mode); an empty
        # number triggers the nested fan-out (component._fetch_pull_reviews_nested).
        if self.endpoint == "repository_pull_reviews" and self.pull_number is not None and len(self.repository) > 1:
            raise UserException(
                "Pull Request Reviews with a Pull Request Number set targets exactly one repository; "
                "select a single repository, or clear the number to fetch reviews for all pull requests."
            )
        return self

    @model_validator(mode="after")
    def _validate_dates(self) -> Self:
        # `since`/`until` are free-text from the UI, fed downstream as the fetch-window bounds
        # (server-side `since`/`created` + a client-side window filter). Accept ISO instants AND
        # relative phrases ("90 days ago", "now"); reject anything else here so a typo is a clean
        # exit 1, not an uncaught error (exit 2) at fetch time. Empty since/until = unbounded that end.
        for label, value in (("Since", self.since), ("Until", self.until)):
            if value and _parse_date(value) is None:
                raise UserException(
                    f"{label} must be ISO-8601 or a relative phrase (e.g. '2024-01-31T00:00:00Z' or '90 days ago')."
                )
        return self

    def parsed_since(self) -> datetime | None:
        """The window lower bound as a tz-aware datetime (relative phrases resolved), or None."""
        return _parse_date(self.since)

    def parsed_until(self) -> datetime | None:
        """The window upper bound as a tz-aware datetime (relative phrases resolved), or None."""
        return _parse_date(self.until)

    def __init__(self, **data: Any):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Configuration validation error: {', '.join(error_messages)}") from e
