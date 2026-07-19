"""Current-user resolution for routes.

Resolution order:
1. ``$CSFLOW_USER`` override (tests / local CLI compatibility).
2. ``Config.default_user`` (single-user local deployment).
"""

from __future__ import annotations

import os

from fastapi import Request
from starlette.websockets import WebSocket

from app.api.errors import ApiError
from app.config import load_config
from app.paths import validate_identifier
from app.user_context import set_request_user


def _normalise_user(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ApiError(
            "UNAUTHENTICATED",
            "empty user identity",
            status_code=401,
        )
    try:
        return validate_identifier(value, kind="user")
    except ValueError as exc:
        raise ApiError("UNAUTHENTICATED", str(exc), status_code=401) from exc


def resolve_current_user(conn: Request | WebSocket | None = None) -> str:
    """Resolve and bind request user to context."""
    del conn  # kept in the signature for FastAPI dependency compatibility
    env_override = os.environ.get("CSFLOW_USER")
    if env_override:
        user = _normalise_user(env_override)
        set_request_user(user)
        return user

    user = _normalise_user(load_config().default_user)
    set_request_user(user)
    return user


def current_user(request: Request) -> str:
    """FastAPI dependency yielding the caller username for HTTP routes."""
    return resolve_current_user(request)


__all__ = ["current_user", "resolve_current_user"]
