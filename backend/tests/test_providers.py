"""Tests for app.scheduler.providers — MCP-backed snapshot / inbox."""

from __future__ import annotations

from typing import Any

import pytest

from app.scheduler.compiler import (
    CSFLOW_TASK_ID_KEY,
    CompileResult,
)
from app.scheduler.providers import (
    DispatchClock,
    McpLeaderInboxProvider,
    McpSnapshotProvider,
)


class _FakeMcp:
    def __init__(self, *, tasks=None, inbox=None, event_log=None, fail=False) -> None:
        self._tasks = tasks or []
        self._inbox = inbox or []
        self._event_log = event_log or []
        self._fail = fail
        self.list_calls = 0
        self.recv_calls = 0
        self.peek_calls = 0
        self.event_log_calls = 0
        self.event_log_limit: int | None = None

    async def task_list(self, team_name: str):
        self.list_calls += 1
        if self._fail:
            raise RuntimeError("mcp down")
        return list(self._tasks)

    async def mailbox_receive(self, team_name: str, agent_name: str, *, limit=10):
        self.recv_calls += 1
        if self._fail:
            raise RuntimeError("mcp down")
        return list(self._inbox)

    async def mailbox_peek(self, team_name: str, agent_name: str):
        self.peek_calls += 1
        return list(self._inbox)

    async def mailbox_event_log(self, team_name: str, agent_name: str, *, limit=200):
        self.event_log_calls += 1
        self.event_log_limit = limit
        return list(self._event_log)


def _cr() -> CompileResult:
    return CompileResult(
        team_name="t", leader_agent_id="leader",
        flow_to_clawteam={"t1": "ct1", "t2": "ct2"},
        clawteam_to_flow={"ct1": "t1", "ct2": "t2"},
        member_count=3,
    )


# ── snapshot provider --------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_uses_metadata_csflow_task_id() -> None:
    mcp = _FakeMcp(tasks=[
        {"id": "ct1", "status": "in_progress", "owner": "alice",
         "lockedBy": "alice", "metadata": {CSFLOW_TASK_ID_KEY: "t1"}},
    ])
    clock = DispatchClock()
    clock.mark("t1", 100.0)
    p = McpSnapshotProvider(team_name="t", compile_result=_cr(),
                            mcp=mcp, dispatch_clock=clock)
    snaps = await p()
    assert len(snaps) == 1
    s = snaps[0]
    assert s.task_id == "t1"
    assert s.owner_agent_id == "alice"
    assert s.locked_by_agent == "alice"
    assert s.dispatched_at_epoch == 100.0


@pytest.mark.asyncio
async def test_snapshot_falls_back_to_id_mapping() -> None:
    """When metadata is missing, look up via clawteam_to_flow."""
    mcp = _FakeMcp(tasks=[
        {"id": "ct2", "status": "completed", "owner": "bob"},
    ])
    p = McpSnapshotProvider(team_name="t", compile_result=_cr(),
                            mcp=mcp, dispatch_clock=DispatchClock())
    snaps = await p()
    assert len(snaps) == 1
    assert snaps[0].task_id == "t2"


@pytest.mark.asyncio
async def test_snapshot_skips_unknown_tasks() -> None:
    """ClawTeam tasks not created by ClawsomeFlow are silently ignored."""
    mcp = _FakeMcp(tasks=[
        {"id": "external-task", "status": "pending", "owner": "x"},
    ])
    p = McpSnapshotProvider(team_name="t", compile_result=_cr(),
                            mcp=mcp, dispatch_clock=DispatchClock())
    snaps = await p()
    assert snaps == []


@pytest.mark.asyncio
async def test_snapshot_handles_camelcase_locked_by() -> None:
    """ClawTeam Pydantic dump uses camelCase aliases (lockedBy)."""
    mcp = _FakeMcp(tasks=[
        {"id": "ct1", "status": "in_progress", "owner": "x", "lockedBy": "x",
         "metadata": {CSFLOW_TASK_ID_KEY: "t1"}},
    ])
    p = McpSnapshotProvider(team_name="t", compile_result=_cr(),
                            mcp=mcp, dispatch_clock=DispatchClock())
    snaps = await p()
    assert snaps[0].locked_by_agent == "x"


@pytest.mark.asyncio
async def test_snapshot_returns_empty_on_mcp_failure() -> None:
    mcp = _FakeMcp(fail=True)
    p = McpSnapshotProvider(team_name="t", compile_result=_cr(),
                            mcp=mcp, dispatch_clock=DispatchClock())
    snaps = await p()
    assert snaps == []


# ── inbox provider -----------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_default_is_non_destructive_peek() -> None:
    mcp = _FakeMcp(inbox=[
        {"from_agent": "alice", "content": "hi", "task_id": "t1"},
        {"from_agent": "bob", "content": "yo"},
    ])
    p = McpLeaderInboxProvider(team_name="t", leader_agent_id="leader", mcp=mcp)
    rows = await p()
    assert len(rows) == 2
    assert rows[0]["from_agent"] == "alice"
    assert mcp.recv_calls == 0
    assert mcp.peek_calls == 1


@pytest.mark.asyncio
async def test_inbox_peek_uses_peek_endpoint() -> None:
    mcp = _FakeMcp(inbox=[{"from_agent": "x", "content": "y"}])
    p = McpLeaderInboxProvider(
        team_name="t", leader_agent_id="leader", mcp=mcp, peek=True,
    )
    rows = await p()
    assert mcp.peek_calls == 1
    assert mcp.recv_calls == 0
    assert rows[0]["from_agent"] == "x"


@pytest.mark.asyncio
async def test_inbox_returns_empty_on_failure() -> None:
    mcp = _FakeMcp(fail=True)
    p = McpLeaderInboxProvider(team_name="t", leader_agent_id="leader", mcp=mcp)
    rows = await p()
    assert rows == []


@pytest.mark.asyncio
async def test_inbox_peek_backfills_from_event_log() -> None:
    """Reports the FIFO peek window dropped are recovered from the event log.

    Regression for run 561a9fb188ab: shutdown_request lifecycle noise (one per
    pause/finalize teardown) starved a recent ``task <id> done:`` report out of
    peek's oldest-10 window, so the resumed checkpoint showed an empty summary.
    """
    # Peek only surfaces the (older) shutdown noise.
    peek = [
        {"from_agent": "csflow-scheduler", "to": "leader",
         "content": "Shutdown requested. Reason: run_finalize",
         "requestId": "s1"},
    ]
    # The recent report lives only in the newest-first event log.
    log = [
        {"from": "remote22", "to": "leader",
         "content": "task assemble_itinerary done: 已生成最终版",
         "requestId": "r1"},
        {"from": "csflow-scheduler", "to": "leader",
         "content": "Shutdown requested. Reason: run_finalize",
         "requestId": "s1"},  # duplicate of the peek row → deduped
    ]
    mcp = _FakeMcp(inbox=peek, event_log=log)
    p = McpLeaderInboxProvider(team_name="t", leader_agent_id="leader", mcp=mcp)
    rows = await p()
    assert mcp.peek_calls == 1
    assert mcp.event_log_calls == 1
    # Peek row kept first and verbatim; the report is appended; s1 deduped.
    assert len(rows) == 2
    assert rows[0]["requestId"] == "s1"
    assert any(
        "task assemble_itinerary done" in (r.get("content") or "") for r in rows
    )
    # Exactly one shutdown row survives (dedup by requestId).
    assert sum(
        1 for r in rows if "Shutdown requested" in (r.get("content") or "")
    ) == 1


@pytest.mark.asyncio
async def test_inbox_event_log_failure_falls_back_to_peek() -> None:
    """A transient event-log failure never drops the peek rows."""

    class _EventLogBoom(_FakeMcp):
        async def mailbox_event_log(self, team_name, agent_name, *, limit=200):
            raise RuntimeError("log unavailable")

    mcp = _EventLogBoom(inbox=[{"from_agent": "x", "content": "y", "requestId": "p1"}])
    p = McpLeaderInboxProvider(team_name="t", leader_agent_id="leader", mcp=mcp)
    rows = await p()
    assert len(rows) == 1
    assert rows[0]["from_agent"] == "x"


@pytest.mark.asyncio
async def test_inbox_receive_mode_skips_event_log() -> None:
    """Non-peek (consuming) mode is unchanged: no event-log backfill."""
    mcp = _FakeMcp(inbox=[{"from_agent": "x", "content": "y"}], event_log=[
        {"from": "z", "to": "leader", "content": "task t done:", "requestId": "r"},
    ])
    p = McpLeaderInboxProvider(
        team_name="t", leader_agent_id="leader", mcp=mcp, peek=False,
    )
    rows = await p()
    assert mcp.recv_calls == 1
    assert mcp.event_log_calls == 0
    assert len(rows) == 1


# ── DispatchClock -----------------------------------------------------


def test_dispatch_clock_mark_reset() -> None:
    c = DispatchClock()
    assert c.table == {}
    c.mark("t1", 50.0)
    assert c.table == {"t1": 50.0}
    c.reset("t1")
    assert c.table == {}
    # Reset is idempotent.
    c.reset("t1")
