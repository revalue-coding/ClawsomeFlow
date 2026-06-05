"""FastAPI application factory.

Public API:
* :func:`create_app` — build a fully wired FastAPI ASGI app.
* ``app`` — module-level instance (used by ``uvicorn app.main:app``).

Phase-0 endpoints:
* ``GET /health`` — service liveness + bootstrap snapshot.
* ``GET /version`` — package version.

Future phases register additional routers under ``/api/...`` and ``/ws/...``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import __version__, bootstrap, config as cfg_mod, logging_setup
from app.concurrency import get_lock_manager
from app.deployment import get_deployment_capabilities
from app.runtime_bins import resolve_binary
from app.storage import get_storage


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: bootstrap layout + load config + warm singletons."""
    bootstrap.ensure_data_layout()
    logging_setup.configure_logging()
    log = logging_setup.get_logger("main")

    cfg = cfg_mod.load_config()
    caps = get_deployment_capabilities(cfg)
    cfg_mod.patch_env_from_config(cfg)
    get_lock_manager(cfg)
    get_storage(cfg)  # also runs init_schema()
    if os.environ.get("CSFLOW_DISABLE_COMPLAINT_AUTO_SKIP_WORKER") != "1":
        try:
            from app.scheduler.engine import get_scheduler
            get_scheduler().start_complaint_auto_skip_worker()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("complaint_auto_skip_worker_init_failed", error=str(exc))
    if os.environ.get("CSFLOW_DISABLE_RUN_SCHEDULE_WORKER") != "1":
        try:
            from app.services.run_schedules import get_run_schedule_worker
            get_run_schedule_worker().start()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("run_schedule_worker_init_failed", error=str(exc))

    if (
        caps.auto_spawn_board_proxy
        and os.environ.get("CSFLOW_DISABLE_CLAWTEAM_STACK_CHECK") != "1"
    ):
        runtime_ok, runtime_detail = _probe_clawteam_runtime()
        if not runtime_ok:
            log.error("clawteam_runtime_required_check_failed", error=runtime_detail)
            raise RuntimeError(
                "clawteam runtime readiness check failed: " + runtime_detail
            )
        mcp_ok, mcp_detail = await _probe_clawteam_mcp(cfg.default_user)
        if not mcp_ok:
            log.error("clawteam_mcp_required_check_failed", error=mcp_detail)
            raise RuntimeError(
                "clawteam mcp readiness check failed: " + mcp_detail
            )

    # Phase 9: spawn `clawteam board serve` for local-mode iframe.
    # Skipped when CSFLOW_DISABLE_BOARD=1 (test isolation, server mode).
    if caps.auto_spawn_board_proxy and os.environ.get("CSFLOW_DISABLE_BOARD") != "1":
        from app.board_proxy import get_board_proxy

        board = get_board_proxy(cfg)
        if not board.start():
            detail = board.last_error or "unknown error"
            log.error("board_proxy_required_start_failed", error=detail)
            raise RuntimeError(
                "clawteam board failed to start during csflow startup: "
                + detail
            )

    common_cron_sync_task: asyncio.Task | None = None
    if os.environ.get("CSFLOW_DISABLE_COMMON_CRON_AUTO_SYNC") != "1":
        async def _sync_common_cron_once() -> None:
            try:
                from app.services.openclaw_agents import sync_common_cron_jobs_for_all
                result = await asyncio.to_thread(
                    sync_common_cron_jobs_for_all,
                    user=cfg.default_user,
                    config=cfg,
                )
                synced = sum(1 for ok in result.values() if ok)
                logger = logging_setup.get_logger("main")
                logger.info(
                    "common_cron_autosync_done",
                    total_agents=len(result),
                    synced_agents=synced,
                    failed_agents=max(len(result) - synced, 0),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger = logging_setup.get_logger("main")
                logger.warning("common_cron_autosync_failed", error=str(exc))

        common_cron_sync_task = asyncio.create_task(
            _sync_common_cron_once(),
            name="csflow-common-cron-autosync",
        )

    log.info(
        "app_started",
        version=__version__,
        deployment_mode=cfg.deployment_mode,
        port=cfg.csflow_port,
    )
    try:
        yield
    finally:
        try:
            from app.services.run_schedules import (
                get_run_schedule_worker,
                reset_run_schedule_worker,
            )
            await get_run_schedule_worker().stop()
            reset_run_schedule_worker()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("run_schedule_worker_shutdown_failed", error=str(exc))
        # Drain any in-flight RunControllers before exiting (Phase 5).
        try:
            from app.scheduler.engine import get_scheduler, reset_scheduler
            await get_scheduler().shutdown(timeout=10.0)
            reset_scheduler()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("scheduler_shutdown_failed", error=str(exc))
        # Stop the board proxy if we started one.
        try:
            from app.board_proxy import get_board_proxy, reset_board_proxy
            await get_board_proxy(cfg).stop()
            reset_board_proxy()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("board_proxy_shutdown_failed", error=str(exc))
        try:
            from app.integrations.clawteam_mcp import close_mcp_client
            await close_mcp_client()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("mcp_clients_shutdown_failed", error=str(exc))
        if common_cron_sync_task is not None and not common_cron_sync_task.done():
            common_cron_sync_task.cancel()
            try:
                await common_cron_sync_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        log.info("app_stopped")


def create_app() -> FastAPI:
    """Build the FastAPI ASGI app."""
    app = FastAPI(
        title="ClawsomeFlow",
        description="Vertical agent workflow orchestration on top of ClawTeam + OpenClaw",
        version=__version__,
        lifespan=_lifespan,
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, object]:
        """Liveness probe + on-disk bootstrap snapshot."""
        return {
            "status": "ok",
            "version": __version__,
            "bootstrap": bootstrap.bootstrap_summary().as_dict(),
        }

    @app.get("/version", tags=["meta"])
    async def version() -> dict[str, str]:
        return {"version": __version__}

    # Loopback + bearer-token guard for the public /api surface (OpenClaw
    # gateway paradigm). No-op unless Config.api_token is set (auto-generated at
    # init), so dev/tests are unaffected. Added before routers are exercised;
    # middleware wraps the whole app regardless of registration order.
    from app.api._api_guard import ApiTokenGuardMiddleware
    app.add_middleware(ApiTokenGuardMiddleware)

    # Phase 1: flows CRUD; future phases register more routers here.
    from app.api import register_routers
    register_routers(app)

    # Phase 9: serve the React SPA from frontend/dist (or app/_static when
    # installed as a wheel). Mount last so API routes win every match.
    from app.static import mount_frontend
    mount_frontend(app)

    return app


# Module-level instance for ``uvicorn app.main:app`` and the test client.
app = create_app()


def _probe_clawteam_runtime() -> tuple[bool, str]:
    clawteam_bin = resolve_binary("clawteam")
    if not clawteam_bin:
        return False, "`clawteam` binary not found in PATH"
    proc = subprocess.run(
        [clawteam_bin, "runtime", "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip()
    return False, detail or "non-zero exit from `clawteam runtime --help`"


async def _probe_clawteam_mcp(default_user: str) -> tuple[bool, str]:
    from app.integrations.clawteam_mcp import close_mcp_client, get_mcp_client

    last_error = "unknown mcp failure"
    for attempt in range(1, 3):
        try:
            client = await get_mcp_client(user=default_user)
            await client.team_list()
            return True, ""
        except Exception as exc:  # pragma: no cover - defensive
            last_error = str(exc) or exc.__class__.__name__
            await close_mcp_client(user=default_user)
            if attempt < 2:
                await asyncio.sleep(0.2 * attempt)
    return False, last_error
