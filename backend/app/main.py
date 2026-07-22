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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__, bootstrap, logging_setup
from app import config as cfg_mod
from app.concurrency import get_lock_manager
from app.runtime_bins import resolve_binary
from app.storage import get_storage


def _sweep_orphaned_runs(storage, log) -> int:
    """Reconcile leftover ACTIVE_DRIVING runs to the terminal ``orphaned`` state.

    Called once at startup. We never resume runs, so a run still in
    pending/compiling/running/awaiting_user_checkpoint/complaint_processing has
    lost its driving process (e.g. the previous backend was SIGKILLed before a
    graceful drain). Mark it terminal + stamp ``finished_at`` + append an audit
    event; do NOT run any termination/cleanup flow. Idempotent.
    """
    from datetime import datetime, timezone

    from app.models import RunEvent, RunStatus

    orphaned = 0
    for run in storage.list_active_driving_runs():
        previous = run.status.value if hasattr(run.status, "value") else str(run.status)
        run.status = RunStatus.orphaned
        if run.finished_at is None:
            run.finished_at = datetime.now(timezone.utc)
        storage.run_update(run)
        try:
            storage.event_append(RunEvent(
                run_id=run.id,
                type="run_orphaned",
                payload={"reason": "startup_orphan_sweep", "previous_status": previous},
            ))
        except Exception:  # pragma: no cover - audit event is best-effort
            pass
        orphaned += 1
    if orphaned:
        log.info("orphan_sweep_done", orphaned=orphaned)
    return orphaned


def _resume_unattended_paused_runs(storage, log) -> int:
    """Auto-resume runs a pre-stop DRAIN parked, that have NO human in the loop.

    Called once at startup, after the orphan sweep. The pre-stop drain parks
    every in-flight run as ``paused`` (backend never terminates); a **manual**
    run then waits for the user to click 继续执行, but an **unattended** run
    (scheduled / MCP / delegated) has no human to do that — so we resume it
    automatically so it drives to a terminal status and its result / delegate
    callback / webhook fires. This is what lets a delegated (remote-triggered) or
    scheduled run survive a ClawsomeFlow restart and still complete + return.

    IMPORTANT: only runs whose pause **reason is ``drain``** are auto-resumed. An
    unattended run that a human explicitly PAUSED (reason ``user``) is a
    deliberate intervention and must be left paused for the operator — we never
    override a manual pause, even for an unattended run. (Genuine failures never
    reach ``paused`` for unattended runs — they terminate — so ``drain`` is the
    only auto-resumable reason.) Idempotent + best-effort.
    """
    from app.models import RunStatus
    from app.scheduler.engine import get_scheduler
    from app.scheduler.run_metadata import (
        PAUSE_REASON_DRAIN,
        read_pause_state,
        run_is_unattended,
    )

    sched = get_scheduler()
    resumed = 0
    offset = 0
    while True:
        items, _total = storage.run_list(
            status=RunStatus.paused.value, limit=200, offset=offset,
        )
        if not items:
            break
        for run in items:
            if not run_is_unattended(run):
                continue  # manual run → wait for the user's 继续执行
            blob = read_pause_state(run)
            if not blob or blob.get("reason") != PAUSE_REASON_DRAIN:
                continue  # human-paused (or unknown) → respect the manual pause
            flow = storage.flow_get(run.flow_id)
            if flow is None:
                continue
            try:
                sched.resume_run(run=run, flow=flow, storage=storage)
                resumed += 1
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("unattended_paused_resume_failed", run_id=run.id, error=str(exc))
        if len(items) < 200:
            break
        offset += len(items)
    if resumed:
        log.info("unattended_paused_resumed", resumed=resumed)
    return resumed


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: bootstrap layout + load config + warm singletons."""
    bootstrap.ensure_data_layout()
    logging_setup.configure_logging()
    log = logging_setup.get_logger("main")

    cfg = cfg_mod.load_config()
    cfg_mod.patch_env_from_config(cfg)
    get_lock_manager(cfg)
    get_storage(cfg)  # also runs init_schema()
    # Startup orphan reconciliation: any run still in an ACTIVE_DRIVING state in
    # the DB has no live process behind it (we do NOT resume runs), so reconcile
    # it to the terminal ``orphaned`` status. Runs only change status — no
    # termination/cleanup flow. PRESERVED states (awaiting_user_review/complaint)
    # survive losslessly and are left untouched. Gated for the TestClient suite.
    if os.environ.get("CSFLOW_DISABLE_ORPHAN_SWEEP") != "1":
        try:
            _sweep_orphaned_runs(get_storage(), log)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("orphan_sweep_failed", error=str(exc))
        # Auto-resume unattended (scheduled / MCP / delegated) paused runs so a
        # run with no human in the loop still completes + returns after a restart.
        # Manual paused runs are left for the user's 继续执行.
        try:
            _resume_unattended_paused_runs(get_storage(), log)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("unattended_paused_resume_sweep_failed", error=str(exc))
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

    if os.environ.get("CSFLOW_DISABLE_CLAWTEAM_STACK_CHECK") != "1":
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

    # Spawn `clawteam board serve` for the WebUI iframe.
    # Skipped when CSFLOW_DISABLE_BOARD=1 (test isolation).
    if os.environ.get("CSFLOW_DISABLE_BOARD") != "1":
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
        # Pre-stop drain: gracefully terminate every ACTIVE_DRIVING run to a
        # terminal state (aborted/orphaned) before the process exits, so no run
        # is left dangling and no DB record stays non-terminal. This is the
        # universal chokepoint every stop/restart/upgrade path funnels through.
        try:
            from app.scheduler.engine import get_scheduler, reset_scheduler
            await get_scheduler().drain_to_terminal()
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
        # Kill any leftover bootstrap/create subprocess groups so no residual
        # session/bootstrap processes survive a graceful stop / restart / uninstall.
        try:
            from app.services import subprocess_registry
            subprocess_registry.terminate_all()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("subprocess_registry_shutdown_failed", error=str(exc))
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
