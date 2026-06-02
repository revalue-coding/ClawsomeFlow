"""``csflow stop`` — terminate the backend started by ``csflow serve``."""

from __future__ import annotations

import typer
from rich.console import Console

from app.cli import app
from app.cli._runtime import is_alive, read_pid, remove_pid, stop_process
from app.cli._user_service import ServiceError, stop_if_running

console = Console()


@app.command()
def stop(
    grace: float = typer.Option(
        8.0, "--grace", help="Seconds to wait for SIGTERM before SIGKILL.",
    ),
) -> None:
    """Stop the running backend (PID file at ``~/.clawsomeflow/csflow.pid``)."""
    try:
        if stop_if_running():
            console.print("[green]✓ Stopped managed user service.[/green]")
            remove_pid()
            raise typer.Exit(code=0)
    except ServiceError:
        # No managed user service configured; fallback to PID-only stop.
        pass

    pid = read_pid()
    if pid is None:
        console.print("[yellow]No PID file — nothing to stop.[/yellow]")
        raise typer.Exit(code=0)
    if not is_alive(pid):
        console.print(
            f"[yellow]PID {pid} not running (stale PID file).[/yellow]"
        )
        remove_pid()
        raise typer.Exit(code=0)
    console.print(f"Stopping backend (pid={pid})…")
    if stop_process(pid, grace_seconds=grace):
        remove_pid()
        console.print("[green]✓ Stopped.[/green]")
    else:
        console.print(f"[red]✗ Failed to stop pid {pid}.[/red]")
        raise typer.Exit(code=1)
