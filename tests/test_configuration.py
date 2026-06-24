"""Unit tests for the Configuration validators — every bad config is a clean UserException.

These guard the exit-1 boundary: a malformed config must surface as a UserException (which the
``__main__`` guard maps to exit 1), never an uncaught error (exit 2 — "internal error" in the UI).
Pydantic enum/type failures are re-wrapped with the "Configuration validation error" prefix by the
model's ``__init__``; the custom ``model_validator`` rules raise their own UserException directly.
"""

import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration


def test_pat_auth_empty_token_raises():
    """PAT auth with an empty #github_token is rejected (the _validate_auth rule)."""
    with pytest.raises(UserException, match="Personal Access Token"):
        Configuration(auth_type="pat", **{"#github_token": ""})


def test_unknown_endpoint_raises():
    """A non-empty endpoint that is not a registry key is rejected (the _validate_endpoint rule)."""
    with pytest.raises(UserException, match="Unknown endpoint 'not_a_real_endpoint'"):
        Configuration(auth_type="pat", endpoint="not_a_real_endpoint", **{"#github_token": "ghp_x"})


def test_invalid_state_enum_raises_with_validation_prefix():
    """A bogus state enum is a Pydantic ValidationError re-wrapped with the standard prefix."""
    with pytest.raises(UserException, match="Configuration validation error"):
        Configuration(auth_type="pat", issue_state="bogus", **{"#github_token": "ghp_x"})


def test_malformed_initial_since_raises():
    """A non-ISO initial_since is rejected before it can reach the fetch path (TODO 1)."""
    with pytest.raises(UserException, match="initial_since must be an ISO-8601 timestamp"):
        Configuration(auth_type="pat", initial_since="yesterday", **{"#github_token": "ghp_x"})


def test_malformed_initial_since_out_of_range_month_raises():
    """An ISO-shaped but invalid value (month 13) is still rejected (datetime.fromisoformat raises)."""
    with pytest.raises(UserException, match="initial_since must be an ISO-8601 timestamp"):
        Configuration(auth_type="pat", initial_since="2024-13-01", **{"#github_token": "ghp_x"})


def test_valid_initial_since_with_trailing_z_accepted():
    """A well-formed ISO instant with a trailing Z normalizes and passes (no exception)."""
    cfg = Configuration(auth_type="pat", initial_since="2024-01-31T00:00:00Z", **{"#github_token": "ghp_x"})
    assert cfg.initial_since == "2024-01-31T00:00:00Z"


def test_empty_initial_since_is_allowed():
    """Empty initial_since stays empty (full history on the first run) — not validated."""
    cfg = Configuration(auth_type="pat", initial_since="", **{"#github_token": "ghp_x"})
    assert cfg.initial_since == ""
