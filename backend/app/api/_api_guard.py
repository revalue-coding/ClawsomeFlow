"""Network guard for the whole app surface (peer-symmetric security model).

Every ClawsomeFlow instance is identical (no hub / no central gateway), so
the same two laws apply on every peer:

**Law 1 — remote clients may reach ONLY ``/api/external/*``.** The service
binds ``0.0.0.0`` so peers can collaborate with zero setup, but a connection
whose *source IP* is not loopback is rejected on every other surface (main
``/api``, ``/ws``, SPA, health) with ``403 REMOTE_NOT_ALLOWED``. The
collaboration surface itself carries its own narrow credentials (one-time
ticket / pairing secret — see ``app.api.external``), which is why it may stay
open. ``csflow external expose off`` locks even that surface back to
loopback-only. Client-IP checks cannot be forged remotely (unlike ``Host`` /
``Sec-Fetch-Site`` headers); local port-forwards (SSH ``-L``, vite dev proxy)
still present a loopback source and keep working.

**Law 2 — the bearer-token guard for local ``/api`` callers.** Activates
**only** when ``Config.api_token`` is set (auto-generated at init, stored
privately in ``~/.clawsomeflow/config.json``); when unset it is a complete
no-op so dev and the test-suite behave as before. For guarded paths
(``/api/*`` minus ``/api/internal/*`` which keeps its own minted-token auth):

1. **Host allowlist** (anti DNS-rebinding): the ``Host`` header must resolve
   to a loopback hostname, else ``403``.
2. **Valid token** → allow (``Authorization: Bearer <api_token>`` or
   ``X-API-Key``).
3. **Same-origin browser SPA** → allow without a token (Fetch-Metadata /
   ``Origin`` / ``Referer``). Safe because Law 1 already guarantees the
   client socket is loopback — a remote caller can never reach this branch,
   and a malicious same-OS-user process can read config.json anyway (OS
   boundary, out of scope).
4. Otherwise → ``401``.

WebSocket scopes (``/ws/*``) are covered by Law 1 (loopback clients only).
"""

from __future__ import annotations

import hmac
from ipaddress import ip_address
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import load_config

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _client_is_loopback(scope) -> bool:
    """True when the transport peer is the local machine.

    ``testclient`` is Starlette's TestClient default. ``client is None``
    (in-process / unix-socket transports) counts as local.
    """
    client = scope.get("client")
    if client is None:
        return True
    host = str(client[0])
    if host in ("testclient", "localhost"):
        return True
    if host.startswith("127."):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname_of_host_header(value: str) -> str:
    """Extract the bare hostname from a ``Host`` header (strip port / brackets)."""
    h = (value or "").strip()
    if not h:
        return ""
    if h.startswith("["):  # IPv6 literal: [::1]:17017
        end = h.find("]")
        return h[1:end] if end != -1 else h
    if ":" in h:
        h = h.rsplit(":", 1)[0]
    return h


def _hostname_of_url(value: str) -> str:
    """Extract the hostname from an absolute URL (Origin / Referer)."""
    try:
        return (urlparse(value).hostname or "").strip()
    except Exception:
        return ""


def _presented_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    xkey = request.headers.get("x-api-key")
    if xkey and xkey.strip():
        return xkey.strip()
    return None


def _deny(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status_code)


def _is_same_origin_browser(request: Request) -> bool:
    sec_fetch_site = request.headers.get("sec-fetch-site")
    if sec_fetch_site in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin:
        return _hostname_of_url(origin) in _LOOPBACK_HOSTS
    # No Origin and no Fetch-Metadata: fall back to Referer (older browsers).
    if sec_fetch_site is None:
        referer = request.headers.get("referer")
        if referer:
            return _hostname_of_url(referer) in _LOOPBACK_HOSTS
    return False


def evaluate(request: Request, api_token: str) -> Response | None:
    """Return a denial ``Response`` or ``None`` to allow. Assumes *api_token*
    is truthy (caller checks the no-op case)."""
    host = _hostname_of_host_header(request.headers.get("host", ""))
    if host and host not in _LOOPBACK_HOSTS:
        return _deny("HOST_NOT_ALLOWED", "API restricted to loopback host", 403)

    presented = _presented_token(request)
    if presented and hmac.compare_digest(presented, api_token):
        return None

    if _is_same_origin_browser(request):
        return None

    if presented:
        return _deny("UNAUTHENTICATED", "invalid API token", 401)
    return _deny(
        "UNAUTHENTICATED",
        "missing API token (send 'Authorization: Bearer <api_token>')",
        401,
    )


# External-execution collaboration surface: its endpoints carry their own
# per-task ticket / pairing-credential auth (see app.api.external), so the
# global bearer/same-origin rules do NOT apply here — only the Host rule,
# which defaults open (``Config.external_api_expose=True``) because the
# credential gate is sufficient. ``expose off`` re-locks to loopback-only.
_EXTERNAL_PREFIX = "/api/external/"


def _is_guarded_path(path: str) -> bool:
    # Gate the public API only; /api/internal/* has its own minted-token auth
    # and /api/external/* has its own ticket auth + dedicated Host rule.
    return (
        path.startswith("/api/")
        and not path.startswith("/api/internal/")
        and not path.startswith(_EXTERNAL_PREFIX)
    )


def evaluate_external(
    request: Request,
    *,
    expose_enabled: bool,
    client_is_loopback: bool = True,
) -> Response | None:
    """Access rule for ``/api/external/*``: open by default.

    Remote callers are allowed when ``Config.external_api_expose`` is True
    (the default — the surface is credential-gated). ``expose off`` re-locks
    to loopback-only: both a non-loopback ``Host`` and a non-loopback client
    socket are then rejected (the socket check cannot be forged). Token
    verification is the endpoint's own job (one-time ticket / pairing
    credential).
    """
    if expose_enabled:
        return None
    host = _hostname_of_host_header(request.headers.get("host", ""))
    if (host and host not in _LOOPBACK_HOSTS) or not client_is_loopback:
        return _deny(
            "HOST_NOT_ALLOWED",
            "external API surface is locked to loopback "
            "(config.external_api_expose=false; "
            "'csflow external expose on' to re-open)",
            403,
        )
    return None


class ApiTokenGuardMiddleware:
    """Pure-ASGI middleware enforcing the two laws in the module docstring."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        stype = scope.get("type")

        if stype == "websocket":
            # Law 1 for /ws/*: loopback clients only.
            if not _client_is_loopback(scope):
                await receive()  # consume "websocket.connect"
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return

        if stype != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        if path.startswith(_EXTERNAL_PREFIX):
            cfg = load_config()
            request = Request(scope, receive=receive)
            denied = evaluate_external(
                request,
                # Default open when the key is somehow absent (matches Config).
                expose_enabled=bool(getattr(cfg, "external_api_expose", True)),
                client_is_loopback=_client_is_loopback(scope),
            )
            if denied is not None:
                await denied(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # Law 1: everything except /api/external/* is local-only. A source-IP
        # check — remote callers cannot forge it (Host/Sec-Fetch-Site can be).
        if not _client_is_loopback(scope):
            denied = _deny(
                "REMOTE_NOT_ALLOWED",
                "only /api/external/* is remote-reachable; "
                "this surface accepts loopback connections only "
                "(use an SSH tunnel for remote administration)",
                403,
            )
            await denied(scope, receive, send)
            return

        if not _is_guarded_path(path):
            await self.app(scope, receive, send)
            return

        # Cheap no-op when no token configured (dev / tests / opted-out users).
        api_token = getattr(load_config(), "api_token", None)
        if not api_token:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        denied = evaluate(request, api_token)
        if denied is not None:
            await denied(scope, receive, send)
            return
        await self.app(scope, receive, send)


__all__ = ["ApiTokenGuardMiddleware", "evaluate", "evaluate_external"]
