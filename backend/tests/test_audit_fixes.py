"""Tests for the 5 plan-vs-implementation audit fixes (E1–E5).

Each test exercises the exact behaviour the plan mandates and that the
audit found missing or wrong before this commit:

* **E1** no early session shutdown — session shutdown must happen in run tail cleanup
* **E2** retry resets ClawTeam task — :class:`RunController._reset_clawteam_task`
* **E3** ready-set excludes Busy / 1-task-per-owner-per-tick
* **E4** finalize stamps ``finished_at``
* **E5** WS payload uses camelCase consistent with REST events
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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
from app.scheduler import controller as ctrl_mod
from app.scheduler.compiler import CompileResult
from app.scheduler.controller import RunController, _TaskState
from app.scheduler.failure import TaskSnapshot
from app.scheduler.naming import team_name_for_run
from app.scheduler.sessions.base import SessionState, WorkerSession
from app.storage import get_storage


# ── shared stubs ------------------------------------------------------


class _RecordingSession(WorkerSession):
    def __init__(self, *, agent: FlowAgent, team_name: str, run_id: str) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        self.dispatched: list[tuple[str, str]] = []
        self.shutdowns = 0

    async def _do_spawn(self) -> None: ...
    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        self.dispatched.append((task_id, message))
    async def _do_resume(self) -> None: ...
    async def _do_shutdown(self) -> None:
        self.shutdowns += 1


class _FakeLookup:
    async def list_team(self, team, *, repo=None, force=False):
        return []
    async def get(self, team, agent_name, *, repo=None, force=False):
        return None


def _spec_two_tasks_same_owner(*, dispose: bool = True) -> FlowSpec:
    """Two tasks owned by alice + a leader summary."""
    return FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/r",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2,
                      dispose_after_done=dispose),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/r",
                      is_leader=True, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="t2", owner_agent_id="alice", subject="y",
                     description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="sum",
                     description="", depends_on=["t1", "t2"],
                     is_leader_summary=True),
        ],
    )


def _persist_flow_and_run(spec: FlowSpec) -> FlowRun:
    storage = get_storage()
    flow = storage.flow_create(
        Flow(name="t", description="", owner_user="alice").with_spec(spec)
    )
    return storage.run_create(FlowRun(
        id="run-audit-fix", flow_id=flow.id, flow_version=1,
        team_name=team_name_for_run("run-audit-fix"),
        status=RunStatus.running, inputs={}, user="alice",
    ))


# ── E3: ready-set excludes Busy + 1-task-per-owner-per-tick ----------


@pytest.mark.asyncio
async def test_e3_one_dispatch_per_owner_per_tick() -> None:
    spec = _spec_two_tasks_same_owner()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(a: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=a, team_name=run.team_name, run_id=run.id)
        sessions[a.id] = s
        return s

    rc = RunController(
        run=run, spec=spec, worktree_lookup=_FakeLookup(),
        session_factory=factory,
        snapshot_provider=lambda: _async_return([
            TaskSnapshot(
                task_id="t1",
                owner_agent_id="alice",
                status="pending",
                locked_by_agent=None,
                metadata={},
                dispatched_at_epoch=None,
            ),
            TaskSnapshot(
                task_id="t2",
                owner_agent_id="alice",
                status="pending",
                locked_by_agent=None,
                metadata={},
                dispatched_at_epoch=None,
            ),
            TaskSnapshot(
                task_id="ts",
                owner_agent_id="leader",
                status="blocked",
                locked_by_agent=None,
                metadata={},
                dispatched_at_epoch=None,
            ),
        ]),
    )
    await rc.tick()  # both t1 and t2 are ready, alice owns both
    # Only the FIRST one should have been dispatched this tick.
    assert len(sessions["alice"].dispatched) == 1
    assert sessions["alice"].state == SessionState.Busy


@pytest.mark.asyncio
async def test_e3_busy_session_skipped_in_ready_set() -> None:
    spec = _spec_two_tasks_same_owner()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(a):
        s = _RecordingSession(agent=a, team_name=run.team_name, run_id=run.id)
        sessions[a.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="pending",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="alice",
            status="pending",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts",
            owner_agent_id="leader",
            status="blocked",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
    ]

    async def _snapshots():
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, worktree_lookup=_FakeLookup(),
        session_factory=factory,
        snapshot_provider=_snapshots,
    )
    await rc.tick()  # dispatch t1 (alice → Busy)
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="in_progress",
            locked_by_agent="alice",
            metadata={},
            dispatched_at_epoch=datetime.now(timezone.utc).timestamp(),
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="alice",
            status="pending",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts",
            owner_agent_id="leader",
            status="blocked",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()  # alice still Busy → t2 skipped, no new dispatch
    assert len(sessions["alice"].dispatched) == 1


# ── E1: no early shutdown (统一在 run_loop finally) ------------------


@pytest.mark.asyncio
async def test_e1_no_early_shutdown_even_when_dispose_after_done_true() -> None:
    spec = _spec_two_tasks_same_owner(dispose=True)
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(a):
        s = _RecordingSession(agent=a, team_name=run.team_name, run_id=run.id)
        sessions[a.id] = s
        return s

    snapshots: list[TaskSnapshot] = []

    async def snap():
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, worktree_lookup=_FakeLookup(),
        session_factory=factory, snapshot_provider=snap,
    )
    # Tick 1: dispatch t1.
    await rc.tick()
    snapshots.append(TaskSnapshot(
        task_id="t1", owner_agent_id="alice", status="completed",
        locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
    ))
    # Tick 2: t1 completed → idle alice; she still owns t2 → NOT disposed.
    await rc.tick()
    assert sessions["alice"].state == SessionState.Busy  # t2 dispatched
    snapshots.append(TaskSnapshot(
        task_id="t2", owner_agent_id="alice", status="completed",
        locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
    ))
    # Tick 3: t2 completed → alice idle。正常路径禁止提前 shutdown。
    await rc.tick()
    assert sessions["alice"].state == SessionState.Idle
    assert sessions["alice"].shutdowns == 0


@pytest.mark.asyncio
async def test_e1_no_early_shutdown_when_dispose_after_done_false() -> None:
    spec = _spec_two_tasks_same_owner(dispose=False)
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(a):
        s = _RecordingSession(agent=a, team_name=run.team_name, run_id=run.id)
        sessions[a.id] = s
        return s

    snapshots: list[TaskSnapshot] = []

    async def snap():
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, worktree_lookup=_FakeLookup(),
        session_factory=factory, snapshot_provider=snap,
    )
    # Tick 1: dispatch t1 → alice spawn + Busy.
    await rc.tick()
    snapshots.append(TaskSnapshot(
        task_id="t1", owner_agent_id="alice", status="completed",
        locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
    ))
    # Tick 2: t1 completed; t2 dispatched (alice owns both).
    await rc.tick()
    snapshots.append(TaskSnapshot(
        task_id="t2", owner_agent_id="alice", status="completed",
        locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
    ))
    # Tick 3: t2 completed → alice idle, no more tasks。仍然不得提前 shutdown。
    await rc.tick()
    assert sessions["alice"].state == SessionState.Idle
    assert sessions["alice"].shutdowns == 0


# ── E2: retry path resets ClawTeam task --------------------------------


@pytest.mark.asyncio
async def test_e2_retry_calls_mcp_task_update_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec_two_tasks_same_owner()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(a):
        s = _RecordingSession(agent=a, team_name=run.team_name, run_id=run.id)
        sessions[a.id] = s
        return s

    captured: list[dict[str, Any]] = []

    async def fake_get_mcp(**_kw):
        class _M:
            async def task_update(self, **kw):
                captured.append(dict(kw))
                return {}
        return _M()

    # Patch where controller.py imports from.
    from app.integrations import clawteam_mcp as mcp_mod
    monkeypatch.setattr(mcp_mod, "get_mcp_client", fake_get_mcp)

    rc = RunController(
        run=run, spec=spec, worktree_lookup=_FakeLookup(),
        session_factory=factory,
        snapshot_provider=lambda: _async_return([
            TaskSnapshot(task_id="t1", owner_agent_id="alice", status="in_progress",
                         locked_by_agent="alice", metadata={}, dispatched_at_epoch=0),
            TaskSnapshot(task_id="t2", owner_agent_id="alice", status="pending",
                         locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
            TaskSnapshot(task_id="ts", owner_agent_id="leader", status="blocked",
                         locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        ]),
    )
    rc.compile_result = CompileResult(
        team_name=run.team_name, leader_agent_id="leader",
        flow_to_clawteam={"t1": "ct-t1", "t2": "ct-t2", "ts": "ct-ts"},
        clawteam_to_flow={"ct-t1": "t1", "ct-t2": "t2", "ct-ts": "ts"},
    )

    await rc.tick()  # dispatches t1 + timeout-detected retry
    # Wait for the fire-and-forget reset to land.
    import asyncio as _aio
    for _ in range(20):
        if captured:
            break
        await _aio.sleep(0.01)
    assert any(
        row.get("task_id") == "ct-t1"
        and row.get("status") == "pending"
        and row.get("force") is True
        for row in captured
    ), captured


# ── E4: finalize stamps finished_at -----------------------------------


@pytest.mark.asyncio
async def test_e4_finalize_stamps_finished_at() -> None:
    from app.scheduler.finalize import FinalizeInput, finalize_run
    from app.models import Flow, FlowSpec
    spec = _spec_two_tasks_same_owner()
    run = _persist_flow_and_run(spec)
    flow = Flow(
        id=run.flow_id, name="t", description="",
        owner_user="alice", spec=spec.model_dump(mode="json"),
        cleanup_team_on_finish=False,
    )

    class _NopCli:
        async def workspace_merge(self, **kw): return True, ""
        async def workspace_cleanup(self, **kw): return True
        async def team_cleanup(self, **kw): pass

    class _NopMcp:
        async def workspace_agent_diff(self, *a, **kw): return None

    assert run.finished_at is None
    out = await finalize_run(
        FinalizeInput(
            run=run, flow=flow, agents=spec.agents,
            leader_agent_id="leader", has_failed_tasks=False,
        ),
        storage=get_storage(), cli=_NopCli(), mcp=_NopMcp(),
        worktree_lookup=_FakeLookup(),
    )
    assert out.final_status == RunStatus.awaiting_user_complaint
    # Current design: entering complaint phase means orchestration work is done,
    # so ``finished_at`` is stamped immediately.
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_e4_finalize_failed_path_stamps_finished_at() -> None:
    from app.scheduler.finalize import FinalizeInput, finalize_run
    from app.models import Flow
    spec = _spec_two_tasks_same_owner()
    run = _persist_flow_and_run(spec)
    flow = Flow(
        id=run.flow_id, name="t", description="",
        owner_user="alice", spec=spec.model_dump(mode="json"),
        cleanup_team_on_finish=False,
    )

    class _NopCli:
        async def workspace_merge(self, **kw): return True, ""
        async def workspace_cleanup(self, **kw): return True
        async def team_cleanup(self, **kw): pass

    class _NopMcp:
        async def workspace_agent_diff(self, *a, **kw): return None

    out = await finalize_run(
        FinalizeInput(
            run=run, flow=flow, agents=spec.agents,
            leader_agent_id="leader", has_failed_tasks=True,
        ),
        storage=get_storage(), cli=_NopCli(), mcp=_NopMcp(),
        worktree_lookup=_FakeLookup(),
    )
    assert out.final_status == RunStatus.failed
    assert run.finished_at is not None  # terminal → stamped


# ── E5: WS payload camelCase ------------------------------------------


@pytest.mark.asyncio
async def test_e5_event_publish_uses_camelcase_keys() -> None:
    """Controller._emit_event publishes ``agentId``/``taskId`` to broadcaster."""
    from app.events import get_event_broadcaster
    spec = _spec_two_tasks_same_owner()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(a):
        s = _RecordingSession(agent=a, team_name=run.team_name, run_id=run.id)
        sessions[a.id] = s
        return s

    rc = RunController(
        run=run, spec=spec, worktree_lookup=_FakeLookup(),
        session_factory=factory,
        snapshot_provider=lambda: _async_return([
            TaskSnapshot(
                task_id="t1",
                owner_agent_id="alice",
                status="pending",
                locked_by_agent=None,
                metadata={},
                dispatched_at_epoch=None,
            ),
            TaskSnapshot(
                task_id="t2",
                owner_agent_id="alice",
                status="pending",
                locked_by_agent=None,
                metadata={},
                dispatched_at_epoch=None,
            ),
            TaskSnapshot(
                task_id="ts",
                owner_agent_id="leader",
                status="blocked",
                locked_by_agent=None,
                metadata={},
                dispatched_at_epoch=None,
            ),
        ]),
    )

    received = []
    bus = get_event_broadcaster()
    async with bus.subscribe(run.id) as q:
        await rc.tick()  # triggers dispatch → emits "task_dispatched"
        # Drain published event.
        import asyncio as _aio
        try:
            while True:
                received.append(await _aio.wait_for(q.get(), timeout=0.1))
        except _aio.TimeoutError:
            pass
    assert any(
        ev.get("type") == "task_dispatched"
        and "agentId" in ev and "taskId" in ev
        and "agent_id" not in ev
        for ev in received
    ), received


# ── helpers -----------------------------------------------------------


async def _async_return(value):
    return value
