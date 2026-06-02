"""``csflow uninstall`` — safe uninstall that preserves ``~/.clawsomeflow/``.

This command only removes OpenClaw-side managed registrations and stops
the managed ClawsomeFlow user service so the configured API port is freed.
Data deletion is intentionally split into ``csflow purge-data``.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from app import config as cfg_mod, paths
from app.cli import app
from app.cli._runtime import is_alive, read_pid, remove_pid, stop_process
from app.cli._user_service import (
    ServiceError,
    describe_port_listeners,
    stop_disable_and_release_port,
)

console = Console()


@app.command()
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Remove OpenClaw registrations and stop managed runtime, keeping local data."""
    if not yes:
        confirmed = typer.confirm(
            "Uninstall ClawsomeFlow integration now? "
            "(this keeps ~/.clawsomeflow data)",
            default=False,
        )
        if not confirmed:
            raise typer.Exit(code=0)

    cfg = cfg_mod.load_config()

    # Service teardown only targets ClawsomeFlow's managed user service
    # (systemd --user/launchd unit named by CSFLOW_SERVICE_NAME).
    service_stopped = False
    service_disabled = False
    reclaimed_pids: list[int] = []
    remaining_listeners: list[str] = []
    try:
        (
            service_stopped,
            service_disabled,
            reclaimed_pids,
            remaining_listeners,
        ) = stop_disable_and_release_port(port=cfg.csflow_port)
    except ServiceError as exc:
        console.print(
            f"[yellow]⚠[/yellow] Failed to manage background service automatically: {exc}"
        )

    pid = read_pid()
    if pid is not None:
        if is_alive(pid):
            if not stop_process(pid):
                console.print(
                    f"[red]Uninstall error:[/red] failed to stop local backend pid {pid}"
                )
                raise typer.Exit(code=1)
            service_stopped = True
            remove_pid()
        else:
            remove_pid()

    remaining_listeners = describe_port_listeners(cfg.csflow_port)
    if remaining_listeners:
        untouched = "\n".join(f"  - {row}" for row in remaining_listeners)
        console.print(
            f"[yellow]⚠[/yellow] Port {cfg.csflow_port} still has listeners "
            "(left untouched):\n"
            f"{untouched}\n"
            "If this is not expected, stop those processes manually."
        )

    try:
        from app.integrations.openclaw_install import uninstall_from_openclaw
        result = asyncio.run(
            uninstall_from_openclaw(purge_data_dir=False, config=cfg),
        )
    except Exception as exc:
        console.print(f"[red]Uninstall error: {exc}[/red]")
        raise typer.Exit(code=1)

    if service_stopped:
        console.print("[green]✓[/green] Stopped managed ClawsomeFlow service")
    if service_disabled:
        console.print("[green]✓[/green] Disabled managed ClawsomeFlow auto-start")
    if reclaimed_pids:
        console.print(
            "[green]✓[/green] Reclaimed stale ClawsomeFlow listeners: "
            + ", ".join(str(pid) for pid in reclaimed_pids)
        )

    console.print(
        f"[green]✓[/green] Removed agents from openclaw.json: "
        f"{', '.join(result.agents_removed) or '(none)'}"
    )
    if result.workspaces_removed:
        console.print(
            f"[green]✓[/green] Removed legacy runtime workspace data: "
            f"{', '.join(str(p) for p in result.workspaces_removed)}"
        )
    console.print(
        f"\n[bold green]Done.[/bold green]   "
        f"Your data at [dim]{paths.clawsomeflow_home()}[/dim] is intact:\n"
        "  • config.json + db.sqlite (or your PG)\n"
        "  • flows/ + runs/ + logs/\n"
        "  • agents/<id>/workspace/ (your OpenClaw agent project repos)\n\n"
        "Re-installing later will pick everything up where you left off.\n"
        "To delete all local data explicitly, use [bold]csflow purge-data[/bold]."
    )
