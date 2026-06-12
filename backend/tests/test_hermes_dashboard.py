"""Tests for Hermes dashboard auto-start."""

from __future__ import annotations

import socket
from unittest.mock import MagicMock

import pytest

from app.services import hermes_dashboard as dash


def test_dashboard_url_default() -> None:
    assert dash.dashboard_url() == "http://127.0.0.1:9119/chat"


def test_ensure_returns_when_port_already_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash, "hermes_executable", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(dash, "_port_open", lambda host, port, timeout_sec=0.4: True)
    assert dash.ensure_hermes_dashboard_url() == "http://127.0.0.1:9119/chat"


def test_ensure_spawns_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash, "hermes_executable", lambda: "/usr/bin/hermes")
    seen: dict[str, object] = {"open": False, "spawned": False}

    def _port(host: str, port: int, *, timeout_sec: float = 0.4) -> bool:
        del host, port, timeout_sec
        return bool(seen["open"])

    def _spawn(*, exe: str, host: str, port: int, skip_build: bool):
        del exe, host, port, skip_build
        seen["spawned"] = True
        seen["open"] = True
        proc = MagicMock()
        proc.pid = 123
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(dash, "_port_open", _port)
    monkeypatch.setattr(dash, "_spawn_dashboard", _spawn)
    assert dash.ensure_hermes_dashboard_url() == "http://127.0.0.1:9119/chat"
    assert seen["spawned"] is True
