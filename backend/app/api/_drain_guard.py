"""Reject mutating public API calls while the scheduler is draining.

During ``csflow stop`` / upgrade / restart, :meth:`FlowScheduler.drain_to_terminal`
cooperatively pauses every live run. A concurrent user click (pause / continue /
abort / trigger) can race that finalize and confuse banners or worktrees. This
middleware freezes those writes for the duration of the drain:

* Applies to ``/api/*`` mutating methods (POST/PUT/PATCH/DELETE).
* Leaves ``GET`` / ``HEAD`` / ``OPTIONS`` alone (status polls stay live).
* Leaves ``/api/external/*`` alone — remote receipts must still land so a
  failure that arrives mid-drain is durable for the next start.
* ``/health`` is never under ``/api``, so the WebUI freeze gate can still
  observe ``draining=true``.
"""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_EXTERNAL_PREFIX = "/api/external/"


class ServiceDrainGuardMiddleware:
    """Pure-ASGI middleware: 503 SERVICE_DRAINING on mutating /api during drain."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "") or ""
        method = (scope.get("method") or "GET").upper()
        if (
            path.startswith("/api/")
            and not path.startswith(_EXTERNAL_PREFIX)
            and method not in _SAFE_METHODS
        ):
            try:
                from app.scheduler.engine import get_scheduler
                draining = get_scheduler().is_draining()
            except Exception:  # pragma: no cover - defensive
                draining = False
            if draining:
                denied = JSONResponse(
                    {
                        "error": "SERVICE_DRAINING",
                        "message": (
                            "service is shutting down / upgrading; "
                            "mutating API calls are temporarily rejected"
                        ),
                    },
                    status_code=503,
                )
                await denied(scope, receive, send)
                return
        await self.app(scope, receive, send)


__all__ = ["ServiceDrainGuardMiddleware"]
