"""HTTP routers grouped by resource (per API.md).

Public API:
* :func:`register_routers` — attach all routers to a FastAPI app.

Phase-by-phase additions:
* Phase 1: ``flows``
* Phase 4: ``openclaw_agents``
* Phase 7: ``runs`` + ``profiles`` + ``ws``
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api import errors  # noqa: F401 — registers exception handlers
from app.api import (
    agent_store,
    clawteam_board,
    flows,
    hermes_agents,
    managed_agents,
    openclaw_agents,
    profiles,
    runs,
    system,
    task_decompose,
    ws,
)


def register_routers(app: FastAPI) -> None:
    """Attach all currently-implemented routers to *app*."""
    app.include_router(clawteam_board.router)  # /clawteam-board*
    app.include_router(flows.router, prefix="/api")
    app.include_router(agent_store.router, prefix="/api")
    app.include_router(openclaw_agents.router, prefix="/api")
    app.include_router(hermes_agents.router, prefix="/api")
    app.include_router(managed_agents.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(profiles.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(task_decompose.public_router, prefix="/api")
    app.include_router(task_decompose.internal_router, prefix="/api")
    app.include_router(ws.router)  # /ws/{run_id} (no /api prefix)
    errors.register_exception_handlers(app)


__all__ = ["register_routers"]
