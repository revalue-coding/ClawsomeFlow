"""Tests for app.scheduler.controller.RunController.

We bring up a tiny in-process Run with stub sessions + DI snapshot/inbox
providers, then drive ``tick()`` directly and verify the state machine
behaves as the design intends. No real subprocesses are spawned.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.integrations.clawteam_cli import CliInvocationError
from app.models import (
    AgentKind,
    ExternalChannel,
    ExternalNodeConfig,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    OnFailure,
    RunEvent,
    RunStatus,
)
from app.scheduler.prompts import WorkerReport
from app.scheduler.compiler import CompileResult
from app.scheduler import controller as ctrl_mod
from app.scheduler.controller import RunController, _TaskState
from app.scheduler.failure import TaskSnapshot
from app.scheduler.naming import team_name_for_run
from app.scheduler.sessions.base import (
    DispatchOutcome,
    SessionState,
    WorkerSession,
)
from app.scheduler.sessions.tmux_ready import wait_tui_ready  # noqa: F401
from app.storage import get_storage
from app.worktree.lookup import WorktreeInfo, WorktreeLookup


# ── stub session that records every action ------------------------------


class _RecordingSession(WorkerSession):
    def __init__(self, *, agent: FlowAgent, team_name: str, run_id: str) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        self.dispatched: list[tuple[str, str]] = []
        self.spawned = 0
        self.resumed = 0
        self.shutdowns = 0

    async def _do_spawn(self) -> None:
        self.spawned += 1

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        self.dispatched.append((task_id, message))

    async def _do_resume(self) -> None:
        self.resumed += 1

    async def _do_shutdown(self) -> None:
        self.shutdowns += 1


class _FailDispatchSession(_RecordingSession):
    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        raise RuntimeError("inject failed")


class _FailCliDispatchSession(_RecordingSession):
    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        del message, task_id
        raise CliInvocationError(
            argv=["clawteam", "runtime", "inject", "t", "alice"],
            exit_code=17,
            stderr="tmux target 'clawteam-team:alice' not found",
            stdout="",
        )


class _FailSpawnSession(_RecordingSession):
    async def _do_spawn(self) -> None:
        raise RuntimeError("spawn failed: repo/worktree unavailable")


class _SlowSpawnSession(_RecordingSession):
    def __init__(
        self,
        *,
        agent: FlowAgent,
        team_name: str,
        run_id: str,
        entered: asyncio.Event,
        allow_finish: asyncio.Event,
    ) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        self._entered = entered
        self._allow_finish = allow_finish

    async def _do_spawn(self) -> None:
        self.spawned += 1
        self._entered.set()
        await self._allow_finish.wait()


class _FlakyDispatchSession(_RecordingSession):
    def __init__(self, *, agent: FlowAgent, team_name: str, run_id: str) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        self._first = True

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        if self._first:
            self._first = False
            raise RuntimeError("tmux pane missing")
        await super()._do_dispatch(message=message, task_id=task_id)


class _FlakyTmuxTargetMissingSession(_RecordingSession):
    def __init__(self, *, agent: FlowAgent, team_name: str, run_id: str) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        self._first = True

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        if self._first:
            self._first = False
            raise CliInvocationError(
                argv=[
                    "clawteam",
                    "runtime",
                    "inject",
                    self.team_name,
                    self.agent.id,
                    "--summary",
                    "<payload>",
                ],
                exit_code=1,
                stderr=f"tmux target '{self.tmux_target}' not found",
                stdout="",
            )
        await super()._do_dispatch(message=message, task_id=task_id)


# ── fixtures ----------------------------------------------------------


@pytest.fixture
def fake_lookup(monkeypatch: pytest.MonkeyPatch) -> WorktreeLookup:
    """Stand-in WorktreeLookup that returns a synthetic worktree per agent."""

    class _FakeLookup:
        def __init__(self) -> None:
            self.calls = 0

        async def list_team(self, team, *, repo=None, force=False):
            return []

        async def get(self, team, agent_name, *, repo=None, force=False):
            self.calls += 1
            return WorktreeInfo(
                agent_name=agent_name,
                branch_name=f"clawteam/{team}/{agent_name}",
                worktree_path=f"/tmp/wt/{agent_name}",
                repo_root=repo or "/tmp/main",
                base_branch="main",
            )

    return _FakeLookup()  # type: ignore[return-value]


def _make_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=True, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="step",
                     description="d", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="summary",
                     description="d", depends_on=["t1"], is_leader_summary=True),
        ],
    )


def _make_openclaw_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(
                id="leader",
                kind=AgentKind.openclaw,
                repo=None,
                is_leader=True,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=1,
            ),
        ],
        tasks=[
            FlowTask(
                id="summary",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=[],
                is_leader_summary=True,
            ),
        ],
    )


def _make_parallel_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(
                id="alice",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="bob",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1",
                owner_agent_id="alice",
                subject="step-a",
                description="d",
                depends_on=[],
            ),
            FlowTask(
                id="t2",
                owner_agent_id="bob",
                subject="step-b",
                description="d",
                depends_on=[],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=["t1", "t2"],
                is_leader_summary=True,
            ),
        ],
    )


def _make_worker_plus_openclaw_leader_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(
                id="alice",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.openclaw,
                repo=None,
                is_leader=True,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1",
                owner_agent_id="alice",
                subject="step",
                description="d",
                depends_on=[],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=["t1"],
                is_leader_summary=True,
            ),
        ],
    )


def _persist_flow_and_run(spec: FlowSpec, *, status_seed: dict | None = None) -> FlowRun:
    storage = get_storage()
    flow = Flow(name="t", description="desc", owner_user="alice").with_spec(spec)
    saved = storage.flow_create(flow)
    team = team_name_for_run("run-12345678")
    run = FlowRun(id="run-12345678", flow_id=saved.id, flow_version=1,
                  team_name=team, status=RunStatus.pending,
                  inputs={"x": "y"}, user="alice")
    return storage.run_create(run)


def _compile_result_for_spec(spec: FlowSpec, *, team_name: str) -> CompileResult:
    flow_to_ct = {t.id: f"ct-{t.id}" for t in spec.tasks}
    ct_to_flow = {v: k for k, v in flow_to_ct.items()}
    leader = next(a.id for a in spec.agents if a.is_leader)
    return CompileResult(
        team_name=team_name,
        leader_agent_id=leader,
        flow_to_clawteam=flow_to_ct,
        clawteam_to_flow=ct_to_flow,
        member_count=len(spec.agents),
    )


# ── tests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_dispatch_then_completion(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = []

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=None,
    )

    # Tick 1: ClawTeam source-of-truth says t1 pending and ts blocked.
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    assert sessions["alice"].dispatched, "alice should have received t1 dispatch"
    assert sessions["alice"].dispatched[0][0] == "t1"
    assert not sessions.get("leader") or not sessions["leader"].dispatched

    # ClawTeam would now report t1=completed and ts=pending (dependency unlocked).
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="completed",
            locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    # 正常路径下会话应保持存活到 run_loop 尾部统一 shutdown；
    # 当前 tick 只应把 alice 从 Busy 释放回 Idle。
    assert sessions["alice"].state == SessionState.Idle
    assert "leader" in sessions
    assert sessions["leader"].dispatched[0][0] == "ts"

    # Mark summary complete; loop should reach terminal next tick.
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="completed",
            locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="completed",
            locked_by_agent="leader", metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    assert rc._terminal_check() is True


@pytest.mark.asyncio
async def test_crashed_without_worktree_does_fresh_spawn(fake_lookup) -> None:
    """Regression: an agent whose initial (prewarm) spawn failed sits in Crashed
    with NO recorded worktree. Startup must FRESH-spawn (recreating the worktree)
    instead of calling resume(), which hard-fails 'cannot resume without recorded
    worktree' and dead-ends the task."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup, session_factory=factory,
        snapshot_provider=None, leader_inbox_provider=None,
    )
    agent = next(a for a in spec.agents if a.id == "alice")
    sess = rc._ensure_session_handle(agent)
    # Simulate a failed prewarm: Crashed, no worktree recorded.
    await sess.spawn()
    sess.mark_crashed()
    assert sess.state == SessionState.Crashed
    assert sess.worktree is None
    sess.spawned = 0
    sess.resumed = 0

    await rc._run_startup_sequence(agent=agent, source="test")

    assert sess.spawned == 1          # fresh spawn
    assert sess.resumed == 0          # NOT resume
    assert sess.state == SessionState.Idle
    assert sess.worktree is not None  # _refresh_worktree populated it


@pytest.mark.asyncio
async def test_crashed_with_worktree_resumes(fake_lookup) -> None:
    """A genuinely-crashed session that DID record a worktree still resumes."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    def factory(agent: FlowAgent) -> WorkerSession:
        return _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup, session_factory=factory,
        snapshot_provider=None, leader_inbox_provider=None,
    )
    agent = next(a for a in spec.agents if a.id == "alice")
    sess = rc._ensure_session_handle(agent)
    await sess.spawn()
    sess.worktree = WorktreeInfo(
        agent_name="alice", branch_name="b", worktree_path="/tmp/wt/alice",
        repo_root="/tmp/main", base_branch="main",
    )
    sess.mark_crashed()
    sess.spawned = 0
    sess.resumed = 0

    await rc._run_startup_sequence(agent=agent, source="test")

    assert sess.resumed == 1          # resume (worktree present)
    assert sess.spawned == 0
    assert sess.state == SessionState.Idle


async def _rerun_prompt_for(run, spec, fake_lookup) -> str:
    from app.scheduler.controller import _CheckpointItem

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=None, leader_inbox_provider=None,
    )
    agent = next(a for a in spec.agents if a.id == "alice")
    sess = rc._ensure_session_handle(agent)
    sess.worktree = WorktreeInfo(
        agent_name="alice", branch_name="clawteam/t/alice",
        worktree_path="/tmp/wt/alice", repo_root="/tmp/main", base_branch="main",
    )
    item = _CheckpointItem(task_id="t1", subject="x", owner_agent_id="alice")
    # Rerun message == feedback preamble + the exact initial-dispatch message.
    return await rc._build_checkpoint_rerun_prompt(
        downstream_task_id="ts", upstream_item=item, feedback="please fix the chart",
    )


@pytest.mark.asyncio
async def test_checkpoint_rerun_prompt_includes_self_merge_when_scheduled(fake_lookup) -> None:
    """省心/auto-merge run: a checkpoint rerun must self-merge to baseline."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    run.is_scheduled = True
    prompt = await _rerun_prompt_for(run, spec, fake_lookup)
    assert "self-merge" in prompt.lower()
    assert "csflow-locked-merge.py" in prompt
    assert "clawteam/t/alice" in prompt  # feature branch passed to the tool
    assert "please fix the chart" in prompt  # user feedback still embedded
    # Rerun reuses the real dispatch message verbatim (identical execution reqs).
    assert "## Completion Checklist" in prompt


@pytest.mark.asyncio
async def test_checkpoint_rerun_prompt_omits_self_merge_when_not_scheduled(fake_lookup) -> None:
    """Normal (manual-merge) run: rerun prompt must NOT self-merge (user merges)."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)  # is_scheduled defaults False
    prompt = await _rerun_prompt_for(run, spec, fake_lookup)
    assert "git merge --no-ff" not in prompt
    assert "**Self-merge:**" not in prompt
    assert "please fix the chart" in prompt


def test_summary_dispatch_gated_until_all_tasks_completed(fake_lookup) -> None:
    """The leader summary waits for ALL non-summary tasks, even ones it does
    not depend on (depends_on now only selects which outputs feed the summary)."""
    spec = FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/main", is_leader=False),
            FlowAgent(id="bob", kind=AgentKind.claude, repo="/tmp/main", is_leader=False),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/main", is_leader=True),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="a", description="d"),
            FlowTask(id="t2", owner_agent_id="bob", subject="b", description="d"),
            FlowTask(id="ts", owner_agent_id="leader", subject="s", description="d",
                     depends_on=["t1"], is_leader_summary=True),
        ],
    )
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=None,
    )

    # ts depends only on t1, but unrelated t2 is still pending → summary held
    # back even though its own dependency (t1) is done.
    rc._tasks["t1"].state = _TaskState.completed
    rc._tasks["t2"].state = _TaskState.pending
    rc._tasks["ts"].state = _TaskState.pending
    ready_ids = {b.task.id for b in rc._ready_tasks()}
    assert "ts" not in ready_ids
    assert "t2" in ready_ids

    # Once every non-summary task is completed, the summary becomes dispatchable.
    rc._tasks["t2"].state = _TaskState.completed
    ready_ids = {b.task.id for b in rc._ready_tasks()}
    assert ready_ids == {"ts"}


def test_terminal_check_uses_leader_summary_completion(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=None,
    )

    # Natural completion now gates on leader summary completion.
    rc._tasks["t1"].state = _TaskState.pending
    rc._tasks["ts"].state = _TaskState.completed
    assert rc._terminal_check() is True


@pytest.mark.asyncio
async def test_dispatch_syncs_clawteam_task_to_in_progress(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    compile_result = _compile_result_for_spec(spec, team_name=run.team_name)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    updates: list[dict[str, Any]] = []

    class _FakeMcp:
        async def task_update(self, team_name, task_id, **kwargs):
            updates.append({"team_name": team_name, "task_id": task_id, **kwargs})
            return {"id": task_id, "status": kwargs.get("status")}

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr("app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp_client)

    snapshots = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        compile_result=compile_result,
    )

    await rc.tick()

    assert sessions["alice"].dispatched
    assert any(
        row["task_id"] == "ct-t1" and row.get("status") == "in_progress"
        for row in updates
    )


@pytest.mark.asyncio
async def test_ensure_session_idle_reuses_existing_openclaw_tmux(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=None,
        leader_inbox_provider=None,
    )

    stale = factory(spec.agents[0])
    rc._sessions["leader"] = stale
    await stale.spawn()
    await stale.shutdown(reason="run_finalize")
    assert stale.state == SessionState.Exited

    async def fake_wait_shell_ready(*args, **kwargs) -> bool:
        del args, kwargs
        return True

    monkeypatch.setattr(ctrl_mod, "wait_shell_ready", fake_wait_shell_ready)

    reused = await rc._ensure_session_idle(spec.agents[0])
    assert reused is not stale
    assert reused.state == SessionState.Idle
    assert isinstance(reused, _RecordingSession)
    assert reused.spawned == 0


@pytest.mark.asyncio
async def test_failure_retry_marks_session_crashed_and_redispatches(
    fake_lookup,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    # First snapshot: alice dispatched t1 and now exceeds timeout window.
    snapshots: list[TaskSnapshot] = []

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()  # initial dispatch of t1
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="in_progress",
            locked_by_agent="alice", metadata={}, dispatched_at_epoch=0,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()  # detect failure → retry → resume → redispatch (all 1 tick)
    book = rc._tasks["t1"]
    assert book.retries == 1
    # After retry+redispatch in one tick the session should be Busy again on
    # the redispatched task, and the task book is back to in_progress.
    assert book.state == _TaskState.in_progress
    assert sessions["alice"].resumed == 1
    assert sessions["alice"].state == SessionState.Busy
    # alice should have been dispatched twice: once initially, once after retry.
    assert len(sessions["alice"].dispatched) == 2


@pytest.mark.asyncio
async def test_pause_on_max_retries_then_resumable(
    fake_lookup,
) -> None:
    spec = _make_spec()
    # Limit max_retries to 0 so first failure aborts.
    spec.agents[0] = FlowAgent(
        id="alice", kind=AgentKind.claude, repo="/tmp/main",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=0,
    )
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = []

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="in_progress",
            locked_by_agent="alice", metadata={}, dispatched_at_epoch=0,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    assert rc._tasks["t1"].state == _TaskState.blocked
    # Retries exhausted → the backend PAUSES (resumable) rather than terminating.
    assert rc._pause_evt.is_set()
    assert not rc._cancel_evt.is_set()
    outcome = rc._build_outcome()
    assert outcome.final_status == RunStatus.paused
    assert "t1" in outcome.failed_task_ids


@pytest.mark.asyncio
async def test_snapshot_pending_requeues_and_redispatches_without_waiting_timeout(
    fake_lookup,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = []

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    assert [tid for tid, _ in sessions["alice"].dispatched] == ["t1"]

    # Simulate ClawTeam on-exit/on-crash resetting task back to pending.
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="pending",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=0,
        ),
    ]
    await rc.tick()

    # Scheduler reconciles pending immediately and redispatches in same tick.
    assert [tid for tid, _ in sessions["alice"].dispatched] == ["t1", "t1"]
    assert rc._tasks["t1"].state == _TaskState.in_progress


@pytest.mark.asyncio
async def test_blocked_snapshot_remains_blocked_and_not_dispatched(
    fake_lookup,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
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
            task_id="ts",
            owner_agent_id="leader",
            status="blocked",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    await rc.tick()

    assert [tid for tid, _ in sessions["alice"].dispatched] == ["t1"]
    assert not sessions.get("leader") or not sessions["leader"].dispatched
    assert rc._tasks["ts"].state == _TaskState.blocked


@pytest.mark.asyncio
async def test_pending_snapshot_dispatches_without_local_dependency_recheck(
    fake_lookup,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    # ClawTeam source-of-truth says both tasks are pending; controller should
    # trust the snapshot and dispatch in task order (first tick only first one).
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
            task_id="ts",
            owner_agent_id="leader",
            status="pending",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    await rc.tick()
    assert [tid for tid, _ in sessions["alice"].dispatched] == ["t1"]
    assert not sessions.get("leader") or not sessions["leader"].dispatched

    # Even though ClawTeam reports the summary as pending, the summary gate
    # holds it until every non-summary task has completed.
    await rc.tick()
    assert not sessions.get("leader") or not sessions["leader"].dispatched

    # Mark the worker task completed → summary becomes dispatchable. We still
    # trust the snapshot's pending flag for the summary (no local depends_on
    # graph recheck); only the all-tasks-complete gate applies.
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="completed",
            locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    assert [tid for tid, _ in sessions["leader"].dispatched] == ["ts"]
    assert rc._tasks["ts"].state == _TaskState.in_progress


@pytest.mark.asyncio
async def test_owner_with_clawteam_in_progress_does_not_get_second_dispatch(
    fake_lookup,
) -> None:
    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="alice",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="a", description="d", depends_on=[]),
            FlowTask(id="t2", owner_agent_id="alice", subject="b", description="d", depends_on=[]),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=["t1", "t2"],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = []

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    snapshots[:] = [
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
    await rc.tick()
    assert [tid for tid, _ in sessions["alice"].dispatched] == ["t1"]

    # Simulate stale local session state (e.g. controller restart) while
    # ClawTeam still reports alice's first task in_progress.
    sessions["alice"].mark_idle(reason="test_stale_local_idle")
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="in_progress",
            locked_by_agent="alice",
            metadata={},
            dispatched_at_epoch=time.time(),
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
    await rc.tick()

    # No second dispatch while owner already has one task in progress.
    assert [tid for tid, _ in sessions["alice"].dispatched] == ["t1"]
    assert rc._tasks["t1"].state == _TaskState.in_progress
    assert rc._tasks["t2"].state == _TaskState.pending


@pytest.mark.asyncio
async def test_skip_policy_escalates_to_failed_and_aborts(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    spec.agents[0] = FlowAgent(
        id="alice", kind=AgentKind.claude, repo="/tmp/main",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.skip, max_retries=2,
    )
    run = _persist_flow_and_run(spec)
    compile_result = _compile_result_for_spec(spec, team_name=run.team_name)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = []

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    updates: list[dict[str, Any]] = []

    class _FakeMcp:
        async def task_update(self, team_name, task_id, **kwargs):
            updates.append({"team_name": team_name, "task_id": task_id, **kwargs})
            return {"id": task_id, "status": kwargs.get("status")}

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr("app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp_client)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        compile_result=compile_result,
    )

    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()
    snapshots[:] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="in_progress",
            locked_by_agent="alice", metadata={}, dispatched_at_epoch=0,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]
    await rc.tick()

    book = rc._tasks["t1"]
    assert book.state == _TaskState.blocked
    # Backend never terminates: a failed task PAUSES the run (resumable) instead
    # of cancelling it.
    assert rc._pause_evt.is_set()
    assert not rc._cancel_evt.is_set()
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=100)
    assert any(
        e.type == "task_failed"
        and e.task_id == "t1"
        and (e.payload or {}).get("effective_action") == "pause"
        for e in events
    )
    assert any(
        row["task_id"] == "ct-t1" and row.get("status") == "blocked"
        for row in updates
    )
    assert not any(e.type == "task_skipped" and e.task_id == "t1" for e in events)


@pytest.mark.asyncio
async def test_run_loop_terminates_when_all_tasks_done(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    # finalize_run obtains a ClawTeam MCP client to snapshot worktree diffs.
    # Without this stub it spawns the real `clawteam-mcp` binary, which is not
    # present on CI runners / contributor clones (FileNotFoundError -> finalize
    # fails -> run ends 'completed' instead of 'awaiting_user_complaint'). The
    # diff is read through _safe_diff, so returning None == "no changes".
    class _FakeMcp:
        async def workspace_agent_diff(self, *args, **kwargs):
            return None

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr(
        "app.scheduler.finalize.get_mcp_client", _fake_get_mcp_client
    )

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    # Mock snapshots as ClawTeam source-of-truth states.
    state = {"t1_done": False, "ts_done": False}

    async def snap_provider() -> list[TaskSnapshot]:
        out: list[TaskSnapshot] = []
        alice_dispatched = bool(sessions.get("alice") and sessions["alice"].dispatched)
        leader_dispatched = bool(sessions.get("leader") and sessions["leader"].dispatched)
        if state["t1_done"]:
            out.append(TaskSnapshot(
                task_id="t1", owner_agent_id="alice", status="completed",
                locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
            ))
            if state["ts_done"] and leader_dispatched:
                out.append(TaskSnapshot(
                    task_id="ts", owner_agent_id="leader", status="completed",
                    locked_by_agent="leader", metadata={}, dispatched_at_epoch=None,
                ))
            else:
                out.append(TaskSnapshot(
                    task_id="ts", owner_agent_id="leader", status="pending",
                    locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
                ))
        else:
            out.append(TaskSnapshot(
                task_id="t1", owner_agent_id="alice",
                status="in_progress" if alice_dispatched else "pending",
                locked_by_agent="alice" if alice_dispatched else None,
                metadata={},
                dispatched_at_epoch=time.time() if alice_dispatched else None,
            ))
            out.append(TaskSnapshot(
                task_id="ts", owner_agent_id="leader", status="blocked",
                locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
            ))
        return out

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    async def driver():
        # let scheduler tick a few times, flipping completion flags
        await asyncio.sleep(0.05)
        state["t1_done"] = True
        await asyncio.sleep(0.05)
        state["ts_done"] = True

    drive_task = asyncio.create_task(driver())
    outcome = await rc.run_loop(max_ticks=50)
    await drive_task

    # When worker worktrees contain no diff/commits, manual merge is skipped.
    # The run goes straight to complaint stage with no pending merge card.
    assert outcome.final_status == RunStatus.awaiting_user_complaint
    assert "t1" in outcome.completed_task_ids
    assert "ts" in outcome.completed_task_ids
    assert rc.run.pending_merges is None


@pytest.mark.asyncio
async def test_terminal_check_persists_execution_log_with_required_fields(
    fake_lookup,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    state = {"t1_done": False, "ts_done": False}
    mailbox = [{"from_agent": "alice", "content": "task t1 done: ok", "task_id": "t1"}]

    async def snap_provider() -> list[TaskSnapshot]:
        out: list[TaskSnapshot] = []
        alice_dispatched = bool(sessions.get("alice") and sessions["alice"].dispatched)
        if state["t1_done"]:
            out.append(TaskSnapshot(
                task_id="t1", owner_agent_id="alice", status="completed",
                locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
            ))
            leader_dispatched = bool(
                sessions.get("leader") and sessions["leader"].dispatched
            )
            if state["ts_done"] and leader_dispatched:
                out.append(TaskSnapshot(
                    task_id="ts", owner_agent_id="leader", status="completed",
                    locked_by_agent="leader", metadata={}, dispatched_at_epoch=None,
                ))
            else:
                out.append(TaskSnapshot(
                    task_id="ts", owner_agent_id="leader", status="pending",
                    locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
                ))
        else:
            out.append(TaskSnapshot(
                task_id="t1", owner_agent_id="alice",
                status="in_progress" if alice_dispatched else "pending",
                locked_by_agent="alice" if alice_dispatched else None,
                metadata={},
                dispatched_at_epoch=time.time() if alice_dispatched else None,
            ))
            out.append(TaskSnapshot(
                task_id="ts", owner_agent_id="leader", status="blocked",
                locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
            ))
        return out

    async def inbox_provider():
        if mailbox:
            rows = list(mailbox)
            mailbox.clear()
            return rows
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="goal with runtime parameters",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )

    async def driver():
        await asyncio.sleep(0.05)
        state["t1_done"] = True
        await asyncio.sleep(0.1)
        state["ts_done"] = True

    drive_task = asyncio.create_task(driver())
    await rc.run_loop(max_ticks=50)
    await drive_task

    events = get_storage().event_list(run_id=run.id, since_id=None, limit=500)
    snap_events = [e for e in events if e.type == "run_terminal_execution_log"]
    assert snap_events, "missing terminal execution log event"
    payload = snap_events[-1].payload

    assert payload["team_name"] == run.team_name
    assert "goal with runtime parameters" in payload["flow_goal"]
    # Terminal execution log is captured before finalize mutates internal
    # scheduler keys in run.inputs.
    assert payload["run_inputs"] == {"x": "y"}
    assert payload["non_completed_tasks_at_terminal_check"] == []

    task_map = {t["task_id"]: t for t in payload["tasks"]}
    assert task_map["t1"]["owner_agent_id"] == "alice"
    assert "Task #t1" in task_map["t1"]["input_message"]
    assert task_map["t1"]["output_messages"][0]["summary"] == "task t1 done: ok"

    session_map = {s["agent_id"]: s for s in payload["agent_sessions"]}
    assert session_map["alice"]["session_id"]
    assert session_map["leader"]["session_id"]


@pytest.mark.asyncio
async def test_terminal_log_records_non_completed_tasks_when_leader_done(
    fake_lookup,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=None,
    )
    rc._tasks["t1"].state = _TaskState.pending
    rc._tasks["ts"].state = _TaskState.completed

    assert rc._terminal_check() is True
    await rc._persist_terminal_execution_log(trigger="test_leader_summary_gate")

    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    snap_events = [e for e in events if e.type == "run_terminal_execution_log"]
    assert snap_events, "missing terminal execution log event"
    payload = snap_events[-1].payload
    rows = payload["non_completed_tasks_at_terminal_check"]
    assert any(
        row["task_id"] == "t1" and row["state"] == "pending"
        for row in rows
    )
    assert not any(row["task_id"] == "ts" for row in rows)


@pytest.mark.asyncio
async def test_leader_summary_uses_structured_inbox(fake_lookup) -> None:
    """plan §8.5 leader prompt should render `from_agent` + `task_id` per inbox row."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    # Snapshot reports t1 done and ts unblocked; structured inbox carries author + task_id.
    snapshots = [
        TaskSnapshot(task_id="t1", owner_agent_id="alice", status="completed",
                     locked_by_agent="alice", metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="ts", owner_agent_id="leader", status="pending",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider():
        return [
            {"from_agent": "alice", "content": "shipped feature X",
             "task_id": "t1"},
        ]

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()  # absorb completion/unblock and dispatch leader summary
    leader_msgs = sessions.get("leader")
    assert leader_msgs is not None and leader_msgs.dispatched, "leader was not dispatched"
    leader_prompt = leader_msgs.dispatched[0][1]
    # Worker report block should contain alice + t1, NOT the placeholder "?".
    assert "alice" in leader_prompt
    assert "t1" in leader_prompt
    assert "shipped feature X" in leader_prompt


def test_pick_latest_leader_complaint_requires_strict_relay_header(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
    )
    rows = [
        {"from": "alice", "content": "worker chatter"},
        {"from": "leader", "content": "plain complaint feedback without tag"},
        {"from": "leader", "content": "[csflow-complaint-relay:ct-old:web] stale"},
        {
            "from": "leader",
            "content": "[csflow-complaint-relay:ct-1:web] 请补充异常路径处理",
        },
    ]
    assert rc._pick_latest_leader_complaint(
        rows,
        relay_task_id="ct-1",
        target_agent_id="web",
    ) == "请补充异常路径处理"
    assert rc._pick_latest_leader_complaint(
        rows,
        relay_task_id="ct-1",
        target_agent_id="api",
    ) is None


@pytest.mark.asyncio
async def test_cancel_run_terminates_loop_quickly(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: asyncio.sleep(0, result=[]),
    )
    runner = asyncio.create_task(rc.run_loop(max_ticks=10))
    await asyncio.sleep(0)  # let loop enter
    rc.cancel()
    out = await runner
    assert out.final_status in (RunStatus.aborted, RunStatus.failed, RunStatus.completed)
    # Main assertion: loop terminated and persisted terminal status.
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status != RunStatus.running


@pytest.mark.asyncio
async def test_tick_skips_spawning_owner_and_dispatches_other_ready_owner(fake_lookup) -> None:
    spec = _make_parallel_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
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
            owner_agent_id="bob",
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

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )
    alice_sess = _RecordingSession(agent=spec.agents[0], team_name=run.team_name, run_id=run.id)
    alice_sess._transition_force(SessionState.Spawning, reason="test_prewarm")
    rc._sessions["alice"] = alice_sess

    await rc.tick()
    assert "bob" in sessions
    assert sessions["bob"].dispatched
    assert sessions["bob"].dispatched[0][0] == "t2"
    assert not alice_sess.dispatched
    assert rc._tasks["t1"].state == _TaskState.pending
    assert rc._tasks["t2"].state == _TaskState.in_progress


@pytest.mark.asyncio
async def test_ensure_session_idle_dedupes_concurrent_startup(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    entered = asyncio.Event()
    allow_finish = asyncio.Event()
    sessions: dict[str, WorkerSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        if agent.id == "alice":
            s = _SlowSpawnSession(
                agent=agent,
                team_name=run.team_name,
                run_id=run.id,
                entered=entered,
                allow_finish=allow_finish,
            )
            sessions[agent.id] = s
            return s
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=_empty_snapshots,
    )
    alice = next(a for a in spec.agents if a.id == "alice")
    first = asyncio.create_task(rc._ensure_session_idle(alice))
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    second = asyncio.create_task(rc._ensure_session_idle(alice))
    await asyncio.sleep(0)
    allow_finish.set()
    first_sess, second_sess = await asyncio.gather(first, second)
    assert first_sess is second_sess
    recorded = sessions["alice"]
    assert isinstance(recorded, _SlowSpawnSession)
    assert recorded.spawned == 1


@pytest.mark.asyncio
async def test_ensure_startup_task_closes_coro_when_create_task_fails(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    closed = {"value": False}

    class _CloseAwareAwaitable:
        def __await__(self):
            async def _inner():
                await asyncio.sleep(0)
                return rc._ensure_session_handle(spec.agents[0])

            return _inner().__await__()

        def close(self) -> None:
            closed["value"] = True

    def _fake_run_startup_sequence(*, agent: FlowAgent, source: str):
        del agent, source
        return _CloseAwareAwaitable()

    def _fake_create_task(*_args, **_kwargs):
        raise RuntimeError("loop closing")

    monkeypatch.setattr(rc, "_run_startup_sequence", _fake_run_startup_sequence)
    monkeypatch.setattr(ctrl_mod.asyncio, "create_task", _fake_create_task)

    with pytest.raises(RuntimeError, match="loop closing"):
        rc._ensure_startup_task(agent=spec.agents[0], source="test")
    assert closed["value"] is True


@pytest.mark.asyncio
async def test_prewarm_failure_emits_event_without_immediate_run_failure(fake_lookup) -> None:
    spec = _make_parallel_spec()
    run = _persist_flow_and_run(spec)

    def factory(agent: FlowAgent) -> WorkerSession:
        if agent.id == "bob":
            return _FailSpawnSession(agent=agent, team_name=run.team_name, run_id=run.id)
        return _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)

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
            owner_agent_id="bob",
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

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )
    await rc.tick()
    assert rc._tasks["t1"].state == _TaskState.in_progress
    assert rc._first_dispatch_task_id == "t1"
    if rc._prewarm_task is not None:
        await rc._prewarm_task
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(
        e.type == "session_prewarm_failed" and e.agent_id == "bob"
        for e in events
    )
    assert not any(e.type == "task_session_start_failed" for e in events)
    assert rc._forced_failed is False


@pytest.mark.asyncio
async def test_first_tick_dispatches_only_first_pending_task(fake_lookup) -> None:
    spec = _make_parallel_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
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
            owner_agent_id="bob",
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

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )
    await rc.tick()
    assert sessions["alice"].dispatched
    assert sessions["alice"].dispatched[0][0] == "t1"
    assert "bob" not in sessions or not sessions["bob"].dispatched
    assert rc._first_dispatch_task_id == "t1"
    assert rc._first_dispatch_owner_id == "alice"


@pytest.mark.asyncio
async def test_prewarm_targets_follow_task_owner_order_after_first_dispatch(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_parallel_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    rc._first_dispatch_task_id = "t1"
    rc._first_dispatch_owner_id = "alice"
    called: list[str] = []

    def _fake_startup(*, agent: FlowAgent, source: str) -> asyncio.Task[WorkerSession]:
        del source
        called.append(agent.id)
        return asyncio.create_task(
            asyncio.sleep(
                0,
                result=rc._ensure_session_handle(agent),
            )
        )

    monkeypatch.setattr(rc, "_ensure_startup_task", _fake_startup)
    await rc._prewarm_tui_sessions()
    assert called == ["bob", "leader"]


@pytest.mark.asyncio
async def test_run_loop_cancels_unfinished_prewarm_startups(fake_lookup) -> None:
    spec = _make_parallel_spec()
    run = _persist_flow_and_run(spec)
    entered = asyncio.Event()
    allow_finish = asyncio.Event()
    sessions: dict[str, WorkerSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        if agent.id == "bob":
            s = _SlowSpawnSession(
                agent=agent,
                team_name=run.team_name,
                run_id=run.id,
                entered=entered,
                allow_finish=allow_finish,
            )
            sessions[agent.id] = s
            return s
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=lambda: asyncio.sleep(
            0,
            result=[
                TaskSnapshot(
                    task_id="t1",
                    owner_agent_id="alice",
                    status=(
                        "in_progress"
                        if (
                            isinstance(sessions.get("alice"), _RecordingSession)
                            and bool(sessions["alice"].dispatched)
                        )
                        else "pending"
                    ),
                    locked_by_agent="alice" if sessions.get("alice") and sessions["alice"].dispatched else None,
                    metadata={},
                    dispatched_at_epoch=None,
                ),
                TaskSnapshot(
                    task_id="t2",
                    owner_agent_id="bob",
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
            ],
        ),
    )
    runner = asyncio.create_task(rc.run_loop(max_ticks=50))
    await asyncio.wait_for(entered.wait(), timeout=2.0)
    rc.cancel()
    outcome = await asyncio.wait_for(runner, timeout=5.0)
    assert outcome.final_status in {RunStatus.aborted, RunStatus.completed, RunStatus.failed}
    assert rc._prewarm_task is None
    assert not rc._startup_tasks
    bob = sessions["bob"]
    assert isinstance(bob, _SlowSpawnSession)
    assert bob.shutdowns >= 1
    assert bob.state == SessionState.Exited


@pytest.mark.asyncio
async def test_empty_snapshot_emits_snapshot_unavailable_event(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    await rc.tick()
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=100)
    assert any(e.type == "snapshot_unavailable" for e in events)


@pytest.mark.asyncio
async def test_run_loop_unhandled_exception_pauses_and_emits_event(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )

    async def _boom_tick():
        raise RuntimeError("tick boom")

    monkeypatch.setattr(rc, "tick", _boom_tick)
    outcome = await rc.run_loop(max_ticks=2)
    # Scenario 9: the backend never terminates — a scheduler exception PAUSES the
    # run (resumable) with a confirmation hint, instead of marking it failed.
    assert outcome.final_status == RunStatus.paused
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=100)
    assert any(e.type == "run_loop_exception" for e in events)
    from app.scheduler.run_metadata import (
        PAUSE_REASON_INTERNAL_ERROR,
        read_pause_state,
    )
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.paused
    blob = read_pause_state(refreshed)
    assert blob is not None
    assert blob.get("reason") == PAUSE_REASON_INTERNAL_ERROR
    assert blob.get("needs_confirmation") is True


@pytest.mark.asyncio
async def test_run_loop_unhandled_exception_terminates_unattended_run(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 9 for an UNATTENDED run: a scheduler exception can't PAUSE (no
    human to resume) — it drives to terminal ``failed`` so the caller / callback /
    webhook gets a result instead of hanging forever. Contrast the attended run,
    which parks in ``paused`` with a confirm hint."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    run.inputs = {"_csflow_unattended": "true"}
    get_storage().run_update(run)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )

    async def _boom_tick():
        raise RuntimeError("tick boom")

    monkeypatch.setattr(rc, "tick", _boom_tick)
    outcome = await rc.run_loop(max_ticks=2)
    # Terminal failed (NOT paused): _forced_failed drove finalize to a result.
    assert rc._forced_failed is True
    assert outcome.final_status == RunStatus.failed
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.failed
    assert refreshed.finished_at is not None


@pytest.mark.asyncio
async def test_prepare_resume_reconciles_from_clawteam_snapshot(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_resume is the single re-derivation point after a pause.

    From the fresh ClawTeam snapshot: completed stays completed; ANY runnable
    non-completed LOCAL task is reset to pending so it re-runs — covering both an
    interrupted ``in_progress`` task AND a failure-``blocked`` task whose deps are
    done (so a failure-paused node reliably restarts). A ``blocked`` task still
    gated on incomplete deps is left blocked. An in_progress EXTERNAL task is left
    (its outstanding receipt ticket resolves it — never re-dispatched).
    """
    spec = FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=False, merge_strategy=MergeStrategy.manual),
            FlowAgent(id="ext", kind=AgentKind.external,
                      external=ExternalNodeConfig(channel=ExternalChannel.human),
                      is_leader=False),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=True, merge_strategy=MergeStrategy.manual),
        ],
        tasks=[
            FlowTask(id="t_local", owner_agent_id="alice", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="t_ext", owner_agent_id="ext", subject="x",
                     description="", depends_on=[]),
            # Failure-blocked, deps satisfied → must re-run from pending.
            FlowTask(id="t_failed", owner_agent_id="alice", subject="x",
                     description="", depends_on=[]),
            # Blocked, waiting on an incomplete dep (t_local) → stays blocked.
            FlowTask(id="t_depblk", owner_agent_id="alice", subject="x",
                     description="", depends_on=["t_local"]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y",
                     description="", depends_on=["t_local"], is_leader_summary=True),
        ],
    )
    run = _persist_flow_and_run(spec)
    compile_result = _compile_result_for_spec(spec, team_name=run.team_name)

    updates: list[dict[str, Any]] = []

    class _FakeMcp:
        async def task_update(self, team_name, task_id, **kwargs):
            updates.append({"task_id": task_id, **kwargs})
            return {"id": task_id, "status": kwargs.get("status")}

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr(
        "app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp_client,
    )

    async def snap_provider() -> list[TaskSnapshot]:
        return [
            TaskSnapshot(task_id="t_local", owner_agent_id="alice",
                         status="in_progress", locked_by_agent="alice",
                         metadata={}, dispatched_at_epoch=0),
            TaskSnapshot(task_id="t_ext", owner_agent_id="ext",
                         status="in_progress", locked_by_agent=None,
                         metadata={}, dispatched_at_epoch=0),
            TaskSnapshot(task_id="t_failed", owner_agent_id="alice",
                         status="blocked", locked_by_agent=None,
                         metadata={}, dispatched_at_epoch=None),
            TaskSnapshot(task_id="t_depblk", owner_agent_id="alice",
                         status="blocked", locked_by_agent=None,
                         metadata={}, dispatched_at_epoch=None),
        ]

    # Stale FAILED reports from the pre-pause attempt still sit in the leader
    # mailbox (peek is non-consuming). They must be suppressed on resume so the
    # reset tasks are not immediately re-failed.
    async def inbox_provider() -> list[str]:
        return ["FAILED: t_local: boom", "FAILED: t_failed: crash"]

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
        compile_result=compile_result,
    )
    await rc.prepare_resume()

    # Stale FAILED messages captured → the tick's failure detector will ignore
    # them (so re-run tasks aren't immediately re-failed on resume).
    assert "FAILED: t_local: boom" in rc._resume_suppressed_failed_msgs
    assert "FAILED: t_failed: crash" in rc._resume_suppressed_failed_msgs

    def _reset_to_pending(flow_id: str) -> bool:
        ct = compile_result.flow_to_clawteam[flow_id]
        return any(u["task_id"] == ct and u.get("status") == "pending" for u in updates)

    # Local in_progress → reset to pending (re-dispatches on first tick).
    assert rc._tasks["t_local"].state == _TaskState.pending
    assert _reset_to_pending("t_local")
    # Failure-blocked with deps done → reset to pending (the node re-runs).
    assert rc._tasks["t_failed"].state == _TaskState.pending
    assert _reset_to_pending("t_failed")
    # Blocked on an incomplete dep → left blocked (not runnable yet).
    assert rc._tasks["t_depblk"].state == _TaskState.blocked
    assert not _reset_to_pending("t_depblk")
    # External in_progress → left as-is; NOT reset.
    assert rc._tasks["t_ext"].state == _TaskState.in_progress
    assert not _reset_to_pending("t_ext")


@pytest.mark.asyncio
async def test_prepare_resume_reemits_task_completed_for_completion_during_pause(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completion that happened while the run was PAUSED (e.g. an external
    human task finished via the todo card, which emits only
    ``external_task_completed`` — never ``task_completed``) must heal the board
    on resume. prepare_resume leaves such a task's book NON-completed so the
    first ``_apply_snapshots`` detects the transition and emits ``task_completed``.
    A completion that WAS already announced is pre-seeded completed so the tick
    does not re-emit. Both suppress re-audit via ``_completed_audited``.
    """
    spec = FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=False, merge_strategy=MergeStrategy.manual),
            FlowAgent(id="ext", kind=AgentKind.external,
                      external=ExternalNodeConfig(channel=ExternalChannel.human),
                      is_leader=False),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=True, merge_strategy=MergeStrategy.manual),
        ],
        tasks=[
            # Completed & announced BEFORE the pause (has a task_completed event).
            FlowTask(id="t_local", owner_agent_id="alice", subject="x",
                     description="", depends_on=[]),
            # External task the user completed WHILE paused → only an
            # external_task_completed event exists, never task_completed.
            FlowTask(id="t_ext", owner_agent_id="ext", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y",
                     description="", depends_on=["t_local", "t_ext"],
                     is_leader_summary=True),
        ],
    )
    run = _persist_flow_and_run(spec)
    compile_result = _compile_result_for_spec(spec, team_name=run.team_name)
    storage = get_storage()
    # t_local's completion was announced before the pause.
    storage.event_append(RunEvent(
        run_id=run.id, type="task_completed", agent_id="alice", task_id="t_local",
        payload={"old": "in_progress", "new": "completed"},
    ))
    # t_ext completed during the pause — ONLY external_task_completed exists.
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_completed", agent_id="ext",
        task_id="t_ext", payload={},
    ))

    class _FakeMcp:
        async def task_update(self, team_name, task_id, **kwargs):
            return {"id": task_id, "status": kwargs.get("status")}

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr(
        "app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp_client,
    )

    completed_snaps = [
        TaskSnapshot(task_id="t_local", owner_agent_id="alice",
                     status="completed", locked_by_agent=None,
                     metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="t_ext", owner_agent_id="ext",
                     status="completed", locked_by_agent=None,
                     metadata={}, dispatched_at_epoch=None),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return completed_snaps

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        compile_result=compile_result,
    )
    await rc.prepare_resume()

    # Announced completion → pre-seeded completed (first tick won't re-emit).
    assert rc._tasks["t_local"].state == _TaskState.completed
    # Completion during pause → left NON-completed so the tick emits it.
    assert rc._tasks["t_ext"].state != _TaskState.completed
    # Both suppress OpenClaw re-audit.
    assert {"t_local", "t_ext"} <= rc._completed_audited

    # The first snapshot application heals the board: task_completed for t_ext
    # (pending→completed), none for t_local (already completed).
    changed = rc._apply_snapshots(completed_snaps)
    assert changed
    assert rc._tasks["t_ext"].state == _TaskState.completed
    completed_events = [
        e for e in storage.event_list(run_id=run.id, limit=1000)
        if e.type == "task_completed"
    ]
    tids = [e.task_id for e in completed_events]
    assert tids.count("t_ext") == 1  # newly emitted on resume
    assert tids.count("t_local") == 1  # NOT re-emitted (pre-seeded completed)


@pytest.mark.asyncio
async def test_prepare_resume_reconciles_external_failure_during_pause(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FAILED external receipt that lands while the run is PAUSED (no live tick
    observed the ``FAILED:`` inbox signal, so ClawTeam stays ``in_progress``) is
    reconciled on resume: prepare_resume surfaces it (``task_failed`` event) and
    resets it to ``pending`` so 继续执行 re-dispatches it. A genuinely-waiting
    external ``in_progress`` task (no completion event for its latest nonce) is
    left untouched — its outstanding ticket must NOT be invalidated.

    Regression for run f2d8b01c57b6: a second remote node that failed after the
    first failure paused the run was neither displayed nor re-dispatched.
    """
    spec = FlowSpec(
        agents=[
            FlowAgent(id="ext_failed", kind=AgentKind.external,
                      external=ExternalNodeConfig(channel=ExternalChannel.remote_csflow,
                                                  base_url="http://x", flow_id="f",
                                                  pair_token_ref="r"),
                      is_leader=False),
            FlowAgent(id="ext_waiting", kind=AgentKind.external,
                      external=ExternalNodeConfig(channel=ExternalChannel.remote_csflow,
                                                  base_url="http://y", flow_id="g",
                                                  pair_token_ref="s"),
                      is_leader=False),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=True, merge_strategy=MergeStrategy.manual),
        ],
        tasks=[
            FlowTask(id="t_failed", owner_agent_id="ext_failed", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="t_waiting", owner_agent_id="ext_waiting", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y",
                     description="", depends_on=["t_failed", "t_waiting"],
                     is_leader_summary=True),
        ],
    )
    run = _persist_flow_and_run(spec)
    compile_result = _compile_result_for_spec(spec, team_name=run.team_name)
    storage = get_storage()
    # t_failed: latest dispatch nonce N1 has a FAILURE receipt (ok=false).
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_dispatched", agent_id="ext_failed",
        task_id="t_failed", payload={"nonce": "N1"},
    ))
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_completed", agent_id="ext_failed",
        task_id="t_failed", payload={"nonce": "N1", "ok": False, "summary": "boom"},
    ))
    # t_waiting: dispatched (nonce N2) but NO completion event → still waiting.
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_dispatched", agent_id="ext_waiting",
        task_id="t_waiting", payload={"nonce": "N2"},
    ))

    class _FakeMcp:
        async def task_update(self, team_name, task_id, **kwargs):
            return {"id": task_id, "status": kwargs.get("status")}

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr(
        "app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp_client,
    )

    snaps = [
        TaskSnapshot(task_id="t_failed", owner_agent_id="ext_failed",
                     status="in_progress", locked_by_agent=None,
                     metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="t_waiting", owner_agent_id="ext_waiting",
                     status="in_progress", locked_by_agent=None,
                     metadata={}, dispatched_at_epoch=None),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return snaps

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        compile_result=compile_result,
    )
    await rc.prepare_resume()

    # Orphaned failure → reset to pending (re-dispatchable) and surfaced.
    assert rc._tasks["t_failed"].state == _TaskState.pending
    # Genuinely-waiting external task → left in_progress, never re-dispatched.
    assert rc._tasks["t_waiting"].state == _TaskState.in_progress

    failed_events = [
        e for e in storage.event_list(run_id=run.id, limit=1000)
        if e.type == "task_failed"
    ]
    failed_tids = [e.task_id for e in failed_events]
    assert failed_tids.count("t_failed") == 1  # surfaced on resume
    assert "t_waiting" not in failed_tids
    # The surfaced failure carries the receipt summary.
    assert any(e.payload.get("detail") == "boom" for e in failed_events)
    # Nonce identity tagged on the surfaced failure + recorded as handled.
    assert any(e.payload.get("nonce") == "N1" for e in failed_events)
    assert "N1" in rc._handled_external_failure_nonces


def _external_only_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(id="ext", kind=AgentKind.external,
                      external=ExternalNodeConfig(
                          channel=ExternalChannel.remote_csflow,
                          base_url="http://x", flow_id="f", pair_token_ref="r"),
                      is_leader=False),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/main",
                      is_leader=True, merge_strategy=MergeStrategy.manual),
        ],
        tasks=[
            FlowTask(id="t_ext", owner_agent_id="ext", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y",
                     description="", depends_on=["t_ext"], is_leader_summary=True),
        ],
    )


@pytest.mark.asyncio
async def test_live_external_failure_detected_via_nonce_not_inbox(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LIVE external failure is detected from the ``external_task_completed``
    nonce event — NOT from any leader-inbox FAILED string. Handling it resets the
    task to pending, tags ``task_failed`` with the nonce, records the nonce as
    handled, and pauses. A second detection pass finds nothing (already handled).
    """
    spec = _external_only_spec()
    run = _persist_flow_and_run(spec)
    compile_result = _compile_result_for_spec(spec, team_name=run.team_name)
    storage = get_storage()
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_dispatched", agent_id="ext",
        task_id="t_ext", payload={"nonce": "N1"},
    ))
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_completed", agent_id="ext",
        task_id="t_ext", payload={"nonce": "N1", "ok": False, "summary": "boom"},
    ))

    class _FakeMcp:
        async def task_update(self, team_name, task_id, **kwargs):
            return {"id": task_id, "status": kwargs.get("status")}

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr(
        "app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp_client,
    )

    rc = RunController(
        run=run, spec=spec, flow_description="d", worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots, compile_result=compile_result,
    )
    rc._tasks["t_ext"].state = _TaskState.in_progress

    snaps = [TaskSnapshot(
        task_id="t_ext", owner_agent_id="ext", status="in_progress",
        locked_by_agent=None, metadata={}, dispatched_at_epoch=0,
    )]
    recs = rc._detect_external_failures(snaps)
    assert len(recs) == 1
    assert recs[0].external_nonce == "N1"
    assert recs[0].reason.value == "leader_inbox_failed"

    await rc._handle_failure(recs[0])
    assert rc._tasks["t_ext"].state == _TaskState.pending
    assert rc._pause_evt.is_set()
    assert "N1" in rc._handled_external_failure_nonces
    failed = [
        e for e in storage.event_list(run_id=run.id, limit=1000)
        if e.type == "task_failed" and e.task_id == "t_ext"
    ]
    assert len(failed) == 1
    assert failed[0].payload.get("nonce") == "N1"

    # Same nonce, still in_progress snapshot → NOT re-detected (handled).
    assert rc._detect_external_failures(snaps) == []


@pytest.mark.asyncio
async def test_external_handled_nonce_seeded_from_events(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuilt controller seeds the handled-nonce set from prior ``task_failed``
    events so it never re-processes a receipt it already surfaced."""
    spec = _external_only_spec()
    run = _persist_flow_and_run(spec)
    compile_result = _compile_result_for_spec(spec, team_name=run.team_name)
    storage = get_storage()
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_dispatched", agent_id="ext",
        task_id="t_ext", payload={"nonce": "N1"},
    ))
    storage.event_append(RunEvent(
        run_id=run.id, type="external_task_completed", agent_id="ext",
        task_id="t_ext", payload={"nonce": "N1", "ok": False, "summary": "boom"},
    ))
    storage.event_append(RunEvent(
        run_id=run.id, type="task_failed", agent_id="ext", task_id="t_ext",
        payload={"reason": "leader_inbox_failed", "nonce": "N1"},
    ))

    class _FakeMcp:
        async def task_update(self, team_name, task_id, **kwargs):
            return {"id": task_id, "status": kwargs.get("status")}

    async def _fake_get_mcp_client(*, user: str):
        del user
        return _FakeMcp()

    monkeypatch.setattr(
        "app.integrations.clawteam_mcp.get_mcp_client", _fake_get_mcp_client,
    )

    rc = RunController(
        run=run, spec=spec, flow_description="d", worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots, compile_result=compile_result,
    )
    rc._seed_handled_external_nonces()
    assert "N1" in rc._handled_external_failure_nonces
    rc._tasks["t_ext"].state = _TaskState.in_progress
    snaps = [TaskSnapshot(
        task_id="t_ext", owner_agent_id="ext", status="in_progress",
        locked_by_agent=None, metadata={}, dispatched_at_epoch=0,
    )]
    # Already handled (seeded) → not re-detected.
    assert rc._detect_external_failures(snaps) == []


def test_backend_failure_always_pauses_even_unattended(fake_lookup) -> None:
    """A detected node failure ALWAYS pauses (never terminates) — the user must
    fix + 继续执行. This holds even for unattended runs, which are then NOT
    auto-resumed (only drain-pauses are). So both a manual and an unattended run
    pause on failure."""
    spec = _make_spec()

    manual = _persist_flow_and_run(spec)
    rc_manual = RunController(
        run=manual, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup, snapshot_provider=_empty_snapshots,
    )
    rc_manual._backend_stop_after_failure(detail="boom")
    assert rc_manual._pause_evt.is_set()
    assert not rc_manual._cancel_evt.is_set()

    # Unattended (delegated / scheduled / MCP) also PAUSES on failure.
    unattended = manual.model_copy(update={"inputs": {"_csflow_unattended": "true"}})
    rc_unatt = RunController(
        run=unattended, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup, snapshot_provider=_empty_snapshots,
    )
    rc_unatt._backend_stop_after_failure(detail="boom")
    assert rc_unatt._pause_evt.is_set()
    assert not rc_unatt._cancel_evt.is_set()


def test_pause_reason_user_outranks_prior_drain(fake_lookup) -> None:
    """If pre-stop drain races onto a controller first, a later user 暂停执行
    must still win the banner reason (not stay stuck on 'service restart')."""
    from app.scheduler.run_metadata import PAUSE_REASON_DRAIN, PAUSE_REASON_USER

    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup, snapshot_provider=_empty_snapshots,
    )
    rc.pause(reason=PAUSE_REASON_DRAIN, detail="service stop / upgrade drain")
    assert rc._pause_reason == PAUSE_REASON_DRAIN
    rc.pause(reason=PAUSE_REASON_USER, detail="user requested pause")
    assert rc._pause_reason == PAUSE_REASON_USER
    assert rc._pause_detail == "user requested pause"


def test_pause_reason_drain_does_not_clobber_user(fake_lookup) -> None:
    from app.scheduler.run_metadata import PAUSE_REASON_DRAIN, PAUSE_REASON_USER

    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup, snapshot_provider=_empty_snapshots,
    )
    rc.pause(reason=PAUSE_REASON_USER, detail="user requested pause")
    rc.pause(reason=PAUSE_REASON_DRAIN, detail="service stop / upgrade drain")
    assert rc._pause_reason == PAUSE_REASON_USER


@pytest.mark.asyncio
async def test_run_loop_finally_shuts_down_remaining_sessions(fake_lookup) -> None:
    spec = _make_spec()
    spec.agents[0] = spec.agents[0].model_copy(update={"dispose_after_done": False})
    spec.agents[1] = spec.agents[1].model_copy(update={"dispose_after_done": False})
    run = _persist_flow_and_run(spec)

    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    state = {"t1_done": False, "ts_done": False}

    async def snap_provider() -> list[TaskSnapshot]:
        out = []
        alice_dispatched = bool(sessions.get("alice") and sessions["alice"].dispatched)
        if state["t1_done"]:
            out.append(TaskSnapshot(
                task_id="t1", owner_agent_id="alice", status="completed",
                locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
            ))
            leader_dispatched = bool(
                sessions.get("leader") and sessions["leader"].dispatched
            )
            if state["ts_done"] and leader_dispatched:
                out.append(TaskSnapshot(
                    task_id="ts", owner_agent_id="leader", status="completed",
                    locked_by_agent="leader", metadata={}, dispatched_at_epoch=None,
                ))
            else:
                out.append(TaskSnapshot(
                    task_id="ts", owner_agent_id="leader", status="pending",
                    locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
                ))
        else:
            out.append(TaskSnapshot(
                task_id="t1", owner_agent_id="alice",
                status="in_progress" if alice_dispatched else "pending",
                locked_by_agent="alice" if alice_dispatched else None,
                metadata={},
                dispatched_at_epoch=time.time() if alice_dispatched else None,
            ))
            out.append(TaskSnapshot(
                task_id="ts", owner_agent_id="leader", status="blocked",
                locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
            ))
        return out

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )

    async def driver():
        await asyncio.sleep(0.05)
        state["t1_done"] = True
        await asyncio.sleep(0.8)
        state["ts_done"] = True

    drive_task = asyncio.create_task(driver())
    await rc.run_loop(max_ticks=50)
    await drive_task

    assert sessions["alice"].state == SessionState.Exited
    assert sessions.get("leader") is not None
    assert sessions["leader"].state == SessionState.Exited
    assert sessions["alice"].shutdowns >= 1
    assert sessions["leader"].shutdowns >= 1


@pytest.mark.asyncio
async def test_dispatch_failure_emits_user_visible_event(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    def factory(agent: FlowAgent) -> WorkerSession:
        return _FailCliDispatchSession(agent=agent, team_name=run.team_name, run_id=run.id)

    snapshots = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )
    await rc.tick()
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=100)
    failed = [e for e in events if e.type == "task_dispatch_failed"]
    assert failed
    payload = failed[-1].payload or {}
    assert payload.get("exit_code") == 17
    assert "tmux target" in str(payload.get("stderr_tail") or "")


@pytest.mark.asyncio
async def test_dispatch_tmux_target_not_found_recovers_within_same_tick(
    fake_lookup,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _FlakyTmuxTargetMissingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _FlakyTmuxTargetMissingSession(
            agent=agent,
            team_name=run.team_name,
            run_id=run.id,
        )
        sessions[agent.id] = s
        return s

    snapshots = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )
    await rc.tick()

    alice = sessions["alice"]
    assert alice.resumed == 1
    assert [tid for tid, _ in alice.dispatched] == ["t1"]
    assert rc._tasks["t1"].state == _TaskState.in_progress
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(e.type == "task_dispatch_recovered" for e in events)
    assert not any(e.type == "task_dispatch_failed" and e.task_id == "t1" for e in events)


@pytest.mark.asyncio
async def test_runtime_socket_error_recovery_requeues_task_and_marks_session_crashed(
    fake_lookup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=None,
    )

    alice = factory(spec.agents[0])
    rc._sessions["alice"] = alice
    await alice.spawn()
    await alice.dispatch(task_id="t1", message="seed")
    rc._tasks["t1"].state = _TaskState.in_progress
    rc._tasks["t1"].dispatched_at = time.time() - 90
    rc.dispatch_clock.mark("t1", time.time() - 90)

    reset_calls: list[tuple[str, str | None]] = []

    async def _fake_reset(flow_task_id: str, *, locked_by: str | None) -> bool:
        reset_calls.append((flow_task_id, locked_by))
        return True

    async def _fake_capture(target: str, history_lines: int = 80) -> str:
        del target, history_lines
        return (
            "...\n"
            "API Error: The socket connection was closed unexpectedly\n"
            "❯ "
        )

    monkeypatch.setattr(rc, "_reset_clawteam_task", _fake_reset)
    monkeypatch.setattr(ctrl_mod, "tmux_capture_pane", _fake_capture)

    changed = await rc._runtime_socket_error_recovery_tick()
    assert changed is True
    assert reset_calls == [("t1", "alice")]
    assert alice.state == SessionState.Crashed
    assert rc._tasks["t1"].state == _TaskState.pending
    assert rc._tasks["t1"].runtime_socket_recoveries == 1
    assert "t1" not in rc.dispatch_clock.table

    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    recovered = [e for e in events if e.type == "task_runtime_socket_recovered"]
    assert recovered, "missing task_runtime_socket_recovered event"
    payload = recovered[-1].payload or {}
    assert payload.get("reason") == "socket_connection_closed_unexpectedly"
    assert payload.get("recovery_count") == 1


@pytest.mark.asyncio
async def test_runtime_socket_error_recovery_waits_without_prompt_before_fallback_window(
    fake_lookup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=None,
    )

    alice = factory(spec.agents[0])
    rc._sessions["alice"] = alice
    await alice.spawn()
    await alice.dispatch(task_id="t1", message="seed")
    rc._tasks["t1"].state = _TaskState.in_progress
    rc._tasks["t1"].dispatched_at = time.time() - 12
    rc.dispatch_clock.mark("t1", time.time() - 12)

    reset_calls: list[tuple[str, str | None]] = []

    async def _fake_reset(flow_task_id: str, *, locked_by: str | None) -> bool:
        reset_calls.append((flow_task_id, locked_by))
        return True

    async def _fake_capture(target: str, history_lines: int = 80) -> str:
        del target, history_lines
        return "API Error: The socket connection was closed unexpectedly"

    monkeypatch.setattr(rc, "_reset_clawteam_task", _fake_reset)
    monkeypatch.setattr(ctrl_mod, "tmux_capture_pane", _fake_capture)

    changed = await rc._runtime_socket_error_recovery_tick()
    assert changed is False
    assert not reset_calls
    assert alice.state == SessionState.Busy
    assert rc._tasks["t1"].state == _TaskState.in_progress

def test_worker_exit_report_emits_structured_exit_event(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=None,
    )
    rc._last_dispatch_failures["alice"] = {
        "exit_code": 1,
        "stderr_tail": "tmux target not found",
    }

    def _fake_read_exit(*, agent_id: str):
        assert agent_id == "alice"
        return {
            "agent_name": "alice",
            "exit_code": 42,
            "stderr_tail": "fatal: auth failed",
            "abandoned_tasks": ["t1"],
            "timestamp": "2026-05-20T03:12:41.857106+00:00",
        }

    monkeypatch.setattr(rc, "_read_latest_exit_journal_entry", _fake_read_exit)
    rc._record_worker_reports([
        WorkerReport(
            from_agent="alice",
            summary="Agent 'alice' exited unexpectedly. Reset 1 task(s) to pending: step",
            task_id="t1",
            timestamp=None,
        ),
    ])
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=100)
    rows = [e for e in events if e.type == "worker_process_exit_observed"]
    assert rows, "missing worker_process_exit_observed event"
    payload = rows[-1].payload or {}
    assert payload.get("exit_code") == 42
    assert payload.get("abandoned_tasks") == ["t1"]
    assert "fatal: auth failed" in str(payload.get("stderr_tail") or "")
    assert payload.get("last_dispatch_failure", {}).get("exit_code") == 1


@pytest.mark.asyncio
async def test_spawn_failure_pauses_run_with_agent_error_event(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)

    def factory(agent: FlowAgent) -> WorkerSession:
        return _FailSpawnSession(agent=agent, team_name=run.team_name, run_id=run.id)

    snapshots = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
    )
    outcome = await rc.run_loop(max_ticks=5)
    # A SessionStartupError is scenario 9 — the backend PAUSES (resumable) with a
    # confirmation hint instead of terminating.
    assert outcome.final_status == RunStatus.paused
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    startup_failed = [e for e in events if e.type == "task_session_start_failed"]
    assert startup_failed, "missing task_session_start_failed event"
    first = startup_failed[0]
    assert first.agent_id == "alice"
    assert first.task_id == "t1"
    assert str(first.payload.get("phase") or "") == "spawn"
    assert not any(e.type == "run_loop_exception" for e in events)


@pytest.mark.asyncio
async def test_custom_dispatch_retries_after_session_missing(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _FlakyDispatchSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _FlakyDispatchSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=_empty_snapshots,
    )
    alice = next(a for a in spec.agents if a.id == "alice")
    await rc._dispatch_custom_task(agent=alice, task_id="custom-1", message="hello")
    sess = sessions["alice"]
    assert sess.resumed == 1
    assert [t for t, _msg in sess.dispatched] == ["custom-1"]


@pytest.mark.asyncio
async def test_complaint_dispatch_uses_headless_for_openclaw(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    called = {"headless": 0, "custom": 0}

    async def fake_headless(*, agent, task_id, message, dispatch_kind="complaint"):
        del agent, task_id, message, dispatch_kind
        called["headless"] += 1

    async def fake_custom(*, agent, task_id, message):
        del agent, task_id, message
        called["custom"] += 1

    monkeypatch.setattr(rc, "_dispatch_openclaw_headless", fake_headless)
    monkeypatch.setattr(rc, "_dispatch_custom_task", fake_custom)
    await rc._dispatch_complaint_task(
        agent=spec.agents[0],
        task_id="complaint-1",
        message="hello",
    )
    assert called == {"headless": 1, "custom": 0}


@pytest.mark.asyncio
async def test_complaint_dispatch_uses_custom_for_non_openclaw(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    called = {"headless": 0, "custom": 0}

    async def fake_headless(*, agent, task_id, message, dispatch_kind="complaint"):
        del agent, task_id, message, dispatch_kind
        called["headless"] += 1

    async def fake_custom(*, agent, task_id, message):
        del agent, task_id, message
        called["custom"] += 1

    leader = next(a for a in spec.agents if a.is_leader)
    monkeypatch.setattr(rc, "_dispatch_openclaw_headless", fake_headless)
    monkeypatch.setattr(rc, "_dispatch_custom_task", fake_custom)
    await rc._dispatch_complaint_task(
        agent=leader,
        task_id="complaint-leader-1",
        message="hello",
    )
    assert called == {"headless": 0, "custom": 1}


@pytest.mark.asyncio
async def test_merge_requirement_dispatch_uses_custom_for_non_openclaw(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    called = {"headless": 0, "custom": 0}

    async def fake_headless(*, agent, task_id, message, dispatch_kind="complaint"):
        del agent, task_id, message, dispatch_kind
        called["headless"] += 1

    async def fake_custom(*, agent, task_id, message):
        del agent, task_id, message
        called["custom"] += 1

    leader = next(a for a in spec.agents if a.is_leader)
    monkeypatch.setattr(rc, "_dispatch_openclaw_headless", fake_headless)
    monkeypatch.setattr(rc, "_dispatch_custom_task", fake_custom)
    await rc._dispatch_merge_requirement_task(
        agent=leader,
        task_id="merge-leader-1",
        message="merge please",
    )
    assert called == {"headless": 0, "custom": 1}


@pytest.mark.asyncio
async def test_openclaw_dispatch_cwd_recovers_missing_worktree_via_session_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)
    recovered_dir = tmp_path / "recovered-worktree"
    recovered_dir.mkdir(parents=True, exist_ok=True)

    class _LookupMissingThenRecovered:
        def __init__(self) -> None:
            self.calls = 0

        async def list_team(self, team, *, repo=None, force=False):  # pragma: no cover - interface only
            del team, repo, force
            return []

        async def get(self, team, agent_name, *, repo=None, force=False):
            del team, force
            self.calls += 1
            if self.calls == 1:
                return None
            return WorktreeInfo(
                agent_name=agent_name,
                branch_name=f"clawteam/{run.team_name}/{agent_name}",
                worktree_path=str(recovered_dir),
                repo_root=repo or "/tmp/main",
                base_branch="main",
            )

    lookup = _LookupMissingThenRecovered()
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=lookup,  # type: ignore[arg-type]
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    sess = _RecordingSession(
        agent=spec.agents[0],
        team_name=run.team_name,
        run_id=run.id,
    )
    sess.adopt_existing(reason="test-session")

    async def fake_ensure(agent: FlowAgent) -> WorkerSession:
        del agent
        return sess

    monkeypatch.setattr(rc, "_ensure_session_idle", fake_ensure)
    cwd, source = await rc._openclaw_dispatch_cwd(spec.agents[0])
    assert cwd == str(recovered_dir)
    assert source == "worktree_created"
    assert lookup.calls >= 2


@pytest.mark.asyncio
async def test_openclaw_headless_dispatch_injects_verified_workdir_context(
    fake_lookup,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    verified = tmp_path / "verified-worktree"
    verified.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}

    async def fake_cwd(agent: FlowAgent) -> tuple[str, str]:
        del agent
        return str(verified), "worktree_created"

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self) -> None:  # pragma: no cover - timeout path only
            return None

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(rc, "_resolve_openclaw_executable", lambda: "openclaw")
    monkeypatch.setattr(rc, "_openclaw_dispatch_cwd", fake_cwd)
    monkeypatch.setattr(ctrl_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    await rc._dispatch_openclaw_headless(
        agent=spec.agents[0],
        task_id="complaint-openclaw-1",
        message="hello world",
    )

    argv = list(captured["argv"])
    assert "--message" in argv
    msg = str(argv[argv.index("--message") + 1])
    assert "## ClawsomeFlow Complaint Dispatch Context" in msg
    assert f"- verified_workdir: `{str(verified)}`" in msg
    assert "- workdir_source: `worktree_created`" in msg
    assert msg.rstrip().endswith("hello world")
    env = captured.get("env") or {}
    assert env.get("CLAWTEAM_AGENT_NAME") == "leader"
    assert env.get("OH_AGENT_NAME") == "leader"
    assert env.get("CLAWTEAM_TEAM_NAME") == run.team_name


@pytest.mark.asyncio
async def test_hermes_headless_dispatch_injects_minimal_complaint_context(
    fake_lookup,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
    )
    verified = tmp_path / "verified-worktree"
    verified.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}

    async def fake_cwd(agent: FlowAgent) -> tuple[str, str]:
        del agent
        return str(verified), "worktree_created"

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self) -> None:
            return None

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(rc, "_hermes_dispatch_cwd", fake_cwd)
    monkeypatch.setattr(shutil, "which", lambda name: "hermes" if name == "hermes" else None)
    monkeypatch.setattr(ctrl_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    hermes_agent = FlowAgent(
        id="myh", kind=AgentKind.hermes, repo="/tmp/r",
        target_branch="main", is_leader=False,
    )
    await rc._dispatch_hermes_headless(
        agent=hermes_agent,
        task_id="complaint-hermes-1",
        message="hello hermes",
    )

    argv = list(captured["argv"])
    # Always a FRESH quiet chat turn: no ``--resume`` (TUI session id is not
    # accepted) and no ``-c`` (most-recent CLI session is not guaranteed to be
    # this run's tmux subtask session — same profile may serve concurrent runs
    # or the operator's own terminal chats).
    assert argv[:6] == ["hermes", "-p", "myh", "chat", "--yolo", "-Q"]
    assert "--resume" not in argv
    assert "-c" not in argv
    assert argv[-2] == "-q"
    msg = str(argv[-1])
    assert msg.startswith("## ClawsomeFlow Complaint Dispatch Context\n\nhello hermes")
    assert "verified_workdir" not in msg


@pytest.mark.asyncio
async def test_openclaw_headless_dispatch_auto_repairs_scope_pending(
    fake_lookup,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    verified = tmp_path / "verified-worktree"
    verified.mkdir(parents=True, exist_ok=True)
    calls = {"spawn": 0, "repair": 0}

    async def fake_cwd(agent: FlowAgent) -> tuple[str, str]:
        del agent
        return str(verified), "worktree_created"

    class _Proc:
        def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")

        def kill(self) -> None:
            return None

    procs = [
        _Proc(returncode=1, stderr="scope upgrade pending approval; requestId=req-a"),
        _Proc(returncode=0, stdout='{"ok":true}'),
    ]

    async def fake_create_subprocess_exec(*argv, **kwargs):
        del argv, kwargs
        idx = calls["spawn"]
        calls["spawn"] += 1
        return procs[idx]

    def fake_scope_repair(*, config):
        del config
        calls["repair"] += 1
        return ["req-a"]

    monkeypatch.setattr(rc, "_resolve_openclaw_executable", lambda: "openclaw")
    monkeypatch.setattr(rc, "_openclaw_dispatch_cwd", fake_cwd)
    monkeypatch.setattr(ctrl_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(ctrl_mod, "repair_pending_scope_upgrades", fake_scope_repair)

    await rc._dispatch_openclaw_headless(
        agent=spec.agents[0],
        task_id="complaint-openclaw-scope",
        message="hello world",
    )
    assert calls["repair"] == 1
    assert calls["spawn"] == 2


def test_leader_complaint_prompt_requires_explicit_from_sender(fake_lookup) -> None:
    spec = _make_spec()
    spec.agents[0] = FlowAgent(
        id="web", kind=AgentKind.openclaw, repo=None,
        is_leader=False, merge_strategy=MergeStrategy.agent_self,
        on_failure=OnFailure.retry, max_retries=2,
    )
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
    )
    prompt = rc._build_leader_complaint_prompt(
        task_id="ct-1",
        complaint_text="请核查合并是否完成",
        targets=[spec.agents[0]],
    )
    assert "--from leader" in prompt
    assert "is mandatory and cannot be omitted" in prompt
    # Leader must NOT mention merge — merge wording is reserved for OpenClaw fixes.
    assert "merge" not in prompt.lower()
    assert "VERY IMPORTANT! you MUST execute" in prompt
    assert "[csflow-complaint-relay:ct-1:<agent_id>]" in prompt
    assert "`<agent_id>` in the header must exactly match the inbox recipient" in prompt


def test_agent_complaint_prompt_merge_only_for_openclaw(fake_lookup) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
    )
    # OpenClaw fix → keeps in-task self-merge instruction.
    oc_prompt = rc._build_agent_complaint_prompt(
        task_id="ct-2", user_complaint="输出不完整",
        leader_feedback="请补全边界条件", merge_required=True,
    )
    assert "immediately perform branch merge in this task" not in oc_prompt
    assert "you must resolve them yourself" in oc_prompt
    assert "VERY IMPORTANT! you MUST execute" in oc_prompt
    # Hermes / other → MUST NOT mention merge at all.
    hermes_prompt = rc._build_agent_complaint_prompt(
        task_id="ct-3", user_complaint="输出不完整",
        leader_feedback="请补全边界条件", merge_required=False,
    )
    assert "merge" not in hermes_prompt.lower()
    assert "Remember what the user was dissatisfied with" in hermes_prompt
    assert "refine your behavioral guidelines accordingly" in hermes_prompt
    assert "Implement fixes based on the complaint" not in hermes_prompt
    assert "Make changes in this task worktree" not in hermes_prompt
    assert "VERY IMPORTANT! you MUST execute" in hermes_prompt
    # Worktree write-prohibition (PR module may still publish this worktree's
    # content after the complaint phase — Hermes must not touch it).
    assert "STRICTLY FORBIDDEN" in hermes_prompt
    assert "do NOT create, modify or delete ANY file" in hermes_prompt
    # OpenClaw fix tasks DO write + self-merge — no prohibition there.
    assert "STRICTLY FORBIDDEN" not in oc_prompt


@pytest.mark.asyncio
async def test_complaint_dispatch_routes_hermes_to_headless(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
    )
    routed: dict[str, str] = {}

    async def fake_oc(*, agent, task_id, message, dispatch_kind="complaint"):
        routed["kind"] = "openclaw"

    async def fake_hermes(*, agent, task_id, message, dispatch_kind="complaint"):
        routed["kind"] = "hermes"

    monkeypatch.setattr(rc, "_dispatch_openclaw_headless", fake_oc)
    monkeypatch.setattr(rc, "_dispatch_hermes_headless", fake_hermes)

    hermes_agent = FlowAgent(
        id="myh", kind=AgentKind.hermes, repo="/tmp/r",
        target_branch="main", is_leader=False,
    )
    await rc._dispatch_complaint_task(agent=hermes_agent, task_id="c1", message="m")
    assert routed["kind"] == "hermes"


@pytest.mark.asyncio
async def test_complaint_phase_excludes_temporary_hermes_workers(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="oc-worker",
                kind=AgentKind.openclaw,
                is_leader=False,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="hermes-temp",
                kind=AgentKind.hermes,
                is_leader=False,
                is_temporary=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.openclaw,
                is_leader=True,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=[],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.complaint_processing
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)

    class _Mcp:
        async def task_create(self, team, subject, *, owner, description="", metadata=None):
            del team, description, metadata
            if "Handle user complaint" in subject and owner == "leader":
                return {"id": "ct-leader-1"}
            if subject.startswith("Handle user complaint:"):
                return {"id": f"ct-fix-{owner}"}
            return {"id": f"ct-{owner}"}

        async def task_get(self, team, task_id):
            del team, task_id
            return {"status": "completed"}

        async def mailbox_peek(self, team, agent_id):
            del team
            if agent_id == "oc-worker":
                return [{
                    "from": "leader",
                    "content": "[csflow-complaint-relay:ct-leader-1:oc-worker] fix it",
                }]
            return []

    async def fake_get_mcp_client(*, user: str):
        del user
        return _Mcp()

    fix_dispatched: list[str] = []

    async def fake_dispatch_complaint_task(*, agent, task_id, message):
        del task_id, message
        fix_dispatched.append(agent.id)

    async def fake_dispatch_merge_requirements(**kwargs):
        del kwargs
        return []

    async def fake_wait_merge(*, mcp, task_ids, phase):
        del mcp, task_ids, phase
        return None

    async def noop_tail_cleanup(**kwargs):
        del kwargs
        class _Out:
            team_cleaned = False
            cleaned_openclaw_agents: list[str] = []
            failed_openclaw_agents: list[str] = []
        return _Out()

    monkeypatch.setattr("app.integrations.clawteam_mcp.get_mcp_client", fake_get_mcp_client)
    monkeypatch.setattr(ctrl_mod, "run_terminal_tail_cleanup", noop_tail_cleanup)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
    )
    monkeypatch.setattr(rc, "_dispatch_complaint_task", fake_dispatch_complaint_task)
    monkeypatch.setattr(rc, "_dispatch_merge_requirements", fake_dispatch_merge_requirements)
    monkeypatch.setattr(rc, "_wait_for_merge_requirement_tasks", fake_wait_merge)

    await rc.run_user_complaint_phase(complaint_text="请改进质量")

    assert "oc-worker" in fix_dispatched
    assert "hermes-temp" not in fix_dispatched


@pytest.mark.asyncio
async def test_complaint_phase_leader_dispatch_failure_does_not_stall_run(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="oc1",
                kind=AgentKind.openclaw,
                is_leader=False,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=[],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.complaint_processing
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)

    class _Mcp:
        async def task_create(self, *args, **kwargs):
            del args, kwargs
            return {"id": "ct-leader-1"}

    async def fake_get_mcp_client(*, user: str):
        del user
        return _Mcp()

    async def fake_dispatch(*, agent, task_id, message):
        del agent, task_id, message
        raise RuntimeError("leader process vanished")

    async def noop_tail_cleanup(**kwargs):
        del kwargs
        class _Out:
            team_cleaned = False
            cleaned_openclaw_agents: list[str] = []
            failed_openclaw_agents: list[str] = []
        return _Out()

    monkeypatch.setattr("app.integrations.clawteam_mcp.get_mcp_client", fake_get_mcp_client)
    monkeypatch.setattr(ctrl_mod, "run_terminal_tail_cleanup", noop_tail_cleanup)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    monkeypatch.setattr(rc, "_dispatch_custom_task", fake_dispatch)

    await rc.run_user_complaint_phase(complaint_text="请改进质量")
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.completed
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert not any(e.type == "run_complaint_phase_skipped" for e in events)


@pytest.mark.asyncio
async def test_complaint_phase_merges_non_target_agents_after_manager_complete(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="oc-a",
                kind=AgentKind.openclaw,
                is_leader=False,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="oc-b",
                kind=AgentKind.openclaw,
                is_leader=False,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.openclaw,
                is_leader=True,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=[],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.complaint_processing
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)

    class _Mcp:
        async def task_create(self, team, subject, *, owner, description="", metadata=None):
            del team, description, metadata
            if "Handle user complaint" in subject and owner == "leader":
                return {"id": "ct-leader-1"}
            if subject.startswith("Handle user complaint:"):
                return {"id": f"ct-fix-{owner}"}
            return {"id": f"ct-{owner}"}

        async def task_get(self, team, task_id):
            del team, task_id
            return {"status": "completed"}

        async def mailbox_peek(self, team, agent_id):
            del team
            if agent_id == "oc-a":
                return [{
                    "from": "leader",
                    "content": "[csflow-complaint-relay:ct-leader-1:oc-a] 请补全异常路径",
                }]
            return []

    async def fake_get_mcp_client(*, user: str):
        del user
        return _Mcp()

    async def fake_dispatch_complaint_task(*, agent, task_id, message):
        del agent, task_id, message
        return None

    merge_calls: list[tuple[str, list[str]]] = []

    async def fake_dispatch_merge_requirements(
        *,
        mcp,
        agents,
        phase,
        reason,
        source_task_id=None,
    ):
        del mcp, reason, source_task_id
        merge_calls.append((phase, [a.id for a in agents]))
        return []

    async def fake_wait_merge(*, mcp, task_ids, phase):
        del mcp, task_ids, phase
        return None

    async def noop_tail_cleanup(**kwargs):
        del kwargs
        class _Out:
            team_cleaned = False
            cleaned_openclaw_agents: list[str] = []
            failed_openclaw_agents: list[str] = []
        return _Out()

    monkeypatch.setattr("app.integrations.clawteam_mcp.get_mcp_client", fake_get_mcp_client)
    monkeypatch.setattr(ctrl_mod, "run_terminal_tail_cleanup", noop_tail_cleanup)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    monkeypatch.setattr(rc, "_dispatch_complaint_task", fake_dispatch_complaint_task)
    monkeypatch.setattr(rc, "_dispatch_merge_requirements", fake_dispatch_merge_requirements)
    monkeypatch.setattr(rc, "_wait_for_merge_requirement_tasks", fake_wait_merge)

    await rc.run_user_complaint_phase(complaint_text="请改进质量")

    assert merge_calls == [("post_manager_complete", ["oc-b"])]


@pytest.mark.asyncio
async def test_skip_complaint_phase_dispatches_direct_merge_requirements(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.awaiting_user_complaint
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)

    class _Mcp:
        async def task_create(self, *args, **kwargs):
            del args, kwargs
            return {"id": "ct-merge-1"}

        async def task_get(self, *args, **kwargs):
            del args, kwargs
            return {"status": "completed"}

    async def fake_get_mcp_client(*, user: str):
        del user
        return _Mcp()

    merge_calls: list[tuple[str, list[str]]] = []

    async def fake_dispatch_merge_requirements(
        *,
        mcp,
        agents,
        phase,
        reason,
        source_task_id=None,
    ):
        del mcp, reason, source_task_id
        merge_calls.append((phase, [a.id for a in agents]))
        return []

    async def fake_wait_merge(*, mcp, task_ids, phase):
        del mcp, task_ids, phase
        return None

    async def noop_tail_cleanup(**kwargs):
        del kwargs
        class _Out:
            team_cleaned = False
            cleaned_openclaw_agents: list[str] = []
            failed_openclaw_agents: list[str] = []
        return _Out()

    monkeypatch.setattr("app.integrations.clawteam_mcp.get_mcp_client", fake_get_mcp_client)
    monkeypatch.setattr(ctrl_mod, "run_terminal_tail_cleanup", noop_tail_cleanup)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )
    monkeypatch.setattr(rc, "_dispatch_merge_requirements", fake_dispatch_merge_requirements)
    monkeypatch.setattr(rc, "_wait_for_merge_requirement_tasks", fake_wait_merge)

    await rc.skip_user_complaint_phase()
    assert merge_calls == [("satisfaction_direct", ["leader"])]


@pytest.mark.asyncio
async def test_merge_requirement_dispatch_failure_is_internal_only(
    fake_lookup,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.complaint_processing
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)

    class _Mcp:
        async def task_create(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("mcp task_create unavailable")

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )

    task_ids = await rc._dispatch_merge_requirements(
        mcp=_Mcp(),
        agents=spec.agents,
        phase="satisfaction_direct",
        reason="test",
    )
    assert task_ids == []
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=100)
    assert not any(e.type == "run_merge_requirement_dispatch_failed" for e in events)


@pytest.mark.asyncio
async def test_merge_requirement_wait_failure_is_internal_only(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_openclaw_spec()
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.complaint_processing
    run.inputs = {"_csflow_post_complaint_final_status": "completed"}
    get_storage().run_update(run)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
    )

    async def _boom_wait(*, mcp, task_ids, timeout_sec, poll_sec=2.0):
        del mcp, task_ids, timeout_sec, poll_sec
        raise RuntimeError("wait failed")

    monkeypatch.setattr(rc, "_wait_for_clawteam_tasks_completed", _boom_wait)
    await rc._wait_for_merge_requirement_tasks(
        mcp=object(),
        task_ids=["ct-1"],
        phase="satisfaction_direct",
    )
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=100)
    assert not any(e.type == "run_merge_requirement_wait_failed" for e in events)


def _make_checkpoint_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(
                id="alice",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="bob",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1",
                owner_agent_id="alice",
                subject="upstream",
                description="do upstream",
                requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t2",
                owner_agent_id="bob",
                subject="downstream",
                description="do downstream",
                depends_on=["t1"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="wrap up",
                depends_on=["t2"],
                is_leader_summary=True,
            ),
        ],
    )


def _make_multi_item_checkpoint_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(
                id="alice",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="bob",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1",
                owner_agent_id="alice",
                subject="upstream-a",
                description="do upstream-a",
                requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t1b",
                owner_agent_id="bob",
                subject="upstream-b",
                description="do upstream-b",
                requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t2",
                owner_agent_id="bob",
                subject="downstream",
                description="do downstream",
                depends_on=["t1", "t1b"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="wrap up",
                depends_on=["t2"],
                is_leader_summary=True,
            ),
        ],
    )


def _make_same_owner_multi_item_checkpoint_spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(
                id="alice",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="bob",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry,
                max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1",
                owner_agent_id="alice",
                subject="upstream-a",
                description="do upstream-a",
                requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t1b",
                owner_agent_id="alice",
                subject="upstream-b",
                description="do upstream-b",
                requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t2",
                owner_agent_id="bob",
                subject="downstream",
                description="do downstream",
                depends_on=["t1", "t1b"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="wrap up",
                depends_on=["t2"],
                is_leader_summary=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_checkpoint_blocks_dispatch_until_all_items_approved(fake_lookup) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )

    activity = await rc.tick()
    assert activity is True
    assert run.status == RunStatus.awaiting_user_checkpoint
    assert sessions.get("bob") is None or sessions["bob"].dispatched == []

    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert cp["downstream_task_id"] == "t2"
    assert [it["task_id"] for it in cp["items"]] == ["t1"]
    assert cp["items"][0]["decision"] == "pending"

    await rc.approve_checkpoint_item(upstream_task_id="t1")
    assert run.status == RunStatus.running
    assert rc.checkpoint_snapshot() is None

    activity = await rc.tick()
    assert activity is True
    assert "bob" in sessions
    assert sessions["bob"].dispatched
    assert sessions["bob"].dispatched[-1][0] == "t2"


@pytest.mark.asyncio
async def test_checkpoint_rerun_dispatches_feedback_prompt_and_marks_in_progress(
    fake_lookup,
) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    assert run.status == RunStatus.awaiting_user_checkpoint

    await rc.request_checkpoint_rerun(
        upstream_task_id="t1",
        feedback="请补充边界条件和风险说明",
    )
    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert cp["items"][0]["decision"] == "rerun_requested"
    assert cp["items"][0]["rerun_count"] == 1

    alice = sessions.get("alice")
    assert alice is not None
    assert alice.dispatched
    rerun_task_id, rerun_prompt = alice.dispatched[-1]
    assert rerun_task_id == "checkpoint-rerun-t1"
    assert "task t1 done:" in rerun_prompt
    assert "请补充边界条件和风险说明" in rerun_prompt

    t1_book = rc._tasks["t1"]
    assert t1_book.state == _TaskState.in_progress


@pytest.mark.asyncio
async def test_checkpoint_processes_multiple_upstreams_one_by_one(
    fake_lookup,
) -> None:
    spec = _make_multi_item_checkpoint_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t1b",
            owner_agent_id="bob",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    base_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    inbox_rows: list[dict[str, str]] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: baseline-a",
            "timestamp": base_ts.isoformat(),
        },
        {
            "from_agent": "bob",
            "task_id": "t1b",
            "content": "task t1b done: baseline-b",
            "timestamp": (base_ts + timedelta(seconds=1)).isoformat(),
        },
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return list(inbox_rows)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    assert run.status == RunStatus.awaiting_user_checkpoint
    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert [row["task_id"] for row in cp["items"]] == ["t1"]

    await rc.approve_checkpoint_item(upstream_task_id="t1")
    assert rc.checkpoint_snapshot() is None
    assert run.status == RunStatus.running

    await rc.tick()
    cp2 = rc.checkpoint_snapshot()
    assert cp2 is not None
    assert [row["task_id"] for row in cp2["items"]] == ["t1b"]
    assert sessions.get("bob") is None or sessions["bob"].dispatched == []

    await rc.approve_checkpoint_item(upstream_task_id="t1b")
    assert run.status == RunStatus.running
    await rc.tick()
    assert "bob" in sessions
    assert sessions["bob"].dispatched
    assert sessions["bob"].dispatched[-1][0] == "t2"


@pytest.mark.asyncio
async def test_checkpoint_rerun_keeps_focus_on_current_item_until_approved(
    fake_lookup,
) -> None:
    spec = _make_multi_item_checkpoint_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t1b",
            owner_agent_id="bob",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    inbox_rows: list[dict[str, str]] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: baseline-a",
            "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        },
        {
            "from_agent": "bob",
            "task_id": "t1b",
            "content": "task t1b done: baseline-b",
            "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat(),
        },
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return list(inbox_rows)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert [row["task_id"] for row in cp["items"]] == ["t1"]
    await rc.request_checkpoint_rerun(upstream_task_id="t1", feedback="重跑 A")

    with pytest.raises(KeyError) as exc_info:
        await rc.request_checkpoint_rerun(upstream_task_id="t1b", feedback="重跑 B")
    assert "t1b" in str(exc_info.value)

    inbox_rows[0] = {
        "from_agent": "alice",
        "task_id": "t1",
        "content": "task t1 done: refreshed-a",
        "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
    }
    changed = await rc._refresh_checkpoint_outputs()
    assert changed is True
    cp_after_refresh = rc.checkpoint_snapshot()
    assert cp_after_refresh is not None
    assert cp_after_refresh["items"][0]["task_id"] == "t1"
    assert cp_after_refresh["items"][0]["decision"] == "pending"

    await rc.approve_checkpoint_item(upstream_task_id="t1")
    await rc.tick()
    cp2 = rc.checkpoint_snapshot()
    assert cp2 is not None
    assert [row["task_id"] for row in cp2["items"]] == ["t1b"]


@pytest.mark.asyncio
async def test_checkpoint_rerun_ignores_stale_inbox_report_before_request(
    fake_lookup,
) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    now = datetime.now(timezone.utc)
    inbox_rows: list[dict[str, str]] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: baseline",
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
        },
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return list(inbox_rows)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    await rc.request_checkpoint_rerun(
        upstream_task_id="t1",
        feedback="需要补充失败路径",
    )

    # Older report should be ignored after rerun is requested.
    inbox_rows[:] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: stale-should-ignore",
            "timestamp": (now - timedelta(hours=1)).isoformat(),
        },
    ]
    changed = await rc._refresh_checkpoint_outputs()
    assert changed is False
    cp = rc.checkpoint_snapshot()
    assert cp is not None
    item = cp["items"][0]
    assert item["decision"] == "rerun_requested"
    assert "baseline" in (item["summary"] or "")

    inbox_rows[:] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: fresh-fixed",
            "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        },
    ]
    changed2 = await rc._refresh_checkpoint_outputs()
    assert changed2 is True
    cp2 = rc.checkpoint_snapshot()
    assert cp2 is not None
    item2 = cp2["items"][0]
    assert item2["decision"] == "pending"
    assert item2["has_unread_update"] is True
    assert "fresh-fixed" in (item2["summary"] or "")


@pytest.mark.asyncio
async def test_downstream_uses_user_approved_checkpoint_output(fake_lookup) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    now = datetime.now(timezone.utc)
    inbox_rows: list[dict[str, str]] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: baseline-old",
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
        },
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return list(inbox_rows)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    await rc.request_checkpoint_rerun(
        upstream_task_id="t1",
        feedback="请更新为最终版",
    )
    inbox_rows[:] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: approved-new",
            "timestamp": (now + timedelta(seconds=30)).isoformat(),
        },
    ]
    changed = await rc._refresh_checkpoint_outputs()
    assert changed is True
    await rc.approve_checkpoint_item(upstream_task_id="t1")

    # Even if a newer message appears later, downstream must keep using the
    # explicitly approved checkpoint version.
    inbox_rows[:] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: late-unapproved",
            "timestamp": (now + timedelta(minutes=1)).isoformat(),
        },
    ]
    await rc.tick()

    bob = sessions.get("bob")
    assert bob is not None
    assert bob.dispatched
    _, prompt = bob.dispatched[-1]
    assert "approved-new" in prompt
    assert "late-unapproved" not in prompt


@pytest.mark.asyncio
async def test_checkpoint_mark_read_clears_unread_highlight(fake_lookup) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    now = datetime.now(timezone.utc)
    inbox_rows: list[dict[str, str]] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: baseline",
            "timestamp": (now - timedelta(minutes=10)).isoformat(),
        },
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return list(inbox_rows)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    await rc.request_checkpoint_rerun(
        upstream_task_id="t1",
        feedback="补充边界",
    )
    inbox_rows[:] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: updated",
            "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat(),
        },
    ]
    changed = await rc._refresh_checkpoint_outputs()
    assert changed is True
    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert cp["items"][0]["has_unread_update"] is True

    await rc.mark_checkpoint_item_read(upstream_task_id="t1")
    cp2 = rc.checkpoint_snapshot()
    assert cp2 is not None
    assert cp2["items"][0]["has_unread_update"] is False


@pytest.mark.asyncio
async def test_checkpoint_rerun_dispatch_failure_rolls_back_task_state_and_clock(
    fake_lookup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    now = datetime.now(timezone.utc)
    inbox_rows: list[dict[str, str]] = [
        {
            "from_agent": "alice",
            "task_id": "t1",
            "content": "task t1 done: baseline",
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
        },
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return list(inbox_rows)

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()

    status_updates: list[tuple[str, str, str, bool]] = []

    async def _fake_update(flow_task_id: str, *, status: str, caller: str, force: bool = True) -> bool:
        status_updates.append((flow_task_id, status, caller, force))
        return True

    async def _boom_dispatch(*, agent: FlowAgent, task_id: str, message: str) -> None:
        del agent, task_id, message
        raise RuntimeError("custom dispatch boom")

    monkeypatch.setattr(rc, "_update_clawteam_task_status", _fake_update)
    monkeypatch.setattr(rc, "_dispatch_custom_task", _boom_dispatch)

    with pytest.raises(RuntimeError) as exc_info:
        await rc.request_checkpoint_rerun(
            upstream_task_id="t1",
            feedback="请补充失败分支",
        )
    assert "custom dispatch boom" in str(exc_info.value)

    assert len(status_updates) >= 2
    assert status_updates[0][0] == "t1" and status_updates[0][1] == "in_progress"
    assert status_updates[1][0] == "t1" and status_updates[1][1] == "completed"

    t1_book = rc._tasks["t1"]
    assert t1_book.state == _TaskState.completed
    assert t1_book.dispatched_at is None
    assert "t1" not in rc.dispatch_clock.table

    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert cp["items"][0]["task_id"] == "t1"
    assert cp["items"][0]["decision"] == "pending"

    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(
        e.type == "task_checkpoint_updated"
        and (e.payload or {}).get("decision") == "rerun_dispatch_failed"
        for e in events
    )


@pytest.mark.asyncio
async def test_checkpoint_abort_emits_checkpoint_cleared_event(fake_lookup) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1",
            owner_agent_id="alice",
            status="completed",
            locked_by_agent=None,
            metadata={},
            dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2",
            owner_agent_id="bob",
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

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    assert run.status == RunStatus.awaiting_user_checkpoint
    assert rc.checkpoint_snapshot() is not None

    run.status = RunStatus.aborted
    rc.cancel()
    changed = await rc.tick()
    assert changed is True
    assert rc.checkpoint_snapshot() is None

    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(
        e.type == "task_checkpoint_cleared"
        and (e.payload or {}).get("decision") == "cancelled"
        for e in events
    )


async def _empty_snapshots() -> list[TaskSnapshot]:
    return []


def _merge_req_controller(spec: FlowSpec, fake_lookup) -> RunController:
    run = _persist_flow_and_run(spec)
    return RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
        leader_inbox_provider=None,
    )


def test_merge_requirement_agents_normal_returns_openclaw(fake_lookup) -> None:
    # OpenClaw leader → normal mode defers its merge to the complaint phase.
    spec = _make_worker_plus_openclaw_leader_spec()
    rc = _merge_req_controller(spec, fake_lookup)
    assert [a.id for a in rc._merge_requirement_agents()] == ["leader"]


@pytest.mark.parametrize("mode_key", ["csflow.easy_mode", "csflow.dev_mode"])
def test_merge_requirement_agents_easy_dev_returns_empty(fake_lookup, mode_key: str) -> None:
    # Easy / dev mode merge in-task, so the complaint phase must not re-dispatch
    # standalone merge-requirement tasks — even with an OpenClaw agent present.
    spec = _make_worker_plus_openclaw_leader_spec()
    spec.variables = {mode_key: "true"}
    rc = _merge_req_controller(spec, fake_lookup)
    assert rc._merge_requirement_agents() == []


# ── tick-level leader inbox memoization ────────────────────────────────


@pytest.mark.asyncio
async def test_leader_inbox_peeked_at_most_once_per_tick(fake_lookup) -> None:
    """Failure detection AND leader-summary dispatch both read the leader
    inbox inside one tick; the MCP ``mailbox_peek`` must fire only once
    (pure RPC dedup — peek is non-consuming so data is identical)."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    calls = {"n": 0}

    async def inbox_provider() -> list[str]:
        calls["n"] += 1
        return ["task ct-t1 done: all good"]

    snapshots = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="completed",
            locked_by_agent="alice", metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )

    await rc.tick()
    assert calls["n"] == 1, (
        f"leader inbox peeked {calls['n']}× in one tick — memo not applied"
    )
    # The memo must NOT leak across ticks: the next tick peeks fresh.
    await rc.tick()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_leader_inbox_not_cached_outside_tick(fake_lookup) -> None:
    """Complaint phase / terminal flush run outside tick(); they must always
    see a fresh peek (no stale memo)."""
    spec = _make_spec()
    run = _persist_flow_and_run(spec)
    calls = {"n": 0}

    async def inbox_provider() -> list[str]:
        calls["n"] += 1
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
        leader_inbox_provider=inbox_provider,
    )

    await rc._fetch_leader_inbox()
    await rc._fetch_leader_inbox()
    assert calls["n"] == 2


def test_headless_dispatch_timeout_matches_task_floor() -> None:
    """A legitimate long complaint-fix turn must never be killed before the
    scheduler's own task-timeout floor (user invariant: running-normally
    tasks are NEVER failed just for taking long)."""
    from app.scheduler.controller import _OPENCLAW_HEADLESS_TIMEOUT_SEC
    from app.scheduler.failure import MIN_TASK_TIMEOUT_SECONDS

    assert _OPENCLAW_HEADLESS_TIMEOUT_SEC == MIN_TASK_TIMEOUT_SECONDS
    assert _OPENCLAW_HEADLESS_TIMEOUT_SEC >= 14400


# ── external execution nodes: checkpoint + awaiting_external ─────────────


def _make_external_checkpoint_spec() -> FlowSpec:
    """t1 (external human node, requires checkpoint) → t2 (local) → ts."""
    from app.models import ExternalChannel, ExternalNodeConfig

    return FlowSpec(
        agents=[
            FlowAgent(
                id="ext-node",
                kind=AgentKind.external,
                external=ExternalNodeConfig(
                    channel=ExternalChannel.human, assignee="Alice",
                ),
            ),
            FlowAgent(
                id="bob",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=False,
                merge_strategy=MergeStrategy.manual,
            ),
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1",
                owner_agent_id="ext-node",
                subject="external upstream",
                description="review the deliverable",
                requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t2",
                owner_agent_id="bob",
                subject="downstream",
                description="do downstream",
                depends_on=["t1"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="wrap up",
                depends_on=["t2"],
                is_leader_summary=True,
            ),
        ],
    )


def _external_checkpoint_snapshots() -> list[TaskSnapshot]:
    return [
        TaskSnapshot(task_id="t1", owner_agent_id="ext-node", status="completed",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="t2", owner_agent_id="bob", status="pending",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="ts", owner_agent_id="leader", status="blocked",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
    ]


@pytest.mark.asyncio
async def test_waiting_webhook_external_redispatch_invalidates_prior_nonce(
    fake_lookup, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run-detail redispatch for webhook/remote: fresh ticket, old nonce stale."""
    from app.models import ExternalChannel, ExternalNodeConfig
    from app.scheduler.sessions.external import ExternalNodeSession
    from app.services import external_tasks as ext_svc

    nonces: list[str] = []

    async def fake_dispatch(**kw: Any) -> None:
        from app.events import publish_run_event

        nonce = f"nonce-{len(nonces) + 1}"
        nonces.append(nonce)
        pkg = dict(kw.get("package") or {})
        pkg.setdefault("leaderAgentId", "leader")
        pkg.setdefault("clawteamTaskId", kw["task_id"])
        publish_run_event(
            kw["storage"],
            run_id=kw["run_id"],
            event_type=ext_svc.EXTERNAL_TASK_DISPATCHED_EVENT,
            agent_id=kw["agent"].id,
            task_id=kw["task_id"],
            payload={
                "channel": kw["agent"].external.channel.value,
                "nonce": nonce,
                "message": kw["message"],
                **pkg,
            },
        )

    monkeypatch.setattr(ext_svc, "dispatch_external_task", fake_dispatch)

    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="wh",
                kind=AgentKind.external,
                external=ExternalNodeConfig(
                    channel=ExternalChannel.webhook,
                    endpoint_url="https://partner.example/hook",
                ),
            ),
            FlowAgent(
                id="leader", kind=AgentKind.claude, repo="/tmp/main",
                is_leader=True, merge_strategy=MergeStrategy.manual,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1", owner_agent_id="wh", subject="call partner",
                description="do remote work",
            ),
            FlowTask(
                id="ts", owner_agent_id="leader", subject="summary",
                description="wrap", depends_on=["t1"], is_leader_summary=True,
            ),
        ],
    )
    run = _persist_flow_and_run(spec)
    storage = get_storage()

    def factory(agent: FlowAgent) -> WorkerSession:
        if agent.kind == AgentKind.external:
            async def package_provider(task_id: str) -> dict[str, Any]:
                return {"subject": "call partner", "clawteamTaskId": task_id}

            return ExternalNodeSession(
                agent=agent, team_name=run.team_name, run_id=run.id,
                storage=storage, package_provider=package_provider,
            )
        return _RecordingSession(
            agent=agent, team_name=run.team_name, run_id=run.id,
        )

    snapshots = [
        TaskSnapshot(task_id="t1", owner_agent_id="wh", status="pending",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="ts", owner_agent_id="leader", status="blocked",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=lambda: [],
    )
    await rc.tick()
    assert len(nonces) == 1
    assert rc._sessions["wh"].state.value == "busy"

    await rc.redispatch_waiting_external_task(task_id="t1")
    assert nonces == ["nonce-1", "nonce-2"]
    latest = ext_svc.latest_dispatch_event(storage, run_id=run.id, task_id="t1")
    assert latest is not None
    assert (latest.payload or {}).get("nonce") == "nonce-2"
    # Session ends Busy again after the replacement dispatch.
    assert rc._sessions["wh"].state.value == "busy"

    # Late callback from the FIRST dispatch must be rejected — no ClawTeam write.
    class _FakeMcp:
        def __init__(self) -> None:
            self.mailbox_calls: list[dict[str, Any]] = []
            self.task_updates: list[dict[str, Any]] = []

        async def mailbox_send(self, **kw: Any) -> None:
            self.mailbox_calls.append(kw)

        async def task_update(self, **kw: Any) -> dict[str, Any]:
            self.task_updates.append(kw)
            return {}

    fake = _FakeMcp()

    async def _mcp(*, user: str | None = None) -> _FakeMcp:
        del user
        return fake

    monkeypatch.setattr(
        "app.integrations.clawteam_mcp.get_mcp_client", _mcp,
    )
    with pytest.raises(ext_svc.ExternalTaskError) as stale_exc:
        await ext_svc.complete_external_task(
            storage=storage,
            run=run,
            task_id="t1",
            nonce="nonce-1",
            ok=True,
            summary="late result from first dispatch",
            source="test",
        )
    assert stale_exc.value.code == "EXTERNAL_TICKET_STALE"
    assert fake.mailbox_calls == []
    assert fake.task_updates == []

    # The NEW dispatch's nonce is still accepted.
    recorded = await ext_svc.complete_external_task(
        storage=storage,
        run=run,
        task_id="t1",
        nonce="nonce-2",
        ok=True,
        summary="fresh result",
        source="test",
    )
    assert recorded["status"] == "recorded"
    assert len(fake.task_updates) == 1
    assert "fresh result" in fake.mailbox_calls[0]["content"]


@pytest.mark.asyncio
async def test_checkpoint_external_item_one_click_redispatch(fake_lookup) -> None:
    """External checkpoint items rerun WITHOUT feedback by re-dispatching the
    ORIGINAL task (same task id — not a checkpoint-rerun-* custom task), and
    the checkpoint payload exposes owner_kind / external_channel so the UI
    can hide the feedback + diff affordances."""
    spec = _make_external_checkpoint_spec()
    run = _persist_flow_and_run(spec)
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots = _external_checkpoint_snapshots()

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    assert run.status == RunStatus.awaiting_user_checkpoint

    cp = rc.checkpoint_snapshot()
    assert cp is not None
    item = cp["items"][0]
    assert item["task_id"] == "t1"
    assert item["owner_kind"] == "external"
    assert item["external_channel"] == "human"

    # One-click rerun: empty feedback must be accepted for external items.
    await rc.request_checkpoint_rerun(upstream_task_id="t1", feedback="")
    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert cp["items"][0]["decision"] == "rerun_requested"

    ext_sess = sessions.get("ext-node")
    assert ext_sess is not None
    assert ext_sess.dispatched
    rerun_task_id, rerun_message = ext_sess.dispatched[-1]
    # Original task id — the external session re-mints the ticket + re-sends
    # the channel outbound for THIS task (fresh nonce invalidates the old one).
    assert rerun_task_id == "t1"
    assert "checkpoint-rerun" not in rerun_task_id
    # The message is the external task sheet, not a feedback-preamble prompt.
    assert "ClawsomeFlow External Task" in rerun_message
    assert "ClawsomeFlow Manual Checkpoint Rerun" not in rerun_message

    assert rc._tasks["t1"].state == _TaskState.in_progress


@pytest.mark.asyncio
async def test_checkpoint_local_agent_rerun_still_requires_feedback(
    fake_lookup,
) -> None:
    spec = _make_checkpoint_spec()
    run = _persist_flow_and_run(spec)

    snapshots = [
        TaskSnapshot(task_id="t1", owner_agent_id="alice", status="completed",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="t2", owner_agent_id="bob", status="pending",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="ts", owner_agent_id="leader", status="blocked",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    await rc.tick()
    assert run.status == RunStatus.awaiting_user_checkpoint
    with pytest.raises(ValueError, match="feedback is required"):
        await rc.request_checkpoint_rerun(upstream_task_id="t1", feedback="  ")


@pytest.mark.asyncio
async def test_eager_checkpoint_opens_while_parallel_external_in_flight(
    fake_lookup,
) -> None:
    """A completed checkpoint-required task must open review immediately even
    when its only dependent is the summary (not yet ready). Parallel external
    in-flight tasks keep their waiting card — both coexist."""
    from app.models import ExternalChannel, ExternalNodeConfig

    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="local", kind=AgentKind.claude, repo="/tmp/main",
                merge_strategy=MergeStrategy.manual,
            ),
            FlowAgent(
                id="ext-node", kind=AgentKind.external,
                external=ExternalNodeConfig(channel=ExternalChannel.remote_csflow,
                                            base_url="http://peer:17017",
                                            flow_id="flow-x",
                                            pair_token_ref="peer"),
            ),
            FlowAgent(
                id="leader", kind=AgentKind.claude, repo="/tmp/main",
                is_leader=True, merge_strategy=MergeStrategy.manual,
            ),
        ],
        tasks=[
            FlowTask(
                id="t_local", owner_agent_id="local", subject="safety notes",
                description="done", requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t_ext", owner_agent_id="ext-node", subject="assemble",
                description="remote work", requires_human_checkpoint=True,
            ),
            FlowTask(
                id="ts", owner_agent_id="leader", subject="summary",
                description="wrap",
                depends_on=["t_local", "t_ext"],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.running
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    # Local checkpoint task already completed; parallel external still
    # in_progress — summary must NOT be ready (not all non-summary done).
    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t_local", owner_agent_id="local", status="completed",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t_ext", owner_agent_id="ext-node", status="in_progress",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )

    await rc.tick()
    assert run.status == RunStatus.awaiting_user_checkpoint
    cp = rc.checkpoint_snapshot()
    assert cp is not None
    assert [it["task_id"] for it in cp["items"]] == ["t_local"]
    # Summary must stay gated (not all non-summary tasks completed).
    assert sessions.get("leader") is None or sessions["leader"].dispatched == []


@pytest.mark.asyncio
async def test_checkpoint_pauses_local_but_allows_parallel_external(
    fake_lookup,
) -> None:
    """While a checkpoint is open: local ready tasks stay paused (worktree
    safety); independent external ready tasks still dispatch."""
    from app.models import ExternalChannel, ExternalNodeConfig

    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="alice", kind=AgentKind.claude, repo="/tmp/main",
                merge_strategy=MergeStrategy.manual,
            ),
            FlowAgent(
                id="bob", kind=AgentKind.claude, repo="/tmp/main",
                merge_strategy=MergeStrategy.manual,
            ),
            FlowAgent(
                id="charlie", kind=AgentKind.claude, repo="/tmp/main",
                merge_strategy=MergeStrategy.manual,
            ),
            FlowAgent(
                id="ext-node", kind=AgentKind.external,
                external=ExternalNodeConfig(channel=ExternalChannel.human),
            ),
            FlowAgent(
                id="leader", kind=AgentKind.claude, repo="/tmp/main",
                is_leader=True, merge_strategy=MergeStrategy.manual,
            ),
        ],
        tasks=[
            FlowTask(
                id="t1", owner_agent_id="alice", subject="upstream",
                description="done", requires_human_checkpoint=True,
            ),
            FlowTask(
                id="t2", owner_agent_id="bob", subject="downstream",
                description="blocked by checkpoint", depends_on=["t1"],
            ),
            FlowTask(
                id="t_local", owner_agent_id="charlie", subject="parallel local",
                description="must NOT dispatch during checkpoint",
            ),
            FlowTask(
                id="t_ext", owner_agent_id="ext-node", subject="parallel external",
                description="MAY dispatch during checkpoint",
            ),
            FlowTask(
                id="ts", owner_agent_id="leader", subject="summary",
                description="wrap", depends_on=["t2", "t_local", "t_ext"],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.running
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(
            task_id="t1", owner_agent_id="alice", status="completed",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t2", owner_agent_id="bob", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t_local", owner_agent_id="charlie", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="t_ext", owner_agent_id="ext-node", status="pending",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
        TaskSnapshot(
            task_id="ts", owner_agent_id="leader", status="blocked",
            locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
        ),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )
    # Mirror completed t1 into local books so partition sees a completed
    # checkpoint upstream (snapshot apply path).
    await rc.tick()
    assert run.status == RunStatus.awaiting_user_checkpoint
    assert rc.checkpoint_snapshot() is not None
    # Downstream gated by checkpoint — never dispatched.
    assert sessions.get("bob") is None or sessions["bob"].dispatched == []
    # Parallel local agent must stay paused while reviewing.
    assert sessions.get("charlie") is None or sessions["charlie"].dispatched == []
    # Parallel external may dispatch (no worktree race).
    assert "ext-node" in sessions
    assert sessions["ext-node"].dispatched
    assert sessions["ext-node"].dispatched[-1][0] == "t_ext"


@pytest.mark.asyncio
async def test_tick_flips_awaiting_external_and_back(fake_lookup) -> None:
    """While every in-flight task is external-owned the run shows
    ``awaiting_external``; as soon as a local task is in flight it returns to
    ``running``. Terminal/checkpoint statuses are never touched here."""
    from app.models import ExternalChannel, ExternalNodeConfig

    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="ext-node",
                kind=AgentKind.external,
                external=ExternalNodeConfig(channel=ExternalChannel.human),
            ),
            FlowAgent(
                id="bob", kind=AgentKind.claude, repo="/tmp/main",
                merge_strategy=MergeStrategy.manual,
            ),
            FlowAgent(
                id="leader", kind=AgentKind.claude, repo="/tmp/main",
                is_leader=True, merge_strategy=MergeStrategy.manual,
            ),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="ext-node", subject="external",
                     description="external work"),
            FlowTask(id="t2", owner_agent_id="bob", subject="local",
                     description="local work", depends_on=["t1"]),
            FlowTask(id="ts", owner_agent_id="leader", subject="summary",
                     description="wrap", depends_on=["t2"],
                     is_leader_summary=True),
        ],
    )
    run = _persist_flow_and_run(spec)
    run.status = RunStatus.running
    sessions: dict[str, _RecordingSession] = {}

    def factory(agent: FlowAgent) -> WorkerSession:
        s = _RecordingSession(agent=agent, team_name=run.team_name, run_id=run.id)
        sessions[agent.id] = s
        return s

    snapshots: list[TaskSnapshot] = [
        TaskSnapshot(task_id="t1", owner_agent_id="ext-node", status="pending",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="t2", owner_agent_id="bob", status="blocked",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
        TaskSnapshot(task_id="ts", owner_agent_id="leader", status="blocked",
                     locked_by_agent=None, metadata={}, dispatched_at_epoch=None),
    ]

    async def snap_provider() -> list[TaskSnapshot]:
        return list(snapshots)

    async def inbox_provider() -> list[dict[str, str]]:
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=factory,
        snapshot_provider=snap_provider,
        leader_inbox_provider=inbox_provider,
    )

    # Tick 1: only the external task is dispatched → awaiting_external.
    await rc.tick()
    assert sessions["ext-node"].dispatched
    assert run.status == RunStatus.awaiting_external

    # External result arrives (t1 completed) → local t2 dispatches → running.
    snapshots[0] = TaskSnapshot(
        task_id="t1", owner_agent_id="ext-node", status="completed",
        locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
    )
    snapshots[1] = TaskSnapshot(
        task_id="t2", owner_agent_id="bob", status="pending",
        locked_by_agent=None, metadata={}, dispatched_at_epoch=None,
    )
    await rc.tick()
    assert sessions["bob"].dispatched
    assert run.status == RunStatus.running


def test_awaiting_external_is_active_driving_status() -> None:
    """awaiting_external requires a live RunController (poll loop) — it must
    be classified as active-driving so drain/orphan sweeps treat it right."""
    from app.models import ACTIVE_DRIVING_RUN_STATUSES

    assert RunStatus.awaiting_external in ACTIVE_DRIVING_RUN_STATUSES


@pytest.mark.asyncio
async def test_complaint_and_merge_targets_exclude_external(fake_lookup) -> None:
    """External nodes must never enter the complaint phase or the
    merge-requirement (auto-merge) target lists."""
    from app.models import ExternalChannel, ExternalNodeConfig

    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="ext-node",
                kind=AgentKind.external,
                external=ExternalNodeConfig(channel=ExternalChannel.webhook,
                                            endpoint_url="https://x.example/t"),
            ),
            FlowAgent(id="oc-worker", kind=AgentKind.openclaw),
            FlowAgent(
                id="leader", kind=AgentKind.claude, repo="/tmp/main",
                is_leader=True, merge_strategy=MergeStrategy.manual,
            ),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="ext-node", subject="ext",
                     description="x"),
            FlowTask(id="t2", owner_agent_id="oc-worker", subject="oc",
                     description="y", depends_on=["t1"]),
            FlowTask(id="ts", owner_agent_id="leader", subject="summary",
                     description="wrap", depends_on=["t2"],
                     is_leader_summary=True),
        ],
    )
    run = _persist_flow_and_run(spec)
    rc = RunController(
        run=run, spec=spec, flow_description="demo",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=_empty_snapshots,
        leader_inbox_provider=None,
    )
    complaint_ids = [a.id for a in rc._complaint_target_agents()]
    assert "ext-node" not in complaint_ids
    assert complaint_ids == ["oc-worker"]
    merge_ids = [a.id for a in rc._merge_requirement_agents()]
    assert "ext-node" not in merge_ids
    assert merge_ids == ["oc-worker"]
