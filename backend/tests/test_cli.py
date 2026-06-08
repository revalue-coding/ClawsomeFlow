"""Tests for :mod:`app.cli` (typer entry-point)."""

from __future__ import annotations

from typer.testing import CliRunner

from app import __version__
from app.cli import app
from app.cli.ops.runs import _extract_param_fields


def test_extract_param_fields_json_array() -> None:
    spec = {"variables": {"csflow.runtime.param_fields": '["target", "branch"]'}}
    assert _extract_param_fields(spec) == ["target", "branch"]


def test_extract_param_fields_comma_fallback_and_dedupe() -> None:
    spec = {"variables": {"csflow.runtime.param_fields": "a, b\nb,  c "}}
    assert _extract_param_fields(spec) == ["a", "b", "c"]


def test_extract_param_fields_legacy_requirement() -> None:
    spec = {"variables": {"csflow.runtime.requirement": "target project"}}
    assert _extract_param_fields(spec) == ["target project"]


def test_extract_param_fields_none() -> None:
    assert _extract_param_fields({}) == []
    assert _extract_param_fields({"variables": {}}) == []


def test_runs_start_no_prompt_errors_on_missing_field(monkeypatch) -> None:
    import app.cli.ops.runs as runs_mod

    monkeypatch.setattr(
        runs_mod, "get",
        lambda path, **kw: {
            "name": "demo",
            "spec": {"variables": {"csflow.runtime.param_fields": '["target"]'}},
        },
    )
    # post must never be reached when a required field is missing.
    monkeypatch.setattr(
        runs_mod, "post",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not POST")),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["runs", "start", "flow1", "--no-prompt"])
    assert result.exit_code != 0
    assert "target" in result.output


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
