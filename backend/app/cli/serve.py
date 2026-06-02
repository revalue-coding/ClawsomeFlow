"""``csflow serve`` — boot uvicorn (skip dep-check / init).

This is what ``csflow start`` calls under the hood after dependency check
+ init. Exposed standalone so users who manage their own setup (systemd
unit, supervisor, foreman, ...) can skip the friendlier preamble.
"""

from __future__ import annotations

import os

import typer
import uvicorn
from rich.console import Console

from app import config as cfg_mod
from app.cli import app
from app.cli._runtime import remove_pid, write_pid

console = Console()


@app.command()
def serve(
    port: int | None = typer.Option(
        None, "--port", "-p", help="Override config.csflow_port.",
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", "-h", help="Bind address.",
    ),
    reload: bool = typer.Option(
        False, "--reload", help="Enable auto-reload (dev only).",
    ),
) -> None:
    """Start the FastAPI backend (assumes ``csflow init`` already ran)."""
    cfg = cfg_mod.load_config()
    actual_port = port or cfg.csflow_port

    write_pid()
    try:
        console.print(
            f"[bold]🚀 ClawsomeFlow ready[/bold]\n"
            f"   Web UI:           [link=http://{host}:{actual_port}]http://{host}:{actual_port}[/link]\n"
            f"   API docs:         [link=http://{host}:{actual_port}/docs]http://{host}:{actual_port}/docs[/link]\n"
            f"   ClawTeam Board:   [link=http://{host}:{cfg.clawteam_board_port}/]http://{host}:{cfg.clawteam_board_port}/[/link]"
            f"  [dim](auto-spawned subprocess)[/dim]\n\n"
            f"[dim]Press Ctrl+C to stop.[/dim]\n"
        )
        uvicorn.run(
            "app.main:app",
            host=host,
            port=actual_port,
            reload=reload,
            log_level="info",
        )
    finally:
        remove_pid()
