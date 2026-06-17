"""Tests for Hermes dashboard auto-start."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import hermes_dashboard as dash
from app.services.hermes_agents import HermesUnavailable


def test_dashboard_url_default() -> None:
    assert dash.dashboard_url() == "http://127.0.0.1:9119/chat"


def test_looks_like_hermes() -> None:
    index = (
        "<!doctype html><html><head><title>Hermes Agent - Dashboard</title>"
        '<script>window.__HERMES_SESSION_TOKEN__="x";</script></head></html>'
    )
    assert dash._looks_like_hermes(index) is True
    # A foreign HTTP server (e.g. python http.server / nginx) must not match.
    assert dash._looks_like_hermes("<html><body>Directory listing for /</body></html>") is False
    assert dash._looks_like_hermes("<h1>Welcome to nginx!</h1>") is False


def test_ensure_returns_when_hermes_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash, "hermes_executable", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(dash, "_classify", lambda host, port: "hermes")

    def _no_spawn(**_kw):  # pragma: no cover - must not be called
        raise AssertionError("must not spawn when Hermes is already running")

    monkeypatch.setattr(dash, "_spawn_dashboard", _no_spawn)
    assert dash.ensure_hermes_dashboard_url() == "http://127.0.0.1:9119/chat"


def test_ensure_spawns_on_free_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash, "hermes_executable", lambda: "/usr/bin/hermes")
    state = {"running": False, "spawned": False}

    def _classify(host: str, port: int) -> str:
        del host, port
        return "hermes" if state["running"] else "free"

    def _spawn(*, exe: str, host: str, port: int, skip_build: bool, profile=None):
        del exe, host, port, skip_build
        state["spawned"] = True
        state["running"] = True
        proc = MagicMock()
        proc.pid = 123
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(dash, "_classify", _classify)
    monkeypatch.setattr(dash, "_spawn_dashboard", _spawn)
    assert dash.ensure_hermes_dashboard_url() == "http://127.0.0.1:9119/chat"
    assert state["spawned"] is True


def test_ensure_auto_switches_when_default_port_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dash, "hermes_executable", lambda: "/usr/bin/hermes")
    started = {9120: False}

    def _classify(host: str, port: int) -> str:
        del host
        if port == 9119:
            return "foreign"  # squatted by another service
        if port == 9120:
            return "hermes" if started[9120] else "free"
        return "free"

    def _spawn(*, exe: str, host: str, port: int, skip_build: bool, profile=None):
        del exe, host, skip_build
        assert port == 9120, f"should spawn on the first free port, got {port}"
        started[9120] = True
        proc = MagicMock()
        proc.pid = 456
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(dash, "_classify", _classify)
    monkeypatch.setattr(dash, "_spawn_dashboard", _spawn)
    assert dash.ensure_hermes_dashboard_url() == "http://127.0.0.1:9120/chat"


def test_ensure_profile_spawns_dedicated_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile dashboard must NOT reuse the root instance on 9119 (it serves a
    different home): it spawns its own ``hermes -p <id> dashboard`` on a free
    port, passing the profile through, and tracks it for reuse."""
    dash._PROFILE_DASHBOARDS.clear()
    monkeypatch.setattr(dash, "hermes_executable", lambda: "/usr/bin/hermes")
    state = {9120: False}
    seen_profile: list[str | None] = []

    def _classify(host: str, port: int) -> str:
        del host
        if port == 9119:
            return "hermes"  # root dashboard already serving — must be ignored
        if port == 9120:
            return "hermes" if state[9120] else "free"
        return "free"

    def _spawn(*, exe: str, host: str, port: int, skip_build: bool, profile=None):
        del exe, host, skip_build
        seen_profile.append(profile)
        assert port == 9120
        state[9120] = True
        proc = MagicMock()
        proc.pid = 789
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(dash, "_classify", _classify)
    monkeypatch.setattr(dash, "_spawn_dashboard", _spawn)
    monkeypatch.setattr(dash._subproc_registry, "register", lambda proc: None)

    url = dash.ensure_hermes_dashboard_url(profile="math")
    assert url == "http://127.0.0.1:9120/chat"
    assert seen_profile == ["math"]
    assert dash._PROFILE_DASHBOARDS["math"][1] == 9120

    # A second call reuses the tracked instance (no second spawn).
    seen_profile.clear()
    url2 = dash.ensure_hermes_dashboard_url(profile="math")
    assert url2 == "http://127.0.0.1:9120/chat"
    assert seen_profile == []
    dash._PROFILE_DASHBOARDS.clear()


def test_ensure_raises_when_all_ports_foreign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash, "hermes_executable", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(dash, "_classify", lambda host, port: "foreign")

    def _no_spawn(**_kw):  # pragma: no cover - must not be called
        raise AssertionError("must not spawn when no free port is available")

    monkeypatch.setattr(dash, "_spawn_dashboard", _no_spawn)
    with pytest.raises(HermesUnavailable, match="No free port"):
        dash.ensure_hermes_dashboard_url()
