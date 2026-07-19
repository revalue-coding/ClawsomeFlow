"""Structured logging (structlog) setup.

Public API:
* :func:`configure_logging` — idempotent setup; call once at process start.
* :func:`get_logger` — returns a structlog ``BoundLogger`` (preferred over
  ``structlog.get_logger`` so we can inject defaults later).
* :func:`bind_context` — bind common fields (``run_id`` / ``agent_id`` / ...)
  for the duration of a ``with`` block.
* :func:`spawn_cmd_built` and friends — small wrappers that emit the
  standard events listed in DEV.md §7 with the agreed field names.

All output is JSON-encoded (written to ~/.clawsomeflow/.logs/ files).
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    unbind_contextvars,
)
from structlog.types import EventDict

from app import paths


# ──────────────────────────────────────────────────────────────────────
# Setup
# ──────────────────────────────────────────────────────────────────────


_configured = False


def _utc_iso(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    """Add an ISO-8601 UTC timestamp to every event."""
    event_dict["ts"] = datetime.now(timezone.utc).isoformat()
    return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    to_file: bool = True,
    to_stderr: bool = True,
) -> None:
    """Idempotent structlog configuration.

    Args:
        level: Minimum log level (``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``).
        to_file: Emit JSONL to ``~/.clawsomeflow/.logs/csflow-{date}.jsonl``.
        to_stderr: Also mirror to stderr (useful in dev / under systemd
            so journalctl picks it up via ``StandardOutput=journal``).
    """
    global _configured
    if _configured:
        return

    handlers: list[logging.Handler] = []
    if to_file:
        log_path = paths.logs_dir() / f"csflow-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    if to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            _utc_iso,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger.

    ``configure_logging`` is called automatically if not already configured.
    """
    if not _configured:
        configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


# ──────────────────────────────────────────────────────────────────────
# Context binding
# ──────────────────────────────────────────────────────────────────────


@contextmanager
def bind_context(**kwargs: Any) -> Iterator[None]:
    """Bind context vars (``run_id``, ``agent_id``, ``task_id``, ``user`` ...)
    that are merged into every log event inside the ``with`` block."""
    keys = list(kwargs.keys())
    bind_contextvars(**kwargs)
    try:
        yield
    finally:
        unbind_contextvars(*keys)


def clear_context() -> None:
    """Clear all bound context vars (used in tests / between requests)."""
    clear_contextvars()


# ──────────────────────────────────────────────────────────────────────
# Standard event helpers (see DEV.md §7)
#
# Using these helpers (instead of free-form log.info()) ensures field
# names stay consistent so jq queries / dashboards keep working.
# ──────────────────────────────────────────────────────────────────────


def spawn_cmd_built(
    cmd_argv: list[str],
    *,
    workspace: bool,
    repo: str | None,
    keepalive: bool,
    has_task: bool,
    has_skill: bool,
) -> None:
    """Log a constructed ``clawteam spawn`` command.

    These four flags directly correspond to the four anti-loop defences
    (DEV.md §4 / §5). Aggregated logs are queryable via
    ``csflow logs verify-anti-loop``.
    """
    log = get_logger("clawteam_cli")
    log.info(
        "spawn_cmd_built",
        cmd_argv=cmd_argv,
        workspace=workspace,
        repo=repo,
        keepalive=keepalive,
        has_task=has_task,
        has_skill=has_skill,
    )


def spawn_cmd_executed(
    *,
    cmd_argv: list[str],
    exit_code: int,
    stderr: str = "",
    stdout: str = "",
) -> None:
    """Log the result of a spawn command execution."""
    log = get_logger("clawteam_cli")
    if exit_code == 0:
        log.info("spawn_cmd_executed", cmd_argv=cmd_argv, exit_code=0)
    else:
        log.error(
            "spawn_cmd_executed",
            cmd_argv=cmd_argv,
            exit_code=exit_code,
            stderr=stderr[:2000],  # cap to avoid log explosion on huge stderrs
            stdout=stdout[:2000],
        )


def runtime_inject(
    *,
    target: str,
    summary_len: int,
    success: bool,
    error_msg: str | None = None,
    exit_code: int | None = None,
) -> None:
    log = get_logger("clawteam_cli")
    log.info(
        "runtime_inject",
        target=target,
        summary_len=summary_len,
        success=success,
        exit_code=exit_code,
        error_msg=error_msg,
    )


def workspace_merge(
    *,
    agent_id: str,
    team: str,
    success: bool,
    stderr: str = "",
) -> None:
    """Workspace merge outcome (per DEV.md §7).

    Conflicts emit WARNING; clean merges emit INFO.
    """
    log = get_logger("clawteam_cli")
    if success:
        log.info(
            "workspace_merge",
            agent_id=agent_id, team=team, success=True,
        )
    else:
        log.warning(
            "workspace_merge",
            agent_id=agent_id, team=team, success=False,
            stderr=(stderr or "")[:2000],
        )


def openclaw_json_modify(
    *,
    operation: str,
    agent_id: str | None = None,
    agent_count: int | None = None,
) -> None:
    """``~/.openclaw/openclaw.json`` mutation event (per DEV.md §7).

    ``lock_wait_ms`` is logged separately by :func:`lock_acquired` —
    correlate via the surrounding ``openclaw_json`` lock acquisition entry.
    """
    log = get_logger("openclaw_json")
    log.info(
        "openclaw_json_modify",
        operation=operation,
        agent_id=agent_id,
        agent_count=agent_count,
    )


def lock_acquired(*, key: str, wait_ms: float) -> None:
    """Log lock acquisition; emits WARNING if wait > 1000ms."""
    log = get_logger("concurrency")
    if wait_ms > 1000:
        log.warning("lock_acquired", key=key, wait_ms=round(wait_ms, 2))
    else:
        log.debug("lock_acquired", key=key, wait_ms=round(wait_ms, 2))


def lock_timeout(*, key: str, waited_ms: float) -> None:
    """A named lock (asyncio or file) timed out before it could be acquired."""
    log = get_logger("concurrency")
    log.error("lock_timeout", key=key, waited_ms=round(waited_ms, 2))


def file_lock_acquired(*, path: str, wait_ms: float) -> None:
    """Cross-process repo file lock acquisition; WARNING if wait > 1000ms."""
    log = get_logger("concurrency")
    if wait_ms > 1000:
        log.warning("file_lock_acquired", path=path, wait_ms=round(wait_ms, 2))
    else:
        log.debug("file_lock_acquired", path=path, wait_ms=round(wait_ms, 2))


def workspace_violation(
    *,
    agent_id: str,
    task_id: str | None,
    dirty_files: str,
) -> None:
    """OpenClaw post-task audit detected writes to the main repo."""
    log = get_logger("worktree.audit")
    log.warning(
        "workspace_violation",
        agent_id=agent_id,
        task_id=task_id,
        dirty_files=dirty_files[:1000],
    )


def run_state_transition(*, from_state: str, to_state: str, reason: str = "") -> None:
    log = get_logger("scheduler")
    log.info(
        "run_state_transition",
        **{"from": from_state, "to": to_state, "reason": reason},
    )


def task_state_transition(*, task_id: str, old: str, new: str) -> None:
    log = get_logger("scheduler")
    log.info("task_state_transition", task_id=task_id, old=old, new=new)


def task_dispatched(
    *,
    task_id: str,
    decision: str,
    session_state_before: str,
    session_state_after: str,
) -> None:
    log = get_logger("scheduler")
    log.info(
        "task_dispatched",
        task_id=task_id,
        decision=decision,
        session_state_before=session_state_before,
        session_state_after=session_state_after,
    )


def failure_detected(*, task_id: str, reason: str, **extra: Any) -> None:
    log = get_logger("scheduler")
    log.warning("failure_detected", task_id=task_id, reason=reason, **extra)


__all__ = [
    "configure_logging",
    "get_logger",
    "bind_context",
    "clear_context",
    "spawn_cmd_built",
    "spawn_cmd_executed",
    "runtime_inject",
    "workspace_merge",
    "openclaw_json_modify",
    "lock_acquired",
    "lock_timeout",
    "file_lock_acquired",
    "workspace_violation",
    "run_state_transition",
    "task_state_transition",
    "task_dispatched",
    "failure_detected",
]


def _get_log_path_for_today() -> Path:
    """Test helper: get the JSONL log file for today."""
    return paths.logs_dir() / f"csflow-{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
