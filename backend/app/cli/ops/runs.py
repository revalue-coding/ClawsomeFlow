"""``csflow runs`` — list / start / show / abort / merge."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
import json as jsonlib

import typer
from rich.console import Console
from rich.table import Table

from app import paths
from app.cli.ops._http import get, post
from app.storage import get_storage

app = typer.Typer(no_args_is_help=True)
console = Console()


def _parse_kv(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values:
        if "=" not in v:
            raise typer.BadParameter(f"--input must be key=value, got {v!r}")
        k, val = v.split("=", 1)
        out[k.strip()] = val.strip()
    return out


@app.command("list")
def list_runs(
    flow: str | None = typer.Option(None, "--flow", help="Filter by flow id."),
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON."),
) -> None:
    """List runs."""
    data = get("/api/runs", flowId=flow, **({"status": status} if status else {}))
    items = data.get("items", [])
    if json:
        typer.echo(jsonlib.dumps(items, ensure_ascii=False, indent=2))
        return
    t = Table(title=f"Runs ({len(items)})")
    t.add_column("ID", style="bold")
    t.add_column("Flow")
    t.add_column("Status")
    t.add_column("Started", style="dim")
    t.add_column("Finished", style="dim")
    for r in items:
        t.add_row(
            r["id"], r["flowId"], r["status"], r["startedAt"],
            r.get("finishedAt") or "—",
        )
    console.print(t)


@app.command("start")
def start_run(
    flow_id: str = typer.Argument(...),
    inputs: list[str] = typer.Option(
        [], "--input", "-i", help="key=value (repeatable).",
    ),
) -> None:
    """Trigger a Run for a flow."""
    parsed = _parse_kv(inputs)
    data = post(f"/api/flows/{flow_id}/runs", {"inputs": parsed})
    console.print(
        f"[green]✓[/green] run [bold]{data['id']}[/bold] "
        f"team=[dim]{data['teamName']}[/dim] status={data['status']}"
    )


@app.command("show")
def show_run(
    run_id: str = typer.Argument(...),
    json: bool = typer.Option(False, "--json"),
    events: int = typer.Option(0, "--events", help="Tail N most-recent events."),
) -> None:
    """Show a Run's status, pending merges, and (optional) recent events."""
    data = get(f"/api/runs/{run_id}")
    if json:
        typer.echo(jsonlib.dumps(data, ensure_ascii=False, indent=2))
        return
    console.print(
        f"[bold]{data['id']}[/bold]  status=[bold]{data['status']}[/bold]\n"
        f"  flow={data['flowId']} v{data['flowVersion']}  team={data['teamName']}\n"
        f"  started={data['startedAt']}  finished={data.get('finishedAt') or '—'}\n"
        f"  board: {data.get('clawteamBoardUrl') or '(none)'}"
    )
    if data.get("pendingMerges"):
        console.print(f"\n[bold]pending merges ({len(data['pendingMerges'])})[/bold]")
        for p in data["pendingMerges"]:
            diff = p.get("diffSummary") or {}
            console.print(
                f"  - {p['agentId']}  branch={p['branch']}  "
                f"diff={diff.get('files_changed', '?')} files"
            )
    if events > 0:
        ev = get(f"/api/runs/{run_id}/events", limit=events)
        console.print(f"\n[bold]events (last {len(ev.get('items', []))}):[/bold]")
        for e in ev.get("items", []):
            console.print(
                f"  [dim]{e['ts']}[/dim] [bold]{e['type']}[/bold] "
                f"agent={e.get('agentId') or '—'} task={e.get('taskId') or '—'}"
            )


@app.command("abort")
def abort_run(run_id: str = typer.Argument(...)) -> None:
    """Cancel an active Run."""
    data = post(f"/api/runs/{run_id}/abort")
    console.print(f"[green]✓[/green] status={data['status']}")


@app.command("merge")
def merge_pending(
    run_id: str = typer.Argument(...),
    agent_id: str = typer.Argument(...),
) -> None:
    """Manually merge one pending agent."""
    data = post(f"/api/runs/{run_id}/merge", {"agentId": agent_id})
    if data["success"]:
        console.print(f"[green]✓[/green] merged {agent_id}")
    else:
        console.print(f"[red]✗[/red] conflict on {agent_id}\n{data['message'][:400]}")
        raise typer.Exit(code=1)


@app.command("dismiss-merge")
def dismiss_merge(
    run_id: str = typer.Argument(...),
    agent_id: str = typer.Argument(...),
) -> None:
    """Drop a pending merge entry without merging (worktree preserved)."""
    post(f"/api/runs/{run_id}/dismiss-merge", {"agentId": agent_id})
    console.print(f"[green]✓[/green] dismissed {agent_id}")


@app.command("cleanup-history")
def cleanup_history(
    keep_days: int = typer.Option(
        30,
        "--keep-days",
        min=1,
        help="Keep history within the most recent N days (terminal runs only).",
    ),
    all: bool = typer.Option(
        False, "--all", help="Delete all terminal history regardless of age.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation.",
    ),
) -> None:
    """Clear old terminal run history/events to reclaim disk space."""
    cutoff = None if all else (datetime.now(timezone.utc) - timedelta(days=keep_days))
    if not yes:
        if all:
            msg = (
                "Delete ALL terminal run history (runs/events + completed request logs)?"
            )
        else:
            msg = (
                f"Delete terminal history older than {keep_days} days "
                "(runs/events + completed request logs)?"
            )
        if not typer.confirm(msg, default=False):
            raise typer.Exit(code=0)

    storage = get_storage()
    summary = storage.history_cleanup(before=cutoff)
    run_ids = list(summary.get("deleted_run_ids", []))
    run_dirs_deleted = 0
    for rid in run_ids:
        p = paths.runs_dir() / rid
        if not p.exists():
            continue
        shutil.rmtree(p, ignore_errors=True)
        run_dirs_deleted += 1

    console.print(
        "[green]✓[/green] history cleanup done\n"
        f"  runs_deleted={summary.get('runs_deleted', 0)}\n"
        f"  events_deleted={summary.get('events_deleted', 0)}\n"
        f"  openclaw_requests_deleted={summary.get('openclaw_requests_deleted', 0)}\n"
        f"  task_decompose_requests_deleted={summary.get('task_decompose_requests_deleted', 0)}\n"
        f"  run_dirs_deleted={run_dirs_deleted}"
    )
