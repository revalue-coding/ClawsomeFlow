"""Tests for OpenClaw chat progress turns (services/openclaw_chat) + status API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.api import openclaw_agents as router_mod
from app.main import create_app
from app.scheduler.naming import openclaw_user_chat_session_id
from app.services import openclaw_chat as chat


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_registry():
    chat._TURNS.clear()
    yield
    chat._TURNS.clear()


class _FakeProc:
    def __init__(self, pid: int = 5151) -> None:
        self.pid = pid


def test_snapshot_shape() -> None:
    turn = chat.ChatTurn(agent_id="a1", session_key="sk", started_at=time.monotonic())
    snap = turn.snapshot()
    assert snap["status"] == "running"
    assert snap["steps"] == []
    assert "elapsedSec" in snap["progress"]
    assert snap["final"] == "" and snap["error"] == ""


def test_finish_progress_carries_final() -> None:
    chat.start_progress("a1", "sk-fin")
    chat.finish_progress("sk-fin", status="done", final="all done")
    snap = chat.get_turn("sk-fin").snapshot()
    assert snap["status"] == "done"
    assert snap["final"] == "all done"


def test_kill_turn_kills_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(
        chat._subproc_registry, "kill_group",
        lambda proc, **kw: (killed.append(proc.pid) or True),
    )
    monkeypatch.setattr(chat._subproc_registry, "unregister", lambda proc: None)

    turn = chat.start_progress("a1", "sk1")
    turn.agent_proc = _FakeProc(pid=111)

    assert chat.kill_turn("sk1") is True
    assert killed == [111]
    assert chat.get_turn("sk1") is None
    assert turn.status == "error"
    assert chat.kill_turn("sk1") is False


def test_start_progress_supersedes_prior_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(
        chat._subproc_registry, "kill_group",
        lambda proc, **kw: (killed.append(proc.pid) or True),
    )
    monkeypatch.setattr(chat._subproc_registry, "unregister", lambda proc: None)

    first = chat.start_progress("a1", "sk2")
    first.agent_proc = _FakeProc(pid=999)
    second = chat.start_progress("a1", "sk2")

    assert killed == [999]
    assert chat.get_turn("sk2") is second
    assert first.status == "error"


def test_chat_status_idle_then_running(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CSFLOW_USER", "alice")
    monkeypatch.setattr(router_mod, "_ensure_chat_target_access", lambda *a, **k: None)

    r = client.get("/api/openclaw/agents/a1/chat/status")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "idle"

    sk = openclaw_user_chat_session_id("alice", "a1")
    chat.start_progress("a1", sk)

    body = client.get("/api/openclaw/agents/a1/chat/status").json()
    assert body["status"] == "running"
    assert body["steps"] == []


def test_reset_kills_turn(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CSFLOW_USER", "alice")
    monkeypatch.setattr(router_mod, "_ensure_chat_target_access", lambda *a, **k: None)
    killed: list[str] = []
    monkeypatch.setattr(chat, "kill_turn", lambda sk: (killed.append(sk) or False))

    async def _fake_reset_completion(**kw):
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_reset_completion)

    r = client.post("/api/openclaw/agents/a1/reset")
    assert r.status_code == 204, r.text
    assert killed == [openclaw_user_chat_session_id("alice", "a1")]


def test_history_records_server_timestamp() -> None:
    """Every appended message carries a server ``ts`` (epoch ms) so the chat UI
    can render an authoritative per-message time — including assistant replies,
    whose streamed text differs from the persisted text and so cannot rely on
    the client's content-matched cache backfill."""
    import asyncio

    from app.services import openclaw_chat_history as history

    async def _run() -> list[dict]:
        sk = "sk-ts-test"
        await history.clear_messages(sk)
        await history.append_message(sk, role="user", content="hi")
        await history.append_message(sk, role="assistant", content="hello")
        return await history.list_messages(sk)

    rows = asyncio.run(_run())
    assert len(rows) == 2
    assert all(isinstance(row.get("ts"), int) and row["ts"] > 0 for row in rows)
    # Recorded in order → non-decreasing timestamps.
    assert rows[0]["ts"] <= rows[1]["ts"]


def test_drop_trailing_unanswered_user() -> None:
    """A failed turn leaves a user row with no assistant; the next send must
    drop that orphan before appending the new user message."""
    import asyncio

    from app.services import openclaw_chat_history as history

    async def _run() -> tuple[bool, bool, list[dict], list[dict]]:
        sk = "sk-drop-orphan"
        await history.clear_messages(sk)
        await history.append_message(sk, role="user", content="ok-q")
        await history.append_message(sk, role="assistant", content="ok-a")
        await history.append_message(sk, role="user", content="failed-q")
        dropped = await history.drop_trailing_unanswered_user(sk)
        after_drop = await history.list_messages(sk)
        noop = await history.drop_trailing_unanswered_user(sk)
        after_noop = await history.list_messages(sk)
        return dropped, noop, after_drop, after_noop

    dropped, noop, after_drop, after_noop = asyncio.run(_run())
    assert dropped is True
    assert noop is False
    assert [(r["role"], r["content"]) for r in after_drop] == [
        ("user", "ok-q"),
        ("assistant", "ok-a"),
    ]
    assert [(r["role"], r["content"]) for r in after_noop] == [
        ("user", "ok-q"),
        ("assistant", "ok-a"),
    ]


def test_drop_trailing_unanswered_user_clears_empty_session() -> None:
    import asyncio

    from app.services import openclaw_chat_history as history

    async def _run() -> list[dict]:
        sk = "sk-drop-only-user"
        await history.clear_messages(sk)
        await history.append_message(sk, role="user", content="alone")
        assert await history.drop_trailing_unanswered_user(sk) is True
        return await history.list_messages(sk)

    assert asyncio.run(_run()) == []
