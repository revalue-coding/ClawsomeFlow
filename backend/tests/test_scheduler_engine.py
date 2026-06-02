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
