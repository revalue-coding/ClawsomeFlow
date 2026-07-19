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


def test_confirm_active_runs_skipped_when_non_interactive(monkeypatch) -> None:
    import typer
    from rich.console import Console

    from app.cli import _runtime

    monkeypatch.setattr(_runtime, "read_pid", lambda: 1234)
    monkeypatch.setattr(_runtime, "is_alive", lambda _pid: True)
    monkeypatch.setattr(_runtime, "active_driving_run_count", lambda: 3)
    monkeypatch.setattr(
        typer, "confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    # --yes / backend self-calls bypass the check entirely.
    _runtime.confirm_no_active_runs_or_exit(
        non_interactive=True, action="stop the service", console=Console(),
    )


def test_confirm_active_runs_skipped_when_service_down(monkeypatch) -> None:
    import typer
    from rich.console import Console

    from app.cli import _runtime

    monkeypatch.setattr(_runtime, "read_pid", lambda: None)
    monkeypatch.setattr(
        _runtime, "active_driving_run_count",
        lambda: (_ for _ in ()).throw(AssertionError("should not query DB")),
    )
    monkeypatch.setattr(
        typer, "confirm",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    _runtime.confirm_no_active_runs_or_exit(
        non_interactive=False, action="restart the service", console=Console(),
    )


def test_confirm_active_runs_exits_when_user_declines(monkeypatch) -> None:
    import typer
    from rich.console import Console

    from app.cli import _runtime

    monkeypatch.setattr(_runtime, "read_pid", lambda: 1234)
    monkeypatch.setattr(_runtime, "is_alive", lambda _pid: True)
    monkeypatch.setattr(_runtime, "active_driving_run_count", lambda: 2)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)
    import pytest

    with pytest.raises(typer.Exit):
        _runtime.confirm_no_active_runs_or_exit(
            non_interactive=False, action="stop the service", console=Console(),
        )


def test_confirm_active_runs_proceeds_when_user_accepts(monkeypatch) -> None:
    import typer
    from rich.console import Console

    from app.cli import _runtime

    monkeypatch.setattr(_runtime, "read_pid", lambda: 1234)
    monkeypatch.setattr(_runtime, "is_alive", lambda _pid: True)
    monkeypatch.setattr(_runtime, "active_driving_run_count", lambda: 2)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    # Accepting returns normally (no exit).
    _runtime.confirm_no_active_runs_or_exit(
        non_interactive=False, action="stop the service", console=Console(),
    )


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
    assert "default_user" in result.stdout
