"""``csflow serve`` — boot uvicorn (skip dep-check / init).

This is the process the managed service unit (systemd/launchd) runs;
``csflow start`` reaches it indirectly via that unit. Exposed standalone
so users who manage their own setup (systemd unit, supervisor, foreman,
...) can skip the friendlier preamble.
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
    # Peer-symmetric collaboration: every instance is identical (no hub), so
    # the bind widens by default to make /api/external/* reachable. The guard
    # middleware enforces that remote source IPs can reach ONLY that
    # credential-gated prefix — main /api, /ws and the SPA stay loopback-only.
    # ``csflow external expose off`` keeps the loopback bind.
    if host == "127.0.0.1" and getattr(cfg, "external_api_expose", True):
        from app.integrations.internal_token import ensure_api_token_initialised

        widened = ensure_api_token_initialised(cfg)
        if widened is not cfg:
            cfg_mod.save_config(widened)
            cfg = widened
        host = "0.0.0.0"

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
