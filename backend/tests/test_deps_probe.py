"""Tests for dependency-probe robustness.

The preflight runs before the service is usable, so a probe that raises takes
the whole install down. The case that actually bit a user: on WSL the inherited
Windows ``PATH`` contains directories the Linux side cannot read, which makes
``execvp`` report a *missing* binary as EACCES instead of ENOENT.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.cli import deps


@pytest.fixture
def path_with_unreadable_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepend an unreadable directory to PATH, as WSL does with /mnt/c entries."""
    bad = tmp_path / "unreadable"
    bad.mkdir()
    bad.chmod(0o000)
    monkeypatch.setenv("PATH", f"{bad}{os.pathsep}/usr/bin{os.pathsep}/bin")
    yield bad
    bad.chmod(0o755)


def test_unreadable_path_entry_turns_enoent_into_eacces(
    path_with_unreadable_dir: Path,
) -> None:
    """Pin the OS behaviour the fix exists for; if this stops holding, the
    dedicated handling below is no longer load-bearing."""
    with pytest.raises(PermissionError):
        subprocess.run(["definitely-not-a-real-binary-xyz"], capture_output=True)


def test_run_reports_unavailable_instead_of_raising(
    path_with_unreadable_dir: Path,
) -> None:
    assert deps._run(["definitely-not-a-real-binary-xyz", "--version"]) is None


def test_exec_reports_failure_instead_of_raising(
    path_with_unreadable_dir: Path,
) -> None:
    ok, output = deps._exec(["definitely-not-a-real-binary-xyz"], timeout=5.0)
    assert ok is False
    assert output


def test_check_node_degrades_when_path_is_poisoned(
    path_with_unreadable_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Node is optional, so a poisoned PATH must not abort the preflight."""
    monkeypatch.setattr(deps, "_run", lambda *_a, **_kw: None)
    status = deps.check_node()
    assert status.ok is False
    assert status.found_version is None


def test_run_all_survives_missing_optional_tools(
    path_with_unreadable_dir: Path,
) -> None:
    """``run_all`` is what the installer calls; it must never raise."""
    results = deps.run_all()
    assert set(results) >= set(deps.REQUIRED)
    assert "node" in results


def test_node_is_not_install_blocking() -> None:
    """Guard the classification the installer relies on."""
    assert "node" in deps.OPTIONAL
    assert "node" not in deps.REQUIRED
    all_missing = {
        name: deps.Status(
            name=name, ok=False, found_version=None, detail="", install_hint=None,
        )
        for name in deps._CHECKS
    }
    assert "node" not in deps.fatal_missing(all_missing)
