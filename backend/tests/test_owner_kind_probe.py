"""Tests for maximum-tolerance owner-kind CLI probes."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.integrations import owner_kind_probe as probe


def test_probe_binary_installed_via_resolve_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "resolve_binary", lambda name: "/opt/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(probe.shutil, "which", lambda _name: None)
    assert probe.probe_binary_installed("claude") is True


def test_probe_binary_installed_via_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "resolve_binary", lambda _name: None)
    monkeypatch.setattr(probe.shutil, "which", lambda _name: None)
    monkeypatch.setattr(probe, "_probe_npm_global_bin", lambda _name: False)

    class _Proc:
        returncode = 0
        stdout = "/home/user/.local/bin/gemini\n"

    monkeypatch.setattr(probe, "_is_executable", lambda path: str(path).endswith("gemini"))
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: _Proc())
    assert probe.probe_binary_installed("gemini") is True


def test_detect_persistent_owner_kinds_uses_openclaw_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "resolve_openclaw_executable", lambda **kw: "/opt/openclaw")
    monkeypatch.setattr(probe, "probe_binary_installed", lambda *a, **kw: False)
    assert probe.detect_persistent_owner_kinds() == ["openclaw"]


def test_detect_temporary_owner_kinds_unions_cursor_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []

    def _fake_probe(*names: str, extra_paths=()) -> bool:
        seen.append(tuple(names))
        return names == ("agent", "cursor")

    monkeypatch.setattr(probe, "probe_binary_installed", _fake_probe)
    kinds = probe.detect_temporary_owner_kinds()
    assert "cursor" in kinds
    assert ("agent", "cursor") in seen


def test_detect_temporary_owner_kinds_includes_qoder_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_probe(*names: str, extra_paths=()) -> bool:
        return "qodercli" in names

    monkeypatch.setattr(probe, "probe_binary_installed", _fake_probe)
    assert "qoder" in probe.detect_temporary_owner_kinds()


def test_cursor_extra_paths_only_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    paths = probe._cursor_extra_paths()
    assert any("Cursor.app" in str(p) for p in paths)

    monkeypatch.setattr(probe.sys, "platform", "linux")
    assert probe._cursor_extra_paths() == ()


def test_probe_binary_installed_extra_paths(tmp_path: Path) -> None:
    tool = tmp_path / "agent"
    tool.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    tool.chmod(0o755)
    assert probe.probe_binary_installed("missing", extra_paths=[tool]) is True
