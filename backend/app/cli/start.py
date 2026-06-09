"""``csflow start`` — the streamlit-style one-shot UX.

Pipeline (per plan §11.1):
    1. Dependency check (deps.run_all → render table).
    2. If anything required is missing, ask whether to abort.
    3. If config.json doesn't exist yet, run ``csflow init`` interactively.
    4. Restart managed user service (background mode).

Designed for the absolute newcomer: zero arguments needed for the
common path; ``--yes`` skips every confirmation for scripted use.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from app import __version__, config as cfg_mod
from app.cli import app
from app.cli.deps import (
    check_non_openclaw_agent_tools,
    check_openclaw,
    fatal_missing,
    install_tool,
    render_table,
    run_all,
)
from app.cli.init import init as do_init
from app.cli._user_service import ServiceError, restart_and_enable, service_status_hint
from app.config import DEFAULT_CLAWTEAM_BOARD_PORT, DEFAULT_PORT

console = Console()


def _render_agent_runtime_summary() -> None:
    openclaw = check_openclaw()
    if not openclaw.ok:
        console.print(
            "[yellow]OpenClaw is not installed: non-OpenClaw agents can still run, "
            "but OpenClaw agents are unavailable (auto-install is not performed).[/yellow]"
        )
        console.print("")

    tool_rows = check_non_openclaw_agent_tools()
    table = Table(title="Agent Runtime Check")
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("Detected Info", style="dim")

    available_labels: list[str] = []
    missing_labels: list[str] = []
    if openclaw.ok:
        available_labels.append("OpenClaw")
        table.add_row(
            "OpenClaw",
            "[green]Available[/green]",
            openclaw.found_version or "openclaw --version",
        )
    else:
        missing_labels.append("OpenClaw")
        table.add_row(
            "OpenClaw",
            "[yellow]Not installed[/yellow]",
            openclaw.detail or openclaw.install_hint,
        )

    for row in tool_rows:
        if row.available:
            available_labels.append(row.label)
            detected = row.found_version or row.detail
            table.add_row(row.label, "[green]Available[/green]", detected)
        else:
            missing_labels.append(row.label)
            table.add_row(row.label, "[yellow]Unavailable[/yellow]", row.detail)

    console.print(table)
    console.print(
        f"[bold]Currently available agents:[/bold] "
        + (", ".join(available_labels) if available_labels else "(none)")
    )
    console.print(
        f"[bold]Also supported (setup required):[/bold] "
        + (", ".join(missing_labels) if missing_labels else "(none)")
    )


@app.command()
def start(
    port: int | None = typer.Option(
        None, "--port", "-p", help="HTTP port for the backend.",
    ),
    board_port: int = typer.Option(
        DEFAULT_CLAWTEAM_BOARD_PORT, "--board-port",
        help="Port for the bundled ClawTeam Board subprocess.",
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", "-h", help="Bind address.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip every interactive confirmation (use defaults).",
    ),
    auto_install_deps: bool = typer.Option(
        True,
        "--auto-install-deps/--no-auto-install-deps",
        help="Automatically try installing missing required dependencies.",
    ),
    skip_deps: bool = typer.Option(
        False, "--skip-deps", help="Don't run the dependency check.",
    ),
) -> None:
    """Run dependency check → init/upgrade → restart managed background service."""
    console.print("[bold]🦞 ClawsomeFlow first-time setup[/bold]\n")
    config_path = cfg_mod.paths.clawsomeflow_home_path() / "config.json"

    if not skip_deps:
        results = run_all()
        console.print(render_table(results))
        missing = fatal_missing(results)
        if missing:
            console.print(
                f"\n[red]Missing required dependencies: "
                f"{', '.join(missing)}.[/red]"
            )
            install_now = auto_install_deps
            if auto_install_deps and not yes:
                install_now = typer.confirm(
                    "Try installing missing required dependencies now?",
                    default=True,
                )
            if install_now:
                for name in missing:
                    if not yes:
                        confirmed = typer.confirm(
                            f"Install '{name}' now? ({results[name].install_hint})",
                            default=True,
                        )
                        if not confirmed:
                            continue
                    console.print(f"[bold]Installing dependency:[/bold] {name}")
                    install_result = install_tool(name, non_interactive=yes)
                    if install_result.ok:
                        console.print(f"[green]✓ Installed {name}[/green]")
                    else:
                        console.print(
                            f"[red]✗ Failed to install {name}:[/red] {install_result.detail}"
                        )
                results = run_all()
                console.print("")
                console.print(render_table(results))
                missing = fatal_missing(results)
            if missing:
                console.print(
                    f"\n[red]Still missing required dependencies: "
                    f"{', '.join(missing)}.[/red]"
                )
                if yes:
                    raise typer.Exit(code=1)
                proceed = typer.confirm(
                    "Continue anyway? (startup may fail until dependencies are installed)",
                    default=False,
                )
                if not proceed:
                    raise typer.Exit(code=1)
        console.print("")

    if not config_path.exists():
        console.print("[bold]Initialising ~/.clawsomeflow/...[/bold]")
        do_init(
            port=port or DEFAULT_PORT,
            user=None,
            board_port=board_port,
            mode="local",
            force=False,
            yes=yes,
            skip_openclaw=False,
            restart_service=False,
        )
    else:
        console.print(
            "[dim]Config exists — using current settings; pass --force-init "
            "(via `csflow init`) to reset.[/dim]"
        )
        from app import upgrade as upgrade_mod
        needs, marker = upgrade_mod.needs_upgrade()
        if needs:
            console.print(
                f"\n[bold yellow]Detected stale data dir[/bold yellow] "
                f"(marker {marker or 'none'} → package {__version__}). "
                "Running internal reconcile pipeline..."
            )
        else:
            console.print(
                "\n[dim]Version marker is current; running safe redeploy "
                "to sync bundled skills/rules/tools.[/dim]"
            )
        report = upgrade_mod.run_upgrade(include_frontend_build=needs)
        if report.repair_warnings:
            console.print(
                "[yellow]Some optional repairs need your attention "
                "(the service still starts normally):[/yellow]\n  • "
                + "\n  • ".join(report.repair_warnings)
            )
        if not report.ok:
            console.print(
                "[red]Upgrade had errors:[/red]\n  • "
                + "\n  • ".join(report.errors)
            )
            if not yes:
                proceed = typer.confirm(
                    "Continue starting anyway?", default=False,
                )
                if not proceed:
                    raise typer.Exit(code=1)
        else:
            if needs:
                console.print(
                    f"[green]✓[/green] Upgraded to {report.to_version}"
                )
            else:
                console.print("[green]✓[/green] Safe redeploy finished.")

    console.print("")
    cfg = cfg_mod.load_config()
    actual_port = port or cfg.csflow_port
    try:
        restart_and_enable(host=host, port=actual_port, non_interactive=yes)
    except ServiceError as exc:
        console.print(f"[red]✗ Failed to start managed service:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print("[bold green]✓ ClawsomeFlow is running in background.[/bold green]")
    console.print("")
    _render_agent_runtime_summary()
    console.print(
        f"\n   Web UI: [link=http://{host}:{actual_port}]http://{host}:{actual_port}[/link]\n"
        f"   Service: [dim]{service_status_hint()}[/dim]\n"
        f"   Version: [bold]{__version__}[/bold]"
    )
