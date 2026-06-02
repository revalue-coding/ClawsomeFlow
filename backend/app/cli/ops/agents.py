"""``csflow agents`` — list / create / chat / remove."""

from __future__ import annotations

import json as jsonlib

import typer
from rich.console import Console
from rich.table import Table

from app.cli.ops._http import delete, get, post

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("list")
def list_agents(
    json: bool = typer.Option(False, "--json"),
    all_users: bool = typer.Option(False, "--all-users", help="Include other users."),
) -> None:
    """List ClawsomeFlow-managed OpenClaw agents."""
    data = get("/api/openclaw/agents", **({"allUsers": "true"} if all_users else {}))
    items = data.get("items", [])
    if json:
        typer.echo(jsonlib.dumps(items, ensure_ascii=False, indent=2))
        return
    t = Table(title=f"OpenClaw agents ({len(items)})")
    t.add_column("ID", style="bold")
    t.add_column("Name")
    t.add_column("Owner")
    t.add_column("Created", style="dim")
    for a in items:
        t.add_row(a["id"], a["name"], a["createdByUser"], a["createdAt"])
    console.print(t)


@app.command("create")
def create_agent(
    agent_id: str = typer.Option(..., "--id", help="OpenClaw agent id."),
    name: str = typer.Option(..., "--name", help="OpenClaw agent display name."),
    responsibility: str = typer.Option(
        ...,
        "--responsibility",
        help="Core responsibility for this agent.",
    ),
    extra: str = typer.Option(
        "",
        "--extra",
        help="Optional additional requirements for role definition.",
    ),
    team_id: str | None = typer.Option(
        None,
        "--team-id",
        help="Optional team id.",
    ),
) -> None:
    """Create one OpenClaw agent and complete bootstrap synchronously."""
    description = responsibility.strip()
    extra_text = extra.strip()
    if extra_text:
        description = f"{description}\n\nAdditional requirements: {extra_text}"
    payload: dict[str, object] = {
        "id": agent_id,
        "name": name,
        "description": description,
        "nlPrompt": description,
    }
    if team_id:
        payload["teamId"] = team_id
    created = post("/api/openclaw/agents", payload)
    console.print(
        "[green]✓[/green] created and bootstrapped "
        f"[bold]{created['id']}[/bold] at [dim]{created['workspacePath']}[/dim]"
    )


@app.command("remove")
def remove_agent(
    agent_id: str = typer.Argument(...),
    purge: bool = typer.Option(False, "--purge", help="Also wipe workspace data."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete an OpenClaw agent."""
    if not yes:
        msg = (
            f"Delete {agent_id} AND wipe its workspace data?"
            if purge
            else f"Delete {agent_id}? (workspace data kept)"
        )
        if not typer.confirm(msg, default=False):
            raise typer.Exit(code=0)
    delete(
        f"/api/openclaw/agents/{agent_id}{'?purge=true' if purge else ''}"
    )
    console.print(f"[green]✓[/green] removed {agent_id}")


@app.command("remove-hard")
def remove_agent_hard(
    agent_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Hard-remove an OpenClaw agent (unregister + delete workspace directory)."""
    if not yes:
        confirmed = typer.confirm(
            "Hard-remove this agent? This will unregister it from OpenClaw and "
            "PERMANENTLY delete ~/.clawsomeflow/agents/<id>/ data.",
            default=False,
        )
        if not confirmed:
            raise typer.Exit(code=0)
    delete(f"/api/openclaw/agents/{agent_id}?purge=true")
    console.print(
        f"[green]✓[/green] hard-removed {agent_id} "
        "(openclaw unregistered + workspace deleted)"
    )


@app.command("chat")
def chat(
    agent_id: str = typer.Argument(...),
    message: str = typer.Argument(...),
) -> None:
    """One-shot non-streaming chat (use Web UI for live streaming)."""
    data = post(
        f"/api/openclaw/agents/{agent_id}/chat",
        {"messages": [{"role": "user", "content": message}], "stream": False},
    )
    # OpenAI-compat response shape.
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = jsonlib.dumps(data, ensure_ascii=False, indent=2)
    console.print(content)


@app.command("reinstall-skills")
def reinstall_skills_cmd(
    agent_id: str | None = typer.Argument(
        None,
        help="Agent id to update; omit to reinstall for ALL managed agents.",
    ),
) -> None:
    """Refresh runtime assets inside one or all OpenClaw workspaces.

    Run this after upgrading ClawsomeFlow if the new release ships an
    additional user-agent skill or template/tool update. Idempotent —
    re-deploys common agent rules/skills, standard ``csflow-*`` skills, and
    common built-in cron job definitions.
    """
    # Talk to the service layer directly (CLI is co-resident with the
    # backend's storage; calls are local-only). This intentionally does
    # NOT go through HTTP because it's an admin maintenance op.
    from app.services.openclaw_agents import (
        AgentNotFound,
        reinstall_skills,
        reinstall_skills_for_all,
    )
    if agent_id:
        try:
            installed = reinstall_skills(agent_id)
        except AgentNotFound as exc:
            console.print(f"[red]✗ {exc}[/red]")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]✓[/green] {agent_id}: installed {', '.join(installed)}"
        )
        return
    out = reinstall_skills_for_all()
    if not out:
        console.print("[dim]No managed OpenClaw agents found.[/dim]")
        return
    for aid, skills in out.items():
        if skills:
            console.print(
                f"[green]✓[/green] {aid}: installed {', '.join(skills)}"
            )
        else:
            console.print(f"[red]✗[/red] {aid}: failed (see logs)")
