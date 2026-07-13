"""``csflow mcp`` — run the ClawsomeFlow MCP server and register it with agents.

``csflow mcp serve`` launches the stdio MCP server (agents spawn this as a
local MCP server). The ``install`` / ``uninstall`` / ``print-config`` /
``list-platforms`` commands wire that server into a supported agent platform's
MCP configuration (Phase 3).
"""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("serve", hidden=True)
def serve() -> None:
    """Run the ClawsomeFlow MCP server over stdio (blocks).

    Not a user-facing command: the agent platform spawns it via the MCP config
    written by ``csflow mcp install`` (``command: csflow, args: [mcp, serve]``).
    Hidden from help for that reason, but remains invokable so that config works.
    It talks to the local backend over loopback using the configured api_token,
    so the backend service (``csflow start``) must be running.
    """
    from app.mcp.server import serve as _serve

    _serve()


@app.command("list-platforms", hidden=True)
def list_platforms() -> None:
    """List agent platforms this command can register the MCP server with.

    Hidden from help (the supported platforms are documented in the README /
    ``csflow mcp install --help``); kept as a convenience command.
    """
    from app.services import mcp_install

    for pid in mcp_install.supported_platforms():
        spec = mcp_install._spec(pid)
        auto = "manual" if spec.style in ("manual",) else (
            "per-agent" if spec.style == "hermes" else "auto"
        )
        console.print(f"  [bold]{pid}[/bold]  [dim]({auto})[/dim]  {spec.note}")


@app.command("install")
def install(
    platform: str = typer.Option(..., "--platform", "-p", help="Agent platform id (see list-platforms)."),
    agent: str = typer.Option("", "--agent", "-a", help="Hermes agent/profile id; omit to use the Hermes default profile."),
    name: str = typer.Option("clawsomeflow", "--name", help="MCP server entry name."),
    force: bool = typer.Option(False, "--force", help="Skip the CLI-installed check / overwrite an existing entry."),
) -> None:
    """Register the ClawsomeFlow MCP server (``csflow mcp serve``) with an agent platform.

    Hermes is per-agent (pass --agent <id>; omit it to target the default profile);
    other platforms are global. Platforms whose format ClawsomeFlow does not
    manage print a snippet to paste manually.
    """
    from app.services import mcp_install

    try:
        res = mcp_install.install(platform, agent_id=agent or None, name=name, force=force)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    marker = {"written": "[green]✓[/green]", "removed": "[green]✓[/green]"}.get(
        res.action, "[yellow]•[/yellow]"
    )
    console.print(f"{marker} [{res.action}] {res.message}")
    if res.path:
        console.print(f"  [dim]{res.path}[/dim]")


@app.command("uninstall")
def uninstall(
    platform: str = typer.Option(..., "--platform", "-p", help="Agent platform id."),
    agent: str = typer.Option("", "--agent", "-a", help="Hermes agent/profile id; omit to use the Hermes default profile."),
    name: str = typer.Option("clawsomeflow", "--name", help="MCP server entry name to remove."),
) -> None:
    """Remove the ClawsomeFlow MCP server entry from an agent platform's config."""
    from app.services import mcp_install

    try:
        res = mcp_install.uninstall(platform, agent_id=agent or None, name=name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[{res.action}] {res.message}")
    if res.path:
        console.print(f"  [dim]{res.path}[/dim]")


@app.command("print-config")
def print_config(
    platform: str = typer.Option(..., "--platform", "-p", help="Agent platform id."),
    name: str = typer.Option("clawsomeflow", "--name", help="MCP server entry name."),
) -> None:
    """Print a copy-pasteable MCP-server config snippet for a platform (no writes)."""
    from app.services import mcp_install

    try:
        typer.echo(mcp_install.print_config(platform, name=name))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
