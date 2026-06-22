"""``csflow stop`` — terminate the backend started by ``csflow serve``."""

from __future__ import annotations

import typer
from rich.console import Console

from app import config as cfg_mod
from app.cli import app
from app.cli._runtime import (
    confirm_no_active_runs_or_exit,
    is_alive,
    read_pid,
    remove_pid,
    stop_process,
)
from app.cli._user_service import (
    ServiceError,
    reclaim_stale_port_listeners,
    stop_if_running,
)

console = Console()


@app.command()
def stop(
    grace: float = typer.Option(
        8.0, "--grace", help="Seconds to wait for SIGTERM before SIGKILL.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the in-flight-run confirmation (use for scripts/automation).",
    ),
) -> None:
    """Stop the running backend (PID file at ``~/.clawsomeflow/csflow.pid``)."""
    # Confirm before terminating in-flight runs (the pre-stop drain aborts them).
    confirm_no_active_runs_or_exit(
        non_interactive=yes, action="stop the service", console=console,
    )

    stopped_any = False

    try:
        if stop_if_running():
            console.print("[green]✓ Stopped managed user service.[/green]")
            remove_pid()
            stopped_any = True
    except ServiceError:
        # No managed user service configured; fallback to PID-only stop.
        pass

    if not stopped_any:
        pid = read_pid()
        if pid is None:
            pass
        elif not is_alive(pid):
            console.print(
                f"[yellow]PID {pid} not running (stale PID file).[/yellow]"
            )
            remove_pid()
        else:
            console.print(f"Stopping backend (pid={pid})…")
            if stop_process(pid, grace_seconds=grace):
                remove_pid()
                console.print("[green]✓ Stopped.[/green]")
                stopped_any = True
            else:
                console.print(f"[red]✗ Failed to stop pid {pid}.[/red]")
                raise typer.Exit(code=1)

    # Final safety net: reclaim an orphaned manual `uvicorn app.main:app`
    # listener on the configured port — e.g. one left behind by
    # `deploy.sh source` / run-dev-bg.sh, which is neither the managed service
    # nor tracked by the PID file. Without this, the official install path
    # (`install-user.sh`) would abort with "port still occupied".
    try:
        port = cfg_mod.load_config().csflow_port
    except Exception:
        port = None
    if port is not None:
        reclaimed = reclaim_stale_port_listeners(port)
        if reclaimed:
            pids = ", ".join(str(p) for p in reclaimed)
            console.print(
                f"[green]✓ Reclaimed orphaned dev listener(s) on port {port}:[/green] {pids}"
            )
            stopped_any = True

    if not stopped_any:
        console.print("[yellow]Nothing to stop.[/yellow]")
    raise typer.Exit(code=0)
