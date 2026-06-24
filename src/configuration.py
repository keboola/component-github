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

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, model_validator

from endpoints import ENDPOINTS


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

    All async-loaded fields (``organization``, ``team_slug``, ``repository``) and the token
    default to empty so sync actions — which instantiate this before the form is complete —
    never trip validation. Presence of a required target is enforced in ``run()`` against the
    registry, not here. See the sync-action gotcha in component-develop's configuration model.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # --- root: authentication + global options ---
    auth_type: AuthType = AuthType.pat
    github_token: str = Field(alias="#github_token", default="")

    # --- row: endpoint selection + target + filters ---
    endpoint: str = ""
    organization: str = ""
    team_slug: str = ""
    repository: str = ""
    pull_number: int | None = None
    load_type: LoadType = LoadType.incremental_load
    initial_since: str = ""
    issue_state: State = State.all
    pull_state: State = State.all
    milestone_state: State = State.all

    @computed_field
    @property
    def incremental_load(self) -> bool:
        """True when this row should upsert incrementally (vs full overwrite)."""
        return self.load_type == LoadType.incremental_load

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
    def _validate_initial_since(self) -> Self:
        # `initial_since` is unvalidated free-text from the UI and is fed verbatim downstream:
        # the stop-paging watermark parses it (component._stop_paging_predicate) and it is also
        # injected into the server-side `since`/`created` query params. Reject a malformed value
        # here so a typo ("yesterday", "2024-13-01") is a clean exit 1, not an uncaught ValueError
        # (exit 2) at fetch time. Empty stays empty (full history on the first run).
        if self.initial_since:
            try:
                datetime.fromisoformat(self.initial_since.replace("Z", "+00:00"))
            except ValueError as exc:
                raise UserException("initial_since must be an ISO-8601 timestamp, e.g. 2024-01-31T00:00:00Z") from exc
        return self

    def __init__(self, **data: Any):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Configuration validation error: {', '.join(error_messages)}") from e
