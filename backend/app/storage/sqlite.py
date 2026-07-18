"""SQLite-backed implementation of :class:`StorageBackend`.

Notes:
* Uses synchronous SQLAlchemy / SQLModel under the hood. FastAPI handlers
  call these from async context via ``run_in_threadpool`` if needed; the
  scheduler hot path queries are short and pinned to a single Run, so
  in-thread execution is fine.
* SQLite WAL mode is enabled so the scheduler can read while an HTTP write
  is in flight.
* All ``datetime`` values are stored as ISO-8601 UTC; SQLAlchemy handles
  conversion via the column type.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy import update as sa_update
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, delete, func, select

from app import paths
from app.logging_setup import get_logger
from app.models import (
    ACTIVE_DRIVING_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AgentStoreOrder,
    AgentStoreOwnership,
    Flow,
    FlowRun,
    FlowRunSchedule,
    FlowRunScheduleExecution,
    HermesAgent,
    OpenclawAgent,
    OpenclawAgentRequest,
    OpenclawRequestStatus,
    OpenclawTeam,
    RunEvent,
    RunStatus,
    TaskDecomposeRequest,
    TaskDecomposeStatus,
)
from app.storage import StorageVersionConflict

logger = get_logger("storage.sqlite")


def _enable_wal(engine: Engine) -> None:
    """Enable WAL on every new SQLite connection (concurrency-friendly)."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # pragma: no cover - exec at connect time
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


# Single source of truth lives in app.models (TERMINAL_RUN_STATUSES).
_TERMINAL_STATUSES: frozenset[RunStatus] = TERMINAL_RUN_STATUSES
_ACTIVE_DRIVING_STATUSES: frozenset[RunStatus] = ACTIVE_DRIVING_RUN_STATUSES


class SqliteStorage:
    """Implements :class:`StorageBackend` over the local SQLite db."""

    def __init__(self, *, url: str | None = None) -> None:
        # ``check_same_thread=False`` is safe because we always wrap mutations
        # in a Session (per-call) rather than sharing connection state.
        self._url = url or f"sqlite:///{paths.db_path()}"
        self._engine = create_engine(
            self._url,
            connect_args={"check_same_thread": False},
            future=True,
        )
        _enable_wal(self._engine)

    # ---- Lifecycle ----

    def init_schema(self) -> None:
        SQLModel.metadata.create_all(self._engine)
        self._ensure_openclaw_agent_team_column()
        self._ensure_flowrun_is_scheduled_column()
        # Product baseline (May 2026): cleanup_team_on_finish is always enabled.
        # Backfill legacy rows created before this baseline.
        with self._session() as s:
            s.exec(
                sa_update(Flow)
                .where(Flow.cleanup_team_on_finish.is_(False))
                .values(cleanup_team_on_finish=True)
            )
            s.commit()

    def close(self) -> None:
        self._engine.dispose()

    # Internal helper -- explicit session so callers don't worry about cleanup.
    def _session(self) -> Session:
        return Session(self._engine, expire_on_commit=False)

    def _ensure_openclaw_agent_team_column(self) -> None:
        """Add ``openclawagent.team_id`` for legacy DBs created pre-team feature."""
        with self._engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info('openclawagent')").fetchall()
            columns = {str(r[1]) for r in rows}
            if "team_id" in columns:
                return
            conn.exec_driver_sql(
                "ALTER TABLE openclawagent "
                "ADD COLUMN team_id VARCHAR NOT NULL DEFAULT ''"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_openclawagent_team_id "
                "ON openclawagent(team_id)"
            )

    def _ensure_flowrun_is_scheduled_column(self) -> None:
        """Add ``flowrun.is_scheduled`` for legacy DBs created pre-schedule flag."""
        with self._engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info('flowrun')").fetchall()
            columns = {str(r[1]) for r in rows}
            if "is_scheduled" in columns:
                return
            conn.exec_driver_sql(
                "ALTER TABLE flowrun "
                "ADD COLUMN is_scheduled BOOLEAN NOT NULL DEFAULT 0"
            )

    # ---- Flows ----

    def flow_create(self, flow: Flow) -> Flow:
        with self._session() as s:
            s.add(flow)
            s.commit()
            s.refresh(flow)
            return flow

    def flow_get(self, flow_id: str) -> Flow | None:
        with self._session() as s:
            return s.get(Flow, flow_id)

    def flow_list(
        self,
        *,
        owner_user: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Flow], int]:
        with self._session() as s:
            stmt = select(Flow)
            count_stmt = select(func.count()).select_from(Flow)
            if owner_user:
                stmt = stmt.where(Flow.owner_user == owner_user)
                count_stmt = count_stmt.where(Flow.owner_user == owner_user)
            if q:
                like = f"%{q}%"
                stmt = stmt.where((Flow.name.ilike(like)) | (Flow.description.ilike(like)))
                count_stmt = count_stmt.where(
                    (Flow.name.ilike(like)) | (Flow.description.ilike(like))
                )
            stmt = stmt.order_by(Flow.updated_at.desc()).offset(offset).limit(limit)
            items = list(s.exec(stmt).all())
            total = s.exec(count_stmt).one()
            return items, int(total)

    def flow_update(self, flow: Flow, *, expected_version: int) -> Flow:
        with self._session() as s:
            current = s.get(Flow, flow.id)
            if current is None:
                raise KeyError(flow.id)
            if current.version != expected_version:
                raise StorageVersionConflict(
                    flow_id=flow.id,
                    expected=expected_version,
                    actual=current.version,
                )
            # Apply mutable fields from `flow` onto the row, bump version + updated_at.
            current.name = flow.name
            current.description = flow.description
            current.cleanup_team_on_finish = flow.cleanup_team_on_finish
            current.spec = flow.spec
            current.version = expected_version + 1
            current.updated_at = datetime.now(timezone.utc)
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def flow_delete(self, flow_id: str) -> bool:
        with self._session() as s:
            flow = s.get(Flow, flow_id)
            if flow is None:
                return False
            # FlowRun.flow_id and RunEvent.run_id are foreign-key linked.
            # Purge terminal run history before deleting Flow so old completed
            # runs don't block the delete request on FK constraints.
            run_ids = list(
                s.exec(
                    select(FlowRun.id)
                    .where(FlowRun.flow_id == flow_id)
                    .where(FlowRun.status.in_([st.value for st in _TERMINAL_STATUSES]))
                ).all()
            )
            if run_ids:
                s.exec(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                s.exec(delete(FlowRun).where(FlowRun.id.in_(run_ids)))
            s.delete(flow)
            s.commit()
            return True

    # ---- FlowRuns ----

    def run_create(self, run: FlowRun) -> FlowRun:
        with self._session() as s:
            s.add(run)
            s.commit()
            s.refresh(run)
            return run

    def run_get(self, run_id: str) -> FlowRun | None:
        with self._session() as s:
            return s.get(FlowRun, run_id)

    def run_list(
        self,
        *,
        flow_id: str | None = None,
        status: str | None = None,
        user: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FlowRun], int]:
        with self._session() as s:
            stmt = select(FlowRun)
            count_stmt = select(func.count()).select_from(FlowRun)
            if flow_id:
                stmt = stmt.where(FlowRun.flow_id == flow_id)
                count_stmt = count_stmt.where(FlowRun.flow_id == flow_id)
            if status:
                stmt = stmt.where(FlowRun.status == status)
                count_stmt = count_stmt.where(FlowRun.status == status)
            if user:
                stmt = stmt.where(FlowRun.user == user)
                count_stmt = count_stmt.where(FlowRun.user == user)
            stmt = stmt.order_by(FlowRun.started_at.desc()).offset(offset).limit(limit)
            items = list(s.exec(stmt).all())
            total = s.exec(count_stmt).one()
            return items, int(total)

    def run_update(self, run: FlowRun) -> FlowRun:
        # Webhook hook (app.services.run_notify): run_update is the single
        # choke point every status flip goes through, so the notify decisions
        # (terminal: dedupe marker stamped into run.inputs in the same commit;
        # checkpoint: fires only on the transition into a waiting-for-user
        # state, detected against the previously persisted status) are made
        # here — and the actual POST fires on a daemon thread afterwards.
        # Full no-op unless the Flow has webhook channels configured
        # (spec.variables[csflow.notify_webhooks]).
        from app.services.external_tasks import (
            prepare_delegate_callback,
            send_delegate_callback,
        )
        from app.services.run_notify import (
            flow_channels_for_run,
            prepare_checkpoint_notification,
            prepare_terminal_notification,
            send_run_notification,
        )

        # The webhook must NEVER affect the run itself: every notify step is
        # guarded so a failure can't abort the commit or raise into the
        # scheduler, and the actual POST fires on a daemon thread (below) so
        # it can never block task execution. prepare_* already swallow their
        # own errors; the outer guards are belt-and-suspenders. Channels are
        # loaded ONCE here (own session, before ours opens) from the CURRENT
        # Flow so live config edits + scheduled runs both take effect.
        try:
            channels = flow_channels_for_run(run)
        except Exception as exc:  # pragma: no cover — defensive
            self._log_notify_guard(exc)
            channels = []
        try:
            notification = (
                prepare_terminal_notification(run, channels=channels)
                if channels else None
            )
        except Exception as exc:  # pragma: no cover — defensive
            self._log_notify_guard(exc)
            notification = None
        # Delegated-run result callback (external execution nodes): decision +
        # dedupe marker land in the same commit, POST fires on a daemon thread
        # after — exactly the run_notify pattern (same single choke point).
        try:
            delegate_callback = prepare_delegate_callback(run)
        except Exception as exc:  # pragma: no cover — defensive
            self._log_notify_guard(exc)
            delegate_callback = None
        with self._session() as s:
            current = s.get(FlowRun, run.id)
            if current is None:
                raise KeyError(run.id)
            if notification is None and channels:
                try:
                    notification = prepare_checkpoint_notification(
                        run, old_status=current.status, channels=channels,
                    )
                except Exception as exc:  # pragma: no cover — defensive
                    self._log_notify_guard(exc)
                    notification = None
            for field in (
                "status", "inputs", "finished_at", "pending_merges",
            ):
                setattr(current, field, getattr(run, field))
            s.add(current)
            s.commit()
            s.refresh(current)
        if notification is not None:
            try:
                send_run_notification(notification)
            except Exception as exc:  # pragma: no cover — defensive
                self._log_notify_guard(exc)
        if delegate_callback is not None:
            try:
                send_delegate_callback(delegate_callback)
            except Exception as exc:  # pragma: no cover — defensive
                self._log_notify_guard(exc)
        return current

    @staticmethod
    def _log_notify_guard(exc: Exception) -> None:
        """The webhook path must never affect the run — swallow + log."""
        logger.warning("run_notify_guard_swallowed", error=str(exc))

    def run_count_active_for_flow(self, flow_id: str) -> int:
        with self._session() as s:
            stmt = (
                select(func.count())
                .select_from(FlowRun)
                .where(FlowRun.flow_id == flow_id)
                .where(FlowRun.status.notin_([s.value for s in _TERMINAL_STATUSES]))
            )
            return int(s.exec(stmt).one())

    def list_active_driving_runs(self) -> list[FlowRun]:
        """Runs in an ACTIVE_DRIVING state (need a live process to progress).

        Used by the startup orphan sweep and the pre-stop drain. Excludes the
        PRESERVED non-terminal states (awaiting_user_review/complaint), which
        survive a restart losslessly.
        """
        with self._session() as s:
            stmt = (
                select(FlowRun)
                .where(
                    FlowRun.status.in_(
                        [st.value for st in _ACTIVE_DRIVING_STATUSES]
                    )
                )
                .order_by(FlowRun.started_at.desc())
            )
            return list(s.exec(stmt).all())

    def count_active_driving_runs(self) -> int:
        """Cheap count of ACTIVE_DRIVING runs (for upgrade/CLI guards)."""
        with self._session() as s:
            stmt = (
                select(func.count())
                .select_from(FlowRun)
                .where(
                    FlowRun.status.in_(
                        [st.value for st in _ACTIVE_DRIVING_STATUSES]
                    )
                )
            )
            return int(s.exec(stmt).one())

    def run_count_active_for_openclaw_agent(self, agent_id: str) -> int:
        """Active = the agent appears in any non-terminal Run's spec.

        Scans non-terminal runs and inspects spec.agents, loading each
        distinct Flow once (many runs share a flow — avoids the former
        per-run ``s.get(Flow, ...)`` N+1). Server mode P1 introduces a
        join table.
        """
        with self._session() as s:
            stmt = (
                select(FlowRun)
                .where(FlowRun.status.notin_([st.value for st in _TERMINAL_STATUSES]))
            )
            runs = list(s.exec(stmt).all())
            if not runs:
                return 0
            flow_ids = {run.flow_id for run in runs}
            flow_stmt = select(Flow).where(Flow.id.in_(flow_ids))
            matching_flow_ids = {
                flow.id
                for flow in s.exec(flow_stmt).all()
                if any(
                    a.get("kind") == "openclaw" and a.get("id") == agent_id
                    for a in flow.spec.get("agents", [])
                )
            }
            return sum(1 for run in runs if run.flow_id in matching_flow_ids)

    def run_schedule_create(self, schedule: FlowRunSchedule) -> FlowRunSchedule:
        with self._session() as s:
            s.add(schedule)
            s.commit()
            s.refresh(schedule)
            return schedule

    def run_schedule_get(self, schedule_id: str) -> FlowRunSchedule | None:
        with self._session() as s:
            return s.get(FlowRunSchedule, schedule_id)

    def run_schedule_list(self, *, user: str | None = None) -> list[FlowRunSchedule]:
        with self._session() as s:
            stmt = select(FlowRunSchedule)
            if user:
                stmt = stmt.where(FlowRunSchedule.user == user)
            stmt = stmt.order_by(FlowRunSchedule.next_run_at.asc(), FlowRunSchedule.created_at.asc())
            return list(s.exec(stmt).all())

    def run_schedule_list_due(
        self, *, before: datetime, limit: int = 50,
    ) -> list[FlowRunSchedule]:
        with self._session() as s:
            stmt = (
                select(FlowRunSchedule)
                .where(FlowRunSchedule.next_run_at <= before)
                .order_by(FlowRunSchedule.next_run_at.asc(), FlowRunSchedule.created_at.asc())
                .limit(limit)
            )
            return list(s.exec(stmt).all())

    def run_schedule_update(self, schedule: FlowRunSchedule) -> FlowRunSchedule:
        with self._session() as s:
            current = s.get(FlowRunSchedule, schedule.id)
            if current is None:
                raise KeyError(schedule.id)
            for field in (
                "name",
                "run_mode",
                "execute_mode",
                "interval_days",
                "next_run_at",
                "items",
            ):
                setattr(current, field, getattr(schedule, field))
            current.updated_at = datetime.now(timezone.utc)
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def run_schedule_delete(self, schedule_id: str) -> bool:
        with self._session() as s:
            schedule = s.get(FlowRunSchedule, schedule_id)
            if schedule is None:
                return False
            s.delete(schedule)
            s.commit()
            return True

    def run_schedule_execution_create(
        self, execution: FlowRunScheduleExecution,
    ) -> FlowRunScheduleExecution:
        with self._session() as s:
            s.add(execution)
            s.commit()
            s.refresh(execution)
            return execution

    def run_schedule_execution_get(
        self, execution_id: str,
    ) -> FlowRunScheduleExecution | None:
        with self._session() as s:
            return s.get(FlowRunScheduleExecution, execution_id)

    def run_schedule_execution_list(
        self,
        *,
        user: str | None = None,
        schedule_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FlowRunScheduleExecution], int]:
        with self._session() as s:
            stmt = select(FlowRunScheduleExecution)
            cnt = select(func.count()).select_from(FlowRunScheduleExecution)
            if user:
                stmt = stmt.where(FlowRunScheduleExecution.user == user)
                cnt = cnt.where(FlowRunScheduleExecution.user == user)
            if schedule_id:
                stmt = stmt.where(FlowRunScheduleExecution.schedule_id == schedule_id)
                cnt = cnt.where(FlowRunScheduleExecution.schedule_id == schedule_id)
            total = int(s.exec(cnt).one())
            rows = list(
                s.exec(
                    stmt.order_by(FlowRunScheduleExecution.started_at.desc())
                    .limit(limit)
                    .offset(offset)
                ).all()
            )
            return rows, total

    def run_schedule_execution_clear(self, *, user: str | None = None) -> int:
        """Delete finished schedule-execution records (history). In-flight
        (``status == "running"``) records are preserved. Scoped to ``user`` when
        given. Returns the number of records deleted."""
        with self._session() as s:
            stmt = select(FlowRunScheduleExecution).where(
                FlowRunScheduleExecution.status != "running"
            )
            if user:
                stmt = stmt.where(FlowRunScheduleExecution.user == user)
            ids = [row.id for row in s.exec(stmt).all()]
            if ids:
                s.exec(
                    delete(FlowRunScheduleExecution).where(
                        FlowRunScheduleExecution.id.in_(ids)
                    )
                )
                s.commit()
            return len(ids)

    def run_schedule_execution_reap_orphans(self) -> int:
        """Reconcile orphaned in-flight schedule executions to a terminal state.

        Every schedule execution runs as an in-process asyncio task, so any row
        still ``status == "running"`` at worker start belongs to a previous
        process that has since died — it can never reach a terminal status on
        its own and would otherwise (a) display forever and (b) be skipped by
        :meth:`run_schedule_execution_clear` (which preserves genuinely-running
        rows). Mark them ``failed`` with ``finished_at`` set so they become
        clearable. Returns the number of rows reaped."""
        reaped = 0
        with self._session() as s:
            rows = list(
                s.exec(
                    select(FlowRunScheduleExecution).where(
                        FlowRunScheduleExecution.status == "running"
                    )
                ).all()
            )
            if not rows:
                return 0
            now = datetime.now(timezone.utc)
            for row in rows:
                results = list(row.item_results or [])
                next_index = max(
                    (int(r.get("index", -1)) for r in results), default=-1
                ) + 1
                results.append(
                    {
                        "index": next_index,
                        "flow_id": "",
                        "flow_name": "",
                        "status": "failed",
                        "reason": "service restarted before execution finished",
                        "reason_code": "worker_interrupted",
                        "run_id": "",
                    }
                )
                row.item_results = results
                row.status = "failed"
                row.failed_items = sum(
                    1 for r in results if r.get("status") == "failed"
                )
                row.succeeded_items = sum(
                    1 for r in results if r.get("status") == "succeeded"
                )
                row.skipped_items = sum(
                    1 for r in results if r.get("status") == "skipped"
                )
                row.total_items = len(results)
                row.finished_at = now
                s.add(row)
                reaped += 1
            s.commit()
        return reaped

    def run_schedule_execution_update(
        self, execution: FlowRunScheduleExecution,
    ) -> FlowRunScheduleExecution:
        with self._session() as s:
            current = s.get(FlowRunScheduleExecution, execution.id)
            if current is None:
                raise KeyError(execution.id)
            for field in (
                "schedule_name",
                "run_mode",
                "execute_mode",
                "status",
                "total_items",
                "succeeded_items",
                "failed_items",
                "skipped_items",
                "run_ids",
                "item_results",
                "finished_at",
            ):
                setattr(current, field, getattr(execution, field))
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    # ---- RunEvents ----

    def event_append(self, event: RunEvent) -> RunEvent:
        with self._session() as s:
            s.add(event)
            s.commit()
            s.refresh(event)
            return event

    def event_list(
        self, *, run_id: str, since_id: int | None = None, limit: int = 100,
    ) -> list[RunEvent]:
        with self._session() as s:
            stmt = select(RunEvent).where(RunEvent.run_id == run_id)
            if since_id is not None:
                stmt = stmt.where(RunEvent.id > since_id)
            stmt = stmt.order_by(RunEvent.id).limit(limit)
            return list(s.exec(stmt).all())

    def history_cleanup(self, *, before: datetime | None = None) -> dict[str, object]:
        """Delete old terminal execution history to reclaim disk space.

        Scope:
        - terminal FlowRun rows + their RunEvent rows
        - finished OpenClaw NL-create request rows
        - finished task-decompose request rows
        """
        with self._session() as s:
            run_stmt = select(FlowRun).where(
                FlowRun.status.in_([st.value for st in _TERMINAL_STATUSES])
            )
            if before is not None:
                run_stmt = run_stmt.where(
                    ((FlowRun.finished_at.is_not(None)) & (FlowRun.finished_at < before))
                    | ((FlowRun.finished_at.is_(None)) & (FlowRun.started_at < before))
                )
            run_rows = list(s.exec(run_stmt).all())
            run_ids = [r.id for r in run_rows]

            events_deleted = 0
            if run_ids:
                events_deleted = int(s.exec(
                    select(func.count())
                    .select_from(RunEvent)
                    .where(RunEvent.run_id.in_(run_ids))
                ).one())
                s.exec(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                s.exec(delete(FlowRun).where(FlowRun.id.in_(run_ids)))

            terminal_openclaw_req = [
                OpenclawRequestStatus.succeeded.value,
                OpenclawRequestStatus.failed.value,
                OpenclawRequestStatus.timed_out.value,
            ]
            req_stmt = select(OpenclawAgentRequest).where(
                OpenclawAgentRequest.status.in_(terminal_openclaw_req)
            )
            if before is not None:
                req_stmt = req_stmt.where(OpenclawAgentRequest.updated_at < before)
            req_rows = list(s.exec(req_stmt).all())
            req_ids = [r.request_id for r in req_rows]
            if req_ids:
                s.exec(delete(OpenclawAgentRequest).where(
                    OpenclawAgentRequest.request_id.in_(req_ids)
                ))

            terminal_decompose_req = [
                TaskDecomposeStatus.succeeded.value,
                TaskDecomposeStatus.failed.value,
                TaskDecomposeStatus.timed_out.value,
            ]
            td_stmt = select(TaskDecomposeRequest).where(
                TaskDecomposeRequest.status.in_(terminal_decompose_req)
            )
            if before is not None:
                td_stmt = td_stmt.where(TaskDecomposeRequest.updated_at < before)
            td_rows = list(s.exec(td_stmt).all())
            td_ids = [r.request_id for r in td_rows]
            if td_ids:
                s.exec(delete(TaskDecomposeRequest).where(
                    TaskDecomposeRequest.request_id.in_(td_ids)
                ))

            s.commit()
            return {
                "deleted_run_ids": run_ids,
                "runs_deleted": len(run_ids),
                "events_deleted": events_deleted,
                "openclaw_requests_deleted": len(req_ids),
                "task_decompose_requests_deleted": len(td_ids),
            }

    def run_clear_history(self, *, user: str | None = None) -> dict[str, int]:
        """Delete terminal (finished) ``FlowRun`` rows + their ``RunEvent`` rows.

        Active (non-terminal) runs are always preserved so an in-progress Flow is
        never destroyed. Scoped to ``user`` when given. Returns counts of deleted
        runs/events."""
        with self._session() as s:
            run_stmt = select(FlowRun).where(
                FlowRun.status.in_([st.value for st in _TERMINAL_STATUSES])
            )
            if user:
                run_stmt = run_stmt.where(FlowRun.user == user)
            run_ids = [r.id for r in s.exec(run_stmt).all()]
            events_deleted = 0
            if run_ids:
                events_deleted = int(
                    s.exec(
                        select(func.count())
                        .select_from(RunEvent)
                        .where(RunEvent.run_id.in_(run_ids))
                    ).one()
                )
                s.exec(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                s.exec(delete(FlowRun).where(FlowRun.id.in_(run_ids)))
                s.commit()
            return {"runs_deleted": len(run_ids), "events_deleted": events_deleted}

    # ---- OpenclawAgents ----

    def openclaw_team_create(self, team: OpenclawTeam) -> OpenclawTeam:
        with self._session() as s:
            s.add(team)
            s.commit()
            s.refresh(team)
            return team

    def openclaw_team_get(self, team_id: str) -> OpenclawTeam | None:
        with self._session() as s:
            return s.get(OpenclawTeam, team_id)

    def openclaw_team_list(self, *, owner_user: str | None = None) -> list[OpenclawTeam]:
        with self._session() as s:
            stmt = select(OpenclawTeam)
            if owner_user:
                stmt = stmt.where(OpenclawTeam.created_by_user == owner_user)
            stmt = stmt.order_by(OpenclawTeam.created_at.desc())
            return list(s.exec(stmt).all())

    def openclaw_team_update(self, team: OpenclawTeam) -> OpenclawTeam:
        with self._session() as s:
            current = s.get(OpenclawTeam, team.id)
            if current is None:
                raise KeyError(team.id)
            current.name = team.name
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def openclaw_create(self, agent: OpenclawAgent) -> OpenclawAgent:
        with self._session() as s:
            s.add(agent)
            s.commit()
            s.refresh(agent)
            return agent

    def openclaw_get(self, agent_id: str) -> OpenclawAgent | None:
        with self._session() as s:
            return s.get(OpenclawAgent, agent_id)

    def openclaw_list(self, *, owner_user: str | None = None) -> list[OpenclawAgent]:
        with self._session() as s:
            stmt = select(OpenclawAgent)
            if owner_user:
                stmt = stmt.where(OpenclawAgent.created_by_user == owner_user)
            stmt = stmt.order_by(OpenclawAgent.created_at.desc())
            return list(s.exec(stmt).all())

    def openclaw_update(self, agent: OpenclawAgent) -> OpenclawAgent:
        with self._session() as s:
            current = s.get(OpenclawAgent, agent.id)
            if current is None:
                raise KeyError(agent.id)
            for field in ("name", "description", "team_id", "openclaw_config_snapshot"):
                setattr(current, field, getattr(agent, field))
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def openclaw_delete(self, agent_id: str) -> bool:
        with self._session() as s:
            agent = s.get(OpenclawAgent, agent_id)
            if agent is None:
                return False
            s.delete(agent)
            s.commit()
            return True

    # ---- HermesAgents ----

    def hermes_create(self, agent: HermesAgent) -> HermesAgent:
        with self._session() as s:
            s.add(agent)
            s.commit()
            s.refresh(agent)
            return agent

    def hermes_get(self, agent_id: str) -> HermesAgent | None:
        with self._session() as s:
            return s.get(HermesAgent, agent_id)

    def hermes_list(self, *, owner_user: str | None = None) -> list[HermesAgent]:
        with self._session() as s:
            stmt = select(HermesAgent)
            if owner_user:
                stmt = stmt.where(HermesAgent.created_by_user == owner_user)
            stmt = stmt.order_by(HermesAgent.created_at.desc())
            return list(s.exec(stmt).all())

    def hermes_update(self, agent: HermesAgent) -> HermesAgent:
        with self._session() as s:
            current = s.get(HermesAgent, agent.id)
            if current is None:
                raise KeyError(agent.id)
            for field in ("name", "description", "team_id"):
                setattr(current, field, getattr(agent, field))
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def hermes_delete(self, agent_id: str) -> bool:
        with self._session() as s:
            agent = s.get(HermesAgent, agent_id)
            if agent is None:
                return False
            s.delete(agent)
            s.commit()
            return True

    # ---- OpenclawAgentRequests ----

    # ---- AgentStore ----

    def agent_store_ownership_create(self, row: AgentStoreOwnership) -> AgentStoreOwnership:
        with self._session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def agent_store_ownership_get(
        self, *, owner_user: str, listing_id: str,
    ) -> AgentStoreOwnership | None:
        with self._session() as s:
            stmt = (
                select(AgentStoreOwnership)
                .where(AgentStoreOwnership.owner_user == owner_user)
                .where(AgentStoreOwnership.listing_id == listing_id)
                .order_by(AgentStoreOwnership.acquired_at.desc())
                .limit(1)
            )
            return s.exec(stmt).first()

    def agent_store_ownership_list(
        self, *, owner_user: str,
    ) -> list[AgentStoreOwnership]:
        with self._session() as s:
            stmt = (
                select(AgentStoreOwnership)
                .where(AgentStoreOwnership.owner_user == owner_user)
                .order_by(AgentStoreOwnership.acquired_at.desc())
            )
            return list(s.exec(stmt).all())

    def agent_store_ownership_update(self, row: AgentStoreOwnership) -> AgentStoreOwnership:
        with self._session() as s:
            current = s.get(AgentStoreOwnership, row.id)
            if current is None:
                raise KeyError(row.id)
            for field in (
                "owner_user",
                "listing_id",
                "listing_type",
                "title",
                "acquired_via",
                "source_repo",
                "source_manifest_path",
                "listing_snapshot",
                "acquired_at",
            ):
                setattr(current, field, getattr(row, field))
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def agent_store_order_create(self, row: AgentStoreOrder) -> AgentStoreOrder:
        with self._session() as s:
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def agent_store_order_list(
        self,
        *,
        owner_user: str,
        listing_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AgentStoreOrder], int]:
        with self._session() as s:
            stmt = select(AgentStoreOrder).where(AgentStoreOrder.owner_user == owner_user)
            count_stmt = (
                select(func.count())
                .select_from(AgentStoreOrder)
                .where(AgentStoreOrder.owner_user == owner_user)
            )
            if listing_id:
                stmt = stmt.where(AgentStoreOrder.listing_id == listing_id)
                count_stmt = count_stmt.where(AgentStoreOrder.listing_id == listing_id)
            stmt = (
                stmt.order_by(AgentStoreOrder.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = list(s.exec(stmt).all())
            total = int(s.exec(count_stmt).one())
            return rows, total

    def agent_store_order_update(self, row: AgentStoreOrder) -> AgentStoreOrder:
        with self._session() as s:
            current = s.get(AgentStoreOrder, row.id)
            if current is None:
                raise KeyError(row.id)
            for field in (
                "owner_user",
                "listing_id",
                "status",
                "currency",
                "amount",
                "is_mock",
                "payment_provider",
                "external_payment_id",
            ):
                setattr(current, field, getattr(row, field))
            current.updated_at = datetime.now(timezone.utc)
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def openclaw_request_create(
        self, request: OpenclawAgentRequest,
    ) -> OpenclawAgentRequest:
        with self._session() as s:
            s.add(request)
            s.commit()
            s.refresh(request)
            return request

    def openclaw_request_get(
        self, request_id: str,
    ) -> OpenclawAgentRequest | None:
        with self._session() as s:
            return s.get(OpenclawAgentRequest, request_id)

    def openclaw_request_update(
        self, request: OpenclawAgentRequest,
    ) -> OpenclawAgentRequest:
        with self._session() as s:
            current = s.get(OpenclawAgentRequest, request.request_id)
            if current is None:
                raise KeyError(request.request_id)
            for field in (
                "status",
                "requested_agent_id",
                "error_code",
                "error_message",
                "expires_at",
            ):
                setattr(current, field, getattr(request, field))
            current.updated_at = datetime.now(timezone.utc)
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def openclaw_request_list(
        self,
        *,
        user: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[OpenclawAgentRequest], int]:
        with self._session() as s:
            stmt = select(OpenclawAgentRequest)
            count_stmt = select(func.count()).select_from(OpenclawAgentRequest)
            if user:
                stmt = stmt.where(OpenclawAgentRequest.user == user)
                count_stmt = count_stmt.where(OpenclawAgentRequest.user == user)
            stmt = (
                stmt.order_by(OpenclawAgentRequest.created_at.desc())
                .offset(offset).limit(limit)
            )
            items = list(s.exec(stmt).all())
            total = s.exec(count_stmt).one()
            return items, int(total)

    # ---- TaskDecomposeRequests ----

    def task_decompose_create(
        self, request: TaskDecomposeRequest,
    ) -> TaskDecomposeRequest:
        with self._session() as s:
            s.add(request)
            s.commit()
            s.refresh(request)
            return request

    def task_decompose_get(
        self, request_id: str,
    ) -> TaskDecomposeRequest | None:
        with self._session() as s:
            return s.get(TaskDecomposeRequest, request_id)

    def task_decompose_update(
        self, request: TaskDecomposeRequest,
    ) -> TaskDecomposeRequest:
        with self._session() as s:
            current = s.get(TaskDecomposeRequest, request.request_id)
            if current is None:
                raise KeyError(request.request_id)
            for field in (
                "status",
                "result_agents",
                "result_tasks",
                "error_code",
                "error_message",
                "expires_at",
            ):
                setattr(current, field, getattr(request, field))
            current.updated_at = datetime.now(timezone.utc)
            s.add(current)
            s.commit()
            s.refresh(current)
            return current

    def task_decompose_list(
        self,
        *,
        user: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TaskDecomposeRequest], int]:
        with self._session() as s:
            stmt = select(TaskDecomposeRequest)
            count_stmt = select(func.count()).select_from(TaskDecomposeRequest)
            if user:
                stmt = stmt.where(TaskDecomposeRequest.user == user)
                count_stmt = count_stmt.where(TaskDecomposeRequest.user == user)
            stmt = (
                stmt.order_by(TaskDecomposeRequest.created_at.desc())
                .offset(offset).limit(limit)
            )
            items = list(s.exec(stmt).all())
            total = s.exec(count_stmt).one()
            return items, int(total)


__all__ = ["SqliteStorage"]
