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


def test_unparseable_initial_since_raises():
    """A value that is neither ISO nor a relative phrase is rejected (clean exit 1)."""
    with pytest.raises(UserException, match="ISO-8601 or a relative phrase"):
        Configuration(auth_type="pat", initial_since="qwerty", **{"#github_token": "ghp_x"})


def test_valid_initial_since_with_trailing_z_accepted():
    """A well-formed ISO instant with a trailing Z is accepted and resolves to that instant."""
    cfg = Configuration(auth_type="pat", initial_since="2024-01-31T00:00:00Z", **{"#github_token": "ghp_x"})
    assert cfg.initial_since == "2024-01-31T00:00:00Z"
    assert cfg.parsed_initial_since().startswith("2024-01-31T00:00:00")


def test_empty_initial_since_is_allowed():
    """Empty initial_since stays empty (full history on the first run) — parses to None."""
    cfg = Configuration(auth_type="pat", initial_since="", **{"#github_token": "ghp_x"})
    assert cfg.initial_since == ""
    assert cfg.parsed_initial_since() is None


def test_relative_initial_since_parses_to_instant():
    """A relative phrase ('5 days ago') is valid and resolves to an absolute tz-aware instant."""
    cfg = Configuration(auth_type="pat", initial_since="5 days ago", **{"#github_token": "ghp_x"})
    assert cfg.initial_since == "5 days ago"  # raw value preserved for the form
    parsed = cfg.parsed_initial_since()
    assert parsed is not None and ("+00:00" in parsed or parsed.endswith("Z"))
