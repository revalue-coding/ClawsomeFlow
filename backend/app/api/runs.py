"""Runs API (per API.md "Runs" section + plan §11.5).

Endpoints:

* ``POST   /api/flows/{flow_id}/runs``                — trigger
* ``GET    /api/runs``                                — list
* ``GET    /api/runs/{run_id}``                       — detail (incl.
  ``pendingMerges`` + ``clawteamBoardUrl``)
* ``GET    /api/runs/{run_id}/terminals``             — tmux pane snapshots
  grouped by Flow tasks
* ``GET    /api/runs/{run_id}/checkpoint``            — current manual
  checkpoint snapshot (if any)
* ``GET    /api/runs/{run_id}/events``                — paginated events
  (used by clients to backfill after WS reconnect)
* ``POST   /api/runs/{run_id}/abort``                 — cooperative cancel
* ``POST   /api/runs/{run_id}/merge``                 — manual merge a
  pending agent (delegates to :func:`perform_manual_merge`)
* ``POST   /api/runs/{run_id}/dismiss-merge``         — drop a pending
  entry without merging (then auto cleanup that agent worktree)
* ``POST   /api/runs/{run_id}/complaint``             — submit complaint
  text and start background complaint workflow
* ``POST   /api/runs/{run_id}/complaint/skip``        — skip complaint
  stage and finish run
* ``POST   /api/runs/{run_id}/checkpoint/items/{task_id}/approve`` —
  approve one upstream checkpoint item
* ``POST   /api/runs/{run_id}/checkpoint/items/{task_id}/rerun`` —
  request upstream rerun with user feedback
* ``POST   /api/runs/{run_id}/checkpoint/items/{task_id}/mark-read`` —
  clear unread highlight state for a refreshed checkpoint item
* ``POST   /api/runs/{run_id}/retry-task/{task_id}``  — flip a ClawTeam
  task back to ``pending`` so the controller redispatches it next tick
* ``POST   /api/runs/{run_id}/external-tasks/{task_id}/redispatch`` —
  re-dispatch a waiting webhook / remote_csflow external task (fresh
  ticket; prior dispatch invalidated)

Cross-references:
* The Run row is created here (``Run.status = pending``); the scheduler
  flips it to ``compiling → running → ...`` as it moves through the
  lifecycle (see :mod:`app.scheduler.engine`).
* ``clawteamBoardUrl`` is built from
  :attr:`Config.clawteam_board_port` (local mode default 17018).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path as FsPath
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api.errors import ApiError
from app.config import Config, load_config
from app.integrations.clawteam_cli import get_clawteam_cli
from app.integrations.clawteam_mcp import get_mcp_client
from app.logging_setup import get_logger
from app.models import (
    DEFAULT_TARGET_BRANCH,
    TERMINAL_RUN_STATUSES,
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    FlowRunSchedule,
    FlowRunScheduleExecution,
    FlowSpec,
    MergeStrategy,
    RunEvent,
    RunStatus,
    iso_utc,
)
from app.scheduler.controller import RunController
from app.scheduler.engine import abort_run_to_terminal, get_scheduler
from app.scheduler.finalize import (
    _resolve_agent_repo_for_run,
    classify_merge_failure,
    cleanup_non_openclaw_workspace_after_review_decision,
    perform_manual_merge,
    run_terminal_tail_cleanup,
)
from app.scheduler.naming import team_name_for_run
from app.scheduler.run_metadata import (
    DEV_PENDING_PR_AGENT_IDS_KEY,
    FAILED_AUTO_MERGE_AGENT_IDS_KEY,
    PAUSE_REASON_USER,
    POST_COMPLAINT_STATUS_KEY,
    POST_REVIEW_TERMINAL_STATUS_KEY,
    PRESERVE_WORKTREE_AGENT_IDS_KEY,
    REVERTED_MERGE_AGENT_IDS_KEY,
    UNATTENDED_KEY,
    read_pause_state,
)
from app.scheduler.sessions.tmux_ready import tmux_capture_pane
from app.services import run_schedules as run_schedule_svc
from app.services.run_notify import NOTIFIED_MARKER_KEY
from app.services.run_report import extract_leader_report
from app.storage import StorageBackend, get_storage
from app.worktree.lookup import WorktreeLookup, get_worktree_lookup

router = APIRouter(tags=["runs"])
logger = get_logger("api.runs")


# ──────────────────────────────────────────────────────────────────────
# Common
# ──────────────────────────────────────────────────────────────────────


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


def _storage_dep() -> StorageBackend:
    return get_storage()


def _config_dep() -> Config:
    return load_config()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]
ConfigDep = Annotated[Config, Depends(_config_dep)]


_TERMINAL = TERMINAL_RUN_STATUSES
# States from which the user may 暂停执行 (pause) or 终止执行流 (terminate). The
# controls open only once breakpoint recovery is GUARANTEED — i.e. after the
# ClawTeam team + tasks are compiled and the loop is driving (``running`` and
# the states it flips to). They are deliberately NOT offered during
# ``pending`` / ``compiling`` (the team isn't fully set up, so a pause could not
# be resumed cleanly), and they disappear once a run reaches the merge-review /
# complaint phases (already restart-safe; the user acts on merges / complaint
# there). Between those two points the set is contiguous, so once the buttons
# appear they never blink out mid-run. ``paused`` itself is terminatable
# (promote to aborted) but not re-pausable.
_PAUSE_ALLOWED = {
    RunStatus.running,
    RunStatus.awaiting_external,
    RunStatus.awaiting_user_checkpoint,
}
_TERMINATE_ALLOWED = _PAUSE_ALLOWED | {RunStatus.paused}
_MERGE_DECISION_ALLOWED = {
    RunStatus.awaiting_user_review,
    RunStatus.failed,
    RunStatus.aborted,
}
# Statuses that surface the developer-mode PR module (read-only list/diff).
# ``complaint_processing`` is included so the UI can keep showing pending items
# while headless fix agents run; mutating actions use ``_PR_MODULE_ACTIONABLE``.
# Abnormal terminals (aborted / failed / complaint_failed / orphaned) force a
# full worktree cleanup, so the module must never render for them.
_PR_MODULE_VISIBLE_STATUSES = {
    RunStatus.awaiting_user_complaint,
    RunStatus.complaint_processing,
    RunStatus.completed,
    RunStatus.completed_with_conflicts,
}
# Statuses that allow submit / merge / discard on the PR module.
_PR_MODULE_ACTIONABLE_STATUSES = {
    RunStatus.awaiting_user_complaint,
    RunStatus.completed,
    RunStatus.completed_with_conflicts,
}

# Single-sourced scheduler-internal run.inputs markers (see run_metadata.py).
_POST_COMPLAINT_STATUS_KEY = POST_COMPLAINT_STATUS_KEY
_POST_REVIEW_TERMINAL_STATUS_KEY = POST_REVIEW_TERMINAL_STATUS_KEY
_PRESERVE_WORKTREE_AGENT_IDS_KEY = PRESERVE_WORKTREE_AGENT_IDS_KEY
_REVERTED_MERGE_AGENT_IDS_KEY = REVERTED_MERGE_AGENT_IDS_KEY


# ──────────────────────────────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────────────────────────────


class RunPauseView(_CamelModel):
    """Why a ``paused`` run is parked — drives the Run detail pause banner."""

    reason: str = ""                 # user | failure | internal_error | drain
    detail: str = ""
    failure_inbox_message: str = ""  # raw/synthetic FAILED:… line when reason=failure
    needs_confirmation: bool = False  # scenario 9: internal error → confirm before resume
    at: str | None = None


class RunSummary(_CamelModel):
    id: str
    flow_id: str
    flow_version: int
    team_name: str
    status: str
    user: str
    started_at: str
    finished_at: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: True only for runs launched by a timed schedule (run_schedules.py). The
    #: WebUI renders a "Scheduled" tag so timed runs are distinguishable in history.
    is_scheduled: bool = False
    #: Present only while ``status == "paused"`` — why the run was parked.
    pause: RunPauseView | None = None


class PendingMergeView(_CamelModel):
    agent_id: str
    branch: str
    target_branch: str = DEFAULT_TARGET_BRANCH
    diff_summary: dict[str, Any] = Field(default_factory=dict)
    leader_suggestion: str = ""


class PendingMergeDiffView(_CamelModel):
    """Full unified diff for a pending-merge agent worktree (view-diff modal)."""

    agent_id: str
    branch: str
    base_branch: str
    target_branch: str = DEFAULT_TARGET_BRANCH
    repo_root: str = ""
    patch: str = ""
    patch_truncated: bool = False
    uncommitted_patch: str = ""
    uncommitted_truncated: bool = False
    base_ahead: int = 0
    branch_ahead: int = 0


class RunDiffAgentView(_CamelModel):
    """One agent's merged-into-baseline summary for the post-run "Run diff" list.

    Only agents whose branch actually landed content on a baseline branch this
    run appear (``merge_count > 0``); OpenClaw agents are excluded upstream.
    """

    agent_id: str
    branch: str = ""
    repo_root: str = ""
    merge_count: int = 0
    commit_count: int = 0
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0


class RunDiffView(_CamelModel):
    items: list[RunDiffAgentView] = Field(default_factory=list)


class RunAgentDiffView(RunDiffAgentView):
    """Full unified diff for one agent's merged content (Run-diff modal)."""

    patch: str = ""
    patch_truncated: bool = False


class RunMergeRevertView(_CamelModel):
    """Result of the "撤销合入" (revert-merge) action for one agent."""

    agent_id: str
    ok: bool = False
    target_branch: str = DEFAULT_TARGET_BRANCH
    reverted_merges: list[str] = Field(default_factory=list)
    revert_head: str = ""
    message: str = ""


class PendingPrAgentView(_CamelModel):
    """One dev-mode agent whose worktree awaits a PR decision (PR module)."""

    agent_id: str
    branch: str = ""
    base_branch: str = ""
    target_branch: str = DEFAULT_TARGET_BRANCH
    repo_root: str = ""
    worktree_path: str = ""


class PendingPrListView(_CamelModel):
    items: list[PendingPrAgentView] = Field(default_factory=list)


class PendingPrSubmitResponse(_CamelModel):
    """Result of the one-click "PR to baseline branch" action."""

    agent_id: str
    success: bool = False
    pr_url: str = ""
    message: str = ""


class RunDetail(RunSummary):
    inputs: dict[str, Any] = Field(default_factory=dict)
    pending_merges: list[PendingMergeView] | None = None
    clawteam_board_url: str | None = None
    spec_snapshot: dict[str, Any] = Field(default_factory=dict)


class RunTaskTerminalView(_CamelModel):
    task_id: str
    subject: str
    owner_agent_id: str
    owner_kind: str | None = None
    tmux_target: str
    work_dir: str = ""
    pane_text: str = ""
    available: bool = False


class RunTaskTerminalListResponse(_CamelModel):
    items: list[RunTaskTerminalView]


class RunTaskTerminalPaneView(_CamelModel):
    owner_agent_id: str
    pane_text: str = ""
    available: bool = False


class RunListResponse(_CamelModel):
    items: list[RunSummary]
    total: int


class ClearRunHistoryResponse(_CamelModel):
    runs_deleted: int
    events_deleted: int


class RunCreatePayload(_CamelModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    runtime_prompt: str | None = None
    # When True, mark the run "unattended": the scheduler skips the human
    # merge-review, complaint and checkpoint phases and drives straight to a
    # terminal status (same behaviour as a timed-schedule run; execution mode
    # normal/easy/dev is preserved). Used by MCP-triggered runs and
    # ``csflow runs start --unattended``. Trigger still returns immediately.
    unattended: bool = False


class RunCreateResponse(_CamelModel):
    id: str
    status: str
    team_name: str


class RunResultView(_CamelModel):
    """Status + leader work report for a run (for MCP / CLI result queries)."""

    run_id: str
    status: str
    terminal: bool
    success: bool
    report: str | None = None
    reason: str | None = None
    finished_at: str | None = None


class RunScheduleItemView(_CamelModel):
    flow_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class RunScheduleSummary(_CamelModel):
    id: str
    name: str
    run_mode: str
    execute_mode: str
    interval_days: int | None = None
    next_run_at: str
    items: list[RunScheduleItemView] = Field(default_factory=list)
    created_at: str
    updated_at: str


class RunScheduleListResponse(_CamelModel):
    items: list[RunScheduleSummary]
    total: int


class RunScheduleCreateItemPayload(_CamelModel):
    flow_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class RunScheduleCreatePayload(_CamelModel):
    name: str = Field(..., min_length=1)
    run_mode: str = Field(default="serial", pattern="^(parallel|serial)$")
    execute_mode: str = Field(..., pattern="^(once|recurring)$")
    interval_days: int | None = Field(default=None, ge=1)
    run_at: datetime
    items: list[RunScheduleCreateItemPayload] = Field(..., min_length=1)


class RunScheduleUpdatePayload(_CamelModel):
    name: str = Field(..., min_length=1)
    run_mode: str = Field(..., pattern="^(parallel|serial)$")
    execute_mode: str = Field(..., pattern="^(once|recurring)$")
    interval_days: int | None = Field(default=None, ge=1)
    run_at: datetime
    items: list[RunScheduleCreateItemPayload] = Field(..., min_length=1)


class RunScheduleExecutionItemView(_CamelModel):
    index: int
    flow_id: str
    flow_name: str = ""
    status: str
    reason: str = ""
    reason_code: str = ""
    run_id: str = ""


class RunScheduleExecutionSummary(_CamelModel):
    id: str
    schedule_id: str
    schedule_name: str
    run_mode: str
    execute_mode: str
    status: str
    total_items: int
    succeeded_items: int
    failed_items: int
    skipped_items: int
    started_at: str
    finished_at: str | None = None


class RunScheduleExecutionDetail(RunScheduleExecutionSummary):
    run_ids: list[str] = Field(default_factory=list)
    item_results: list[RunScheduleExecutionItemView] = Field(default_factory=list)


class RunScheduleExecutionListResponse(_CamelModel):
    items: list[RunScheduleExecutionSummary]
    total: int


class ClearScheduleExecutionsResponse(_CamelModel):
    deleted: int


class EventView(_CamelModel):
    id: int
    ts: str
    type: str
    agent_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(_CamelModel):
    items: list[EventView]
    next_since_id: int | None = None


class MergePayload(_CamelModel):
    agent_id: str = Field(..., description="The pending merge agent id")


class MergeResponse(_CamelModel):
    agent_id: str
    success: bool
    message: str = ""


class ComplaintPayload(_CamelModel):
    message: str = Field(..., description="User complaint text")


class CheckpointRerunPayload(_CamelModel):
    feedback: str = Field(..., description="Guidance for upstream task rerun")


# ──────────────────────────────────────────────────────────────────────
# Mappers
# ──────────────────────────────────────────────────────────────────────


def _board_url(team_name: str, cfg: Config) -> str:
    # Always return a same-origin path so SSH / reverse-proxy access keeps
    # working without forcing users to expose/forward a second board port.
    return f"/clawteam-board-proxy/?team={team_name}"


def _public_run_inputs(inputs: dict[str, Any] | None) -> dict[str, Any]:
    """Expose only user-defined run inputs to API callers.

    Internal scheduler/runtime keys are prefixed with ``_csflow_`` and must not
    leak into user-facing "Execution Parameters" displays.
    """
    if not inputs:
        return {}
    out: dict[str, Any] = {}
    for key, value in (inputs or {}).items():
        k = str(key).strip()
        # ``_csflow_*`` are scheduler markers; ``csflow.terminal_webhook_notified_at``
        # is the run-notify dedupe stamp (dotted, not underscore-prefixed) — neither
        # is a user-provided run input, so keep both out of "Execution Parameters".
        if not k or k.startswith("_csflow_") or k == NOTIFIED_MARKER_KEY:
            continue
        out[k] = value
    return out


def _to_summary(r: FlowRun) -> RunSummary:
    pause_view: RunPauseView | None = None
    if r.status == RunStatus.paused:
        blob = read_pause_state(r)
        if blob is not None:
            pause_view = RunPauseView(
                reason=str(blob.get("reason") or ""),
                detail=str(blob.get("detail") or ""),
                failure_inbox_message=str(blob.get("failure_inbox_message") or ""),
                needs_confirmation=bool(blob.get("needs_confirmation")),
                at=(str(blob["at"]) if blob.get("at") else None),
            )
    return RunSummary(
        id=r.id, flow_id=r.flow_id, flow_version=r.flow_version,
        team_name=r.team_name,
        status=_status_str(r.status),
        user=r.user,
        started_at=iso_utc(r.started_at),
        finished_at=iso_utc(r.finished_at) if r.finished_at else None,
        inputs=_public_run_inputs(r.inputs),
        is_scheduled=bool(r.is_scheduled),
        pause=pause_view,
    )


def _to_schedule_summary(row: FlowRunSchedule) -> RunScheduleSummary:
    items: list[RunScheduleItemView] = []
    for raw in row.items or []:
        if not isinstance(raw, dict):
            continue
        flow_id = str(raw.get("flow_id") or "").strip()
        if not flow_id:
            continue
        raw_inputs = raw.get("inputs")
        inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        items.append(RunScheduleItemView(flow_id=flow_id, inputs=inputs))
    return RunScheduleSummary(
        id=row.id,
        name=row.name,
        run_mode=row.run_mode,
        execute_mode=row.execute_mode,
        interval_days=row.interval_days,
        next_run_at=iso_utc(row.next_run_at),
        items=items,
        created_at=iso_utc(row.created_at),
        updated_at=iso_utc(row.updated_at),
    )


def _to_schedule_execution_summary(
    row: FlowRunScheduleExecution,
) -> RunScheduleExecutionSummary:
    return RunScheduleExecutionSummary(
        id=row.id,
        schedule_id=row.schedule_id,
        schedule_name=row.schedule_name,
        run_mode=row.run_mode,
        execute_mode=row.execute_mode,
        status=row.status,
        total_items=row.total_items,
        succeeded_items=row.succeeded_items,
        failed_items=row.failed_items,
        skipped_items=row.skipped_items,
        started_at=iso_utc(row.started_at),
        finished_at=iso_utc(row.finished_at) if row.finished_at else None,
    )


def _to_schedule_execution_detail(
    row: FlowRunScheduleExecution,
) -> RunScheduleExecutionDetail:
    results: list[RunScheduleExecutionItemView] = []
    for raw in row.item_results or []:
        if not isinstance(raw, dict):
            continue
        results.append(
            RunScheduleExecutionItemView(
                index=int(raw.get("index", 0)),
                flow_id=str(raw.get("flow_id") or ""),
                flow_name=str(raw.get("flow_name") or ""),
                status=str(raw.get("status") or ""),
                reason=str(raw.get("reason") or ""),
                reason_code=str(raw.get("reason_code") or ""),
                run_id=str(raw.get("run_id") or ""),
            )
        )
    return RunScheduleExecutionDetail(
        **_to_schedule_execution_summary(row).model_dump(mode="json"),
        run_ids=[str(x) for x in (row.run_ids or [])],
        item_results=results,
    )


def _to_detail(r: FlowRun, *, flow: Flow | None, cfg: Config) -> RunDetail:
    pending = None
    if r.pending_merges:
        pending = [PendingMergeView(**p) for p in r.pending_merges]
    spec_snapshot = {}
    if flow is not None:
        # Keep API contract consistent: nested spec uses camelCase keys.
        spec_snapshot = FlowSpec.model_validate(flow.spec).model_dump(
            mode="json",
            by_alias=True,
        )
    pause_view: RunPauseView | None = None
    if r.status == RunStatus.paused:
        blob = read_pause_state(r)
        if blob is not None:
            pause_view = RunPauseView(
                reason=str(blob.get("reason") or ""),
                detail=str(blob.get("detail") or ""),
                failure_inbox_message=str(blob.get("failure_inbox_message") or ""),
                needs_confirmation=bool(blob.get("needs_confirmation")),
                at=(str(blob["at"]) if blob.get("at") else None),
            )
    return RunDetail(
        id=r.id, flow_id=r.flow_id, flow_version=r.flow_version,
        team_name=r.team_name,
        status=_status_str(r.status),
        user=r.user,
        started_at=iso_utc(r.started_at),
        finished_at=iso_utc(r.finished_at) if r.finished_at else None,
        inputs=_public_run_inputs(r.inputs),
        is_scheduled=bool(r.is_scheduled),
        pause=pause_view,
        pending_merges=pending,
        clawteam_board_url=_board_url(r.team_name, cfg),
        spec_snapshot=spec_snapshot,
    )


def _to_event_view(e: RunEvent) -> EventView:
    return EventView(
        id=e.id or 0, ts=iso_utc(e.ts), type=e.type,
        agent_id=e.agent_id, task_id=e.task_id, payload=e.payload or {},
    )


def _status_str(s: RunStatus | str) -> str:
    return s.value if hasattr(s, "value") else str(s)


def _normalize_runtime_prompt(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = text.strip()
    return cleaned or None


def _runtime_prompt_from_inputs(inputs: dict[str, Any] | None) -> str | None:
    """Build a stable run-time prompt block from structured run inputs."""
    if not inputs:
        return None
    lines: list[str] = []
    for raw_key, raw_val in inputs.items():
        key = str(raw_key).strip()
        if not key:
            continue
        value = str(raw_val).strip() if raw_val is not None else ""
        if not value:
            continue
        lines.append(f"- **{key}**: {value}")
    if not lines:
        return None
    return "\n".join(lines)


def _prepend_runtime_prompt(text: str, runtime_prompt: str) -> str:
    """Prefix a run-time instruction block to flow/task descriptions."""
    header = (
        "## Run-time User Parameters\n"
        "The user provided these parameters when triggering this run:\n"
        f"{runtime_prompt}\n\n"
    )
    body = text.strip()
    if not body:
        return header.strip()
    return f"{header}{body}"


def _inject_runtime_prompt_into_spec(
    *, spec: FlowSpec, runtime_prompt: str | None,
) -> FlowSpec:
    """Return a spec copy with prompt prefixed to every task description."""
    if not runtime_prompt:
        return spec
    copied = spec.model_copy(deep=True)
    for task in copied.tasks:
        task.description = _prepend_runtime_prompt(task.description or "", runtime_prompt)
    return copied


def _ensure_owner(run: FlowRun, user: str) -> None:
    if run.user != user:
        raise ApiError("FORBIDDEN", "run belongs to a different user", status_code=403)


def _mark_awaiting_user_complaint(
    *,
    run: FlowRun,
    storage: StorageBackend,
    final_status_after_complaint: RunStatus,
) -> None:
    merged_inputs = dict(run.inputs or {})
    merged_inputs.pop(_POST_REVIEW_TERMINAL_STATUS_KEY, None)
    merged_inputs[_POST_COMPLAINT_STATUS_KEY] = final_status_after_complaint.value
    run.inputs = merged_inputs
    run.status = RunStatus.awaiting_user_complaint
    if run.finished_at is None:
        run.finished_at = datetime.now(timezone.utc)
    storage.run_update(run)


def _consume_post_review_terminal_status(run: FlowRun) -> RunStatus | None:
    merged_inputs = dict(run.inputs or {})
    raw = str(merged_inputs.get(_POST_REVIEW_TERMINAL_STATUS_KEY) or "").strip()
    if not raw:
        return None
    merged_inputs.pop(_POST_REVIEW_TERMINAL_STATUS_KEY, None)
    run.inputs = merged_inputs
    try:
        parsed = RunStatus(raw)
    except Exception:
        return None
    if parsed in {RunStatus.complaint_failed, RunStatus.failed, RunStatus.aborted}:
        return parsed
    return None


def _read_preserved_worktree_agent_ids(run: FlowRun) -> set[str]:
    raw = (run.inputs or {}).get(_PRESERVE_WORKTREE_AGENT_IDS_KEY)
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        aid = str(item or "").strip()
        if aid:
            out.add(aid)
    return out


def _write_preserved_worktree_agent_ids(
    *,
    run: FlowRun,
    storage: StorageBackend,
    agent_ids: set[str],
) -> None:
    merged_inputs = dict(run.inputs or {})
    if agent_ids:
        merged_inputs[_PRESERVE_WORKTREE_AGENT_IDS_KEY] = sorted(agent_ids)
    else:
        merged_inputs.pop(_PRESERVE_WORKTREE_AGENT_IDS_KEY, None)
    run.inputs = merged_inputs
    storage.run_update(run)


def _emit_run_event(
    storage: StorageBackend, run_id: str, event_type: str, *,
    agent_id: str | None = None, payload: dict[str, Any] | None = None,
) -> None:
    """Persist + broadcast a RunEvent via the single-source publisher."""
    from app.events import publish_run_event
    publish_run_event(
        storage, run_id=run_id, event_type=event_type,
        agent_id=agent_id, payload=payload,
    )


def _read_reverted_merge_agent_ids(run: FlowRun) -> set[str]:
    raw = (run.inputs or {}).get(_REVERTED_MERGE_AGENT_IDS_KEY)
    if not isinstance(raw, list):
        return set()
    return {str(a).strip() for a in raw if str(a or "").strip()}


def _mark_merge_reverted(
    *, run: FlowRun, storage: StorageBackend, agent_id: str,
) -> None:
    current = _read_reverted_merge_agent_ids(run)
    current.add(agent_id)
    merged_inputs = dict(run.inputs or {})
    merged_inputs[_REVERTED_MERGE_AGENT_IDS_KEY] = sorted(current)
    run.inputs = merged_inputs
    storage.run_update(run)


def _controller_for_run(
    *,
    run: FlowRun,
    flow: Flow,
    storage: StorageBackend,
) -> RunController:
    sched = get_scheduler()
    existing = sched.get_controller(run.id)
    if existing is not None:
        return existing
    return RunController(
        run=run,
        spec=FlowSpec.model_validate(flow.spec),
        flow=flow,
        flow_description=flow.description,
        storage=storage,
    )


def _agents_from_flow_spec(flow: Flow | None) -> list[FlowAgent]:
    if flow is None:
        return []
    try:
        return list(FlowSpec.model_validate(flow.spec).agents)
    except Exception:
        return []


async def _cleanup_terminal_tail(
    *,
    run: FlowRun,
    storage: StorageBackend,
    flow: Flow | None = None,
) -> None:
    await run_terminal_tail_cleanup(
        run=run,
        flow=flow,
        agents=_agents_from_flow_spec(flow),
        storage=storage,
    )


# ──────────────────────────────────────────────────────────────────────
# Trigger
# ──────────────────────────────────────────────────────────────────────


@router.post(
    "/flows/{flow_id}/runs",
    response_model=RunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_run(
    flow_id: Annotated[str, Path()],
    payload: Annotated[RunCreatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> RunCreateResponse:
    """Create a Run row + hand off to the scheduler.

    Returns immediately (status=``pending``). The scheduler flips the row
    to ``compiling`` once it begins talking to ClawTeam, then ``running``,
    then the appropriate terminal state.
    """
    flow = storage.flow_get(flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {flow_id!r} not found", status_code=404)
    if flow.owner_user != user:
        raise ApiError(
            "FORBIDDEN", f"flow {flow_id!r} belongs to a different user",
            status_code=403,
        )
    # Backfill legacy Flows created before the global "always cleanup team"
    # policy. Keep trigger non-blocking if optimistic update conflicts.
    if not flow.cleanup_team_on_finish:
        flow.cleanup_team_on_finish = True
        try:
            flow = storage.flow_update(flow, expected_version=flow.version)
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning(
                "flow_cleanup_policy_backfill_skipped",
                flow_id=flow.id,
                error=str(exc),
            )

    # Per-Flow execution mode ("省心模式" / "开发者模式") is persisted in the
    # spec's ``variables`` dict (see frontend flowRuntime.ts) and resolved by
    # app/flow_modes.py — NOT by ``is_scheduled``. A manually triggered run is
    # never ``is_scheduled`` (that flag means literally "timed trigger" and is
    # set only by services/run_schedules.py). The mode drives per-task
    # self-merge (dispatch prompts) and whether the merge-review phase is
    # skipped; the complaint phase runs for every manual run regardless of mode
    # (see scheduler/finalize.py). Human checkpoints are unaffected.

    # Pre-generate the Run id so we can derive team_name **before** insert
    # (storage.run_update is intentionally narrow — it only refreshes the
    # mutable scheduler fields, never team_name).
    from app.models import _new_id
    run_id = _new_id("run")
    # Unattended flag rides as an internal ``_csflow_*`` marker in run.inputs
    # (stripped from public "Execution Parameters" by _public_run_inputs and not
    # shown to agents — the runtime prompt is built from payload.inputs, which
    # never carries the marker). ``is_scheduled`` stays False: it means literally
    # "timed trigger" (run_schedules.py only). run_is_unattended() unions both.
    run_inputs: dict[str, Any] = dict(payload.inputs or {})
    if payload.unattended:
        run_inputs[UNATTENDED_KEY] = "true"
    run = FlowRun(
        id=run_id,
        flow_id=flow.id, flow_version=flow.version,
        team_name=team_name_for_run(run_id),
        status=RunStatus.pending,
        inputs=run_inputs,
        user=user,
        is_scheduled=False,
    )
    saved = storage.run_create(run)

    # Hand off to the scheduler. The controller's run_loop will run
    # asynchronously; this call returns immediately.
    runtime_prompt = _normalize_runtime_prompt(payload.runtime_prompt)
    if runtime_prompt is None:
        runtime_prompt = _runtime_prompt_from_inputs(payload.inputs or {})
    spec = _inject_runtime_prompt_into_spec(
        spec=FlowSpec.model_validate(flow.spec),
        runtime_prompt=runtime_prompt,
    )
    flow_description = (
        _prepend_runtime_prompt(flow.description, runtime_prompt)
        if runtime_prompt else flow.description
    )
    sched = get_scheduler()
    sched.start_run(
        run=saved, spec=spec, flow=flow,
        flow_description=flow_description,
        storage=storage,
    )
    return RunCreateResponse(
        id=saved.id, status=_status_str(saved.status), team_name=saved.team_name,
    )


# ──────────────────────────────────────────────────────────────────────
# List + detail
# ──────────────────────────────────────────────────────────────────────


@router.get("/runs", response_model=RunListResponse)
def list_runs(
    user: UserDep,
    storage: StorageDep,
    cfg: ConfigDep,
    flow_id: Annotated[str | None, Query(alias="flowId")] = None,
    status_q: Annotated[str | None, Query(alias="status")] = None,
    all_users: Annotated[bool, Query(alias="allUsers")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    del cfg  # dependency kept for signature stability
    items, total = storage.run_list(
        flow_id=flow_id, status=status_q,
        user=None if all_users else user,
        limit=limit, offset=offset,
    )
    return RunListResponse(items=[_to_summary(r) for r in items], total=total)


@router.delete("/runs/history", response_model=ClearRunHistoryResponse)
def clear_run_history(
    user: UserDep,
    storage: StorageDep,
) -> ClearRunHistoryResponse:
    """Delete the caller's finished Runs (+ their events). Active Runs are kept."""
    result = storage.run_clear_history(user=user)
    return ClearRunHistoryResponse(
        runs_deleted=int(result.get("runs_deleted", 0)),
        events_deleted=int(result.get("events_deleted", 0)),
    )


@router.get("/run-schedules", response_model=RunScheduleListResponse)
def list_run_schedules(
    user: UserDep,
    storage: StorageDep,
) -> RunScheduleListResponse:
    rows = run_schedule_svc.list_schedules(user=user, storage=storage)
    items = [_to_schedule_summary(row) for row in rows]
    return RunScheduleListResponse(items=items, total=len(items))


@router.post(
    "/run-schedules",
    response_model=RunScheduleSummary,
    status_code=status.HTTP_201_CREATED,
)
def create_run_schedule(
    payload: Annotated[RunScheduleCreatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> RunScheduleSummary:
    try:
        row = run_schedule_svc.create_schedule(
            user=user,
            name=payload.name,
            run_mode=payload.run_mode,
            execute_mode=payload.execute_mode,
            interval_days=payload.interval_days,
            run_at=payload.run_at,
            items=[
                {
                    "flow_id": item.flow_id,
                    "inputs": item.inputs,
                }
                for item in payload.items
            ],
            storage=storage,
        )
    except ValueError as exc:
        raise ApiError("INVALID_PAYLOAD", str(exc), status_code=400) from exc
    return _to_schedule_summary(row)


@router.get("/run-schedules/{schedule_id}", response_model=RunScheduleSummary)
def get_run_schedule(
    schedule_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunScheduleSummary:
    try:
        row = run_schedule_svc.get_schedule(schedule_id, user=user, storage=storage)
    except KeyError as exc:
        raise ApiError(
            "NOT_FOUND",
            f"run schedule {schedule_id!r} not found",
            status_code=404,
        ) from exc
    except PermissionError as exc:
        raise ApiError(
            "FORBIDDEN",
            "run schedule belongs to a different user",
            status_code=403,
        ) from exc
    return _to_schedule_summary(row)


@router.patch("/run-schedules/{schedule_id}", response_model=RunScheduleSummary)
def update_run_schedule(
    schedule_id: Annotated[str, Path()],
    payload: Annotated[RunScheduleUpdatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> RunScheduleSummary:
    try:
        row = run_schedule_svc.update_schedule(
            schedule_id,
            user=user,
            name=payload.name,
            run_mode=payload.run_mode,
            execute_mode=payload.execute_mode,
            interval_days=payload.interval_days,
            run_at=payload.run_at,
            items=[
                {
                    "flow_id": item.flow_id,
                    "inputs": item.inputs,
                }
                for item in payload.items
            ],
            storage=storage,
        )
    except KeyError as exc:
        raise ApiError(
            "NOT_FOUND",
            f"run schedule {schedule_id!r} not found",
            status_code=404,
        ) from exc
    except PermissionError as exc:
        raise ApiError(
            "FORBIDDEN",
            "run schedule belongs to a different user",
            status_code=403,
        ) from exc
    except ValueError as exc:
        raise ApiError("INVALID_PAYLOAD", str(exc), status_code=400) from exc
    return _to_schedule_summary(row)


@router.delete("/run-schedules/{schedule_id}", status_code=204)
def delete_run_schedule(
    schedule_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    try:
        run_schedule_svc.delete_schedule(schedule_id, user=user, storage=storage)
    except KeyError as exc:
        raise ApiError(
            "NOT_FOUND",
            f"run schedule {schedule_id!r} not found",
            status_code=404,
        ) from exc
    except PermissionError as exc:
        raise ApiError(
            "FORBIDDEN",
            "run schedule belongs to a different user",
            status_code=403,
        ) from exc


@router.get("/run-schedule-executions", response_model=RunScheduleExecutionListResponse)
def list_run_schedule_executions(
    user: UserDep,
    storage: StorageDep,
    schedule_id: Annotated[str | None, Query(alias="scheduleId")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunScheduleExecutionListResponse:
    rows, total = run_schedule_svc.list_schedule_executions(
        user=user,
        schedule_id=(schedule_id or "").strip() or None,
        limit=limit,
        offset=offset,
        storage=storage,
    )
    return RunScheduleExecutionListResponse(
        items=[_to_schedule_execution_summary(row) for row in rows],
        total=total,
    )


@router.delete(
    "/run-schedule-executions",
    response_model=ClearScheduleExecutionsResponse,
)
def clear_run_schedule_executions(
    user: UserDep,
    storage: StorageDep,
) -> ClearScheduleExecutionsResponse:
    """Delete the caller's finished schedule-execution records."""
    deleted = run_schedule_svc.clear_schedule_executions(user=user, storage=storage)
    return ClearScheduleExecutionsResponse(deleted=deleted)


@router.get(
    "/run-schedule-executions/{execution_id}",
    response_model=RunScheduleExecutionDetail,
)
def get_run_schedule_execution(
    execution_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunScheduleExecutionDetail:
    try:
        row = run_schedule_svc.get_schedule_execution(
            execution_id,
            user=user,
            storage=storage,
        )
    except KeyError as exc:
        raise ApiError(
            "NOT_FOUND",
            f"run schedule execution {execution_id!r} not found",
            status_code=404,
        ) from exc
    except PermissionError as exc:
        raise ApiError(
            "FORBIDDEN",
            "run schedule execution belongs to a different user",
            status_code=403,
        ) from exc
    return _to_schedule_execution_detail(row)


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
    cfg: ConfigDep,
) -> RunDetail:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    flow = storage.flow_get(run.flow_id)
    return _to_detail(run, flow=flow, cfg=cfg)


def _repo_by_agent_id(spec: FlowSpec) -> dict[str, str | None]:
    repo_by_agent: dict[str, str | None] = {}
    for agent in spec.agents:
        repo = str(agent.repo or "").strip()
        repo_by_agent[agent.id] = str(FsPath(repo).expanduser()) if repo else None
    return repo_by_agent


async def _work_dir_by_agent_id(
    *,
    lookup: WorktreeLookup,
    team_name: str,
    spec: FlowSpec,
) -> dict[str, str]:
    repo_by_agent = _repo_by_agent_id(spec)
    work_dir_by_agent: dict[str, str] = {}
    repos_to_query: set[str | None] = {None}
    repos_to_query.update(repo for repo in repo_by_agent.values() if repo)
    for repo in repos_to_query:
        try:
            workspaces = await lookup.list_team(team_name, repo=repo)
        except Exception:
            workspaces = []
        for workspace in workspaces:
            work_dir_by_agent.setdefault(workspace.agent_name, workspace.worktree_path)

    for agent in spec.agents:
        if work_dir_by_agent.get(agent.id):
            continue
        repo = repo_by_agent.get(agent.id)
        if not repo:
            continue
        try:
            workspace = await lookup.get(team_name, agent.id, repo=repo)
        except Exception:
            workspace = None
        if workspace is not None:
            work_dir_by_agent[agent.id] = workspace.worktree_path
    return work_dir_by_agent


async def _capture_owner_panes(
    *,
    team_name: str,
    owners: list[str],
    history_lines: int,
) -> dict[str, str]:
    async def _capture(owner_agent_id: str) -> tuple[str, str]:
        tmux_target = f"clawteam-{team_name}:{owner_agent_id}"
        try:
            pane_text = await tmux_capture_pane(tmux_target, history_lines=history_lines)
        except Exception:
            pane_text = ""
        return owner_agent_id, pane_text

    if not owners:
        return {}
    captures = await asyncio.gather(*(_capture(owner) for owner in owners))
    return {owner: text for owner, text in captures}


async def _build_run_terminal_items(
    *,
    run: FlowRun,
    spec: FlowSpec,
    capture_map: dict[str, str],
) -> list[RunTaskTerminalView]:
    lookup = get_worktree_lookup()
    work_dir_by_agent = await _work_dir_by_agent_id(
        lookup=lookup, team_name=run.team_name, spec=spec,
    )
    kind_by_agent = {agent.id: agent.kind.value for agent in spec.agents}
    items: list[RunTaskTerminalView] = []
    for task in spec.tasks:
        owner_agent_id = str(task.owner_agent_id or "").strip()
        pane_text = capture_map.get(owner_agent_id, "")
        items.append(
            RunTaskTerminalView(
                task_id=task.id,
                subject=task.subject,
                owner_agent_id=owner_agent_id,
                owner_kind=kind_by_agent.get(owner_agent_id),
                tmux_target=f"clawteam-{run.team_name}:{owner_agent_id}",
                work_dir=work_dir_by_agent.get(owner_agent_id, ""),
                pane_text=pane_text,
                available=bool(pane_text.strip()),
            )
        )
    return items


async def _run_terminal_spec(
    run_id: str,
    storage: StorageBackend,
    user: str,
) -> tuple[FlowRun, FlowSpec]:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {run.flow_id!r} not found", status_code=404)
    spec = FlowSpec.model_validate(flow.spec)
    return run, spec


@router.get("/runs/{run_id}/terminals", response_model=RunTaskTerminalListResponse)
async def list_run_terminals(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
    history_lines: Annotated[int, Query(alias="historyLines", ge=20, le=400)] = 120,
) -> RunTaskTerminalListResponse:
    run, spec = await _run_terminal_spec(run_id, storage, user)

    owners: list[str] = []
    for task in spec.tasks:
        owner = str(task.owner_agent_id or "").strip()
        if owner and owner not in owners:
            owners.append(owner)

    capture_map = await _capture_owner_panes(
        team_name=run.team_name, owners=owners, history_lines=history_lines,
    )
    items = await _build_run_terminal_items(run=run, spec=spec, capture_map=capture_map)
    return RunTaskTerminalListResponse(items=items)


@router.get("/runs/{run_id}/terminals/meta", response_model=RunTaskTerminalListResponse)
async def list_run_terminals_meta(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunTaskTerminalListResponse:
    run, spec = await _run_terminal_spec(run_id, storage, user)
    items = await _build_run_terminal_items(run=run, spec=spec, capture_map={})
    return RunTaskTerminalListResponse(items=items)


@router.get(
    "/runs/{run_id}/terminals/panes/{owner_agent_id}",
    response_model=RunTaskTerminalPaneView,
)
async def get_run_terminal_pane(
    run_id: Annotated[str, Path()],
    owner_agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
    history_lines: Annotated[int, Query(alias="historyLines", ge=20, le=400)] = 120,
) -> RunTaskTerminalPaneView:
    run, spec = await _run_terminal_spec(run_id, storage, user)
    owner = owner_agent_id.strip()
    known_owners = {
        str(task.owner_agent_id or "").strip()
        for task in spec.tasks
        if str(task.owner_agent_id or "").strip()
    }
    if owner not in known_owners:
        raise ApiError(
            "NOT_FOUND",
            f"terminal owner {owner!r} not found on run {run_id!r}",
            status_code=404,
        )
    capture_map = await _capture_owner_panes(
        team_name=run.team_name, owners=[owner], history_lines=history_lines,
    )
    pane_text = capture_map.get(owner, "")
    return RunTaskTerminalPaneView(
        owner_agent_id=owner,
        pane_text=pane_text,
        available=bool(pane_text.strip()),
    )


@router.get("/runs/{run_id}/checkpoint")
def get_run_checkpoint(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> dict[str, Any] | None:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    controller = get_scheduler().get_controller(run.id)
    if controller is None:
        return None
    return controller.checkpoint_snapshot()


# ──────────────────────────────────────────────────────────────────────
# Events (paginated)
# ──────────────────────────────────────────────────────────────────────


@router.get("/runs/{run_id}/events", response_model=EventListResponse)
def list_events(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
    since_id: Annotated[int | None, Query(alias="sinceId", ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventListResponse:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    rows = storage.event_list(run_id=run.id, since_id=since_id, limit=limit)
    items = [_to_event_view(e) for e in rows]
    next_since = items[-1].id if items else since_id
    return EventListResponse(items=items, next_since_id=next_since)


# ──────────────────────────────────────────────────────────────────────
# Result (status + leader work report) — for MCP / CLI result queries
# ──────────────────────────────────────────────────────────────────────


_SUCCESS_RUN_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.completed,
    RunStatus.completed_with_conflicts,
})


@router.get("/runs/{run_id}/result", response_model=RunResultView)
def get_run_result(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunResultView:
    """Status + leader work report for one run (non-blocking, always safe to poll).

    ``report`` is the leader's final work report, extracted from the run's
    ``run_terminal_execution_log`` event; it is ``None`` until the run reaches a
    terminal status. ``terminal``/``success`` classify the current status.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    status_val = run.status if isinstance(run.status, RunStatus) else RunStatus(run.status)
    is_terminal = status_val in TERMINAL_RUN_STATUSES
    report: str | None = None
    reason: str | None = None
    if is_terminal:
        rows = storage.event_list(run_id=run.id, since_id=None, limit=500)
        report = extract_leader_report(rows)
        if status_val not in _SUCCESS_RUN_STATUSES:
            reason = _terminal_reason_from_events(rows)
    return RunResultView(
        run_id=run.id,
        status=_status_str(status_val),
        terminal=is_terminal,
        success=status_val in _SUCCESS_RUN_STATUSES,
        report=report,
        reason=reason,
        finished_at=iso_utc(run.finished_at) if run.finished_at else None,
    )


def _terminal_reason_from_events(events: list[RunEvent]) -> str | None:
    """Best-effort short failure reason for a non-success terminal run.

    Reads the ``detail`` field of the latest ``run_terminal_execution_log`` (or a
    ``run_finalize_failed`` event). Returns None when nothing usable is present.
    """
    wanted = {"run_terminal_execution_log", "run_finalize_failed"}
    for ev in reversed(events):
        if getattr(ev, "type", None) not in wanted:
            continue
        payload = getattr(ev, "payload", None) or {}
        for key in ("detail", "reason", "error", "trigger"):
            val = str(payload.get(key) or "").strip()
            if val:
                return val
    return None


# ──────────────────────────────────────────────────────────────────────
# Actions
# ──────────────────────────────────────────────────────────────────────


@router.post("/runs/{run_id}/abort", response_model=RunSummary)
async def abort_run(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """终止执行流 — the user's irreversible terminate. Destructive finalize.

    Allowed only from pre-review states + ``paused`` (:data:`_TERMINATE_ALLOWED`).
    Once a run is in merge-review / complaint the user acts on merges / complaint
    instead of terminating the whole run. This is the ONLY path that terminates a
    run — the backend never does (it pauses).
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status not in _TERMINATE_ALLOWED:
        raise ApiError(
            "RUN_NOT_RUNNING",
            f"run cannot be terminated in state {_status_str(run.status)}",
            status_code=409,
        )
    sched = get_scheduler()
    # Shared primitive: instant DB flip to aborted + cooperative cancel.
    cancelled = abort_run_to_terminal(run, sched=sched, storage=storage)
    if not cancelled:
        # No live scheduler task to finalize cleanup (e.g. terminating a paused
        # run whose controller is already gone); do the destructive cleanup here.
        flow = storage.flow_get(run.flow_id)
        await _cleanup_terminal_tail(run=run, storage=storage, flow=flow)
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.post("/runs/{run_id}/pause", response_model=RunSummary)
async def pause_run(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """暂停执行 — resumable stop. Cooperative; the controller parks the run.

    The live controller finishes its current tick, tears down sessions, resets
    interrupted tasks to pending, preserves worktrees + team, and lands in
    ``paused``. The user can later 继续执行 (resume) or 终止执行流 (terminate).
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status not in _PAUSE_ALLOWED:
        raise ApiError(
            "RUN_NOT_PAUSABLE",
            f"run cannot be paused in state {_status_str(run.status)}",
            status_code=409,
        )
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "RUN_NOT_RUNNING",
            "no live run to pause",
            status_code=409,
        )
    controller.pause(reason=PAUSE_REASON_USER, detail="user requested pause")
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.post("/runs/{run_id}/continue", response_model=RunSummary)
async def continue_run(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """继续执行 — resume a paused run from where it left off.

    Rebuilds a controller against the existing ClawTeam team + on-disk worktrees
    (see :meth:`FlowScheduler.resume_run`) and re-drives the DAG: completed tasks
    stay completed, interrupted / failed tasks re-run in their existing worktree,
    and an external task's outstanding receipt is honoured (already-arrived →
    progresses; still-waiting → keeps waiting, never re-dispatched).
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status != RunStatus.paused:
        raise ApiError(
            "RUN_NOT_PAUSED",
            f"run is not paused ({_status_str(run.status)})",
            status_code=409,
        )
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {run.flow_id!r} not found", status_code=404)
    sched = get_scheduler()
    sched.resume_run(run=run, flow=flow, storage=storage)
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.get(
    "/runs/{run_id}/pending-merges/{agent_id}/diff",
    response_model=PendingMergeDiffView,
)
async def get_pending_merge_diff(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> PendingMergeDiffView:
    """Return the full unified diff of a pending-merge agent's worktree.

    Powers the "View diff" modal in the awaiting-review UI: the patch is the
    committed content (``base...branch``) that "Merge" would bring into the
    target branch, plus any not-yet-committed working-tree changes (which would
    NOT be merged). Only agents currently in ``pending_merges`` are diffable.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    item = next(
        (p for p in (run.pending_merges or []) if p.get("agent_id") == agent_id),
        None,
    )
    if item is None:
        raise ApiError(
            "MERGE_NOT_PENDING",
            f"agent {agent_id!r} is not in pending_merges",
            status_code=404,
        )
    target_branch = (
        str(item.get("target_branch") or DEFAULT_TARGET_BRANCH).strip()
        or DEFAULT_TARGET_BRANCH
    )
    branch_hint = str(item.get("branch") or "").strip()
    repo = str(item.get("repo_root") or item.get("repo") or "").strip() or None
    if repo is None:
        repo = _resolve_agent_repo_for_run(run=run, agent_id=agent_id, storage=storage)
    cli = get_clawteam_cli()
    try:
        result = await cli.workspace_agent_patch(
            team=run.team_name, agent=agent_id, repo=repo,
        )
    except Exception as exc:  # pragma: no cover - defensive git/subprocess guard
        logger.warning(
            "pending_merge_diff_failed",
            run_id=run.id, agent_id=agent_id, error=str(exc),
        )
        raise ApiError(
            "DIFF_UNAVAILABLE",
            f"failed to compute diff for agent {agent_id!r}",
            status_code=502,
        ) from exc
    if result is None:
        raise ApiError(
            "WORKSPACE_NOT_FOUND",
            f"no worktree found for agent {agent_id!r} (it may have been cleaned up)",
            status_code=404,
        )
    return PendingMergeDiffView(
        agent_id=agent_id,
        branch=str(result.get("branch") or branch_hint),
        base_branch=str(result.get("base_branch") or ""),
        target_branch=target_branch,
        repo_root=str(result.get("repo_root") or ""),
        patch=str(result.get("patch") or ""),
        patch_truncated=bool(result.get("patch_truncated")),
        uncommitted_patch=str(result.get("uncommitted_patch") or ""),
        uncommitted_truncated=bool(result.get("uncommitted_truncated")),
        base_ahead=int(result.get("base_ahead") or 0),
        branch_ahead=int(result.get("branch_ahead") or 0),
    )


@router.get(
    "/runs/{run_id}/checkpoint/items/{task_id}/diff",
    response_model=PendingMergeDiffView,
)
async def get_checkpoint_item_diff(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> PendingMergeDiffView:
    """Return the full worktree diff (vs baseline) for one manual-checkpoint item.

    Powers the "View changes" modal in the awaiting-checkpoint UI: shows every
    modification the item's owner agent has made *so far*, relative to the
    baseline. The active checkpoint lives only on the live controller, so this
    409s when no controller is attached (same as ``GET .../checkpoint``).

    Two reference points, because a checkpoint item may or may not have merged
    already, and both must render:

    * **Not-yet-merged** (normal manual TUI tasks): the live worktree's
      committed content (``base...branch``) plus not-yet-committed working-tree
      changes — :meth:`ClawTeamCli.workspace_agent_patch`.
    * **Auto-merge / self-merge sub-tasks** (OpenClaw always, plus dev/easy
      self-merge tasks): the agent already merged its branch into the baseline as
      the final task step, so the three-dot ``base...branch`` diff is empty (the
      branch is fully contained in the base). We then reconstruct the agent's
      contribution from the baseline's **merge history** —
      :meth:`ClawTeamCli.run_merged_agent_patch`, the same read-only primitive the
      post-run Run diff uses — so the reviewer still sees what changed.

    Read only (no checkout / no lock) in both paths.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    controller = get_scheduler().get_controller(run.id)
    snapshot = controller.checkpoint_snapshot() if controller is not None else None
    if snapshot is None:
        raise ApiError(
            "CHECKPOINT_UNAVAILABLE",
            "run is not awaiting a manual checkpoint",
            status_code=409,
        )
    item = next(
        (it for it in (snapshot.get("items") or []) if it.get("task_id") == task_id),
        None,
    )
    if item is None:
        raise ApiError(
            "CHECKPOINT_ITEM_NOT_FOUND",
            f"checkpoint item {task_id!r} not found",
            status_code=404,
        )
    agent_id = str(item.get("owner_agent_id") or "").strip()
    if not agent_id:
        raise ApiError(
            "CHECKPOINT_ITEM_NOT_FOUND",
            f"checkpoint item {task_id!r} has no owner agent",
            status_code=404,
        )
    repo = _resolve_agent_repo_for_run(run=run, agent_id=agent_id, storage=storage)
    cli = get_clawteam_cli()
    try:
        result = await cli.workspace_agent_patch(
            team=run.team_name, agent=agent_id, repo=repo,
        )
    except Exception as exc:  # pragma: no cover - defensive git/subprocess guard
        logger.warning(
            "checkpoint_diff_failed",
            run_id=run.id, agent_id=agent_id, error=str(exc),
        )
        result = None

    wt = result or {}
    branch = str(wt.get("branch") or item.get("branch_name") or "")
    base_branch = str(wt.get("base_branch") or item.get("base_branch") or "")
    repo_root = str(wt.get("repo_root") or "")
    committed_patch = str(wt.get("patch") or "")
    committed_truncated = bool(wt.get("patch_truncated"))
    uncommitted_patch = str(wt.get("uncommitted_patch") or "")
    uncommitted_truncated = bool(wt.get("uncommitted_truncated"))

    # Auto-merge / self-merge sub-tasks have already landed their work on the
    # baseline, so the three-dot worktree diff is empty. Reconstruct what the
    # agent merged from the baseline's merge history so the change is still shown.
    if not committed_patch.strip():
        try:
            merged = await cli.run_merged_agent_patch(
                team=run.team_name, agent=agent_id, repo=repo, include_patch=True,
            )
        except Exception as exc:  # pragma: no cover - defensive git/subprocess guard
            logger.warning(
                "checkpoint_merged_diff_failed",
                run_id=run.id, agent_id=agent_id, error=str(exc),
            )
            merged = None
        if merged and str(merged.get("patch") or "").strip():
            committed_patch = str(merged.get("patch") or "")
            committed_truncated = bool(merged.get("patch_truncated"))
            branch = branch or str(merged.get("branch") or "")
            repo_root = repo_root or str(merged.get("repo_root") or "")

    # Nothing to show and no live worktree — the workspace was cleaned up and no
    # matching merge exists in history.
    if result is None and not committed_patch.strip() and not uncommitted_patch.strip():
        raise ApiError(
            "WORKSPACE_NOT_FOUND",
            f"no worktree or merged history found for agent {agent_id!r} "
            "(it may have been cleaned up)",
            status_code=404,
        )

    return PendingMergeDiffView(
        agent_id=agent_id,
        branch=branch,
        base_branch=base_branch,
        # No merge target at a checkpoint — the diff is purely "vs baseline", so
        # the header reads "<branch> (base: <base>) → <base>".
        target_branch=base_branch or DEFAULT_TARGET_BRANCH,
        repo_root=repo_root,
        patch=committed_patch,
        patch_truncated=committed_truncated,
        uncommitted_patch=uncommitted_patch,
        uncommitted_truncated=uncommitted_truncated,
        base_ahead=int(wt.get("base_ahead") or 0),
        branch_ahead=int(wt.get("branch_ahead") or 0),
    )


def _run_diff_agents(run: FlowRun, storage: StorageBackend) -> list[FlowAgent]:
    """Agents eligible for the post-run Run-diff module (spec order).

    Includes OpenClaw (in-task self-merge) AND the leader — the leader owns a
    worktree for its summary task and may modify + merge it into the baseline
    exactly like any worker, so the user must be able to see and revert it.
    Excludes only ``merge_strategy=skip`` nodes (external executors, which own
    no worktree/branch). Returns ``[]`` when the flow/spec can't be loaded.
    """
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        return []
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        return []
    return [a for a in spec.agents if a.merge_strategy != MergeStrategy.skip]


@router.get("/runs/{run_id}/run-diff", response_model=RunDiffView)
async def get_run_diff(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunDiffView:
    """List each eligible agent whose branch actually merged content into a
    baseline branch during this run (the post-run "Run diff" module).

    Reconstructed read-only from baseline-repo merge history (see
    :meth:`ClawTeamCli.run_merged_agent_patch`), so it survives worktree cleanup
    and correctly attributes commits to *this* run even when a baseline branch is
    shared by concurrent runs. An agent is shown only when its merge brought
    **effective file changes** (``files_changed > 0``) — a merge commit that
    landed zero net changes is NOT shown. Agents the user reverted ("撤销合入")
    are also excluded. No patch text here — the caller fetches a single agent's
    full diff lazily via the per-agent endpoint.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    reverted = _read_reverted_merge_agent_ids(run)
    cli = get_clawteam_cli()
    items: list[RunDiffAgentView] = []
    for agent in _run_diff_agents(run, storage):
        if agent.id in reverted:
            continue
        repo = str(agent.repo or "").strip() or None
        try:
            result = await cli.run_merged_agent_patch(
                team=run.team_name, agent=agent.id, repo=repo, include_patch=False,
            )
        except Exception as exc:  # pragma: no cover - defensive git/subprocess guard
            logger.warning(
                "run_diff_summary_failed",
                run_id=run.id, agent_id=agent.id, error=str(exc),
            )
            continue
        # Show only agents that landed real file changes — a merge commit with
        # zero net changes (e.g. duplicated/empty content) is not an effective merge.
        if result is None or int(result.get("files_changed") or 0) <= 0:
            continue
        items.append(
            RunDiffAgentView(
                agent_id=agent.id,
                branch=str(result.get("branch") or ""),
                repo_root=str(result.get("repo_root") or ""),
                merge_count=int(result.get("merge_count") or 0),
                commit_count=int(result.get("commit_count") or 0),
                files_changed=int(result.get("files_changed") or 0),
                insertions=int(result.get("insertions") or 0),
                deletions=int(result.get("deletions") or 0),
            )
        )
    return RunDiffView(items=items)


@router.get(
    "/runs/{run_id}/run-diff/{agent_id}",
    response_model=RunAgentDiffView,
)
async def get_run_agent_diff(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunAgentDiffView:
    """Full unified diff of what *agent_id* merged into a baseline this run.

    Powers the "View diff" modal in the Run-diff module. Read-only history
    reconstruction (no checkout / no lock). 404 when the agent is unknown/
    ineligible (``merge_strategy=skip`` external node) or nothing of theirs
    merged. OpenClaw + leader ARE eligible (see :func:`_run_diff_agents`).
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if agent_id in _read_reverted_merge_agent_ids(run):
        raise ApiError(
            "MERGE_REVERTED",
            f"agent {agent_id!r} merge was reverted",
            status_code=404,
        )
    agent = next(
        (a for a in _run_diff_agents(run, storage) if a.id == agent_id), None,
    )
    if agent is None:
        raise ApiError(
            "AGENT_NOT_FOUND",
            f"agent {agent_id!r} is not eligible for Run diff in this run",
            status_code=404,
        )
    repo = str(agent.repo or "").strip() or None
    cli = get_clawteam_cli()
    try:
        result = await cli.run_merged_agent_patch(
            team=run.team_name, agent=agent.id, repo=repo, include_patch=True,
        )
    except Exception as exc:  # pragma: no cover - defensive git/subprocess guard
        logger.warning(
            "run_diff_failed", run_id=run.id, agent_id=agent.id, error=str(exc),
        )
        raise ApiError(
            "DIFF_UNAVAILABLE",
            f"failed to compute run diff for agent {agent_id!r}",
            status_code=502,
        ) from exc
    if result is None or int(result.get("files_changed") or 0) <= 0:
        raise ApiError(
            "NO_MERGED_CHANGES",
            f"agent {agent_id!r} merged no effective content into a baseline this run",
            status_code=404,
        )
    return RunAgentDiffView(
        agent_id=agent.id,
        branch=str(result.get("branch") or ""),
        repo_root=str(result.get("repo_root") or ""),
        merge_count=int(result.get("merge_count") or 0),
        commit_count=int(result.get("commit_count") or 0),
        files_changed=int(result.get("files_changed") or 0),
        insertions=int(result.get("insertions") or 0),
        deletions=int(result.get("deletions") or 0),
        patch=str(result.get("patch") or ""),
        patch_truncated=bool(result.get("patch_truncated")),
    )


@router.post(
    "/runs/{run_id}/run-diff/{agent_id}/revert",
    response_model=RunMergeRevertView,
)
async def revert_run_agent_merge(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunMergeRevertView:
    """Revert (撤销合入) this run's merges of *agent_id* on its baseline branch.

    Available while the run awaits complaint input and at any terminal status.
    Uses ``git revert -m 1`` — a **non-destructive** operation that adds
    inverse commits; it never rewrites history or edits files by any other
    means. If git can't do it cleanly (conflicts with later commits, dirty
    tree, …) nothing is changed and the git reason is returned (``ok=false``,
    ``409 MERGE_REVERT_FAILED``). On success the agent is recorded as reverted
    and disappears from the Run-diff module; its worktree is NOT preserved —
    the normal (deferred) terminal cleanup removes it.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    # Block while the scheduler / complaint agents may be writing baselines
    # (running, complaint_processing, …); allow awaiting_user_complaint (no
    # scheduler activity — the run is parked on user input) and terminals.
    if run.status not in _TERMINAL and run.status != RunStatus.awaiting_user_complaint:
        raise ApiError(
            "MERGE_REVERT_NOT_ALLOWED",
            f"revert is not available in status {_status_str(run.status)}",
            status_code=409,
        )
    agent = next(
        (a for a in _run_diff_agents(run, storage) if a.id == agent_id), None,
    )
    if agent is None:
        raise ApiError(
            "AGENT_NOT_FOUND",
            f"agent {agent_id!r} is not eligible for Run diff in this run",
            status_code=404,
        )
    if agent_id in _read_reverted_merge_agent_ids(run):
        # Idempotent: already reverted → report success without touching git.
        return RunMergeRevertView(
            agent_id=agent_id, ok=True,
            target_branch=str(agent.target_branch or DEFAULT_TARGET_BRANCH),
            message="already reverted",
        )
    repo = str(agent.repo or "").strip() or None
    target = str(agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
    cli = get_clawteam_cli()
    try:
        result = await cli.revert_agent_merges(
            team=run.team_name, agent=agent.id, repo=repo, target_branch=target,
        )
    except Exception as exc:  # pragma: no cover - defensive git/subprocess guard
        logger.warning(
            "run_merge_revert_error", run_id=run.id, agent_id=agent.id, error=str(exc),
        )
        raise ApiError(
            "MERGE_REVERT_FAILED",
            f"failed to revert merges for agent {agent_id!r}: {exc}",
            status_code=409,
        ) from exc
    if not result.get("ok"):
        _emit_run_event(
            storage, run.id, "run_merge_revert_failed", agent_id=agent_id,
            payload={"target_branch": target, "message": str(result.get("message") or "")},
        )
        raise ApiError(
            "MERGE_REVERT_FAILED",
            str(result.get("message") or "git revert failed"),
            status_code=409,
        )
    _mark_merge_reverted(run=run, storage=storage, agent_id=agent_id)
    _emit_run_event(
        storage, run.id, "run_merge_reverted", agent_id=agent_id,
        payload={
            "target_branch": result.get("target_branch") or target,
            "reverted_merges": result.get("merge_shas") or [],
            "revert_head": result.get("revert_head") or "",
        },
    )
    return RunMergeRevertView(
        agent_id=agent_id,
        ok=True,
        target_branch=str(result.get("target_branch") or target),
        reverted_merges=[str(s) for s in (result.get("merge_shas") or [])],
        revert_head=str(result.get("revert_head") or ""),
        message=str(result.get("message") or ""),
    )


# ──────────────────────────────────────────────────────────────────────
# Developer-mode PR module (pending PRs)
# ──────────────────────────────────────────────────────────────────────


def _list_dev_pending_pr_agent_ids(run: FlowRun) -> list[str]:
    """Marker list in original (spec) order, de-duplicated."""
    raw = (run.inputs or {}).get(DEV_PENDING_PR_AGENT_IDS_KEY)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        aid = str(item or "").strip()
        if aid and aid not in seen:
            out.append(aid)
            seen.add(aid)
    return out


def _remove_dev_pending_pr_agent(
    *, run: FlowRun, storage: StorageBackend, agent_id: str,
) -> list[str]:
    """Drop *agent_id* from the pending-PR marker; returns the remaining ids."""
    remaining = [a for a in _list_dev_pending_pr_agent_ids(run) if a != agent_id]
    merged_inputs = dict(run.inputs or {})
    if remaining:
        merged_inputs[DEV_PENDING_PR_AGENT_IDS_KEY] = remaining
    else:
        merged_inputs.pop(DEV_PENDING_PR_AGENT_IDS_KEY, None)
    run.inputs = merged_inputs
    storage.run_update(run)
    return remaining


def _flow_currently_dev_mode(flow: Flow | None) -> bool:
    if flow is None:
        return False
    from app.flow_modes import flow_mode

    return flow_mode((flow.spec or {}).get("variables") or {}) == "dev"


def _spec_agent_for_run(
    *, run: FlowRun, agent_id: str, storage: StorageBackend,
) -> FlowAgent | None:
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        return None
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        return None
    return next((a for a in spec.agents if a.id == agent_id), None)


async def _find_pending_pr_workspace_row(
    *, run: FlowRun, agent_id: str, storage: StorageBackend,
) -> dict[str, Any] | None:
    """Resolve the live worktree row for a pending-PR agent (None when gone).

    A worktree removed out-of-band simply disappears from the module by
    design — we never surface an error for it.
    """
    repo = _resolve_agent_repo_for_run(run=run, agent_id=agent_id, storage=storage)
    cli = get_clawteam_cli()
    try:
        rows = await cli.workspace_list(team=run.team_name, repo=repo)
    except Exception as exc:
        logger.warning(
            "pending_pr_workspace_list_failed",
            run_id=run.id, agent_id=agent_id, error=str(exc),
        )
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("agent_name") or "").strip() != agent_id:
            continue
        wt = str(row.get("worktree_path") or "").strip()
        if not wt or not FsPath(wt).expanduser().exists():
            return None
        return row
    return None


_PR_PUSH_TIMEOUT_SEC = 300.0
_PR_CREATE_TIMEOUT_SEC = 120.0


async def _run_pr_command(
    argv: list[str], *, cwd: str, timeout_sec: float,
) -> tuple[int, str, str]:
    """Run one PR-pipeline subprocess with a hard timeout + group kill."""
    import os
    import signal

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        return 124, "", f"timed out after {int(timeout_sec)}s: {' '.join(argv)}"
    return (
        proc.returncode or 0,
        (stdout_b or b"").decode(errors="replace"),
        (stderr_b or b"").decode(errors="replace"),
    )


async def _pending_pr_tail_cleanup_if_done(
    *, run: FlowRun, storage: StorageBackend, remaining: list[str],
) -> None:
    if remaining:
        return
    flow = storage.flow_get(run.flow_id)
    await _cleanup_terminal_tail(run=run, storage=storage, flow=flow)


def _ensure_pending_pr_actionable(run: FlowRun) -> None:
    """Reject PR-module actions outside allowed statuses (409).

    During ``complaint_processing`` headless fix agents run with worktrees as
    cwd; user merge/PR/discard actions must not race them.
    """
    if run.status not in _PR_MODULE_ACTIONABLE_STATUSES:
        raise ApiError(
            "PR_NOT_ACTIONABLE",
            f"pending-PR actions are not available in status {_status_str(run.status)}",
            status_code=409,
        )


async def _pending_pr_post_action_cleanup(
    *, run: FlowRun, storage: StorageBackend, agent_id: str, remaining: list[str],
) -> None:
    """Worktree cleanup after a successful submit / merge / discard.

    Terminal runs clean the agent worktree immediately (and trigger the
    deferred team cleanup once the marker empties). While the run is still
    ``awaiting_user_complaint`` ALL deletions are deferred: complaint fix
    agents need worktrees as their cwd, and the whole team is swept in one
    place when the complaint phase finishes (run_terminal_tail_cleanup — a
    handled agent is no longer in the marker, so it is not preserved there).
    """
    if run.status not in _TERMINAL:
        return
    await cleanup_non_openclaw_workspace_after_review_decision(
        run=run, agent_id=agent_id, storage=storage,
    )
    await _pending_pr_tail_cleanup_if_done(run=run, storage=storage, remaining=remaining)


async def _clear_worktree_uncommitted(
    *, run: FlowRun, storage: StorageBackend, agent_id: str, worktree: str,
) -> None:
    """Drop ALL uncommitted changes in *worktree* before a merge / PR push.

    Defensive: dispatch prompts require agents to commit their work in-task
    and complaint-phase Hermes agents are told not to touch worktrees at all —
    but if anything slipped through, uncommitted noise must never ride into a
    user-triggered merge/PR. Instrumented: when dirt IS found we log a warning
    and emit a ``worktree_uncommitted_cleared`` RunEvent, so "did the agent
    commit as instructed?" is auditable from logs.
    """
    cli = get_clawteam_cli()
    entries: list[str] = []
    try:
        dirty, entries = await cli.workspace_has_uncommitted_changes(
            worktree_path=worktree,
        )
    except Exception as exc:
        logger.warning(
            "pending_pr_dirty_check_failed",
            run_id=run.id, agent_id=agent_id, error=str(exc),
        )
        dirty = True  # be safe: still run the reset below
        entries = []
    if not dirty:
        return
    if entries:
        # Instrumentation: the agent did NOT commit everything as instructed.
        logger.warning(
            "agent_left_uncommitted_changes",
            run_id=run.id, agent_id=agent_id,
            worktree=worktree, entries=entries[:50],
        )
        _emit_run_event(
            storage, run.id, "worktree_uncommitted_cleared", agent_id=agent_id,
            payload={"worktree": worktree, "entries": entries[:50]},
        )
    for argv in (["git", "reset", "--hard", "HEAD"], ["git", "clean", "-fd"]):
        rc, _out, err = await _run_pr_command(argv, cwd=worktree, timeout_sec=60.0)
        if rc != 0:
            logger.warning(
                "pending_pr_worktree_clear_failed",
                run_id=run.id, agent_id=agent_id,
                argv=argv, detail=(err or "")[:500],
            )


@router.get("/runs/{run_id}/pending-prs", response_model=PendingPrListView)
async def list_pending_prs(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> PendingPrListView:
    """Dev-mode PR module list: agents whose worktree still awaits a PR decision.

    Empty (module hidden) unless ALL of: the run recorded pending-PR agents at
    finalize (i.e. it EXECUTED in developer mode), the Flow is CURRENTLY in
    developer mode, the run status is in ``_PR_MODULE_VISIBLE_STATUSES``
    (``awaiting_user_complaint``, ``complaint_processing``, or a healthy
    terminal), and the agent's worktree still exists on disk (out-of-band
    deletions silently drop out — never an error).
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    # Module surfaces while awaiting complaint input, during complaint
    # processing (read-only — actions are gated separately), and at healthy
    # terminals. Abnormal terminals (aborted / failed / complaint_failed /
    # orphaned) stay hidden because termination force-cleans all worktrees
    # even if a stale marker lingers in run.inputs.
    if run.status not in _PR_MODULE_VISIBLE_STATUSES:
        return PendingPrListView()
    pending_ids = _list_dev_pending_pr_agent_ids(run)
    if not pending_ids:
        return PendingPrListView()
    flow = storage.flow_get(run.flow_id)
    if not _flow_currently_dev_mode(flow):
        return PendingPrListView()
    items: list[PendingPrAgentView] = []
    for agent_id in pending_ids:
        agent = _spec_agent_for_run(run=run, agent_id=agent_id, storage=storage)
        if agent is None or agent.kind == AgentKind.openclaw:
            continue
        row = await _find_pending_pr_workspace_row(
            run=run, agent_id=agent_id, storage=storage,
        )
        if row is None:
            continue
        target = (agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
        items.append(
            PendingPrAgentView(
                agent_id=agent_id,
                branch=str(row.get("branch_name") or ""),
                base_branch=str(row.get("base_branch") or ""),
                target_branch=target,
                repo_root=str(row.get("repo_root") or ""),
                worktree_path=str(row.get("worktree_path") or ""),
            )
        )
    return PendingPrListView(items=items)


@router.get(
    "/runs/{run_id}/pending-prs/{agent_id}/diff",
    response_model=PendingMergeDiffView,
)
async def get_pending_pr_diff(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> PendingMergeDiffView:
    """Full unified diff of a pending-PR agent's worktree ("查看全部修改")."""
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if agent_id not in _list_dev_pending_pr_agent_ids(run):
        raise ApiError(
            "PR_NOT_PENDING",
            f"agent {agent_id!r} has no pending PR in this run",
            status_code=404,
        )
    agent = _spec_agent_for_run(run=run, agent_id=agent_id, storage=storage)
    target = DEFAULT_TARGET_BRANCH
    if agent is not None:
        target = (agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
    repo = _resolve_agent_repo_for_run(run=run, agent_id=agent_id, storage=storage)
    cli = get_clawteam_cli()
    try:
        result = await cli.workspace_agent_patch(
            team=run.team_name, agent=agent_id, repo=repo,
        )
    except Exception as exc:  # pragma: no cover - defensive git/subprocess guard
        logger.warning(
            "pending_pr_diff_failed",
            run_id=run.id, agent_id=agent_id, error=str(exc),
        )
        raise ApiError(
            "DIFF_UNAVAILABLE",
            f"failed to compute diff for agent {agent_id!r}",
            status_code=502,
        ) from exc
    if result is None:
        raise ApiError(
            "WORKSPACE_NOT_FOUND",
            f"no worktree found for agent {agent_id!r} (it may have been cleaned up)",
            status_code=404,
        )
    return PendingMergeDiffView(
        agent_id=agent_id,
        branch=str(result.get("branch") or ""),
        base_branch=str(result.get("base_branch") or ""),
        target_branch=target,
        repo_root=str(result.get("repo_root") or ""),
        patch=str(result.get("patch") or ""),
        patch_truncated=bool(result.get("patch_truncated")),
        uncommitted_patch=str(result.get("uncommitted_patch") or ""),
        uncommitted_truncated=bool(result.get("uncommitted_truncated")),
        base_ahead=int(result.get("base_ahead") or 0),
        branch_ahead=int(result.get("branch_ahead") or 0),
    )


@router.post(
    "/runs/{run_id}/pending-prs/{agent_id}/submit",
    response_model=PendingPrSubmitResponse,
)
async def submit_pending_pr(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> PendingPrSubmitResponse:
    """One-click PR: push the worktree branch, open a PR against the baseline.

    Remote validity is the developer's responsibility by design (dev mode
    only): we simply run ``git push -u origin <branch>`` then
    ``gh pr create --base <baseline> --head <branch>`` inside the worktree and
    surface any failure verbatim (frontend shows it in an alert; nothing is
    mutated on failure). On success the local worktree is removed and the agent
    leaves the module.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    _ensure_pending_pr_actionable(run)
    if agent_id not in _list_dev_pending_pr_agent_ids(run):
        raise ApiError(
            "PR_NOT_PENDING",
            f"agent {agent_id!r} has no pending PR in this run",
            status_code=404,
        )
    flow = storage.flow_get(run.flow_id)
    if not _flow_currently_dev_mode(flow):
        raise ApiError(
            "NOT_DEV_MODE",
            "the Flow is not in developer mode",
            status_code=409,
        )
    row = await _find_pending_pr_workspace_row(
        run=run, agent_id=agent_id, storage=storage,
    )
    if row is None:
        raise ApiError(
            "WORKSPACE_NOT_FOUND",
            f"no worktree found for agent {agent_id!r} (it may have been cleaned up)",
            status_code=404,
        )
    worktree = str(FsPath(str(row.get("worktree_path") or "")).expanduser())
    branch = str(row.get("branch_name") or "").strip()
    if not branch:
        branch = f"clawteam/{run.team_name}/{agent_id}"
    agent = _spec_agent_for_run(run=run, agent_id=agent_id, storage=storage)
    target = DEFAULT_TARGET_BRANCH
    if agent is not None:
        target = (agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH

    # Defensive: only committed content may ride into the PR.
    await _clear_worktree_uncommitted(
        run=run, storage=storage, agent_id=agent_id, worktree=worktree,
    )
    rc, out, err = await _run_pr_command(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree, timeout_sec=_PR_PUSH_TIMEOUT_SEC,
    )
    if rc != 0:
        detail = (err or out).strip()[:1000]
        _emit_run_event(
            storage, run.id, "dev_pr_submit_failed", agent_id=agent_id,
            payload={"step": "push", "branch": branch, "detail": detail},
        )
        return PendingPrSubmitResponse(
            agent_id=agent_id, success=False,
            message=f"git push failed: {detail}",
        )

    title = f"[ClawsomeFlow] {agent_id}: {branch} -> {target}"
    body = (
        f"Automated PR opened by ClawsomeFlow developer mode.\n\n"
        f"- Run: {run.id}\n- Agent: {agent_id}\n- Branch: `{branch}` -> `{target}`"
    )
    rc, out, err = await _run_pr_command(
        ["gh", "pr", "create", "--base", target, "--head", branch,
         "--title", title, "--body", body],
        cwd=worktree, timeout_sec=_PR_CREATE_TIMEOUT_SEC,
    )
    if rc != 0:
        detail = (err or out).strip()[:1000]
        _emit_run_event(
            storage, run.id, "dev_pr_submit_failed", agent_id=agent_id,
            payload={"step": "pr_create", "branch": branch, "detail": detail},
        )
        return PendingPrSubmitResponse(
            agent_id=agent_id, success=False,
            message=f"gh pr create failed: {detail}",
        )
    pr_url = next(
        (ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith("http")),
        "",
    )
    _emit_run_event(
        storage, run.id, "dev_pr_submitted", agent_id=agent_id,
        payload={"branch": branch, "target_branch": target, "pr_url": pr_url},
    )
    remaining = _remove_dev_pending_pr_agent(run=run, storage=storage, agent_id=agent_id)
    await _pending_pr_post_action_cleanup(
        run=run, storage=storage, agent_id=agent_id, remaining=remaining,
    )
    return PendingPrSubmitResponse(
        agent_id=agent_id, success=True, pr_url=pr_url, message="PR created",
    )


@router.post(
    "/runs/{run_id}/pending-prs/{agent_id}/discard",
    response_model=RunSummary,
)
async def discard_pending_pr(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Discard a pending-PR worktree without pushing anything anywhere."""
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    _ensure_pending_pr_actionable(run)
    if agent_id not in _list_dev_pending_pr_agent_ids(run):
        raise ApiError(
            "PR_NOT_PENDING",
            f"agent {agent_id!r} has no pending PR in this run",
            status_code=404,
        )
    _emit_run_event(
        storage, run.id, "dev_pr_discarded", agent_id=agent_id, payload={},
    )
    remaining = _remove_dev_pending_pr_agent(run=run, storage=storage, agent_id=agent_id)
    await _pending_pr_post_action_cleanup(
        run=run, storage=storage, agent_id=agent_id, remaining=remaining,
    )
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.post(
    "/runs/{run_id}/pending-prs/{agent_id}/merge",
    response_model=MergeResponse,
)
async def merge_pending_pr(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> MergeResponse:
    """Directly merge a pending-PR agent's worktree branch into its baseline.

    Local-only (no remote required — the locked merge fetches origin only when
    one is configured, otherwise it is a pure local merge), so this works even
    when the repo has no remote. On success the worktree is removed and the
    agent leaves the module; on failure nothing changes and the git reason is
    surfaced (the frontend shows an alert). Requires the Flow to currently be in
    developer mode.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    _ensure_pending_pr_actionable(run)
    if agent_id not in _list_dev_pending_pr_agent_ids(run):
        raise ApiError(
            "PR_NOT_PENDING",
            f"agent {agent_id!r} has no pending PR in this run",
            status_code=404,
        )
    flow = storage.flow_get(run.flow_id)
    if not _flow_currently_dev_mode(flow):
        raise ApiError(
            "NOT_DEV_MODE",
            "the Flow is not in developer mode",
            status_code=409,
        )
    agent = _spec_agent_for_run(run=run, agent_id=agent_id, storage=storage)
    target = DEFAULT_TARGET_BRANCH
    if agent is not None:
        target = (agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
    source_branch = f"clawteam/{run.team_name}/{agent_id}"
    merge_repo = _resolve_agent_repo_for_run(run=run, agent_id=agent_id, storage=storage)
    # Defensive: uncommitted worktree noise never rides into the merge (the
    # merge itself only takes commits, but a dirty worktree left by an agent
    # is also evidence it skipped its mandatory commit step — log + clear).
    row = await _find_pending_pr_workspace_row(
        run=run, agent_id=agent_id, storage=storage,
    )
    if row is not None:
        wt = str(FsPath(str(row.get("worktree_path") or "")).expanduser())
        if wt:
            await _clear_worktree_uncommitted(
                run=run, storage=storage, agent_id=agent_id, worktree=wt,
            )
    cli = get_clawteam_cli()
    ok, msg = await cli.workspace_merge(
        team=run.team_name, agent=agent_id, repo=merge_repo, target=target,
    )
    if ok:
        _emit_run_event(
            storage, run.id, "dev_pr_merged", agent_id=agent_id,
            payload={"source_branch": source_branch, "target_branch": target,
                     "repo_root": merge_repo},
        )
    else:
        failure_kind = classify_merge_failure(msg)
        _emit_run_event(
            storage, run.id,
            "merge_conflict" if failure_kind == "conflict" else "merge_error",
            agent_id=agent_id,
            payload={"source_branch": source_branch, "target_branch": target,
                     "repo_root": merge_repo, "stderr": (msg or "")[:1000],
                     "failure_kind": failure_kind},
        )
    if not ok:
        # Keep the worktree + marker so the user can retry / discard / PR.
        reason = (msg or "").strip()[:900]
        if failure_kind == "conflict":
            guidance = (
                "Merge failed due to a git conflict. Resolve it manually in the "
                "agent worktree, then retry or discard. The worktree is kept intact."
            )
        else:
            guidance = (
                "Merge failed due to an environment/repository error. Verify the "
                "repository/branch state, then retry. The worktree is kept intact."
            )
        message = f"{guidance}\n\n{reason}" if reason else guidance
        return MergeResponse(agent_id=agent_id, success=False, message=message[:1000])
    remaining = _remove_dev_pending_pr_agent(run=run, storage=storage, agent_id=agent_id)
    await _pending_pr_post_action_cleanup(
        run=run, storage=storage, agent_id=agent_id, remaining=remaining,
    )
    return MergeResponse(agent_id=agent_id, success=True, message="merged")


# ──────────────────────────────────────────────────────────────────────
# Failed in-task auto-merge module (easy / dev manual runs)
# ──────────────────────────────────────────────────────────────────────


def _list_failed_auto_merge_agent_ids(run: FlowRun) -> list[str]:
    raw = (run.inputs or {}).get(FAILED_AUTO_MERGE_AGENT_IDS_KEY)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        aid = str(item or "").strip()
        if aid and aid not in seen:
            out.append(aid)
            seen.add(aid)
    return out


def _remove_failed_auto_merge_agent(
    *, run: FlowRun, storage: StorageBackend, agent_id: str,
) -> list[str]:
    remaining = [a for a in _list_failed_auto_merge_agent_ids(run) if a != agent_id]
    merged_inputs = dict(run.inputs or {})
    if remaining:
        merged_inputs[FAILED_AUTO_MERGE_AGENT_IDS_KEY] = remaining
    else:
        merged_inputs.pop(FAILED_AUTO_MERGE_AGENT_IDS_KEY, None)
    run.inputs = merged_inputs
    storage.run_update(run)
    return remaining


def _flow_easy_or_dev_mode(flow: Flow | None) -> bool:
    if flow is None:
        return False
    from app.flow_modes import flow_mode

    return flow_mode((flow.spec or {}).get("variables") or {}) in ("easy", "dev")


def _ensure_failed_auto_merge_actionable(run: FlowRun) -> None:
    if run.status not in _PR_MODULE_ACTIONABLE_STATUSES:
        raise ApiError(
            "AUTO_MERGE_NOT_ACTIONABLE",
            f"failed auto-merge actions are not available in status "
            f"{_status_str(run.status)}",
            status_code=409,
        )


async def _failed_auto_merge_post_action_cleanup(
    *, run: FlowRun, storage: StorageBackend, agent_id: str, remaining: list[str],
) -> None:
    await _pending_pr_post_action_cleanup(
        run=run, storage=storage, agent_id=agent_id, remaining=remaining,
    )


@router.get("/runs/{run_id}/failed-auto-merges", response_model=PendingPrListView)
async def list_failed_auto_merges(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> PendingPrListView:
    """Agents that should have self-merged in-task but did not (easy/dev runs)."""
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status not in _PR_MODULE_VISIBLE_STATUSES:
        return PendingPrListView()
    pending_ids = _list_failed_auto_merge_agent_ids(run)
    if not pending_ids:
        return PendingPrListView()
    flow = storage.flow_get(run.flow_id)
    if not _flow_easy_or_dev_mode(flow):
        return PendingPrListView()
    items: list[PendingPrAgentView] = []
    for agent_id in pending_ids:
        agent = _spec_agent_for_run(run=run, agent_id=agent_id, storage=storage)
        if agent is None:
            continue
        row = await _find_pending_pr_workspace_row(
            run=run, agent_id=agent_id, storage=storage,
        )
        if row is None:
            continue
        target = (agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
        items.append(
            PendingPrAgentView(
                agent_id=agent_id,
                branch=str(row.get("branch_name") or ""),
                base_branch=str(row.get("base_branch") or ""),
                target_branch=target,
                repo_root=str(row.get("repo_root") or ""),
                worktree_path=str(row.get("worktree_path") or ""),
            )
        )
    return PendingPrListView(items=items)


@router.get(
    "/runs/{run_id}/failed-auto-merges/{agent_id}/diff",
    response_model=PendingMergeDiffView,
)
async def get_failed_auto_merge_diff(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> PendingMergeDiffView:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if agent_id not in _list_failed_auto_merge_agent_ids(run):
        raise ApiError(
            "AUTO_MERGE_NOT_PENDING",
            f"agent {agent_id!r} has no failed auto-merge entry in this run",
            status_code=404,
        )
    agent = _spec_agent_for_run(run=run, agent_id=agent_id, storage=storage)
    target = DEFAULT_TARGET_BRANCH
    if agent is not None:
        target = (agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
    repo = _resolve_agent_repo_for_run(run=run, agent_id=agent_id, storage=storage)
    cli = get_clawteam_cli()
    try:
        result = await cli.workspace_agent_patch(
            team=run.team_name, agent=agent_id, repo=repo,
        )
    except Exception as exc:
        logger.warning(
            "failed_auto_merge_diff_failed",
            run_id=run.id, agent_id=agent_id, error=str(exc),
        )
        raise ApiError(
            "DIFF_UNAVAILABLE",
            f"failed to compute diff for agent {agent_id!r}",
            status_code=502,
        ) from exc
    if result is None:
        raise ApiError(
            "WORKSPACE_NOT_FOUND",
            f"no worktree found for agent {agent_id!r} (it may have been cleaned up)",
            status_code=404,
        )
    return PendingMergeDiffView(
        agent_id=agent_id,
        branch=str(result.get("branch") or ""),
        base_branch=str(result.get("base_branch") or ""),
        target_branch=target,
        repo_root=str(result.get("repo_root") or ""),
        patch=str(result.get("patch") or ""),
        patch_truncated=bool(result.get("patch_truncated")),
        uncommitted_patch=str(result.get("uncommitted_patch") or ""),
        uncommitted_truncated=bool(result.get("uncommitted_truncated")),
        base_ahead=int(result.get("base_ahead") or 0),
        branch_ahead=int(result.get("branch_ahead") or 0),
    )


@router.post(
    "/runs/{run_id}/failed-auto-merges/{agent_id}/merge",
    response_model=MergeResponse,
)
async def merge_failed_auto_merge(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> MergeResponse:
    """Merge a failed auto-merge agent's worktree into its baseline branch."""
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    _ensure_failed_auto_merge_actionable(run)
    if agent_id not in _list_failed_auto_merge_agent_ids(run):
        raise ApiError(
            "AUTO_MERGE_NOT_PENDING",
            f"agent {agent_id!r} has no failed auto-merge entry in this run",
            status_code=404,
        )
    flow = storage.flow_get(run.flow_id)
    if not _flow_easy_or_dev_mode(flow):
        raise ApiError(
            "NOT_EASY_OR_DEV_MODE",
            "the Flow is not in easy or developer mode",
            status_code=409,
        )
    agent = _spec_agent_for_run(run=run, agent_id=agent_id, storage=storage)
    target = DEFAULT_TARGET_BRANCH
    if agent is not None:
        target = (agent.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
    source_branch = f"clawteam/{run.team_name}/{agent_id}"
    merge_repo = _resolve_agent_repo_for_run(run=run, agent_id=agent_id, storage=storage)
    row = await _find_pending_pr_workspace_row(
        run=run, agent_id=agent_id, storage=storage,
    )
    if row is not None:
        wt = str(FsPath(str(row.get("worktree_path") or "")).expanduser())
        if wt:
            await _clear_worktree_uncommitted(
                run=run, storage=storage, agent_id=agent_id, worktree=wt,
            )
    cli = get_clawteam_cli()
    ok, msg = await cli.workspace_merge(
        team=run.team_name, agent=agent_id, repo=merge_repo, target=target,
    )
    if ok:
        _emit_run_event(
            storage, run.id, "failed_auto_merge_merged", agent_id=agent_id,
            payload={"source_branch": source_branch, "target_branch": target,
                     "repo_root": merge_repo},
        )
    else:
        failure_kind = classify_merge_failure(msg)
        _emit_run_event(
            storage, run.id,
            "merge_conflict" if failure_kind == "conflict" else "merge_error",
            agent_id=agent_id,
            payload={"source_branch": source_branch, "target_branch": target,
                     "repo_root": merge_repo, "stderr": (msg or "")[:1000],
                     "failure_kind": failure_kind, "context": "failed_auto_merge"},
        )
    if not ok:
        reason = (msg or "").strip()[:900]
        return MergeResponse(agent_id=agent_id, success=False, message=reason)
    remaining = _remove_failed_auto_merge_agent(
        run=run, storage=storage, agent_id=agent_id,
    )
    await _failed_auto_merge_post_action_cleanup(
        run=run, storage=storage, agent_id=agent_id, remaining=remaining,
    )
    return MergeResponse(agent_id=agent_id, success=True, message="merged")


@router.post(
    "/runs/{run_id}/failed-auto-merges/{agent_id}/discard",
    response_model=RunSummary,
)
async def discard_failed_auto_merge(
    run_id: Annotated[str, Path()],
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Discard worktree without merging (failed auto-merge module)."""
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    _ensure_failed_auto_merge_actionable(run)
    if agent_id not in _list_failed_auto_merge_agent_ids(run):
        raise ApiError(
            "AUTO_MERGE_NOT_PENDING",
            f"agent {agent_id!r} has no failed auto-merge entry in this run",
            status_code=404,
        )
    _emit_run_event(
        storage, run.id, "failed_auto_merge_discarded", agent_id=agent_id, payload={},
    )
    remaining = _remove_failed_auto_merge_agent(
        run=run, storage=storage, agent_id=agent_id,
    )
    await _failed_auto_merge_post_action_cleanup(
        run=run, storage=storage, agent_id=agent_id, remaining=remaining,
    )
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.post("/runs/{run_id}/merge", response_model=MergeResponse)
async def merge_pending(
    run_id: Annotated[str, Path()],
    payload: Annotated[MergePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> MergeResponse:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status not in _MERGE_DECISION_ALLOWED:
        raise ApiError(
            "NOT_AWAITING_REVIEW",
            f"run is not awaiting review (status={_status_str(run.status)})",
            status_code=409,
        )
    if not run.pending_merges or not any(
        p.get("agent_id") == payload.agent_id for p in run.pending_merges
    ):
        raise ApiError(
            "MERGE_NOT_PENDING",
            f"agent {payload.agent_id!r} is not in pending_merges",
            status_code=404,
        )
    ok, msg = await perform_manual_merge(
        run=run,
        agent_id=payload.agent_id,
        storage=storage,
        terminalize_when_resolved=False,
    )
    preserved_agents = _read_preserved_worktree_agent_ids(run)
    if ok:
        # Worktree deletion is DEFERRED to the end of the complaint phase
        # (unified cleanup), so a merged agent's worktree stays available for
        # any complaint-phase fixes. Dropping it from the preserve set means it
        # WILL be removed by the terminal tail cleanup.
        preserved_agents.discard(payload.agent_id)
    else:
        # Manual-merge FAILURE is the only case that keeps the worktree.
        preserved_agents.add(payload.agent_id)
    _write_preserved_worktree_agent_ids(
        run=run,
        storage=storage,
        agent_ids=preserved_agents,
    )
    if run.pending_merges is None:
        post_review_terminal = _consume_post_review_terminal_status(run)
        if post_review_terminal is not None:
            run.status = post_review_terminal
            if run.finished_at is None:
                run.finished_at = datetime.now(timezone.utc)
            storage.run_update(run)
            flow = storage.flow_get(run.flow_id)
            await _cleanup_terminal_tail(run=run, storage=storage, flow=flow)
        elif run.status == RunStatus.awaiting_user_review:
            target_status = (
                RunStatus.completed_with_conflicts if not ok else RunStatus.completed
            )
            _mark_awaiting_user_complaint(
                run=run,
                storage=storage,
                final_status_after_complaint=target_status,
            )
        else:
            # failed/aborted merge-decision path: stay terminal, then attempt
            # deferred cleanup now that pending merges are resolved.
            flow = storage.flow_get(run.flow_id)
            await _cleanup_terminal_tail(run=run, storage=storage, flow=flow)
    if ok:
        return MergeResponse(agent_id=payload.agent_id, success=True, message=msg[:1000])
    reason = (msg or "").strip()[:700]
    failure_kind = classify_merge_failure(msg)
    if failure_kind == "conflict":
        guidance = (
            "Merge failed due to git conflict. Please resolve conflicts manually in "
            "the agent worktree, then decide whether to merge or skip. "
            "ClawsomeFlow keeps this worktree intact and does not auto-clean it."
        )
    else:
        guidance = (
            "Merge failed due to environment/repository error (not a git content conflict). "
            "Please verify repository/workspace metadata and branch state, then retry. "
            "ClawsomeFlow keeps this worktree intact and does not auto-clean it."
        )
    message = f"{guidance}\n\n{reason}" if reason else guidance
    return MergeResponse(agent_id=payload.agent_id, success=False, message=message[:1000])


@router.post("/runs/{run_id}/dismiss-merge", response_model=RunSummary)
async def dismiss_pending_merge(
    run_id: Annotated[str, Path()],
    payload: Annotated[MergePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Drop a pending merge entry without merging.

    Worktree removal is DEFERRED: regardless of merge or dismiss, the worktree
    is kept until the complaint phase finishes, then removed by the unified
    terminal cleanup (manual-merge FAILURES are the only ones preserved).

    When this resolves the final pending merge, the run enters
    ``awaiting_user_complaint`` before terminal team cleanup.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status not in _MERGE_DECISION_ALLOWED:
        raise ApiError(
            "NOT_AWAITING_REVIEW",
            f"run is not awaiting review (status={_status_str(run.status)})",
            status_code=409,
        )
    if not run.pending_merges:
        raise ApiError("NOT_AWAITING_REVIEW", "no pending merges", status_code=409)
    new_pending = [
        p for p in run.pending_merges if p.get("agent_id") != payload.agent_id
    ]
    if len(new_pending) == len(run.pending_merges):
        raise ApiError(
            "MERGE_NOT_PENDING",
            f"agent {payload.agent_id!r} is not in pending_merges",
            status_code=404,
        )
    run.pending_merges = new_pending or None
    preserved_agents = _read_preserved_worktree_agent_ids(run)
    # Dismiss = discard work; worktree removal is DEFERRED to the unified
    # complaint-phase terminal cleanup (not preserved).
    preserved_agents.discard(payload.agent_id)
    _write_preserved_worktree_agent_ids(
        run=run,
        storage=storage,
        agent_ids=preserved_agents,
    )
    if run.pending_merges is None:
        post_review_terminal = _consume_post_review_terminal_status(run)
        if post_review_terminal is not None:
            run.status = post_review_terminal
            if run.finished_at is None:
                run.finished_at = datetime.now(timezone.utc)
            storage.run_update(run)
            flow = storage.flow_get(run.flow_id)
            await _cleanup_terminal_tail(run=run, storage=storage, flow=flow)
        elif run.status == RunStatus.awaiting_user_review:
            _mark_awaiting_user_complaint(
                run=run,
                storage=storage,
                final_status_after_complaint=RunStatus.completed,
            )
        else:
            storage.run_update(run)
            flow = storage.flow_get(run.flow_id)
            await _cleanup_terminal_tail(run=run, storage=storage, flow=flow)
    else:
        storage.run_update(run)
    return _to_summary(run)


@router.post("/runs/{run_id}/complaint", response_model=RunSummary)
async def submit_user_complaint(
    run_id: Annotated[str, Path()],
    payload: Annotated[ComplaintPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Start the post-merge complaint workflow in background."""
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status != RunStatus.awaiting_user_complaint:
        raise ApiError(
            "NOT_AWAITING_COMPLAINT",
            f"run is not awaiting complaint (status={_status_str(run.status)})",
            status_code=409,
        )
    text = (payload.message or "").strip()
    if not text:
        raise ApiError("INVALID_COMPLAINT", "complaint message is empty", status_code=400)
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {run.flow_id!r} not found", status_code=404)
    sched = get_scheduler()
    if sched.complaint_in_progress(run.id):
        raise ApiError(
            "COMPLAINT_ALREADY_RUNNING",
            "complaint workflow is already running for this run",
            status_code=409,
        )
    run.status = RunStatus.complaint_processing
    storage.run_update(run)
    try:
        sched.start_run_complaint_phase(
            run=run, flow=flow, complaint_text=text, storage=storage,
        )
    except Exception as exc:
        # Rollback status so the user can retry submit.
        run.status = RunStatus.awaiting_user_complaint
        storage.run_update(run)
        raise ApiError(
            "COMPLAINT_START_FAILED",
            f"failed to start complaint workflow: {exc}",
            status_code=409,
        ) from exc
    return _to_summary(run)


@router.post("/runs/{run_id}/complaint/skip", response_model=RunSummary)
async def skip_user_complaint(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Skip complaint submission and finish the run in background."""
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    sched = get_scheduler()
    if run.status == RunStatus.complaint_processing and sched.complaint_in_progress(run.id):
        raise ApiError(
            "COMPLAINT_ALREADY_RUNNING",
            "complaint workflow is already running for this run",
            status_code=409,
        )
    if run.status != RunStatus.awaiting_user_complaint:
        raise ApiError(
            "NOT_AWAITING_COMPLAINT",
            f"run is not awaiting complaint (status={_status_str(run.status)})",
            status_code=409,
        )
    if sched.complaint_in_progress(run.id):
        raise ApiError(
            "COMPLAINT_ALREADY_RUNNING",
            "complaint workflow is already running for this run",
            status_code=409,
        )
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {run.flow_id!r} not found", status_code=404)
    run.status = RunStatus.complaint_processing
    storage.run_update(run)
    try:
        sched.start_run_skip_complaint_phase(run=run, flow=flow, storage=storage)
    except RuntimeError as exc:
        if "already running" in str(exc).lower():
            raise ApiError(
                "COMPLAINT_ALREADY_RUNNING",
                "complaint workflow is already running for this run",
                status_code=409,
            ) from exc
        run.status = RunStatus.awaiting_user_complaint
        storage.run_update(run)
        raise ApiError(
            "COMPLAINT_START_FAILED",
            f"failed to start skip workflow: {exc}",
            status_code=409,
        ) from exc
    except Exception as exc:
        run.status = RunStatus.awaiting_user_complaint
        storage.run_update(run)
        raise ApiError(
            "COMPLAINT_START_FAILED",
            f"failed to start skip workflow: {exc}",
            status_code=409,
        ) from exc
    return _to_summary(run)


@router.post("/runs/{run_id}/retry-task/{task_id}", response_model=RunSummary)
async def retry_task(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Force a task back to ``pending`` so the controller redispatches it.

    Looks up the controller (must still be active in this process) to
    translate ``task_id`` (FlowTask.id) → ClawTeam task id via
    :class:`CompileResult`.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status in _TERMINAL:
        raise ApiError(
            "RUN_NOT_RUNNING",
            "retry-task requires the run to still be active",
            status_code=409,
        )
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None or controller.compile_result is None:
        raise ApiError(
            "RETRY_UNAVAILABLE",
            "controller / compile-result not available; cannot resolve ClawTeam task id",
            status_code=409,
        )
    ct_id = controller.compile_result.flow_to_clawteam.get(task_id)
    if ct_id is None:
        raise ApiError(
            "TASK_NOT_FOUND", f"task {task_id!r} not found in this run",
            status_code=404,
        )
    mcp = await get_mcp_client(user=run.user)
    await mcp.task_update(
        team_name=run.team_name, task_id=ct_id,
        status="pending", caller="csflow-scheduler", force=True,
    )
    return _to_summary(run)


class ExternalTaskCompletePayload(_CamelModel):
    status: str = "success"  # "success" | "failed"
    summary: str = ""


@router.post(
    "/runs/{run_id}/external-tasks/{task_id}/complete",
    response_model=RunSummary,
)
async def complete_external_task_webui(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    payload: Annotated[ExternalTaskCompletePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Submit an external-node task result from the WebUI (human channel).

    Same-origin trusted path: the run owner needs no ticket — the latest
    outstanding dispatch nonce is looked up server-side and the shared
    completion service (``services/external_tasks``) does the rest
    (mailbox_send + task_update, idempotent per dispatch attempt).
    """
    from app.services.external_tasks import (
        ExternalTaskError,
        complete_external_task,
        latest_dispatch_event,
    )

    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status in _TERMINAL:
        raise ApiError(
            "EXTERNAL_RUN_NOT_ACTIVE",
            "external task completion requires the run to still be active",
            status_code=409,
        )
    if payload.status not in ("success", "failed"):
        raise ApiError(
            "INVALID_PAYLOAD", "status must be 'success' or 'failed'",
            status_code=400,
        )
    dispatch_ev = latest_dispatch_event(storage, run_id=run.id, task_id=task_id)
    if dispatch_ev is None:
        raise ApiError(
            "EXTERNAL_TASK_NOT_DISPATCHED",
            f"task {task_id!r} has no outstanding external dispatch",
            status_code=409,
        )
    nonce = str((dispatch_ev.payload or {}).get("nonce") or "")
    try:
        await complete_external_task(
            storage=storage,
            run=run,
            task_id=task_id,
            nonce=nonce,
            ok=(payload.status == "success"),
            summary=payload.summary,
            source="webui",
        )
    except ExternalTaskError as exc:
        raise ApiError(exc.code, exc.message, status_code=exc.status_code) from exc
    return _to_summary(run)


@router.post(
    "/runs/{run_id}/external-tasks/{task_id}/redispatch",
    response_model=RunSummary,
)
async def redispatch_external_task_webui(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    """Re-dispatch a waiting webhook / remote_csflow external task.

    Mints a fresh one-time ticket and re-sends the channel outbound. The
    previous dispatch nonce is invalidated immediately (late callbacks with
    the old ticket → ``EXTERNAL_TICKET_STALE``). Human-channel tasks are
    rejected — they use the submit / report-failure form instead.
    """
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status in _TERMINAL:
        raise ApiError(
            "EXTERNAL_RUN_NOT_ACTIVE",
            "external task redispatch requires the run to still be active",
            status_code=409,
        )
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "REDISPATCH_UNAVAILABLE",
            "controller not available; cannot redispatch external task",
            status_code=409,
        )
    try:
        await controller.redispatch_waiting_external_task(task_id=task_id)
    except KeyError as exc:
        raise ApiError(
            "TASK_NOT_FOUND",
            f"task {task_id!r} not found in this run",
            status_code=404,
        ) from exc
    except ValueError as exc:
        raise ApiError("INVALID_PAYLOAD", str(exc), status_code=400) from exc
    except RuntimeError as exc:
        msg = str(exc)
        if "no outstanding external dispatch" in msg:
            raise ApiError(
                "EXTERNAL_TASK_NOT_DISPATCHED", msg, status_code=409,
            ) from exc
        raise ApiError("REDISPATCH_FAILED", msg, status_code=409) from exc
    except Exception as exc:  # pragma: no cover — channel/outbound failures
        logger.warning(
            "external_task_redispatch_failed",
            run_id=run.id, task_id=task_id, error=str(exc),
        )
        raise ApiError(
            "REDISPATCH_FAILED",
            f"failed to redispatch external task {task_id!r}: {exc}",
            status_code=409,
        ) from exc
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.post(
    "/runs/{run_id}/checkpoint/items/{task_id}/approve",
    response_model=RunSummary,
)
async def approve_checkpoint_item(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "CHECKPOINT_UNAVAILABLE",
            "controller is not active; checkpoint action cannot be applied",
            status_code=409,
        )
    if controller.checkpoint_snapshot() is None:
        raise ApiError(
            "NOT_AWAITING_CHECKPOINT",
            f"run has no active checkpoint (status={_status_str(run.status)})",
            status_code=409,
        )
    try:
        await controller.approve_checkpoint_item(upstream_task_id=task_id)
    except KeyError as exc:
        raise ApiError(
            "CHECKPOINT_ITEM_NOT_FOUND",
            f"checkpoint item {task_id!r} not found",
            status_code=404,
        ) from exc
    except ValueError as exc:
        raise ApiError("INVALID_PAYLOAD", str(exc), status_code=400) from exc
    except RuntimeError as exc:
        raise ApiError("NOT_AWAITING_CHECKPOINT", str(exc), status_code=409) from exc
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.post(
    "/runs/{run_id}/checkpoint/items/{task_id}/rerun",
    response_model=RunSummary,
)
async def rerun_checkpoint_item(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    payload: Annotated[CheckpointRerunPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    # Feedback is required for local agents but OPTIONAL for external-node
    # items (one-click re-dispatch has no feedback slot); the controller
    # enforces the per-owner-kind rule and raises ValueError when a local
    # agent rerun arrives without feedback.
    text = (payload.feedback or "").strip()
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "CHECKPOINT_UNAVAILABLE",
            "controller is not active; checkpoint action cannot be applied",
            status_code=409,
        )
    if controller.checkpoint_snapshot() is None:
        raise ApiError(
            "NOT_AWAITING_CHECKPOINT",
            f"run has no active checkpoint (status={_status_str(run.status)})",
            status_code=409,
        )
    try:
        await controller.request_checkpoint_rerun(
            upstream_task_id=task_id,
            feedback=text,
        )
    except KeyError as exc:
        raise ApiError(
            "CHECKPOINT_ITEM_NOT_FOUND",
            f"checkpoint item {task_id!r} not found",
            status_code=404,
        ) from exc
    except ValueError as exc:
        raise ApiError("INVALID_PAYLOAD", str(exc), status_code=400) from exc
    except RuntimeError as exc:
        msg = str(exc)
        if "active checkpoint rerun" in msg:
            raise ApiError("CHECKPOINT_RERUN_CONFLICT", msg, status_code=409) from exc
        raise ApiError("NOT_AWAITING_CHECKPOINT", str(exc), status_code=409) from exc
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)


@router.post(
    "/runs/{run_id}/checkpoint/items/{task_id}/mark-read",
    response_model=RunSummary,
)
async def mark_checkpoint_item_read(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "CHECKPOINT_UNAVAILABLE",
            "controller is not active; checkpoint action cannot be applied",
            status_code=409,
        )
    if controller.checkpoint_snapshot() is None:
        raise ApiError(
            "NOT_AWAITING_CHECKPOINT",
            f"run has no active checkpoint (status={_status_str(run.status)})",
            status_code=409,
        )
    try:
        await controller.mark_checkpoint_item_read(upstream_task_id=task_id)
    except KeyError as exc:
        raise ApiError(
            "CHECKPOINT_ITEM_NOT_FOUND",
            f"checkpoint item {task_id!r} not found",
            status_code=404,
        ) from exc
    except ValueError as exc:
        raise ApiError("INVALID_PAYLOAD", str(exc), status_code=400) from exc
    except RuntimeError as exc:
        raise ApiError("NOT_AWAITING_CHECKPOINT", str(exc), status_code=409) from exc
    refreshed = storage.run_get(run.id) or run
    return _to_summary(refreshed)
