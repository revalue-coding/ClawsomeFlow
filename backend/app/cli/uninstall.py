"""``csflow uninstall`` — remove OpenClaw integration and optional data wipe.

Default: unregister from OpenClaw and stop the managed service, keeping
``~/.clawsomeflow/``. Pass ``--purge-data`` to delete all local data after
the same teardown (requires typing ``PURGE`` or
``--i-understand-this-deletes-everything``).
"""

from __future__ import annotations

import asyncio
import errno
import shutil
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from app import config as cfg_mod, paths
from app.cli import app
from app.cli._runtime import is_alive, read_pid, remove_pid, stop_process
from app.cli._user_service import (
    ServiceError,
    describe_port_listeners,
    stop_disable_and_release_port,
)

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


def _print_purge_inventory(home: Path) -> None:
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


def _confirm_purge_interactive() -> bool:
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
        return False
    return True


def _rmtree_robust(path: Path) -> None:
    """``shutil.rmtree`` that tolerates concurrent ENOENT (e.g. pid cleanup)."""

    def onerror(
        func: Callable[..., object],
        p: str,
        exc_info: tuple[type[BaseException], BaseException, object],
    ) -> None:
        exc = exc_info[1]
        if isinstance(exc, FileNotFoundError):
            return
        if isinstance(exc, OSError) and exc.errno == errno.ENOENT:
            return
        raise exc

    shutil.rmtree(path, onerror=onerror)


def _stop_managed_runtime(cfg: cfg_mod.Config) -> tuple[
    bool, bool, list[int], list[str],
]:
    service_stopped = False
    service_disabled = False
    reclaimed_pids: list[int] = []
    remaining_listeners: list[str] = []
    try:
        (
            service_stopped,
            service_disabled,
            reclaimed_pids,
            remaining_listeners,
        ) = stop_disable_and_release_port(port=cfg.csflow_port)
    except ServiceError as exc:
        console.print(
            f"[yellow]⚠[/yellow] Failed to manage background service automatically: {exc}"
        )

    pid = read_pid()
    if pid is not None:
        if is_alive(pid):
            if not stop_process(pid):
                console.print(
                    f"[red]Uninstall error:[/red] failed to stop local backend pid {pid}"
                )
                raise typer.Exit(code=1)
            service_stopped = True
        remove_pid()

    remaining_listeners = describe_port_listeners(cfg.csflow_port)
    if remaining_listeners:
        untouched = "\n".join(f"  - {row}" for row in remaining_listeners)
        console.print(
            f"[yellow]⚠[/yellow] Port {cfg.csflow_port} still has listeners "
            "(left untouched):\n"
            f"{untouched}\n"
            "If this is not expected, stop those processes manually."
        )

    return service_stopped, service_disabled, reclaimed_pids, remaining_listeners


@app.command(
    short_help="Stop service + unregister OpenClaw; --purge-data wipes ~/.clawsomeflow/",
)
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip uninstall confirmation."),
    purge_data: bool = typer.Option(
        False,
        "--purge-data",
        help="Also delete ~/.clawsomeflow/ (requires PURGE confirmation).",
    ),
    purge_confirmed: bool = typer.Option(
        False,
        "--i-understand-this-deletes-everything",
        help="Non-interactive purge consent (only with --purge-data).",
    ),
    keep_openclaw: bool = typer.Option(
        False,
        "--keep-openclaw",
        help="Don't unregister from openclaw.json (only with --purge-data).",
    ),
) -> None:
    """Remove OpenClaw registrations and stop managed runtime.

    By default local data at ~/.clawsomeflow/ is preserved. Use --purge-data
    to wipe it entirely (cannot be undone).
    """
    home = paths.clawsomeflow_home()

    if purge_data:
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

        _print_purge_inventory(home)

        if not purge_confirmed and not _confirm_purge_interactive():
            raise typer.Exit(code=1)
    elif not yes:
        confirmed = typer.confirm(
            "Uninstall ClawsomeFlow integration now? "
            "(this keeps ~/.clawsomeflow data)",
            default=False,
        )
        if not confirmed:
            raise typer.Exit(code=0)

    cfg = cfg_mod.load_config()

    (
        service_stopped,
        service_disabled,
        reclaimed_pids,
        _remaining_listeners,
    ) = _stop_managed_runtime(cfg)

    if not keep_openclaw:
        try:
            from app.integrations.openclaw_install import uninstall_from_openclaw

            result = asyncio.run(
                uninstall_from_openclaw(purge_data_dir=False, config=cfg),
            )
        except Exception as exc:
            console.print(f"[red]Uninstall error: {exc}[/red]")
            raise typer.Exit(code=1)
    else:
        from app.integrations.openclaw_install import UninstallResult

        result = UninstallResult([], [], False)

    if purge_data:
        if home.exists():
            _rmtree_robust(home)
            console.print(f"[green]✓[/green] Removed [dim]{home}[/dim]")
        console.print(
            "[bold green]Done.[/bold green] Re-run [bold]csflow start[/bold] "
            "to re-initialise from scratch."
        )
        raise typer.Exit(code=0)

    if service_stopped:
        console.print("[green]✓[/green] Stopped managed ClawsomeFlow service")
    if service_disabled:
        console.print("[green]✓[/green] Disabled managed ClawsomeFlow auto-start")
    if reclaimed_pids:
        console.print(
            "[green]✓[/green] Reclaimed stale ClawsomeFlow listeners: "
            + ", ".join(str(pid) for pid in reclaimed_pids)
        )

    console.print(
        f"[green]✓[/green] Removed agents from openclaw.json: "
        f"{', '.join(result.agents_removed) or '(none)'}"
    )
    if result.workspaces_removed:
        console.print(
            f"[green]✓[/green] Removed legacy runtime workspace data: "
            f"{', '.join(str(p) for p in result.workspaces_removed)}"
        )
    console.print(
        f"\n[bold green]Done.[/bold green]   "
        f"Your data at [dim]{home}[/dim] is intact:\n"
        "  • config.json + db.sqlite (or your PG)\n"
        "  • flows/ + runs/ + logs/\n"
        "  • agents/<id>/workspace/ (your OpenClaw agent project repos)\n\n"
        "Re-installing later will pick everything up where you left off.\n"
        "To delete all local data, run [bold]csflow uninstall --purge-data[/bold]."
    )
