"""``csflow logs`` — query / tail the structured JSONL log file.

Subcommands:
* ``csflow logs tail``                    — tail recent log entries.
* ``csflow logs verify-anti-loop``        — fail loud if any spawn ever
  carried ``--task`` / ``--skill clawteam`` / ``keepalive=True``.
* ``csflow logs grep RUN_ID``             — filter by run_id field.
* ``csflow logs export TARGET``           — export a support bundle zip
  (logs + runs + key metadata) for troubleshooting.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zipfile import ZIP_DEFLATED, ZipFile

import typer
from rich.console import Console

from app import paths
from app.cli import app

console = Console()
logs_app = typer.Typer(help="Inspect / tail structured JSONL logs.", no_args_is_help=True)
app.add_typer(logs_app, name="logs")


def _today_log() -> Path:
    return (
        paths.logs_dir() / f"csflow-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    )


def _iter_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _collect_bundle_files() -> list[tuple[Path, Path, str]]:
    items: list[tuple[Path, Path, str]] = []

    def _add_tree(root: Path, arc_root: Path, category: str) -> None:
        for p in _iter_files(root):
            items.append((p, arc_root / p.relative_to(root), category))

    def _add_file(src: Path, arc_path: Path, category: str) -> None:
        if src.exists() and src.is_file():
            items.append((src, arc_path, category))

    _add_tree(paths.logs_dir(), Path(".logs"), "logs")
    _add_tree(paths.runs_dir(), Path(".runs"), "runs")
    _add_tree(paths.flows_dir(), Path(".flows"), "flows")
    _add_file(paths.config_path(), Path("config.json"), "config")
    _add_file(paths.db_path(), Path("db.sqlite"), "database")
    _add_file(paths.version_marker_path(), Path(".csflow-version"), "version")
    _add_file(paths.migrations_ledger_path(), Path(".csflow-migrations.json"), "migrations-ledger")
    _add_file(
        paths.system_dir() / "openclaw-managed-agents.json",
        Path(".system/openclaw-managed-agents.json"),
        "openclaw",
    )
    return items


def _resolve_export_archive(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    default_name = f"csflow-logs-{stamp}.zip"

    if target.exists():
        if target.is_dir():
            return target / default_name
        if target.suffix.lower() != ".zip":
            raise typer.BadParameter("Target file must end with .zip")
        return target

    if target.suffix:
        if target.suffix.lower() != ".zip":
            raise typer.BadParameter("Target file must end with .zip")
        return target

    # No file suffix: treat as a destination directory and auto-name bundle.
    target.mkdir(parents=True, exist_ok=True)
    return target / default_name


def _iter_entries(path: Path, limit: int | None) -> Iterable[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        lines = fh.readlines()
    if limit is not None:
        lines = lines[-limit:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _format(entry: dict) -> str:
    ts = entry.get("ts", "")
    lvl = (entry.get("level") or entry.get("levelname") or "info").lower()
    color = {"warning": "yellow", "error": "red", "debug": "dim"}.get(lvl, "white")
    body = " ".join(
        f"{k}={v}" for k, v in entry.items()
        if k not in {"ts", "level", "event", "logger"}
    )
    return f"[dim]{ts}[/dim] [{color}]{lvl:5}[/{color}] [bold]{entry.get('event', '?')}[/bold]  {body}"


@logs_app.command()
def tail(
    n: int = typer.Option(50, "-n", "--lines", help="How many lines to show."),
    follow: bool = typer.Option(False, "-f", "--follow", help="Tail -f."),
    file: str | None = typer.Option(
        None, "--file", help="Override log file path (default: today's).",
    ),
) -> None:
    """Print the last *n* entries of today's JSONL log."""
    path = Path(file).expanduser() if file else _today_log()
    if not path.exists():
        console.print(f"[dim]No log file at {path}[/dim]")
        raise typer.Exit(code=0)
    for entry in _iter_entries(path, n):
        console.print(_format(entry))
    if not follow:
        return
    pos = path.stat().st_size
    while True:
        time.sleep(0.5)
        if not path.exists():
            continue
        size = path.stat().st_size
        if size <= pos:
            continue
        with path.open(encoding="utf-8") as fh:
            fh.seek(pos)
            chunk = fh.read()
        pos = size
        for line in chunk.splitlines():
            try:
                console.print(_format(json.loads(line)))
            except json.JSONDecodeError:
                pass


@logs_app.command()
def export(
    target: str = typer.Argument(
        ...,
        help="Destination .zip file path, or a directory to place bundle in.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite target bundle if it already exists.",
    ),
) -> None:
    """Export local troubleshooting bundle as a zip archive."""
    files = _collect_bundle_files()
    if not files:
        console.print(f"[dim]No diagnostic files under {paths.clawsomeflow_home()}[/dim]")
        raise typer.Exit(code=0)

    archive = _resolve_export_archive(Path(target).expanduser())
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists() and not overwrite:
        console.print(
            f"[red]✗ Target already exists:[/red] {archive}\n"
            "Use --overwrite to replace it."
        )
        raise typer.Exit(code=1)

    raw_bytes = 0
    category_counts: dict[str, int] = {}
    manifest = {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "sourceHome": str(paths.clawsomeflow_home()),
        "categories": {},
        "files": [],
    }
    try:
        with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as zf:
            for file_path, arc_path, category in files:
                size = file_path.stat().st_size
                raw_bytes += size
                zf.write(file_path, arcname=arc_path.as_posix())
                category_counts[category] = category_counts.get(category, 0) + 1
                manifest["files"].append({
                    "path": arc_path.as_posix(),
                    "category": category,
                    "bytes": size,
                })
            manifest["categories"] = category_counts
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
    except OSError as exc:
        console.print(f"[red]✗ Failed to export support bundle:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    categories = ", ".join(f"{k}={v}" for k, v in sorted(category_counts.items()))
    console.print(
        "[green]✓[/green] exported "
        f"{len(files)} files to [bold]{archive}[/bold] "
        f"({raw_bytes} bytes raw)"
    )
    if categories:
        console.print(f"[dim]{categories}[/dim]")


@logs_app.command()
def grep(
    run_id: str = typer.Argument(..., help="Run id to filter on."),
    file: str | None = typer.Option(None, "--file"),
) -> None:
    """Filter today's log by ``run_id``."""
    path = Path(file).expanduser() if file else _today_log()
    if not path.exists():
        raise typer.Exit(code=0)
    found = 0
    for e in _iter_entries(path, None):
        if e.get("run_id") == run_id:
            console.print(_format(e))
            found += 1
    console.print(f"\n[dim]{found} entries for run_id={run_id}[/dim]")


@logs_app.command(name="verify-anti-loop")
def verify_anti_loop(
    file: str | None = typer.Option(None, "--file"),
) -> None:
    """Audit every ``spawn_cmd_built`` event to confirm the 4 defences held.

    Per DEV.md §4: every spawn must NOT have ``--task``, must NOT have
    ``--skill clawteam``, must include ``--no-keepalive``. We scan and
    report any violation; exit 1 if any found.
    """
    path = Path(file).expanduser() if file else _today_log()
    if not path.exists():
        console.print(f"[dim]No log file at {path}[/dim]")
        raise typer.Exit(code=0)

    total = 0
    violations: list[tuple[int, str, dict]] = []
    for i, e in enumerate(_iter_entries(path, None), start=1):
        if e.get("event") != "spawn_cmd_built":
            continue
        total += 1
        argv = e.get("cmd_argv") or []
        if "--task" in argv:
            violations.append((i, "--task present", e))
        if any(
            argv[j] == "--skill" and j + 1 < len(argv) and argv[j + 1] == "clawteam"
            for j in range(len(argv))
        ):
            violations.append((i, "--skill clawteam present", e))
        if e.get("keepalive") is True or "--no-keepalive" not in argv:
            violations.append((i, "missing --no-keepalive", e))

    if violations:
        console.print(
            f"[red]✗ {len(violations)} anti-loop violations across "
            f"{total} spawn events.[/red]"
        )
        for line, reason, entry in violations[:20]:
            console.print(
                f"  [red]L{line}[/red] {reason}\n    "
                f"[dim]{json.dumps(entry, ensure_ascii=False)[:240]}[/dim]"
            )
        raise typer.Exit(code=1)
    console.print(
        f"[green]✓ All {total} spawn events comply with the 4 anti-loop defences.[/green]"
    )
