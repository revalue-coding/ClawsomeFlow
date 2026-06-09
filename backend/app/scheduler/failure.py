"""Failure detection (DEV.md §5.6) + on_failure handling.

The scheduler calls :func:`detect_failures` once per loop iteration with
a snapshot of the live ClawTeam tasks. Returns a list of
:class:`FailureRecord` describing what went wrong; the controller then
applies :class:`OnFailure` policy via :func:`apply_on_failure`.

The active signals:

1. **worker_reported_failed** — task metadata key ``csflow_failed`` was set
   by the worker (the dispatch template tells workers to do this when a step
   genuinely can't be completed).
2. **timeout** — wall-clock since dispatch exceeded effective timeout:
   ``max(FlowTask.timeout_seconds, 14400)``.
3. **leader_inbox_failed** — leader received an inbox message starting with
   ``FAILED:`` (a final-resort signal we honour even if the worker forgot to
   set the metadata key).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from app.logging_setup import failure_detected, get_logger
from app.models import FlowAgent, FlowTask, OnFailure

logger = get_logger("scheduler.failure")

# Runtime safety floor: timeout less than 240 minutes (4h) is treated as too
# aggressive for Flow-level scheduling.
_MIN_TASK_TIMEOUT_SECONDS = 14400


class FailureReason(str, Enum):
    worker_reported = "worker_reported"
    timeout = "timeout"
    leader_inbox_failed = "leader_inbox_failed"


@dataclass(frozen=True)
class FailureRecord:
    """One detected failure per task per loop iteration."""

    task_id: str
    agent_id: str
    reason: FailureReason
    detail: str = ""

    def as_log(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "agent_id": self.agent_id,
            "reason": self.reason.value, "detail": self.detail[:300],
        }


# ──────────────────────────────────────────────────────────────────────
# Inputs
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TaskSnapshot:
    """The minimum information we need from one ClawTeam task to detect failures.

    Built by the controller from MCP ``task_list`` results + per-Run dispatch
    bookkeeping. ``status`` is the ClawTeam task status string (``pending`` /
    ``in_progress`` / ``completed`` / ``blocked``).
    """

    task_id: str
    owner_agent_id: str
    status: str
    locked_by_agent: str | None
    metadata: dict[str, Any]
    dispatched_at_epoch: float | None  # None = not yet dispatched


# ──────────────────────────────────────────────────────────────────────
# Detection
# ──────────────────────────────────────────────────────────────────────


def detect_failures(
    *,
    team_name: str,
    flow_tasks: dict[str, FlowTask],
    snapshots: Iterable[TaskSnapshot],
    leader_agent_id: str,
    leader_inbox_messages: list[str] | None = None,
    now: float | None = None,
) -> list[FailureRecord]:
    """Apply failure signals to *snapshots* and return all failures found.

    Signals are evaluated in the order listed in the module docstring; the
    first matching signal "wins" per task (we don't double-count).
    """
    # Kept in signature for call-site compatibility / future expansion.
    del team_name, leader_agent_id
    out: list[FailureRecord] = []
    now = now if now is not None else time.time()
    # Materialise so we can iterate twice (signals 1-2 loop + the
    # completed-task lookup for signal 3 below).
    snapshots = list(snapshots)

    for snap in snapshots:
        # Skip terminal states.
        if snap.status in ("completed",):
            continue
        ftask = flow_tasks.get(snap.task_id)

        # Signal 1: explicit metadata.
        marker = snap.metadata.get("csflow_failed") if snap.metadata else None
        if marker:
            out.append(_record(snap, FailureReason.worker_reported, str(marker)))
            continue

        # Signal 2: timeout.
        if (
            ftask is not None
            and snap.dispatched_at_epoch is not None
            and snap.status in ("pending", "in_progress")
        ):
            elapsed = now - snap.dispatched_at_epoch
            configured_timeout = max(int(ftask.timeout_seconds), 1)
            effective_timeout = max(configured_timeout, _MIN_TASK_TIMEOUT_SECONDS)
            if elapsed > effective_timeout:
                out.append(_record(
                    snap, FailureReason.timeout,
                    "elapsed="
                    f"{int(elapsed)}s > limit={effective_timeout}s "
                    f"(configured={configured_timeout}s, min={_MIN_TASK_TIMEOUT_SECONDS}s)",
                ))
                continue

    # Signal 3: leader inbox FAILED:<task_id>:<reason>.
    # Treated as additive (in case 1-2 didn't fire — e.g. worker still alive
    # but report says blocked).
    # IMPORTANT: tasks that the worker has already marked ``completed`` in
    # ClawTeam are not re-failed here. The worker dispatch prompt now
    # instructs agents to mark the task completed **even on failure** and
    # carry the failure context to the leader via the "task X done:
    # FAILED — ..." inbox prefix. That structured form is intentionally
    # NOT parsed as a failure signal (it doesn't start with "FAILED"), so
    # the scheduler advances and the leader sees the failure in its
    # summary input. We still keep the legacy "FAILED:<tid>:<reason>"
    # form as a last-resort signal for cases where the agent died before
    # marking the task completed.
    completed_task_ids = {
        snap.task_id for snap in snapshots if snap.status == "completed"
    }
    if leader_inbox_messages:
        already = {(r.task_id, r.reason) for r in out}
        for msg in leader_inbox_messages:
            parsed = _parse_failed_inbox(msg)
            if parsed is None:
                continue
            tid, reason = parsed
            if tid in completed_task_ids:
                # Worker already moved the task to completed — no retry
                # needed, leader will read the failure context from inbox.
                continue
            owner = flow_tasks.get(tid).owner_agent_id if tid in flow_tasks else "?"
            key = (tid, FailureReason.leader_inbox_failed)
            if key in already:
                continue
            out.append(FailureRecord(
                task_id=tid, agent_id=owner,
                reason=FailureReason.leader_inbox_failed, detail=reason,
            ))
            already.add(key)

    for rec in out:
        failure_detected(**rec.as_log())
    return out


def _record(snap: TaskSnapshot, reason: FailureReason, detail: str) -> FailureRecord:
    return FailureRecord(
        task_id=snap.task_id,
        agent_id=snap.locked_by_agent or snap.owner_agent_id,
        reason=reason,
        detail=detail,
    )


def _parse_failed_inbox(msg: str) -> tuple[str, str] | None:
    """Parse ``FAILED: task_id: reason`` (lenient about whitespace)."""
    if not msg or "FAILED" not in msg:
        return None
    text = msg.strip()
    if not text.upper().startswith("FAILED"):
        return None
    parts = text.split(":", 2)
    if len(parts) < 2:
        return None
    tid = parts[1].strip()
    reason = parts[2].strip() if len(parts) >= 3 else ""
    return tid, reason


# ──────────────────────────────────────────────────────────────────────
# on_failure policy
# ──────────────────────────────────────────────────────────────────────


@dataclass
class FailureDecision:
    """How the controller should react to one :class:`FailureRecord`."""

    action: str  # "retry" | "skip" | "abort"
    new_retry_count: int


def apply_on_failure(
    *,
    record: FailureRecord,
    agent: FlowAgent,
    current_retry_count: int,
) -> FailureDecision:
    """Translate ``agent.on_failure`` + ``agent.max_retries`` into an action."""
    if agent.on_failure == OnFailure.abort:
        return FailureDecision(action="abort", new_retry_count=current_retry_count)
    if agent.on_failure == OnFailure.skip:
        return FailureDecision(action="skip", new_retry_count=current_retry_count)
    # retry
    if current_retry_count + 1 > agent.max_retries:
        # Out of retries → escalate to abort.
        logger.info(
            "failure_handled",
            task_id=record.task_id, strategy="abort_after_retries",
            retry_count=current_retry_count, max_retries=agent.max_retries,
        )
        return FailureDecision(action="abort", new_retry_count=current_retry_count)
    new_count = current_retry_count + 1
    logger.info(
        "failure_handled",
        task_id=record.task_id, strategy="retry",
        retry_count=new_count, max_retries=agent.max_retries,
    )
    return FailureDecision(action="retry", new_retry_count=new_count)


__all__ = [
    "FailureDecision",
    "FailureReason",
    "FailureRecord",
    "TaskSnapshot",
    "apply_on_failure",
    "detect_failures",
]
