"""Tests for :mod:`app.main` (FastAPI app)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.logging_setup import get_logger
from app.main import _sweep_orphaned_runs, create_app


def _make_run(run_id: str, status):
    """Create a Flow + FlowRun in *status*; return the run."""
    from app.models import (
        AgentKind,
        Flow,
        FlowAgent,
        FlowRun,
        FlowSpec,
        FlowTask,
    )
    from app.storage import get_storage

    spec = FlowSpec(
        agents=[FlowAgent(
            id="leader", kind=AgentKind.claude, repo="/tmp/main", is_leader=True,
        )],
        tasks=[FlowTask(
            id="ts", owner_agent_id="leader", subject="x", description="",
            depends_on=[], is_leader_summary=True,
        )],
    )
    storage = get_storage()
    flow = storage.flow_create(
        Flow(name="t", description="", owner_user="alice").with_spec(spec)
    )
    return storage.run_create(FlowRun(
        id=run_id, flow_id=flow.id, flow_version=1, team_name=f"csflow-{run_id}",
        status=status, inputs={}, user="alice",
    ))


def test_sweep_orphaned_runs_reconciles_active_driving_only() -> None:
    from app.models import RunStatus
    from app.storage import get_storage

    running = _make_run("run-sweep-running", RunStatus.running)
    pending = _make_run("run-sweep-pending", RunStatus.pending)
    checkpoint = _make_run("run-sweep-ckpt", RunStatus.awaiting_user_checkpoint)
    review = _make_run("run-sweep-review", RunStatus.awaiting_user_review)
    complaint = _make_run("run-sweep-complaint", RunStatus.awaiting_user_complaint)
    completed = _make_run("run-sweep-done", RunStatus.completed)

    log = get_logger("test")
    swept = _sweep_orphaned_runs(get_storage(), log)
    assert swept == 3

    storage = get_storage()
    for r in (running, pending, checkpoint):
        refreshed = storage.run_get(r.id)
        assert refreshed.status == RunStatus.orphaned
        assert refreshed.finished_at is not None
    # PRESERVED + already-terminal states untouched.
    assert storage.run_get(review.id).status == RunStatus.awaiting_user_review
    assert storage.run_get(complaint.id).status == RunStatus.awaiting_user_complaint
    assert storage.run_get(completed.id).status == RunStatus.completed

    # Idempotent: a second sweep finds nothing new.
    assert _sweep_orphaned_runs(get_storage(), log) == 0

    events = storage.event_list(run_id=running.id, since_id=None, limit=50)
    assert any(e.type == "run_orphaned" for e in events)


@pytest.fixture
def client():
    """TestClient as context manager so FastAPI lifespan events fire."""
    with TestClient(create_app()) as c:
        yield c


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert "bootstrap" in body


def test_health_bootstrap_summary_fields(client: TestClient) -> None:
    resp = client.get("/health")
    snap = resp.json()["bootstrap"]
    for key in (
        "home",
        "config_present",
        "db_present",
        "flows_count",
        "runs_count",
        "agents_count",
        "skills_source_count",
    ):
        assert key in snap
    # After lifespan startup, the layout exists and config is auto-created.
    assert snap["config_present"] is True
    assert snap["flows_count"] == 0
    assert snap["runs_count"] == 0


def test_version_endpoint(client: TestClient) -> None:
    resp = client.get("/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": __version__}


def test_startup_fails_when_required_board_proxy_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CSFLOW_DISABLE_BOARD", "0")

    class _FakeBoard:
        last_error = "clawteam board missing"

        def start(self) -> bool:
            return False

        async def stop(self, *, grace_seconds: float = 5.0) -> None:  # pragma: no cover
            return None

    monkeypatch.setattr("app.board_proxy.get_board_proxy", lambda _cfg=None: _FakeBoard())

    with pytest.raises(RuntimeError, match="clawteam board failed to start"):
        with TestClient(create_app()):
            pass


def test_startup_fails_when_clawteam_runtime_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CSFLOW_DISABLE_CLAWTEAM_STACK_CHECK", "0")
    monkeypatch.setenv("CSFLOW_DISABLE_BOARD", "1")
    monkeypatch.setattr(
        "app.main._probe_clawteam_runtime",
        lambda: (False, "runtime command missing"),
    )
    with pytest.raises(RuntimeError, match="clawteam runtime readiness check failed"):
        with TestClient(create_app()):
            pass


def test_startup_fails_when_clawteam_mcp_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CSFLOW_DISABLE_CLAWTEAM_STACK_CHECK", "0")
    monkeypatch.setenv("CSFLOW_DISABLE_BOARD", "1")
    monkeypatch.setattr("app.main._probe_clawteam_runtime", lambda: (True, ""))

    async def _fake_probe(_default_user: str) -> tuple[bool, str]:
        return False, "mcp bootstrap timeout"

    monkeypatch.setattr("app.main._probe_clawteam_mcp", _fake_probe)
    with pytest.raises(RuntimeError, match="clawteam mcp readiness check failed"):
        with TestClient(create_app()):
            pass
