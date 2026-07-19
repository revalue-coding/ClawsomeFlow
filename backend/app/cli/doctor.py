"""``csflow doctor`` — env + integration audit.

Runs the dependency checker, then probes the live OpenClaw gateway and
reports the openclaw.json + skills install state. Useful when a Run
fails for an obscure reason and the user wants to know if the
environment changed.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from app import config as cfg_mod
from app.cli import app
from app.cli.deps import fatal_missing, render_table, run_all

console = Console()


@app.command()
def doctor() -> None:
    """Comprehensive environment + integration audit."""
    console.print("[bold]🦞 ClawsomeFlow doctor[/bold]\n")

    # 1. Toolchain.
    results = run_all()
    console.print(render_table(results))
    missing = fatal_missing(results)
    console.print("")

    # 2. ClawsomeFlow config / data layout.
    cfg = cfg_mod.load_config()
    summary = _config_table(cfg)
    console.print(summary)
    console.print("")

    # 3. OpenClaw integration.
    try:
        from app.integrations.openclaw_install import install_summary
        snap = install_summary(cfg)
    except Exception as exc:
        console.print(f"[red]openclaw integration probe failed: {exc}[/red]")
        snap = {}
    if snap:
        oct = Table(title="OpenClaw integration", show_header=False)
        oct.add_column("k", style="bold")
        oct.add_column("v")
        for k, v in snap.items():
            oct.add_row(k, str(v))
        console.print(oct)
        console.print("")

    # 4. OpenClaw gateway HTTP probe (best-effort).
    try:
        from app.integrations.openclaw_bridge import OpenclawBridge
        async def _probe():
            async with OpenclawBridge.from_config(cfg) as bridge:
                return await bridge.health()
        health = asyncio.run(_probe())
        gw = Table(title="OpenClaw gateway", show_header=False)
        gw.add_column("k", style="bold")
        gw.add_column("v")
        gw.add_row("reachable", "[green]yes[/green]" if health.reachable else "[red]no[/red]")
        gw.add_row("auth_ok", "[green]yes[/green]" if health.auth_ok else "[red]no[/red]")
        gw.add_row(
            "chat_completions_enabled",
            "[green]yes[/green]" if health.chat_completions_enabled else "[yellow]no[/yellow]",
        )
        if health.detail:
            gw.add_row("detail", health.detail)
        console.print(gw)
    except Exception as exc:
        console.print(f"[yellow]OpenClaw gateway probe skipped: {exc}[/yellow]")

    if missing:
        console.print(
            f"\n[red]✗ Missing required tools: {', '.join(missing)}. "
            "See install hints above.[/red]"
        )
        raise typer.Exit(code=1)
    # Scope the verdict: the ✓ covers the REQUIRED toolchain only — the
    # optional OpenClaw gateway probe above may still be red.
    console.print(
        "\n[green]✓ Required toolchain OK.[/green] "
        "[dim](OpenClaw integration status is reported separately above.)[/dim]"
    )


def _config_table(cfg) -> Table:
    t = Table(title="ClawsomeFlow config", show_header=False)
    t.add_column("k", style="bold")
    t.add_column("v")
    t.add_row("csflow_port", str(cfg.csflow_port))
    t.add_row("clawteam_board_port", str(cfg.clawteam_board_port))
    t.add_row("default_user", cfg.default_user)
    t.add_row("openclaw_home", str(cfg.openclaw_home_path))
    t.add_row("clawteam_data_dir", cfg.clawteam_data_dir or "(default ~/.clawteam)")
    t.add_row("storage", f"{cfg.storage.kind} ({cfg.storage.url or 'default sqlite'})")
    return t
