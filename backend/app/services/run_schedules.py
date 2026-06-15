"""Timed Flow-run schedules (local in-process worker).

This module owns two things:
1) CRUD validation helpers for ``FlowRunSchedule`` rows.
2) A lightweight in-process async worker that scans due schedules and
   triggers Flow runs on time.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.logging_setup import get_logger
from app.models import (
    Flow,
    FlowRun,
    FlowRunSchedule,
    FlowRunScheduleExecution,
    FlowSpec,
    RunStatus,
    _new_id,
)
from app.scheduler.engine import get_scheduler
from app.scheduler.naming import team_name_for_run
from app.storage import StorageBackend, get_storage

logger = get_logger("svc.run_schedules")

_POLL_INTERVAL_SEC = 2.0
_MAX_DUE_PER_TICK = 20
_RUN_MODE_PARALLEL = "parallel"
_RUN_MODE_SERIAL = "serial"
_EXECUTE_MODE_ONCE = "once"
_EXECUTE_MODE_RECURRING = "recurring"
_VALID_RUN_MODES = {_RUN_MODE_PARALLEL, _RUN_MODE_SERIAL}
_VALID_EXECUTE_MODES = {_EXECUTE_MODE_ONCE, _EXECUTE_MODE_RECURRING}
_RUNTIME_PARAM_FIELDS_KEY = "csflow.runtime.param_fields"
_TERMINAL_RUN_STATUSES = {
    RunStatus.completed,
    RunStatus.completed_with_conflicts,
    RunStatus.complaint_failed,
    RunStatus.failed,
    RunStatus.aborted,
}
_RUN_SUCCESS_STATUSES = {RunStatus.completed, RunStatus.completed_with_conflicts}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_schedule_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in items:
        flow_id = str((raw or {}).get("flow_id") or "").strip()
        if not flow_id:
            continue
        raw_inputs = (raw or {}).get("inputs")
        inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        out.append({"flow_id": flow_id, "inputs": dict(inputs)})
    if not out:
        raise ValueError("at least one valid flow item is required")
    return out


def _normalize_schedule_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("schedule name is required")
    return cleaned


def _required_fields_from_flow(flow: Flow) -> list[str]:
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        return []
    raw = (spec.variables or {}).get(_RUNTIME_PARAM_FIELDS_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in parsed:
        field = str(entry or "").strip()
        if not field or field in seen:
            continue
        seen.add(field)
        out.append(field)
    return out


def _normalize_item_inputs(
    *,
    flow_id: str,
    raw_inputs: Any,
    required_fields: list[str],
) -> dict[str, str]:
    source = raw_inputs if isinstance(raw_inputs, dict) else {}
    if required_fields:
        out: dict[str, str] = {}
        for field in required_fields:
            value = str(source.get(field) or "").strip()
            if not value:
                raise ValueError(f"flow {flow_id!r} missing required input: {field}")
            out[field] = value
        return out

    out: dict[str, str] = {}
    for raw_key, raw_value in source.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        value = str(raw_value or "").strip()
        if not value:
            continue
        out[key] = value
    return out


def _normalize_items_for_user(
    *,
    user: str,
    items: list[dict[str, Any]],
    storage: StorageBackend,
) -> list[dict[str, Any]]:
    base = _normalize_schedule_items(items)
    out: list[dict[str, Any]] = []
    for item in base:
        flow_id = str(item.get("flow_id") or "").strip()
        if not flow_id:
            continue
        flow = storage.flow_get(flow_id)
        if flow is None:
            raise ValueError(f"flow {flow_id!r} not found")
        if flow.owner_user != user:
            raise ValueError(f"flow {flow_id!r} belongs to a different user")
        out.append(
            {
                "flow_id": flow_id,
                "inputs": _normalize_item_inputs(
                    flow_id=flow_id,
                    raw_inputs=item.get("inputs"),
                    required_fields=_required_fields_from_flow(flow),
                ),
            }
        )
    if not out:
        raise ValueError("at least one valid flow item is required")
    return out


def _validate_schedule_modes(
    *,
    run_mode: str,
    execute_mode: str,
    interval_days: int | None,
) -> int | None:
    if run_mode not in _VALID_RUN_MODES:
        raise ValueError("run_mode must be one of: parallel, serial")
    if execute_mode not in _VALID_EXECUTE_MODES:
        raise ValueError("execute_mode must be one of: once, recurring")
    if execute_mode == _EXECUTE_MODE_RECURRING:
        if interval_days is None or interval_days < 1:
            raise ValueError("interval_days must be >= 1 for recurring schedules")
        return int(interval_days)
    return None


def _runtime_prompt_from_inputs(inputs: dict[str, Any] | None) -> str | None:
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
    *,
    spec: FlowSpec,
    runtime_prompt: str | None,
) -> FlowSpec:
    if not runtime_prompt:
        return spec
    copied = spec.model_copy(deep=True)
    for task in copied.tasks:
        task.description = _prepend_runtime_prompt(task.description or "", runtime_prompt)
    return copied


def _start_run_for_flow(
    *,
    flow: Flow,
    user: str,
    inputs: dict[str, Any],
    storage: StorageBackend,
) -> FlowRun:
    run_id = _new_id("run")
    run = FlowRun(
        id=run_id,
        flow_id=flow.id,
        flow_version=flow.version,
        team_name=team_name_for_run(run_id),
        status=RunStatus.pending,
        inputs=inputs or {},
        user=user,
        is_scheduled=True,
    )
    saved = storage.run_create(run)

    runtime_prompt = _runtime_prompt_from_inputs(inputs)
    spec = _inject_runtime_prompt_into_spec(
        spec=FlowSpec.model_validate(flow.spec),
        runtime_prompt=runtime_prompt,
    )
    flow_description = (
        _prepend_runtime_prompt(flow.description, runtime_prompt)
        if runtime_prompt
        else flow.description
    )
    get_scheduler().start_run(
        run=saved,
        spec=spec,
        flow=flow,
        flow_description=flow_description,
        storage=storage,
    )
    return saved


async def _wait_run_terminal(
    *,
    run_id: str,
    storage: StorageBackend,
    stop_evt: asyncio.Event,
) -> RunStatus | None:
    while True:
        row = storage.run_get(run_id)
        if row is None:
            return None
        status = row.status
        status_value = status.value if isinstance(status, RunStatus) else str(status)
        if status in _TERMINAL_RUN_STATUSES or status_value in {
            s.value for s in _TERMINAL_RUN_STATUSES
        }:
            if isinstance(status, RunStatus):
                return status
            try:
                return RunStatus(status_value)
            except ValueError:
                return None
        if stop_evt.is_set():
            return None
        await asyncio.sleep(_POLL_INTERVAL_SEC)


@dataclass(frozen=True)
class _PlannedScheduleItem:
    index: int
    flow_id: str
    flow_name: str
    inputs: dict[str, Any]
    raw_item: dict[str, Any]


def _item_result(
    *,
    index: int,
    flow_id: str,
    flow_name: str,
    status: str,
    reason: str = "",
    reason_code: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "index": index,
        "flow_id": flow_id,
        "flow_name": flow_name,
        "status": status,
        "reason": reason,
        "reason_code": reason_code,
        "run_id": run_id or "",
    }


def _precheck_schedule_items(
    *,
    schedule: FlowRunSchedule,
    storage: StorageBackend,
) -> tuple[list[_PlannedScheduleItem], list[dict[str, Any]], bool]:
    planned: list[_PlannedScheduleItem] = []
    failures: list[dict[str, Any]] = []
    for index, raw in enumerate(schedule.items or []):
        if not isinstance(raw, dict):
            failures.append(
                _item_result(
                    index=index,
                    flow_id="",
                    flow_name="",
                    status="failed",
                    reason="invalid schedule item payload",
                    reason_code="invalid_item_payload",
                )
            )
            continue
        flow_id = str(raw.get("flow_id") or "").strip()
        if not flow_id:
            failures.append(
                _item_result(
                    index=index,
                    flow_id="",
                    flow_name="",
                    status="failed",
                    reason="flow_id is empty",
                    reason_code="flow_id_empty",
                )
            )
            continue
        flow = storage.flow_get(flow_id)
        if flow is None:
            failures.append(
                _item_result(
                    index=index,
                    flow_id=flow_id,
                    flow_name=flow_id,
                    status="failed",
                    reason=f"flow {flow_id!r} not found",
                    reason_code="flow_not_found",
                )
            )
            continue
        if flow.owner_user != schedule.user:
            failures.append(
                _item_result(
                    index=index,
                    flow_id=flow_id,
                    flow_name=flow.name,
                    status="failed",
                    reason=f"flow {flow_id!r} belongs to a different user",
                    reason_code="flow_forbidden",
                )
            )
            continue
        raw_inputs = raw.get("inputs")
        inputs = raw_inputs if isinstance(raw_inputs, dict) else {}
        planned.append(
            _PlannedScheduleItem(
                index=index,
                flow_id=flow_id,
                flow_name=flow.name,
                inputs=inputs,
                raw_item=raw,
            )
        )

    if failures:
        results = failures[:]
        for item in planned:
            results.append(
                _item_result(
                    index=item.index,
                    flow_id=item.flow_id,
                    flow_name=item.flow_name,
                    status="skipped",
                    reason="execution stopped by precheck failure",
                    reason_code="precheck_blocked",
                )
            )
        results.sort(key=lambda x: int(x.get("index", 0)))
        return [], results, True
    return planned, [], False


async def _trigger_configured_run(
    *,
    schedule: FlowRunSchedule,
    item: _PlannedScheduleItem,
    storage: StorageBackend,
) -> tuple[str | None, str, str]:
    flow_id = item.flow_id
    flow = storage.flow_get(flow_id)
    if flow is None:
        logger.warning(
            "run_schedule_flow_missing",
            schedule_id=schedule.id,
            flow_id=flow_id,
        )
        return None, "flow_not_found", f"flow {flow_id!r} not found"
    if flow.owner_user != schedule.user:
        logger.warning(
            "run_schedule_flow_forbidden",
            schedule_id=schedule.id,
            flow_id=flow_id,
            schedule_user=schedule.user,
            flow_owner=flow.owner_user,
        )
        return None, "flow_forbidden", f"flow {flow_id!r} belongs to a different user"
    try:
        run = _start_run_for_flow(
            flow=flow,
            user=schedule.user,
            inputs=item.inputs,
            storage=storage,
        )
    except Exception as exc:
        logger.exception(
            "run_schedule_trigger_failed",
            schedule_id=schedule.id,
            flow_id=flow_id,
            error=str(exc),
        )
        return None, "trigger_failed", f"failed to start run: {exc}"
    return run.id, "", ""


def create_schedule(
    *,
    user: str,
    run_at: datetime,
    items: list[dict[str, Any]],
    run_mode: str,
    execute_mode: str,
    interval_days: int | None = None,
    name: str,
    storage: StorageBackend | None = None,
) -> FlowRunSchedule:
    storage = storage or get_storage()
    normalized_name = _normalize_schedule_name(name)
    normalized_items = _normalize_items_for_user(
        user=user,
        items=items,
        storage=storage,
    )
    valid_interval_days = _validate_schedule_modes(
        run_mode=run_mode,
        execute_mode=execute_mode,
        interval_days=interval_days,
    )
    schedule = FlowRunSchedule(
        user=user,
        name=normalized_name,
        run_mode=run_mode,
        execute_mode=execute_mode,
        interval_days=valid_interval_days,
        next_run_at=_ensure_utc(run_at),
        items=normalized_items,
    )
    return storage.run_schedule_create(schedule)


def list_schedules(
    *,
    user: str,
    storage: StorageBackend | None = None,
) -> list[FlowRunSchedule]:
    storage = storage or get_storage()
    return storage.run_schedule_list(user=user)


def get_schedule(
    schedule_id: str,
    *,
    user: str,
    storage: StorageBackend | None = None,
) -> FlowRunSchedule:
    storage = storage or get_storage()
    row = storage.run_schedule_get(schedule_id)
    if row is None:
        raise KeyError(schedule_id)
    if row.user != user:
        raise PermissionError(schedule_id)
    return row


def update_schedule(
    schedule_id: str,
    *,
    user: str,
    run_at: datetime,
    items: list[dict[str, Any]],
    run_mode: str,
    execute_mode: str,
    interval_days: int | None = None,
    name: str,
    storage: StorageBackend | None = None,
) -> FlowRunSchedule:
    storage = storage or get_storage()
    row = storage.run_schedule_get(schedule_id)
    if row is None:
        raise KeyError(schedule_id)
    if row.user != user:
        raise PermissionError(schedule_id)

    row.name = _normalize_schedule_name(name)
    row.run_mode = run_mode
    row.execute_mode = execute_mode
    row.interval_days = _validate_schedule_modes(
        run_mode=run_mode,
        execute_mode=execute_mode,
        interval_days=interval_days,
    )
    row.next_run_at = _ensure_utc(run_at)
    row.items = _normalize_items_for_user(
        user=user,
        items=items,
        storage=storage,
    )
    row.updated_at = _now_utc()
    return storage.run_schedule_update(row)


def delete_schedule(
    schedule_id: str,
    *,
    user: str,
    storage: StorageBackend | None = None,
) -> bool:
    storage = storage or get_storage()
    row = storage.run_schedule_get(schedule_id)
    if row is None:
        raise KeyError(schedule_id)
    if row.user != user:
        raise PermissionError(schedule_id)
    return storage.run_schedule_delete(schedule_id)


def list_schedule_executions(
    *,
    user: str,
    schedule_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    storage: StorageBackend | None = None,
) -> tuple[list[FlowRunScheduleExecution], int]:
    storage = storage or get_storage()
    return storage.run_schedule_execution_list(
        user=user,
        schedule_id=schedule_id,
        limit=limit,
        offset=offset,
    )


def clear_schedule_executions(
    *,
    user: str,
    storage: StorageBackend | None = None,
) -> int:
    """Delete the caller's finished schedule-execution records. Returns count."""
    storage = storage or get_storage()
    return storage.run_schedule_execution_clear(user=user)


def get_schedule_execution(
    execution_id: str,
    *,
    user: str,
    storage: StorageBackend | None = None,
) -> FlowRunScheduleExecution:
    storage = storage or get_storage()
    row = storage.run_schedule_execution_get(execution_id)
    if row is None:
        raise KeyError(execution_id)
    if row.user != user:
        raise PermissionError(execution_id)
    return row


def _execution_status_from_results(results: list[dict[str, Any]]) -> str:
    succeeded = 0
    failed = 0
    skipped = 0
    for item in results:
        status = str(item.get("status") or "")
        if status == "succeeded":
            succeeded += 1
        elif status == "failed":
            failed += 1
        elif status == "skipped":
            skipped += 1
    if failed == 0 and skipped == 0:
        return "succeeded"
    if succeeded > 0:
        return "partial_failed"
    return "failed"


class RunScheduleWorker:
    """Background poller that triggers due ``FlowRunSchedule`` rows."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._running_schedule_ids: set[str] = set()
        self._execution_tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="csflow-run-schedule-worker",
        )

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for task in list(self._execution_tasks):
            task.cancel()
        if self._execution_tasks:
            await asyncio.gather(*self._execution_tasks, return_exceptions=True)
        self._execution_tasks.clear()
        self._running_schedule_ids.clear()

    async def _run_loop(self) -> None:
        logger.info("run_schedule_worker_started")
        while not self._stop_evt.is_set():
            try:
                await self._tick()
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("run_schedule_worker_tick_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=_POLL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
        logger.info("run_schedule_worker_stopped")

    async def _tick(self) -> None:
        storage = get_storage()
        due = storage.run_schedule_list_due(
            before=_now_utc(),
            limit=_MAX_DUE_PER_TICK,
        )
        for row in due:
            if row.id in self._running_schedule_ids:
                continue
            self._running_schedule_ids.add(row.id)
            task = asyncio.create_task(
                self._execute_schedule(row.id),
                name=f"csflow-run-schedule:{row.id}",
            )
            self._execution_tasks.add(task)
            task.add_done_callback(self._execution_tasks.discard)

    async def _execute_schedule(self, schedule_id: str) -> None:
        execution: FlowRunScheduleExecution | None = None
        item_results: list[dict[str, Any]] = []
        run_ids: list[str] = []
        schedule: FlowRunSchedule | None = None
        try:
            storage = get_storage()
            schedule = storage.run_schedule_get(schedule_id)
            if schedule is None:
                return
            now = _now_utc()
            if _ensure_utc(schedule.next_run_at) > now:
                return
            execution = storage.run_schedule_execution_create(
                FlowRunScheduleExecution(
                    schedule_id=schedule.id,
                    schedule_name=schedule.name,
                    user=schedule.user,
                    run_mode=schedule.run_mode,
                    execute_mode=schedule.execute_mode,
                    status="running",
                    total_items=len(schedule.items or []),
                )
            )
            # Claim early so one-shot schedules never retrigger after process
            # restarts; recurring schedules update next_run_at immediately.
            if schedule.execute_mode == _EXECUTE_MODE_ONCE:
                storage.run_schedule_delete(schedule.id)
                logger.info(
                    "run_schedule_once_claimed",
                    schedule_id=schedule.id,
                    execution_id=execution.id if execution is not None else None,
                )
            elif schedule.execute_mode == _EXECUTE_MODE_RECURRING:
                every_days = schedule.interval_days or 1
                schedule.next_run_at = now + timedelta(days=every_days)
                storage.run_schedule_update(schedule)

            planned, precheck_results, blocked = _precheck_schedule_items(
                schedule=schedule,
                storage=storage,
            )
            if blocked:
                item_results = precheck_results
            elif schedule.run_mode == _RUN_MODE_SERIAL:
                for idx, item in enumerate(planned):
                    run_id, reason_code, reason = await _trigger_configured_run(
                        schedule=schedule,
                        item=item,
                        storage=storage,
                    )
                    if not run_id:
                        item_results.append(
                            _item_result(
                                index=item.index,
                                flow_id=item.flow_id,
                                flow_name=item.flow_name,
                                status="failed",
                                reason=reason or "failed to start run",
                                reason_code=reason_code or "trigger_failed",
                            )
                        )
                        for rest in planned[idx + 1:]:
                            item_results.append(
                                _item_result(
                                    index=rest.index,
                                    flow_id=rest.flow_id,
                                    flow_name=rest.flow_name,
                                    status="skipped",
                                    reason="stopped after previous serial failure",
                                    reason_code="stopped_after_serial_failure",
                                )
                            )
                        break
                    run_ids.append(run_id)
                    terminal = await _wait_run_terminal(
                        run_id=run_id,
                        storage=storage,
                        stop_evt=self._stop_evt,
                    )
                    if terminal in _RUN_SUCCESS_STATUSES:
                        item_results.append(
                            _item_result(
                                index=item.index,
                                flow_id=item.flow_id,
                                flow_name=item.flow_name,
                                status="succeeded",
                                reason_code=str(terminal.value),
                                run_id=run_id,
                            )
                        )
                        continue
                    fail_code = (
                        str(terminal.value)
                        if isinstance(terminal, RunStatus)
                        else "run_missing_or_worker_stopped"
                    )
                    fail_reason = (
                        f"run finished with status {terminal.value}"
                        if isinstance(terminal, RunStatus)
                        else "run disappeared before reaching terminal status"
                    )
                    item_results.append(
                        _item_result(
                            index=item.index,
                            flow_id=item.flow_id,
                            flow_name=item.flow_name,
                            status="failed",
                            reason=fail_reason,
                            reason_code=fail_code,
                            run_id=run_id,
                        )
                    )
                    for rest in planned[idx + 1:]:
                        item_results.append(
                            _item_result(
                                index=rest.index,
                                flow_id=rest.flow_id,
                                flow_name=rest.flow_name,
                                status="skipped",
                                reason="stopped after previous serial failure",
                                reason_code="stopped_after_serial_failure",
                            )
                        )
                    break
            else:
                to_wait: list[tuple[_PlannedScheduleItem, str]] = []
                for item in planned:
                    run_id, reason_code, reason = await _trigger_configured_run(
                        schedule=schedule,
                        item=item,
                        storage=storage,
                    )
                    if not run_id:
                        item_results.append(
                            _item_result(
                                index=item.index,
                                flow_id=item.flow_id,
                                flow_name=item.flow_name,
                                status="failed",
                                reason=reason or "failed to start run",
                                reason_code=reason_code or "trigger_failed",
                            )
                        )
                        continue
                    run_ids.append(run_id)
                    to_wait.append((item, run_id))
                if to_wait:
                    waited = await asyncio.gather(
                        *[
                            _wait_run_terminal(
                                run_id=run_id,
                                storage=storage,
                                stop_evt=self._stop_evt,
                            )
                            for _, run_id in to_wait
                        ]
                    )
                    for (item, run_id), terminal in zip(to_wait, waited, strict=False):
                        if terminal in _RUN_SUCCESS_STATUSES:
                            item_results.append(
                                _item_result(
                                    index=item.index,
                                    flow_id=item.flow_id,
                                    flow_name=item.flow_name,
                                    status="succeeded",
                                    reason_code=str(terminal.value),
                                    run_id=run_id,
                                )
                            )
                            continue
                        fail_code = (
                            str(terminal.value)
                            if isinstance(terminal, RunStatus)
                            else "run_missing_or_worker_stopped"
                        )
                        fail_reason = (
                            f"run finished with status {terminal.value}"
                            if isinstance(terminal, RunStatus)
                            else "run disappeared before reaching terminal status"
                        )
                        item_results.append(
                            _item_result(
                                index=item.index,
                                flow_id=item.flow_id,
                                flow_name=item.flow_name,
                                status="failed",
                                reason=fail_reason,
                                reason_code=fail_code,
                                run_id=run_id,
                            )
                        )

            item_results.sort(key=lambda x: int(x.get("index", 0)))
            succeeded_items = sum(1 for item in item_results if item.get("status") == "succeeded")
            failed_items = sum(1 for item in item_results if item.get("status") == "failed")
            skipped_items = sum(1 for item in item_results if item.get("status") == "skipped")
            if execution is not None:
                execution.status = _execution_status_from_results(item_results)
                execution.run_ids = run_ids
                execution.item_results = item_results
                execution.succeeded_items = succeeded_items
                execution.failed_items = failed_items
                execution.skipped_items = skipped_items
                execution.total_items = len(item_results)
                execution.finished_at = _now_utc()
                storage.run_schedule_execution_update(execution)
        except asyncio.CancelledError:
            logger.warning("run_schedule_execution_cancelled", schedule_id=schedule_id)
            if execution is not None:
                item_results.append(
                    _item_result(
                        index=max([int(i.get("index", -1)) for i in item_results], default=-1) + 1,
                        flow_id="",
                        flow_name="",
                        status="failed",
                        reason="schedule worker stopped before execution finished",
                        reason_code="worker_stopped",
                    )
                )
                execution.status = "failed"
                execution.run_ids = run_ids
                execution.item_results = item_results
                execution.succeeded_items = sum(
                    1 for item in item_results if item.get("status") == "succeeded"
                )
                execution.failed_items = sum(
                    1 for item in item_results if item.get("status") == "failed"
                )
                execution.skipped_items = sum(
                    1 for item in item_results if item.get("status") == "skipped"
                )
                execution.total_items = len(item_results)
                execution.finished_at = _now_utc()
                try:
                    storage.run_schedule_execution_update(execution)
                except Exception:
                    logger.exception(
                        "run_schedule_execution_update_failed_on_cancel",
                        schedule_id=schedule_id,
                    )
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "run_schedule_execute_failed",
                schedule_id=schedule_id,
                error=str(exc),
            )
            if execution is not None:
                item_results.append(
                    _item_result(
                        index=max([int(i.get("index", -1)) for i in item_results], default=-1) + 1,
                        flow_id="",
                        flow_name="",
                        status="failed",
                        reason=f"internal error: {exc}",
                        reason_code="internal_error",
                    )
                )
                execution.status = "failed"
                execution.run_ids = run_ids
                execution.item_results = item_results
                execution.succeeded_items = sum(
                    1 for item in item_results if item.get("status") == "succeeded"
                )
                execution.failed_items = sum(
                    1 for item in item_results if item.get("status") == "failed"
                )
                execution.skipped_items = sum(
                    1 for item in item_results if item.get("status") == "skipped"
                )
                execution.total_items = len(item_results)
                execution.finished_at = _now_utc()
                try:
                    storage.run_schedule_execution_update(execution)
                except Exception:
                    logger.exception(
                        "run_schedule_execution_update_failed",
                        schedule_id=schedule_id,
                    )
        finally:
            self._running_schedule_ids.discard(schedule_id)


_worker_singleton: RunScheduleWorker | None = None


def get_run_schedule_worker() -> RunScheduleWorker:
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = RunScheduleWorker()
    return _worker_singleton


def reset_run_schedule_worker() -> None:
    global _worker_singleton
    _worker_singleton = None


__all__ = [
    "RunScheduleWorker",
    "create_schedule",
    "delete_schedule",
    "get_schedule_execution",
    "get_run_schedule_worker",
    "get_schedule",
    "list_schedule_executions",
    "list_schedules",
    "reset_run_schedule_worker",
    "update_schedule",
]

