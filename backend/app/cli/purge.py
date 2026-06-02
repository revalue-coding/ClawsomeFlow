"""``csflow purge-data`` — destructive: wipe ``~/.clawsomeflow/``.

Separated from ``csflow uninstall`` so it cannot fire by accident:

* Requires the user to type the literal word ``PURGE`` (case-sensitive).
* Lists every directory it'll delete *before* asking, with sizes.
* Refuses to run while the backend is up (``csflow status`` PID alive).
* ``-y / --yes`` is intentionally **not supported**. Scripted use-case
  must explicitly pass ``--i-understand-this-deletes-everything``,
  which is verbose by design.

Order of operations:
1. Stop check (refuses if uvicorn pid is alive).
2. Inventory: list dirs + sizes; total bytes.
3. Strong prompt: literal "PURGE" required.
4. Run ``uninstall_from_openclaw(purge_data_dir=True)`` — this also
   removes the OpenClaw integration before deleting the data dir.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app import config as cfg_mod, paths
from app.cli import app
from app.cli._runtime import is_alive, read_pid

console = Console()


def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if isinstance(n, float) else f"{n} {unit}"
        n = n / 1024
    return f"{n:.1f} TB"


@app.command(name="purge-data")
def purge_data(
    confirmed: bool = typer.Option(
        False, "--i-understand-this-deletes-everything",
        help="Required for non-interactive runs. Verbose by design.",
    ),
    keep_openclaw: bool = typer.Option(
        False, "--keep-openclaw",
        help="Don't unregister from openclaw.json (default: also unregister).",
    ),
) -> None:
    """Delete ``~/.clawsomeflow/`` entirely. Cannot be undone."""
    home = paths.clawsomeflow_home()

    pid = read_pid()
    if pid and is_alive(pid):
        console.print(
            f"[red]Refusing to purge:[/red] backend is running (pid {pid}). "
            "Run [bold]csflow stop[/bold] first."
        )
        raise typer.Exit(code=1)

    if not home.exists():
        console.print(f"[yellow]Nothing to delete.[/yellow] {home} doesn't exist.")
        raise typer.Exit(code=0)

    table = Table(title=f"Will DELETE: {home}", show_lines=False)
    table.add_column("Path")
    table.add_column("Size", justify="right")
    children = sorted(home.iterdir())
    total = 0
    for child in children:
        sz = _dir_size(child) if child.is_dir() else child.stat().st_size
        total += sz
        suffix = "/" if child.is_dir() else ""
        table.add_row(f"{child.name}{suffix}", _human(sz))
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{_human(total)}[/bold]")
    console.print(table)

    if not confirmed:
        console.print(
            "\n[bold red]This permanently deletes ALL ClawsomeFlow data.[/bold red]\n"
            "It includes:\n"
            "  • Your DB (flows, runs, history)\n"
            "  • Every OpenClaw agent's workspace + git repo + commits\n"
            "  • All structured logs\n"
        )
        answer = typer.prompt(
            "To proceed, type the word [bold]PURGE[/bold]",
            default="",
            show_default=False,
        )
        if answer.strip() != "PURGE":
            console.print("[yellow]Aborted (you didn't type PURGE).[/yellow]")
            raise typer.Exit(code=1)

    # Best-effort: also unregister from openclaw.json before deleting the
    # workspace dirs they point at. (User can opt out with --keep-openclaw.)
    if not keep_openclaw:
        try:
            from app.integrations.openclaw_install import uninstall_from_openclaw
            cfg = cfg_mod.load_config()
            asyncio.run(uninstall_from_openclaw(purge_data_dir=False, config=cfg))
            console.print("[green]✓[/green] Unregistered from openclaw.json")
        except Exception as exc:
            console.print(
                f"[yellow]⚠[/yellow] OpenClaw unregister failed ({exc}); "
                "continuing with data wipe."
            )

    shutil.rmtree(home)
    console.print(f"[green]✓[/green] Removed [dim]{home}[/dim]")
    console.print(
        "[bold green]Done.[/bold green] Re-run [bold]csflow start[/bold] "
        "to re-initialise from scratch."
    )
