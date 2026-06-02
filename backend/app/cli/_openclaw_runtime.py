"""OpenClaw runtime guard for deploy/upgrade entrypoints.

Policy:
1) OpenClaw CLI must exist before deploy/upgrade proceeds.
2) If missing, user can choose auto-install (`npm install -g openclaw`).
3) OpenClaw gateway must already be healthy; do not auto-start it.
4) Any failure above aborts deploy/upgrade.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

import typer
from rich.console import Console

from app.cli.deps import check_openclaw, install_tool
from app.config import Config
from app.integrations.openclaw_cli import resolve_openclaw_executable

_HEALTH_TIMEOUT_SEC = 15.0
MIN_OPENCLAW_VERSION = "2026.5.12"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _trim(text: str, *, limit: int = 480) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    return raw if len(raw) <= limit else raw[:limit] + "..."


def _parse_version_triplet(raw: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(raw or "")
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _format_version_triplet(version: tuple[int, int, int]) -> str:
    return f"{version[0]}.{version[1]}.{version[2]}"


def _openclaw_env(config: Config | None) -> dict[str, str]:
    env = os.environ.copy()
    if config is None:
        return env
    home = config.openclaw_home_path
    env["OPENCLAW_STATE_DIR"] = str(home)
    env["OPENCLAW_CONFIG_PATH"] = str(home / "openclaw.json")
    return env


def _run_openclaw(
    executable: str,
    args: list[str],
    *,
    config: Config | None,
    timeout: float,
) -> tuple[bool, str]:
    argv = [executable, *args]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=_openclaw_env(config),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, output


def _parse_json_from_output(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    first_obj = text.find("{")
    if first_obj < 0:
        return None
    try:
        parsed = json.loads(text[first_obj:])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _gateway_is_healthy(
    executable: str,
    *,
    config: Config | None,
) -> tuple[bool, str]:
    ok, out = _run_openclaw(
        executable,
        ["health", "--json"],
        config=config,
        timeout=_HEALTH_TIMEOUT_SEC,
    )
    if not ok:
        return False, _trim(out) or "openclaw health check failed"
    payload = _parse_json_from_output(out)
    if payload is not None and payload.get("ok") is not True:
        detail = payload.get("error") or payload.get("message") or out
        return False, _trim(str(detail)) or "openclaw health returned ok=false"
    return True, ""


def ensure_openclaw_version_compatible_or_exit(
    *,
    action_label: str,
    minimum_version: str = MIN_OPENCLAW_VERSION,
    console: Console | None = None,
) -> None:
    """Enforce minimum OpenClaw CLI version when OpenClaw is installed."""
    out = console or Console()
    status = check_openclaw()
    if not status.ok:
        # OpenClaw is optional at deploy/upgrade time. Missing runtime is handled elsewhere.
        return

    detected_raw = (status.found_version or "").strip()
    detected = _parse_version_triplet(detected_raw)
    minimum = _parse_version_triplet(minimum_version)
    if minimum is None:
        raise RuntimeError(f"invalid minimum OpenClaw version: {minimum_version!r}")

    if detected is None:
        out.print(
            f"[red]✗ {action_label} failed: unable to parse installed OpenClaw version.[/red]"
        )
        out.print(
            f"[yellow]Detected value:[/yellow] {_trim(detected_raw) or '(empty output)'}"
        )
        out.print(
            f"[yellow]ClawsomeFlow requires OpenClaw ≥ {minimum_version}. "
            "Please upgrade OpenClaw and retry.[/yellow]"
        )
        out.print("[dim]Suggested command: npm install -g openclaw@latest[/dim]")
        raise typer.Exit(code=1)

    if detected < minimum:
        out.print(
            f"[red]✗ {action_label} failed: OpenClaw {_format_version_triplet(detected)} "
            f"is below required minimum {minimum_version}.[/red]"
        )
        out.print(
            "[yellow]This ClawsomeFlow build depends on newer OpenClaw interfaces: "
            "`heartbeat.isolatedSession`, `heartbeat.includeSystemPromptSection`, "
            "and `openclaw cron list --agent`.[/yellow]"
        )
        out.print("[dim]Please run: npm install -g openclaw@latest[/dim]")
        raise typer.Exit(code=1)


def ensure_openclaw_ready_or_exit(
    *,
    yes: bool,
    action_label: str,
    config: Config | None = None,
    auto_install: bool = True,
    console: Console | None = None,
) -> None:
    """Enforce OpenClaw CLI + gateway readiness; exits on any failure."""
    out = console or Console()
    status = check_openclaw()
    if not status.ok:
        out.print(f"[red]✗ {action_label} failed: OpenClaw is not installed on this system.[/red]")
        out.print("[yellow]Install OpenClaw first, or allow ClawsomeFlow to install it automatically.[/yellow]")
        if not auto_install:
            raise typer.Exit(code=1)
        install_now = yes or typer.confirm(
            "Install OpenClaw automatically now?",
            default=True,
        )
        if not install_now:
            raise typer.Exit(code=1)
        out.print("[bold]Installing dependency:[/bold] openclaw")
        install_result = install_tool("openclaw", non_interactive=yes)
        if not install_result.ok:
            out.print(
                f"[red]✗ OpenClaw installation failed, {action_label} has been aborted:[/red] "
                f"{_trim(install_result.detail)}"
            )
            raise typer.Exit(code=1)
        status = check_openclaw()
        if not status.ok:
            out.print(
                f"[red]✗ OpenClaw is still unavailable after installation, {action_label} has been aborted:[/red] "
                f"{_trim(status.detail)}"
            )
            raise typer.Exit(code=1)
        out.print("[green]✓ OpenClaw installation completed.[/green]")

    executable = resolve_openclaw_executable()
    if executable is None:
        out.print(f"[red]✗ {action_label} failed: unable to resolve OpenClaw executable.[/red]")
        raise typer.Exit(code=1)

    healthy, detail = _gateway_is_healthy(executable, config=config)
    if healthy:
        return

    out.print(
        f"[red]✗ {action_label} failed: OpenClaw service is not healthy:[/red] "
        f"{_trim(detail) or 'health check failed'}"
    )
    out.print(
        "[yellow]ClawsomeFlow will not auto-start OpenClaw. "
        "Please start the same OpenClaw instance/version you are currently using in WebUI, "
        "then retry.[/yellow]"
    )
    out.print("[dim]Suggested check: openclaw gateway status --deep[/dim]")
    raise typer.Exit(code=1)


__all__ = [
    "MIN_OPENCLAW_VERSION",
    "ensure_openclaw_ready_or_exit",
    "ensure_openclaw_version_compatible_or_exit",
]
