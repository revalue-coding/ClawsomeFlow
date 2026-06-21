"""Tests for :mod:`app.runtime_bins`."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import runtime_bins as mod


def test_resolve_binary_prefers_managed_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    venv_bin = home / ".clawsomeflow" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    tool = venv_bin / "clawteam"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)

    monkeypatch.setenv("CSFLOW_HOME", str(home / ".clawsomeflow"))
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mod, "current_entrypoint_bindir", lambda: None)
    monkeypatch.setattr(mod, "current_python_bindir", lambda: Path("/usr/bin"))

    resolved = mod.resolve_binary("clawteam")
    assert resolved == str(tool.resolve())
