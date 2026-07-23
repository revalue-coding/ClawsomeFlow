"""Shared runtime helpers used by multiple CLI modules.

Owns the PID-file convention so ``csflow stop`` knows which process to
signal, plus a tiny HTTP poller used to wait for the backend to come up
after the managed service (systemd/launchd) starts it.
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


def wait_for_health(*, host: str, port: int, timeout_seconds: float = 60.0) -> bool:
    """Poll ``/health`` until it answers 200 or the timeout elapses.

    "systemd unit active" only means the process launched; first boot after
    an upgrade still runs init/migration before uvicorn listens. Poll HTTP so
    callers can report *actual* readiness instead of guessing.
    """
    import urllib.error
    import urllib.request

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{probe_host}:{port}/health"
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1.0)
    return False


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


def notify_active_runs_will_pause(*, non_interactive: bool, action: str, console) -> None:
    """Tell the user in-flight runs will be PAUSED (never aborted) on stop/restart.

    The pre-stop drain (:meth:`FlowScheduler.drain_to_terminal`) parks every
    live run resumably — it NEVER terminates one — so a stop/restart/upgrade
    does not need the user's permission. This is purely informational: it does
    NOT prompt and never blocks the operation. Skipped when ``non_interactive``
    (``--yes`` / backend self-calls) or when the service is down / idle.
    ``action`` is a short verb phrase, e.g. ``"stop the service"``.
    """
    if non_interactive:
        return
    pid = read_pid()
    if pid is None or not is_alive(pid):
        return
    count = active_driving_run_count()
    if count <= 0:
        return
    console.print(
        f"[yellow]⚠ {count} run(s) are still executing.[/yellow] "
        f"They will be paused when you {action}, then resumed after it comes "
        "back (scheduled / delegated runs resume automatically; resume the "
        "rest with 继续执行 / Continue in the UI)."
    )


__all__ = [
    "active_driving_run_count",
    "notify_active_runs_will_pause",
    "is_alive",
    "pid_file",
    "read_pid",
    "remove_pid",
    "stop_process",
    "wait_for_health",
    "write_pid",
]
