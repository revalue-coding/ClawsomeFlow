"""Tests for uninstall safety / full-uninstall flow and purge-data command.

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


# ── csflow uninstall (with optional full wipe) ───────────────────────


def test_uninstall_no_purge_flag_in_help(runner: CliRunner, tmp_home: Path) -> None:
    """Sanity: uninstall must not expose destructive flags."""
    result = runner.invoke(app, ["uninstall", "--help"])
    assert result.exit_code == 0
    assert "--purge" not in result.output
    assert "--thorough" not in result.output


def test_uninstall_preserves_data_with_yes(
    runner: CliRunner, tmp_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with ``--yes``, uninstall must leave ~/.clawsomeflow/ alone."""
    # Stub out the OpenClaw side-effect — we're not testing that here.
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


# ── csflow purge-data ─────────────────────────────────────────────────


def test_purge_requires_literal_PURGE(
    runner: CliRunner, tmp_home: Path,
) -> None:
    """Without the magic word, purge-data must abort and leave files intact."""
    # Provide an answer that's NOT the magic word.
    result = runner.invoke(app, ["purge-data"], input="yes\n")
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
    result = runner.invoke(app, ["purge-data"], input="PURGE\n")
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
        "purge-data", "--i-understand-this-deletes-everything",
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
    # Also patch the names re-exported by app.cli.purge.
    from app.cli import purge as purge_mod
    monkeypatch.setattr(purge_mod, "read_pid", lambda: 12345)
    monkeypatch.setattr(purge_mod, "is_alive", lambda pid: True)

    result = runner.invoke(app, [
        "purge-data", "--i-understand-this-deletes-everything",
    ])
    assert result.exit_code == 1, result.output
    assert "backend is running" in result.output
    assert (tmp_home / ".flows" / "demo.json").exists()


def test_purge_yes_short_flag_does_not_exist(runner: CliRunner) -> None:
    """``-y`` MUST NOT exist on purge-data — that was an explicit design call.

    Defends against muscle memory from other commands that DO accept ``-y``.
    """
    result = runner.invoke(app, ["purge-data", "-y"])
    # Typer rejects unknown options with exit 2.
    assert result.exit_code == 2, result.output
    assert "no such option" in result.output.lower() or \
           "unexpected" in result.output.lower() or \
           "-y" in result.output
