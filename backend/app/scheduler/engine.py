"""FlowScheduler — the singleton that owns all RunControllers in this process.

A simple in-memory dict of run_id → RunController + asyncio.Task (the whole
product is a single-process local deployment).

Public API:

* :func:`get_scheduler` / :func:`reset_scheduler` — singleton lifecycle.
* :meth:`FlowScheduler.start_run` — compile the Flow into a fresh ClawTeam
  team + tasks (Phase 6 :func:`compile_flow_to_clawteam`), wire up the
  default MCP-backed providers, and hand off to a background ``asyncio.Task``
  running the controller's ``run_loop``.
* :meth:`FlowScheduler.cancel_run` — cooperative cancel.
* :meth:`FlowScheduler.shutdown` — wait for all running controllers to
  finish (called from FastAPI lifespan on shutdown).

The scheduler intentionally **does not** create the Run row or validate the
Flow — those happen in the API layer (Phase 7). It DOES own the
"Flow → ClawTeam team+tasks" compilation so the API doesn't have to.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.logging_setup import get_logger
from app.models import Flow, FlowRun, FlowSpec, RunStatus
from app.scheduler.compiler import compile_flow_to_clawteam
from app.scheduler.controller import RunController, RunOutcome
from app.scheduler.finalize import run_terminal_tail_cleanup
from app.scheduler.providers import (
    McpLeaderInboxProvider,
    McpSnapshotProvider,
)
from app.scheduler.run_metadata import (
    PAUSE_REASON_DRAIN as _PAUSE_REASON_DRAIN,
)
from app.scheduler.run_metadata import (
    POST_COMPLAINT_STATUS_KEY as _POST_COMPLAINT_STATUS_KEY,
)
from app.scheduler.run_metadata import (
    POST_REVIEW_TERMINAL_STATUS_KEY as _POST_REVIEW_TERMINAL_STATUS_KEY,
)
from app.storage import StorageBackend, get_storage

logger = get_logger("scheduler.engine")

_COMPLAINT_AUTO_SKIP_TIMEOUT = timedelta(hours=12)
_COMPLAINT_AUTO_SKIP_POLL_SEC = 60.0
_COMPLAINT_AUTO_SKIP_BATCH = 200

# Bounded grace window for the pre-stop drain's best-effort session/team
# cleanup. Kept comfortably below systemd ``TimeoutStopSec=30`` so the actual
# service stop is never blocked even if cleanup hangs (DB is already terminal
# by then). See cli/_user_service.py.
_DRAIN_CLEANUP_TIMEOUT_SEC = 15.0


def abort_run_to_terminal(
    run: FlowRun,
    *,
    sched: FlowScheduler,
    storage: StorageBackend,
    final_status: RunStatus = RunStatus.aborted,
) -> bool:
    """Flip *run* to a terminal state instantly and signal cooperative cancel.

    Shared by the run-abort button (``api/runs.py``) and the pre-stop drain
    (:meth:`FlowScheduler.drain_to_terminal`) so "stop terminates the run" and
    "user clicks 中止执行流" go through exactly one code path.

    The DB flip is synchronous + immediate (so the record is terminal even if
    the process is SIGKILLed a moment later). Returns whether a live scheduler
    task (controller loop or complaint task) was signalled to cancel — callers
    with no live task may want to run inline cleanup themselves.
    """
    from app.scheduler.run_metadata import PAUSE_STATE_KEY as _PAUSE_STATE_KEY
    cancelled = sched.cancel_run(run.id)
    run.status = final_status
    run.pending_merges = None
    merged_inputs = dict(run.inputs or {})
    merged_inputs.pop(_POST_COMPLAINT_STATUS_KEY, None)
    merged_inputs.pop(_POST_REVIEW_TERMINAL_STATUS_KEY, None)
    merged_inputs.pop(_PAUSE_STATE_KEY, None)
    run.inputs = merged_inputs
    if run.finished_at is None:
        run.finished_at = datetime.now(timezone.utc)
    storage.run_update(run)
    return cancelled


@dataclass
class _Entry:
    controller: RunController
    task: asyncio.Task


class FlowScheduler:
    """Process-wide manager of in-flight :class:`RunController` instances."""

    def __init__(self) -> None:
        self._runs: dict[str, _Entry] = {}
        self._complaints: dict[str, asyncio.Task] = {}
        self._complaint_auto_skip_task: asyncio.Task | None = None
        self._complaint_auto_skip_stop_evt = asyncio.Event()
        self._stopped = False
        # Set for the duration of :meth:`drain_to_terminal`. The complaint
        # supervisor's cancel handler checks it: a drain-cancel reverts to the
        # PRESERVED ``awaiting_user_complaint`` (backend never terminates), while
        # a user-triggered cancel still aborts.
        self._draining = False

    # ── lifecycle ───────────────────────────────────────────────────

    def start_run(
        self,
        *,
        run: FlowRun,
        spec: FlowSpec,
        flow: Flow | None = None,
        flow_description: str = "",
        compile: bool = True,
        storage: StorageBackend | None = None,
    ) -> RunController:
        """Create a controller for *run* and schedule its loop. Idempotent.

        If ``compile=True`` (default) the controller's ``run_loop`` will
        first call :func:`compile_flow_to_clawteam` to materialise the
        ClawTeam team + tasks, then wire up MCP-backed providers. Set
        ``compile=False`` for unit-test paths that prefer to inject their
        own snapshot provider.

        ``flow`` is required for ``compile=True`` because the team_name
        is derived from the Run id and ``cleanup_team_on_finish`` lives
        on the Flow row.
        """
        if self._stopped:
            raise RuntimeError("FlowScheduler is stopped — refusing new runs")
        if run.id in self._runs:
            return self._runs[run.id].controller

        controller = RunController(
            run=run, spec=spec, flow=flow, flow_description=flow_description,
            storage=storage,
        )
        task = asyncio.create_task(
            self._supervise(
                run.id, controller, do_compile=compile, storage=storage,
            ),
            name=f"csflow-run:{run.id}",
        )
        self._runs[run.id] = _Entry(controller=controller, task=task)
        logger.info("scheduler_start_run", run_id=run.id, compile=compile)
        return controller

    def resume_run(
        self,
        *,
        run: FlowRun,
        flow: Flow,
        storage: StorageBackend | None = None,
    ) -> RunController:
        """Resume a ``paused`` run: rebuild a controller and re-drive its DAG.

        Rebuilds a :class:`RunController` from the persisted ``run`` +
        ``flow.spec`` (same pattern as the complaint phase) and schedules its
        loop with ``resume=True`` so the supervisor rewires providers against the
        EXISTING ClawTeam team (no recompile — that would duplicate tasks) and
        reuses on-disk worktrees. Idempotent: a run already live returns its
        controller unchanged.
        """
        if self._stopped:
            raise RuntimeError("FlowScheduler is stopped — refusing resume")
        entry = self._runs.get(run.id)
        if entry is not None and not entry.task.done():
            return entry.controller
        controller = RunController(
            run=run,
            spec=FlowSpec.model_validate(flow.spec),
            flow=flow,
            flow_description=flow.description,
            storage=storage,
        )
        task = asyncio.create_task(
            self._supervise(
                run.id, controller, do_compile=False, storage=storage, resume=True,
            ),
            name=f"csflow-resume:{run.id}",
        )
        self._runs[run.id] = _Entry(controller=controller, task=task)
        logger.info("scheduler_resume_run", run_id=run.id)
        return controller

    def cancel_run(self, run_id: str) -> bool:
        """Politely ask a Run (or complaint phase) to stop."""
        handled = False
        entry = self._runs.get(run_id)
        if entry is not None and not entry.task.done():
            entry.controller.cancel()
            handled = True
        complaint = self._complaints.get(run_id)
        if complaint is not None and not complaint.done():
            complaint.cancel()
            handled = True
        return handled

    def get_controller(self, run_id: str) -> RunController | None:
        entry = self._runs.get(run_id)
        return entry.controller if entry else None

    def active_runs(self) -> list[str]:
        return [rid for rid, e in self._runs.items() if not e.task.done()]

    def complaint_in_progress(self, run_id: str) -> bool:
        task = self._complaints.get(run_id)
        return bool(task and not task.done())

    def start_complaint_auto_skip_worker(self) -> None:
        """Start background sweeper for stale awaiting_user_complaint runs."""
        if self._stopped:
            return
        if self._complaint_auto_skip_task is not None and not self._complaint_auto_skip_task.done():
            return
        self._complaint_auto_skip_stop_evt = asyncio.Event()
        self._complaint_auto_skip_task = asyncio.create_task(
            self._complaint_auto_skip_loop(),
            name="csflow-complaint-auto-skip",
        )

    def start_run_complaint_phase(
        self,
        *,
        run: FlowRun,
        flow: Flow,
        complaint_text: str,
        storage: StorageBackend | None = None,
    ) -> None:
        if self._stopped:
            raise RuntimeError("FlowScheduler is stopped — refusing complaint task")
        task = self._complaints.get(run.id)
        if task is not None and not task.done():
            raise RuntimeError(f"complaint phase already running for run {run.id}")
        controller = self.get_controller(run.id)
        if controller is None:
            controller = RunController(
                run=run,
                spec=FlowSpec.model_validate(flow.spec),
                flow=flow,
                flow_description=flow.description,
                storage=storage,
            )
        job = asyncio.create_task(
            self._supervise_complaint(
                run_id=run.id,
                controller=controller,
                complaint_text=complaint_text,
                storage=storage,
            ),
            name=f"csflow-complaint:{run.id}",
        )
        self._complaints[run.id] = job

    def start_run_skip_complaint_phase(
        self,
        *,
        run: FlowRun,
        flow: Flow,
        storage: StorageBackend | None = None,
    ) -> None:
        """Start background "very satisfied" complaint-skip workflow."""
        if self._stopped:
            raise RuntimeError("FlowScheduler is stopped — refusing complaint task")
        task = self._complaints.get(run.id)
        if task is not None and not task.done():
            raise RuntimeError(f"complaint phase already running for run {run.id}")
        controller = self.get_controller(run.id)
        if controller is None:
            controller = RunController(
                run=run,
                spec=FlowSpec.model_validate(flow.spec),
                flow=flow,
                flow_description=flow.description,
                storage=storage,
            )
        job = asyncio.create_task(
            self._supervise_complaint(
                run_id=run.id,
                controller=controller,
                complaint_text=None,
                storage=storage,
            ),
            name=f"csflow-complaint-skip:{run.id}",
        )
        self._complaints[run.id] = job

    async def drain_to_terminal(
        self,
        *,
        storage: StorageBackend | None = None,
        timeout: float = _DRAIN_CLEANUP_TIMEOUT_SEC,
    ) -> dict[str, int]:
        """Park every ACTIVE_DRIVING run before the service stops.

        Universal pre-stop drain (invoked from the FastAPI lifespan shutdown,
        which is the single chokepoint every stop/restart/upgrade path funnels
        SIGTERM through). **The backend never terminates a run** — every run it
        drives is parked RESUMABLY, never aborted:

        * **Live pre-review run (RunController):** signal a cooperative
          :meth:`RunController.pause` (reason ``drain``). The controller's loop
          exits into the NON-destructive finalize path — worktrees + ClawTeam
          team preserved, interrupted tasks reset to pending, status →
          ``paused`` — and we wait (bounded) for it below. Paused controllers are
          NOT cancelled (cancel == terminate).
        * **In-progress complaint phase:** cancel the complaint task; its
          drain-cancel handler reverts the run to the PRESERVED
          ``awaiting_user_complaint`` so the user can re-submit after restart.
        * **Residual (no live driver — previous SIGKILL, or loop already
          finished):** reconcile to the terminal ``orphaned`` — an accepted
          SIGKILL-class degradation, since we cannot cleanly pause without a
          controller.

        PRESERVED non-terminal states (``paused`` / ``awaiting_user_review`` /
        ``awaiting_user_complaint``) are not in ACTIVE_DRIVING and so are left
        untouched. On timeout the wait only warns — the service stop is never
        blocked; a run whose pause finalize did not complete stays ``running``
        in the DB and is swept to ``orphaned`` at next startup.
        """
        store = storage or get_storage()
        self._stopped = True
        self._draining = True
        self._complaint_auto_skip_stop_evt.set()

        paused = 0
        reverted = 0
        orphaned = 0
        try:
            active_runs = store.list_active_driving_runs()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("drain_list_active_failed", error=str(exc))
            active_runs = []
        for run in active_runs:
            if self.complaint_in_progress(run.id):
                # Handled by the complaint task's drain-cancel handler below.
                reverted += 1
                continue
            entry = self._runs.get(run.id)
            if entry is not None and not entry.task.done():
                try:
                    entry.controller.pause(
                        reason=_PAUSE_REASON_DRAIN,
                        detail="service stop / upgrade drain",
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("drain_pause_failed", run_id=run.id, error=str(exc))
                    continue
                paused += 1
                continue
            # Residual — no live driver. Reconcile to orphaned (SIGKILL-class).
            try:
                run.status = RunStatus.orphaned
                if run.finished_at is None:
                    run.finished_at = datetime.now(timezone.utc)
                store.run_update(run)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("drain_orphan_failed", run_id=run.id, error=str(exc))
                continue
            orphaned += 1

        # Phase 2: cancel complaint tasks (→ drain-revert) + the auto-skip
        # worker, then wait (bounded) for the paused controllers to finish their
        # non-destructive finalize. Do NOT cancel paused run controllers.
        for task in self._complaints.values():
            task.cancel()
        auto_task = self._complaint_auto_skip_task
        if auto_task is not None:
            auto_task.cancel()
        waits: list[asyncio.Task] = [
            e.task for e in self._runs.values() if not e.task.done()
        ]
        waits += [t for t in self._complaints.values() if not t.done()]
        if auto_task is not None and not auto_task.done():
            waits.append(auto_task)
        if waits:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*waits, return_exceptions=True), timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "drain_wait_timeout",
                    pending=[rid for rid, e in self._runs.items() if not e.task.done()],
                )
        self._complaint_auto_skip_task = None
        self._draining = False

        logger.info(
            "scheduler_drained", paused=paused, reverted=reverted, orphaned=orphaned,
        )
        return {"paused": paused, "reverted": reverted, "orphaned": orphaned}

    async def shutdown(self, *, timeout: float = 30.0) -> None:
        """Cancel every active Run and wait up to *timeout* for them to exit."""
        self._stopped = True
        self._complaint_auto_skip_stop_evt.set()
        for entry in self._runs.values():
            entry.controller.cancel()
        for task in self._complaints.values():
            task.cancel()
        auto_task = self._complaint_auto_skip_task
        if auto_task is not None:
            auto_task.cancel()
        if not self._runs and not self._complaints and auto_task is None:
            return
        try:
            waits: list[asyncio.Task] = [
                *(e.task for e in self._runs.values()),
                *self._complaints.values(),
            ]
            if auto_task is not None:
                waits.append(auto_task)
            await asyncio.wait_for(
                asyncio.gather(
                    *waits,
                    return_exceptions=True,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "scheduler_shutdown_timeout",
                pending=[rid for rid, e in self._runs.items() if not e.task.done()],
            )
        finally:
            self._complaint_auto_skip_task = None

    async def sweep_stale_awaiting_user_complaints(
        self,
        *,
        storage: StorageBackend | None = None,
        now: datetime | None = None,
    ) -> int:
        """Auto-finish stale awaiting_user_complaint runs as "very satisfied"."""
        store = storage or get_storage()
        current = now or datetime.now(timezone.utc)
        auto_skipped = 0
        offset = 0
        while True:
            items, _total = store.run_list(
                status=RunStatus.awaiting_user_complaint.value,
                limit=_COMPLAINT_AUTO_SKIP_BATCH,
                offset=offset,
            )
            if not items:
                break
            for run in items:
                if self.complaint_in_progress(run.id):
                    continue
                marker = run.finished_at or run.started_at
                marker_utc = (
                    marker
                    if marker.tzinfo is not None
                    else marker.replace(tzinfo=timezone.utc)
                )
                if current - marker_utc < _COMPLAINT_AUTO_SKIP_TIMEOUT:
                    continue
                flow = store.flow_get(run.flow_id)
                if flow is None:
                    self._emit_run_event(
                        store,
                        run_id=run.id,
                        event_type="run_complaint_auto_skip_failed",
                        payload={
                            "reason": "flow_not_found",
                            "flow_id": run.flow_id,
                        },
                    )
                    continue
                controller = self.get_controller(run.id)
                if controller is None:
                    controller = RunController(
                        run=run,
                        spec=FlowSpec.model_validate(flow.spec),
                        flow=flow,
                        flow_description=flow.description,
                        storage=store,
                    )
                try:
                    await controller.skip_user_complaint_phase()
                except Exception as exc:
                    logger.warning(
                        "run_complaint_auto_skip_failed",
                        run_id=run.id,
                        error=str(exc),
                    )
                    self._emit_run_event(
                        store,
                        run_id=run.id,
                        event_type="run_complaint_auto_skip_failed",
                        payload={"reason": "exception", "error": str(exc)[:1000]},
                    )
                    continue
                auto_skipped += 1
                self._emit_run_event(
                    store,
                    run_id=run.id,
                    event_type="run_complaint_auto_skipped",
                    payload={
                        "reason": "timeout_12h",
                        "timeout_seconds": int(_COMPLAINT_AUTO_SKIP_TIMEOUT.total_seconds()),
                        "waited_seconds": int((current - marker_utc).total_seconds()),
                    },
                )
            if len(items) < _COMPLAINT_AUTO_SKIP_BATCH:
                break
            offset += len(items)
        return auto_skipped

    # ── internal ────────────────────────────────────────────────────

    async def _complaint_auto_skip_loop(self) -> None:
        logger.info(
            "run_complaint_auto_skip_worker_started",
            timeout_seconds=int(_COMPLAINT_AUTO_SKIP_TIMEOUT.total_seconds()),
        )
        while not self._stopped and not self._complaint_auto_skip_stop_evt.is_set():
            try:
                await self.sweep_stale_awaiting_user_complaints()
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(
                    "run_complaint_auto_skip_tick_failed",
                    error=str(exc),
                )
            try:
                await asyncio.wait_for(
                    self._complaint_auto_skip_stop_evt.wait(),
                    timeout=_COMPLAINT_AUTO_SKIP_POLL_SEC,
                )
            except asyncio.TimeoutError:
                pass
        logger.info("run_complaint_auto_skip_worker_stopped")

    def _emit_run_event(
        self,
        store: StorageBackend,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        from app.events import publish_run_event
        publish_run_event(
            store,
            run_id=run_id,
            event_type=event_type,
            payload=dict(payload),
        )

    async def _supervise(
        self, run_id: str, controller: RunController,
        *, do_compile: bool, storage: StorageBackend | None,
        resume: bool = False,
    ) -> RunOutcome:
        try:
            if resume:
                # Resume a paused run: rewire providers against the EXISTING
                # ClawTeam team (never recompile — that would duplicate tasks)
                # and seed sessions to reuse existing worktrees.
                await self._rewire_existing_team(controller, storage=storage)
                await controller.prepare_resume()
            elif do_compile:
                await self._compile_and_wire(controller, storage=storage)
            outcome = await controller.run_loop()
        except Exception as exc:
            logger.exception(
                "run_loop_uncaught_exception", run_id=run_id, error=str(exc),
            )
            # This outer handler catches a COMPILE / wiring failure (before the
            # run_loop starts) or a truly-uncaught escape from run_loop. A compile
            # failure means the ClawTeam team + tasks were never fully set up, so
            # there is nothing to resume — mark the run ``failed`` and force
            # cleanup. (In-execution scheduler errors are scenario 9 and are
            # handled INSIDE run_loop, which PAUSES the run resumably instead.)
            store = storage or get_storage()
            try:
                if controller.run.status not in (
                    RunStatus.completed,
                    RunStatus.completed_with_conflicts,
                    RunStatus.complaint_failed,
                    RunStatus.failed,
                    RunStatus.aborted,
                    RunStatus.paused,
                ):
                    controller.run.status = RunStatus.failed
                    if controller.run.finished_at is None:
                        controller.run.finished_at = datetime.now(timezone.utc)
                    store.run_update(controller.run)
                from app.events import publish_run_event
                publish_run_event(
                    store,
                    run_id=controller.run.id,
                    event_type="run_uncaught_exception",
                    payload={"error": str(exc)[:1000]},
                )
            except Exception as persist_exc:  # pragma: no cover — defensive
                logger.warning(
                    "run_uncaught_exception_persist_failed",
                    run_id=run_id,
                    error=str(persist_exc),
                )
            try:
                await run_terminal_tail_cleanup(
                    run=controller.run,
                    flow=controller.flow,
                    agents=controller.spec.agents,
                    storage=store,
                    worktree_lookup=controller.worktree_lookup,
                )
            except Exception as cleanup_exc:  # pragma: no cover — defensive
                logger.warning(
                    "run_uncaught_exception_tail_cleanup_failed",
                    run_id=run_id,
                    error=str(cleanup_exc),
                )
            return RunOutcome(
                final_status=controller.run.status,
                completed_task_ids=[],
                failed_task_ids=[],
                skipped_task_ids=[],
                reason=f"uncaught_exception:{exc}",
            )
        else:
            logger.info(
                "scheduler_run_finished",
                run_id=run_id,
                final_status=outcome.final_status.value,
                completed=len(outcome.completed_task_ids),
                failed=len(outcome.failed_task_ids),
                skipped=len(outcome.skipped_task_ids),
            )
            return outcome
        finally:
            self._runs.pop(run_id, None)

    async def _compile_and_wire(
        self, controller: RunController, *, storage: StorageBackend | None,
    ) -> None:
        """Run Phase 6 compilation + plug in default MCP providers.

        Sets ``Run.status`` from ``pending → compiling → running`` so the UI
        sees the transition. ``RunController.run_loop`` flips it to
        ``running`` itself when the loop starts.
        """
        from app.integrations.clawteam_mcp import get_mcp_client
        store = storage or get_storage()
        # pending → compiling
        if controller.run.status == RunStatus.pending:
            controller.run.status = RunStatus.compiling
            try:
                store.run_update(controller.run)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("run_status_compile_persist_failed", error=str(exc))
        # Compile to ClawTeam.
        cr = await compile_flow_to_clawteam(
            spec=controller.spec,
            team_name=controller.team_name,
            user=controller.run.user,
            flow_description=controller.flow_description,
        )
        controller.compile_result = cr
        # Wire up MCP-backed providers (controller already has a DispatchClock).
        mcp = await get_mcp_client(user=controller.run.user)
        if controller._snapshot_provider is None:
            controller._snapshot_provider = McpSnapshotProvider(
                team_name=controller.team_name,
                compile_result=cr,
                mcp=mcp,
                dispatch_clock=controller.dispatch_clock,
            )
        if controller._leader_inbox_provider is None:
            controller._leader_inbox_provider = McpLeaderInboxProvider(
                team_name=controller.team_name,
                leader_agent_id=controller._leader_id,
                mcp=mcp,
                peek=True,
            )

    async def _rewire_existing_team(
        self, controller: RunController, *, storage: StorageBackend | None,
    ) -> None:
        """Wire MCP providers against an EXISTING ClawTeam team (resume path).

        Reconstructs the ``CompileResult`` task-id mapping from the live team's
        tasks — every ClawTeam task the compiler created carries
        ``metadata.csflow_task_id`` precisely so the mapping survives a restart —
        then plugs in the same snapshot / leader-inbox providers ``start_run``
        uses. Does NOT recompile (``compile_flow_to_clawteam`` is not idempotent
        and would duplicate the team + tasks).
        """
        from app.integrations.clawteam_mcp import get_mcp_client
        from app.scheduler.compiler import CSFLOW_TASK_ID_KEY, CompileResult

        mcp = await get_mcp_client(user=controller.run.user)
        try:
            rows = await mcp.task_list(controller.team_name)
        except Exception as exc:
            raise RuntimeError(
                f"resume: cannot read existing team {controller.team_name}: {exc}"
            ) from exc
        flow_to_clawteam: dict[str, str] = {}
        clawteam_to_flow: dict[str, str] = {}
        for row in rows:
            meta = row.get("metadata") or {}
            flow_id = meta.get(CSFLOW_TASK_ID_KEY)
            ct_id = row.get("id")
            if isinstance(flow_id, str) and isinstance(ct_id, str):
                flow_to_clawteam[flow_id] = ct_id
                clawteam_to_flow[ct_id] = flow_id
        cr = CompileResult(
            team_name=controller.team_name,
            leader_agent_id=controller._leader_id,
            flow_to_clawteam=flow_to_clawteam,
            clawteam_to_flow=clawteam_to_flow,
            member_count=len(controller.spec.agents),
        )
        controller.compile_result = cr
        if controller._snapshot_provider is None:
            controller._snapshot_provider = McpSnapshotProvider(
                team_name=controller.team_name,
                compile_result=cr,
                mcp=mcp,
                dispatch_clock=controller.dispatch_clock,
            )
        if controller._leader_inbox_provider is None:
            controller._leader_inbox_provider = McpLeaderInboxProvider(
                team_name=controller.team_name,
                leader_agent_id=controller._leader_id,
                mcp=mcp,
                peek=True,
            )

    async def _supervise_complaint(
        self,
        *,
        run_id: str,
        controller: RunController,
        complaint_text: str | None,
        storage: StorageBackend | None,
    ) -> None:
        store = storage or get_storage()
        try:
            if complaint_text is None:
                await controller.skip_user_complaint_phase()
            else:
                await controller.run_user_complaint_phase(complaint_text=complaint_text)
        except asyncio.CancelledError:
            # A pre-stop drain cancel must NOT terminate (backend never
            # terminates): revert to the PRESERVED awaiting_user_complaint so the
            # user can re-submit after the restart. A user-triggered cancel
            # (not draining) still aborts.
            if self._draining:
                logger.info("run_complaint_phase_drain_reverted", run_id=run_id)
                try:
                    await controller._shutdown_remaining_sessions(reason="run_finalize")
                except Exception:
                    pass
                try:
                    controller.run.status = RunStatus.awaiting_user_complaint
                    controller.run.finished_at = None
                    store.run_update(controller.run)
                    from app.events import publish_run_event
                    publish_run_event(
                        store,
                        run_id=controller.run.id,
                        event_type="run_complaint_phase_drain_reverted",
                        payload={"reason": "drain"},
                    )
                except Exception as persist_exc:  # pragma: no cover - defensive
                    logger.warning(
                        "run_complaint_phase_drain_revert_persist",
                        run_id=run_id,
                        error=str(persist_exc),
                    )
                return
            logger.info("run_complaint_phase_cancelled", run_id=run_id)
            try:
                await controller._shutdown_remaining_sessions(reason="run_abort")
            except Exception:
                pass
            try:
                controller.run.status = RunStatus.aborted
                if controller.run.finished_at is None:
                    controller.run.finished_at = datetime.now(timezone.utc)
                store.run_update(controller.run)
                from app.events import publish_run_event
                publish_run_event(
                    store,
                    run_id=controller.run.id,
                    event_type="run_complaint_phase_cancelled",
                    payload={"reason": "user_abort"},
                )
                try:
                    await run_terminal_tail_cleanup(
                        run=controller.run,
                        flow=controller.flow,
                        agents=controller.spec.agents,
                        storage=store,
                        worktree_lookup=controller.worktree_lookup,
                        preserve_worktree_dirs=controller.run.status not in {
                            RunStatus.completed,
                            RunStatus.completed_with_conflicts,
                        },
                    )
                except Exception as cleanup_exc:  # pragma: no cover — defensive
                    logger.warning(
                        "run_complaint_phase_cancelled_tail_cleanup_failed",
                        run_id=run_id,
                        error=str(cleanup_exc),
                    )
            except Exception as persist_exc:
                logger.warning(
                    "run_complaint_phase_cancelled_persist",
                    run_id=run_id,
                    error=str(persist_exc),
                )
            return
        except Exception as exc:
            logger.exception("run_complaint_phase_failed", run_id=run_id, error=str(exc))
            try:
                await controller._shutdown_remaining_sessions(reason="run_finalize")
            except Exception:
                pass
            try:
                # Complaint phase failed: mark terminal as complaint_failed
                # while still keeping the detailed failure internal-only.
                controller.run.status = RunStatus.complaint_failed
                if controller.run.finished_at is None:
                    controller.run.finished_at = datetime.now(timezone.utc)
                store.run_update(controller.run)
                try:
                    await run_terminal_tail_cleanup(
                        run=controller.run,
                        flow=controller.flow,
                        agents=controller.spec.agents,
                        storage=store,
                        worktree_lookup=controller.worktree_lookup,
                        preserve_worktree_dirs=True,
                    )
                except Exception as cleanup_exc:  # pragma: no cover — defensive
                    logger.warning(
                        "run_complaint_phase_failed_tail_cleanup_failed",
                        run_id=run_id,
                        error=str(cleanup_exc),
                    )
            except Exception as persist_exc:
                logger.warning(
                    "run_complaint_phase_failed_silent_finalize",
                    run_id=run_id,
                    error=str(persist_exc),
                )
        finally:
            self._complaints.pop(run_id, None)


# ── singleton ──────────────────────────────────────────────────────

_singleton: FlowScheduler | None = None


def get_scheduler() -> FlowScheduler:
    global _singleton
    if _singleton is None:
        _singleton = FlowScheduler()
    return _singleton


def reset_scheduler() -> None:
    global _singleton
    _singleton = None


__all__ = ["FlowScheduler", "get_scheduler", "reset_scheduler"]
