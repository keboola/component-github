"""Credential resolution — the only auth-aware code in the component.

GitHub accepts every token type as ``Authorization: Bearer <token>``, so the HTTP client is
auth-agnostic. This module turns either credential source into a bare token string:

* **PAT** — the user-pasted ``#github_token`` field, read directly.
* **OAuth** — the Keboola broker injects ``authorization.oauth_api.credentials`` at runtime;
  ``#data`` is the provider's *raw token response as a string*, so we parse it ourselves and
  read ``access_token`` (the broker has no knowledge of GitHub's response shape — spec §3.1).
"""

import json
from typing import Any

from keboola.component.exceptions import UserException

from configuration import AuthType, Configuration


def resolve_token(configuration: Configuration, oauth_credentials: dict[str, Any] | None) -> str:
    """Return the bearer token string for the configured auth method.

    :param configuration: the validated component configuration.
    :param oauth_credentials: ``authorization.oauth_api.credentials`` injected by the Job Queue
        (``None`` when the component runs the PAT path or the broker injected nothing).
    :raises UserException: if a PAT is empty, or OAuth ``#data`` is missing/unparseable/has no
        ``access_token``.
    """
    if configuration.auth_type == AuthType.oauth:
        return _resolve_oauth_token(oauth_credentials)
    return _resolve_pat(configuration)


def _resolve_pat(configuration: Configuration) -> str:
    token = configuration.github_token
    if not token:
        # Belt-and-braces: the Configuration validator already enforces this for the PAT path.
        raise UserException("A GitHub Personal Access Token (#github_token) is required.")
    return token


def _resolve_oauth_token(oauth_credentials: dict[str, Any] | None) -> str:
    if not oauth_credentials:
        raise UserException(
            "OAuth authentication selected but no broker credentials were injected. "
            "Re-authorize the configuration, or check the OAuth setup."
        )

    raw_data = oauth_credentials.get("#data")
    if not raw_data:
        raise UserException("OAuth credentials are missing the '#data' token payload.")

    try:
        token_response = json.loads(raw_data)
    except (json.JSONDecodeError, TypeError) as exc:
        raise UserException(f"OAuth '#data' is not valid JSON: {exc}") from exc

    access_token = token_response.get("access_token") if isinstance(token_response, dict) else None
    if not access_token:
        raise UserException("OAuth token response in '#data' has no 'access_token'.")
    return access_token
