"""Tests for app.scheduler.compiler — Flow → ClawTeam compilation."""

from __future__ import annotations

from typing import Any

import pytest

from app.models import (
    AgentKind,
    FlowAgent,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    OnFailure,
)
from app.scheduler import compiler


# ── stubs --------------------------------------------------------------


class _StubCli:
    def __init__(self) -> None:
        self.spawn_team_calls: list[dict] = []

    async def team_spawn_team(self, **kw):
        self.spawn_team_calls.append(kw)
        return {"team": kw["team"]}


class _StubMcp:
    """Captures every call so tests can verify shape + ordering."""

    def __init__(self) -> None:
        self.member_calls: list[dict] = []
        self.task_calls: list[dict] = []
        self._next_id = 0

    async def team_member_add(self, **kw):
        self.member_calls.append(kw)
        return {"ok": True}

    async def task_create(self, **kw):
        self._next_id += 1
        # Format mimics ClawTeam: 8-char hex id.
        ct_id = f"ct{self._next_id:06d}"
        self.task_calls.append({**kw, "_assigned_id": ct_id})
        return {"id": ct_id, "status": "pending", **kw}


def _spec() -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/r1",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="bob", kind=AgentKind.claude, repo="/r2",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/r1",
                      is_leader=True, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="A1", description="",
                     depends_on=[], timeout_seconds=300),
            FlowTask(id="t2", owner_agent_id="bob",  subject="B1", description="",
                     depends_on=["t1"], timeout_seconds=300),
            FlowTask(id="ts", owner_agent_id="leader", subject="Sum",
                     description="", depends_on=["t1", "t2"],
                     is_leader_summary=True, timeout_seconds=600),
        ],
    )


# ── tests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_creates_team_and_registers_members() -> None:
    cli, mcp = _StubCli(), _StubMcp()
    res = await compiler.compile_flow_to_clawteam(
        spec=_spec(), team_name="csflow-x", user="alice",
        cli=cli, mcp=mcp,
    )
    assert res.team_name == "csflow-x"
    assert res.leader_agent_id == "leader"
    # Leader registered via team_spawn_team CLI; non-leaders via team_member_add.
    assert len(cli.spawn_team_calls) == 1
    assert cli.spawn_team_calls[0]["agent_name"] == "leader"
    assert {c["member_name"] for c in mcp.member_calls} == {"alice", "bob"}


@pytest.mark.asyncio
async def test_compile_creates_tasks_in_topological_order() -> None:
    cli, mcp = _StubCli(), _StubMcp()
    await compiler.compile_flow_to_clawteam(
        spec=_spec(), team_name="t", user="u", cli=cli, mcp=mcp,
    )
    # t1 has no deps → first; t2 depends on t1 → second; ts depends on both → last.
    order = [c["subject"] for c in mcp.task_calls]
    assert order == ["A1", "B1", "Sum"]


@pytest.mark.asyncio
async def test_compile_records_blocked_by_with_clawteam_ids() -> None:
    cli, mcp = _StubCli(), _StubMcp()
    res = await compiler.compile_flow_to_clawteam(
        spec=_spec(), team_name="t", user="u", cli=cli, mcp=mcp,
    )
    # Look at the tasks in the order they were sent.
    t1, t2, ts = mcp.task_calls
    assert t1["blocked_by"] is None
    # t2 blocked by ct000001 (the assigned id of t1).
    assert t2["blocked_by"] == ["ct000001"]
    # ts blocked by both.
    assert ts["blocked_by"] == ["ct000001", "ct000002"]
    # Mapping is correct.
    assert res.flow_to_clawteam == {"t1": "ct000001", "t2": "ct000002", "ts": "ct000003"}
    assert res.clawteam_to_flow == {"ct000001": "t1", "ct000002": "t2", "ct000003": "ts"}


@pytest.mark.asyncio
async def test_compile_stamps_csflow_task_id_metadata() -> None:
    cli, mcp = _StubCli(), _StubMcp()
    await compiler.compile_flow_to_clawteam(
        spec=_spec(), team_name="t", user="u", cli=cli, mcp=mcp,
    )
    for call in mcp.task_calls:
        assert compiler.CSFLOW_TASK_ID_KEY in call["metadata"]
        # And timeout_seconds gets propagated for failure detector use.
        assert call["metadata"]["timeout_seconds"] in {300, 600}


@pytest.mark.asyncio
async def test_extract_task_id_tolerates_nested_payload() -> None:
    assert compiler._extract_task_id({"id": "abc"}) == "abc"
    assert compiler._extract_task_id({"task": {"id": "abc"}}) == "abc"
    with pytest.raises(RuntimeError):
        compiler._extract_task_id({})
    with pytest.raises(RuntimeError):
        compiler._extract_task_id(None)


def test_toposort_stable_ordering() -> None:
    # Sibling tasks (no deps among themselves) keep authored order.
    tasks = [
        FlowTask(id=f"t{i}", owner_agent_id="x", subject=f"s{i}",
                 description="", depends_on=[]) for i in range(5)
    ]
    out = compiler._toposort_tasks(tasks)
    assert [t.id for t in out] == ["t0", "t1", "t2", "t3", "t4"]
