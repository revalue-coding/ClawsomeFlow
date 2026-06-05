"""Loopback + bearer-token guard for the public ``/api`` surface.

Mirrors the OpenClaw gateway paradigm (loopback bind + mandatory token). The
guard activates **only** when ``Config.api_token`` is set (auto-generated at
init, stored privately in ``~/.clawsomeflow/config.json``). When the token is
unset the guard is a complete no-op, so local dev and the test-suite — which
never set a token — behave exactly as before.

Who is allowed when the guard is active (request must be to ``/api/*`` but not
``/api/internal/*`` — the latter keeps its own minted-token auth):

1. **Host allowlist** (anti DNS-rebinding): the ``Host`` header must resolve to
   a loopback hostname, else ``403``.
2. **Valid token** → allow. External callers send ``Authorization: Bearer
   <api_token>`` (or ``X-API-Key: <api_token>``). This is the path your local
   external service uses.
3. **Same-origin browser SPA** → allow without a token, so the bundled WebUI
   keeps working untouched. Detected via Fetch-Metadata (``Sec-Fetch-Site:
   same-origin|none``) with an ``Origin``/``Referer`` loopback fallback for
   older browsers. (A non-browser client could forge these headers, but a
   malicious process running as the same OS user can already read config.json
   directly — that case is out of scope for any in-process auth.)
4. Otherwise → ``401``.

The WebSocket surface (``/ws/*``) is not under ``/api`` and is therefore not
gated here; it remains loopback-only.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.config import load_config

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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


def _is_guarded_path(path: str) -> bool:
    # Gate the public API only; /api/internal/* has its own minted-token auth.
    return path.startswith("/api/") and not path.startswith("/api/internal/")


class ApiTokenGuardMiddleware:
    """Pure-ASGI middleware enforcing :func:`evaluate` on guarded HTTP paths."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not _is_guarded_path(scope.get("path", "")):
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


__all__ = ["ApiTokenGuardMiddleware", "evaluate"]
