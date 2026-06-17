"""Tests for OpenClaw chat progress turns (services/openclaw_chat) + the
``/chat/status`` reconnect endpoint and kill-on-reset wiring.

The trajectory follower is disabled in tests (CSFLOW_DISABLE_OPENCLAW_CHAT_FOLLOWER,
set by the autouse fixture) so nothing forks a real ``openclaw sessions`` process.
The heavy ``_ensure_chat_target_access`` (agent reindex) is monkeypatched away so
these stay fast and focused on the progress layer.
"""

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


# ── service: snapshot / steps ─────────────────────────────────────────


def test_snapshot_shape_and_steps() -> None:
    turn = chat.ChatTurn(agent_id="a1", session_key="sk", started_at=time.monotonic())
    turn.append_step("info", name="reading files")
    turn.append_step("tool", name="bash")
    snap = turn.snapshot()
    assert snap["status"] == "running"
    assert [s["name"] for s in snap["steps"]] == ["reading files", "bash"]
    assert snap["steps"][0]["seq"] == 1 and snap["steps"][1]["seq"] == 2
    assert "elapsedSec" in snap["progress"]
    assert snap["final"] == "" and snap["error"] == ""


def test_finish_progress_carries_final() -> None:
    chat.start_progress("a1", "sk-fin")  # follower disabled by env
    chat.finish_progress("sk-fin", status="done", final="all done")
    snap = chat.get_turn("sk-fin").snapshot()
    assert snap["status"] == "done"
    assert snap["final"] == "all done"


# ── service: kill / supersede ─────────────────────────────────────────


def test_kill_turn_kills_agent_and_follower(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(
        chat._subproc_registry, "kill_group",
        lambda proc, **kw: (killed.append(proc.pid) or True),
    )
    monkeypatch.setattr(chat._subproc_registry, "unregister", lambda proc: None)

    turn = chat.start_progress("a1", "sk1")
    turn.agent_proc = _FakeProc(pid=111)
    turn.follower = _FakeProc(pid=222)

    assert chat.kill_turn("sk1") is True
    assert set(killed) == {111, 222}
    assert chat.get_turn("sk1") is None
    assert turn.status == "error"
    # Idempotent.
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
    second = chat.start_progress("a1", "sk2")  # supersede

    assert killed == [999]  # the prior turn's in-flight agent was killed
    assert chat.get_turn("sk2") is second
    assert first.status == "error"


# ── service: session-key resolution parsing ───────────────────────────


def test_resolve_session_key_matches_user_chat_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "sessions": [
            {"key": "agent:web:main", "sessionId": "x"},
            {"key": "agent:web:user-chat-alice-web", "sessionId": "y"},
        ]
    }

    class _R:
        returncode = 0
        stdout = __import__("json").dumps(payload)

    monkeypatch.setattr(chat.subprocess, "run", lambda *a, **k: _R())
    got = chat._resolve_session_key("/usr/bin/openclaw", "web", "user-chat-alice-web")
    assert got == "agent:web:user-chat-alice-web"


def test_resolve_session_key_none_on_cli_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(chat.subprocess, "run", lambda *a, **k: _R())
    assert chat._resolve_session_key("/usr/bin/openclaw", "web", "user-chat-alice-web") is None


# ── API: /chat/status reconnect + reset kills the turn ────────────────


def test_chat_status_idle_then_running(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CSFLOW_USER", "alice")
    monkeypatch.setattr(router_mod, "_ensure_chat_target_access", lambda *a, **k: None)

    r = client.get("/api/openclaw/agents/a1/chat/status")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "idle"

    sk = openclaw_user_chat_session_id("alice", "a1")
    turn = chat.start_progress("a1", sk)
    turn.append_step("info", name="thinking")

    body = client.get("/api/openclaw/agents/a1/chat/status").json()
    assert body["status"] == "running"
    assert body["steps"][0]["name"] == "thinking"


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
