"""Tests for :mod:`app.integrations.mcp_compat`."""

from __future__ import annotations

import pytest

from app.integrations import mcp_compat as mod


def test_parse_mcp_version() -> None:
    assert mod.parse_mcp_version("1.27.1") == (1, 27, 1)
    assert mod.parse_mcp_version("2.0.0a2") == (2, 0, 0)
    assert mod.parse_mcp_version("not-a-version") is None


def test_mcp_sdk_compatible() -> None:
    assert mod.mcp_sdk_compatible("1.28.0") is True
    assert mod.mcp_sdk_compatible("2.0.0a2") is False


def test_mcp_sdk_compatible_uses_installed_version_when_unspecified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "installed_mcp_version", lambda: None)
    assert mod.mcp_sdk_compatible() is False
    monkeypatch.setattr(mod, "installed_mcp_version", lambda: "2.0.0a2")
    assert mod.mcp_sdk_compatible() is False


def test_incompatible_mcp_detail_for_alpha() -> None:
    detail = mod.incompatible_mcp_detail("2.0.0a2")
    assert "Incompatible mcp" in detail
    assert "1.x" in detail


def test_ensure_mcp_sdk_compatible_noop_when_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "mcp_sdk_compatible", lambda: True)
    called = {"n": 0}

    def _fail(*_a, **_kw):
        called["n"] += 1
        return False, "should not run"

    monkeypatch.setattr(mod, "pip_install_mcp_sdk", _fail)
    ok, detail = mod.ensure_mcp_sdk_compatible()
    assert ok is True
    assert detail == ""
    assert called["n"] == 0


def test_ensure_mcp_sdk_compatible_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    states = {"compatible": False}

    def _compatible() -> bool:
        return states["compatible"]

    def _repair(*, pip_cmd=None):
        states["compatible"] = True
        return True, ""

    monkeypatch.setattr(mod, "mcp_sdk_compatible", _compatible)
    monkeypatch.setattr(mod, "pip_install_mcp_sdk", _repair)

    ok, detail = mod.ensure_mcp_sdk_compatible()
    assert ok is True
    assert detail == ""


def test_pip_install_mcp_sdk_invokes_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(mod.subprocess, "run", _run)
    ok, detail = mod.pip_install_mcp_sdk(pip_cmd=["python", "-m", "pip", "install", "--upgrade"])
    assert ok is True
    assert detail == ""
    assert captured["cmd"] == [
        "python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        mod.MCP_SDK_SPEC,
    ]
