from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer
from rich.console import Console


def _quiet_console() -> Console:
    return Console(record=True, force_terminal=False, color_system=None)


def test_guard_exits_when_openclaw_missing_and_install_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import _openclaw_runtime as guard
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        guard,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=False,
            found_version=None,
            detail="missing",
            install_hint="npm install -g openclaw",
        ),
    )
    monkeypatch.setattr(guard.typer, "confirm", lambda *_a, **_kw: False)

    with pytest.raises(typer.Exit) as exc:
        guard.ensure_openclaw_ready_or_exit(
            yes=False,
            action_label="部署",
            auto_install=True,
            console=_quiet_console(),
        )
    assert exc.value.exit_code == 1


def test_guard_can_install_when_gateway_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import _openclaw_runtime as guard
    from app.cli import deps as deps_mod

    checks = iter(
        [
            deps_mod.Status(
                name="openclaw",
                ok=False,
                found_version=None,
                detail="missing",
                install_hint="npm install -g openclaw",
            ),
            deps_mod.Status(
                name="openclaw",
                ok=True,
                found_version="OpenClaw 2026.5.18",
                detail="",
                install_hint="",
            ),
        ]
    )
    monkeypatch.setattr(guard, "check_openclaw", lambda: next(checks))
    monkeypatch.setattr(
        guard,
        "install_tool",
        lambda name, *, non_interactive=False: deps_mod.InstallResult(
            name=name,
            ok=True,
            detail="",
        ),
    )
    monkeypatch.setattr(
        guard,
        "resolve_openclaw_executable",
        lambda: "/usr/bin/openclaw",
    )
    monkeypatch.setattr(guard, "_gateway_is_healthy", lambda *_a, **_kw: (True, ""))

    guard.ensure_openclaw_ready_or_exit(
        yes=True,
        action_label="部署",
        auto_install=True,
        config=SimpleNamespace(openclaw_home_path="/tmp/.openclaw"),
        console=_quiet_console(),
    )


def test_guard_exits_when_gateway_unhealthy_without_auto_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import _openclaw_runtime as guard
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        guard,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=True,
            found_version="OpenClaw 2026.5.18",
            detail="",
            install_hint="",
        ),
    )
    monkeypatch.setattr(
        guard,
        "resolve_openclaw_executable",
        lambda: "/usr/bin/openclaw",
    )
    monkeypatch.setattr(
        guard,
        "_gateway_is_healthy",
        lambda *_a, **_kw: (False, "down"),
    )
    console = _quiet_console()

    with pytest.raises(typer.Exit) as exc:
        guard.ensure_openclaw_ready_or_exit(
            yes=True,
            action_label="升级",
            auto_install=True,
            console=console,
        )
    assert exc.value.exit_code == 1
    output = console.export_text()
    assert "will not auto-start OpenClaw" in output


def test_version_guard_exits_when_openclaw_too_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import _openclaw_runtime as guard
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        guard,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=True,
            found_version="OpenClaw 2026.3.13",
            detail="",
            install_hint="npm install -g openclaw",
        ),
    )

    with pytest.raises(typer.Exit) as exc:
        guard.ensure_openclaw_version_compatible_or_exit(
            action_label="部署",
            console=_quiet_console(),
        )
    assert exc.value.exit_code == 1


def test_version_guard_accepts_compatible_openclaw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import _openclaw_runtime as guard
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        guard,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=True,
            found_version="OpenClaw 2026.5.18",
            detail="",
            install_hint="npm install -g openclaw",
        ),
    )

    guard.ensure_openclaw_version_compatible_or_exit(
        action_label="upgrade",
        console=_quiet_console(),
    )


def test_version_guard_skips_when_openclaw_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import _openclaw_runtime as guard
    from app.cli import deps as deps_mod

    console = _quiet_console()
    monkeypatch.setattr(
        guard,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=False,
            found_version=None,
            detail="missing",
            install_hint="npm install -g openclaw",
        ),
    )

    # Missing OpenClaw should not trigger version checks or user prompts.
    guard.ensure_openclaw_version_compatible_or_exit(
        action_label="upgrade",
        console=console,
    )
    assert console.export_text().strip() == ""
