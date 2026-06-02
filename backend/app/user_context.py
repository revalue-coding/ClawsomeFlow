"""Request-scoped user context shared across app layers.

Purpose:
* API auth resolves the caller identity once per request.
* Integrations (ClawTeam CLI/MCP) can read that identity without threading
  ``user`` through every intermediate function signature.
"""

from __future__ import annotations

from contextvars import ContextVar

_current_user: ContextVar[str | None] = ContextVar(
    "csflow_current_user",
    default=None,
)


def set_request_user(user: str | None) -> None:
    """Bind the caller user to the current async context."""
    _current_user.set(user)


def get_request_user() -> str | None:
    """Return the user bound to the current async context, if any."""
    return _current_user.get()


__all__ = ["get_request_user", "set_request_user"]
