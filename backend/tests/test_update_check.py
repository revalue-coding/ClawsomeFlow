"""Tests for the stable-release update check + self-upgrade trigger."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import update_check


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _releases(*versions: str) -> dict:
    return {"releases": {v: [{"filename": f"{v}.whl"}] for v in versions}}


@pytest.fixture(autouse=True)
def _clear_cache():
    update_check.reset_cache()
    yield
    update_check.reset_cache()


# ── service: version selection / channel policy ───────────────────────


def test_stable_current_with_newer_stable(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", "0.1.0")
    monkeypatch.setattr(
        update_check.httpx, "get",
        lambda *a, **k: _FakeResp(_releases("0.1.0", "0.2.0", "0.3.0b1")),
    )
    status = update_check.compute_update_status(force=True)
    assert status.is_prerelease is False
    assert status.latest_version == "0.2.0"  # prerelease 0.3.0b1 filtered out
    assert status.update_available is True


def test_prerelease_current_never_prompts_and_skips_network(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", "0.1.1b12")

    def _boom(*a, **k):
        raise AssertionError("network must not be hit for prerelease installs")

    monkeypatch.setattr(update_check.httpx, "get", _boom)
    status = update_check.compute_update_status(force=True)
    assert status.is_prerelease is True
    assert status.update_available is False
    assert status.latest_version is None


def test_already_latest(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", "0.2.0")
    monkeypatch.setattr(
        update_check.httpx, "get",
        lambda *a, **k: _FakeResp(_releases("0.1.0", "0.2.0")),
    )
    status = update_check.compute_update_status(force=True)
    assert status.update_available is False
    assert status.latest_version == "0.2.0"


def test_prereleases_filtered_when_only_betas_newer(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", "0.2.0")
    monkeypatch.setattr(
        update_check.httpx, "get",
        lambda *a, **k: _FakeResp(_releases("0.2.0", "0.3.0b1", "0.3.0rc1")),
    )
    status = update_check.compute_update_status(force=True)
    # No newer *stable* release exists.
    assert status.latest_version == "0.2.0"
    assert status.update_available is False


def test_network_failure_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", "0.1.0")

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(update_check.httpx, "get", _boom)
    status = update_check.compute_update_status(force=True)
    assert status.latest_version is None
    assert status.update_available is False


def test_cache_avoids_repeat_requests(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", "0.1.0")
    calls = {"n": 0}

    def _counter(*a, **k):
        calls["n"] += 1
        return _FakeResp(_releases("0.1.0", "0.2.0"))

    monkeypatch.setattr(update_check.httpx, "get", _counter)
    update_check.fetch_latest_stable(now=0.0)
    update_check.fetch_latest_stable(now=1.0)  # within TTL
    assert calls["n"] == 1
    update_check.fetch_latest_stable(force=True, now=2.0)
    assert calls["n"] == 2


# ── API: GET /update-status ───────────────────────────────────────────


def test_update_status_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(update_check, "__version__", "0.1.0")
    monkeypatch.setattr(
        update_check.httpx, "get",
        lambda *a, **k: _FakeResp(_releases("0.1.0", "0.2.0")),
    )
    with TestClient(create_app()) as client:
        r = client.get("/api/system/update-status?force=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updateAvailable"] is True
    assert body["latestVersion"] == "0.2.0"
    assert body["enabled"] is True


def test_update_status_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(update_check_enabled=False),
    )
    with TestClient(create_app()) as client:
        r = client.get("/api/system/update-status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


# ── API: POST /upgrade guards + launch ────────────────────────────────


def _stub_status(**kw):
    base = dict(
        current_version="0.1.0",
        latest_version="0.2.0",
        update_available=True,
        is_prerelease=False,
        upgrade_script_url="https://clawsomeflow.com/upgrade.sh",
    )
    base.update(kw)
    return update_check.UpdateStatus(**base)


def test_upgrade_triggers_launch(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.update_check.compute_update_status",
        lambda **k: _stub_status(),
    )
    launched: dict = {}

    def _fake_launch(url: str) -> str:
        launched["url"] = url
        return "subprocess"

    monkeypatch.setattr("app.api.system._launch_self_upgrade", _fake_launch)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/upgrade")
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    assert r.json()["via"] == "subprocess"
    assert launched["url"] == "https://clawsomeflow.com/upgrade.sh"


def test_upgrade_rejected_for_prerelease(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.update_check.compute_update_status",
        lambda **k: _stub_status(is_prerelease=True, update_available=False, latest_version=None),
    )
    monkeypatch.setattr(
        "app.api.system._launch_self_upgrade",
        lambda url: (_ for _ in ()).throw(AssertionError("must not launch")),
    )
    with TestClient(create_app()) as client:
        r = client.post("/api/system/upgrade")
    assert r.status_code == 409
    assert r.json()["error"] == "UPGRADE_NOT_ALLOWED"


def test_upgrade_rejected_when_no_update(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.update_check.compute_update_status",
        lambda **k: _stub_status(update_available=False, latest_version="0.1.0"),
    )
    with TestClient(create_app()) as client:
        r = client.post("/api/system/upgrade")
    assert r.status_code == 409
    assert r.json()["error"] == "NO_UPGRADE_AVAILABLE"


def test_upgrade_rejected_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(update_check_enabled=False),
    )
    with TestClient(create_app()) as client:
        r = client.post("/api/system/upgrade")
    assert r.status_code == 409
    assert r.json()["error"] == "UPDATE_CHECK_DISABLED"


# ── API: active-runs gate on /upgrade + GET /active-runs ───────────────


def _make_active_run(status, run_id: str = "run-active-up01"):
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


def test_upgrade_rejected_when_active_runs_unconfirmed(monkeypatch) -> None:
    from app.models import RunStatus

    monkeypatch.setattr(
        "app.api.system.update_check.compute_update_status",
        lambda **k: _stub_status(),
    )
    monkeypatch.setattr(
        "app.api.system._launch_self_upgrade",
        lambda url: (_ for _ in ()).throw(AssertionError("must not launch")),
    )
    _make_active_run(RunStatus.running)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/upgrade")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "ACTIVE_RUNS_PRESENT"
    assert r.json()["details"]["active_runs"] == 1


def test_upgrade_proceeds_when_active_runs_confirmed(monkeypatch) -> None:
    from app.models import RunStatus

    monkeypatch.setattr(
        "app.api.system.update_check.compute_update_status",
        lambda **k: _stub_status(),
    )
    launched: dict = {}

    def _fake_launch(url: str) -> str:
        launched["url"] = url
        return "subprocess"

    monkeypatch.setattr("app.api.system._launch_self_upgrade", _fake_launch)
    _make_active_run(RunStatus.running)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/upgrade", json={"confirmActiveRuns": True})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    assert launched["url"] == "https://clawsomeflow.com/upgrade.sh"


def test_active_runs_endpoint_excludes_preserved(monkeypatch) -> None:
    from app.models import RunStatus

    _make_active_run(RunStatus.running, run_id="run-up-running")
    # PRESERVED state survives a restart losslessly → not "active" for the gate.
    _make_active_run(RunStatus.awaiting_user_review, run_id="run-up-review")
    with TestClient(create_app()) as client:
        r = client.get("/api/system/active-runs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["runs"][0]["id"] == "run-up-running"
    assert body["runs"][0]["status"] == "running"
