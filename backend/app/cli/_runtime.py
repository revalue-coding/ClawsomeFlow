"""Shared runtime helpers used by multiple CLI modules.

Owns the PID-file convention so ``csflow stop`` knows which process to
signal, plus a tiny HTTP poller used to wait for the backend to come up
when ``csflow start`` boots uvicorn in-process.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from app import paths


def pid_file() -> Path:
    return paths.clawsomeflow_home() / "csflow.pid"


def write_pid(pid: int | None = None) -> None:
    pid_file().write_text(str(pid if pid is not None else os.getpid()))


def read_pid() -> int | None:
    p = pid_file()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except ValueError:
        return None


def remove_pid() -> None:
    p = pid_file()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def is_alive(pid: int) -> bool:
    """POSIX-only liveness check via ``signal 0``."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def stop_process(pid: int, *, grace_seconds: float = 8.0) -> bool:
    """SIGTERM → wait → SIGKILL. Returns True if the process is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return True
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return True
    time.sleep(0.5)
    return not is_alive(pid)


def active_driving_run_count() -> int:
    """Best-effort count of runs that need a live process. 0 on any error.

    Read directly from the local DB (the CLI cannot see the live process's
    in-memory scheduler). The startup orphan sweep guarantees that while the
    backend is up, any ACTIVE_DRIVING row is genuinely in-flight.
    """
    try:
        from app.storage import get_storage
        return int(get_storage().count_active_driving_runs())
    except Exception:
        return 0


def confirm_no_active_runs_or_exit(*, non_interactive: bool, action: str, console) -> None:
    """Interactively confirm before a stop/restart that would abort live runs.

    Skipped entirely when ``non_interactive`` (``--yes`` / backend self-calls)
    or when the service is not running (nothing in-flight to terminate). Only
    prompts when the backend is up AND has ACTIVE_DRIVING runs. ``action`` is a
    short verb phrase, e.g. ``"stop the service"`` / ``"restart the service"``.
    """
    if non_interactive:
        return
    pid = read_pid()
    if pid is None or not is_alive(pid):
        return
    count = active_driving_run_count()
    if count <= 0:
        return
    import typer

    console.print(
        f"[yellow]⚠ {count} run(s) are still executing.[/yellow] "
        f"They will be gracefully aborted when you {action}."
    )
    if not typer.confirm(f"Continue and {action}?", default=False):
        console.print("[dim]Cancelled; service left running.[/dim]")
        raise typer.Exit(code=0)


__all__ = [
    "active_driving_run_count",
    "confirm_no_active_runs_or_exit",
    "is_alive",
    "pid_file",
    "read_pid",
    "remove_pid",
    "stop_process",
    "write_pid",
]
