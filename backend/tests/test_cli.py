"""Tests for :mod:`app.cli` (typer entry-point)."""

from __future__ import annotations

from typer.testing import CliRunner

from app import __version__
from app.cli import app


def test_version_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_status_command_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # Phase 9 status renders a Rich table — sanity check on the headers.
    assert "ClawsomeFlow status" in result.stdout
    assert "version" in result.stdout
    assert "deployment_mode" in result.stdout
