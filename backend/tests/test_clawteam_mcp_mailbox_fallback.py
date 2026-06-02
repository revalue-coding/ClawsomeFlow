from __future__ import annotations

import pytest

from app.integrations.clawteam_mcp import ClawTeamMcpClient, _extract_mailbox_rows


def test_extract_mailbox_rows_unwraps_result_and_messages() -> None:
    payload = [
        {"result": [
            {"from": "a", "to": "leader", "content": "A done"},
            {"from_agent": "b", "to": "leader", "body": "B done"},
        ]},
        {"messages": [
            {"from": "c", "to": "leader", "content": "C done"},
        ]},
    ]
    rows = _extract_mailbox_rows(payload)
    assert [r.get("from") or r.get("from_agent") for r in rows] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_mailbox_peek_reads_via_cli_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ClawTeamMcpClient(acting_user="alice")
    called: dict[str, object] = {}

    async def fake_cli_rows(*, team_name: str, agent_name: str, consume: bool, limit: int = 10):
        called["team_name"] = team_name
        called["agent_name"] = agent_name
        called["consume"] = consume
        called["limit"] = limit
        return [{"from": "agent", "to": "leader", "content": "task t1 done: ok"}]

    monkeypatch.setattr(client, "_mailbox_rows_via_cli", fake_cli_rows)

    rows = await client.mailbox_peek("team-x", "leader")
    assert rows == [{"from": "agent", "to": "leader", "content": "task t1 done: ok"}]
    assert called == {
        "team_name": "team-x",
        "agent_name": "leader",
        "consume": False,
        "limit": 10,
    }


@pytest.mark.asyncio
async def test_mailbox_receive_reads_via_cli_with_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ClawTeamMcpClient(acting_user="alice")
    called: dict[str, object] = {}

    async def fake_cli_rows(*, team_name: str, agent_name: str, consume: bool, limit: int = 10):
        called["team_name"] = team_name
        called["agent_name"] = agent_name
        called["consume"] = consume
        called["limit"] = limit
        return [{"from": "agent", "to": "leader", "content": "task t2 done: ok"}]

    monkeypatch.setattr(client, "_mailbox_rows_via_cli", fake_cli_rows)

    rows = await client.mailbox_receive("team-x", "leader", limit=3)
    assert rows == [{"from": "agent", "to": "leader", "content": "task t2 done: ok"}]
    assert called == {
        "team_name": "team-x",
        "agent_name": "leader",
        "consume": True,
        "limit": 3,
    }
