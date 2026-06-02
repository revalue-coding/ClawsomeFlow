"""Authentication for ``/api/internal/*`` endpoints.

Defends the loopback Internal API with **two layers**:

1. **Loopback-only IP filter** — the request must originate from
   ``127.0.0.0/8`` or ``::1``. The OpenClaw skills always call the
   loopback URL; rejecting non-loopback at the framework boundary stops
   any reverse-tunneled / proxied call from sneaking in.

2. **Short-lived bearer token** — the ``Authorization: Bearer <token>``
   header must be a valid HMAC-signed token (see
   :mod:`app.integrations.internal_token`).

Both checks must pass; either failure returns ``401`` (we deliberately
don't tell the caller which one failed).
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Annotated

from fastapi import Depends, Header, Request

from app.api.errors import ApiError
from app.integrations import internal_token as it


#: Hosts considered safe to call the Internal API. ``testclient`` is the
#: Starlette ``TestClient`` default (so unit tests don't need to spoof IPs);
#: real production callers always come from 127.0.0.1 / ::1.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _is_loopback(ip: str) -> bool:
    if ip in _LOOPBACK_HOSTS:
        return True
    if ip.startswith("127."):
        return True
    try:
        return ip_address(ip).is_loopback
    except ValueError:
        return False


def require_loopback(request: Request) -> None:
    """Reject requests that didn't come from the loopback interface."""
    client = request.client
    if client is None or not _is_loopback(client.host):
        # Generic message — don't leak the policy.
        raise ApiError(
            "UNAUTHORIZED",
            "internal API is restricted to loopback callers",
            status_code=401,
        )


def require_internal_token(
    authorization: Annotated[str | None, Header()] = None,
) -> it.TokenClaims:
    """Verify the Bearer token; return its claims for downstream handlers."""
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(
            "UNAUTHORIZED",
            "missing bearer token",
            status_code=401,
        )
    raw = authorization.removeprefix("Bearer ").strip()
    try:
        return it.verify_token(raw)
    except it.InvalidToken as exc:
        raise ApiError(
            "UNAUTHORIZED", f"token rejected: {exc}", status_code=401,
        ) from exc


def require_internal_caller(
    _: Annotated[None, Depends(require_loopback)],
    claims: Annotated[it.TokenClaims, Depends(require_internal_token)],
) -> it.TokenClaims:
    """Composite dependency: enforces both layers + returns claims."""
    return claims


InternalCallerDep = Annotated[it.TokenClaims, Depends(require_internal_caller)]


__all__ = [
    "InternalCallerDep",
    "require_internal_caller",
    "require_internal_token",
    "require_loopback",
]
