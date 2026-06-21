"""Phase 9 CLI tests — covers init / doctor / logs verify-anti-loop / ops sub-apps."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
import typer
from typer.testing import CliRunner

from app.cli import app
from app import paths


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _stub_openclaw_runtime_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep phase-9 CLI tests independent from host OpenClaw installation."""
    from app.cli import init as init_mod
    from app.cli import start as start_mod
    from app.cli import upgrade as upgrade_mod

    for mod in (init_mod, start_mod, upgrade_mod):
        for attr in (
            "ensure_openclaw_ready_or_exit",
            "ensure_openclaw_version_compatible_or_exit",
        ):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, lambda **_kw: None)


# ── deps probe surface --------------------------------------------------


def test_deps_run_all_returns_all_known_tools() -> None:
    from app.cli.deps import OPTIONAL, REQUIRED, run_all
    res = run_all()
    expected = set(REQUIRED) | set(OPTIONAL)
    assert set(res) == expected
    for s in res.values():
        assert s.name in expected
        # `ok` and `install_hint` are always set; `found_version` may be None.
        assert isinstance(s.ok, bool)
        assert s.install_hint != ""


def test_deps_python_check_passes_on_311_plus() -> None:
    from app.cli.deps import check_python
    s = check_python()
    # We can only assert the field shape; the actual `ok` depends on the
    # interpreter the tests run in.
    assert s.found_version
    assert "." in s.found_version


def test_deps_check_clawteam_uses_resolved_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], timeout: float = 5.0) -> str | None:
        calls.append(cmd)
        if cmd == ["/opt/runtime/bin/clawteam", "runtime", "--help"]:
            return "usage"
        if cmd == ["/opt/runtime/bin/clawteam", "--version"]:
            return "clawteam 0.3.0"
        return None

    monkeypatch.setattr(
        deps_mod,
        "resolve_binary",
        lambda name: (
            "/opt/runtime/bin/clawteam"
            if name == "clawteam"
            else "/opt/runtime/bin/clawteam-mcp"
            if name == "clawteam-mcp"
            else None
        ),
    )
    monkeypatch.setattr(deps_mod, "_run", _fake_run)
    monkeypatch.setattr(deps_mod, "mcp_sdk_compatible", lambda: True)

    status = deps_mod.check_clawteam()
    assert status.ok is True
    assert calls[:2] == [
        ["/opt/runtime/bin/clawteam", "runtime", "--help"],
        ["/opt/runtime/bin/clawteam", "--version"],
    ]


def test_deps_check_clawteam_passes_when_version_unavailable_but_runtime_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        deps_mod,
        "resolve_binary",
        lambda name: (
            "/opt/runtime/bin/clawteam"
            if name == "clawteam"
            else "/opt/runtime/bin/clawteam-mcp"
            if name == "clawteam-mcp"
            else None
        ),
    )
    monkeypatch.setattr(
        deps_mod,
        "_run",
        lambda cmd, timeout=5.0: (
            "usage" if cmd == ["/opt/runtime/bin/clawteam", "runtime", "--help"] else None
        ),
    )
    monkeypatch.setattr(deps_mod, "mcp_sdk_compatible", lambda: True)

    status = deps_mod.check_clawteam()
    assert status.ok is True
    assert "version output unavailable" in (status.found_version or "")


def test_deps_check_clawteam_fails_on_mcp2_alpha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        deps_mod,
        "resolve_binary",
        lambda name: "/opt/runtime/bin/clawteam" if name == "clawteam" else None,
    )
    monkeypatch.setattr(
        deps_mod,
        "_run",
        lambda cmd, timeout=5.0: (
            "clawteam 0.3.0"
            if cmd == ["/opt/runtime/bin/clawteam", "--version"]
            else "usage"
            if cmd == ["/opt/runtime/bin/clawteam", "runtime", "--help"]
            else None
        ),
    )
    monkeypatch.setattr(deps_mod, "mcp_sdk_compatible", lambda: False)
    monkeypatch.setattr(
        deps_mod,
        "incompatible_mcp_detail",
        lambda: "Incompatible mcp 2.0.0a2 installed",
    )

    status = deps_mod.check_clawteam()
    assert status.ok is False
    assert "Incompatible mcp" in status.detail


def test_deps_check_clawteam_fails_when_clawteam_mcp_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        deps_mod,
        "resolve_binary",
        lambda name: "/opt/runtime/bin/clawteam" if name == "clawteam" else None,
    )
    monkeypatch.setattr(
        deps_mod,
        "_run",
        lambda cmd, timeout=5.0: (
            "clawteam 0.3.0"
            if cmd == ["/opt/runtime/bin/clawteam", "--version"]
            else "usage"
            if cmd == ["/opt/runtime/bin/clawteam", "runtime", "--help"]
            else None
        ),
    )
    monkeypatch.setattr(deps_mod, "mcp_sdk_compatible", lambda: True)

    status = deps_mod.check_clawteam()
    assert status.ok is False
    assert "clawteam-mcp" in status.detail


def test_check_cursor_requires_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    monkeypatch.setattr(
        deps_mod.shutil,
        "which",
        lambda name: "/usr/local/bin/agent" if name == "agent" else None,
    )
    monkeypatch.setattr(deps_mod, "_run", lambda *_a, **_kw: None)

    status = deps_mod.check_cursor()
    assert status.ok is False
    assert "command probe failed" in status.detail
    assert "bootstrap Agent once" in status.detail


def test_check_non_openclaw_tools_cursor_missing_shows_bootstrap_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    monkeypatch.setattr(deps_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        deps_mod,
        "_cursor_bootstrap_command",
        lambda: "cursor agent --help",
    )

    rows = deps_mod.check_non_openclaw_agent_tools()
    cursor_row = next(row for row in rows if row.kind == "cursor")
    assert cursor_row.available is False
    assert "cursor agent --help" in cursor_row.detail
    assert "agent not found in PATH" in cursor_row.detail


def test_install_tool_clawteam_in_venv_avoids_user_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    captured: dict[str, list[str]] = {}

    def _fake_exec(
        cmd: list[str], *, timeout: float = 600.0
    ) -> tuple[bool, str]:
        captured["cmd"] = cmd
        return True, ""

    monkeypatch.setattr(deps_mod, "_running_inside_virtualenv", lambda: True)
    monkeypatch.setattr(
        deps_mod,
        "_clawteam_install_specs",
        lambda: [("pypi", "clawteam")],
    )
    monkeypatch.setattr(deps_mod, "_exec", _fake_exec)
    monkeypatch.setattr(
        deps_mod,
        "check_clawteam",
        lambda: deps_mod.Status(
            name="clawteam",
            ok=True,
            found_version="clawteam 0.3.0",
            detail="",
            install_hint="pip install -U clawteam",
        ),
    )

    result = deps_mod.install_tool("clawteam")
    assert result.ok is True
    assert "--user" not in captured["cmd"]


def test_install_tool_clawteam_falls_back_when_pypi_lacks_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    commands: list[list[str]] = []
    clone_calls: list[list[str]] = []
    checks = iter(
        [
            deps_mod.Status(
                name="clawteam",
                ok=False,
                found_version="clawteam 0.2.0",
                detail="Installed clawteam lacks `runtime` subcommand.",
                install_hint="hint",
            ),
            deps_mod.Status(
                name="clawteam",
                ok=True,
                found_version="clawteam 0.3.0",
                detail="",
                install_hint="hint",
            ),
        ]
    )

    monkeypatch.setattr(deps_mod, "_running_inside_virtualenv", lambda: False)
    monkeypatch.setattr(
        deps_mod,
        "_clawteam_install_specs",
        lambda: [("pypi", "clawteam")],
    )

    def _fake_exec(cmd: list[str], *, timeout: float = 600.0) -> tuple[bool, str]:
        commands.append(cmd)
        return True, ""

    def _fake_clone_install(pip_cmd: list[str]) -> tuple[bool, str]:
        clone_calls.append(list(pip_cmd))
        return True, ""

    monkeypatch.setattr(deps_mod, "_exec", _fake_exec)
    monkeypatch.setattr(deps_mod, "_install_clawteam_from_upstream_clone", _fake_clone_install)
    monkeypatch.setattr(deps_mod, "check_clawteam", lambda: next(checks))

    result = deps_mod.install_tool("clawteam")
    assert result.ok is True
    assert len(commands) == 1
    assert len(clone_calls) == 1
    assert "--user" in commands[0]
    assert commands[0][-1] == "clawteam"
    assert "--user" in clone_calls[0]


def test_install_tool_openclaw_installs_via_npm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    executed: list[list[str]] = []

    def _fake_exec(
        cmd: list[str], *, timeout: float = 600.0
    ) -> tuple[bool, str]:
        executed.append(cmd)
        return True, ""

    monkeypatch.setattr(deps_mod, "_exec", _fake_exec)
    monkeypatch.setattr(
        deps_mod.shutil,
        "which",
        lambda name: "/usr/bin/npm" if name == "npm" else None,
    )
    monkeypatch.setattr(deps_mod.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        deps_mod,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=True,
            found_version="OpenClaw 2026.5.18",
            detail="",
            install_hint="npm install -g openclaw",
        ),
    )

    result = deps_mod.install_tool("openclaw", non_interactive=True)
    assert result.ok is True
    assert executed == [["/usr/bin/npm", "install", "--global", "openclaw"]]


def test_install_tool_hermes_installs_via_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod

    executed: list[list[str]] = []

    def _fake_exec(
        cmd: list[str], *, timeout: float = 600.0
    ) -> tuple[bool, str]:
        executed.append(cmd)
        return True, ""

    monkeypatch.setattr(deps_mod, "_exec", _fake_exec)
    monkeypatch.setattr(deps_mod, "_running_inside_virtualenv", lambda: True)
    monkeypatch.setattr(
        deps_mod,
        "check_hermes",
        lambda: deps_mod.Status(
            name="hermes",
            ok=True,
            found_version="hermes 1.0.0",
            detail="",
            install_hint="pip install hermes-agent",
        ),
    )

    result = deps_mod.install_tool("hermes")
    assert result.ok is True
    assert len(executed) == 1
    assert executed[0][-1] == "hermes-agent"
    assert executed[0][:4] == [deps_mod.sys.executable, "-m", "pip", "install"]


# ── init ---------------------------------------------------------------


def test_init_creates_config_and_skips_openclaw(
    runner: CliRunner, tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "init",
            "--port",
            "17000",
            "--user",
            "tester",
            "--skip-openclaw",
            "--no-restart-service",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Wrote config" in result.stdout
    cfg_path = paths.config_path()
    cfg = json.loads(cfg_path.read_text())
    assert cfg["csflow_port"] == 17000
    assert cfg["default_user"] == "tester"


def test_init_blocks_incompatible_openclaw_when_not_skipped(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import init as init_mod

    monkeypatch.setattr(
        init_mod,
        "ensure_openclaw_version_compatible_or_exit",
        lambda **_kw: (_ for _ in ()).throw(typer.Exit(code=1)),
    )
    result = runner.invoke(app, ["init", "--no-restart-service"])
    assert result.exit_code == 1


def test_init_idempotent(runner: CliRunner) -> None:
    runner.invoke(app, ["init", "--skip-openclaw", "--no-restart-service"])
    result = runner.invoke(
        app,
        ["init", "--skip-openclaw", "--port", "17777", "--no-restart-service"],
    )
    assert result.exit_code == 0
    cfg = json.loads(paths.config_path().read_text())
    assert cfg["csflow_port"] == 17777


def test_init_server_mode_temporarily_disabled(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["init", "--mode", "server", "--pg", "postgres://localhost/x", "--redis", "redis://localhost:6379"],
    )
    assert result.exit_code == 2
    assert not paths.config_path().exists()


def test_install_server_mode_temporarily_disabled(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["install", "--mode", "server", "--pg", "postgres://localhost/x", "--redis", "redis://localhost:6379"],
    )
    assert result.exit_code == 2
    assert not paths.config_path().exists()


def test_install_alias_delegates_to_unified_upgrade_pipeline(
    runner: CliRunner,
) -> None:
    first = runner.invoke(app, ["install", "--skip-openclaw", "--no-restart-service"])
    assert first.exit_code == 0, first.stdout
    second = runner.invoke(
        app, ["install", "--skip-openclaw", "--no-restart-service"]
    )
    assert second.exit_code == 0, second.stdout
    assert "delegates to the unified upgrade pipeline" in second.stdout
    assert "Upgrade report" in second.stdout


def test_first_install_does_not_enter_upgrade_pipeline(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import upgrade as upgrade_mod

    def _should_not_be_called(*_a, **_kw):
        raise AssertionError("first-time install should not call run_upgrade")

    monkeypatch.setattr(upgrade_mod, "run_upgrade", _should_not_be_called)
    result = runner.invoke(
        app, ["install", "--skip-openclaw", "--no-restart-service"]
    )
    assert result.exit_code == 0, result.stdout
    assert "schema ready" in result.stdout
    # First-time install only prepares empty layout; it must not seed built-in agents.
    assert paths.agents_dir().exists()
    assert not any(paths.agents_dir().iterdir())


def test_start_first_boot_forces_local_mode(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import start as start_mod

    captured: dict[str, object] = {}

    def _fake_init(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(start_mod, "do_init", _fake_init)
    monkeypatch.setattr(start_mod, "restart_and_enable", lambda **_kw: None)
    monkeypatch.setattr(
        start_mod.cfg_mod,
        "load_config",
        lambda: SimpleNamespace(csflow_port=17017),
    )

    result = runner.invoke(app, ["start", "--skip-deps", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert captured["mode"] == "local"


def test_start_existing_config_always_runs_safe_redeploy(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import start as start_mod
    from app import upgrade as upgrade_mod

    cfg_path = paths.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        start_mod.cfg_mod,
        "load_config",
        lambda: SimpleNamespace(csflow_port=17017),
    )
    monkeypatch.setattr(start_mod, "restart_and_enable", lambda **_kw: None)
    monkeypatch.setattr(start_mod, "do_init", lambda **_kw: None)
    monkeypatch.setattr(upgrade_mod, "needs_upgrade", lambda: (False, "1.0.0"))

    captured: dict[str, object] = {}

    def _fake_run_upgrade(*, include_frontend_build: bool = False, **_kw):
        captured["include_frontend_build"] = include_frontend_build
        return SimpleNamespace(ok=True, to_version="1.0.0", errors=[], repair_warnings=[])

    monkeypatch.setattr(upgrade_mod, "run_upgrade", _fake_run_upgrade)

    result = runner.invoke(app, ["start", "--skip-deps", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert captured["include_frontend_build"] is False
    assert "Safe redeploy finished" in result.stdout


def test_start_auto_installs_missing_required_deps_in_yes_mode(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod
    from app.cli import start as start_mod

    states = [
        {
            "python": deps_mod.Status("python", True, "3.11.9", "", "hint"),
            "git": deps_mod.Status("git", True, "2.42.0", "", "hint"),
            "tmux": deps_mod.Status("tmux", True, "3.4", "", "hint"),
            "clawteam": deps_mod.Status(
                "clawteam",
                False,
                None,
                "missing runtime",
                "pip install -U clawteam",
            ),
            "node": deps_mod.Status("node", True, "v22.12.0", "", "hint"),
            "openclaw": deps_mod.Status("openclaw", True, "0.1.0", "", "hint"),
            "hermes": deps_mod.Status("hermes", True, "1.0.0", "", "hint"),
            "cursor": deps_mod.Status("cursor", True, "agent 0.9.0", "", "hint"),
        },
        {
            "python": deps_mod.Status("python", True, "3.11.9", "", "hint"),
            "git": deps_mod.Status("git", True, "2.42.0", "", "hint"),
            "tmux": deps_mod.Status("tmux", True, "3.4", "", "hint"),
            "clawteam": deps_mod.Status("clawteam", True, "0.2.0", "", "hint"),
            "node": deps_mod.Status("node", True, "v22.12.0", "", "hint"),
            "openclaw": deps_mod.Status("openclaw", True, "0.1.0", "", "hint"),
            "hermes": deps_mod.Status("hermes", True, "1.0.0", "", "hint"),
            "cursor": deps_mod.Status("cursor", True, "agent 0.9.0", "", "hint"),
        },
    ]
    call_idx = {"n": 0}

    def _fake_run_all():
        i = call_idx["n"]
        call_idx["n"] += 1
        return states[min(i, len(states) - 1)]

    installed: list[tuple[str, bool]] = []

    def _fake_install(name: str, *, non_interactive: bool = False):
        installed.append((name, non_interactive))
        return deps_mod.InstallResult(name=name, ok=True, detail="")

    monkeypatch.setattr(start_mod, "run_all", _fake_run_all)
    monkeypatch.setattr(start_mod, "install_tool", _fake_install)
    monkeypatch.setattr(start_mod, "do_init", lambda **_kw: None)
    monkeypatch.setattr(start_mod, "restart_and_enable", lambda **_kw: None)
    monkeypatch.setattr(
        start_mod.cfg_mod,
        "load_config",
        lambda: SimpleNamespace(csflow_port=17017),
    )

    result = runner.invoke(app, ["start", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert installed == [("clawteam", True)]


def test_start_yes_mode_exits_when_required_deps_still_missing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod
    from app.cli import start as start_mod

    broken = {
        "python": deps_mod.Status("python", True, "3.11.9", "", "hint"),
        "git": deps_mod.Status("git", True, "2.42.0", "", "hint"),
        "tmux": deps_mod.Status("tmux", True, "3.4", "", "hint"),
        "clawteam": deps_mod.Status(
            "clawteam",
            False,
            None,
            "missing runtime",
            "pip install -U clawteam",
        ),
        "node": deps_mod.Status("node", True, "v22.12.0", "", "hint"),
        "openclaw": deps_mod.Status("openclaw", True, "0.1.0", "", "hint"),
        "hermes": deps_mod.Status("hermes", True, "1.0.0", "", "hint"),
        "cursor": deps_mod.Status("cursor", True, "agent 0.9.0", "", "hint"),
    }

    monkeypatch.setattr(start_mod, "run_all", lambda: broken)
    monkeypatch.setattr(
        start_mod,
        "install_tool",
        lambda name, *, non_interactive=False: deps_mod.InstallResult(
            name=name,
            ok=False,
            detail="forced failure",
        ),
    )
    monkeypatch.setattr(start_mod, "do_init", lambda **_kw: None)
    monkeypatch.setattr(start_mod, "restart_and_enable", lambda **_kw: None)
    monkeypatch.setattr(
        start_mod.cfg_mod,
        "load_config",
        lambda: SimpleNamespace(csflow_port=17017),
    )

    result = runner.invoke(app, ["start", "--yes"])
    assert result.exit_code == 1
    assert "Still missing required dependencies" in result.stdout


def test_start_prints_openclaw_hint_and_non_openclaw_tool_summary(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import deps as deps_mod
    from app.cli import start as start_mod

    monkeypatch.setattr(start_mod, "do_init", lambda **_kw: None)
    monkeypatch.setattr(start_mod, "restart_and_enable", lambda **_kw: None)
    monkeypatch.setattr(
        start_mod,
        "service_status_hint",
        lambda: "systemctl --user status csflow",
    )
    monkeypatch.setattr(
        start_mod.cfg_mod,
        "load_config",
        lambda: SimpleNamespace(csflow_port=17017),
    )
    monkeypatch.setattr(
        start_mod,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=False,
            found_version=None,
            detail="missing",
            install_hint="OpenClaw is optional. Install manually only if you need OpenClaw agents.",
        ),
    )
    monkeypatch.setattr(
        start_mod,
        "check_non_openclaw_agent_tools",
        lambda: [
            deps_mod.AgentToolStatus(
                kind="claude",
                label="Claude Code",
                binary="claude",
                available=True,
                found_version="claude 1.2.3",
                detail="/usr/bin/claude",
            ),
            deps_mod.AgentToolStatus(
                kind="codex",
                label="Codex",
                binary="codex",
                available=False,
                found_version=None,
                detail="codex not found in PATH",
            ),
        ],
    )

    result = runner.invoke(app, ["start", "--skip-deps", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "OpenClaw is not installed" in result.stdout
    assert "auto-install is not performed" in result.stdout
    assert "Agent Runtime Check" in result.stdout
    assert "Currently available agents:" in result.stdout
    assert "Claude Code" in result.stdout
    assert "Also supported (setup required):" in result.stdout
    assert "Codex" in result.stdout
    assert result.stdout.rstrip().endswith(
        f"Version: {start_mod.__version__}"
    )


def test_start_platform_summary_puts_openclaw_first_when_available(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import upgrade as upgrade_mod
    from app.cli import deps as deps_mod
    from app.cli import start as start_mod

    cfg_path = paths.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(start_mod, "do_init", lambda **_kw: None)
    monkeypatch.setattr(start_mod, "restart_and_enable", lambda **_kw: None)
    monkeypatch.setattr(
        start_mod,
        "service_status_hint",
        lambda: "systemctl --user status csflow",
    )
    monkeypatch.setattr(
        start_mod.cfg_mod,
        "load_config",
        lambda: SimpleNamespace(csflow_port=17017),
    )
    monkeypatch.setattr(upgrade_mod, "needs_upgrade", lambda: (False, "1.0.0"))
    monkeypatch.setattr(
        upgrade_mod,
        "run_upgrade",
        lambda **_kw: SimpleNamespace(ok=True, to_version="1.0.0", errors=[], repair_warnings=[]),
    )
    monkeypatch.setattr(
        start_mod,
        "check_openclaw",
        lambda: deps_mod.Status(
            name="openclaw",
            ok=True,
            found_version="openclaw 0.1.0",
            detail="",
            install_hint="",
        ),
    )
    monkeypatch.setattr(
        start_mod,
        "check_non_openclaw_agent_tools",
        lambda: [
            deps_mod.AgentToolStatus(
                kind="claude",
                label="Claude Code",
                binary="claude",
                available=True,
                found_version="claude 1.2.3",
                detail="/usr/bin/claude",
            ),
            deps_mod.AgentToolStatus(
                kind="codex",
                label="Codex",
                binary="codex",
                available=False,
                found_version=None,
                detail="codex not found in PATH",
            ),
        ],
    )

    result = runner.invoke(app, ["start", "--skip-deps", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert "Agent Runtime Check" in result.stdout
    assert "OpenClaw" in result.stdout
    assert "Claude Code" in result.stdout
    assert result.stdout.index("OpenClaw") < result.stdout.index("Claude Code")
    assert "Currently available agents: OpenClaw, Claude Code" in result.stdout


def test_start_hides_structured_upgrade_logs_from_terminal(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import upgrade as upgrade_mod
    from app.cli import start as start_mod
    from app.logging_setup import get_logger

    cfg_path = paths.config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(start_mod, "do_init", lambda **_kw: None)
    monkeypatch.setattr(start_mod, "restart_and_enable", lambda **_kw: None)
    monkeypatch.setattr(
        start_mod,
        "service_status_hint",
        lambda: "systemctl --user status csflow",
    )
    monkeypatch.setattr(start_mod, "_render_agent_runtime_summary", lambda: None)
    monkeypatch.setattr(
        start_mod.cfg_mod,
        "load_config",
        lambda: SimpleNamespace(csflow_port=17017),
    )
    monkeypatch.setattr(upgrade_mod, "needs_upgrade", lambda: (False, "1.0.0"))

    def _fake_run_upgrade(**_kw):
        get_logger("upgrade").info(
            "upgrade_start",
            from_version="0.1.0",
            to_version="0.1.1",
            is_first_install=False,
        )
        return SimpleNamespace(ok=True, to_version="1.0.1", errors=[], repair_warnings=[])

    monkeypatch.setattr(upgrade_mod, "run_upgrade", _fake_run_upgrade)

    result = runner.invoke(app, ["start", "--skip-deps", "--yes"])
    assert result.exit_code == 0, result.stdout
    assert '"event": "upgrade_start"' not in result.stdout
    assert "upgrade_start" not in result.stdout


# ── upgrade ------------------------------------------------------------


def _ok_upgrade_report() -> SimpleNamespace:
    return SimpleNamespace(
        ok=True,
        from_version="0.1.0",
        to_version="0.1.1",
        migrations_run=[],
        frontend_build_status="skipped-no-dist",
        frontend_build_detail="",
        schema_ready=True,
        skills_reseeded=True,
        openclaw_status="skipped-no-openclaw-home",
        redeploy_performed=True,
        user_agent_skill_results={},
        user_agent_cron_sync_results={},
        marker_written=True,
        errors=[],
        repair_warnings=[],
    )


def test_upgrade_delegates_to_hosted_stable_script(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import upgrade as upgrade_mod

    called = {"n": 0}

    def _fake_hosted() -> int:
        called["n"] += 1
        return 0

    monkeypatch.setattr(upgrade_mod, "_run_hosted_stable_upgrader", _fake_hosted)

    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 0, result.stdout
    assert called["n"] == 1
    assert "upgrading to latest stable release via hosted script" in result.stdout


def test_upgrade_runtime_defaults_to_yes_without_prompt(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod.paths, "clawsomeflow_home_exists", lambda: True)
    monkeypatch.setattr(upgrade_mod.upgrade, "needs_upgrade", lambda: (True, "0.1.0"))
    monkeypatch.setattr(upgrade_mod.upgrade, "run_upgrade", lambda **_kw: _ok_upgrade_report())
    monkeypatch.setattr(upgrade_mod, "stop_if_running", lambda: False)
    monkeypatch.setattr(upgrade_mod, "read_pid", lambda: None)
    monkeypatch.setattr(upgrade_mod, "render_agent_platform_summary", lambda **_kw: None)

    def _confirm_unexpected(*_a, **_kw):
        raise AssertionError("typer.confirm should not be called by default")

    monkeypatch.setattr(upgrade_mod.typer, "confirm", _confirm_unexpected)

    result = runner.invoke(app, ["upgrade-runtime", "--no-restart-service"])
    assert result.exit_code == 0, result.stdout
    assert "Upgraded to" in result.stdout


def test_upgrade_runtime_no_yes_requires_confirmation(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod.paths, "clawsomeflow_home_exists", lambda: True)
    monkeypatch.setattr(upgrade_mod.upgrade, "needs_upgrade", lambda: (True, "0.1.0"))

    def _run_upgrade_unexpected(**_kw):
        raise AssertionError("run_upgrade should not run when confirmation is rejected")

    monkeypatch.setattr(upgrade_mod.upgrade, "run_upgrade", _run_upgrade_unexpected)
    monkeypatch.setattr(upgrade_mod, "stop_if_running", lambda: False)
    monkeypatch.setattr(upgrade_mod, "read_pid", lambda: None)
    monkeypatch.setattr(upgrade_mod.typer, "confirm", lambda *_a, **_kw: False)

    result = runner.invoke(app, ["upgrade-runtime", "--no-yes", "--no-restart-service"])
    assert result.exit_code == 0, result.stdout
    assert "Upgraded to" not in result.stdout


def test_upgrade_runtime_blocks_incompatible_openclaw(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import upgrade as upgrade_mod

    monkeypatch.setattr(upgrade_mod.paths, "clawsomeflow_home_exists", lambda: True)
    monkeypatch.setattr(upgrade_mod.upgrade, "needs_upgrade", lambda: (True, "0.1.0"))
    monkeypatch.setattr(
        upgrade_mod,
        "ensure_openclaw_version_compatible_or_exit",
        lambda **_kw: (_ for _ in ()).throw(typer.Exit(code=1)),
    )

    result = runner.invoke(app, ["upgrade-runtime", "--no-restart-service"])
    assert result.exit_code == 1


# ── status -------------------------------------------------------------


def test_status_runs_and_includes_pid_state(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "running" in result.stdout
    # No PID file at startup → "no" branch.
    assert "no" in result.stdout


# ── stop with no PID file ---------------------------------------------


def test_stop_no_pid_returns_zero(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.cli import stop as stop_mod

    # Keep the port-reclaim fallback from probing the real host port.
    monkeypatch.setattr(stop_mod, "reclaim_stale_port_listeners", lambda _port: [])
    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "nothing to stop" in out or "stopped managed user service" in out


def test_stop_reclaims_orphaned_dev_listener_without_pid(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No managed service + no PID file, but an orphaned dev uvicorn on the port
    (left by `deploy.sh source`) is reclaimed as a final safety net."""
    from app.cli import stop as stop_mod

    monkeypatch.setattr(stop_mod, "stop_if_running", lambda: False)
    monkeypatch.setattr(stop_mod, "read_pid", lambda: None)
    monkeypatch.setattr(stop_mod, "reclaim_stale_port_listeners", lambda _port: [12345])

    result = runner.invoke(app, ["stop"])
    assert result.exit_code == 0
    out = result.stdout.lower()
    assert "reclaimed" in out
    assert "12345" in result.stdout
    assert "nothing to stop" not in out


# ── doctor (does not require a running backend) -----------------------


def test_doctor_dry_runs_without_clawteam(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor exit code reflects fatal_missing — but it never raises."""
    # Force-fail clawteam check so we exercise the missing branch.
    from app.cli import deps as deps_mod
    def _missing():
        return deps_mod.Status(
            name="clawteam", ok=False, found_version=None,
            detail="forced for test", install_hint="pip install clawteam",
        )
    monkeypatch.setattr(deps_mod, "check_clawteam", _missing)
    monkeypatch.setitem(deps_mod._CHECKS, "clawteam", _missing)

    result = runner.invoke(app, ["doctor"])
    assert "ClawsomeFlow — dependency check" in result.stdout
    assert "clawteam" in result.stdout
    # We forced clawteam missing → required → exit 1.
    assert result.exit_code == 1


# ── logs verify-anti-loop ---------------------------------------------


def test_verify_anti_loop_no_log_file(runner: CliRunner) -> None:
    result = runner.invoke(app, ["logs", "verify-anti-loop"])
    assert result.exit_code == 0
    assert "No log file" in result.stdout


def test_verify_anti_loop_passes_on_clean_logs(
    runner: CliRunner, tmp_path: Path,
) -> None:
    log = tmp_path / "csflow-clean.jsonl"
    log.write_text(
        "\n".join([
            json.dumps({
                "event": "spawn_cmd_built",
                "cmd_argv": [
                    "clawteam", "spawn", "tmux", "claude", "--no-keepalive",
                    "--repo", "/tmp/r", "--workspace",
                ],
                "keepalive": False,
            }),
            json.dumps({"event": "spawn_cmd_executed", "exit_code": 0}),
        ])
        + "\n"
    )
    result = runner.invoke(app, ["logs", "verify-anti-loop", "--file", str(log)])
    assert result.exit_code == 0
    assert "All 1 spawn events comply" in result.stdout


def test_verify_anti_loop_detects_task_violation(
    runner: CliRunner, tmp_path: Path,
) -> None:
    log = tmp_path / "csflow-bad.jsonl"
    log.write_text(json.dumps({
        "event": "spawn_cmd_built",
        "cmd_argv": [
            "clawteam", "spawn", "tmux", "claude", "--no-keepalive",
            "--repo", "/tmp/r", "--workspace", "--task", "do x",
        ],
        "keepalive": False,
    }) + "\n")
    result = runner.invoke(app, ["logs", "verify-anti-loop", "--file", str(log)])
    assert result.exit_code == 1
    assert "--task present" in result.stdout


def test_verify_anti_loop_detects_skill_violation(
    runner: CliRunner, tmp_path: Path,
) -> None:
    log = tmp_path / "csflow-bad-skill.jsonl"
    log.write_text(json.dumps({
        "event": "spawn_cmd_built",
        "cmd_argv": [
            "clawteam", "spawn", "tmux", "claude", "--no-keepalive",
            "--skill", "clawteam",
        ],
        "keepalive": False,
    }) + "\n")
    result = runner.invoke(app, ["logs", "verify-anti-loop", "--file", str(log)])
    assert result.exit_code == 1
    assert "--skill clawteam" in result.stdout


def test_verify_anti_loop_detects_keepalive_violation(
    runner: CliRunner, tmp_path: Path,
) -> None:
    log = tmp_path / "csflow-bad-ka.jsonl"
    log.write_text(json.dumps({
        "event": "spawn_cmd_built",
        "cmd_argv": ["clawteam", "spawn", "tmux", "claude"],  # no --no-keepalive
        "keepalive": True,
    }) + "\n")
    result = runner.invoke(app, ["logs", "verify-anti-loop", "--file", str(log)])
    assert result.exit_code == 1
    assert "missing --no-keepalive" in result.stdout


# ── logs tail ---------------------------------------------------------


def test_logs_tail_handles_missing_file(runner: CliRunner) -> None:
    result = runner.invoke(app, ["logs", "tail", "-n", "5"])
    assert result.exit_code == 0


def test_logs_tail_renders_entries(runner: CliRunner, tmp_path: Path) -> None:
    log = tmp_path / "csflow-tail.jsonl"
    log.write_text(json.dumps({
        "ts": "2026-05-07T10:00:00+00:00",
        "level": "info",
        "event": "task_dispatched",
        "task_id": "t1",
    }) + "\n")
    result = runner.invoke(app, ["logs", "tail", "--file", str(log)])
    assert result.exit_code == 0
    assert "task_dispatched" in result.stdout


# ── logs export -------------------------------------------------------


def test_logs_export_to_zip_file(runner: CliRunner, tmp_path: Path) -> None:
    logs_dir = paths.logs_dir()
    (logs_dir / "csflow-20260507.jsonl").write_text(
        json.dumps({"event": "task_dispatched", "task_id": "t1"}) + "\n"
    )
    (logs_dir / "clawteam-board.log").write_text("board log\n")
    run_dir = paths.runs_dir() / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "task_dispatched", "taskId": "t1"}) + "\n"
    )
    (run_dir / "meta.json").write_text(json.dumps({"id": "run_001"}))
    (paths.flows_dir() / "flow_001.json").write_text(json.dumps({"id": "flow_001"}))
    paths.config_path().write_text(json.dumps({"deployment_mode": "local"}))
    paths.db_path().write_text("sqlite-bytes")
    paths.version_marker_path().write_text("1.2.3\n")
    (paths.system_dir() / "openclaw-managed-agents.json").write_text(
        json.dumps({"agent_ids": ["leader"]})
    )

    out_zip = tmp_path / "support-bundle.zip"
    result = runner.invoke(app, ["logs", "export", str(out_zip)])
    assert result.exit_code == 0, result.stdout
    assert out_zip.exists()

    with ZipFile(out_zip) as zf:
        names = set(zf.namelist())
        assert ".logs/csflow-20260507.jsonl" in names
        assert ".logs/clawteam-board.log" in names
        assert ".runs/run_001/events.jsonl" in names
        assert ".runs/run_001/meta.json" in names
        assert ".flows/flow_001.json" in names
        assert "config.json" in names
        assert "db.sqlite" in names
        assert ".csflow-version" in names
        assert ".system/openclaw-managed-agents.json" in names
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["categories"]["logs"] >= 1
        assert manifest["categories"]["runs"] >= 1


def test_logs_export_to_directory_auto_names_bundle(
    runner: CliRunner, tmp_path: Path,
) -> None:
    logs_dir = paths.logs_dir()
    (logs_dir / "csflow-20260508.jsonl").write_text(
        json.dumps({"event": "spawn_cmd_built"}) + "\n"
    )

    target_dir = tmp_path / "exports"
    result = runner.invoke(app, ["logs", "export", str(target_dir)])
    assert result.exit_code == 0, result.stdout
    bundles = list(target_dir.glob("csflow-logs-*.zip"))
    assert len(bundles) == 1

    with ZipFile(bundles[0]) as zf:
        names = set(zf.namelist())
        assert ".logs/csflow-20260508.jsonl" in names


def test_logs_export_handles_no_diagnostic_data(
    runner: CliRunner, tmp_path: Path,
) -> None:
    out_zip = tmp_path / "no-logs.zip"
    result = runner.invoke(app, ["logs", "export", str(out_zip)])
    assert result.exit_code == 0
    assert "No diagnostic files under" in result.stdout
    assert not out_zip.exists()


def test_logs_export_refuses_overwrite_without_flag(
    runner: CliRunner, tmp_path: Path,
) -> None:
    logs_dir = paths.logs_dir()
    (logs_dir / "csflow-20260509.jsonl").write_text(
        json.dumps({"event": "x"}) + "\n"
    )
    out_zip = tmp_path / "exists.zip"
    out_zip.write_text("old")

    result = runner.invoke(app, ["logs", "export", str(out_zip)])
    assert result.exit_code == 1
    assert "Target already exists" in result.stdout

# ── static asset discovery --------------------------------------------


def test_discover_frontend_dist_finds_repo_dist() -> None:
    """If the repo has frontend/dist (built in Phase 8), discovery finds it."""
    from app.static import discover_frontend_dist
    p = discover_frontend_dist()
    # In CI the dist may not exist; just assert the function tolerates it.
    if p is not None:
        assert (p / "index.html").exists()


def test_static_override_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = tmp_path / "fake-dist"
    fake.mkdir()
    (fake / "index.html").write_text("ok")
    (fake / "assets").mkdir()
    monkeypatch.setenv("CSFLOW_FRONTEND_DIST", str(fake))
    from app.static import discover_frontend_dist
    assert discover_frontend_dist() == fake


# ── board proxy --------------------------------------------------------


def test_board_proxy_skipped_in_server_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.board_proxy import BoardProxyManager
    from app.config import Config
    cfg = Config(deployment_mode="local")  # use base Config first then override
    cfg = cfg.model_copy(update={
        "deployment_mode": "server",
        "broker": {"kind": "redis", "url": "redis://localhost:6379"},
        "storage": {"kind": "postgres", "url": "postgres://localhost/x"},
    })
    mgr = BoardProxyManager(config=cfg)
    assert mgr.start() is False  # server mode → no spawn


def test_board_proxy_handles_missing_clawteam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If clawteam binary not on PATH, start() returns False without raising."""
    from app.board_proxy import BoardProxyManager
    mgr = BoardProxyManager()
    monkeypatch.setattr("app.board_proxy.resolve_binary", lambda _name: None)
    assert mgr.start() is False
    assert mgr.is_running() is False
    assert "not found" in (mgr.last_error or "")


def test_board_proxy_retries_and_recovers_after_transient_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.board_proxy import BoardProxyManager

    mgr = BoardProxyManager()
    monkeypatch.setattr("app.board_proxy.resolve_binary", lambda _name: "/usr/bin/clawteam")
    monkeypatch.setattr(mgr, "_verify_board_subcommand", lambda: True)
    monkeypatch.setattr(mgr, "_reuse_existing_listener_if_possible", lambda: False)
    monkeypatch.setattr(mgr, "_kill_current_proc", lambda: None)
    monkeypatch.setattr(mgr, "_startup_failure_detail", lambda: "first-attempt-failed")
    monkeypatch.setattr(mgr, "_close_log_handle", lambda: None)

    spawn_calls = {"n": 0}

    def _fake_spawn() -> bool:
        spawn_calls["n"] += 1
        mgr._proc = SimpleNamespace(pid=12345, poll=lambda: None)  # type: ignore[assignment]
        return True

    monkeypatch.setattr(mgr, "_spawn_board_once", _fake_spawn)

    wait_calls = {"n": 0}

    def _fake_wait(*, timeout_seconds: float) -> bool:
        wait_calls["n"] += 1
        return wait_calls["n"] > 1

    monkeypatch.setattr(mgr, "_wait_until_ready", _fake_wait)

    assert mgr.start() is True
    assert spawn_calls["n"] == 2
    assert wait_calls["n"] == 2


def test_board_proxy_reuses_existing_user_managed_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.board_proxy import BoardProxyManager

    mgr = BoardProxyManager()
    monkeypatch.setattr("app.board_proxy.resolve_binary", lambda _name: "/usr/bin/clawteam")
    monkeypatch.setattr(mgr, "_verify_board_subcommand", lambda: True)
    monkeypatch.setattr(mgr, "_binary_version", lambda _exe: "clawteam 0.3.0")
    monkeypatch.setattr(mgr, "_pid_executable", lambda _pid: "/usr/bin/clawteam")
    monkeypatch.setattr(mgr, "_listening_pids_for_port", lambda: [4321])
    monkeypatch.setattr(
        mgr,
        "_pid_cmdline",
        lambda _pid: "clawteam board serve --port 17018 --host 127.0.0.1",
    )
    monkeypatch.setattr(
        mgr,
        "_spawn_board_once",
        lambda: (_ for _ in ()).throw(AssertionError("should not spawn when reusing")),
    )

    assert mgr.start() is True
    assert mgr.is_running() is False
    assert mgr.last_error in (None, "")


def test_board_proxy_refuses_to_replace_non_clawteam_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.board_proxy import BoardProxyManager

    mgr = BoardProxyManager()
    monkeypatch.setattr("app.board_proxy.resolve_binary", lambda _name: "/usr/bin/clawteam")
    monkeypatch.setattr(mgr, "_verify_board_subcommand", lambda: True)
    monkeypatch.setattr(mgr, "_listening_pids_for_port", lambda: [5566])
    monkeypatch.setattr(mgr, "_pid_cmdline", lambda _pid: "python3 -m http.server 17018")
    monkeypatch.setattr(
        mgr,
        "_spawn_board_once",
        lambda: (_ for _ in ()).throw(AssertionError("must not replace external listener")),
    )

    assert mgr.start() is False
    assert "non-clawteam-board" in (mgr.last_error or "")


def test_board_proxy_replaces_existing_listener_when_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.board_proxy import BoardProxyManager

    mgr = BoardProxyManager()
    monkeypatch.setattr("app.board_proxy.resolve_binary", lambda _name: "/opt/new/clawteam")
    monkeypatch.setattr(mgr, "_verify_board_subcommand", lambda: True)

    listener_calls = {"n": 0}

    def _listeners() -> list[int]:
        listener_calls["n"] += 1
        return [6011] if listener_calls["n"] == 1 else []

    monkeypatch.setattr(mgr, "_listening_pids_for_port", _listeners)
    monkeypatch.setattr(
        mgr,
        "_pid_cmdline",
        lambda _pid: "clawteam board serve --port 17018 --host 127.0.0.1",
    )
    monkeypatch.setattr(mgr, "_pid_executable", lambda _pid: "/opt/old/clawteam")
    monkeypatch.setattr(
        mgr,
        "_binary_version",
        lambda exe: "clawteam 0.3.0" if exe and "new" in exe else "clawteam 0.2.0",
    )
    monkeypatch.setattr(mgr, "_pid_owned_by_current_user", lambda _pid: True)
    terminated: list[int] = []
    monkeypatch.setattr(
        mgr,
        "_terminate_pid",
        lambda pid: (terminated.append(pid), True)[1],
    )

    spawn_calls = {"n": 0}

    def _fake_spawn() -> bool:
        spawn_calls["n"] += 1
        mgr._proc = SimpleNamespace(pid=3333, poll=lambda: None)  # type: ignore[assignment]
        return True

    monkeypatch.setattr(mgr, "_spawn_board_once", _fake_spawn)
    monkeypatch.setattr(mgr, "_wait_until_ready", lambda *, timeout_seconds: True)

    assert mgr.start() is True
    assert terminated == [6011]
    assert spawn_calls["n"] == 1


def test_board_proxy_version_mismatch_not_owned_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.board_proxy import BoardProxyManager

    mgr = BoardProxyManager()
    monkeypatch.setattr("app.board_proxy.resolve_binary", lambda _name: "/opt/new/clawteam")
    monkeypatch.setattr(mgr, "_verify_board_subcommand", lambda: True)
    monkeypatch.setattr(mgr, "_listening_pids_for_port", lambda: [7788])
    monkeypatch.setattr(
        mgr,
        "_pid_cmdline",
        lambda _pid: "clawteam board serve --port 17018 --host 127.0.0.1",
    )
    monkeypatch.setattr(mgr, "_pid_executable", lambda _pid: "/opt/old/clawteam")
    monkeypatch.setattr(
        mgr,
        "_binary_version",
        lambda exe: "clawteam 0.3.0" if exe and "new" in exe else "clawteam 0.2.0",
    )
    monkeypatch.setattr(mgr, "_pid_owned_by_current_user", lambda _pid: False)
    monkeypatch.setattr(
        mgr,
        "_spawn_board_once",
        lambda: (_ for _ in ()).throw(AssertionError("should not spawn on ownership conflict")),
    )

    assert mgr.start() is False
    assert "cannot auto-upgrade" in (mgr.last_error or "")
