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


__all__ = [
    "is_alive",
    "pid_file",
    "read_pid",
    "remove_pid",
    "stop_process",
    "write_pid",
]
