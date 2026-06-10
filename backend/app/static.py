"""Frontend-static-asset discovery + FastAPI mount helper.

`csflow start` runs a single uvicorn process that serves both the API
and the React SPA (so the user doesn't have to think about two ports).
This module owns the discovery logic — it tries the three places the
`dist/` could live, in priority order:

1. **Editable dev tree** — ``frontend/dist/`` next to ``backend/`` in the
   repo (the layout this monorepo uses). Picked up automatically when
   developers run ``pip install -e ./backend``.
2. **Bundled wheel resource** — ``app/_static/`` inside the installed
   package. ``hatch build`` will sweep ``frontend/dist/`` here when we
   ship a wheel (configured in ``pyproject.toml``).
3. **Explicit override** — ``$CSFLOW_FRONTEND_DIST`` env var. Useful for
   integration tests pointing at a fixture, or ops scenarios where the
   built SPA lives outside the wheel (e.g. a CDN-served bundle mirrored
   locally).

If none of those resolves, the route falls back to a plain text page
explaining how to run ``npm run build`` (so backend-only deployments
still respond on `/`).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.logging_setup import get_logger

logger = get_logger("static")


_STATIC_OVERRIDE_ENV = "CSFLOW_FRONTEND_DIST"
_PACKAGED_SUBDIR = "_static"


def discover_frontend_dist() -> Path | None:
    """Return the directory containing ``index.html``, or None if absent."""
    # 1. Explicit override.
    override = os.environ.get(_STATIC_OVERRIDE_ENV)
    if override:
        p = Path(override).expanduser()
        if (p / "index.html").exists():
            return p
        logger.warning("static_override_not_found", path=str(p))

    # 2. Editable dev tree: <repo>/frontend/dist
    here = Path(__file__).resolve()
    for ancestor in [here.parents[2], here.parents[3]]:
        cand = ancestor / "frontend" / "dist"
        if (cand / "index.html").exists():
            return cand

    # 3. Bundled in the wheel.
    pkg_static = here.parent / _PACKAGED_SUBDIR
    if (pkg_static / "index.html").exists():
        return pkg_static

    return None


def mount_frontend(app: FastAPI) -> Path | None:
    """Mount ``index.html`` + ``/assets/`` so the SPA is reachable at ``/``.

    Returns the path that was mounted (or None if no dist was found —
    in that case the helper installs a plain-text fallback handler so
    backend-only deployments still respond on ``/``).
    """
    dist = discover_frontend_dist()
    if dist is None:
        @app.get("/", include_in_schema=False)
        async def _missing_static() -> PlainTextResponse:  # noqa: D401
            return PlainTextResponse(
                "ClawsomeFlow backend is running, but no frontend bundle was "
                "found.\n\n"
                "To build it:\n"
                "    cd frontend && npm install && npm run build\n\n"
                "Or set CSFLOW_FRONTEND_DIST to a prebuilt dist directory.\n"
                f"API docs: /docs\n",
            )
        logger.info("frontend_static_missing", message="serving fallback page on /")
        return None

    # HTML shell must not be cached across upgrades: new builds change hashed
    # ``/assets/*`` URLs; a stale ``index.html`` would still request old chunks
    # (often 404).  Hashed assets under ``/assets/`` are safe to cache forever.
    @app.middleware("http")
    async def _frontend_http_cache_headers(request: Request, call_next):  # noqa: D401
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    # Static assets first (cached, hashed filenames).
    app.mount(
        "/assets",
        StaticFiles(directory=str(dist / "assets")),
        name="frontend-assets",
    )
    # SPA-style fallback: anything that isn't /api, /ws, /assets, /docs, etc.
    # serves index.html so React Router can take over.
    index_path = dist / "index.html"
    _index_no_cache = {
        "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "Surrogate-Control": "no-store",
    }

    def _spa_index_response() -> HTMLResponse:
        # Avoid FileResponse's ETag/Last-Modified — with conditional GET some
        # clients could keep pairing a "fresh" 304 with an older cached body.
        body = index_path.read_text(encoding="utf-8")
        return HTMLResponse(body, headers=_index_no_cache)

    @app.get("/", include_in_schema=False, response_model=None)
    async def _root():
        return _spa_index_response()

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def _spa(full_path: str):
        # Anything that the API didn't claim (FastAPI routes are matched
        # in declaration order; this handler is registered last) → SPA.
        # We still 404 for static-asset misses so the browser doesn't load
        # index.html in place of a missing image / font.
        if full_path.startswith(("api/", "ws/", "assets/", "health", "version", "docs", "openapi.json")):
            return PlainTextResponse("not found", status_code=404)
        # Root-level static files shipped in dist (logo.png, favicon, robots.txt,
        # …). ``/assets/*`` is already mounted above; this serves the *root* of
        # the bundle so e.g. ``/logo.png`` returns the PNG instead of falling
        # through to index.html (HTML), which is what broke the sidebar logo and
        # favicon in the packaged wheel. Guard against path traversal by
        # resolving and confirming the file stays inside ``dist``.
        if full_path:
            candidate = (dist / full_path).resolve()
            try:
                candidate.relative_to(dist.resolve())
            except ValueError:
                candidate = None  # escaped the dist root → fall through to SPA
            if candidate is not None and candidate.is_file():
                return FileResponse(candidate)
        return _spa_index_response()

    logger.info("frontend_static_mounted", dist=str(dist))
    return dist


__all__ = ["discover_frontend_dist", "mount_frontend"]
