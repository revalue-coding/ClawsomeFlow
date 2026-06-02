"""Tests for ``RunController._compose_dispatch_context`` upstream wiring.

These verify that:

1. A worker task with first-level ``depends_on`` populates
   ``DispatchContext.upstream_outputs`` exactly once per direct parent.
2. We never walk the DAG transitively (only 1-hop parents).
3. Each ``UpstreamOutput`` carries (a) the upstream task subject,
   (b) the upstream owner agent, (c) the upstream session's worktree
   info if the session exists, (d) the strict-match upstream completion
   message for that depended task id (agent + task_id must both match).
4. Leader summary tasks DO NOT get the upstream block (their special
   ``## 各 Worker 给你的汇报`` block already covers it).
"""

from __future__ import annotations

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
from app.scheduler.controller import RunController
from app.scheduler.naming import team_name_for_run
from app.scheduler.sessions.base import WorkerSession
from app.storage import get_storage
from app.worktree.lookup import WorktreeInfo


class _RecordingSession(WorkerSession):
    """Same stub as test_run_controller.py — just enough to drive ``tick``."""

    def __init__(self, *, agent: FlowAgent, team_name: str, run_id: str) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        self.dispatched: list[tuple[str, str]] = []

    async def _do_spawn(self) -> None: pass
    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        self.dispatched.append((task_id, message))
    async def _do_resume(self) -> None: pass
    async def _do_shutdown(self) -> None: pass


def _agent(id: str, *, leader: bool = False) -> FlowAgent:
    return FlowAgent(
        id=id, kind=AgentKind.claude, repo="/tmp/main",
        is_leader=leader, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )


def _make_three_tier_spec() -> FlowSpec:
    """A → B → C linear chain + leader summary depends on C.

    Used to verify that B's dispatch sees ONLY A (not transitively
    through anything earlier), and C's dispatch sees ONLY B (not A).
    """
    return FlowSpec(
        agents=[
            _agent("a"), _agent("b"), _agent("c"),
            _agent("leader", leader=True),
        ],
        tasks=[
            FlowTask(id="t-a", owner_agent_id="a", subject="A subject", description="A"),
            FlowTask(id="t-b", owner_agent_id="b", subject="B subject", description="B",
                     depends_on=["t-a"]),
            FlowTask(id="t-c", owner_agent_id="c", subject="C subject", description="C",
                     depends_on=["t-b"]),
            FlowTask(id="ts", owner_agent_id="leader", subject="Sum", description="S",
                     depends_on=["t-c"], is_leader_summary=True),
        ],
    )


def _persist_run(spec: FlowSpec) -> FlowRun:
    storage = get_storage()
    flow = Flow(name="t", description="d", owner_user="alice").with_spec(spec)
    saved = storage.flow_create(flow)
    run = FlowRun(id="run-87654321", flow_id=saved.id, flow_version=1,
                  team_name=team_name_for_run("run-87654321"),
                  status=RunStatus.pending, inputs={}, user="alice")
    return storage.run_create(run)


@pytest.fixture
def fake_lookup():
    class _L:
        async def list_team(self, team, *, repo=None, force=False): return []

        async def get(self, team, agent_name, *, repo=None, force=False):
            return WorktreeInfo(
                agent_name=agent_name,
                branch_name=f"clawteam/{team}/{agent_name}",
                worktree_path=f"/tmp/wt/{agent_name}",
                repo_root=repo or "/tmp/main",
                base_branch="main",
            )
    return _L()


@pytest.mark.asyncio
async def test_no_upstream_when_task_has_no_deps(fake_lookup) -> None:
    spec = _make_three_tier_spec()
    run = _persist_run(spec)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=lambda: _empty(),
    )
    # Bring up agent A's session so worktree info is available.
    sess_a = await rc._ensure_session_idle(rc._agents["a"])
    ctx = await rc._compose_dispatch_context(rc._agents["a"], rc._tasks["t-a"].task)
    assert ctx.upstream_outputs == []


@pytest.mark.asyncio
async def test_first_level_only_no_transitive_walk(fake_lookup) -> None:
    """B depends on A; C depends on B. C must see ONLY B, never A."""
    spec = _make_three_tier_spec()
    run = _persist_run(spec)

    # Inbox provider returns the production dict shape (matches what the
    # ClawTeam MCP `mailbox_receive` returns and what
    # `_fetch_leader_inbox_structured` parses).
    inbox: list[dict] = [
        {"from_agent": "a", "task_id": "t-a", "content": "A done."},
        {"from_agent": "b", "task_id": "t-b", "content": "B done."},
    ]

    async def inbox_provider():
        return list(inbox)

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=inbox_provider,
    )
    # Bring up B's session (so it has worktree info for upstream of C).
    await rc._ensure_session_idle(rc._agents["b"])

    ctx = await rc._compose_dispatch_context(rc._agents["c"], rc._tasks["t-c"].task)
    assert [u.task_id for u in ctx.upstream_outputs] == ["t-b"]
    only = ctx.upstream_outputs[0]
    assert only.from_agent == "b"
    assert only.subject == "B subject"
    assert only.summary == "[task t-b] B done."
    # First-level only: A's transitive presence MUST NOT leak.
    assert all(u.task_id != "t-a" for u in ctx.upstream_outputs)


@pytest.mark.asyncio
async def test_multiple_first_level_deps_all_listed(fake_lookup) -> None:
    """Diamond DAG: D depends on A and B. Both must show up exactly once."""
    spec = FlowSpec(
        agents=[_agent("a"), _agent("b"), _agent("d"),
                _agent("leader", leader=True)],
        tasks=[
            FlowTask(id="t-a", owner_agent_id="a", subject="A", description=""),
            FlowTask(id="t-b", owner_agent_id="b", subject="B", description=""),
            FlowTask(id="t-d", owner_agent_id="d", subject="D", description="",
                     depends_on=["t-a", "t-b"]),
            FlowTask(id="ts", owner_agent_id="leader", subject="S", description="",
                     depends_on=["t-d"], is_leader_summary=True),
        ],
    )
    run = _persist_run(spec)

    async def inbox_provider():
        return [
            {"from_agent": "a", "task_id": "t-a", "content": "A summary."},
            {"from_agent": "b", "task_id": "t-b", "content": "B summary."},
        ]

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=inbox_provider,
    )
    await rc._ensure_session_idle(rc._agents["a"])
    await rc._ensure_session_idle(rc._agents["b"])

    ctx = await rc._compose_dispatch_context(rc._agents["d"], rc._tasks["t-d"].task)
    ids = sorted(u.task_id for u in ctx.upstream_outputs)
    assert ids == ["t-a", "t-b"]
    summaries = {u.task_id: u.summary for u in ctx.upstream_outputs}
    assert summaries == {
        "t-a": "[task t-a] A summary.",
        "t-b": "[task t-b] B summary.",
    }
    paths = {u.task_id: u.worktree_path for u in ctx.upstream_outputs}
    assert paths["t-a"].endswith("/wt/a")
    assert paths["t-b"].endswith("/wt/b")


@pytest.mark.asyncio
async def test_summary_missing_from_inbox_renders_none(fake_lookup) -> None:
    spec = _make_three_tier_spec()
    run = _persist_run(spec)

    async def empty_inbox():
        return []

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=empty_inbox,
    )
    await rc._ensure_session_idle(rc._agents["a"])
    ctx = await rc._compose_dispatch_context(rc._agents["b"], rc._tasks["t-b"].task)
    assert len(ctx.upstream_outputs) == 1
    assert ctx.upstream_outputs[0].summary is None


@pytest.mark.asyncio
async def test_upstream_picks_matching_task_message_when_same_agent_has_history(
    fake_lookup,
) -> None:
    """Strict-match by task_id and keep only the latest matching message."""
    spec = FlowSpec(
        agents=[_agent("a"), _agent("d"), _agent("leader", leader=True)],
        tasks=[
            FlowTask(id="t-a1", owner_agent_id="a", subject="A1", description=""),
            FlowTask(id="t-a2", owner_agent_id="a", subject="A2", description=""),
            FlowTask(
                id="t-d",
                owner_agent_id="d",
                subject="D",
                description="",
                depends_on=["t-a2"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="S",
                description="",
                depends_on=["t-d"],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_run(spec)

    async def inbox_provider():
        # Same sender, two different completed tasks in history.
        return [
            {"from_agent": "a", "content": "task t-a1 done: old summary"},
            {"from_agent": "a", "content": "task t-a2 done: intermediate summary"},
            {"from_agent": "a", "content": "task t-a2 done: expected summary"},
        ]

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=inbox_provider,
    )
    await rc._ensure_session_idle(rc._agents["a"])
    ctx = await rc._compose_dispatch_context(rc._agents["d"], rc._tasks["t-d"].task)
    assert len(ctx.upstream_outputs) == 1
    assert ctx.upstream_outputs[0].task_id == "t-a2"
    assert ctx.upstream_outputs[0].summary == "[task t-a2] task t-a2 done: expected summary"


@pytest.mark.asyncio
async def test_upstream_accepts_last_task_field_for_task_mapping(fake_lookup) -> None:
    spec = FlowSpec(
        agents=[_agent("a"), _agent("d"), _agent("leader", leader=True)],
        tasks=[
            FlowTask(id="t-a2", owner_agent_id="a", subject="A2", description=""),
            FlowTask(
                id="t-d",
                owner_agent_id="d",
                subject="D",
                description="",
                depends_on=["t-a2"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="S",
                description="",
                depends_on=["t-d"],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_run(spec)

    async def inbox_provider():
        return [
            {"from": "a", "content": "summary from lastTask", "lastTask": "t-a2"},
        ]

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=inbox_provider,
    )
    await rc._ensure_session_idle(rc._agents["a"])
    ctx = await rc._compose_dispatch_context(rc._agents["d"], rc._tasks["t-d"].task)
    assert len(ctx.upstream_outputs) == 1
    assert ctx.upstream_outputs[0].summary == "[task t-a2] summary from lastTask"


@pytest.mark.asyncio
async def test_upstream_requires_strict_agent_match(fake_lookup) -> None:
    """Even if task id matches, sender must be the upstream owner agent."""
    spec = FlowSpec(
        agents=[_agent("a"), _agent("d"), _agent("leader", leader=True)],
        tasks=[
            FlowTask(id="t-a", owner_agent_id="a", subject="A", description=""),
            FlowTask(
                id="t-d",
                owner_agent_id="d",
                subject="D",
                description="",
                depends_on=["t-a"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="S",
                description="",
                depends_on=["t-d"],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_run(spec)

    async def inbox_provider():
        return [
            {
                "from": "agent",
                "content": "task t-a done: summary from generic sender",
            },
        ]

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=inbox_provider,
    )
    await rc._ensure_session_idle(rc._agents["a"])
    ctx = await rc._compose_dispatch_context(rc._agents["d"], rc._tasks["t-d"].task)
    assert len(ctx.upstream_outputs) == 1
    assert ctx.upstream_outputs[0].summary is None


@pytest.mark.asyncio
async def test_upstream_same_owner_multiple_deps_keep_per_task_strict_match(fake_lookup) -> None:
    spec = FlowSpec(
        agents=[_agent("a"), _agent("d"), _agent("leader", leader=True)],
        tasks=[
            FlowTask(id="t-a1", owner_agent_id="a", subject="A1", description=""),
            FlowTask(id="t-a2", owner_agent_id="a", subject="A2", description=""),
            FlowTask(
                id="t-d",
                owner_agent_id="d",
                subject="D",
                description="",
                depends_on=["t-a1", "t-a2"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="S",
                description="",
                depends_on=["t-d"],
                is_leader_summary=True,
            ),
        ],
    )
    run = _persist_run(spec)

    async def inbox_provider():
        return [
            {"from_agent": "a", "task_id": "t-a1", "content": "A1 summary"},
            {"from_agent": "a", "task_id": "t-a3", "content": "A3 irrelevant"},
            {"from_agent": "a", "task_id": "t-a2", "content": "A2 summary"},
        ]

    rc = RunController(
        run=run,
        spec=spec,
        flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=inbox_provider,
    )
    await rc._ensure_session_idle(rc._agents["a"])
    ctx = await rc._compose_dispatch_context(rc._agents["d"], rc._tasks["t-d"].task)
    assert [u.task_id for u in ctx.upstream_outputs] == ["t-a1", "t-a2"]
    summaries = {u.task_id: u.summary for u in ctx.upstream_outputs}
    assert summaries == {
        "t-a1": "[task t-a1] A1 summary",
        "t-a2": "[task t-a2] A2 summary",
    }


@pytest.mark.asyncio
async def test_leader_summary_task_does_not_get_upstream_block(fake_lookup) -> None:
    """Leader summary task gets ``worker_reports`` instead — never
    ``upstream_outputs`` (would duplicate the existing summary block)."""
    spec = _make_three_tier_spec()
    run = _persist_run(spec)

    async def inbox_provider():
        return [{"from_agent": "c", "task_id": "t-c", "content": "C done."}]

    rc = RunController(
        run=run, spec=spec, flow_description="d",
        worktree_lookup=fake_lookup,
        session_factory=lambda a: _RecordingSession(
            agent=a, team_name=run.team_name, run_id=run.id,
        ),
        snapshot_provider=lambda: _empty(),
        leader_inbox_provider=inbox_provider,
    )
    # Bring leader and worker C's sessions up.
    await rc._ensure_session_idle(rc._agents["leader"])
    await rc._ensure_session_idle(rc._agents["c"])

    ctx = await rc._compose_dispatch_context(rc._agents["leader"], rc._tasks["ts"].task)
    assert ctx.upstream_outputs == []
    assert len(ctx.worker_reports) == 1
    assert ctx.worker_reports[0].task_id == "t-c"


# helpers


async def _empty():
    return []
