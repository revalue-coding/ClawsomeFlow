"""Tests for app.scheduler.engine.FlowScheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    OnFailure,
    RunStatus,
)
from app.scheduler import engine
from app.scheduler.naming import team_name_for_run
from app.scheduler.sessions.base import WorkerSession
from app.storage import get_storage


class _NoopSession(WorkerSession):
    async def _do_spawn(self) -> None:
        pass

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        pass

    async def _do_resume(self) -> None:
        pass

    async def _do_shutdown(self) -> None:
        pass


def _spec() -> FlowSpec:
    return FlowSpec(
        agents=[FlowAgent(
            id="leader", kind=AgentKind.claude, repo="/tmp/main",
            is_leader=True, merge_strategy=MergeStrategy.manual,
            on_failure=OnFailure.retry, max_retries=2,
        )],
        tasks=[FlowTask(
            id="ts", owner_agent_id="leader", subject="x",
            description="", depends_on=[], is_leader_summary=True,
        )],
    )


def _persist(spec: FlowSpec, *, cleanup_team_on_finish: bool = False) -> FlowRun:
    storage = get_storage()
    flow = Flow(
        name="t",
        description="",
        owner_user="alice",
        cleanup_team_on_finish=cleanup_team_on_finish,
    ).with_spec(spec)
    saved = storage.flow_create(flow)
    return storage.run_create(FlowRun(
        id="run-99887766", flow_id=saved.id, flow_version=1,
        team_name=team_name_for_run("run-99887766"),
        status=RunStatus.pending, inputs={}, user="alice",
    ))


@pytest.fixture(autouse=True)
def _reset_scheduler() -> None:
    engine.reset_scheduler()
    yield
    engine.reset_scheduler()


@pytest.mark.asyncio
async def test_start_run_creates_controller_and_task() -> None:
    spec = _spec()
    run = _persist(spec)
    sched = engine.get_scheduler()
    controller = sched.start_run(run=run, spec=spec)
    assert controller is not None
    assert run.id in sched.active_runs()
    sched.cancel_run(run.id)
    await sched.shutdown(timeout=5.0)


@pytest.mark.asyncio
async def test_start_run_idempotent_returns_same_controller() -> None:
    spec = _spec()
    run = _persist(spec)
    sched = engine.get_scheduler()
    a = sched.start_run(run=run, spec=spec)
    b = sched.start_run(run=run, spec=spec)
    assert a is b
    await sched.shutdown(timeout=5.0)


@pytest.mark.asyncio
async def test_cancel_unknown_run_returns_false() -> None:
    sched = engine.get_scheduler()
    assert sched.cancel_run("does-not-exist") is False


@pytest.mark.asyncio
async def test_cancel_run_cancels_complaint_background_task() -> None:
    sched = engine.get_scheduler()
    gate = asyncio.Event()

    async def _job() -> None:
        await gate.wait()

    task = asyncio.create_task(_job())
    sched._complaints["run-complaint-x"] = task
    assert sched.cancel_run("run-complaint-x") is True
    await asyncio.sleep(0)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_shutdown_drains_running_tasks() -> None:
    spec = _spec()
    run = _persist(spec)
    sched = engine.get_scheduler()
    sched.start_run(run=run, spec=spec)
    await sched.shutdown(timeout=5.0)
    # All tasks must have finished by now.
    assert sched.active_runs() == []


@pytest.mark.asyncio
async def test_run_entry_is_popped_after_supervise_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    run = _persist(spec)
    sched = engine.get_scheduler()

    async def _done(self) -> engine.RunOutcome:
        self.run.status = RunStatus.completed
        return engine.RunOutcome(
            final_status=RunStatus.completed,
            completed_task_ids=["ts"],
            failed_task_ids=[],
            skipped_task_ids=[],
            reason="done",
        )

    monkeypatch.setattr(engine.RunController, "run_loop", _done)
    sched.start_run(run=run, spec=spec, compile=False)
    await asyncio.sleep(0.05)
    assert sched.get_controller(run.id) is None
    assert run.id not in sched._runs
    await sched.shutdown(timeout=5.0)


@pytest.mark.asyncio
async def test_start_run_after_shutdown_raises() -> None:
    sched = engine.get_scheduler()
    await sched.shutdown(timeout=2.0)
    spec = _spec()
    run = _persist(spec)
    with pytest.raises(RuntimeError):
        sched.start_run(run=run, spec=spec)


@pytest.mark.asyncio
async def test_uncaught_compile_exception_marks_run_failed_and_emits_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    run = _persist(spec, cleanup_team_on_finish=True)
    sched = engine.get_scheduler()
    captured: dict[str, str] = {}

    async def _boom_compile(**_kw):
        raise RuntimeError("compile boom")

    async def _fake_cleanup(*, run, storage, **_kw):
        captured["run_id"] = run.id
        class _Out:
            team_cleaned = True
            cleaned_openclaw_agents: list[str] = []
            failed_openclaw_agents: list[str] = []
        return _Out()

    monkeypatch.setattr(engine, "compile_flow_to_clawteam", _boom_compile)
    monkeypatch.setattr(engine, "run_terminal_tail_cleanup", _fake_cleanup)
    sched.start_run(run=run, spec=spec)
    await asyncio.sleep(0.05)

    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.failed
    assert refreshed.finished_at is not None

    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(e.type == "run_uncaught_exception" for e in events)
    assert captured["run_id"] == run.id
    await sched.shutdown(timeout=5.0)


@pytest.mark.asyncio
async def test_sweep_stale_awaiting_user_complaints_auto_skips_after_12h() -> None:
    spec = _spec()
    run = _persist(spec)
    stale = datetime.now(timezone.utc) - timedelta(hours=12, minutes=5)
    run.status = RunStatus.awaiting_user_complaint
    run.finished_at = stale
    get_storage().run_update(run)

    sched = engine.get_scheduler()
    count = await sched.sweep_stale_awaiting_user_complaints(now=datetime.now(timezone.utc))
    assert count == 1
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.completed
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(e.type == "run_complaint_auto_skipped" for e in events)


@pytest.mark.asyncio
async def test_sweep_stale_awaiting_user_complaints_keeps_recent_runs() -> None:
    spec = _spec()
    run = _persist(spec)
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    run.status = RunStatus.awaiting_user_complaint
    run.finished_at = recent
    get_storage().run_update(run)

    sched = engine.get_scheduler()
    count = await sched.sweep_stale_awaiting_user_complaints(now=datetime.now(timezone.utc))
    assert count == 0
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.awaiting_user_complaint


@pytest.mark.asyncio
async def test_complaint_exception_does_not_fail_or_emit_user_failure_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    run = _persist(spec)
    run.status = RunStatus.complaint_processing
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)
    flow = get_storage().flow_get(run.flow_id)
    assert flow is not None

    async def _boom(self, *, complaint_text: str) -> None:
        del self, complaint_text
        raise RuntimeError("complaint boom")

    captured: dict[str, object] = {}

    async def _noop_tail_cleanup(**kwargs):
        captured.update(kwargs)
        class _Out:
            team_cleaned = False
            cleaned_openclaw_agents: list[str] = []
            failed_openclaw_agents: list[str] = []
        return _Out()

    monkeypatch.setattr(engine.RunController, "run_user_complaint_phase", _boom)
    monkeypatch.setattr(engine, "run_terminal_tail_cleanup", _noop_tail_cleanup)

    sched = engine.get_scheduler()
    sched.start_run_complaint_phase(run=run, flow=flow, complaint_text="请处理")
    task = sched._complaints[run.id]
    await asyncio.wait_for(task, timeout=2.0)

    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.complaint_failed
    assert captured.get("preserve_worktree_dirs") is True
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert not any(e.type == "run_complaint_phase_failed" for e in events)


@pytest.mark.asyncio
async def test_cancelled_complaint_preserves_worktree_dirs_on_tail_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    run = _persist(spec)
    run.status = RunStatus.awaiting_user_complaint
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)
    flow = get_storage().flow_get(run.flow_id)
    assert flow is not None

    entered = asyncio.Event()

    async def _block(self, *, complaint_text: str) -> None:
        del self, complaint_text
        entered.set()
        await asyncio.Event().wait()

    captured: dict[str, object] = {}

    async def _capture_tail_cleanup(**kwargs):
        captured.update(kwargs)
        class _Out:
            team_cleaned = False
            cleaned_openclaw_agents: list[str] = []
            failed_openclaw_agents: list[str] = []
        return _Out()

    monkeypatch.setattr(engine.RunController, "run_user_complaint_phase", _block)
    monkeypatch.setattr(engine, "run_terminal_tail_cleanup", _capture_tail_cleanup)

    sched = engine.get_scheduler()
    sched.start_run_complaint_phase(run=run, flow=flow, complaint_text="请处理")
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    assert sched.cancel_run(run.id) is True
    task = sched._complaints[run.id]
    await asyncio.wait_for(task, timeout=3.0)

    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.aborted
    assert captured.get("preserve_worktree_dirs") is True


def _persist_run(
    run_id: str, status: RunStatus, *, is_scheduled: bool = False,
) -> FlowRun:
    storage = get_storage()
    flow = Flow(name="t", description="", owner_user="alice").with_spec(_spec())
    saved = storage.flow_create(flow)
    return storage.run_create(FlowRun(
        id=run_id, flow_id=saved.id, flow_version=1,
        team_name=team_name_for_run(run_id),
        status=status, inputs={}, user="alice", is_scheduled=is_scheduled,
    ))


@pytest.mark.asyncio
async def test_drain_to_terminal_pauses_active_orphans_residual_keeps_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()

    # A truly-active run: a live controller blocked in run_loop until a stop
    # signal. The backend never terminates — drain PAUSES it (resumable).
    active = _persist_run("run-active-01", RunStatus.running)

    async def _pause_aware_loop(self, *, max_ticks: int | None = None):
        cancel_w = asyncio.ensure_future(self._cancel_evt.wait())
        pause_w = asyncio.ensure_future(self._pause_evt.wait())
        await asyncio.wait({cancel_w, pause_w}, return_when=asyncio.FIRST_COMPLETED)
        for w in (cancel_w, pause_w):
            if not w.done():
                w.cancel()
        pausing = self._pause_evt.is_set() and not self._cancel_evt.is_set()
        final = RunStatus.paused if pausing else RunStatus.aborted
        self.run.status = final
        self.run.finished_at = None if pausing else datetime.now(timezone.utc)
        get_storage().run_update(self.run)
        return engine.RunOutcome(
            final_status=final,
            completed_task_ids=[], failed_task_ids=[], skipped_task_ids=[],
            reason="drain",
        )

    monkeypatch.setattr(engine.RunController, "run_loop", _pause_aware_loop)

    # A SCHEDULED live run is just an ordinary run → PAUSED like any other (it
    # auto-resumes on restart; its schedule sequence re-attaches to it).
    scheduled = _persist_run("run-sched-05", RunStatus.running, is_scheduled=True)
    # Residual run: ACTIVE_DRIVING in the DB but with NO live controller.
    residual = _persist_run("run-residual-02", RunStatus.running)
    # Preserved runs: survive a restart losslessly; must NOT be touched.
    review = _persist_run("run-review-03", RunStatus.awaiting_user_review)
    complaint = _persist_run("run-complaint-04", RunStatus.awaiting_user_complaint)

    sched = engine.get_scheduler()
    sched.start_run(run=active, spec=spec, compile=False)
    sched.start_run(run=scheduled, spec=spec, compile=False)
    await asyncio.sleep(0.02)
    assert sched.get_controller(active.id) is not None

    result = await sched.drain_to_terminal(timeout=3.0)
    assert result == {"paused": 2, "reverted": 0, "orphaned": 1}

    storage = get_storage()
    # Both the manual and the scheduled run are PAUSED (resumable), finished_at None.
    assert storage.run_get(active.id).status == RunStatus.paused
    assert storage.run_get(active.id).finished_at is None
    assert storage.run_get(scheduled.id).status == RunStatus.paused
    # Residual (no live driver) → orphaned (accepted SIGKILL-class degradation).
    assert storage.run_get(residual.id).status == RunStatus.orphaned
    assert storage.run_get(residual.id).finished_at is not None
    # Preserved states are left intact for post-restart merge/complaint.
    assert storage.run_get(review.id).status == RunStatus.awaiting_user_review
    assert storage.run_get(complaint.id).status == RunStatus.awaiting_user_complaint
    assert sched.active_runs() == []


@pytest.mark.asyncio
async def test_drain_to_terminal_reverts_inflight_complaint_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _persist_run("run-complaint-active-01", RunStatus.complaint_processing)
    storage = get_storage()
    flow = storage.flow_get(run.flow_id)
    assert flow is not None

    entered = asyncio.Event()

    async def _block_skip(self) -> None:
        del self
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(engine.RunController, "skip_user_complaint_phase", _block_skip)

    sched = engine.get_scheduler()
    sched.start_run_skip_complaint_phase(run=run, flow=flow)
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    assert sched.complaint_in_progress(run.id) is True

    result = await sched.drain_to_terminal(timeout=3.0)
    # Backend never terminates: an in-progress complaint reverts to the PRESERVED
    # awaiting_user_complaint so the user can re-submit after the restart.
    assert result == {"paused": 0, "reverted": 1, "orphaned": 0}

    refreshed = storage.run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.awaiting_user_complaint
    assert refreshed.finished_at is None
    assert sched.complaint_in_progress(run.id) is False


@pytest.mark.asyncio
async def test_drain_to_terminal_noop_when_no_active_runs() -> None:
    _persist_run("run-review-only", RunStatus.awaiting_user_review)
    sched = engine.get_scheduler()
    result = await sched.drain_to_terminal(timeout=2.0)
    assert result == {"paused": 0, "reverted": 0, "orphaned": 0}


@pytest.mark.asyncio
async def test_resume_run_rewires_existing_team_without_recompile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _persist_run("run-resume-01", RunStatus.paused)
    storage = get_storage()
    flow = storage.flow_get(run.flow_id)
    assert flow is not None

    # Fake MCP whose task_list returns the EXISTING team's csflow-tagged tasks,
    # from which resume must reconstruct the CompileResult id-mapping.
    class _FakeMcp:
        async def task_list(self, team):  # noqa: ANN001
            del team
            return [
                {"id": "ct-ts", "metadata": {"csflow_task_id": "ts"},
                 "status": "pending", "owner": "leader"},
            ]

    async def _fake_get_mcp(*, user):  # noqa: ANN001
        del user
        return _FakeMcp()

    monkeypatch.setattr(
        "app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp,
    )

    captured: dict[str, object] = {}

    async def _fake_prepare(self) -> None:
        captured["prepared"] = True

    async def _fake_loop(self, *, max_ticks=None):  # noqa: ANN001
        captured["compile_result"] = self.compile_result
        self.run.status = RunStatus.completed
        get_storage().run_update(self.run)
        return engine.RunOutcome(
            final_status=RunStatus.completed,
            completed_task_ids=[], failed_task_ids=[], skipped_task_ids=[],
            reason="done",
        )

    monkeypatch.setattr(engine.RunController, "prepare_resume", _fake_prepare)
    monkeypatch.setattr(engine.RunController, "run_loop", _fake_loop)

    # Guard: resume must NEVER recompile (that would duplicate the team/tasks).
    async def _boom_compile(**kwargs):  # noqa: ANN003
        raise AssertionError("resume must not recompile the team")

    monkeypatch.setattr(engine, "compile_flow_to_clawteam", _boom_compile)

    sched = engine.get_scheduler()
    sched.resume_run(run=run, flow=flow, storage=storage)
    for _ in range(100):
        if sched.get_controller(run.id) is None:
            break
        await asyncio.sleep(0.02)

    assert captured.get("prepared") is True
    cr = captured.get("compile_result")
    assert cr is not None
    assert cr.flow_to_clawteam == {"ts": "ct-ts"}
    assert cr.clawteam_to_flow == {"ct-ts": "ts"}
    assert storage.run_get(run.id).status == RunStatus.completed
