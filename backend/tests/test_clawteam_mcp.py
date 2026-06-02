"""Tests for :mod:`app.integrations.clawteam_mcp`.

Real MCP integration test: spawns the actual ``clawteam-mcp`` stdio server
in a tmp data dir and exercises the workflow used in the scheduler hot path
(team_create → team_member_add → task_create → task_list → task_update).

Skipped automatically if ``clawteam-mcp`` isn't on PATH.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile

import pytest

from app.integrations.clawteam_mcp import ClawTeamMcpClient


requires_mcp = pytest.mark.skipif(
    not shutil.which("clawteam-mcp"),
    reason="clawteam-mcp not installed (need clawteam>=0.3)",
)


@pytest.fixture
def isolated_clawteam_dir(monkeypatch: pytest.MonkeyPatch):
    tmp = tempfile.mkdtemp(prefix="csflow_mcp_test_")
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", tmp)
    monkeypatch.setenv("CLAWTEAM_USER", "csflow-test")
    yield tmp
    subprocess.run(["rm", "-rf", tmp])


@pytest.mark.asyncio
async def test_get_mcp_client_is_scoped_by_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations import clawteam_mcp as mod

    class _FakeClient:
        def __init__(self, user: str) -> None:
            self.user = user
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    created: list[str] = []

    async def _fake_start(cls, *, acting_user=None, config=None):
        user = str(acting_user)
        created.append(user)
        return _FakeClient(user)

    await mod.close_mcp_client()
    monkeypatch.setattr(mod.ClawTeamMcpClient, "start", classmethod(_fake_start))

    c1 = await mod.get_mcp_client(user="alice")
    c2 = await mod.get_mcp_client(user="bob")
    c1_again = await mod.get_mcp_client(user="alice")

    assert c1 is c1_again
    assert c1 is not c2
    assert created == ["alice", "bob"]

    await mod.close_mcp_client()
    assert c1.closed is True
    assert c2.closed is True


@requires_mcp
def test_full_compile_loop(isolated_clawteam_dir: str) -> None:
    """Complete scheduler-compile flow against a live MCP server."""

    async def go() -> None:
        client = await ClawTeamMcpClient.start()
        try:
            # 1. Create team
            team = "csflow-mcp-roundtrip"
            t = await client.team_create(
                team_name=team, leader_name="leader",
                leader_id="leader-id-xyz", description="mcp test",
                user="csflow-test",
            )
            assert t["name"] == team
            assert t["leadAgentId"] == "leader-id-xyz"

            # 2. Add a worker member
            m = await client.team_member_add(
                team_name=team, member_name="alice",
                agent_id="alice-id-abc", agent_type="general-purpose",
                user="csflow-test",
            )
            assert m["name"] == "alice"

            # 3. Create two tasks (one with blocked_by)
            t1 = await client.task_create(
                team_name=team, subject="Task 1", owner="alice",
            )
            t2 = await client.task_create(
                team_name=team, subject="Task 2", owner="leader",
                blocked_by=[t1["id"]],
            )
            assert t1["status"] == "pending"
            # t2 is blocked because it depends on t1
            assert t2["status"] == "blocked"

            # 4. List tasks
            tasks = await client.task_list(team_name=team)
            assert len(tasks) == 2

            # 5. Update task status (mark t1 in_progress)
            t1u = await client.task_update(
                team_name=team, task_id=t1["id"], status="in_progress",
                caller="alice",
            )
            assert t1u["status"] == "in_progress"

            # 6. Filter by status
            in_prog = await client.task_list(team_name=team, status="in_progress")
            assert len(in_prog) == 1
            assert in_prog[0]["id"] == t1["id"]

            # 7. Complete t1 → t2 should auto-unblock
            t1u = await client.task_update(
                team_name=team, task_id=t1["id"], status="completed",
                caller="alice",
            )
            assert t1u["status"] == "completed"

            t2_again = await client.task_get(team_name=team, task_id=t2["id"])
            assert t2_again is not None
            assert t2_again["status"] == "pending"  # auto-unblocked

            # 8. task_get on a known id returns the row.
            t1_get = await client.task_get(team, t1["id"])
            assert t1_get is not None
            assert t1_get["status"] == "completed"

            # 9. Mailbox roundtrip is intentionally exercised in Phase 5
            # integration tests when the scheduler / inbox routing details
            # are wired up; the MCP wrapper signatures already match the
            # server-side ones (verified via inspect.signature).

        finally:
            await client.close()

    asyncio.run(go())


@requires_mcp
def test_mailbox_signature_matches_server(isolated_clawteam_dir: str) -> None:
    """Verify our mailbox_* wrappers don't trigger a Pydantic validation error
    on the server (i.e. our argument names align with the tool signatures).
    Doesn't assert message content — just that the call succeeds end-to-end."""

    async def go() -> None:
        client = await ClawTeamMcpClient.start()
        try:
            await client.team_create(
                team_name="csflow-mb", leader_name="lead",
                leader_id="lead-id", user="csflow-test",
            )
            # mailbox_send: argument names match (from_agent, to, content)
            await client.mailbox_send(
                "csflow-mb", from_agent="lead", to="lead", content="hi",
            )
            # mailbox_peek + mailbox_receive: agent_name (not "agent") name match
            await client.mailbox_peek("csflow-mb", "lead")
            await client.mailbox_receive("csflow-mb", "lead", limit=5)
        finally:
            await client.close()

    asyncio.run(go())


@requires_mcp
def test_team_get_nonexistent_returns_none(isolated_clawteam_dir: str) -> None:
    """team_get should swallow MCP errors and return None for missing teams."""

    async def go() -> None:
        client = await ClawTeamMcpClient.start()
        try:
            result = await client.team_get("nonexistent-team-xyz")
            assert result is None
        finally:
            await client.close()

    asyncio.run(go())
