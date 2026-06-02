"""``csflow status`` — runtime + on-disk snapshot."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from app import __version__, bootstrap, config as cfg_mod
from app.cli import app
from app.cli._runtime import is_alive, pid_file, read_pid

console = Console()


@app.command()
def status() -> None:
    """Print version, config summary, runtime PID, and on-disk counts."""
    cfg = cfg_mod.load_config()
    snap = bootstrap.bootstrap_summary().as_dict()
    pid = read_pid()
    running = pid is not None and is_alive(pid)

    t = Table(title="🦞 ClawsomeFlow status", show_header=False, show_lines=False)
    t.add_column("k", style="bold")
    t.add_column("v")
    t.add_row("version", __version__)
    t.add_row(
        "running",
        f"[green]yes[/green] (pid={pid})" if running else "[yellow]no[/yellow]",
    )
    t.add_row("pid file", str(pid_file()))
    t.add_row("deployment_mode", cfg.deployment_mode)
    t.add_row("port", str(cfg.csflow_port))
    t.add_row("clawteam_board_port", str(cfg.clawteam_board_port))
    t.add_row("default_user", cfg.default_user)
    t.add_row("openclaw_home", str(cfg.openclaw_home_path))
    t.add_row("data home", str(snap["home"]))
    t.add_row("flows / runs / agents", f"{snap['flows_count']} / {snap['runs_count']} / {snap['agents_count']}")
    console.print(t)
