"""Process-group tracking for long-running child subprocesses.

Single-agent chat turns are short-lived (``subprocess.run`` / one-shot CLI) and
need no tracking. The only children that can outlive a graceful shutdown are the
**bootstrap / create** subprocesses (hermes ``hermes -z`` bootstrap, OpenClaw
create bootstrap), which may run for minutes. We spawn those in their own
process group (``start_new_session=True``) and register them here so the FastAPI
lifespan shutdown (and therefore ``csflow stop`` / restart / uninstall, which all
send SIGTERM → uvicorn → lifespan) can ``killpg`` any leftover group — guaranteeing
no residual session/bootstrap processes.

Note: a hard SIGKILL of the parent bypasses lifespan and cannot be intercepted;
that is an inherent OS limit, not covered here.
"""

from __future__ import annotations

import os
import signal
import threading
from typing import Any, Protocol

from app.logging_setup import get_logger

logger = get_logger("services.subprocess_registry")


class _ProcLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...  # subprocess.Popen


_LOCK = threading.Lock()
_LIVE: set[Any] = set()


def register(proc: Any) -> None:
    """Track a live process spawned with ``start_new_session=True``."""
    with _LOCK:
        _LIVE.add(proc)


def unregister(proc: Any) -> None:
    with _LOCK:
        _LIVE.discard(proc)


def kill_group(proc: Any, *, sig: int = signal.SIGKILL) -> bool:
    """Kill the whole process group of *proc* (it must have been started with
    ``start_new_session=True``). Falls back to killing just *proc*. Returns
    whether a signal was delivered."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return False
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:  # pragma: no cover - defensive; fall back to direct kill
        try:
            proc.kill()
            return True
        except Exception:
            return False


def terminate_all() -> int:
    """Best-effort kill every registered process group. Returns the count of
    groups signalled. Safe to call from shutdown."""
    with _LOCK:
        procs = list(_LIVE)
        _LIVE.clear()
    killed = 0
    for proc in procs:
        # SIGTERM first for a chance at clean exit, then SIGKILL to be sure.
        if kill_group(proc, sig=signal.SIGTERM):
            killed += 1
        kill_group(proc, sig=signal.SIGKILL)
    if killed:
        logger.info("subprocess_registry_terminated", groups=killed)
    return killed
