"""``csflow flows`` — list / show."""

from __future__ import annotations

import json as jsonlib

import typer
from rich.console import Console
from rich.table import Table

from app.cli.ops._http import get

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("list")
def list_flows(
    json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """List flows owned by the current user."""
    data = get("/api/flows")
    items = data.get("items", [])
    if json:
        typer.echo(jsonlib.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        console.print("[dim]No flows yet.[/dim]")
        return
    t = Table(title=f"Flows ({len(items)})")
    t.add_column("ID", style="bold")
    t.add_column("Name")
    t.add_column("Owner")
    t.add_column("v")
    t.add_column("Updated", style="dim")
    for f in items:
        t.add_row(f["id"], f["name"], f["ownerUser"], str(f["version"]), f["updatedAt"])
    console.print(t)


@app.command("show")
def show_flow(
    flow_id: str = typer.Argument(...),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """Show a single flow's full spec."""
    data = get(f"/api/flows/{flow_id}")
    if json:
        typer.echo(jsonlib.dumps(data, ensure_ascii=False, indent=2))
        return
    console.print(f"[bold]{data['name']}[/bold]  [dim]{data['id']}[/dim]")
    console.print(f"v{data['version']} · owner={data['ownerUser']}")
    console.print(f"description: {data['description'] or '(none)'}\n")
    console.print("[bold]agents[/bold]")
    for a in data["spec"].get("agents", []):
        marker = "👑 " if a.get("isLeader") else "   "
        console.print(
            f"  {marker}{a['id']}  kind={a['kind']}  repo={a.get('repo') or '(openclaw)'}"
        )
    console.print("\n[bold]tasks[/bold]")
    for t in data["spec"].get("tasks", []):
        deps = ", ".join(t.get("dependsOn", []) or []) or "—"
        sum_pill = " [yellow]Σ[/yellow]" if t.get("isLeaderSummary") else ""
        console.print(
            f"  {t['id']}  owner={t['ownerAgentId']}  deps=[{deps}]{sum_pill}\n"
            f"    [dim]{t.get('subject', '')}[/dim]"
        )
