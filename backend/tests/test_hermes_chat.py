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


def test_run_turn_recovers_empty_stdout_from_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (0.1.25b1 dropped this): rc==0 but empty stdout must recover
    the answer from a one-shot ``sessions export`` (the answer of a -c resume /
    tool-heavy turn often lives only in the session store)."""
    job = _mk_job("sk-empty")
    monkeypatch.setattr(chat_svc, "_spawn_hermes", lambda *a, **k: _FakePopen())
    monkeypatch.setattr(chat_svc, "_communicate", lambda proc: (0, "", ""))
    monkeypatch.setattr(chat_svc, "_discover_session_id", lambda aid: "20260101_000000_abc")
    monkeypatch.setattr(
        chat_svc,
        "_export_session",
        lambda aid, sid: {"messages": [{"role": "assistant", "content": "from session"}]},
    )
    chat_svc._JOBS["sk-empty"] = job

    chat_svc._run_turn(job, "hi", "/tmp/wd", resume=False)

    assert job.status == "done"
    assert job.final_text == "from session"


def test_run_turn_empty_stdout_tool_only_marks_no_text_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc==0, empty stdout, no assistant text but tool calls happened → a
    legitimate 'no visible reply' (done + marker), NOT an error."""
    job = _mk_job("sk-toolonly")
    monkeypatch.setattr(chat_svc, "_spawn_hermes", lambda *a, **k: _FakePopen())
    monkeypatch.setattr(chat_svc, "_communicate", lambda proc: (0, "", ""))
    monkeypatch.setattr(chat_svc, "_discover_session_id", lambda aid: "sid_x")
    monkeypatch.setattr(
        chat_svc, "_export_session", lambda aid, sid: {"tool_call_count": 2, "messages": []}
    )
    chat_svc._JOBS["sk-toolonly"] = job

    chat_svc._run_turn(job, "hi", "/tmp/wd", resume=False)

    assert job.status == "done"
    assert job.final_text == chat_svc._NO_TEXT_REPLY_MARKER


def test_run_turn_empty_stdout_no_recovery_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc==0, empty stdout, nothing recoverable → still a clean error (not hang)."""
    job = _mk_job("sk-none")
    monkeypatch.setattr(chat_svc, "_spawn_hermes", lambda *a, **k: _FakePopen())
    monkeypatch.setattr(chat_svc, "_communicate", lambda proc: (0, "", ""))
    monkeypatch.setattr(chat_svc, "_discover_session_id", lambda aid: None)
    chat_svc._JOBS["sk-none"] = job

    chat_svc._run_turn(job, "hi", "/tmp/wd", resume=False)

    assert job.status == "error"
    assert "no reply" in job.error


def test_run_turn_spawn_exception_fails_job_not_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (0.1.25b1): if Popen raises (e.g. ``cwd='~'`` →
    FileNotFoundError) inside the background thread, the job must transition to
    ``error`` — never wedge in ``running`` (which streamed an endless spinner to
    the WebUI with no reply and no error)."""
    job = _mk_job("sk-boom")

    def _boom_spawn(agent_id, *, message, workdir, resume):
        raise FileNotFoundError(2, "No such file or directory: '~'")

    monkeypatch.setattr(chat_svc, "_spawn_hermes", _boom_spawn)
    chat_svc._JOBS["sk-boom"] = job

    chat_svc._run_turn(job, "hi", "~", resume=False)

    assert job.status == "error"
    assert job.status != "running"
    assert "~" in job.error or "failed to start" in job.error


def test_start_chat_expands_tilde_workdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (0.1.25b1): a tilde workdir must be expanded to an absolute
    path before it reaches Popen, so the spawn never raises FileNotFoundError."""
    seen: dict[str, str] = {}

    def _fake_spawn(agent_id, *, message, workdir, resume):
        seen["workdir"] = workdir
        return _FakePopen()

    monkeypatch.setattr(chat_svc, "_spawn_hermes", _fake_spawn)
    monkeypatch.setattr(chat_svc, "_communicate", lambda proc: (0, "ok", ""))
    monkeypatch.setattr(chat_svc.ha, "hermes_executable", lambda: "/usr/bin/hermes")

    job = chat_svc.start_chat(
        "math", message="hi", workdir="~", resume=False, session_key="sk-tilde"
    )
    # _run_turn runs in a daemon thread — wait briefly for it to spawn.
    for _ in range(200):
        if "workdir" in seen:
            break
        time.sleep(0.01)
    chat_svc._JOBS.pop("sk-tilde", None)

    assert seen.get("workdir", "~") != "~"
    assert seen["workdir"].startswith("/")
    assert job.agent_id == "math"


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
