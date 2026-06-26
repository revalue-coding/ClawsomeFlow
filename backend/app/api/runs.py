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
from app.deployment import get_deployment_capabilities
from app.integrations.clawteam_mcp import get_mcp_client
from app.logging_setup import get_logger
from app.models import (
    DEFAULT_TARGET_BRANCH,
    TERMINAL_RUN_STATUSES,
    Flow,
    FlowAgent,
    FlowRun,
    FlowRunSchedule,
    FlowRunScheduleExecution,
    FlowSpec,
    RunEvent,
    RunStatus,
    iso_utc,
)
from app.scheduler.controller import RunController
from app.scheduler.engine import abort_run_to_terminal, get_scheduler
from app.scheduler.finalize import (
    classify_merge_failure,
    perform_manual_merge,
    run_terminal_tail_cleanup,
)
from app.scheduler.naming import team_name_for_run
from app.scheduler.sessions.tmux_ready import tmux_capture_pane
from app.services import run_schedules as run_schedule_svc
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
_MERGE_DECISION_ALLOWED = {
    RunStatus.awaiting_user_review,
    RunStatus.failed,
    RunStatus.aborted,
}

_POST_COMPLAINT_STATUS_KEY = "_csflow_post_complaint_final_status"
_POST_REVIEW_TERMINAL_STATUS_KEY = "_csflow_post_review_terminal_status"
_PRESERVE_WORKTREE_AGENT_IDS_KEY = "_csflow_preserve_worktree_agent_ids"


# ──────────────────────────────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────────────────────────────


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


class PendingMergeView(_CamelModel):
    agent_id: str
    branch: str
    target_branch: str = DEFAULT_TARGET_BRANCH
    diff_summary: dict[str, Any] = Field(default_factory=dict)
    leader_suggestion: str = ""


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


class RunCreateResponse(_CamelModel):
    id: str
    status: str
    team_name: str


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
        if not k or k.startswith("_csflow_"):
            continue
        out[k] = value
    return out


def _to_summary(r: FlowRun) -> RunSummary:
    return RunSummary(
        id=r.id, flow_id=r.flow_id, flow_version=r.flow_version,
        team_name=r.team_name,
        status=_status_str(r.status),
        user=r.user,
        started_at=iso_utc(r.started_at),
        finished_at=iso_utc(r.finished_at) if r.finished_at else None,
        inputs=_public_run_inputs(r.inputs),
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
    return RunDetail(
        id=r.id, flow_id=r.flow_id, flow_version=r.flow_version,
        team_name=r.team_name,
        status=_status_str(r.status),
        user=r.user,
        started_at=iso_utc(r.started_at),
        finished_at=iso_utc(r.finished_at) if r.finished_at else None,
        inputs=_public_run_inputs(r.inputs),
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
    run = FlowRun(
        id=run_id,
        flow_id=flow.id, flow_version=flow.version,
        team_name=team_name_for_run(run_id),
        status=RunStatus.pending,
        inputs=payload.inputs or {},
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
    caps = get_deployment_capabilities(cfg)
    if all_users and not caps.allow_all_users_query:
        raise ApiError(
            "FORBIDDEN",
            "allUsers=true is disabled in server mode until RBAC is enabled",
            status_code=403,
        )
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
# Actions
# ──────────────────────────────────────────────────────────────────────


@router.post("/runs/{run_id}/abort", response_model=RunSummary)
async def abort_run(
    run_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> RunSummary:
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    _ensure_owner(run, user)
    if run.status in _TERMINAL:
        raise ApiError(
            "RUN_NOT_RUNNING",
            f"run is already terminal ({_status_str(run.status)})",
            status_code=409,
        )
    sched = get_scheduler()
    # Shared primitive: instant DB flip to aborted + cooperative cancel.
    cancelled = abort_run_to_terminal(run, sched=sched, storage=storage)
    if not cancelled:
        # No live scheduler task to finalize cleanup; do best-effort here.
        flow = storage.flow_get(run.flow_id)
        await _cleanup_terminal_tail(run=run, storage=storage, flow=flow)
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
    if run.status != RunStatus.awaiting_user_checkpoint:
        raise ApiError(
            "NOT_AWAITING_CHECKPOINT",
            f"run is not awaiting checkpoint (status={_status_str(run.status)})",
            status_code=409,
        )
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "CHECKPOINT_UNAVAILABLE",
            "controller is not active; checkpoint action cannot be applied",
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
    if run.status != RunStatus.awaiting_user_checkpoint:
        raise ApiError(
            "NOT_AWAITING_CHECKPOINT",
            f"run is not awaiting checkpoint (status={_status_str(run.status)})",
            status_code=409,
        )
    text = (payload.feedback or "").strip()
    if not text:
        raise ApiError(
            "INVALID_CHECKPOINT_FEEDBACK",
            "feedback cannot be empty",
            status_code=400,
        )
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "CHECKPOINT_UNAVAILABLE",
            "controller is not active; checkpoint action cannot be applied",
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
    if run.status != RunStatus.awaiting_user_checkpoint:
        raise ApiError(
            "NOT_AWAITING_CHECKPOINT",
            f"run is not awaiting checkpoint (status={_status_str(run.status)})",
            status_code=409,
        )
    sched = get_scheduler()
    controller = sched.get_controller(run.id)
    if controller is None:
        raise ApiError(
            "CHECKPOINT_UNAVAILABLE",
            "controller is not active; checkpoint action cannot be applied",
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
