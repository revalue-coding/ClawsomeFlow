"""Current-user resolution for routes.

Resolution order:
1. ``$CSFLOW_USER`` override (tests / local CLI compatibility).
2. ``server`` mode: request headers ``X-CSFLOW-User`` / ``X-Forwarded-User``.
3. ``local`` mode: ``Config.default_user``.

In server mode we intentionally reject process-global fallback users so each
request carries an explicit caller identity.
"""

from __future__ import annotations

import os

from fastapi import Request
from starlette.websockets import WebSocket

from app.api.errors import ApiError
from app.config import load_config
from app.deployment import get_deployment_capabilities
from app.paths import validate_identifier
from app.user_context import set_request_user

_SERVER_USER_HEADER_CANDIDATES = (
    "x-csflow-user",
    "x-forwarded-user",
)


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


def _extract_header_user(conn: Request | WebSocket | None) -> str | None:
    if conn is None:
        return None
    for key in _SERVER_USER_HEADER_CANDIDATES:
        value = conn.headers.get(key)
        if value and value.strip():
            return value.strip()
    return None


def resolve_current_user(conn: Request | WebSocket | None = None) -> str:
    """Resolve and bind request user to context."""
    env_override = os.environ.get("CSFLOW_USER")
    if env_override:
        user = _normalise_user(env_override)
        set_request_user(user)
        return user

    cfg = load_config()
    caps = get_deployment_capabilities(cfg)
    if caps.requires_request_identity_headers:
        header_user = _extract_header_user(conn)
        if not header_user:
            raise ApiError(
                "UNAUTHENTICATED",
                "server mode requires request-level user identity "
                "(X-CSFLOW-User or X-Forwarded-User)",
                status_code=401,
            )
        user = _normalise_user(header_user)
        set_request_user(user)
        return user

    user = _normalise_user(cfg.default_user)
    set_request_user(user)
    return user


def current_user(request: Request) -> str:
    """FastAPI dependency yielding the caller username for HTTP routes."""
    return resolve_current_user(request)


__all__ = ["current_user", "resolve_current_user"]
