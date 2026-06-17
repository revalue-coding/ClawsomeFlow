"""Tests for tracked Hermes chat jobs (services/hermes_chat) + reset/status API.

These cover the behaviours that fix the "chat looks stuck / reset leaves a
runaway process / refresh loses state" issues, without spawning a real ``hermes``
process: the process group is a fake and the session-export CLI is mocked.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import HermesAgent
from app.scheduler.naming import hermes_user_chat_session_id
from app.services import hermes_agents as svc
from app.services import hermes_chat as chat_svc
from app.storage import get_storage


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_registry():
    """Isolate the module-global job registry between tests."""
    chat_svc._JOBS.clear()
    yield
    chat_svc._JOBS.clear()


class _FakePopen:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid

    def poll(self):  # still running
        return None


def _mk_job(session_key: str, agent_id: str = "math") -> chat_svc.ChatJob:
    return chat_svc.ChatJob(
        agent_id=agent_id,
        session_key=session_key,
        proc=_FakePopen(),
        started_at=time.monotonic(),
    )


# ── pure helpers ──────────────────────────────────────────────────────


def test_tool_names_and_count() -> None:
    data = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"function": {"name": "cronjob"}}]},
            {"role": "tool", "content": "ok"},
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "search_files"}}, {"name": "skill_view"}],
            },
        ],
        "tool_call_count": 3,
    }
    assert chat_svc._tool_names(data) == ["cronjob", "search_files", "skill_view"]
    assert chat_svc._count_tool_calls(data) == 3


def test_apply_progress_appends_only_new_tools() -> None:
    job = _mk_job("sk")
    d1 = {
        "messages": [{"role": "assistant", "tool_calls": [{"function": {"name": "a"}}]}],
        "tool_call_count": 1,
        "api_call_count": 2,
        "message_count": 3,
    }
    seen = chat_svc._apply_progress(job, d1, 0)
    assert seen == 1
    assert [s.get("name") for s in job.steps] == ["a"]
    assert job.progress.tool_calls == 1
    assert job.progress.api_calls == 2

    d2 = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"function": {"name": "a"}}]},
            {"role": "assistant", "tool_calls": [{"function": {"name": "b"}}]},
        ],
        "tool_call_count": 2,
        "api_call_count": 4,
        "message_count": 5,
    }
    seen = chat_svc._apply_progress(job, d2, seen)
    assert seen == 2
    # Only the newly-seen tool is appended (no duplicate "a").
    assert [s.get("name") for s in job.steps] == ["a", "b"]
    assert job.progress.api_calls == 4


def test_discover_session_id_picks_newest_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    table = (
        "Preview                         Last Active   Src    ID\n"
        "──────────────────────────────────────────────────────────\n"
        "do the thing                    just now      cli    20260617_185542_a98875\n"
        "older one                       20m ago       cli    20260617_184937_a4cde3\n"
    )
    monkeypatch.setattr(svc, "_run_hermes", lambda args, **kw: (0, table, ""))
    assert chat_svc._discover_session_id("math") == "20260617_185542_a98875"


def test_discover_session_id_handles_cli_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_run_hermes", lambda args, **kw: (1, "", "boom"))
    assert chat_svc._discover_session_id("math") is None


def test_snapshot_shape() -> None:
    job = _mk_job("sk2")
    job.steps.append({"kind": "tool", "name": "x", "seq": 1})
    snap = job.snapshot()
    assert snap["status"] == "running"
    assert snap["steps"][0]["name"] == "x"
    assert "elapsedSec" in snap["progress"]


# ── kill / supersede ──────────────────────────────────────────────────


def test_kill_chat_kills_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(
        chat_svc._subproc_registry,
        "kill_group",
        lambda proc, **kw: (killed.append(proc.pid) or True),
    )
    monkeypatch.setattr(chat_svc._subproc_registry, "unregister", lambda proc: None)

    job = _mk_job("sk1")
    chat_svc._JOBS["sk1"] = job

    assert chat_svc.kill_chat("sk1") is True
    assert killed == [job.proc.pid]
    assert chat_svc.get_job("sk1") is None
    assert job.status == "error"
    assert job.error == "cancelled"
    # Idempotent: a second kill is a no-op.
    assert chat_svc.kill_chat("sk1") is False


# ── API: reset kills, status reconnect ────────────────────────────────


def test_reset_endpoint_kills_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CSFLOW_USER", "alice")
    get_storage().hermes_create(
        HermesAgent(id="math", name="Math", profile_root="x", created_by_user="alice")
    )
    called: list[str] = []
    monkeypatch.setattr(chat_svc, "kill_chat", lambda sk: (called.append(sk) or False))

    r = client.post("/api/hermes/agents/math/reset")
    assert r.status_code == 204, r.text
    assert called == [hermes_user_chat_session_id("alice", "math")]


def test_chat_status_idle_then_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CSFLOW_USER", "alice")
    get_storage().hermes_create(
        HermesAgent(id="math", name="Math", profile_root="x", created_by_user="alice")
    )

    r = client.get("/api/hermes/agents/math/chat/status")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "idle"

    sk = hermes_user_chat_session_id("alice", "math")
    job = _mk_job(sk)
    job.steps.append({"kind": "tool", "name": "cronjob", "seq": 1})
    chat_svc._JOBS[sk] = job

    body = client.get("/api/hermes/agents/math/chat/status").json()
    assert body["status"] == "running"
    assert body["steps"][0]["name"] == "cronjob"
    assert body["progress"]["toolCalls"] == 0  # snapshot default until a poll runs
