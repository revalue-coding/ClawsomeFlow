"""RunController — drives one FlowRun end-to-end.

Responsibilities (per DEV.md scheduling design):

* Maintain one :class:`WorkerSession` per :class:`FlowAgent`.
* Compute the **ready set** of tasks (no unfinished deps + not yet dispatched).
* Render a dispatch message via :mod:`app.scheduler.prompts` and ship it.
* Detect failures via :mod:`app.scheduler.failure` + apply ``on_failure``.
* Emit :class:`RunEvent` rows for the WebSocket stream (Phase 7).
* Transition ``FlowRun.status`` (pending → compiling → running → ...).

This file deliberately *only* implements the **scheduling skeleton**:
ready-set computation, single-tick reconciliation loop, dispatch
orchestration, and graceful shutdown. Compilation (Flow → ClawTeam tasks)
and finalize/merge are stubbed — they're tracked separately in Phase 6.
The skeleton can already be exercised end-to-end with stub sessions in
unit tests; concrete spawn/dispatch behaviour comes online once Phase 6
fills in the compiler + finalize_run halves.

Design choices worth flagging:
* **No per-task asyncio.Task.** All task transitions happen on the single
  RunController loop; ``asyncio.gather`` is only used to dispatch ready
  tasks in parallel within one tick. This keeps the state machine local
  and easier to reason about.
* **Adaptive poll interval.** Idle ticks (no ready tasks, nothing dispatched
  recently) back off from 0.5s → 3s; activity resets to 0.5s. Reduces CPU
  on long-running Runs while keeping UI lag low when work is happening.
* **Lock awareness.** Spawn calls are funnelled through ``ClawTeamCli``
  which already takes ``clawteam_main_repo:`` + ``team_spawn:`` locks; the
  controller never grabs those itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from app.logging_setup import (
    bind_context,
    get_logger,
    run_state_transition,
    task_dispatched,
    task_state_transition,
)
from app.integrations.openclaw_cli import resolve_openclaw_executable
from app.integrations.openclaw_install import (
    looks_like_pending_scope_approval,
    repair_pending_scope_upgrades,
)
from app.config import load_config
from app.flow_modes import flow_mode, merge_reference_enabled, task_self_merges
from app.models import (
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    OnFailure,
    RunStatus,
    iso_utc,
)
from app.scheduler.compiler import CompileResult
from app.scheduler.prompts import (
    DispatchContext,
    UpstreamOutput,
    WorkerReport,
    build_external_task_package,
    build_external_task_text,
    build_leader_dispatch,
    build_openclaw_self_merge,
    build_worker_dispatch,
)
from app.scheduler.failure import (
    MIN_TASK_TIMEOUT_SECONDS,
    FailureRecord,
    TaskSnapshot,
    apply_on_failure,
    detect_failures,
)
from app.scheduler.finalize import (
    FinalizeInput,
    finalize_run,
    run_terminal_tail_cleanup,
)
from app.scheduler.run_metadata import POST_COMPLAINT_STATUS_KEY, run_is_unattended
from app.scheduler.naming import openclaw_session_id_for_run, team_name_for_run
from app.repo_merge_lock import self_merge_instruction
from app.scheduler.providers import DispatchClock
from app.services import subprocess_registry
from app.scheduler.sessions.base import (
    DispatchOutcome,
    SessionState,
    WorkerSession,
)
from app.scheduler.sessions.external import ExternalNodeSession
from app.scheduler.sessions.openclaw_tmux import OpenClawTmuxSession
from app.scheduler.sessions.tmux_ready import tmux_capture_pane, wait_shell_ready
from app.scheduler.sessions.tmux_live import (
    TmuxLiveSession,
    UnsupportedAgentKind,
)
from app.storage import StorageBackend, get_storage
from app.user_context import get_request_user, set_request_user
from app.worktree.audit import run_post_task_audit
from app.worktree.lookup import WorktreeInfo, WorktreeLookup, get_worktree_lookup

logger = get_logger("scheduler.controller")

_POST_COMPLAINT_STATUS_KEY = POST_COMPLAINT_STATUS_KEY
_TASK_PREFIX_RE = re.compile(
    r"^\s*task\s+([A-Za-z0-9._-]+)\s+done\s*:",
    re.IGNORECASE,
)
_LEADER_COMPLAINT_RELAY_RE = re.compile(
    r"^\s*\[csflow-complaint-relay:(?P<relay_task_id>[A-Za-z0-9._-]+):"
    r"(?P<target_agent_id>[A-Za-z0-9._-]+)\]\s*(?P<body>.*)$",
    re.DOTALL,
)
_TMUX_TARGET_NOT_FOUND_RE = re.compile(
    r"tmux\s+target.*not\s+found",
    re.IGNORECASE,
)
_AGENT_EXIT_REPORT_RE = re.compile(
    r"Agent\s+['\"](?P<agent>[^'\"]+)['\"]\s+exited\s+unexpectedly\."
    r"\s*Reset\s+(?P<count>\d+)\s+task\(s\)\s+to\s+pending",
    re.IGNORECASE,
)
_RUNTIME_SOCKET_CLOSED_RE = re.compile(
    r"socket\s+connection\s+was\s+closed\s+unexpectedly",
    re.IGNORECASE,
)
_RUNTIME_ERROR_PROMPT_RE = re.compile(
    r"(?:^|\n)(?:❯|\$|%|#|agent>|codex>|gemini>|kimi>|qwen>|pi>|opencode>|nanobot>)[ \t]*$",
    re.MULTILINE,
)


_POLL_MIN_SEC = 0.5
_POLL_MAX_SEC = 3.0
# Headless complaint/merge dispatch runs a REAL agent task to completion in a
# foreground subprocess, so its ceiling must follow the scheduler's task
# timeout floor (4h) — a shorter guard would kill a long-but-healthy task.
# The previous 1h value could misjudge legitimate long fixes as failures.
_OPENCLAW_HEADLESS_TIMEOUT_SEC = MIN_TASK_TIMEOUT_SECONDS
_RUNTIME_SOCKET_CAPTURE_LINES = 140
_RUNTIME_SOCKET_TAIL_CHARS = 1800
_RUNTIME_SOCKET_RECOVERY_LIMIT = 2
_RUNTIME_SOCKET_MIN_ELAPSED_SEC = 8.0
_RUNTIME_SOCKET_PROMPTLESS_RECOVERY_SEC = 45.0


# ──────────────────────────────────────────────────────────────────────
# Per-task scheduler bookkeeping
# ──────────────────────────────────────────────────────────────────────


class _TaskState(str, Enum):
    """Scheduler-side mirror of ClawTeam task status."""

    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    blocked = "blocked"


@dataclass
class _TaskBook:
    """Scheduler bookkeeping for a single Flow task."""

    task: FlowTask
    state: _TaskState = _TaskState.pending
    retries: int = 0
    dispatched_at: float | None = None
    last_dispatch_message_id: str | None = None
    last_dispatch_message: str | None = None
    last_failure: FailureRecord | None = None
    runtime_socket_recoveries: int = 0


@dataclass
class _CheckpointItem:
    """One completed task review item in a manual checkpoint."""

    task_id: str
    subject: str
    owner_agent_id: str
    summary: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    base_branch: str | None = None
    decision: str = "pending"  # pending | approved | rerun_requested
    rerun_count: int = 0
    last_feedback: str | None = None
    has_unread_update: bool = False
    rerun_requested_at: datetime | None = None
    last_report_timestamp: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class _DispatchCheckpoint:
    """Dispatch gate that must be approved before downstream dispatch."""

    downstream_task_id: str
    downstream_subject: str
    downstream_owner_agent_id: str
    item_order: list[str]
    items: dict[str, _CheckpointItem]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ──────────────────────────────────────────────────────────────────────
# Public outcome
# ──────────────────────────────────────────────────────────────────────


@dataclass
class RunOutcome:
    """Returned when the controller finishes (terminal)."""

    final_status: RunStatus
    completed_task_ids: list[str] = field(default_factory=list)
    failed_task_ids: list[str] = field(default_factory=list)
    skipped_task_ids: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class SessionStartupError(RuntimeError):
    """Raised when a worker session fails to start/recover."""

    agent_id: str
    phase: str
    detail: str

    def __post_init__(self) -> None:
        RuntimeError.__init__(
            self,
            f"agent {self.agent_id!r} session startup failed during {self.phase}: {self.detail}",
        )


# ──────────────────────────────────────────────────────────────────────
# Controller
# ──────────────────────────────────────────────────────────────────────


class RunController:
    """Drives one :class:`FlowRun` to completion.

    Use :meth:`run` (async) for the full lifecycle; :meth:`tick` (also async)
    is exposed for unit tests so they can drive the loop one step at a time
    against fake :class:`WorkerSession` instances.
    """

    def __init__(
        self,
        *,
        run: FlowRun,
        spec: FlowSpec,
        flow: Flow | None = None,                  # used by finalize for cleanup_team_on_finish
        flow_description: str = "",
        storage: StorageBackend | None = None,
        worktree_lookup: WorktreeLookup | None = None,
        compile_result: CompileResult | None = None,
        dispatch_clock: DispatchClock | None = None,
        # DI hooks for tests:
        session_factory=None,         # callable(agent) -> WorkerSession
        snapshot_provider=None,       # async callable() -> Iterable[TaskSnapshot]
        leader_inbox_provider=None,   # async callable() -> list[str|dict]
        finalize_fn=None,             # async callable(FinalizeInput, *, storage, ...) -> FinalizeOutcome
    ) -> None:
        self.run = run
        self.spec = spec
        self.flow = flow
        self.flow_description = flow_description
        self.team_name = run.team_name or team_name_for_run(run.id)
        self.storage = storage or get_storage()
        self.worktree_lookup = worktree_lookup or get_worktree_lookup()
        self.compile_result = compile_result
        self.dispatch_clock = dispatch_clock or DispatchClock()

        self._sessions: dict[str, WorkerSession] = {}
        self._tasks: dict[str, _TaskBook] = {
            # Status values are mirrored from ClawTeam snapshots in tick().
            t.id: _TaskBook(task=t)
            for t in spec.tasks
        }
        self._agents: dict[str, FlowAgent] = {a.id: a for a in spec.agents}
        self._leader_id = next(a.id for a in spec.agents if a.is_leader)
        self._leader_summary_task_id = next(
            (t.id for t in spec.tasks if t.is_leader_summary),
            None,
        )
        self._cancel_evt = asyncio.Event()
        self._poll_sec = _POLL_MIN_SEC

        self._session_factory = session_factory or self._default_session_factory
        self._snapshot_provider = snapshot_provider
        self._leader_inbox_provider = leader_inbox_provider
        self._finalize_fn = finalize_fn or finalize_run
        self._completed_audited: set[str] = set()
        self._snapshot_missing_warned = False
        self._forced_failed = False
        self._failed_task_ids: set[str] = set()
        self._skipped_task_ids: set[str] = set()
        self._terminal_snapshot_persisted = False
        self._task_outputs: dict[str, list[dict[str, Any]]] = {}
        self._worker_report_history: list[dict[str, Any]] = []
        self._seen_worker_report_keys: set[tuple[str | None, str, str]] = set()
        self._last_dispatch_failures: dict[str, dict[str, Any]] = {}
        self._dispatch_checkpoint: _DispatchCheckpoint | None = None
        self._checkpoint_lock = asyncio.Lock()
        self._checkpoint_passed_tasks: set[str] = set()
        self._checkpoint_approved_summaries: dict[str, str | None] = {}
        self._startup_tasks: dict[str, asyncio.Task[WorkerSession]] = {}
        self._prewarm_task: asyncio.Task[None] | None = None
        self._first_dispatch_task_id: str | None = None
        self._first_dispatch_owner_id: str | None = None
        # Per-tick leader mailbox_peek memo — populated by
        # :meth:`_fetch_leader_inbox_raw`, valid only inside one ``tick()``
        # (cleared on entry + exit so out-of-loop callers always peek fresh).
        self._leader_inbox_tick_cache: list[Any] | None = None
        self._in_tick = False

    # ── public surface ───────────────────────────────────────────────

    def cancel(self) -> None:
        """Request graceful shutdown — current tick finishes, no new dispatch."""
        self._cancel_evt.set()

    def checkpoint_snapshot(self) -> dict[str, Any] | None:
        """Return current manual-checkpoint snapshot (for API/UI polling)."""
        cp = self._dispatch_checkpoint
        if cp is None:
            return None
        return self._checkpoint_payload(cp)

    async def approve_checkpoint_item(self, *, upstream_task_id: str) -> dict[str, Any]:
        """Mark one upstream checkpoint item as approved."""
        tid = upstream_task_id.strip()
        if not tid:
            raise ValueError("upstream task id is required")
        if self._cancel_evt.is_set():
            raise RuntimeError("run is cancelling; checkpoint action is unavailable")
        payload: dict[str, Any]
        cleared_payload: dict[str, Any] | None = None
        task_id_for_event: str | None = None
        async with self._checkpoint_lock:
            cp = self._dispatch_checkpoint
            if cp is None:
                raise RuntimeError("run is not awaiting manual checkpoint")
            item = cp.items.get(tid)
            if item is None:
                raise KeyError(tid)
            item.decision = "approved"
            item.has_unread_update = False
            item.rerun_requested_at = None
            item.updated_at = datetime.now(timezone.utc)
            cp.updated_at = item.updated_at
            payload = self._checkpoint_payload(cp)
            task_id_for_event = cp.downstream_task_id
            if self._checkpoint_all_approved(cp):
                cleared_payload = dict(payload)
                cleared_payload["decision"] = "all_approved"
                self._dispatch_checkpoint = None
                self._mark_checkpoint_items_passed(cp)
                self._set_status(
                    RunStatus.running,
                    reason=f"checkpoint_approved:{cp.downstream_task_id}",
                )
        self._emit_event(
            "task_checkpoint_updated",
            task_id=task_id_for_event,
            payload={
                **payload,
                "decision": "approve",
                "upstream_task_id": tid,
            },
        )
        if cleared_payload is not None:
            self._emit_event(
                "task_checkpoint_cleared",
                task_id=task_id_for_event,
                payload=cleared_payload,
            )
        return payload

    async def mark_checkpoint_item_read(self, *, upstream_task_id: str) -> dict[str, Any]:
        """Clear unread-highlight state for one checkpoint item."""
        tid = upstream_task_id.strip()
        if not tid:
            raise ValueError("upstream task id is required")
        if self._cancel_evt.is_set():
            raise RuntimeError("run is cancelling; checkpoint action is unavailable")
        task_id_for_event: str | None = None
        payload: dict[str, Any]
        changed = False
        async with self._checkpoint_lock:
            cp = self._dispatch_checkpoint
            if cp is None:
                raise RuntimeError("run is not awaiting manual checkpoint")
            item = cp.items.get(tid)
            if item is None:
                raise KeyError(tid)
            if item.has_unread_update:
                item.has_unread_update = False
                item.updated_at = datetime.now(timezone.utc)
                cp.updated_at = item.updated_at
                changed = True
            payload = self._checkpoint_payload(cp)
            task_id_for_event = cp.downstream_task_id
        if changed:
            self._emit_event(
                "task_checkpoint_updated",
                task_id=task_id_for_event,
                payload={
                    **payload,
                    "decision": "upstream_output_read",
                    "upstream_task_id": tid,
                },
            )
        return payload

    async def request_checkpoint_rerun(
        self,
        *,
        upstream_task_id: str,
        feedback: str,
    ) -> dict[str, Any]:
        """Request an upstream task rerun while staying in checkpoint mode.

        External-node items (owner ``kind=external``) take a one-click
        re-dispatch path: *feedback* is optional and ignored (the channel
        round-trip has no place to inject review feedback) and the ORIGINAL
        task is simply dispatched again — same task id, same package, fresh
        one-time ticket (which invalidates the previous one). Local agents
        keep the feedback-preamble rerun contract.
        """
        tid = upstream_task_id.strip()
        text = feedback.strip()
        if not tid:
            raise ValueError("upstream task id is required")
        if self._cancel_evt.is_set():
            raise RuntimeError("run is cancelling; checkpoint rerun is unavailable")
        rerun_agent = None
        async with self._checkpoint_lock:
            cp = self._dispatch_checkpoint
            if cp is not None:
                pre_item = cp.items.get(tid)
                if pre_item is not None:
                    rerun_agent = self._agents.get(pre_item.owner_agent_id)
        is_external = rerun_agent is not None and rerun_agent.kind == AgentKind.external
        if not text and not is_external:
            raise ValueError("checkpoint rerun feedback is required")
        async with self._checkpoint_lock:
            cp = self._dispatch_checkpoint
            if cp is None:
                raise RuntimeError("run is not awaiting manual checkpoint")
            item = cp.items.get(tid)
            if item is None:
                raise KeyError(tid)
            agent = self._agents.get(item.owner_agent_id)
            if agent is None:
                raise RuntimeError(f"upstream owner agent {item.owner_agent_id!r} not found")
            active_same_owner = next(
                (
                    other.task_id
                    for other in cp.items.values()
                    if other.task_id != tid
                    and other.owner_agent_id == item.owner_agent_id
                    and other.decision == "rerun_requested"
                ),
                None,
            )
            if active_same_owner is not None:
                raise RuntimeError(
                    "owner agent already has an active checkpoint rerun: "
                    f"agent={item.owner_agent_id} task={active_same_owner}"
                )
            item.decision = "rerun_requested"
            item.rerun_count += 1
            item.last_feedback = text
            item.has_unread_update = False
            item.updated_at = datetime.now(timezone.utc)
            item.rerun_requested_at = item.updated_at
            cp.updated_at = item.updated_at
            payload = self._checkpoint_payload(cp)
            downstream_task_id = cp.downstream_task_id
        # Requirement: on rerun request, move the upstream task back to in_progress.
        synced = await self._update_clawteam_task_status(
            tid,
            status="in_progress",
            caller="csflow-checkpoint",
            force=True,
        )
        book = self._tasks.get(tid)
        if book is not None:
            book.state = _TaskState.in_progress
            now_epoch = time.time()
            book.dispatched_at = now_epoch
            self.dispatch_clock.mark(tid, now_epoch)
        if not synced:
            self._emit_event(
                "task_status_sync_failed",
                agent_id=item.owner_agent_id,
                task_id=tid,
                payload={"target_status": "in_progress", "source": "checkpoint_rerun"},
            )
        if is_external:
            # One-click re-dispatch of the ORIGINAL task: the external session
            # re-mints the ticket and re-sends the channel outbound with the
            # same task id/package (ExternalNodeSession derives the package
            # from the task id, so the message here is the same task sheet).
            book_for_rerun = self._tasks.get(tid)
            if book_for_rerun is None:
                raise RuntimeError(f"external checkpoint rerun: unknown task {tid!r}")
            ctx = await self._compose_dispatch_context(agent, book_for_rerun.task)
            prompt = self._render_dispatch_message(agent, book_for_rerun.task, ctx)
            dispatch_task_id = tid
        else:
            prompt = await self._build_checkpoint_rerun_prompt(
                downstream_task_id=downstream_task_id,
                upstream_item=item,
                feedback=text,
            )
            dispatch_task_id = f"checkpoint-rerun-{tid}"
        try:
            await self._dispatch_custom_task(
                agent=self._agents[item.owner_agent_id],
                task_id=dispatch_task_id,
                message=prompt,
            )
        except Exception:
            # Roll back the status mutation done before dispatch so the task
            # does not stay in a fake in_progress window.
            rollback_synced = await self._update_clawteam_task_status(
                tid,
                status="completed",
                caller="csflow-checkpoint",
                force=True,
            )
            book = self._tasks.get(tid)
            if book is not None:
                book.state = _TaskState.completed
                book.dispatched_at = None
            self.dispatch_clock.reset(tid)
            sess = self._sessions.get(item.owner_agent_id)
            if sess is not None:
                sess.mark_idle(reason=f"task_{tid}_rerun_dispatch_failed")
            if not rollback_synced:
                self._emit_event(
                    "task_status_sync_failed",
                    agent_id=item.owner_agent_id,
                    task_id=tid,
                    payload={
                        "target_status": "completed",
                        "source": "checkpoint_rerun_rollback",
                    },
                )
            rollback_payload: dict[str, Any] | None = None
            async with self._checkpoint_lock:
                cp = self._dispatch_checkpoint
                if cp is not None:
                    rollback_item = cp.items.get(tid)
                    if rollback_item is not None and rollback_item.decision == "rerun_requested":
                        rollback_item.decision = "pending"
                        rollback_item.rerun_requested_at = None
                        rollback_item.updated_at = datetime.now(timezone.utc)
                        cp.updated_at = rollback_item.updated_at
                        rollback_payload = self._checkpoint_payload(cp)
            if rollback_payload is not None:
                self._emit_event(
                    "task_checkpoint_updated",
                    task_id=downstream_task_id,
                    payload={
                        **rollback_payload,
                        "decision": "rerun_dispatch_failed",
                        "upstream_task_id": tid,
                    },
                )
            raise
        self._emit_event(
            "task_checkpoint_updated",
            task_id=downstream_task_id,
            payload={
                **payload,
                "decision": "rerun_requested",
                "upstream_task_id": tid,
            },
        )
        return payload

    async def run_user_complaint_phase(self, *, complaint_text: str) -> None:
        """Execute post-merge user complaint workflow in background.

        Flow:
        1) add + dispatch one leader complaint task;
        2) wait leader completion and strictly collect targeted feedback by
           (from_agent + relay task id + target agent id);
        3) dispatch merge requirements immediately to OpenClaw agents that did
           not receive complaint-handling tasks;
        4) add + dispatch complaint-fix tasks for complained OpenClaw agents
           (those tasks must complete fix + workspace update + merge in-task);
        5) wait immediate merge-requirement tasks completed, then terminal cleanup.
        """
        text = complaint_text.strip()
        if not text:
            raise ValueError("complaint text is required")
        self._emit_event(
            "run_complaint_phase_started",
            payload={"complaint_length": len(text)},
        )
        complaint_targets = self._complaint_target_agents()
        openclaw_merge_targets = self._merge_requirement_agents()
        if not complaint_targets and not openclaw_merge_targets:
            self._emit_event(
                "run_complaint_phase_skipped",
                payload={"reason": "no_openclaw_targets"},
            )
            await self._shutdown_remaining_sessions(reason="run_finalize")
            await self._finish_after_complaint_phase()
            self._emit_event(
                "run_complaint_phase_completed",
                payload={"target_agents": [], "target_count": 0, "merge_targets": []},
            )
            return

        from app.integrations.clawteam_mcp import get_mcp_client

        mcp = await get_mcp_client(user=self.run.user)
        if not complaint_targets:
            self._emit_event(
                "run_complaint_phase_skipped",
                payload={"reason": "no_persistent_openclaw_or_hermes_targets"},
            )
            merge_task_ids = await self._dispatch_merge_requirements(
                mcp=mcp,
                agents=openclaw_merge_targets,
                phase="satisfaction_direct",
                reason="no_openclaw_targets",
            )
            await self._wait_for_merge_requirement_tasks(
                mcp=mcp,
                task_ids=merge_task_ids,
                phase="satisfaction_direct",
            )
            await self._shutdown_remaining_sessions(reason="run_finalize")
            await self._finish_after_complaint_phase()
            self._emit_event(
                "run_complaint_phase_completed",
                payload={
                    "target_agents": [],
                    "target_count": 0,
                    "merge_targets": sorted(a.id for a in openclaw_merge_targets),
                },
            )
            return

        leader_task = await mcp.task_create(
            self.team_name,
            "Handle user complaint",
            description="internal complaint relay task (excluded from flow history)",
            owner=self._leader_id,
            metadata={
                "csflow_internal": True,
                "csflow_phase": "user_complaint",
                "csflow_exclude_history": True,
            },
        )
        leader_task_id = str(leader_task.get("id") or "")
        if not leader_task_id:
            raise RuntimeError("leader complaint task create returned empty id")
        try:
            await self._dispatch_complaint_task(
                agent=self._agents[self._leader_id],
                task_id=f"complaint-leader-{leader_task_id}",
                message=self._build_leader_complaint_prompt(
                    task_id=leader_task_id, complaint_text=text, targets=complaint_targets,
                ),
            )
        except Exception as exc:
            # User complaint is an optional post-phase workflow. If leader
            # dispatch cannot start (e.g. session/process disappeared), don't
            # strand the run in complaint_processing.
            logger.warning(
                "complaint_phase_leader_dispatch_failed",
                run_id=self.run.id,
                team=self.team_name,
                error=str(exc)[:1000],
            )
            await self._shutdown_remaining_sessions(reason="run_finalize")
            await self._finish_after_complaint_phase()
            return
        await self._wait_for_clawteam_tasks_completed(
            mcp=mcp, task_ids=[leader_task_id], timeout_sec=1800,
        )

        complaints = await self._collect_agent_complaints(
            mcp=mcp,
            target_agents=complaint_targets,
            relay_task_id=leader_task_id,
        )
        complained_agent_ids = set(complaints.keys())
        if not complaints:
            self._emit_event(
                "run_complaint_phase_skipped",
                payload={"reason": "leader_produced_no_targeted_feedback"},
            )

        immediate_merge_candidates = [
            a for a in openclaw_merge_targets if a.id != self._leader_id
        ]
        immediate_merge_targets = [
            a
            for a in immediate_merge_candidates
            if a.id not in complained_agent_ids
        ]
        immediate_merge_task_ids = await self._dispatch_merge_requirements(
            mcp=mcp,
            agents=immediate_merge_targets,
            phase="post_manager_complete",
            reason="not_targeted_by_complaint",
            source_task_id=leader_task_id,
        )

        complaint_task_ids: list[str] = []
        for target in complaint_targets:
            message = complaints.get(target.id)
            if not message:
                continue
            row = await mcp.task_create(
                self.team_name,
                f"Handle user complaint: {target.id}",
                description="internal complaint fix task (excluded from flow history)",
                owner=target.id,
                metadata={
                    "csflow_internal": True,
                    "csflow_phase": "user_complaint",
                    "csflow_exclude_history": True,
                    "csflow_target_agent": target.id,
                },
            )
            ct_task_id = str(row.get("id") or "")
            if not ct_task_id:
                raise RuntimeError(f"complaint task create returned empty id for {target.id}")
            try:
                await self._dispatch_complaint_task(
                    agent=target,
                    task_id=f"complaint-agent-{target.id}-{ct_task_id}",
                    message=self._build_agent_complaint_prompt(
                        task_id=ct_task_id,
                        user_complaint=text,
                        leader_feedback=message,
                        merge_required=(target.kind == AgentKind.openclaw),
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "complaint_target_dispatch_failed",
                    run_id=self.run.id,
                    team=self.team_name,
                    agent_id=target.id,
                    task_id=ct_task_id,
                    error=str(exc)[:1000],
                )
                continue
            complaint_task_ids.append(ct_task_id)

        if complaint_task_ids:
            await self._wait_for_clawteam_tasks_completed(
                mcp=mcp, task_ids=complaint_task_ids, timeout_sec=2700,
            )

        complaint_merge_in_task_targets = [
            a for a in openclaw_merge_targets if a.id in complained_agent_ids
        ]
        await self._wait_for_merge_requirement_tasks(
            mcp=mcp,
            task_ids=immediate_merge_task_ids,
            phase="post_manager_complete",
        )

        await self._shutdown_remaining_sessions(reason="run_finalize")
        await self._finish_after_complaint_phase()
        self._emit_event(
            "run_complaint_phase_completed",
            payload={
                "target_agents": sorted(list(complaints.keys())),
                "target_count": len(complaints),
                "merge_targets": sorted(a.id for a in openclaw_merge_targets),
                "merge_targets_post_manager": sorted(a.id for a in immediate_merge_targets),
                # Kept for compatibility with existing event consumers. These
                # targets now merge inside complaint-fix tasks (no separate
                # merge-requirement dispatch after complaint fixes).
                "merge_targets_post_complaint": sorted(a.id for a in complaint_merge_in_task_targets),
            },
        )

    async def skip_user_complaint_phase(self) -> None:
        """User chose "very satisfied" — dispatch merge requirements then finish."""
        merge_targets = self._merge_requirement_agents()
        if not merge_targets:
            await self._shutdown_remaining_sessions(reason="run_finalize")
            await self._finish_after_complaint_phase()
            self._emit_event(
                "run_complaint_phase_skipped",
                payload={"reason": "user_satisfied_no_merge_targets", "merge_targets": []},
            )
            return

        from app.integrations.clawteam_mcp import get_mcp_client

        mcp = await get_mcp_client(user=self.run.user)
        merge_task_ids = await self._dispatch_merge_requirements(
            mcp=mcp,
            agents=merge_targets,
            phase="satisfaction_direct",
            reason="user_marked_very_satisfied",
        )
        await self._wait_for_merge_requirement_tasks(
            mcp=mcp,
            task_ids=merge_task_ids,
            phase="satisfaction_direct",
        )
        await self._shutdown_remaining_sessions(reason="run_finalize")
        await self._finish_after_complaint_phase()
        self._emit_event(
            "run_complaint_phase_skipped",
            payload={
                "reason": "user_satisfied",
                "merge_targets": sorted(a.id for a in merge_targets),
            },
        )

    async def run_loop(self, *, max_ticks: int | None = None) -> RunOutcome:
        """Main loop. Returns when the Run reaches a terminal state.

        Lifecycle:
        1. ``RunStatus.running`` (set on entry; compilation is the API layer's
           job and would have flipped the Run from ``pending`` → ``compiling``
           before handing off).
        2. Per-tick reconciliation until terminal (all tasks done) or cancelled.
        3. ``finalize_run`` to settle merges/cleanup and decide the **final
           ``RunStatus``** (overrides our preliminary outcome).
        """
        prev_user = get_request_user()
        set_request_user(self.run.user)
        try:
            with bind_context(run_id=self.run.id, flow_id=self.run.flow_id, user=self.run.user):
                if self._cancel_evt.is_set():
                    self._set_status(
                        RunStatus.aborted, reason="run_cancelled_before_start",
                    )
                else:
                    self._set_status(RunStatus.running, reason="run_started")
                ticks = 0
                loop_exc: Exception | None = None
                try:
                    while not self._cancel_evt.is_set():
                        if max_ticks is not None and ticks >= max_ticks:
                            break
                        activity = await self.tick()
                        ticks += 1
                        if self._terminal_check():
                            await self._persist_terminal_execution_log(
                                trigger="terminal_check_true",
                            )
                            break
                        self._adapt_poll(activity=activity)
                        try:
                            await asyncio.wait_for(
                                self._cancel_evt.wait(), timeout=self._poll_sec,
                            )
                            # cancel_evt set during wait
                            break
                        except asyncio.TimeoutError:
                            pass
                except SessionStartupError as exc:
                    loop_exc = exc
                    self._forced_failed = True
                    logger.warning(
                        "run_loop_session_startup_failed",
                        agent_id=exc.agent_id,
                        phase=exc.phase,
                        error=exc.detail,
                    )
                except Exception as exc:
                    loop_exc = exc
                    self._forced_failed = True
                    logger.exception("run_loop_unhandled_exception", error=str(exc))
                    self._emit_event(
                        "run_loop_exception",
                        payload={"error": str(exc)[:1000]},
                    )
                finally:
                    # Always try to stop live sessions first so abnormal exits do
                    # not leak orphaned agent processes.
                    await self._shutdown_remaining_sessions(reason="run_finalize")
                    outcome = self._build_outcome()
                    if loop_exc is not None:
                        outcome.final_status = RunStatus.failed
                        outcome.reason = f"run loop exception: {loop_exc}"
                    # Hand off to finalize_run for merge / cleanup / final status.
                    final_status = await self._invoke_finalize(outcome)
                    outcome.final_status = final_status
                    self._set_status(final_status, reason=outcome.reason or "finalize")
                    return outcome
        finally:
            set_request_user(prev_user)

    async def tick(self) -> bool:
        """Run one scheduling iteration. Returns True if anything happened.

        Order matters: we **first** absorb the latest live ClawTeam snapshot
        (so just-completed tasks immediately unblock their dependents), then
        run failure detection (which may push tasks back to pending), and
        only **then** dispatch the resulting ready set. This collapses
        "completion → dispatch dependent" into a single tick which keeps the
        UI feeling responsive.

        Within one tick the leader mailbox is peeked **at most once**
        (failure detection / checkpoint refresh / dispatch context all share
        the memoized result — peek is non-consuming so this is purely an RPC
        dedup, not a semantic change).
        """
        self._in_tick = True
        self._leader_inbox_tick_cache = None
        try:
            activity = await self._tick_inner()
            return self._reconcile_external_wait_status() or activity
        finally:
            self._in_tick = False
            self._leader_inbox_tick_cache = None

    async def _tick_inner(self) -> bool:
        activity = False

        # 1. Apply latest snapshot — possibly flipping tasks to completed
        #    and freeing their owning sessions to Idle.
        snapshots = await self._fetch_snapshots()
        if snapshots:
            self._snapshot_missing_warned = False
            activity = self._apply_snapshots(snapshots) or activity
        elif any(
            b.state in (
                _TaskState.pending,
                _TaskState.blocked,
                _TaskState.in_progress,
            )
            for b in self._tasks.values()
        ):
            # ``task_list`` empty while we still have open tasks usually means
            # MCP/task-list degradation; surface it once so users can perceive
            # why the run might stall.
            if not self._snapshot_missing_warned:
                self._emit_event(
                    "snapshot_unavailable",
                    payload={
                        "detail": "task_list returned empty snapshots while run still has open tasks",
                    },
                )
                self._snapshot_missing_warned = True
            # ClawTeam is source-of-truth for pending/unblock decisions;
            # without a fresh snapshot we do not dispatch.
            return activity

        # 2. Failure detection + on_failure.
        if snapshots:
            inbox = await self._fetch_leader_inbox()
            failures = detect_failures(
                team_name=self.team_name,
                flow_tasks={t.id: t for t in self.spec.tasks},
                snapshots=snapshots,
                leader_agent_id=self._leader_id,
                leader_inbox_messages=inbox,
                agents=self._agents,
            )
            if failures:
                activity = True
                for rec in failures:
                    await self._handle_failure(rec)

            # 2.5. Fallback recovery: detect runtime-level socket closure in
            # live pane output and proactively requeue the task for redispatch.
            recovered = await self._runtime_socket_error_recovery_tick()
            activity = activity or recovered

        # 3. Manual checkpoint gate: while waiting for user review, do not
        #    dispatch any task (global pause for this run).
        checkpoint_waiting, checkpoint_changed = await self._checkpoint_gate_tick()
        activity = activity or checkpoint_changed
        if checkpoint_waiting:
            return activity
        if self._cancel_evt.is_set():
            return activity

        # 4. Dispatch the (potentially updated) ready set.
        ready = self._ready_tasks()
        if ready:
            dispatchable, checkpoint_target = self._partition_ready_tasks_for_checkpoint(ready)
            if checkpoint_target is not None:
                downstream_book, checkpoint_book = checkpoint_target
                opened = await self._open_dispatch_checkpoint(
                    downstream_task=downstream_book.task,
                    checkpoint_task=checkpoint_book.task,
                )
                return activity or opened
            if not dispatchable:
                return activity
            if self._first_dispatch_task_id is None:
                # Strategy: prioritize the very first pending task dispatch
                # before any background prewarm of other owners.
                dispatchable = [dispatchable[0]]
            activity = True
            results = await asyncio.gather(
                *(self._dispatch_one(book) for book in dispatchable),
                return_exceptions=True,
            )
            startup_error: SessionStartupError | None = None
            first_success: _TaskBook | None = None
            for book, res in zip(dispatchable, results):
                if isinstance(res, Exception):
                    if isinstance(res, SessionStartupError):
                        self._forced_failed = True
                        self._failed_task_ids.add(book.task.id)
                        book.state = _TaskState.blocked
                        self.dispatch_clock.reset(book.task.id)
                        book.dispatched_at = None
                        synced = await self._mark_clawteam_task_blocked(
                            book.task.id,
                            caller=res.agent_id,
                        )
                        if not synced:
                            self._emit_event(
                                "task_status_sync_failed",
                                agent_id=res.agent_id,
                                task_id=book.task.id,
                                payload={"target_status": "blocked"},
                            )
                        self._emit_event(
                            "task_session_start_failed",
                            agent_id=res.agent_id,
                            task_id=book.task.id,
                            payload={
                                "phase": res.phase,
                                "error": res.detail[:1000],
                            },
                        )
                        startup_error = startup_error or res
                        continue
                    logger.warning(
                        "dispatch_unexpected_exception",
                        task_id=book.task.id, error=str(res),
                    )
                    self._emit_event(
                        "task_dispatch_exception",
                        agent_id=book.task.owner_agent_id,
                        task_id=book.task.id,
                        payload={"error": str(res)[:1000]},
                    )
                    continue
                if (
                    self._first_dispatch_task_id is None
                    and first_success is None
                    and book.state == _TaskState.in_progress
                ):
                    first_success = book
            if first_success is not None:
                self._first_dispatch_task_id = first_success.task.id
                self._first_dispatch_owner_id = first_success.task.owner_agent_id
                self._launch_prewarm_tui_sessions()
            if startup_error is not None:
                # Hard-stop the run: session startup failures are terminal and
                # should not be retried forever across ticks.
                raise startup_error

        return activity

    # ── ready-set + dispatch ─────────────────────────────────────────

    def _reconcile_external_wait_status(self) -> bool:
        """Flip ``running`` ↔ ``awaiting_external`` from the in-flight task mix.

        ``awaiting_external`` = at least one task is in flight and EVERY
        in-flight task is owned by an external execution node — i.e. the run
        is purely blocked on results from outside the local agent stack.
        Only these two statuses ever flip here (checkpoint/review/complaint/
        terminal states own their transitions); the run detail page renders
        its cards from independent data sources, so a checkpoint opening
        while external tasks are pending still shows both.
        """
        if self.run.status not in (RunStatus.running, RunStatus.awaiting_external):
            return False
        in_flight = [
            b for b in self._tasks.values() if b.state == _TaskState.in_progress
        ]
        external_only = bool(in_flight)
        for book in in_flight:
            owner = self._agents.get(book.task.owner_agent_id)
            if owner is None or owner.kind != AgentKind.external:
                external_only = False
                break
        target = RunStatus.awaiting_external if external_only else RunStatus.running
        if self.run.status == target:
            return False
        self._set_status(target, reason="external_wait_reconcile")
        return True

    def _task_requires_manual_checkpoint(self, task: FlowTask) -> bool:
        if task.is_leader_summary:
            return False
        if not bool(getattr(task, "requires_human_checkpoint", False)):
            return False
        # Unattended runs (timed schedule OR MCP / --unattended) have no human in
        # the loop to approve a checkpoint, so the run would stall forever at
        # awaiting_user_checkpoint. Bypass the gate and dispatch the task.
        if run_is_unattended(self.run):
            return False
        return True

    def _checkpoint_all_approved(self, cp: _DispatchCheckpoint) -> bool:
        if not cp.items:
            return True
        return all(item.decision == "approved" for item in cp.items.values())

    def _mark_checkpoint_items_passed(self, cp: _DispatchCheckpoint) -> None:
        for dep_id in cp.item_order:
            item = cp.items.get(dep_id)
            if item is None:
                continue
            self._checkpoint_passed_tasks.add(item.task_id)
            self._checkpoint_approved_summaries[item.task_id] = item.summary

    def _checkpoint_payload(self, cp: _DispatchCheckpoint) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for dep_id in cp.item_order:
            item = cp.items.get(dep_id)
            if item is None:
                continue
            owner = self._agents.get(item.owner_agent_id)
            owner_external = getattr(owner, "external", None) if owner else None
            items.append({
                "task_id": item.task_id,
                "subject": item.subject,
                "owner_agent_id": item.owner_agent_id,
                # Lets the UI adapt checkpoint actions per owner type: external
                # items get one-click re-dispatch, no feedback box, no diff.
                "owner_kind": owner.kind.value if owner else None,
                "external_channel": (
                    owner_external.channel.value if owner_external else None
                ),
                "summary": item.summary,
                "worktree_path": item.worktree_path,
                "branch_name": item.branch_name,
                "base_branch": item.base_branch,
                "decision": item.decision,
                "rerun_count": item.rerun_count,
                "last_feedback": item.last_feedback,
                "has_unread_update": item.has_unread_update,
                "last_report_timestamp": item.last_report_timestamp,
                "updated_at": iso_utc(item.updated_at),
            })
        return {
            "downstream_task_id": cp.downstream_task_id,
            "downstream_subject": cp.downstream_subject,
            "downstream_owner_agent_id": cp.downstream_owner_agent_id,
            "created_at": iso_utc(cp.created_at),
            "updated_at": iso_utc(cp.updated_at),
            "items": items,
            "all_approved": self._checkpoint_all_approved(cp),
        }

    async def _checkpoint_gate_tick(self) -> tuple[bool, bool]:
        """Checkpoint gate before dispatch.

        Returns:
            (waiting, changed)
            - waiting=True  => run is blocked on manual checkpoint and must not dispatch.
            - changed=True  => checkpoint state/status changed this tick.
        """
        if self._dispatch_checkpoint is None:
            return False, False
        if self._cancel_evt.is_set():
            cleared_task_id: str | None = None
            cleared_payload: dict[str, Any] | None = None
            async with self._checkpoint_lock:
                cp = self._dispatch_checkpoint
                if cp is not None:
                    cleared_payload = self._checkpoint_payload(cp)
                    cleared_payload["decision"] = "cancelled"
                    cleared_task_id = cp.downstream_task_id
                    self._dispatch_checkpoint = None
            if cleared_payload is not None:
                self._emit_event(
                    "task_checkpoint_cleared",
                    task_id=cleared_task_id,
                    payload=cleared_payload,
                )
                return False, True
            return False, False
        if self.run.status in {
            RunStatus.completed,
            RunStatus.completed_with_conflicts,
            RunStatus.complaint_failed,
            RunStatus.failed,
            RunStatus.aborted,
        }:
            return False, False
        changed = await self._refresh_checkpoint_outputs()
        cleared_task_id: str | None = None
        cleared_payload: dict[str, Any] | None = None
        async with self._checkpoint_lock:
            cp = self._dispatch_checkpoint
            if cp is None:
                return False, changed
            if self._checkpoint_all_approved(cp):
                cleared_payload = self._checkpoint_payload(cp)
                cleared_payload["decision"] = "all_approved"
                cleared_task_id = cp.downstream_task_id
                self._dispatch_checkpoint = None
                self._mark_checkpoint_items_passed(cp)
                self._set_status(
                    RunStatus.running,
                    reason=f"checkpoint_cleared:{cp.downstream_task_id}",
                )
                changed = True
            else:
                if self.run.status != RunStatus.awaiting_user_checkpoint:
                    self._set_status(
                        RunStatus.awaiting_user_checkpoint,
                        reason=f"await_checkpoint:{cp.downstream_task_id}",
                    )
                    changed = True
        if cleared_payload is not None:
            self._emit_event(
                "task_checkpoint_cleared",
                task_id=cleared_task_id,
                payload=cleared_payload,
            )
            return False, True
        return True, changed

    async def _refresh_checkpoint_outputs(self) -> bool:
        """Refresh upstream summaries while awaiting manual checkpoint review."""
        async with self._checkpoint_lock:
            cp = self._dispatch_checkpoint
            if cp is None:
                return False
            tracked = [
                (dep_id, item.owner_agent_id)
                for dep_id, item in cp.items.items()
            ]
            downstream_task_id = cp.downstream_task_id
        reports = await self._fetch_leader_inbox_structured()
        changed = False
        refreshed_payload: dict[str, Any] | None = None
        async with self._checkpoint_lock:
            cp = self._dispatch_checkpoint
            if cp is None:
                return False
            now = datetime.now(timezone.utc)
            for dep_id, owner_agent_id in tracked:
                item = cp.items.get(dep_id)
                if item is None:
                    continue
                latest = self._collect_upstream_task_report_entry(
                    reports,
                    owner_agent_id=owner_agent_id,
                    task_id=dep_id,
                )
                if latest is None:
                    continue
                latest_summary = self._render_report_summary(latest)
                if latest_summary is None:
                    continue
                latest_ts = (latest.timestamp or "").strip() or None
                if (
                    item.decision == "rerun_requested"
                    and item.rerun_requested_at is not None
                    and self._report_timestamp_is_older_than(
                        report_timestamp=latest_ts,
                        boundary=item.rerun_requested_at,
                    )
                ):
                    # Guard against stale inbox rows arriving after rerun request.
                    continue
                summary_changed = latest_summary != item.summary
                rerun_newer_timestamp = (
                    item.decision == "rerun_requested"
                    and self._is_report_timestamp_newer(
                        current=item.last_report_timestamp,
                        incoming=latest_ts,
                    )
                )
                if not summary_changed and not rerun_newer_timestamp:
                    continue
                item.summary = latest_summary
                item.last_report_timestamp = latest_ts
                # After a rerun produces new output, require explicit re-approval.
                if item.decision == "rerun_requested":
                    item.decision = "pending"
                    item.has_unread_update = True
                    item.rerun_requested_at = None
                item.updated_at = now
                cp.updated_at = now
                changed = True
            if changed:
                refreshed_payload = self._checkpoint_payload(cp)
        if changed and refreshed_payload is not None:
            self._emit_event(
                "task_checkpoint_updated",
                task_id=downstream_task_id,
                payload={
                    **refreshed_payload,
                    "decision": "upstream_output_refreshed",
                },
            )
        return changed

    async def _open_dispatch_checkpoint(
        self,
        *,
        downstream_task: FlowTask,
        checkpoint_task: FlowTask,
    ) -> bool:
        """Open a post-task checkpoint for *checkpoint_task*.

        The checkpoint is opened because *downstream_task* is waiting on
        this upstream dependency to be manually approved.
        """
        if not self._task_requires_manual_checkpoint(checkpoint_task):
            return False
        if checkpoint_task.id in self._checkpoint_passed_tasks:
            return False
        async with self._checkpoint_lock:
            if self._dispatch_checkpoint is not None:
                return True
        inbox = await self._fetch_leader_inbox_structured()
        dep_owner = self._agents.get(checkpoint_task.owner_agent_id)
        owner_agent_id = dep_owner.id if dep_owner is not None else checkpoint_task.owner_agent_id
        dep_sess = self._sessions.get(checkpoint_task.owner_agent_id)
        wt_info = dep_sess.worktree if dep_sess is not None else None
        matched = self._collect_upstream_task_report_entry(
            inbox,
            owner_agent_id=owner_agent_id,
            task_id=checkpoint_task.id,
        )
        item = _CheckpointItem(
            task_id=checkpoint_task.id,
            subject=checkpoint_task.subject,
            owner_agent_id=owner_agent_id,
            summary=self._render_report_summary(matched),
            worktree_path=str(wt_info.worktree_path) if wt_info else None,
            branch_name=wt_info.branch_name if wt_info else None,
            base_branch=wt_info.base_branch if wt_info else None,
            last_report_timestamp=(matched.timestamp or "").strip() or None
            if matched is not None
            else None,
        )
        items = {checkpoint_task.id: item}
        order = [checkpoint_task.id]
        payload: dict[str, Any]
        async with self._checkpoint_lock:
            if self._dispatch_checkpoint is not None:
                return True
            cp = _DispatchCheckpoint(
                downstream_task_id=downstream_task.id,
                downstream_subject=downstream_task.subject,
                downstream_owner_agent_id=downstream_task.owner_agent_id,
                item_order=order,
                items=items,
            )
            self._dispatch_checkpoint = cp
            self._set_status(
                RunStatus.awaiting_user_checkpoint,
                reason=f"checkpoint_opened:{downstream_task.id}",
            )
            payload = self._checkpoint_payload(cp)
        self._emit_event(
            "task_checkpoint_waiting",
            task_id=downstream_task.id,
            payload={
                **payload,
                "decision": "checkpoint_opened",
                "checkpoint_task_id": checkpoint_task.id,
            },
        )
        return True

    def _ready_tasks(self) -> list[_TaskBook]:
        """Snapshot-ready (`pending`) tasks whose owner is not currently Busy.

        Per plan §8.4: if a single agent owns multiple ready tasks (or is
        still Busy on a previous one), only the first eligible task is
        dispatched in this tick — the rest wait for the session to flip
        Idle. Sessions still in Spawning are skipped for this tick so one
        slow startup never blocks other owners' dispatch. This keeps the
        WorkerSession state machine sound (Busy → Busy is illegal) and
        matches plan's owner gating semantics.
        """
        ready: list[_TaskBook] = []
        owners_already_dispatched: set[str] = set()
        # Source-of-truth alignment: if ClawTeam already reports one task
        # in-progress for an owner (mirrored as ``dispatched`` locally),
        # never dispatch another task for that owner in this tick.
        owners_with_active_task: set[str] = {
            b.task.owner_agent_id
            for b in self._tasks.values()
            if b.state == _TaskState.in_progress
        }
        for book in self._tasks.values():
            if book.state != _TaskState.pending:
                continue
            # Leader-summary gate: the summary must be the LAST thing dispatched.
            # It waits until every non-summary task is completed, even tasks it
            # does not explicitly depend on. ``depends_on`` on the summary now
            # only selects which worker outputs feed its review/report (see
            # ``_compose_dispatch_context``); it no longer governs *when* the
            # summary runs. This restores the "summary runs after everyone"
            # scheduling guarantee independent of the (possibly partial) dep set.
            if (
                self._leader_summary_task_id is not None
                and book.task.id == self._leader_summary_task_id
                and not self._all_non_summary_tasks_completed()
            ):
                continue
            owner = book.task.owner_agent_id
            if owner in owners_with_active_task:
                continue
            sess = self._sessions.get(owner)
            if sess is not None and sess.state in {SessionState.Busy, SessionState.Spawning}:
                continue  # let next tick try again
            if owner in owners_already_dispatched:
                continue  # one task per owner per tick
            ready.append(book)
            owners_already_dispatched.add(owner)
        return ready

    def _all_non_summary_tasks_completed(self) -> bool:
        """True when every non-summary task has reached ``completed``.

        Used to gate leader-summary dispatch: the summary must not run until all
        worker tasks are done. Workers mark their ClawTeam task ``completed``
        even on failure (see the failure block in the dispatch prompt), so this
        becomes true on the natural-completion path and the summary can report
        on failures too. Vacuously true when there are no non-summary tasks.
        """
        return all(
            book.state == _TaskState.completed
            for task_id, book in self._tasks.items()
            if task_id != self._leader_summary_task_id
        )

    def _partition_ready_tasks_for_checkpoint(
        self,
        ready: list[_TaskBook],
    ) -> tuple[list[_TaskBook], tuple[_TaskBook, _TaskBook] | None]:
        """Split ready tasks into dispatchable and checkpoint-blocked groups.

        Returns:
            (dispatchable_ready, checkpoint_target)
            - dispatchable_ready: tasks that can be dispatched now.
            - checkpoint_target: (downstream_task, upstream_checkpoint_task)
              that should open a post-task checkpoint before any dispatch.
        """
        dispatchable: list[_TaskBook] = []
        checkpoint_target: tuple[_TaskBook, _TaskBook] | None = None

        for book in ready:
            blocked_by_checkpoint = False
            for dep_id in book.task.depends_on:
                dep_book = self._tasks.get(dep_id)
                if dep_book is None:
                    continue
                dep_task = dep_book.task
                if not self._task_requires_manual_checkpoint(dep_task):
                    continue
                if dep_task.id in self._checkpoint_passed_tasks:
                    continue
                blocked_by_checkpoint = True
                if checkpoint_target is None and dep_book.state == _TaskState.completed:
                    checkpoint_target = (book, dep_book)
                break
            if not blocked_by_checkpoint:
                dispatchable.append(book)

        return dispatchable, checkpoint_target

    async def _dispatch_one(self, book: _TaskBook) -> None:
        agent = self._agents[book.task.owner_agent_id]
        session = await self._ensure_session_idle(agent)

        ctx = await self._compose_dispatch_context(agent, book.task)
        message = self._render_dispatch_message(agent, book.task, ctx)

        before = session.state.value
        outcome: DispatchOutcome = await session.dispatch(
            task_id=book.task.id, message=message,
        )
        after = session.state.value
        task_dispatched(
            task_id=book.task.id,
            decision="live_inject" if before == SessionState.Idle.value else "fresh_spawn",
            session_state_before=before,
            session_state_after=after,
        )
        first_outcome = outcome
        if not outcome.success and self._is_tmux_target_not_found_failure(outcome):
            recovered = await self._retry_dispatch_after_tmux_target_missing(
                agent=agent,
                session=session,
                task_id=book.task.id,
                message=message,
                first_failure=first_outcome,
            )
            if recovered is not None:
                session, outcome, after = recovered
                if outcome.success:
                    self._emit_event(
                        "task_dispatch_recovered",
                        agent_id=agent.id,
                        task_id=book.task.id,
                        payload={
                            "reason": "tmux_target_not_found",
                            "initial_failure": self._dispatch_failure_payload(first_outcome),
                        },
                    )
        if outcome.success:
            self._last_dispatch_failures.pop(agent.id, None)
            synced = await self._update_clawteam_task_status(
                book.task.id,
                status="in_progress",
                caller=agent.id or "csflow-scheduler",
                force=True,
            )
            if not synced:
                self._emit_event(
                    "task_status_sync_failed",
                    agent_id=agent.id,
                    task_id=book.task.id,
                    payload={"target_status": "in_progress"},
                )
            book.state = _TaskState.in_progress
            book.dispatched_at = outcome.dispatched_at
            book.last_dispatch_message = message
            self.dispatch_clock.mark(book.task.id, outcome.dispatched_at)
            self._emit_event(
                "task_dispatched",
                agent_id=agent.id, task_id=book.task.id,
                payload={"decision": "dispatch", "session_state": after},
            )
        else:
            # Dispatch itself failed; let failure detection pick it up next tick.
            payload = self._dispatch_failure_payload(outcome)
            if first_outcome is not outcome:
                payload["initial_failure"] = self._dispatch_failure_payload(first_outcome)
            self._last_dispatch_failures[agent.id] = {
                **payload,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.warning(
                "dispatch_outcome_failure",
                task_id=book.task.id,
                agent_id=agent.id,
                detail=outcome.detail,
                error_type=outcome.error_type,
                exit_code=outcome.exit_code,
            )
            self._emit_event(
                "task_dispatch_failed",
                agent_id=agent.id,
                task_id=book.task.id,
                payload=payload,
            )

    def _dispatch_failure_payload(self, outcome: DispatchOutcome) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "detail": (outcome.detail or "")[:2000],
        }
        if outcome.error_type:
            payload["error_type"] = outcome.error_type
        if outcome.exit_code is not None:
            payload["exit_code"] = outcome.exit_code
        if outcome.stderr:
            payload["stderr_tail"] = outcome.stderr[-2000:]
        if outcome.stdout:
            payload["stdout_tail"] = outcome.stdout[-2000:]
        if outcome.argv:
            payload["command"] = " ".join(shlex.quote(a) for a in outcome.argv)[:2000]
        return payload

    def _is_tmux_target_not_found_failure(self, outcome: DispatchOutcome) -> bool:
        text = "\n".join(
            part for part in [outcome.detail, outcome.stderr, outcome.stdout] if part
        )
        if not text.strip():
            return False
        return _TMUX_TARGET_NOT_FOUND_RE.search(text) is not None

    async def _retry_dispatch_after_tmux_target_missing(
        self,
        *,
        agent: FlowAgent,
        session: WorkerSession,
        task_id: str,
        message: str,
        first_failure: DispatchOutcome,
    ) -> tuple[WorkerSession, DispatchOutcome, str] | None:
        self._emit_event(
            "session_recovering_after_dispatch_failure",
            agent_id=agent.id,
            task_id=task_id,
            payload={
                "reason": "tmux_target_not_found",
                "failure": self._dispatch_failure_payload(first_failure),
            },
        )
        try:
            session.mark_crashed(reason="tmux_target_not_found")
        except Exception:
            # Stale handle; force recreation through _ensure_session_idle.
            self._sessions.pop(agent.id, None)
        try:
            session = await self._ensure_session_idle(agent)
        except Exception as exc:
            self._emit_event(
                "session_recover_failed",
                agent_id=agent.id,
                task_id=task_id,
                payload={
                    "reason": "tmux_target_not_found",
                    "error": str(exc)[:1000],
                },
            )
            logger.warning(
                "session_recover_after_dispatch_failed",
                agent_id=agent.id,
                task_id=task_id,
                error=str(exc),
            )
            return None
        retry_before = session.state.value
        retry_outcome = await session.dispatch(task_id=task_id, message=message)
        retry_after = session.state.value
        task_dispatched(
            task_id=task_id,
            decision="recovered_live_inject",
            session_state_before=retry_before,
            session_state_after=retry_after,
        )
        return session, retry_outcome, retry_after

    async def _runtime_socket_error_recovery_tick(self) -> bool:
        """Detect runtime socket-closure errors and requeue affected tasks.

        Some TUI runtimes can die back to prompt with:
        ``API Error: The socket connection was closed unexpectedly``.
        In that case the task may remain ``in_progress`` forever unless we
        proactively mark the session crashed and redispatch.
        """
        changed = False
        now_epoch = time.time()
        for book in self._tasks.values():
            if book.state != _TaskState.in_progress:
                continue
            agent_id = book.task.owner_agent_id
            sess = self._sessions.get(agent_id)
            elapsed = self._runtime_socket_recovery_elapsed_sec(
                book=book,
                now_epoch=now_epoch,
            )
            if elapsed is None:
                continue
            if not self._runtime_socket_recovery_eligible(
                book=book,
                sess=sess,
                elapsed_sec=elapsed,
            ):
                continue
            assert sess is not None  # narrowed by eligibility check
            pane_text = await self._safe_capture_pane_tail(sess=sess)
            if not pane_text:
                continue
            pane_tail = pane_text[-_RUNTIME_SOCKET_TAIL_CHARS:]
            if _RUNTIME_SOCKET_CLOSED_RE.search(pane_tail) is None:
                continue
            prompt_visible = _RUNTIME_ERROR_PROMPT_RE.search(pane_tail) is not None
            if not prompt_visible and elapsed < _RUNTIME_SOCKET_PROMPTLESS_RECOVERY_SEC:
                continue

            synced = await self._reset_clawteam_task(book.task.id, locked_by=agent_id)
            if not synced:
                self._emit_event(
                    "task_status_sync_failed",
                    agent_id=agent_id,
                    task_id=book.task.id,
                    payload={"target_status": "pending"},
                )
                continue

            sess.mark_crashed(reason="runtime_socket_closed")
            book.runtime_socket_recoveries += 1
            book.state = _TaskState.pending
            book.dispatched_at = None
            self.dispatch_clock.reset(book.task.id)
            changed = True
            self._emit_event(
                "task_runtime_socket_recovered",
                agent_id=agent_id,
                task_id=book.task.id,
                payload={
                    "reason": "socket_connection_closed_unexpectedly",
                    "elapsed_sec": int(elapsed),
                    "prompt_visible": prompt_visible,
                    "recovery_count": book.runtime_socket_recoveries,
                    "recovery_limit": _RUNTIME_SOCKET_RECOVERY_LIMIT,
                    "pane_tail": pane_tail[-_RUNTIME_SOCKET_TAIL_CHARS:],
                },
            )
            logger.warning(
                "runtime_socket_error_recovered",
                run_id=self.run.id,
                task_id=book.task.id,
                agent_id=agent_id,
                elapsed_sec=int(elapsed),
                recovery_count=book.runtime_socket_recoveries,
            )
        return changed

    def _runtime_socket_recovery_elapsed_sec(
        self,
        *,
        book: _TaskBook,
        now_epoch: float,
    ) -> float | None:
        dispatched_at = book.dispatched_at
        if dispatched_at is None:
            dispatched_at = self.dispatch_clock.table.get(book.task.id)
        if dispatched_at is None:
            return None
        return max(now_epoch - dispatched_at, 0.0)

    def _runtime_socket_recovery_eligible(
        self,
        *,
        book: _TaskBook,
        sess: WorkerSession | None,
        elapsed_sec: float,
    ) -> bool:
        if sess is None:
            return False
        if sess.state != SessionState.Busy:
            return False
        if elapsed_sec < _RUNTIME_SOCKET_MIN_ELAPSED_SEC:
            return False
        if book.runtime_socket_recoveries >= _RUNTIME_SOCKET_RECOVERY_LIMIT:
            return False
        return True

    async def _safe_capture_pane_tail(self, *, sess: WorkerSession) -> str:
        try:
            return await tmux_capture_pane(
                sess.tmux_target,
                history_lines=_RUNTIME_SOCKET_CAPTURE_LINES,
            )
        except Exception as exc:
            logger.debug(
                "runtime_socket_pane_capture_failed",
                run_id=self.run.id,
                agent_id=sess.agent.id,
                tmux_target=sess.tmux_target,
                error=str(exc),
            )
            return ""

    def _clawteam_task_id(self, flow_task_id: str) -> str | None:
        if self.compile_result is None:
            return None
        return self.compile_result.flow_to_clawteam.get(flow_task_id)

    async def _update_clawteam_task_status(
        self,
        flow_task_id: str,
        *,
        status: str,
        caller: str,
        force: bool = True,
    ) -> bool:
        """Update ClawTeam task status before mutating local mirror state."""
        ct_id = self._clawteam_task_id(flow_task_id)
        if ct_id is None:
            # Tests may run without compile_result wiring.
            return True
        try:
            from app.integrations.clawteam_mcp import get_mcp_client

            mcp = await get_mcp_client(user=self.run.user)
            await mcp.task_update(
                team_name=self.team_name,
                task_id=ct_id,
                status=status,
                caller=caller or "csflow-scheduler",
                force=force,
            )
            return True
        except Exception as exc:
            logger.warning(
                "task_status_sync_failed",
                flow_task_id=flow_task_id,
                clawteam_task_id=ct_id,
                target_status=status,
                error=str(exc),
            )
            return False

    async def build_self_merge_dispatch(self, agent_id: str, task: FlowTask) -> str:
        """[deprecated escape hatch — see :func:`build_openclaw_self_merge`].

        Canonical design now dispatches merge requirements during
        complaint/satisfaction stage; no regular task prompt should carry
        merge steps. Kept callable so recovery paths can still build an
        explicit merge prompt when needed.
        """
        agent = self._agents[agent_id]
        ctx = await self._compose_dispatch_context(agent, task)
        return build_openclaw_self_merge(ctx)

    # ── snapshots + state sync ───────────────────────────────────────

    async def _fetch_snapshots(self) -> list[TaskSnapshot]:
        if self._snapshot_provider is not None:
            try:
                data = await self._snapshot_provider()
            except Exception as exc:
                logger.warning("snapshot_provider_failed", error=str(exc))
                self._emit_event(
                    "snapshot_fetch_failed",
                    payload={"error": str(exc)[:1000]},
                )
                return []
            return list(data)
        # In-process MCP client wired up in Phase 6 (here only return [] so
        # tests using snapshot_provider work and prod calls fall back gracefully
        # until the controller is fully wired into FlowScheduler).
        return []

    async def _fetch_leader_inbox_raw(self) -> list[Any]:
        """Fetch the leader inbox payload, memoized for the current tick.

        ``mailbox_peek`` is non-consuming, so multiple readers inside one
        tick (failure detection → checkpoint refresh → dispatch context)
        would see near-identical data anyway; the memo just removes the
        duplicate RPCs. Outside a tick (complaint phase, terminal snapshot
        flush) the cache is disabled and every call peeks fresh.
        """
        if self._in_tick and self._leader_inbox_tick_cache is not None:
            return self._leader_inbox_tick_cache
        assert self._leader_inbox_provider is not None
        raw = list(await self._leader_inbox_provider())
        if self._in_tick:
            self._leader_inbox_tick_cache = raw
        return raw

    async def _fetch_leader_inbox(self) -> list[str]:
        """Return the leader's inbox messages as raw strings.

        Used by the failure detector (which only needs the text body to
        scan for ``FAILED:`` markers). Leader-summary dispatch uses
        :meth:`_fetch_leader_inbox_structured` to keep author + task ids.
        """
        if self._leader_inbox_provider is not None:
            try:
                raw = await self._fetch_leader_inbox_raw()
            except Exception as exc:
                logger.warning("leader_inbox_provider_failed", error=str(exc))
                self._emit_event(
                    "leader_inbox_fetch_failed",
                    payload={"error": str(exc)[:1000]},
                )
                return []
            reports = self._normalize_worker_reports(list(raw))
            self._record_worker_reports(reports)
            return [r.summary for r in reports if r.summary]
        return []

    async def _fetch_leader_inbox_structured(self) -> list[WorkerReport]:
        """Like :meth:`_fetch_leader_inbox` but preserves ``from_agent``/``task_id``.

        Returns ``WorkerReport`` rows derived from MCP inbox payload entries.
        Task association is resolved in this order:
        1) explicit ``task_id`` / ``taskId`` fields;
        2) ``last_task`` / ``lastTask`` fields;
        3) summary prefix parse from content: ``task <id> done: ...``.
        Falls back to raw text parsing when the provider returns plain strings
        (test path / legacy DI).
        """
        if self._leader_inbox_provider is None:
            return []
        try:
            raw = await self._fetch_leader_inbox_raw()
        except Exception as exc:
            logger.warning("leader_inbox_provider_failed_structured", error=str(exc))
            self._emit_event(
                "leader_inbox_fetch_failed",
                payload={"error": str(exc)[:1000]},
            )
            return []
        out = self._normalize_worker_reports(list(raw))
        self._record_worker_reports(out)
        return out

    def _normalize_worker_reports(
        self, raw: list[str | dict[str, Any]],
    ) -> list[WorkerReport]:
        out: list[WorkerReport] = []
        for entry in raw:
            if isinstance(entry, dict):
                text = str(entry.get("content") or entry.get("body") or "")
                out.append(WorkerReport(
                    from_agent=str(entry.get("from_agent") or entry.get("from") or "?"),
                    summary=text,
                    task_id=self._extract_task_ref(
                        payload_task_id=(
                            entry.get("task_id")
                            or entry.get("taskId")
                            or entry.get("last_task")
                            or entry.get("lastTask")
                        ),
                        text=text,
                    ),
                    timestamp=str(entry.get("timestamp") or entry.get("ts") or "") or None,
                ))
                continue
            # Plain string: best-effort parse "task <id> done: ..." prefix.
            text = str(entry)
            tid = self._extract_task_ref(payload_task_id=None, text=text)
            out.append(WorkerReport(
                from_agent="?", summary=text, task_id=tid, timestamp=None,
            ))
        return out

    def _extract_task_ref(
        self, *, payload_task_id: Any, text: str,
    ) -> str | None:
        raw = str(payload_task_id or "").strip()
        if raw:
            return raw
        m = _TASK_PREFIX_RE.match(text or "")
        if not m:
            return None
        return m.group(1)

    def _record_worker_reports(self, reports: list[WorkerReport]) -> None:
        """Persist in-memory worker output history for terminal snapshots."""
        if not reports:
            return
        ts = datetime.now(timezone.utc).isoformat()
        for r in reports:
            summary = (r.summary or "").strip()
            key = (r.task_id, r.from_agent, summary)
            if key in self._seen_worker_report_keys:
                continue
            self._seen_worker_report_keys.add(key)
            item = {
                "task_id": r.task_id,
                "from_agent": r.from_agent,
                "summary": summary,
                "collected_at": ts,
            }
            self._worker_report_history.append(item)
            if r.task_id:
                self._task_outputs.setdefault(r.task_id, []).append(item)
                # Durable hand-off for the Run board edge tooltip (survives
                # after the run ends; also available mid-run via WS/events).
                self._emit_event(
                    "task_inbox_handoff",
                    agent_id=r.from_agent if r.from_agent != "?" else None,
                    task_id=r.task_id,
                    payload={
                        "fromAgent": r.from_agent,
                        "summary": summary[:4000],
                        "collectedAt": ts,
                    },
                )
            self._emit_worker_exit_observed_event(report=r, summary=summary)

    def _emit_worker_exit_observed_event(self, *, report: WorkerReport, summary: str) -> None:
        """Persist worker exit diagnostics from leader inbox reports."""
        m = _AGENT_EXIT_REPORT_RE.search(summary or "")
        if not m:
            return
        agent_id = (m.group("agent") or "").strip()
        if not agent_id:
            return
        journal = self._read_latest_exit_journal_entry(agent_id=agent_id) or {}
        payload: dict[str, Any] = {
            "reported_by": report.from_agent,
            "report_task_id": report.task_id,
            "report_summary": summary[:1000],
            "exit_code": journal.get("exit_code"),
            "abandoned_tasks": journal.get("abandoned_tasks") or [],
            "exit_timestamp": journal.get("timestamp"),
        }
        stderr_raw = journal.get("stderr_tail") or journal.get("stderr")
        if isinstance(stderr_raw, str) and stderr_raw.strip():
            payload["stderr_tail"] = stderr_raw[-2000:]
        stdout_raw = journal.get("stdout_tail") or journal.get("stdout")
        if isinstance(stdout_raw, str) and stdout_raw.strip():
            payload["stdout_tail"] = stdout_raw[-2000:]
        last_failure = self._last_dispatch_failures.get(agent_id)
        if last_failure:
            payload["last_dispatch_failure"] = dict(last_failure)
        self._emit_event(
            "worker_process_exit_observed",
            agent_id=agent_id,
            task_id=report.task_id,
            payload=payload,
        )

    def _read_latest_exit_journal_entry(self, *, agent_id: str) -> dict[str, Any] | None:
        data_root = self._clawteam_data_dir()
        path = data_root / "harness" / self.team_name / "exit-journal.jsonl"
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning(
                "exit_journal_read_failed",
                team=self.team_name,
                path=str(path),
                error=str(exc),
            )
            return None
        for line in reversed(lines):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError:
                continue
            if str(row.get("agent_name") or "") != agent_id:
                continue
            out = dict(row)
            exit_code_raw = out.get("exit_code")
            if isinstance(exit_code_raw, str):
                try:
                    out["exit_code"] = int(exit_code_raw.strip())
                except ValueError:
                    out["exit_code"] = exit_code_raw
            return out
        return None

    def _clawteam_data_dir(self) -> Path:
        cfg = load_config()
        if cfg.clawteam_data_dir:
            return Path(cfg.clawteam_data_dir).expanduser()
        return Path.home() / ".clawteam"

    def _apply_snapshots(self, snapshots: list[TaskSnapshot]) -> bool:
        """Sync ClawTeam status into our scheduler bookkeeping.

        Side-effects on ``completed`` transition:
        * Free the owning :class:`WorkerSession` (Busy → Idle).
        * For OpenClaw owners, schedule a :func:`run_post_task_audit`
          (DEV.md §9 layer 3) — runs as a fire-and-forget asyncio task so
          the controller loop doesn't block on git subprocesses.
        """
        changed = False
        audit_targets: list[tuple[FlowAgent, FlowTask]] = []
        for s in snapshots:
            book = self._tasks.get(s.task_id)
            if book is None:
                continue
            old = book.state
            new = old
            status = (s.status or "").strip().lower()
            # Keep scheduler status aligned with ClawTeam status as much as possible.
            if status == "completed":
                new = _TaskState.completed
            elif status in {"in_progress"}:
                new = _TaskState.in_progress
            elif status == "pending":
                new = _TaskState.pending
            elif status == "blocked":
                new = _TaskState.blocked

            agent_id = book.task.owner_agent_id
            if new == _TaskState.completed and old != _TaskState.completed:
                # Free the session.
                if agent_id in self._sessions:
                    self._sessions[agent_id].mark_idle(reason=f"task_{s.task_id}_done")
                # Stop dispatch-clock tracking for this task.
                self.dispatch_clock.reset(s.task_id)
                book.dispatched_at = None
                # Queue an OpenClaw audit (deduped).
                agent = self._agents.get(agent_id)
                if (
                    agent is not None and agent.kind == AgentKind.openclaw
                    and s.task_id not in self._completed_audited
                ):
                    audit_targets.append((agent, book.task))
                    self._completed_audited.add(s.task_id)
            elif new == _TaskState.pending:
                # ClawTeam says task is pending, so there is no active dispatch
                # in flight. Clear local dispatch tracking to avoid waiting for
                # stale timeout windows.
                self.dispatch_clock.reset(s.task_id)
                book.dispatched_at = None
                s.dispatched_at_epoch = None
                # If we previously thought this owner was busy on this task,
                # release local busy state so the task can be redispatched.
                if old == _TaskState.in_progress and agent_id in self._sessions:
                    self._sessions[agent_id].mark_idle(reason=f"task_{s.task_id}_requeued")
            elif new == _TaskState.blocked:
                # Blocked means not dispatchable yet; clear stale dispatch
                # bookkeeping and ensure owner is not held Busy.
                self.dispatch_clock.reset(s.task_id)
                book.dispatched_at = None
                s.dispatched_at_epoch = None
                if old == _TaskState.in_progress and agent_id in self._sessions:
                    self._sessions[agent_id].mark_idle(reason=f"task_{s.task_id}_blocked")
            elif new == _TaskState.in_progress:
                # Snapshot-driven in_progress (including controller restart /
                # external reset races): ensure timeout bookkeeping exists.
                if s.dispatched_at_epoch is not None:
                    book.dispatched_at = s.dispatched_at_epoch
                    self.dispatch_clock.mark(s.task_id, s.dispatched_at_epoch)
                elif s.task_id not in self.dispatch_clock.table:
                    now_epoch = time.time()
                    book.dispatched_at = now_epoch
                    self.dispatch_clock.mark(s.task_id, now_epoch)
            if new != old:
                changed = True
                book.state = new
                task_state_transition(
                    task_id=s.task_id, old=old.value, new=new.value,
                )
                self._emit_event(
                    "task_completed" if new == _TaskState.completed else "task_state",
                    agent_id=book.task.owner_agent_id, task_id=s.task_id,
                    payload={"old": old.value, "new": new.value},
                )
        # Fire-and-forget audits — they never block the loop.
        for agent, ftask in audit_targets:
            asyncio.create_task(self._run_audit(agent, ftask))
        return changed

    async def _reset_clawteam_task(
        self, flow_task_id: str, *, locked_by: str | None,
    ) -> bool:
        """Force a ClawTeam task back to ``pending`` so the controller can
        redispatch it (plan §8.7 retry path).

        Looks up the ClawTeam id via ``compile_result.flow_to_clawteam``;
        if compilation hasn't happened yet (e.g. tests injecting their own
        snapshot provider), no-op so the in-memory state is still consistent.
        Failures are swallowed — failure detection will fire again next tick
        if the reset didn't take.
        """
        ok = await self._update_clawteam_task_status(
            flow_task_id,
            status="pending",
            caller=locked_by or "csflow-scheduler",
            force=True,
        )
        if not ok:
            logger.warning("retry_reset_failed", flow_task_id=flow_task_id)
        return ok

    async def _mark_clawteam_task_blocked(
        self,
        flow_task_id: str,
        *,
        caller: str,
    ) -> bool:
        ok = await self._update_clawteam_task_status(
            flow_task_id,
            status="blocked",
            caller=caller or "csflow-scheduler",
            force=True,
        )
        if not ok:
            logger.warning("failure_block_sync_failed", flow_task_id=flow_task_id)
        return ok

    async def _run_audit(self, agent: FlowAgent, task: FlowTask) -> None:
        """Best-effort post-task audit hook (OpenClaw only)."""
        sess = self._sessions.get(agent.id)
        worktree_path = sess.worktree.worktree_path if (sess and sess.worktree) else None
        # OpenClaw main repo = ~/.clawsomeflow/agents/{id}/workspace/
        main_workspace = self._openclaw_main_repo(agent)
        try:
            await run_post_task_audit(
                agent=agent, task=task,
                main_workspace=main_workspace,
                worktree_path=worktree_path,
                storage=self.storage,
                run_id=self.run.id,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "post_task_audit_failed",
                agent_id=agent.id, task_id=task.id, error=str(exc),
            )

    # ── failure handling ─────────────────────────────────────────────

    async def _handle_failure(self, rec: FailureRecord) -> None:
        book = self._tasks.get(rec.task_id)
        if book is None or book.state == _TaskState.completed:
            return
        agent = self._agents.get(rec.agent_id) or self._agents.get(book.task.owner_agent_id)
        if agent is None:
            return
        decision = apply_on_failure(
            record=rec, agent=agent, current_retry_count=book.retries,
        )
        book.last_failure = rec
        # User-visible failure diagnosis event (also persisted in RunEvent stream)
        # so transient failures (that may later retry successfully) are still
        # visible in UI/history with concrete reason + detail.
        self._emit_event(
            "task_failure_detected",
            agent_id=agent.id,
            task_id=book.task.id,
            payload={
                "reason": rec.reason.value,
                "detail": rec.detail,
                "on_failure": agent.on_failure.value,
                "retry_count": book.retries,
                "max_retries": agent.max_retries,
            },
        )
        if decision.action == "retry":
            book.retries = decision.new_retry_count
            synced = await self._reset_clawteam_task(book.task.id, locked_by=rec.agent_id)
            if not synced:
                self._emit_event(
                    "task_status_sync_failed",
                    agent_id=agent.id,
                    task_id=book.task.id,
                    payload={"target_status": "pending"},
                )
            book.state = _TaskState.pending
            book.dispatched_at = None
            self.dispatch_clock.reset(book.task.id)
            # Mark session crashed so next tick triggers resume.
            sess = self._sessions.get(agent.id)
            if sess is not None and rec.reason.value == "timeout":
                sess.mark_crashed(reason=rec.reason.value)
            self._emit_event(
                "task_retry", agent_id=agent.id, task_id=book.task.id,
                payload={
                    "reason": rec.reason.value,
                    "detail": rec.detail,
                    "retries": book.retries,
                },
            )
        elif decision.action == "skip":
            synced = await self._mark_clawteam_task_blocked(
                book.task.id, caller=rec.agent_id or agent.id,
            )
            if not synced:
                self._emit_event(
                    "task_status_sync_failed",
                    agent_id=agent.id,
                    task_id=book.task.id,
                    payload={"target_status": "blocked"},
                )
            book.state = _TaskState.blocked
            self._failed_task_ids.add(book.task.id)
            self._emit_event(
                "task_failed", agent_id=agent.id, task_id=book.task.id,
                payload={
                    "reason": rec.reason.value,
                    "detail": rec.detail,
                    "policy": OnFailure.skip.value,
                    "effective_action": "abort",
                },
            )
            self.cancel()
        else:  # abort
            synced = await self._mark_clawteam_task_blocked(
                book.task.id, caller=rec.agent_id or agent.id,
            )
            if not synced:
                self._emit_event(
                    "task_status_sync_failed",
                    agent_id=agent.id,
                    task_id=book.task.id,
                    payload={"target_status": "blocked"},
                )
            book.state = _TaskState.blocked
            self._failed_task_ids.add(book.task.id)
            self._emit_event(
                "task_failed", agent_id=agent.id, task_id=book.task.id,
                payload={"reason": rec.reason.value, "detail": rec.detail},
            )
            # Aborting Run: cancel everything.
            self.cancel()

    # ── session lifecycle ────────────────────────────────────────────

    def _launch_prewarm_tui_sessions(self) -> None:
        if self._cancel_evt.is_set():
            return
        if self._prewarm_task is not None and not self._prewarm_task.done():
            return
        if self._first_dispatch_owner_id is None:
            return
        self._prewarm_task = self._create_task_safe(
            self._prewarm_tui_sessions(),
            name=f"csflow-prewarm-{self.run.id}",
        )

    async def _prewarm_tui_sessions(self) -> None:
        first_owner = self._first_dispatch_owner_id
        if first_owner is None:
            return
        ordered_owner_ids: list[str] = []
        seen: set[str] = set()
        for task in self.spec.tasks:
            owner_id = task.owner_agent_id
            if owner_id == first_owner or owner_id in seen:
                continue
            seen.add(owner_id)
            ordered_owner_ids.append(owner_id)
        targets = [
            self._agents[owner_id]
            for owner_id in ordered_owner_ids
            if owner_id in self._agents
        ]
        if not targets:
            return
        for agent in targets:
            if self._cancel_evt.is_set():
                return
            try:
                startup = self._ensure_startup_task(agent=agent, source="prewarm")
                await startup
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit_event(
                    "session_prewarm_failed",
                    agent_id=agent.id,
                    payload={"error": str(exc)[:1000]},
                )

    def _ensure_session_handle(self, agent: FlowAgent) -> WorkerSession:
        sess = self._sessions.get(agent.id)
        if sess is not None and sess.state == SessionState.Exited:
            # Complaint phase can happen after run_loop tail shutdown; recreate
            # a fresh session object so we can dispatch again safely.
            sess = self._session_factory(agent)
            self._sessions[agent.id] = sess
        if sess is None:
            sess = self._session_factory(agent)
            self._sessions[agent.id] = sess
        return sess

    def _create_task_safe(
        self,
        coro: Any,
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        """Create task and close coroutine object on scheduling failure.

        ``asyncio.create_task`` can raise (e.g. loop already closing). When that
        happens, the raw coroutine object would otherwise be left un-awaited and
        later emit ``RuntimeWarning: coroutine ... was never awaited``.
        """
        try:
            return asyncio.create_task(coro, name=name)
        except Exception:
            close = getattr(coro, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise

    def _ensure_startup_task(self, *, agent: FlowAgent, source: str) -> asyncio.Task[WorkerSession]:
        existing = self._startup_tasks.get(agent.id)
        if existing is not None and not existing.done():
            return existing
        task = self._create_task_safe(
            self._run_startup_sequence(agent=agent, source=source),
            name=f"csflow-startup-{self.run.id}-{agent.id}",
        )
        self._startup_tasks[agent.id] = task

        def _cleanup(done: asyncio.Task[WorkerSession], *, agent_id: str = agent.id) -> None:
            current = self._startup_tasks.get(agent_id)
            if current is done:
                self._startup_tasks.pop(agent_id, None)

        task.add_done_callback(_cleanup)
        return task

    async def _run_startup_sequence(self, *, agent: FlowAgent, source: str) -> WorkerSession:
        sess = self._ensure_session_handle(agent)
        try:
            if sess.state == SessionState.Spawning:
                await self._wait_for_spawning_settle(sess=sess)
                sess = self._ensure_session_handle(agent)
            if sess.state == SessionState.Absent:
                reused = await self._try_adopt_existing_openclaw_session(sess)
                if not reused:
                    sess.set_tmux_target_override(None)
                    try:
                        await sess.spawn()
                    except Exception as exc:
                        raise SessionStartupError(
                            agent_id=agent.id,
                            phase="spawn",
                            detail=str(exc),
                        ) from exc
                try:
                    await self._refresh_worktree(sess)
                except Exception as exc:
                    raise SessionStartupError(
                        agent_id=agent.id,
                        phase="worktree_refresh_after_spawn",
                        detail=str(exc),
                    ) from exc
            elif sess.state == SessionState.Crashed:
                sess.set_tmux_target_override(None)
                if sess.worktree is None:
                    # The initial spawn never produced a worktree (e.g. prewarm
                    # spawn failed before _refresh_worktree ran). Resume is
                    # impossible — it hard-fails with "cannot resume without
                    # recorded worktree" — so do a FRESH spawn, which recreates
                    # the worktree, instead of dead-ending the task.
                    try:
                        await sess.spawn()
                    except Exception as exc:
                        raise SessionStartupError(
                            agent_id=agent.id,
                            phase="spawn_after_crash_without_worktree",
                            detail=str(exc),
                        ) from exc
                else:
                    try:
                        await sess.resume()
                    except Exception as exc:
                        raise SessionStartupError(
                            agent_id=agent.id,
                            phase="resume",
                            detail=str(exc),
                        ) from exc
                try:
                    await self._refresh_worktree(sess)
                except Exception as exc:
                    raise SessionStartupError(
                        agent_id=agent.id,
                        phase="worktree_refresh_after_resume",
                        detail=str(exc),
                    ) from exc
            return sess
        except asyncio.CancelledError:
            if sess.state == SessionState.Spawning:
                try:
                    sess.mark_crashed(reason=f"startup_cancelled:{source}")
                except Exception:
                    pass
            raise

    async def _wait_for_spawning_settle(self, *, sess: WorkerSession, timeout_sec: float = 120.0) -> None:
        if sess.state != SessionState.Spawning:
            return
        deadline = time.monotonic() + timeout_sec
        while sess.state == SessionState.Spawning:
            if time.monotonic() >= deadline:
                try:
                    sess.mark_crashed(reason="spawn_wait_timeout")
                except Exception:
                    pass
                raise SessionStartupError(
                    agent_id=sess.agent.id,
                    phase="spawn_wait",
                    detail=f"session remained spawning for {timeout_sec:.1f}s",
                )
            await asyncio.sleep(0.05)

    async def _cancel_background_startups(self) -> None:
        pending: list[asyncio.Task[Any]] = []
        if self._prewarm_task is not None and not self._prewarm_task.done():
            self._prewarm_task.cancel()
            pending.append(self._prewarm_task)
        for startup in list(self._startup_tasks.values()):
            if startup.done():
                continue
            startup.cancel()
            pending.append(startup)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._prewarm_task = None
        for agent_id, startup in list(self._startup_tasks.items()):
            if startup.done():
                self._startup_tasks.pop(agent_id, None)

    async def _ensure_session_idle(self, agent: FlowAgent) -> WorkerSession:
        sess = self._ensure_session_handle(agent)
        if sess.state in {SessionState.Absent, SessionState.Crashed, SessionState.Spawning}:
            startup = self._ensure_startup_task(agent=agent, source="ensure_session_idle")
            sess = await startup
        return sess

    async def _try_adopt_existing_openclaw_session(self, sess: WorkerSession) -> bool:
        """Attach to an already-running OpenClaw tmux shell if it exists.

        This avoids duplicate leader windows when complaint processing starts
        after run-loop shutdown or process restart.
        """
        if sess.agent.kind != AgentKind.openclaw:
            return False
        targets = await self._openclaw_tmux_candidate_targets(agent_id=sess.agent.id)
        for target in targets:
            ready = await wait_shell_ready(
                target,
                timeout_sec=0.6,
                poll_interval=0.2,
            )
            if not ready:
                continue
            try:
                sess.set_tmux_target_override(target)
                sess.adopt_existing(reason="existing_tmux_reused")
            except Exception:
                sess.set_tmux_target_override(None)
                return False
            logger.info(
                "openclaw_existing_session_reused",
                run_id=self.run.id,
                team=self.team_name,
                agent_id=sess.agent.id,
                tmux_target=sess.tmux_target,
            )
            self._emit_event(
                "session_reused",
                agent_id=sess.agent.id,
                payload={
                    "reason": "existing_tmux_reused",
                    "tmux_target": sess.tmux_target,
                },
            )
            return True
        sess.set_tmux_target_override(None)
        return False

    async def _openclaw_tmux_candidate_targets(self, *, agent_id: str) -> list[str]:
        """Prefer concrete ``session:index`` targets when duplicate names exist."""
        by_name = f"clawteam-{self.team_name}:{agent_id}"
        session_name = f"clawteam-{self.team_name}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "list-windows",
                "-t",
                session_name,
                "-F",
                "#{window_index}:#{window_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await proc.communicate()
            if proc.returncode != 0:
                return [by_name]
            indexed: list[tuple[int, str]] = []
            text = stdout.decode("utf-8", errors="replace")
            for line in text.splitlines():
                raw = line.strip()
                if not raw:
                    continue
                idx_s, _, name = raw.partition(":")
                if name != agent_id:
                    continue
                try:
                    idx = int(idx_s)
                except ValueError:
                    continue
                indexed.append((idx, name))
            indexed.sort(reverse=True)
            out = [f"{session_name}:{idx}" for idx, _ in indexed]
            if by_name not in out:
                out.append(by_name)
            return out or [by_name]
        except Exception:
            return [by_name]

    async def _refresh_worktree(self, sess: WorkerSession) -> None:
        """Update sess.worktree from ClawTeam after a fresh / resume spawn."""
        if sess.agent.kind == AgentKind.external:
            # External nodes own no worktree — nothing to look up.
            sess.worktree = None
            return
        repo = sess.agent.repo or self._openclaw_main_repo(sess.agent)
        wt = await self.worktree_lookup.get(
            self.team_name, sess.agent.id, repo=repo, force=True,
        )
        sess.worktree = wt

    def _openclaw_main_repo(self, agent: FlowAgent) -> str:
        from app import paths
        return str(paths.agent_dir(agent.id) / "workspace")

    def _default_session_factory(self, agent: FlowAgent) -> WorkerSession:
        if agent.kind == AgentKind.openclaw:
            return OpenClawTmuxSession(
                agent=agent, team_name=self.team_name, run_id=self.run.id,
                agent_main_repo=self._openclaw_main_repo(agent),
            )
        if agent.kind == AgentKind.external:
            return ExternalNodeSession(
                agent=agent, team_name=self.team_name, run_id=self.run.id,
                storage=self.storage,
                package_provider=self._compose_external_package,
            )
        return TmuxLiveSession(
            agent=agent, team_name=self.team_name, run_id=self.run.id,
        )

    async def _compose_external_package(self, task_id: str) -> dict[str, Any]:
        """Structured outbound package for an external-node dispatch.

        Recomposes the DispatchContext (the leader-inbox peek is memoised per
        tick, so this costs no extra RPC within the dispatching tick)."""
        book = self._tasks.get(task_id)
        if book is None:
            raise RuntimeError(f"external package: unknown task {task_id!r}")
        agent = self._agents[book.task.owner_agent_id]
        ctx = await self._compose_dispatch_context(agent, book.task)
        return build_external_task_package(ctx)

    async def _dispatch_complaint_task(
        self,
        *,
        agent: FlowAgent,
        task_id: str,
        message: str,
    ) -> None:
        if agent.kind == AgentKind.openclaw:
            await self._dispatch_openclaw_headless(
                agent=agent,
                task_id=task_id,
                message=message,
                dispatch_kind="complaint",
            )
            return
        if agent.kind == AgentKind.hermes:
            await self._dispatch_hermes_headless(
                agent=agent,
                task_id=task_id,
                message=message,
                dispatch_kind="complaint",
            )
            return
        await self._dispatch_custom_task(agent=agent, task_id=task_id, message=message)

    async def _hermes_dispatch_cwd(self, agent: FlowAgent) -> tuple[str, str]:
        """Resolve the worktree cwd for a headless Hermes complaint dispatch.

        Hermes is non-OpenClaw → worktree at ``~/.clawteam/workspaces/{team}/{agent}``
        (repo = ``FlowAgent.repo``). Recovers the session if the worktree is gone.
        """
        try:
            wt = await self.worktree_lookup.get(
                self.team_name, agent.id, repo=agent.repo, force=True,
            )
        except Exception:
            wt = None
        if wt is not None:
            path = Path(wt.worktree_path)
            if path.exists() and path.is_dir():
                return str(path), "worktree_existing"
        sess = await self._ensure_session_idle(agent)
        await self._refresh_worktree(sess)
        recovered = sess.worktree
        if recovered is not None:
            path = Path(recovered.worktree_path)
            if path.exists() and path.is_dir():
                return str(path), "worktree_created"
        raise RuntimeError(
            f"hermes dispatch worktree not found after recovery: agent={agent.id!r}",
        )

    async def _dispatch_hermes_headless(
        self,
        *,
        agent: FlowAgent,
        task_id: str,
        message: str,
        dispatch_kind: str = "complaint",
    ) -> None:
        executable = shutil.which("hermes")
        if not executable:
            raise RuntimeError("hermes CLI is not available in PATH")
        cwd, cwd_source = await self._hermes_dispatch_cwd(agent)
        message = self._inject_headless_dispatch_context(
            message=message,
            cwd=cwd,
            cwd_source=cwd_source,
            dispatch_kind=dispatch_kind,
            platform="hermes",
        )
        # Bind the executor to its managed Hermes profile (-p) for persistent
        # agents. Complaint headless dispatch always starts a FRESH quiet chat
        # turn — deliberately no session continuation:
        # * ``--resume <id>``: Hermes TUI's displayed "Session:" id is not
        #   accepted (verified against v0.16.0), and we never learn the tmux
        #   REPL's real session id.
        # * ``-c``: resolves the profile's *most recent* CLI-source session,
        #   which is NOT guaranteed to be this run's tmux subtask session (the
        #   same persistent profile may serve concurrent runs, or the operator
        #   may have chatted via ``hermes -p <id>`` in a terminal meanwhile) —
        #   no deterministic way to verify, so resuming risks the wrong
        #   conversation. The complaint prompt is self-contained instead.
        argv = [executable, "chat", "--yolo", "-Q", "-q", message]
        if not agent.is_temporary:
            argv = [
                executable,
                "-p",
                agent.id,
                "chat",
                "--yolo",
                "-Q",
                "-q",
                message,
            ]
        env = os.environ.copy()
        env.update({
            "CLAWTEAM_AGENT_NAME": agent.id,
            "CLAWTEAM_TEAM_NAME": self.team_name,
        })
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        subprocess_registry.register(proc)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_OPENCLAW_HEADLESS_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as exc:
            subprocess_registry.kill_group(proc)
            await proc.communicate()
            raise RuntimeError(
                f"hermes {dispatch_kind} dispatch timeout on {agent.id} "
                f"({_OPENCLAW_HEADLESS_TIMEOUT_SEC}s)",
            ) from exc
        except asyncio.CancelledError:
            # Run abort / service drain: never leave a headless agent turn
            # running detached (it would keep mutating the workspace).
            subprocess_registry.kill_group(proc)
            raise
        finally:
            subprocess_registry.unregister(proc)
        if (proc.returncode or 0) != 0:
            detail = (
                stderr_b.decode("utf-8", errors="replace").strip()
                or stdout_b.decode("utf-8", errors="replace").strip()
                or f"exit code {proc.returncode or 1}"
            )
            raise RuntimeError(
                f"hermes {dispatch_kind} dispatch failed for {agent.id}: {detail[:1000]}",
            )
        self._emit_event(
            "complaint_headless_dispatched"
            if dispatch_kind == "complaint"
            else "merge_requirement_headless_dispatched",
            agent_id=agent.id,
            task_id=task_id,
            payload={
                "cwd": cwd,
                "cwd_source": cwd_source,
                "dispatch_kind": dispatch_kind,
                "platform": "hermes",
            },
        )

    async def _dispatch_merge_requirement_task(
        self,
        *,
        agent: FlowAgent,
        task_id: str,
        message: str,
    ) -> None:
        if agent.kind == AgentKind.openclaw:
            await self._dispatch_openclaw_headless(
                agent=agent,
                task_id=task_id,
                message=message,
                dispatch_kind="merge_requirement",
            )
            return
        await self._dispatch_custom_task(agent=agent, task_id=task_id, message=message)

    async def _dispatch_openclaw_headless(
        self,
        *,
        agent: FlowAgent,
        task_id: str,
        message: str,
        dispatch_kind: str = "complaint",
    ) -> None:
        executable = self._resolve_openclaw_executable()
        if not executable:
            raise RuntimeError("openclaw CLI is not available in PATH")
        cwd, cwd_source = await self._openclaw_dispatch_cwd(agent)
        message = self._inject_headless_dispatch_context(
            message=message,
            cwd=cwd,
            cwd_source=cwd_source,
            dispatch_kind=dispatch_kind,
            platform="openclaw",
        )
        session_id = openclaw_session_id_for_run(self.team_name, agent.id)
        argv = [
            executable,
            "agent",
            "--local",
            "--agent",
            agent.id,
            "--session-id",
            session_id,
            "--message",
            message,
        ]
        env = os.environ.copy()
        # Force a deterministic ClawTeam identity in headless mode so
        # `clawteam inbox send` without explicit `--from` still carries the
        # expected sender name (e.g. leader complaint fan-out).
        env.update({
            "CLAWTEAM_AGENT_NAME": agent.id,
            "OH_AGENT_NAME": agent.id,
            "CLAUDE_CODE_AGENT_NAME": agent.id,
            "CLAWTEAM_TEAM_NAME": self.team_name,
            "OH_TEAM_NAME": self.team_name,
            "CLAUDE_CODE_TEAM_NAME": self.team_name,
        })
        for attempt in range(2):
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            subprocess_registry.register(proc)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_OPENCLAW_HEADLESS_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError as exc:
                subprocess_registry.kill_group(proc)
                await proc.communicate()
                raise RuntimeError(
                    f"openclaw {dispatch_kind} dispatch timeout on {agent.id} "
                    f"({_OPENCLAW_HEADLESS_TIMEOUT_SEC}s)",
                ) from exc
            except asyncio.CancelledError:
                # Run abort / service drain: kill the in-flight headless
                # turn's whole process group before propagating.
                subprocess_registry.kill_group(proc)
                raise
            finally:
                subprocess_registry.unregister(proc)
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if (proc.returncode or 0) == 0:
                break
            detail = (stderr or "").strip() or (stdout or "").strip()
            if not detail:
                detail = f"exit code {proc.returncode or 1}"
            if attempt == 0 and looks_like_pending_scope_approval(detail):
                logger.warning(
                    "openclaw_headless_scope_pending_detected",
                    run_id=self.run.id,
                    team=self.team_name,
                    agent_id=agent.id,
                    dispatch_kind=dispatch_kind,
                    detail=detail[:240],
                )
                try:
                    repaired = repair_pending_scope_upgrades(config=load_config())
                    logger.info(
                        "openclaw_headless_scope_repair_result",
                        run_id=self.run.id,
                        team=self.team_name,
                        agent_id=agent.id,
                        dispatch_kind=dispatch_kind,
                        repaired_request_ids=repaired,
                        repaired_count=len(repaired),
                    )
                except Exception as exc:
                    logger.warning(
                        "openclaw_headless_scope_repair_failed",
                        run_id=self.run.id,
                        team=self.team_name,
                        agent_id=agent.id,
                        dispatch_kind=dispatch_kind,
                        error=str(exc),
                    )
                continue
            raise RuntimeError(
                f"openclaw {dispatch_kind} dispatch failed for {agent.id}: {detail[:1000]}",
            )
        else:
            raise RuntimeError(
                f"openclaw {dispatch_kind} dispatch failed for {agent.id}: "
                "scope repair retry exhausted",
            )
        event_type = (
            "complaint_headless_dispatched"
            if dispatch_kind == "complaint"
            else "merge_requirement_headless_dispatched"
        )
        self._emit_event(
            event_type,
            agent_id=agent.id,
            task_id=task_id,
            payload={
                "cwd": cwd,
                "cwd_source": cwd_source,
                "session_id": session_id,
                "forced_agent_name": agent.id,
                "dispatch_kind": dispatch_kind,
            },
        )

    async def _openclaw_dispatch_cwd(self, agent: FlowAgent) -> tuple[str, str]:
        main_repo = self._openclaw_main_repo(agent)
        try:
            wt = await self.worktree_lookup.get(
                self.team_name,
                agent.id,
                repo=main_repo,
                force=True,
            )
        except Exception:
            wt = None
        if wt is not None:
            path = Path(wt.worktree_path)
            if path.exists() and path.is_dir():
                return str(path), "worktree_existing"

        # Complaint phase may start long after run-loop sessions were disposed.
        # If no usable worktree exists, create/recover one via the regular
        # session bootstrap path (spawn_fresh/resume), then resolve again.
        sess = await self._ensure_session_idle(agent)
        await self._refresh_worktree(sess)
        recovered = sess.worktree
        if recovered is not None:
            path = Path(recovered.worktree_path)
            if path.exists() and path.is_dir():
                return str(path), "worktree_created"

        raise RuntimeError(
            "openclaw dispatch workspace not found after recovery: "
            f"agent={agent.id!r} main_repo={main_repo!r}",
        )

    def _inject_headless_dispatch_context(
        self,
        *,
        message: str,
        cwd: str,
        cwd_source: str,
        dispatch_kind: str,
        platform: str,
    ) -> str:
        title = (
            "## ClawsomeFlow Complaint Dispatch Context"
            if dispatch_kind == "complaint"
            else "## ClawsomeFlow Merge Requirement Dispatch Context"
        )
        if platform == "hermes":
            return f"{title}\n\n{message}"
        return (
            f"{title}\n"
            f"- verified_workdir: `{cwd}`\n"
            f"- workdir_source: `{cwd_source}`\n"
            "- This directory was validated at dispatch time; only modify files inside it and its subdirectories.\n\n"
            f"{message}"
        )

    def _resolve_openclaw_executable(self) -> str | None:
        return resolve_openclaw_executable()

    async def _dispatch_custom_task(
        self, *, agent: FlowAgent, task_id: str, message: str,
    ) -> None:
        """Dispatch an ad-hoc message outside the original Flow DAG."""
        session = await self._ensure_session_idle(agent)
        outcome = await session.dispatch(task_id=task_id, message=message)
        if outcome.success:
            self._last_dispatch_failures.pop(agent.id, None)
            return
        self._last_dispatch_failures[agent.id] = {
            **self._dispatch_failure_payload(outcome),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        # One self-heal retry: treat the session as crashed, recreate/resume,
        # then dispatch again. This covers cases where users start complaint
        # feedback long after run_loop and the old pane/process vanished.
        try:
            session.mark_crashed(reason="custom_dispatch_failed")
        except Exception:
            # Fallback: drop stale handle and recreate via _ensure_session_idle.
            self._sessions.pop(agent.id, None)
        session = await self._ensure_session_idle(agent)
        retry = await session.dispatch(task_id=task_id, message=message)
        if retry.success:
            self._last_dispatch_failures.pop(agent.id, None)
            return
        self._last_dispatch_failures[agent.id] = {
            **self._dispatch_failure_payload(retry),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        raise RuntimeError(
            f"custom dispatch failed for {agent.id}: {retry.detail or outcome.detail}"
        )

    async def _build_checkpoint_rerun_prompt(
        self,
        *,
        downstream_task_id: str,
        upstream_item: _CheckpointItem,
        feedback: str,
    ) -> str:
        """Rerun message = feedback preamble + the **exact** dispatch message the
        task would receive on first dispatch.

        Invariant (user requirement): rerun-injected execution requirements must
        be identical to the initial dispatch in every case. We therefore reuse
        ``_compose_dispatch_context`` + ``_render_dispatch_message`` rather than
        hand-mirroring the self-merge / inbox / completion steps (which is what
        previously drifted). The preamble only adds rerun-specific context
        (feedback + previous summary); it never restates execution requirements.
        """
        previous_summary = (upstream_item.summary or "").strip()
        previous_block = (
            previous_summary
            if previous_summary
            else "(no previously matched inbox summary)"
        )
        preamble = (
            "## ClawsomeFlow Manual Checkpoint Rerun\n"
            f"- team: `{self.team_name}`\n"
            f"- upstream_task_id: `{upstream_item.task_id}`\n"
            f"- downstream_task_waiting: `{downstream_task_id}`\n"
            f"- owner_agent: `{upstream_item.owner_agent_id}`\n\n"
            "Checkpoint feedback from user:\n"
            f"{feedback}\n\n"
            "Previous upstream output summary:\n"
            f"{previous_block}\n\n"
            "Re-execute this task according to the feedback. **The full execution "
            "requirements below are identical to the original dispatch — follow them "
            "exactly** (commit, merge if the checklist requires it, inbox the leader, "
            "mark the task completed)."
        )
        book = self._tasks.get(upstream_item.task_id)
        agent = self._agents.get(upstream_item.owner_agent_id)
        if book is None or agent is None:
            # Degraded fallback (should not happen in practice): no task/agent to
            # compose a dispatch for — return the preamble alone.
            return preamble
        ctx = await self._compose_dispatch_context(agent, book.task)
        dispatch_message = self._render_dispatch_message(agent, book.task, ctx)
        return preamble + "\n\n" + dispatch_message

    async def _wait_for_clawteam_tasks_completed(
        self,
        *,
        mcp,
        task_ids: list[str],
        timeout_sec: int,
        poll_sec: float = 2.0,
    ) -> None:
        pending = {tid for tid in task_ids if tid}
        if not pending:
            return
        deadline = time.monotonic() + timeout_sec
        while pending:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"waiting complaint tasks timeout: pending={sorted(list(pending))}"
                )
            finished: set[str] = set()
            for tid in list(pending):
                row = await mcp.task_get(self.team_name, tid)
                if not row:
                    continue
                status = str(row.get("status") or "")
                if status == "completed":
                    finished.add(tid)
                    continue
                if status in {"failed", "blocked", "cancelled"}:
                    raise RuntimeError(f"task {tid} entered status={status}")
            pending -= finished
            if pending:
                await asyncio.sleep(poll_sec)

    async def _collect_agent_complaints(
        self,
        *,
        mcp,
        target_agents: list[FlowAgent],
        relay_task_id: str,
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        for agent in target_agents:
            rows = await mcp.mailbox_peek(self.team_name, agent.id)
            msg = self._pick_latest_leader_complaint(
                rows,
                relay_task_id=relay_task_id,
                target_agent_id=agent.id,
            )
            if msg:
                out[agent.id] = msg
        return out

    def _pick_latest_leader_complaint(
        self,
        rows: list[dict[str, Any]],
        *,
        relay_task_id: str,
        target_agent_id: str,
    ) -> str | None:
        for item in reversed(rows):
            from_agent = str(item.get("from_agent") or item.get("from") or "")
            if from_agent != self._leader_id:
                continue
            text = str(item.get("content") or item.get("body") or "").strip()
            if not text:
                continue
            parsed = self._parse_leader_complaint_relay(
                text=text,
                relay_task_id=relay_task_id,
                target_agent_id=target_agent_id,
            )
            if parsed is None:
                continue
            return parsed
        return None

    def _parse_leader_complaint_relay(
        self,
        *,
        text: str,
        relay_task_id: str,
        target_agent_id: str,
    ) -> str | None:
        match = _LEADER_COMPLAINT_RELAY_RE.match(text.strip())
        if match is None:
            return None
        if match.group("relay_task_id") != relay_task_id:
            return None
        if match.group("target_agent_id") != target_agent_id:
            return None
        body = match.group("body").strip()
        return body or "(no additional details)"

    def _build_leader_complaint_prompt(
        self,
        *,
        task_id: str,
        complaint_text: str,
        targets: list[FlowAgent],
    ) -> str:
        targets_txt = ", ".join(f"`{a.id}`" for a in targets) or "(none)"
        # The leader only relays feedback + marks its task complete. It must NOT
        # merge anything (merge wording is reserved for OpenClaw fix tasks).
        return (
            "## ClawsomeFlow Complaint Handling Task (Leader)\n"
            f"- team: `{self.team_name}`\n"
            f"- task_id: `{task_id}`\n"
            f"- feedback targets (and cannot message yourself): {targets_txt}\n\n"
            "Original user complaint:\n"
            f"{complaint_text}\n\n"
            "Execution steps:\n"
            "1) Determine whether this is a valid complaint.\n"
            "2) If valid, send feedback only to agents in the target list (never to yourself), "
            "using this **strict header format**:\n"
            f"   `clawteam inbox send {self.team_name} <agent_id> "
            f"\"[csflow-complaint-relay:{task_id}:<agent_id>] <complaint feedback for that agent>\" "
            f"--from {self._leader_id}`.\n"
            f"3) `--from {self._leader_id}` is mandatory and cannot be omitted.\n"
            "4) `<agent_id>` in the header must exactly match the inbox recipient.\n"
            f"5) VERY IMPORTANT! you MUST execute: `clawteam task update {self.team_name} {task_id} --status completed`.\n"
            "6) If the complaint is not valid, you may skip inbox sends, but step 5 is still mandatory."
        )

    def _build_agent_complaint_prompt(
        self,
        *,
        task_id: str,
        user_complaint: str,
        leader_feedback: str,
        merge_required: bool,
    ) -> str:
        # Only OpenClaw self-merges its complaint fix into baseline. Hermes does
        # not merge — it refines behavioral guidelines instead of code fixes.
        if merge_required:
            execution_requirements = (
                "Execution requirements:\n"
                "1) Implement fixes based on the complaint and leader feedback.\n"
                "2) Make changes in this task worktree. After finishing the fixes and updating "
                "the workspace, merge your worktree branch into the baseline branch in this task. "
                "If the merge hits conflicts, **you must resolve them yourself** and finish the "
                "merge commit — do not leave the merge incomplete.\n"
                "3) No inbox send is needed. After completion, **VERY IMPORTANT! you MUST execute**:\n"
                f"`clawteam task update {self.team_name} {task_id} --status completed`."
            )
        else:
            execution_requirements = (
                "Execution requirements:\n"
                "1) Remember what the user was dissatisfied with and refine your "
                "behavioral guidelines accordingly.\n"
                "2) **STRICTLY FORBIDDEN: do NOT create, modify or delete ANY file in "
                "your current working directory (the task worktree), and do NOT run "
                "any git command that changes its state.** The user may still review, "
                "adopt or publish this worktree's content afterwards — it must stay "
                "exactly as the run left it.\n"
                "3) No inbox send is needed. After completion, **VERY IMPORTANT! you MUST execute**:\n"
                f"`clawteam task update {self.team_name} {task_id} --status completed`."
            )
        return (
            "## ClawsomeFlow Complaint Handling Task (Agent)\n"
            f"- team: `{self.team_name}`\n"
            f"- task_id: `{task_id}`\n\n"
            "Original user complaint:\n"
            f"{user_complaint}\n\n"
            "Leader feedback for you:\n"
            f"{leader_feedback}\n\n"
            f"{execution_requirements}"
        )

    def _flow_mode(self) -> str:
        """Resolve the Flow execution mode from the spec variables."""
        variables = getattr(self.spec, "variables", None) or {}
        return flow_mode(variables)

    def _complaint_target_agents(self) -> list[FlowAgent]:
        """Complaint-fix targets: persistent OpenClaw/Hermes WORKERS only.

        Complaint fixes are dispatched to OpenClaw AND Hermes workers (other
        platforms are intentionally excluded for now). Only OpenClaw is also a
        merge target; Hermes complaint output is NOT merged (worktree removed
        by the unified terminal cleanup). Excluded by construction: the leader,
        temporary inline agents, and every other kind — notably
        ``AgentKind.external`` (external execution nodes have no local session
        or worktree; a complaint can never be "fixed" by re-dispatching them).
        """
        return [
            a for a in self.spec.agents
            if (
                not a.is_leader
                and not a.is_temporary
                and a.kind in (AgentKind.openclaw, AgentKind.hermes)
                and a.id != self._leader_id
            )
        ]

    def _merge_requirement_agents(self) -> list[FlowAgent]:
        # In easy / developer mode every merge is performed in-task (easy → all
        # tasks self-merge; dev → auto-merge tasks + OpenClaw self-merge, no-merge
        # tasks are intentionally discarded). So the complaint phase must NOT
        # re-dispatch standalone merge-requirement tasks. Only normal-mode manual
        # runs defer OpenClaw merges to the complaint/satisfaction stage.
        if self._flow_mode() in ("easy", "dev"):
            return []
        return [agent for agent in self.spec.agents if agent.kind == AgentKind.openclaw]

    async def _dispatch_merge_requirements(
        self,
        *,
        mcp,
        agents: list[FlowAgent],
        phase: str,
        reason: str,
        source_task_id: str | None = None,
    ) -> list[str]:
        task_ids: list[str] = []
        for agent in agents:
            try:
                row = await mcp.task_create(
                    self.team_name,
                    f"Execute workspace merge: {agent.id}",
                    description="internal merge requirement task (excluded from flow history)",
                    owner=agent.id,
                    metadata={
                        "csflow_internal": True,
                        "csflow_phase": "complaint_merge_requirement",
                        "csflow_exclude_history": True,
                        "csflow_target_agent": agent.id,
                        "csflow_merge_phase": phase,
                        "csflow_merge_reason": reason,
                        **(
                            {"csflow_source_task_id": source_task_id}
                            if source_task_id else {}
                        ),
                    },
                )
            except Exception as exc:
                logger.warning(
                    "merge_requirement_task_create_failed",
                    run_id=self.run.id,
                    team=self.team_name,
                    agent_id=agent.id,
                    phase=phase,
                    reason=reason,
                    error=str(exc)[:1000],
                )
                continue
            ct_task_id = str(row.get("id") or "")
            if not ct_task_id:
                logger.warning(
                    "merge_requirement_task_create_empty_id",
                    run_id=self.run.id,
                    team=self.team_name,
                    agent_id=agent.id,
                    phase=phase,
                    reason=reason,
                )
                continue
            message = await self._build_merge_requirement_prompt(
                agent=agent,
                task_id=ct_task_id,
                reason=reason,
            )
            try:
                await self._dispatch_merge_requirement_task(
                    agent=agent,
                    task_id=f"merge-{agent.id}-{ct_task_id}",
                    message=message,
                )
            except Exception as exc:
                logger.warning(
                    "merge_requirement_dispatch_failed",
                    run_id=self.run.id,
                    team=self.team_name,
                    agent_id=agent.id,
                    task_id=ct_task_id,
                    phase=phase,
                    reason=reason,
                    error=str(exc)[:1000],
                )
                continue
            task_ids.append(ct_task_id)
            self._emit_event(
                "run_merge_requirement_dispatched",
                agent_id=agent.id,
                task_id=ct_task_id,
                payload={
                    "phase": phase,
                    "reason": reason,
                    "source_task_id": source_task_id,
                },
            )
        return task_ids

    async def _wait_for_merge_requirement_tasks(
        self,
        *,
        mcp,
        task_ids: list[str],
        phase: str,
    ) -> None:
        pending = sorted({tid for tid in task_ids if tid})
        if not pending:
            return
        try:
            await self._wait_for_clawteam_tasks_completed(
                mcp=mcp,
                task_ids=pending,
                timeout_sec=2700,
            )
        except Exception as exc:
            logger.warning(
                "merge_requirement_wait_failed",
                run_id=self.run.id,
                team=self.team_name,
                phase=phase,
                pending_task_ids=pending,
                error=str(exc)[:1000],
            )

    async def _build_merge_requirement_prompt(
        self,
        *,
        agent: FlowAgent,
        task_id: str,
        reason: str,
    ) -> str:
        repo_root, base_branch, merge_branch = await self._resolve_merge_context(agent=agent)
        merge_line = self_merge_instruction(
            repo_root=repo_root,
            base_branch=base_branch,
            feature_branch=merge_branch,
            merge_message=f"[csflow] merge {merge_branch} after run {self.run.id}",
        )
        return (
            "## ClawsomeFlow Merge Requirement\n"
            f"- team: `{self.team_name}`\n"
            f"- merge_task_id: `{task_id}`\n"
            f"- agent: `{agent.id}`\n"
            f"- reason: `{reason}`\n\n"
            "Merge only:\n"
            f"1) {merge_line}\n"
            "2) `git log --oneline | head -5`\n"
            f"3) `clawteam task update {self.team_name} {task_id} --status completed`\n"
            "4) On failure: inbox leader, then still run step 3:\n"
            f"   `clawteam inbox send {self.team_name} {self._leader_id} "
            f"\"merge request {agent.id} failed: <reason>\" --from {agent.id}`"
        )

    async def _resolve_merge_context(
        self,
        *,
        agent: FlowAgent,
    ) -> tuple[str, str, str]:
        wt: WorktreeInfo | None = None
        sess = self._sessions.get(agent.id)
        if sess is not None and sess.worktree is not None:
            wt = sess.worktree
        if wt is None:
            repo_hint: str | None
            if agent.kind == AgentKind.openclaw:
                repo_hint = self._openclaw_main_repo(agent)
            else:
                repo_hint = (agent.repo or "").strip() or None
            try:
                wt = await self.worktree_lookup.get(
                    self.team_name,
                    agent.id,
                    repo=repo_hint,
                    force=True,
                )
            except Exception:
                wt = None

        branch = wt.branch_name if (wt and wt.branch_name) else f"clawteam/{self.team_name}/{agent.id}"
        if agent.kind == AgentKind.openclaw:
            repo_root = wt.repo_root if (wt and wt.repo_root) else self._openclaw_main_repo(agent)
            from app.integrations.git_repo import resolve_workspace_base_branch

            base = (
                wt.base_branch
                if (wt and wt.base_branch)
                else resolve_workspace_base_branch(repo_root)
            )
        else:
            repo_root = wt.repo_root if (wt and wt.repo_root) else ((agent.repo or "").strip() or "<repo-root>")
            base = wt.base_branch if (wt and wt.base_branch) else ((agent.target_branch or "").strip() or "main")
        return repo_root, base, branch

    async def _finish_after_complaint_phase(self) -> None:
        final_status = self._post_complaint_terminal_status()
        self.run.status = final_status
        if final_status in {
            RunStatus.completed,
            RunStatus.completed_with_conflicts,
            RunStatus.complaint_failed,
            RunStatus.failed,
            RunStatus.aborted,
        } and self.run.finished_at is None:
            self.run.finished_at = datetime.now(timezone.utc)
        self.storage.run_update(self.run)
        await run_terminal_tail_cleanup(
            run=self.run,
            flow=self.flow,
            agents=self.spec.agents,
            storage=self.storage,
            worktree_lookup=self.worktree_lookup,
        )

    def _post_complaint_terminal_status(self) -> RunStatus:
        marker = str((self.run.inputs or {}).get(_POST_COMPLAINT_STATUS_KEY) or "")
        if marker == RunStatus.completed_with_conflicts.value:
            return RunStatus.completed_with_conflicts
        return RunStatus.completed

    # ── dispatch context composition ─────────────────────────────────

    def _render_dispatch_message(
        self, agent: FlowAgent, task: FlowTask, ctx: DispatchContext,
    ) -> str:
        """Pick the dispatch builder exactly as the initial dispatch does.

        Shared by ``_dispatch_one`` and the checkpoint-rerun path so a rerun's
        execution requirements are byte-for-byte identical to the first dispatch
        (DEV.md invariant: rerun == initial dispatch + feedback preamble).
        """
        if agent.kind == AgentKind.external:
            # External executors never talk to ClawTeam — the sheet carries
            # no protocol steps (completion goes through the receipt API).
            return build_external_task_text(ctx)
        if agent.is_leader and task.is_leader_summary:
            return build_leader_dispatch(ctx)
        return build_worker_dispatch(ctx)

    async def _compose_dispatch_context(
        self, agent: FlowAgent, task: FlowTask,
    ) -> DispatchContext:
        sess = self._sessions.get(agent.id)
        wt = sess.worktree if sess is not None else None

        worker_worktrees = []
        worker_reports: list[WorkerReport] = []
        upstream_outputs: list[UpstreamOutput] = []

        if agent.is_leader and task.is_leader_summary:
            # Summary input must follow the task's explicit first-level
            # dependencies; do not inject reports/worktrees from unrelated tasks.
            inbox = await self._fetch_leader_inbox_structured()
            seen_dep_ids: set[str] = set()
            seen_owner_ids: set[str] = set()
            for dep_raw in task.depends_on:
                dep_id = str(dep_raw).strip()
                if not dep_id or dep_id in seen_dep_ids:
                    continue
                seen_dep_ids.add(dep_id)
                dep_book = self._tasks.get(dep_id)
                if dep_book is None:
                    continue
                dep_task = dep_book.task
                dep_owner = self._agents.get(dep_task.owner_agent_id)
                dep_owner_id = dep_owner.id if dep_owner else dep_task.owner_agent_id

                dep_sess = self._sessions.get(dep_task.owner_agent_id)
                if (
                    dep_sess
                    and dep_sess.worktree
                    and dep_owner_id not in seen_owner_ids
                ):
                    worker_worktrees.append(dep_sess.worktree)
                    seen_owner_ids.add(dep_owner_id)

                if (
                    self._task_requires_manual_checkpoint(dep_task)
                    and dep_id in self._checkpoint_approved_summaries
                ):
                    approved_summary = (
                        self._checkpoint_approved_summaries.get(dep_id) or ""
                    ).strip()
                    if approved_summary:
                        worker_reports.append(WorkerReport(
                            from_agent=dep_owner_id,
                            summary=approved_summary,
                            task_id=dep_id,
                            timestamp=None,
                        ))
                    continue

                matched = self._collect_upstream_task_report_entry(
                    inbox,
                    owner_agent_id=dep_owner_id,
                    task_id=dep_id,
                )
                if matched is not None:
                    worker_reports.append(matched)
        elif task.depends_on:
            # Worker (or leader for non-summary): pass first-level upstream
            # outputs only — never transitively. For each depended task, match
            # leader-inbox reports by (from_agent == upstream owner) AND
            # (task_id == depended task id) so downstream sees only that
            # upstream task's own completion output.
            inbox = await self._fetch_leader_inbox_structured()
            for dep_id in task.depends_on:
                dep_book = self._tasks.get(dep_id)
                if dep_book is None:
                    continue
                dep_task = dep_book.task
                dep_owner = self._agents.get(dep_task.owner_agent_id)
                dep_owner_id = dep_owner.id if dep_owner else dep_task.owner_agent_id
                dep_sess = self._sessions.get(dep_task.owner_agent_id)
                wt_info = dep_sess.worktree if dep_sess else None
                if (
                    self._task_requires_manual_checkpoint(dep_task)
                    and dep_id in self._checkpoint_approved_summaries
                ):
                    # Downstream tasks must consume the user-approved version.
                    summary_bundle = self._checkpoint_approved_summaries.get(dep_id)
                else:
                    summary_bundle = self._collect_upstream_task_report(
                        inbox,
                        owner_agent_id=dep_owner_id,
                        task_id=dep_id,
                    )
                # External upstreams own no worktree — never pass path/branch
                # fields downstream (would be empty or misleading).
                dep_is_external = (
                    dep_owner is not None and dep_owner.kind == AgentKind.external
                )
                upstream_outputs.append(UpstreamOutput(
                    task_id=dep_id,
                    subject=dep_task.subject,
                    from_agent=dep_owner_id,
                    worktree_path=(
                        None if dep_is_external
                        else (str(wt_info.worktree_path) if wt_info else None)
                    ),
                    branch_name=(
                        None if dep_is_external
                        else (wt_info.branch_name if wt_info else None)
                    ),
                    base_branch=(
                        None if dep_is_external
                        else (wt_info.base_branch if wt_info else None)
                    ),
                    repo_root=(
                        None if dep_is_external
                        else (wt_info.repo_root if wt_info else None)
                    ),
                    summary=summary_bundle,
                    is_external=dep_is_external,
                ))

        # Translate the FlowTask.id into the ClawTeam-side id the worker
        # must use in its ``clawteam task update`` step (ClawTeam tracks
        # its own opaque ids; using the FlowTask.id makes the CLI
        # respond "Task '<id>' not found" and the run gets stuck).
        ct_task_id: str | None = None
        if self.compile_result is not None:
            ct_task_id = self.compile_result.flow_to_clawteam.get(task.id)

        mode = self._flow_mode()
        self_merge = task_self_merges(
            mode=mode,
            run_is_scheduled=run_is_unattended(self.run),
            task=task,
            agent=agent,
        )

        return DispatchContext(
            run_id=self.run.id,
            team_name=self.team_name,
            flow_description=self.flow_description,
            flow_inputs=dict(self.run.inputs or {}),
            user=self.run.user,
            agent=agent,
            task=task,
            leader_agent_id=self._leader_id,
            clawteam_task_id=ct_task_id,
            worktree=wt,
            worker_worktrees=worker_worktrees,
            worker_reports=worker_reports,
            upstream_outputs=upstream_outputs,
            self_merge=self_merge,
            merge_reference=merge_reference_enabled(mode=mode),
        )

    def _render_report_summary(self, report: WorkerReport | None) -> str | None:
        if report is None:
            return None
        text = (report.summary or "").strip()
        if not text:
            return None
        prefix = f"[task {report.task_id}] " if report.task_id else ""
        return f"{prefix}{text}"

    def _collect_upstream_task_report_entry(
        self,
        reports: list[WorkerReport],
        *,
        owner_agent_id: str,
        task_id: str,
    ) -> WorkerReport | None:
        """Return the latest strict-match upstream completion report row."""
        wanted_task_id = str(task_id).strip()
        if not wanted_task_id:
            return None
        mine = [
            r for r in reports
            if (r.summary or "").strip()
            and r.from_agent == owner_agent_id
            and (r.task_id or "").strip() == wanted_task_id
        ]
        if not mine:
            return None
        ordered = self._reports_chronological(mine)
        return ordered[-1]

    def _collect_upstream_task_report(
        self,
        reports: list[WorkerReport],
        *,
        owner_agent_id: str,
        task_id: str,
    ) -> str | None:
        """Return the latest strict-match upstream completion report.

        Strict-match means:
        1) message sender must be the depended task's owner agent;
        2) parsed/declared task_id must equal the depended task id.
        """
        return self._render_report_summary(
            self._collect_upstream_task_report_entry(
                reports,
                owner_agent_id=owner_agent_id,
                task_id=task_id,
            )
        )

    def _reports_chronological(
        self, rows: list[WorkerReport],
    ) -> list[WorkerReport]:
        stamped: list[tuple[int, float, WorkerReport]] = []
        unstamped: list[tuple[int, WorkerReport]] = []
        for i, r in enumerate(rows):
            epoch = self._parse_iso_timestamp_to_epoch(r.timestamp)
            if epoch is None:
                unstamped.append((i, r))
            else:
                stamped.append((i, epoch, r))
        if not stamped:
            return list(rows)
        stamped.sort(key=lambda item: (item[1], item[0]))
        out = [r for _, _, r in stamped]
        out.extend(r for _, r in unstamped)
        return out

    def _parse_iso_timestamp_to_epoch(self, ts: str | None) -> float | None:
        if not ts:
            return None
        raw = ts.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None

    def _is_report_timestamp_newer(self, *, current: str | None, incoming: str | None) -> bool:
        current_epoch = self._parse_iso_timestamp_to_epoch(current)
        incoming_epoch = self._parse_iso_timestamp_to_epoch(incoming)
        if incoming_epoch is None:
            return False
        if current_epoch is None:
            return True
        return incoming_epoch > current_epoch

    def _report_timestamp_is_older_than(
        self,
        *,
        report_timestamp: str | None,
        boundary: datetime,
    ) -> bool:
        report_epoch = self._parse_iso_timestamp_to_epoch(report_timestamp)
        if report_epoch is None:
            return False
        return report_epoch < boundary.timestamp()

    # ── status / event emission ──────────────────────────────────────

    def _set_status(self, new_status: RunStatus, *, reason: str = "") -> None:
        old = self.run.status
        if old == new_status:
            return
        self.run.status = new_status
        if new_status in (
            RunStatus.completed, RunStatus.completed_with_conflicts,
            RunStatus.complaint_failed,
            RunStatus.failed, RunStatus.aborted,
        ):
            self.run.finished_at = datetime.now(timezone.utc)
        try:
            self.storage.run_update(self.run)
        except Exception as exc:
            logger.warning("run_status_persist_failed", error=str(exc))
        run_state_transition(
            from_state=old.value if hasattr(old, "value") else str(old),
            to_state=new_status.value,
            reason=reason,
        )

    def _emit_event(
        self, event_type: str, *,
        agent_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        # Persist + WS fanout via the canonical helper (field names are
        # camelCase to match the REST `/api/runs/{id}/events` payload).
        from app.events import publish_run_event
        publish_run_event(
            self.storage,
            run_id=self.run.id,
            event_type=event_type,
            agent_id=agent_id,
            task_id=task_id,
            payload=payload,
        )

    async def _shutdown_remaining_sessions(self, *, reason: str) -> None:
        """Best-effort shutdown for all non-exited sessions."""
        await self._cancel_background_startups()
        if not self._sessions:
            return
        for sess in list(self._sessions.values()):
            if sess.state == SessionState.Exited:
                continue
            try:
                await sess.shutdown(reason=reason)
                self._emit_event(
                    "session_disposed",
                    agent_id=sess.agent.id,
                    payload={"reason": reason},
                )
            except Exception as exc:
                logger.warning(
                    "final_shutdown_failed",
                    agent_id=sess.agent.id,
                    error=str(exc),
                )

    async def _persist_terminal_execution_log(self, *, trigger: str) -> None:
        """Write a durable run-level execution snapshot at terminal check."""
        if self._terminal_snapshot_persisted:
            return
        self._terminal_snapshot_persisted = True
        # Flush any remaining leader inbox payload so output hand-offs are complete.
        try:
            await self._fetch_leader_inbox_structured()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("terminal_log_inbox_flush_failed", error=str(exc))
        payload = {
            "trigger": trigger,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run.id,
            "team_name": self.team_name,
            "flow_goal": self.flow_description,
            "run_inputs": dict(self.run.inputs or {}),
            "agent_sessions": self._agent_session_records(),
            "tasks": self._task_execution_records(),
            "non_completed_tasks_at_terminal_check": self._non_completed_task_records(),
            "worker_report_history": list(self._worker_report_history),
        }
        self._emit_event("run_terminal_execution_log", payload=payload)

    def _agent_session_records(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for agent in self.spec.agents:
            sess = self._sessions.get(agent.id)
            rows.append({
                "agent_id": agent.id,
                "agent_kind": agent.kind.value,
                "session_id": self._session_identifier(agent, sess),
                "session_state": (
                    sess.state.value if sess is not None else SessionState.Absent.value
                ),
            })
        return rows

    def _task_execution_records(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in self.spec.tasks:
            book = self._tasks.get(task.id)
            owner_sess = self._sessions.get(task.owner_agent_id)
            owner = self._agents.get(task.owner_agent_id)
            rows.append({
                "task_id": task.id,
                "subject": task.subject,
                "owner_agent_id": task.owner_agent_id,
                "owner_session_id": self._session_identifier(owner, owner_sess),
                "depends_on": list(task.depends_on),
                "state": (book.state.value if book is not None else "unknown"),
                "input_message": (book.last_dispatch_message if book is not None else None),
                "output_messages": list(self._task_outputs.get(task.id, [])),
            })
        return rows

    def _non_completed_task_records(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in self.spec.tasks:
            book = self._tasks.get(task.id)
            state = book.state if book is not None else None
            if state == _TaskState.completed:
                continue
            rows.append({
                "task_id": task.id,
                "owner_agent_id": task.owner_agent_id,
                "depends_on": list(task.depends_on),
                "state": (state.value if state is not None else "unknown"),
            })
        return rows

    def _session_identifier(
        self, agent: FlowAgent | None, sess: WorkerSession | None,
    ) -> str | None:
        if sess is not None:
            sid = getattr(sess, "session_id", None)
            if isinstance(sid, str) and sid:
                return sid
            return sess.tmux_target
        if agent is None:
            return None
        if agent.kind == AgentKind.openclaw:
            return openclaw_session_id_for_run(self.team_name, agent.id)
        return f"clawteam-{self.team_name}:{agent.id}"

    # ── finalize ──────────────────────────────────────────────────────

    async def _invoke_finalize(self, outcome: RunOutcome) -> RunStatus:
        """Drive the merge / cleanup / final-status decision (Phase 6)."""
        if self.flow is None:
            # Without a Flow record, we can't honour cleanup_team_on_finish; we
            # still hand off so finalize can run terminal status/cleanup policy.
            # Synthesize a minimal Flow stub so finalize doesn't choke on
            # attribute access.
            from app.models import Flow as _Flow
            self.flow = _Flow(
                id=self.run.flow_id, name="<unknown>", description="",
                spec={"agents": [], "tasks": []}, owner_user=self.run.user,
                cleanup_team_on_finish=False,
            )
        ipt = FinalizeInput(
            run=self.run, flow=self.flow, agents=list(self._agents.values()),
            leader_agent_id=self._leader_id,
            has_failed_tasks=(
                self._forced_failed
                or bool(self._failed_task_ids)
            ),
            aborted=self._cancel_evt.is_set(),
        )
        try:
            res = await self._finalize_fn(
                ipt, storage=self.storage, worktree_lookup=self.worktree_lookup,
            )
        except Exception as exc:
            logger.exception("finalize_run_failed", error=str(exc))
            self._emit_event(
                "run_finalize_failed",
                payload={"error": str(exc)[:1000]},
            )
            return outcome.final_status  # fall back to controller's view
        # finalize_run already mutated run.status + run.pending_merges; persist.
        try:
            self.storage.run_update(self.run)
        except Exception as exc:
            logger.warning("run_persist_after_finalize_failed", error=str(exc))
        return res.final_status

    # ── terminal check + outcome ─────────────────────────────────────

    def _terminal_check(self) -> bool:
        # Natural completion path: once the leader summary task is completed,
        # enter the finalize phase. The summary is dispatched only after every
        # non-summary task has completed (see ``_ready_tasks`` summary gate), so
        # a completed summary reliably means the whole DAG has run.
        if self._leader_summary_task_id:
            leader_book = self._tasks.get(self._leader_summary_task_id)
            if leader_book is not None and leader_book.state == _TaskState.completed:
                return True
        # Abort path short-circuit: no in-flight in_progress tasks.
        if not self._leader_summary_task_id and all(
            b.state == _TaskState.completed for b in self._tasks.values()
        ):
            return True
        if self._cancel_evt.is_set() and not any(
            b.state == _TaskState.in_progress for b in self._tasks.values()
        ):
            return True
        return False

    def _build_outcome(self) -> RunOutcome:
        completed = [tid for tid, b in self._tasks.items() if b.state == _TaskState.completed]
        failed = sorted(self._failed_task_ids)
        skipped = sorted(self._skipped_task_ids)
        non_completed = [tid for tid, b in self._tasks.items() if b.state != _TaskState.completed]
        if self._cancel_evt.is_set():
            return RunOutcome(
                final_status=RunStatus.aborted,
                completed_task_ids=completed, failed_task_ids=failed,
                skipped_task_ids=skipped,
                reason=f"run aborted ({len(failed)} failed task(s))",
            )
        if failed:
            return RunOutcome(
                final_status=RunStatus.failed,
                completed_task_ids=completed, failed_task_ids=failed,
                skipped_task_ids=skipped,
                reason=f"{len(failed)} task(s) failed",
            )
        # Pending merges (Phase 6) → awaiting_user_review; for now mark completed.
        reason = "run completed"
        if non_completed:
            reason = (
                "leader summary completed; "
                f"{len(non_completed)} task(s) not completed at terminal check"
            )
        return RunOutcome(
            final_status=RunStatus.completed,
            completed_task_ids=completed, failed_task_ids=failed,
            skipped_task_ids=skipped,
            reason=reason,
        )

    def _adapt_poll(self, *, activity: bool) -> None:
        if activity:
            self._poll_sec = _POLL_MIN_SEC
        else:
            self._poll_sec = min(_POLL_MAX_SEC, self._poll_sec * 1.5)


__all__ = ["RunController", "RunOutcome"]
