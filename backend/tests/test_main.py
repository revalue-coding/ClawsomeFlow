"""Tests for :mod:`app.main` (FastAPI app)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.main import create_app


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
