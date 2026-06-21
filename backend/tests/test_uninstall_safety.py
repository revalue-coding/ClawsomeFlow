"""Tests for uninstall safety / full-uninstall flow with --purge-data.

These are safety-critical: a regression here means a user could lose
their entire workspace by typing the wrong command at 2am.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from app import paths
from app.cli import app


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "csflow-home"
    home.mkdir()
    monkeypatch.setenv(paths.CSFLOW_HOME_ENV, str(home))
    # Seed something we'd be sad to lose.
    (home / "config.json").write_text('{"deployment_mode": "local"}', encoding="utf-8")
    (home / ".flows").mkdir()
    (home / ".flows" / "demo.json").write_text('{"id": "demo"}', encoding="utf-8")
    (home / "agents").mkdir()
    (home / "agents" / "alice").mkdir()
    (home / "agents" / "alice" / "important.txt").write_text(
        "user's life work", encoding="utf-8",
    )
    return home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── csflow uninstall (default: keep data) ─────────────────────────────


def test_uninstall_exposes_purge_flag_in_help(runner: CliRunner, tmp_home: Path) -> None:
    result = runner.invoke(app, ["uninstall", "--help"])
    assert result.exit_code == 0
    assert "--purge-data" in result.output
    assert "--thorough" not in result.output


def test_root_help_lists_grouped_commands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Lifecycle:" in result.output
    assert "Operations:" in result.output
    assert "Utilities:" in result.output
    assert "uninstall --purge-data" in result.output


def test_uninstall_preserves_data_with_yes(
    runner: CliRunner, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with ``--yes``, uninstall must leave ~/.clawsomeflow/ alone."""
    from app.integrations import openclaw_install

    async def _noop(**_kw):
        return openclaw_install.UninstallResult([], [], False)

    monkeypatch.setattr(openclaw_install, "uninstall_from_openclaw", _noop)

    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert (tmp_home / "config.json").exists()
    assert (tmp_home / ".flows" / "demo.json").exists()
    assert (tmp_home / "agents" / "alice" / "important.txt").exists()


def test_uninstall_always_keeps_data_even_when_service_teardown_runs(
    runner: CliRunner, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uninstall must stop/disable service and still keep ~/.clawsomeflow."""
    from app.cli import uninstall as uninstall_mod
    from app.integrations import openclaw_install

    called: dict[str, object] = {}

    def _fake_teardown(*, port: int):
        called["port"] = port
        return True, True, [1234], []

    async def _spy(**kwargs):
        called["purge"] = kwargs.get("purge_data_dir")
        return openclaw_install.UninstallResult([], [], False)

    monkeypatch.setattr(uninstall_mod, "stop_disable_and_release_port", _fake_teardown)
    monkeypatch.setattr(uninstall_mod, "describe_port_listeners", lambda _port: [])
    monkeypatch.setattr(openclaw_install, "uninstall_from_openclaw", _spy)

    result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert called["port"] == 17017
    assert called["purge"] is False
    assert tmp_home.exists()
    assert (tmp_home / "config.json").exists()
    assert (tmp_home / ".flows" / "demo.json").exists()


# ── csflow uninstall --purge-data ─────────────────────────────────────


def test_purge_requires_literal_PURGE(
    runner: CliRunner, tmp_home: Path,
) -> None:
    """Without the magic word, --purge-data must abort and leave files intact."""
    result = runner.invoke(app, ["uninstall", "--purge-data"], input="yes\n")
    assert result.exit_code == 1, result.output
    assert "didn't type PURGE" in result.output
    assert (tmp_home / ".flows" / "demo.json").exists()
    assert (tmp_home / "agents" / "alice" / "important.txt").exists()


def test_purge_proceeds_with_literal_PURGE(
    runner: CliRunner, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations import openclaw_install

    async def _noop(**_kw):
        return openclaw_install.UninstallResult([], [], False)

    monkeypatch.setattr(openclaw_install, "uninstall_from_openclaw", _noop)
    result = runner.invoke(app, ["uninstall", "--purge-data"], input="PURGE\n")
    assert result.exit_code == 0, result.output
    assert not tmp_home.exists()


def test_purge_proceeds_with_explicit_flag(
    runner: CliRunner, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripted use: ``--i-understand-this-deletes-everything`` skips prompt."""
    from app.integrations import openclaw_install

    async def _noop(**_kw):
        return openclaw_install.UninstallResult([], [], False)

    monkeypatch.setattr(openclaw_install, "uninstall_from_openclaw", _noop)
    result = runner.invoke(app, [
        "uninstall",
        "--purge-data",
        "--i-understand-this-deletes-everything",
    ])
    assert result.exit_code == 0, result.output
    assert not tmp_home.exists()


def test_purge_refuses_when_backend_running(
    runner: CliRunner, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-flight DB writes would corrupt — purge must wait until you stop."""
    from app.cli import _runtime
    monkeypatch.setattr(_runtime, "read_pid", lambda: 12345)
    monkeypatch.setattr(_runtime, "is_alive", lambda pid: True)
    from app.cli import uninstall as uninstall_mod
    monkeypatch.setattr(uninstall_mod, "read_pid", lambda: 12345)
    monkeypatch.setattr(uninstall_mod, "is_alive", lambda pid: True)

    result = runner.invoke(app, [
        "uninstall",
        "--purge-data",
        "--i-understand-this-deletes-everything",
    ])
    assert result.exit_code == 1, result.output
    assert "backend is running" in result.output
    assert (tmp_home / ".flows" / "demo.json").exists()


def test_purge_yes_short_flag_does_not_skip_purge_prompt(
    runner: CliRunner, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-y`` alone must not wipe data — muscle memory from other commands."""
    from app.integrations import openclaw_install

    async def _noop(**_kw):
        return openclaw_install.UninstallResult([], [], False)

    monkeypatch.setattr(openclaw_install, "uninstall_from_openclaw", _noop)
    result = runner.invoke(app, ["uninstall", "--purge-data", "-y"])
    assert result.exit_code == 1, result.output
    assert (tmp_home / ".flows" / "demo.json").exists()


def test_purge_data_command_removed(runner: CliRunner) -> None:
    result = runner.invoke(app, ["purge-data"])
    assert result.exit_code != 0


def test_rmtree_robust_tolerates_enoent(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent pid cleanup during rmtree must not abort purge."""
    import os
    import shutil

    from app.cli.uninstall import _rmtree_robust

    real_rmtree = shutil.rmtree

    def rmtree_with_simulated_race(path: str | os.PathLike[str], onerror=None):
        if onerror is not None:
            exc = FileNotFoundError(2, "No such file or directory", "csflow.pid")
            onerror(os.unlink, "csflow.pid", (FileNotFoundError, exc, exc.__traceback__))
        real_rmtree(path, onerror=onerror)

    monkeypatch.setattr(shutil, "rmtree", rmtree_with_simulated_race)
    _rmtree_robust(tmp_home)
    assert not tmp_home.exists()
