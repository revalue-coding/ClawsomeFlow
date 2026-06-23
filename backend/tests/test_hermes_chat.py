"""Tests for tracked Hermes chat jobs (services/hermes_chat) + reset/status API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import HermesAgent
from app.scheduler.naming import hermes_user_chat_session_id
from app.services import hermes_chat as chat_svc
from app.storage import get_storage


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_registry():
    chat_svc._JOBS.clear()
    yield
    chat_svc._JOBS.clear()


class _FakePopen:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return "", ""


def _mk_job(session_key: str, agent_id: str = "math") -> chat_svc.ChatJob:
    return chat_svc.ChatJob(
        agent_id=agent_id,
        session_key=session_key,
        started_at=time.monotonic(),
    )


def test_snapshot_shape() -> None:
    job = _mk_job("sk2")
    snap = job.snapshot()
    assert snap["status"] == "running"
    assert snap["steps"] == []
    assert "elapsedSec" in snap["progress"]
    assert snap["progress"]["toolCalls"] == 0


def test_run_turn_success(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _mk_job("sk-run")
    calls: list[bool] = []

    def _fake_spawn(agent_id, *, message, workdir, resume):
        calls.append(resume)
        return _FakePopen()

    def _fake_communicate(proc):
        return 0, "hello reply", ""

    monkeypatch.setattr(chat_svc, "_spawn_hermes", _fake_spawn)
    monkeypatch.setattr(chat_svc, "_communicate", _fake_communicate)
    chat_svc._JOBS["sk-run"] = job

    chat_svc._run_turn(job, "hi", "/tmp/wd", resume=True)

    assert job.status == "done"
    assert job.final_text == "hello reply"
    assert calls == [True]


def test_run_turn_retries_transient_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _mk_job("sk-retry")
    attempts = {"n": 0}

    def _fake_spawn(agent_id, *, message, workdir, resume):
        return _FakePopen()

    def _fake_communicate(proc):
        attempts["n"] += 1
        if attempts["n"] < 2:
            return 1, "", "hermes -z: agent failed: [Errno -2] Name or service not known"
        return 0, "recovered", ""

    monkeypatch.setattr(chat_svc, "_spawn_hermes", _fake_spawn)
    monkeypatch.setattr(chat_svc, "_communicate", _fake_communicate)
    monkeypatch.setattr(chat_svc, "CHAT_CONNECTION_RETRY_DELAYS_SEC", (0.0, 0.0))
    chat_svc._JOBS["sk-retry"] = job

    chat_svc._run_turn(job, "hi", "/tmp/wd", resume=False)

    assert attempts["n"] == 2
    assert job.status == "done"
    assert job.final_text == "recovered"


def test_kill_chat_kills_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(
        chat_svc._subproc_registry,
        "kill_group",
        lambda proc, **kw: (killed.append(proc.pid) or True),
    )
    monkeypatch.setattr(chat_svc._subproc_registry, "unregister", lambda proc: None)

    job = _mk_job("sk1")
    job.proc = _FakePopen()
    chat_svc._JOBS["sk1"] = job

    assert chat_svc.kill_chat("sk1") is True
    assert killed == [job.proc.pid]
    assert chat_svc.get_job("sk1") is None
    assert job.status == "error"
    assert job.error == "cancelled"
    assert chat_svc.kill_chat("sk1") is False


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
    chat_svc._JOBS[sk] = job

    body = client.get("/api/hermes/agents/math/chat/status").json()
    assert body["status"] == "running"
    assert body["steps"] == []
    assert "elapsedSec" in body["progress"]
