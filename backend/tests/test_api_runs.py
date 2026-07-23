"""Tests for /api/runs and /api/flows/{id}/runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.main import create_app
from app.models import (
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    OnFailure,
    RunEvent,
    RunStatus,
)
from app.scheduler import engine as engine_mod
from app.storage import get_storage


@pytest.fixture
def app_client(tmp_path: Path):
    cfg = load_config()
    cfg = cfg.model_copy(update={"default_user": "alice"})
    save_config(cfg)
    with TestClient(create_app()) as c:
        yield c


def _make_flow(
    *,
    owner: str = "alice",
    openclaw: bool = False,
    cleanup_team: bool = False,
) -> Flow:
    storage = get_storage()
    spec = FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=True, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y",
                     description="", depends_on=["t1"], is_leader_summary=True),
        ],
    )
    flow = Flow(
        name="test",
        description="",
        owner_user=owner,
        cleanup_team_on_finish=cleanup_team,
    ).with_spec(spec)
    return storage.flow_create(flow)


def _make_run(*, flow_id: str, user: str = "alice", status: RunStatus = RunStatus.running,
              pending=None, team_name: str = "csflow-test", inputs=None) -> FlowRun:
    storage = get_storage()
    run = storage.run_create(FlowRun(
        flow_id=flow_id, flow_version=1, team_name=team_name,
        status=status, inputs=inputs or {}, user=user,
        pending_merges=pending,
    ))
    return run


# ── Trigger ----------------------------------------------------------


def test_trigger_unknown_flow_404(app_client: TestClient) -> None:
    r = app_client.post("/api/flows/missing-flow/runs", json={"inputs": {}})
    assert r.status_code == 404


def test_trigger_other_user_403(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="bob")
    r = app_client.post(f"/api/flows/{flow.id}/runs", json={})
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


def test_trigger_creates_run_and_calls_scheduler(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="alice")

    captured = {}

    def fake_start_run(self, *, run, spec, flow=None, **kw):
        captured["run_id"] = run.id
        captured["team_name"] = run.team_name
        captured["spec_agent_count"] = len(spec.agents)
        captured["compile"] = kw.get("compile", True)
        from app.scheduler.controller import RunController
        # Return a stub controller so the route's response is well-formed; we
        # don't actually want the run_loop to execute here.
        return RunController(run=run, spec=spec)

    monkeypatch.setattr(engine_mod.FlowScheduler, "start_run", fake_start_run)

    r = app_client.post(f"/api/flows/{flow.id}/runs", json={"inputs": {"goal": "x"}})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["teamName"].startswith("csflow-")
    # Scheduler was invoked with our run + spec.
    assert captured["run_id"] == body["id"]
    assert captured["spec_agent_count"] == 2

    # Run row exists in DB with the stored inputs.
    run_row = get_storage().run_get(body["id"])
    assert run_row is not None
    assert run_row.inputs == {"goal": "x"}


def _stub_start_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_start_run(self, *, run, spec, flow=None, **kw):
        from app.scheduler.controller import RunController
        return RunController(run=run, spec=spec)
    monkeypatch.setattr(engine_mod.FlowScheduler, "start_run", fake_start_run)


def test_trigger_easy_mode_does_not_mark_run_scheduled(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """省心模式: a MANUAL run is never ``is_scheduled`` (that flag means a timed
    trigger only). Easy-mode self-merge + skip-review now derives from the Flow
    mode, and a manual easy-mode run still enters the complaint phase."""
    storage = get_storage()
    spec = FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/r", is_leader=False),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r", is_leader=True),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="x", description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y", description="",
                     depends_on=["t1"], is_leader_summary=True),
        ],
        variables={"csflow.easy_mode": "true"},
    )
    flow = storage.flow_create(
        Flow(name="t", description="", owner_user="alice").with_spec(spec)
    )
    _stub_start_run(monkeypatch)

    r = app_client.post(f"/api/flows/{flow.id}/runs", json={})
    assert r.status_code == 202, r.text
    run_row = get_storage().run_get(r.json()["id"])
    assert run_row is not None
    assert run_row.is_scheduled is False


def test_trigger_without_easy_mode_run_not_scheduled(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="alice")  # no csflow.easy_mode variable
    _stub_start_run(monkeypatch)
    r = app_client.post(f"/api/flows/{flow.id}/runs", json={})
    assert r.status_code == 202, r.text
    run_row = get_storage().run_get(r.json()["id"])
    assert run_row is not None
    assert run_row.is_scheduled is False


def test_trigger_unattended_marks_run_and_hidden_from_public_inputs(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unattended=true rides as a _csflow_ marker in run.inputs (drives
    run_is_unattended) and is NOT exposed in the run's public Execution
    Parameters. is_scheduled stays False."""
    flow = _make_flow(owner="alice")
    _stub_start_run(monkeypatch)

    r = app_client.post(
        f"/api/flows/{flow.id}/runs",
        json={"inputs": {"goal": "x"}, "unattended": True},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]

    run_row = get_storage().run_get(run_id)
    assert run_row is not None
    assert run_row.is_scheduled is False
    assert run_row.inputs.get("_csflow_unattended") == "true"

    # Public detail view hides the internal marker but keeps user inputs.
    detail = app_client.get(f"/api/runs/{run_id}").json()
    assert detail["inputs"] == {"goal": "x"}


def test_run_result_endpoint_extracts_leader_report(
    app_client: TestClient,
) -> None:
    """GET /runs/{id}/result returns terminal/success + the leader report from
    the run_terminal_execution_log event."""
    flow = _make_flow(owner="alice")
    storage = get_storage()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)
    storage.event_append(RunEvent(
        run_id=run.id,
        type="run_terminal_execution_log",
        payload={"worker_report_history": [
            {"from_agent": "leader", "summary": "leader final reply: shipped it — see /repo/out.txt"},
        ]},
    ))

    r = app_client.get(f"/api/runs/{run.id}/result")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runId"] == run.id
    assert body["terminal"] is True
    assert body["success"] is True
    assert body["report"] == "shipped it — see /repo/out.txt"


def test_run_result_endpoint_pending_has_no_report(
    app_client: TestClient,
) -> None:
    flow = _make_flow(owner="alice")
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    body = app_client.get(f"/api/runs/{run.id}/result").json()
    assert body["terminal"] is False
    assert body["success"] is False
    assert body["report"] is None


def test_trigger_backfills_flow_cleanup_policy(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="alice", cleanup_team=False)
    captured: dict[str, Any] = {}

    def fake_start_run(self, *, run, spec, flow=None, **kw):
        captured["cleanup"] = flow.cleanup_team_on_finish if flow is not None else None
        from app.scheduler.controller import RunController
        return RunController(run=run, spec=spec)

    monkeypatch.setattr(engine_mod.FlowScheduler, "start_run", fake_start_run)
    r = app_client.post(f"/api/flows/{flow.id}/runs", json={})
    assert r.status_code == 202, r.text
    refreshed = get_storage().flow_get(flow.id)
    assert refreshed is not None
    assert refreshed.cleanup_team_on_finish is True
    assert captured["cleanup"] is True


def test_trigger_with_runtime_prompt_prefixes_flow_and_tasks(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="alice")
    captured: dict[str, Any] = {}

    def fake_start_run(self, *, run, spec, flow=None, flow_description="", **kw):
        captured["flow_description"] = flow_description
        captured["task_descriptions"] = {t.id: t.description for t in spec.tasks}
        from app.scheduler.controller import RunController
        return RunController(run=run, spec=spec)

    monkeypatch.setattr(engine_mod.FlowScheduler, "start_run", fake_start_run)

    r = app_client.post(
        f"/api/flows/{flow.id}/runs",
        json={"runtimePrompt": "Project = acme-platform"},
    )
    assert r.status_code == 202, r.text
    assert "Run-time User Parameters" in captured["flow_description"]
    assert "Project = acme-platform" in captured["flow_description"]
    assert "Run-time User Parameters" in captured["task_descriptions"]["t1"]
    assert "Project = acme-platform" in captured["task_descriptions"]["t1"]


def test_trigger_builds_runtime_prompt_from_inputs_when_not_provided(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="alice")
    captured: dict[str, Any] = {}

    def fake_start_run(self, *, run, spec, flow=None, flow_description="", **kw):
        captured["flow_description"] = flow_description
        captured["task_descriptions"] = {t.id: t.description for t in spec.tasks}
        from app.scheduler.controller import RunController
        return RunController(run=run, spec=spec)

    monkeypatch.setattr(engine_mod.FlowScheduler, "start_run", fake_start_run)

    r = app_client.post(
        f"/api/flows/{flow.id}/runs",
        json={"inputs": {"目标项目": "acme", "目标单号": "ORD-42"}},
    )
    assert r.status_code == 202, r.text
    assert "Run-time User Parameters" in captured["flow_description"]
    assert "- **目标项目**: acme" in captured["flow_description"]
    assert "- **目标单号**: ORD-42" in captured["flow_description"]
    assert "Run-time User Parameters" in captured["task_descriptions"]["t1"]
    assert "- **目标项目**: acme" in captured["task_descriptions"]["t1"]


def test_run_schedule_create_list_delete(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)
    create = app_client.post(
        "/api/run-schedules",
        json={
            "name": "nightly",
            "runMode": "parallel",
            "executeMode": "once",
            "runAt": run_at.isoformat(),
            "items": [
                {
                    "flowId": flow.id,
                    "inputs": {"目标项目": "acme"},
                }
            ],
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["name"] == "nightly"
    assert body["runMode"] == "parallel"
    assert body["executeMode"] == "once"
    assert body["items"][0]["flowId"] == flow.id
    assert body["items"][0]["inputs"] == {"目标项目": "acme"}

    listed = app_client.get("/api/run-schedules")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == body["id"]

    deleted = app_client.delete(f"/api/run-schedules/{body['id']}")
    assert deleted.status_code == 204, deleted.text
    listed_after = app_client.get("/api/run-schedules")
    assert listed_after.status_code == 200, listed_after.text
    assert listed_after.json()["total"] == 0


def test_run_schedule_update(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)
    created = app_client.post(
        "/api/run-schedules",
        json={
            "name": "初始任务",
            "runMode": "parallel",
            "executeMode": "once",
            "runAt": run_at.isoformat(),
            "items": [{"flowId": flow.id, "inputs": {}}],
        },
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    updated_run_at = run_at + timedelta(days=2)
    updated = app_client.patch(
        f"/api/run-schedules/{schedule_id}",
        json={
            "name": "更新后的任务",
            "runMode": "serial",
            "executeMode": "recurring",
            "intervalDays": 3,
            "runAt": updated_run_at.isoformat(),
            "items": [{"flowId": flow.id, "inputs": {}}],
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["id"] == schedule_id
    assert body["name"] == "更新后的任务"
    assert body["runMode"] == "serial"
    assert body["executeMode"] == "recurring"
    assert body["intervalDays"] == 3


def test_run_schedule_requires_non_empty_name(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)
    created = app_client.post(
        "/api/run-schedules",
        json={
            "name": "   ",
            "runMode": "parallel",
            "executeMode": "once",
            "runAt": run_at.isoformat(),
            "items": [{"flowId": flow.id, "inputs": {}}],
        },
    )
    assert created.status_code == 400, created.text
    assert created.json()["error"] == "INVALID_PAYLOAD"


@pytest.mark.asyncio
async def test_run_schedule_execution_api_list_and_detail(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.services import run_schedules as schedule_mod

    schedule = schedule_mod.create_schedule(
        user="alice",
        name="exec-api",
        run_mode="serial",
        execute_mode="once",
        run_at=run_at,
        items=[{"flow_id": flow.id, "inputs": {}}],
        storage=get_storage(),
    )

    async def fake_trigger(*, schedule, item, storage):
        del schedule, item, storage
        return None, "trigger_failed", "mocked trigger failure"

    monkeypatch.setattr(schedule_mod, "_trigger_configured_run", fake_trigger)
    worker = schedule_mod.get_run_schedule_worker()
    await worker._execute_schedule(schedule.id)

    listed = get_storage().run_schedule_execution_list(
        user="alice",
        schedule_id=schedule.id,
        limit=10,
        offset=0,
    )
    assert listed[1] >= 1
    latest = listed[0][0]

    list_resp = app_client.get(f"/api/run-schedule-executions?scheduleId={schedule.id}")
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["total"] >= 1

    detail_resp = app_client.get(f"/api/run-schedule-executions/{latest.id}")
    assert detail_resp.status_code == 200, detail_resp.text
    detail_body = detail_resp.json()
    assert detail_body["id"] == latest.id
    assert len(detail_body["itemResults"]) >= 1


@pytest.mark.asyncio
async def test_run_schedule_worker_executes_once_and_auto_deletes(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del app_client  # keeps fixture lifespan setup
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.services import run_schedules as schedule_mod

    schedule = schedule_mod.create_schedule(
        user="alice",
        name="once",
        run_mode="parallel",
        execute_mode="once",
        run_at=run_at,
        items=[{"flow_id": flow.id, "inputs": {"目标项目": "acme"}}],
        storage=get_storage(),
    )
    captured: list[str] = []

    async def fake_trigger(*, schedule, item, storage):
        del storage
        captured.append(str(getattr(item, "flow_id", "")))
        return None, "trigger_failed", "mocked"

    monkeypatch.setattr(schedule_mod, "_trigger_configured_run", fake_trigger)
    worker = schedule_mod.get_run_schedule_worker()
    await worker._execute_schedule(schedule.id)
    assert captured == [flow.id]
    assert get_storage().run_schedule_get(schedule.id) is None


@pytest.mark.asyncio
async def test_run_schedule_once_claims_before_waiting_for_terminal(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del app_client
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.services import run_schedules as schedule_mod

    schedule = schedule_mod.create_schedule(
        user="alice",
        name="once-claim-early",
        run_mode="parallel",
        execute_mode="once",
        run_at=run_at,
        items=[{"flow_id": flow.id, "inputs": {}}],
        storage=get_storage(),
    )
    observed: dict[str, Any] = {}

    async def fake_trigger(*, schedule, item, storage):
        del schedule, item, storage
        return "run-once-claimed", "", ""

    async def fake_wait_run_terminal(*, run_id, storage, stop_evt):
        del run_id, stop_evt
        observed["exists_before_wait"] = storage.run_schedule_get(schedule.id) is not None
        return RunStatus.completed

    monkeypatch.setattr(schedule_mod, "_trigger_configured_run", fake_trigger)
    monkeypatch.setattr(schedule_mod, "_wait_run_terminal", fake_wait_run_terminal)
    worker = schedule_mod.get_run_schedule_worker()
    await worker._execute_schedule(schedule.id)

    assert observed["exists_before_wait"] is False
    assert get_storage().run_schedule_get(schedule.id) is None


@pytest.mark.asyncio
async def test_run_schedule_worker_recurring_updates_next_run(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del app_client  # keeps fixture lifespan setup
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.services import run_schedules as schedule_mod

    schedule = schedule_mod.create_schedule(
        user="alice",
        name="recurring",
        run_mode="parallel",
        execute_mode="recurring",
        interval_days=2,
        run_at=run_at,
        items=[{"flow_id": flow.id, "inputs": {}}],
        storage=get_storage(),
    )

    async def fake_trigger(*, schedule, item, storage):
        del schedule, item, storage
        return None, "trigger_failed", "mocked"

    monkeypatch.setattr(schedule_mod, "_trigger_configured_run", fake_trigger)
    worker = schedule_mod.get_run_schedule_worker()
    await worker._execute_schedule(schedule.id)
    refreshed = get_storage().run_schedule_get(schedule.id)
    assert refreshed is not None
    assert refreshed.interval_days == 2
    next_run_at = refreshed.next_run_at
    if next_run_at.tzinfo is None:
        next_run_at = next_run_at.replace(tzinfo=timezone.utc)
    assert next_run_at > datetime.now(timezone.utc) + timedelta(days=1)


@pytest.mark.asyncio
async def test_run_schedule_worker_records_missing_flow_failure(
    app_client: TestClient,
) -> None:
    del app_client
    flow = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.services import run_schedules as schedule_mod

    schedule = schedule_mod.create_schedule(
        user="alice",
        name="missing-flow-check",
        run_mode="serial",
        execute_mode="once",
        run_at=run_at,
        items=[{"flow_id": flow.id, "inputs": {}}],
        storage=get_storage(),
    )
    assert get_storage().flow_delete(flow.id)

    worker = schedule_mod.get_run_schedule_worker()
    await worker._execute_schedule(schedule.id)

    rows, total = get_storage().run_schedule_execution_list(user="alice", limit=10, offset=0)
    assert total >= 1
    latest = rows[0]
    assert latest.schedule_id == schedule.id
    assert latest.failed_items >= 1
    assert any(item.get("reason_code") == "flow_not_found" for item in latest.item_results)


@pytest.mark.asyncio
async def test_run_schedule_serial_stops_after_first_failure(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del app_client
    flow1 = _make_flow(owner="alice")
    flow2 = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.services import run_schedules as schedule_mod

    schedule = schedule_mod.create_schedule(
        user="alice",
        name="serial-stop",
        run_mode="serial",
        execute_mode="once",
        run_at=run_at,
        items=[
            {"flow_id": flow1.id, "inputs": {}},
            {"flow_id": flow2.id, "inputs": {}},
        ],
        storage=get_storage(),
    )
    triggered: list[str] = []

    async def fake_trigger(*, schedule, item, storage):
        del schedule, storage
        triggered.append(item.flow_id)
        return "run-fail-1", "", ""

    async def fake_wait_run_terminal(*, run_id, storage, stop_evt):
        del run_id, storage, stop_evt
        return RunStatus.failed

    monkeypatch.setattr(schedule_mod, "_trigger_configured_run", fake_trigger)
    monkeypatch.setattr(schedule_mod, "_wait_run_terminal", fake_wait_run_terminal)
    worker = schedule_mod.get_run_schedule_worker()
    await worker._execute_schedule(schedule.id)

    assert triggered == [flow1.id]
    rows, _ = get_storage().run_schedule_execution_list(user="alice", limit=10, offset=0)
    latest = rows[0]
    statuses = [item.get("status") for item in latest.item_results]
    assert statuses == ["failed", "skipped"]


@pytest.mark.asyncio
async def test_run_schedule_parallel_continues_when_one_flow_fails(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del app_client
    flow1 = _make_flow(owner="alice")
    flow2 = _make_flow(owner="alice")
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    from app.services import run_schedules as schedule_mod

    schedule = schedule_mod.create_schedule(
        user="alice",
        name="parallel-continue",
        run_mode="parallel",
        execute_mode="once",
        run_at=run_at,
        items=[
            {"flow_id": flow1.id, "inputs": {}},
            {"flow_id": flow2.id, "inputs": {}},
        ],
        storage=get_storage(),
    )

    async def fake_trigger(*, schedule, item, storage):
        del schedule, storage
        if item.flow_id == flow1.id:
            return "run-ok-1", "", ""
        return None, "trigger_failed", "mocked trigger failure"

    async def fake_wait_run_terminal(*, run_id, storage, stop_evt):
        del storage, stop_evt
        if run_id == "run-ok-1":
            return RunStatus.failed
        return RunStatus.completed

    monkeypatch.setattr(schedule_mod, "_trigger_configured_run", fake_trigger)
    monkeypatch.setattr(schedule_mod, "_wait_run_terminal", fake_wait_run_terminal)
    worker = schedule_mod.get_run_schedule_worker()
    await worker._execute_schedule(schedule.id)

    rows, _ = get_storage().run_schedule_execution_list(user="alice", limit=10, offset=0)
    latest = rows[0]
    statuses = [item.get("status") for item in latest.item_results]
    assert statuses == ["failed", "failed"]
    assert latest.skipped_items == 0


@pytest.mark.asyncio
async def test_schedule_execution_resumes_remaining_items_after_restart(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schedule execution a prior process left ``running`` resumes from the
    persisted plan: a succeeded item is skipped, a still-``pending`` item starts
    a fresh run, and an in-flight ``running`` item is RE-ATTACHED (waited on by
    its existing run_id) — NEVER re-triggered. So sequencing is decoupled from
    whether the in-flight run was re-executed mid-way."""
    del app_client
    from app.models import FlowRunScheduleExecution
    from app.services import run_schedules as schedule_mod

    flow_a = _make_flow(owner="alice")
    flow_b = _make_flow(owner="alice")
    flow_c = _make_flow(owner="alice")
    storage = get_storage()
    execution = storage.run_schedule_execution_create(FlowRunScheduleExecution(
        schedule_id="sched-resume", schedule_name="s", user="alice",
        run_mode="serial", execute_mode="once", status="running", total_items=3,
        item_results=[
            {"index": 0, "flow_id": flow_a.id, "flow_name": "a", "status": "succeeded",
             "reason": "", "reason_code": "completed", "run_id": "run-old-0", "inputs": {}},
            # In-flight when the process died — its run auto-resumes on its own.
            {"index": 1, "flow_id": flow_b.id, "flow_name": "b", "status": "running",
             "reason": "", "reason_code": "", "run_id": "run-inflight-1", "inputs": {"k": "v"}},
            # Never started.
            {"index": 2, "flow_id": flow_c.id, "flow_name": "c", "status": "pending",
             "reason": "", "reason_code": "", "run_id": "", "inputs": {}},
        ],
    ))

    triggered: list[str] = []

    async def fake_trigger(*, schedule, item, storage):
        del schedule, storage
        triggered.append(item.flow_id)
        return f"run-new-{item.index}", "", ""

    async def fake_wait(*, run_id, storage, stop_evt):
        del storage, stop_evt
        return RunStatus.completed if run_id else None

    monkeypatch.setattr(schedule_mod, "_trigger_configured_run", fake_trigger)
    monkeypatch.setattr(schedule_mod, "_wait_run_terminal", fake_wait)

    worker = schedule_mod.get_run_schedule_worker()
    await worker._resume_schedule_execution(execution.id)

    # Only the never-started item (flow_c) was triggered; the in-flight item
    # (flow_b) was re-attached (NOT re-triggered), the succeeded item skipped.
    assert triggered == [flow_c.id]
    refreshed = storage.run_schedule_execution_get(execution.id)
    assert refreshed.status == "succeeded"
    assert refreshed.finished_at is not None
    by_idx = {int(e["index"]): e for e in refreshed.item_results}
    assert by_idx[0]["status"] == "succeeded"           # untouched
    assert by_idx[1]["status"] == "succeeded"            # re-attached to its run
    assert by_idx[1]["run_id"] == "run-inflight-1"       # SAME run, not re-triggered
    assert by_idx[2]["status"] == "succeeded"            # fresh run
    assert by_idx[2]["run_id"] == "run-new-2"


# ── List + detail ----------------------------------------------------


def test_list_runs_default_filters_by_user(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    _make_run(
        flow_id=flow.id,
        user="alice",
        team_name="csflow-A",
        inputs={"目标项目": "acme"},
    )
    _make_run(flow_id=flow.id, user="bob", team_name="csflow-B")
    r = app_client.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    teams = {item["teamName"] for item in body["items"]}
    assert teams == {"csflow-A"}
    only = body["items"][0]
    assert only["inputs"] == {"目标项目": "acme"}


def test_list_runs_hides_internal_csflow_inputs(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    _make_run(
        flow_id=flow.id,
        user="alice",
        team_name="csflow-A",
        inputs={
            "目标项目": "acme",
            "_csflow_post_complaint_final_status": "completed",
        },
    )
    r = app_client.get("/api/runs")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["inputs"] == {"目标项目": "acme"}


def test_clear_run_history_deletes_terminal_keeps_active(
    app_client: TestClient,
) -> None:
    flow = _make_flow(owner="alice")
    _make_run(flow_id=flow.id, user="alice", status=RunStatus.completed,
              team_name="csflow-done")
    _make_run(flow_id=flow.id, user="alice", status=RunStatus.running,
              team_name="csflow-active")
    r = app_client.delete("/api/runs/history")
    assert r.status_code == 200
    body = r.json()
    assert body["runsDeleted"] == 1
    # Active run survives; only the terminal one is gone.
    remaining = app_client.get("/api/runs").json()["items"]
    teams = {item["teamName"] for item in remaining}
    assert teams == {"csflow-active"}


def test_clear_run_history_scoped_to_caller(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    _make_run(flow_id=flow.id, user="alice", status=RunStatus.completed,
              team_name="csflow-a")
    _make_run(flow_id=flow.id, user="bob", status=RunStatus.completed,
              team_name="csflow-b")
    r = app_client.delete("/api/runs/history")
    assert r.status_code == 200
    assert r.json()["runsDeleted"] == 1
    # Bob's terminal run is untouched.
    all_runs = app_client.get("/api/runs?allUsers=true").json()["items"]
    assert {i["teamName"] for i in all_runs} == {"csflow-b"}


def test_clear_run_schedule_executions(app_client: TestClient) -> None:
    from app.models import FlowRunScheduleExecution

    storage = get_storage()
    storage.run_schedule_execution_create(FlowRunScheduleExecution(
        schedule_id="s1", user="alice", status="succeeded",
    ))
    storage.run_schedule_execution_create(FlowRunScheduleExecution(
        schedule_id="s1", user="alice", status="running",
    ))
    storage.run_schedule_execution_create(FlowRunScheduleExecution(
        schedule_id="s2", user="bob", status="failed",
    ))
    r = app_client.delete("/api/run-schedule-executions")
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    # Alice's running record + bob's record remain.
    rows, total = storage.run_schedule_execution_list(limit=200)
    assert total == 2


def test_list_runs_all_users_query(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    _make_run(flow_id=flow.id, user="alice", team_name="csflow-A")
    _make_run(flow_id=flow.id, user="bob", team_name="csflow-B")
    r = app_client.get("/api/runs?allUsers=true")
    assert r.status_code == 200
    teams = {i["teamName"] for i in r.json()["items"]}
    assert teams == {"csflow-A", "csflow-B"}


def test_get_run_detail_includes_pending_merges_and_board_url(
    app_client: TestClient,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {"files": 2}}],
    )
    r = app_client.get(f"/api/runs/{run.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["pendingMerges"][0]["agentId"] == "alice"
    assert body["pendingMerges"][0]["diffSummary"] == {"files": 2}
    assert body["clawteamBoardUrl"] == f"/clawteam-board-proxy/?team={run.team_name}"
    assert body["specSnapshot"]["agents"]
    assert "isLeader" in body["specSnapshot"]["agents"][0]
    assert "is_leader" not in body["specSnapshot"]["agents"][0]
    assert "ownerAgentId" in body["specSnapshot"]["tasks"][0]
    assert "owner_agent_id" not in body["specSnapshot"]["tasks"][0]


def test_get_run_detail_hides_internal_csflow_inputs(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id,
        inputs={
            "目标项目": "acme",
            "_csflow_post_complaint_final_status": "completed",
        },
    )
    r = app_client.get(f"/api/runs/{run.id}")
    assert r.status_code == 200
    assert r.json()["inputs"] == {"目标项目": "acme"}


def test_list_run_terminals_returns_tmux_snapshots(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, team_name="csflow-termx")

    async def fake_capture(target: str, *, history_lines: int = 60) -> str:
        if target.endswith(":leader"):
            return ""
        return f"{target}#{history_lines}"

    monkeypatch.setattr("app.api.runs.tmux_capture_pane", fake_capture)
    r = app_client.get(f"/api/runs/{run.id}/terminals?historyLines=80")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    by_task = {item["taskId"]: item for item in body["items"]}
    t1 = by_task["t1"]
    assert t1["ownerAgentId"] == "alice"
    assert t1["ownerKind"] == "claude"
    assert t1["tmuxTarget"] == "clawteam-csflow-termx:alice"
    assert t1["workDir"] == ""
    assert t1["paneText"] == "clawteam-csflow-termx:alice#80"
    assert t1["available"] is True
    ts = by_task["ts"]
    assert ts["ownerAgentId"] == "leader"
    assert ts["tmuxTarget"] == "clawteam-csflow-termx:leader"
    assert ts["paneText"] == ""
    assert ts["available"] is False


def test_list_run_terminals_404_and_403(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    other = _make_run(flow_id=flow.id, user="bob")
    r = app_client.get(f"/api/runs/{other.id}/terminals")
    assert r.status_code == 403
    r = app_client.get("/api/runs/nope/terminals")
    assert r.status_code == 404


def test_list_run_terminals_meta_skips_pane_capture(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, team_name="csflow-termx")

    async def fail_capture(*args, **kwargs) -> str:
        raise AssertionError("meta endpoint must not capture tmux panes")

    monkeypatch.setattr("app.api.runs.tmux_capture_pane", fail_capture)
    r = app_client.get(f"/api/runs/{run.id}/terminals/meta")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["paneText"] == ""
    assert body["items"][0]["available"] is False


def test_get_run_terminal_pane_returns_owner_snapshot(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, team_name="csflow-termx")

    async def fake_capture(target: str, *, history_lines: int = 60) -> str:
        return f"{target}#{history_lines}"

    monkeypatch.setattr("app.api.runs.tmux_capture_pane", fake_capture)
    r = app_client.get(f"/api/runs/{run.id}/terminals/panes/alice?historyLines=80")
    assert r.status_code == 200
    body = r.json()
    assert body["ownerAgentId"] == "alice"
    assert body["paneText"] == "clawteam-csflow-termx:alice#80"
    assert body["available"] is True


def test_get_run_terminal_pane_404_for_unknown_owner(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, team_name="csflow-termx")
    r = app_client.get(f"/api/runs/{run.id}/terminals/panes/nobody")
    assert r.status_code == 404


def test_get_run_404_and_403(app_client: TestClient) -> None:
    flow = _make_flow(owner="alice")
    other = _make_run(flow_id=flow.id, user="bob")
    r = app_client.get(f"/api/runs/{other.id}")
    assert r.status_code == 403
    r = app_client.get("/api/runs/nope")
    assert r.status_code == 404


# ── Events pagination ------------------------------------------------


def test_events_pagination_with_since_id(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id)
    storage = get_storage()
    for i in range(5):
        storage.event_append(RunEvent(
            run_id=run.id, type=f"e{i}",
        ))
    r = app_client.get(f"/api/runs/{run.id}/events")
    body = r.json()
    assert len(body["items"]) == 5
    next_id = body["items"][1]["id"]
    r2 = app_client.get(f"/api/runs/{run.id}/events?sinceId={next_id}&limit=2")
    items2 = r2.json()["items"]
    assert all(item["id"] > next_id for item in items2)
    assert len(items2) == 2


# ── Actions ----------------------------------------------------------


def test_abort_unknown_run_404(app_client: TestClient) -> None:
    r = app_client.post("/api/runs/missing/abort")
    assert r.status_code == 404


def test_abort_terminal_run_409(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)
    r = app_client.post(f"/api/runs/{run.id}/abort")
    assert r.status_code == 409
    assert r.json()["error"] == "RUN_NOT_RUNNING"


def test_abort_unknown_to_scheduler_marks_aborted(app_client: TestClient) -> None:
    """When the controller isn't in this process (DB-only run), still mark aborted."""
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    r = app_client.post(f"/api/runs/{run.id}/abort")
    assert r.status_code == 200
    assert r.json()["status"] == "aborted"


def test_abort_active_scheduler_marks_aborted_without_inline_cleanup(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "cancel_run", lambda _rid: True)
    touched: dict[str, str] = {}

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow
        touched["run_id"] = run.id

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)

    r = app_client.post(f"/api/runs/{run.id}/abort")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "aborted"
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.aborted
    # Active scheduler path defers cleanup to scheduler finalization.
    assert "run_id" not in touched


def test_abort_inactive_scheduler_triggers_inline_cleanup(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    # Terminating a PAUSED run (no live controller) runs inline cleanup.
    run = _make_run(flow_id=flow.id, status=RunStatus.paused)
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "cancel_run", lambda _rid: False)
    touched: dict[str, str] = {}

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow
        touched["run_id"] = run.id

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)

    r = app_client.post(f"/api/runs/{run.id}/abort")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "aborted"
    assert touched["run_id"] == run.id


def test_abort_with_pending_merges_marks_aborted_and_cleans_up(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.running,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "cancel_run", lambda _rid: False)
    touched: dict[str, str] = {}

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow
        touched["run_id"] = run.id

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)

    r = app_client.post(f"/api/runs/{run.id}/abort")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "aborted"
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.aborted
    assert refreshed.pending_merges is None
    assert touched["run_id"] == run.id


# ── Pause / continue / terminate ------------------------------------


def test_terminate_rejected_in_review_phase(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_review)
    r = app_client.post(f"/api/runs/{run.id}/abort")
    assert r.status_code == 409
    assert r.json()["error"] == "RUN_NOT_RUNNING"


def test_terminate_allowed_on_paused_run(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.paused)
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "cancel_run", lambda _rid: False)
    from app.api import runs as runs_mod

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow

    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)
    r = app_client.post(f"/api/runs/{run.id}/abort")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "aborted"


def test_pause_rejected_when_no_live_controller(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    r = app_client.post(f"/api/runs/{run.id}/pause")
    assert r.status_code == 409
    assert r.json()["error"] == "RUN_NOT_RUNNING"


def test_pause_rejected_in_review_phase(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_review)
    r = app_client.post(f"/api/runs/{run.id}/pause")
    assert r.status_code == 409
    assert r.json()["error"] == "RUN_NOT_PAUSABLE"


def test_pause_signals_live_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    sched = engine_mod.get_scheduler()
    signalled: dict[str, object] = {}

    class _Ctl:
        def __init__(self) -> None:
            self.run = run

        def is_pausing(self) -> bool:
            return False

        def pause(self, *, reason: str, detail: str = "", **kw) -> None:
            del kw
            signalled["reason"] = reason

    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Ctl())
    r = app_client.post(f"/api/runs/{run.id}/pause")
    assert r.status_code == 200, r.text
    assert signalled.get("reason") == "user"
    # Eager persist: reason=user is on disk even before finalize flips status.
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    blob = (refreshed.inputs or {}).get("_csflow_pause_state")
    assert isinstance(blob, dict)
    assert blob.get("reason") == "user"


def test_pause_discarded_when_already_pausing(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a pause is in flight (``is_pausing()`` True — e.g. a failure pause
    already fired), a further pause request is DISCARDED: pause() is NOT called
    again (so a higher-authority reason can't be clobbered by the user reason),
    and the endpoint returns 200 with the current summary."""
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    sched = engine_mod.get_scheduler()
    calls: list[str] = []

    class _Ctl:
        def __init__(self) -> None:
            self.run = run

        def is_pausing(self) -> bool:
            return True

        def pause(self, *, reason: str, detail: str = "", **kw) -> None:
            del kw
            calls.append(reason)

    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Ctl())
    r = app_client.post(f"/api/runs/{run.id}/pause")
    assert r.status_code == 200, r.text
    assert calls == []  # discarded — pause() never re-invoked


def test_continue_rejected_when_not_paused(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    r = app_client.post(f"/api/runs/{run.id}/continue")
    assert r.status_code == 409
    assert r.json()["error"] == "RUN_NOT_PAUSED"


def test_continue_paused_calls_resume_run(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.paused)
    sched = engine_mod.get_scheduler()
    called: dict[str, str] = {}

    def fake_resume(*, run, flow, storage=None):  # noqa: ANN001
        del flow, storage
        called["run_id"] = run.id
        return object()

    monkeypatch.setattr(sched, "resume_run", fake_resume)
    r = app_client.post(f"/api/runs/{run.id}/continue")
    assert r.status_code == 200, r.text
    assert called.get("run_id") == run.id


def test_paused_run_summary_exposes_pause_state(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.paused,
        inputs={
            "_csflow_pause_state": {
                "reason": "failure",
                "detail": "Task \"slow step\" failed (timeout): step slow",
                "failure_inbox_message": "FAILED: t1: env broken",
                "failure_task_id": "t1",
                "failure_task_subject": "slow step",
                "failure_agent_id": "alice",
                "failure_signal": "leader_inbox_failed",
                "failure_detail": "env broken",
                "needs_confirmation": False,
            },
        },
    )
    r = app_client.get(f"/api/runs/{run.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paused"
    assert body["pause"]["reason"] == "failure"
    assert body["pause"]["failureInboxMessage"] == "FAILED: t1: env broken"
    assert body["pause"]["failureTaskId"] == "t1"
    assert body["pause"]["failureTaskSubject"] == "slow step"
    assert body["pause"]["failureSignal"] == "leader_inbox_failed"
    assert body["pause"]["needsConfirmation"] is False


def test_dismiss_pending_merge(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[
            {"agent_id": "alice", "branch": "b1", "diff_summary": {}},
            {"agent_id": "bob", "branch": "b2", "diff_summary": {}},
        ],
    )
    from app.api import runs as runs_mod

    async def fake_cleanup(**_kw):
        return True

    # Worktree cleanup is now DEFERRED to the complaint-phase terminal cleanup;
    # the merge/dismiss endpoints no longer call it. Stub tolerantly (raising=False)
    # so these state-machine tests stay valid whether or not the symbol exists.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
        raising=False,
    )
    r = app_client.post(
        f"/api/runs/{run.id}/dismiss-merge", json={"agentId": "alice"},
    )
    assert r.status_code == 200
    refreshed = get_storage().run_get(run.id)
    assert {p["agent_id"] for p in refreshed.pending_merges} == {"bob"}


def test_dismiss_pending_merge_clears_preserve_worktree_flag(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
        inputs={"_csflow_preserve_worktree_agent_ids": ["alice"]},
    )
    from app.api import runs as runs_mod

    async def fake_cleanup(**_kw):
        return True

    # Worktree cleanup is now DEFERRED to the complaint-phase terminal cleanup;
    # the merge/dismiss endpoints no longer call it. Stub tolerantly (raising=False)
    # so these state-machine tests stay valid whether or not the symbol exists.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
        raising=False,
    )
    r = app_client.post(
        f"/api/runs/{run.id}/dismiss-merge", json={"agentId": "alice"},
    )
    assert r.status_code == 200, r.text
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert "_csflow_preserve_worktree_agent_ids" not in (refreshed.inputs or {})


def test_dismiss_pending_merge_unknown_agent_404(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )
    r = app_client.post(
        f"/api/runs/{run.id}/dismiss-merge", json={"agentId": "ghost"},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "MERGE_NOT_PENDING"


def test_dismiss_pending_merge_resolved_enters_awaiting_complaint(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )

    from app.api import runs as runs_mod

    async def fake_cleanup(**_kw):
        return True

    # Worktree cleanup is now DEFERRED to the complaint-phase terminal cleanup;
    # the merge/dismiss endpoints no longer call it. Stub tolerantly (raising=False)
    # so these state-machine tests stay valid whether or not the symbol exists.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
        raising=False,
    )
    r = app_client.post(
        f"/api/runs/{run.id}/dismiss-merge", json={"agentId": "alice"},
    )
    assert r.status_code == 200, r.text
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.awaiting_user_complaint
    assert refreshed.inputs.get("_csflow_post_complaint_final_status") == "completed"
    assert refreshed.finished_at is not None


def test_merge_not_awaiting_review_409(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    r = app_client.post(
        f"/api/runs/{run.id}/merge", json={"agentId": "alice"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "NOT_AWAITING_REVIEW"


def test_pending_merge_diff_returns_patch(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{
            "agent_id": "alice",
            "branch": "clawteam/csflow-test/alice",
            "target_branch": "main",
            "diff_summary": {},
        }],
    )

    captured: dict[str, Any] = {}

    class _FakeCli:
        async def workspace_agent_patch(self, *, team, agent, repo, **kw):
            captured["team"] = team
            captured["agent"] = agent
            captured["repo"] = repo
            return {
                "repo_root": "/tmp/r",
                "worktree_path": "/tmp/wt/alice",
                "branch": "clawteam/csflow-test/alice",
                "base_branch": "main",
                "patch": "diff --git a/x b/x\n+added\n-removed\n",
                "patch_truncated": False,
                "uncommitted_patch": "",
                "uncommitted_truncated": False,
                "base_ahead": 1,
                "branch_ahead": 4,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/pending-merges/alice/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "alice"
    assert body["baseBranch"] == "main"
    assert body["targetBranch"] == "main"
    assert body["branch"] == "clawteam/csflow-test/alice"
    assert "+added" in body["patch"]
    assert body["patchTruncated"] is False
    assert body["baseAhead"] == 1
    assert body["branchAhead"] == 4
    assert captured["team"] == "csflow-test"
    assert captured["agent"] == "alice"


def test_pending_merge_diff_unknown_agent_404(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )
    r = app_client.get(f"/api/runs/{run.id}/pending-merges/ghost/diff")
    assert r.status_code == 404
    assert r.json()["error"] == "MERGE_NOT_PENDING"


def test_pending_merge_diff_workspace_not_found_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )

    class _FakeCli:
        async def workspace_agent_patch(self, *, team, agent, repo, **kw):
            del team, agent, repo, kw
            return None

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/pending-merges/alice/diff")
    assert r.status_code == 404
    assert r.json()["error"] == "WORKSPACE_NOT_FOUND"


def test_checkpoint_item_diff_returns_patch(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {
                "downstream_task_id": "t2",
                "items": [{
                    "task_id": "t1",
                    "owner_agent_id": "alice",
                    "branch_name": "clawteam/csflow-test/alice",
                    "base_branch": "main",
                    "decision": "pending",
                }],
            }

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())

    captured: dict[str, Any] = {}

    class _FakeCli:
        async def workspace_agent_patch(self, *, team, agent, repo, **kw):
            captured["team"] = team
            captured["agent"] = agent
            return {
                "repo_root": "/tmp/r",
                "worktree_path": "/tmp/wt/alice",
                "branch": "clawteam/csflow-test/alice",
                "base_branch": "main",
                "patch": "diff --git a/x b/x\n+added\n",
                "patch_truncated": False,
                "uncommitted_patch": "@@\n+scratch\n",
                "uncommitted_truncated": False,
                "base_ahead": 0,
                "branch_ahead": 2,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/checkpoint/items/t1/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "alice"
    assert body["baseBranch"] == "main"
    # No merge target at a checkpoint → target falls back to the base branch.
    assert body["targetBranch"] == "main"
    assert "+added" in body["patch"]
    assert "+scratch" in body["uncommittedPatch"]
    assert body["branchAhead"] == 2
    assert captured["team"] == "csflow-test"
    assert captured["agent"] == "alice"


def test_checkpoint_item_diff_controller_absent_409(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: None)
    r = app_client.get(f"/api/runs/{run.id}/checkpoint/items/t1/diff")
    assert r.status_code == 409
    assert r.json()["error"] == "CHECKPOINT_UNAVAILABLE"


def test_checkpoint_item_diff_unknown_item_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": [
                {"task_id": "t1", "owner_agent_id": "alice", "decision": "pending"},
            ]}

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.get(f"/api/runs/{run.id}/checkpoint/items/ghost/diff")
    assert r.status_code == 404
    assert r.json()["error"] == "CHECKPOINT_ITEM_NOT_FOUND"


def test_checkpoint_item_diff_workspace_not_found_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": [
                {"task_id": "t1", "owner_agent_id": "alice", "decision": "pending"},
            ]}

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())

    class _FakeCli:
        async def workspace_agent_patch(self, *, team, agent, repo, **kw):
            del team, agent, repo, kw
            return None

        async def run_merged_agent_patch(self, *, team, agent, repo, **kw):
            del team, agent, repo, kw
            return None

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/checkpoint/items/t1/diff")
    assert r.status_code == 404
    assert r.json()["error"] == "WORKSPACE_NOT_FOUND"


def test_checkpoint_item_diff_auto_merge_uses_merge_history(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-merge sub-tasks already merged their branch into the baseline, so the
    live worktree three-dot diff is empty — the endpoint must reconstruct the
    contribution from merge history."""
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": [{
                "task_id": "t1",
                "owner_agent_id": "alice",
                "branch_name": "clawteam/csflow-test/alice",
                "base_branch": "main",
                "decision": "pending",
            }]}

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())

    captured: dict[str, Any] = {}

    class _FakeCli:
        async def workspace_agent_patch(self, *, team, agent, repo, **kw):
            # Branch already merged into base → empty committed diff, no scratch.
            return {
                "repo_root": "/tmp/r",
                "branch": "clawteam/csflow-test/alice",
                "base_branch": "main",
                "patch": "",
                "patch_truncated": False,
                "uncommitted_patch": "",
                "uncommitted_truncated": False,
                "base_ahead": 3,
                "branch_ahead": 0,
            }

        async def run_merged_agent_patch(self, *, team, agent, repo, include_patch=True, **kw):
            captured["merged_team"] = team
            captured["merged_agent"] = agent
            captured["include_patch"] = include_patch
            return {
                "repo_root": "/tmp/r",
                "branch": "clawteam/csflow-test/alice",
                "merge_count": 1,
                "commit_count": 2,
                "files_changed": 1,
                "insertions": 5,
                "deletions": 1,
                "patch": "diff --git a/y b/y\n+merged-line\n",
                "patch_truncated": False,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/checkpoint/items/t1/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "alice"
    # Committed patch comes from the reconstructed merge history, not the worktree.
    assert "+merged-line" in body["patch"]
    assert captured["merged_agent"] == "alice"
    assert captured["include_patch"] is True


def test_checkpoint_item_diff_openclaw_passes_agent_workspace_repo(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenClaw checkpoint items self-merge into ``agents/{id}/workspace``; the
    diff endpoint must pass that repo into merge-history reconstruction."""
    from app import paths

    storage = get_storage()
    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="xuezhe", kind=AgentKind.openclaw, is_leader=False,
                merge_strategy=MergeStrategy.agent_self,
                on_failure=OnFailure.retry, max_retries=2,
            ),
            FlowAgent(
                id="leader", kind=AgentKind.claude, repo="/tmp/r", is_leader=True,
                merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry, max_retries=2,
            ),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="xuezhe", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y",
                     description="", depends_on=["t1"], is_leader_summary=True),
        ],
    )
    flow = Flow(
        name="oc-checkpoint",
        description="",
        owner_user="alice",
    ).with_spec(spec)
    flow = storage.flow_create(flow)
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "ts", "items": [{
                "task_id": "t1",
                "owner_agent_id": "xuezhe",
                "decision": "pending",
            }]}

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())

    expected_repo = str(paths.agent_dir("xuezhe") / "workspace")
    captured: dict[str, Any] = {}

    class _FakeCli:
        async def workspace_agent_patch(self, *, team, agent, repo, **kw):
            captured["wt_repo"] = repo
            return None

        async def run_merged_agent_patch(self, *, team, agent, repo, include_patch=True, **kw):
            captured["merged_repo"] = repo
            return {
                "repo_root": expected_repo,
                "branch": f"clawteam/{team}/xuezhe",
                "patch": "diff --git a/f b/f\n+line\n",
                "patch_truncated": False,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/checkpoint/items/t1/diff")
    assert r.status_code == 200, r.text
    assert captured["wt_repo"] == expected_repo
    assert captured["merged_repo"] == expected_repo
    assert "+line" in r.json()["patch"]


def test_merge_calls_perform_manual_merge(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )

    captured = {}

    async def fake_merge(*, run, agent_id, storage, **kw):
        captured["run_id"] = run.id
        captured["agent_id"] = agent_id
        captured["terminalize"] = kw.get("terminalize_when_resolved")
        return True, "merged ok"

    async def fake_cleanup(*, run, agent_id, storage, **kw):
        del storage, kw
        captured["cleanup_run_id"] = run.id
        captured["cleanup_agent_id"] = agent_id
        return True

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "perform_manual_merge", fake_merge)
    # Worktree cleanup is now DEFERRED to the complaint-phase terminal cleanup;
    # the merge/dismiss endpoints no longer call it. Stub tolerantly (raising=False)
    # so these state-machine tests stay valid whether or not the symbol exists.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
        raising=False,
    )
    r = app_client.post(f"/api/runs/{run.id}/merge", json={"agentId": "alice"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "agentId": "alice", "success": True, "message": "merged ok",
    }
    assert captured["agent_id"] == "alice"
    assert captured["terminalize"] is False
    # Worktree cleanup is DEFERRED to the complaint-phase terminal cleanup, so the
    # merge endpoint must NOT clean the agent worktree immediately.
    assert "cleanup_run_id" not in captured
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert "_csflow_preserve_worktree_agent_ids" not in (refreshed.inputs or {})


def test_merge_conflict_returns_reason_and_manual_resolution_guidance(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )

    async def fake_merge(*, run, agent_id, storage, **kw):
        del agent_id, kw
        run.pending_merges = None
        storage.run_update(run)
        return False, "CONFLICT: content conflict in README.md"

    cleanup_called = {"value": False}

    async def fake_cleanup(**_kw):
        cleanup_called["value"] = True
        return True

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "perform_manual_merge", fake_merge)
    # Worktree cleanup is now DEFERRED to the complaint-phase terminal cleanup;
    # the merge/dismiss endpoints no longer call it. Stub tolerantly (raising=False)
    # so these state-machine tests stay valid whether or not the symbol exists.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
        raising=False,
    )
    r = app_client.post(f"/api/runs/{run.id}/merge", json={"agentId": "alice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "alice"
    assert body["success"] is False
    assert "Merge failed" in body["message"]
    assert "CONFLICT" in body["message"]
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.awaiting_user_complaint
    assert refreshed.inputs.get("_csflow_post_complaint_final_status") == "completed_with_conflicts"
    assert refreshed.inputs.get("_csflow_preserve_worktree_agent_ids") == ["alice"]
    assert cleanup_called["value"] is False


def test_merge_environment_error_returns_repo_guidance(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )

    async def fake_merge(*, run, agent_id, storage, **kw):
        del agent_id, kw
        run.pending_merges = None
        storage.run_update(run)
        return False, "workspace metadata missing repo_root for team='x', agent='alice'"

    cleanup_called = {"value": False}

    async def fake_cleanup(**_kw):
        cleanup_called["value"] = True
        return True

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "perform_manual_merge", fake_merge)
    # Worktree cleanup is now DEFERRED to the complaint-phase terminal cleanup;
    # the merge/dismiss endpoints no longer call it. Stub tolerantly (raising=False)
    # so these state-machine tests stay valid whether or not the symbol exists.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
        raising=False,
    )
    r = app_client.post(f"/api/runs/{run.id}/merge", json={"agentId": "alice"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "alice"
    assert body["success"] is False
    assert "environment/repository error" in body["message"]
    assert "repo_root" in body["message"]
    refreshed = get_storage().run_get(run.id)
    assert refreshed is not None
    assert refreshed.status == RunStatus.awaiting_user_complaint
    assert refreshed.inputs.get("_csflow_post_complaint_final_status") == "completed_with_conflicts"
    assert refreshed.inputs.get("_csflow_preserve_worktree_agent_ids") == ["alice"]
    assert cleanup_called["value"] is False


def test_merge_failed_run_pending_resolved_keeps_terminal_and_triggers_cleanup(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.failed,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )
    captured: dict[str, str] = {}

    async def fake_merge(*, run, agent_id, storage, **kw):
        run.pending_merges = None
        storage.run_update(run)
        return True, "merged ok"

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow
        captured["run_id"] = run.id

    from app.api import runs as runs_mod
    async def fake_review_cleanup(**_kw):
        return True

    monkeypatch.setattr(runs_mod, "perform_manual_merge", fake_merge)
    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)
    # Deferred-cleanup: endpoint no longer calls per-agent review cleanup.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_review_cleanup,
        raising=False,
    )

    r = app_client.post(f"/api/runs/{run.id}/merge", json={"agentId": "alice"})
    assert r.status_code == 200, r.text
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.failed
    assert refreshed.pending_merges is None
    assert "_csflow_preserve_worktree_agent_ids" not in (refreshed.inputs or {})
    assert captured["run_id"] == run.id


def test_merge_review_with_failed_terminal_hint_resolves_to_failed(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
        inputs={"_csflow_post_review_terminal_status": "failed"},
    )
    captured: dict[str, str] = {}

    async def fake_merge(*, run, agent_id, storage, **kw):
        del agent_id, kw
        run.pending_merges = None
        storage.run_update(run)
        return True, "merged ok"

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow
        captured["run_id"] = run.id

    from app.api import runs as runs_mod
    async def fake_review_cleanup(**_kw):
        return True

    monkeypatch.setattr(runs_mod, "perform_manual_merge", fake_merge)
    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)
    # Deferred-cleanup: endpoint no longer calls per-agent review cleanup.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_review_cleanup,
        raising=False,
    )

    r = app_client.post(f"/api/runs/{run.id}/merge", json={"agentId": "alice"})
    assert r.status_code == 200, r.text
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.failed
    assert refreshed.pending_merges is None
    assert (refreshed.inputs or {}).get("_csflow_post_review_terminal_status") is None
    assert "_csflow_preserve_worktree_agent_ids" not in (refreshed.inputs or {})
    assert captured["run_id"] == run.id


def test_dismiss_aborted_run_pending_resolved_keeps_terminal_and_triggers_cleanup(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.aborted,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
    )
    captured: dict[str, str] = {}

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow
        captured["run_id"] = run.id

    from app.api import runs as runs_mod
    async def fake_review_cleanup(**_kw):
        return True

    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)
    # Deferred-cleanup: endpoint no longer calls per-agent review cleanup.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_review_cleanup,
        raising=False,
    )

    r = app_client.post(
        f"/api/runs/{run.id}/dismiss-merge", json={"agentId": "alice"},
    )
    assert r.status_code == 200, r.text
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.aborted
    assert refreshed.pending_merges is None
    assert captured["run_id"] == run.id


def test_dismiss_review_with_aborted_terminal_hint_resolves_to_aborted(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(cleanup_team=True)
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.awaiting_user_review,
        pending=[{"agent_id": "alice", "branch": "b1", "diff_summary": {}}],
        inputs={"_csflow_post_review_terminal_status": "aborted"},
    )
    captured: dict[str, str] = {}

    async def fake_cleanup(*, run, storage, flow=None):
        del storage, flow
        captured["run_id"] = run.id

    from app.api import runs as runs_mod
    async def fake_review_cleanup(**_kw):
        return True

    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_cleanup)
    # Deferred-cleanup: endpoint no longer calls per-agent review cleanup.
    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_review_cleanup,
        raising=False,
    )

    r = app_client.post(
        f"/api/runs/{run.id}/dismiss-merge", json={"agentId": "alice"},
    )
    assert r.status_code == 200, r.text
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.aborted
    assert refreshed.pending_merges is None
    assert (refreshed.inputs or {}).get("_csflow_post_review_terminal_status") is None
    assert captured["run_id"] == run.id


def test_submit_user_complaint_starts_background_workflow(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.awaiting_user_complaint,
        inputs={"_csflow_post_complaint_final_status": "completed"},
    )
    captured: dict[str, str] = {}

    def fake_start(self, *, run, flow, complaint_text, storage=None):
        captured["run_id"] = run.id
        captured["text"] = complaint_text

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "start_run_complaint_phase", fake_start.__get__(sched))
    monkeypatch.setattr(sched, "complaint_in_progress", lambda _rid: False)

    r = app_client.post(
        f"/api/runs/{run.id}/complaint",
        json={"message": "leader summary is weak"},
    )
    assert r.status_code == 200, r.text
    assert captured["run_id"] == run.id
    assert captured["text"] == "leader summary is weak"
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.complaint_processing


def test_skip_user_complaint_starts_background_skip_workflow(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.awaiting_user_complaint,
        inputs={"_csflow_post_complaint_final_status": "completed_with_conflicts"},
    )
    captured: dict[str, str] = {}

    def fake_start_skip(self, *, run, flow, storage=None):
        del storage
        captured["run_id"] = run.id
        captured["flow_id"] = flow.id

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "complaint_in_progress", lambda _rid: False)
    monkeypatch.setattr(
        sched,
        "start_run_skip_complaint_phase",
        fake_start_skip.__get__(sched),
    )

    r = app_client.post(f"/api/runs/{run.id}/complaint/skip")
    assert r.status_code == 200, r.text
    assert captured["run_id"] == run.id
    assert captured["flow_id"] == flow.id
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.complaint_processing


def test_skip_user_complaint_conflict_when_processing_in_progress(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id,
        status=RunStatus.complaint_processing,
    )
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "complaint_in_progress", lambda _rid: True)

    r = app_client.post(f"/api/runs/{run.id}/complaint/skip")
    assert r.status_code == 409
    assert r.json()["error"] == "COMPLAINT_ALREADY_RUNNING"


def test_retry_task_no_active_controller_409(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    r = app_client.post(f"/api/runs/{run.id}/retry-task/t1")
    assert r.status_code == 409
    assert r.json()["error"] == "RETRY_UNAVAILABLE"


def test_retry_task_unknown_id_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a controller, a bad task id should yield TASK_NOT_FOUND."""
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)

    # Simulate an active controller by populating a CompileResult.
    from app.scheduler.compiler import CompileResult
    from app.scheduler.controller import RunController
    spec = FlowSpec.model_validate(flow.spec)
    rc = RunController(run=run, spec=spec)
    rc.compile_result = CompileResult(
        team_name=run.team_name, leader_agent_id="leader",
        flow_to_clawteam={"t1": "ct1"}, clawteam_to_flow={"ct1": "t1"},
    )

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda rid: rc)

    # Patch MCP so we don't hit a real subprocess.
    async def fake_get_mcp_client(**_kw):
        class _M:
            async def task_update(self, **kw):
                return {}
        return _M()
    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_mcp_client", fake_get_mcp_client)

    r = app_client.post(f"/api/runs/{run.id}/retry-task/missing")
    assert r.status_code == 404


def test_retry_task_calls_mcp_with_clawteam_id(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)

    from app.scheduler.compiler import CompileResult
    from app.scheduler.controller import RunController
    spec = FlowSpec.model_validate(flow.spec)
    rc = RunController(run=run, spec=spec)
    rc.compile_result = CompileResult(
        team_name=run.team_name, leader_agent_id="leader",
        flow_to_clawteam={"t1": "ct-real"}, clawteam_to_flow={"ct-real": "t1"},
    )

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda rid: rc)

    captured: dict = {}

    async def fake_get_mcp_client(**_kw):
        class _M:
            async def task_update(self, **kw):
                captured.update(kw)
                return {}
        return _M()
    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_mcp_client", fake_get_mcp_client)

    r = app_client.post(f"/api/runs/{run.id}/retry-task/t1")
    assert r.status_code == 200
    assert captured["task_id"] == "ct-real"
    assert captured["status"] == "pending"
    assert captured["force"] is True


def test_checkpoint_approve_requires_awaiting_status(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)

    class _Controller:
        def checkpoint_snapshot(self):
            return None

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(f"/api/runs/{run.id}/checkpoint/items/t1/approve")
    assert r.status_code == 409
    assert r.json()["error"] == "NOT_AWAITING_CHECKPOINT"


def test_get_checkpoint_snapshot_calls_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {
                "downstream_task_id": "t2",
                "items": [{"task_id": "t1", "agent_id": "alice", "decision": "pending"}],
            }

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.get(f"/api/runs/{run.id}/checkpoint")
    assert r.status_code == 200, r.text
    assert r.json()["downstream_task_id"] == "t2"
    assert r.json()["items"][0]["task_id"] == "t1"


def test_get_checkpoint_snapshot_returns_null_when_controller_absent(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: None)
    r = app_client.get(f"/api/runs/{run.id}/checkpoint")
    assert r.status_code == 200, r.text
    assert r.json() is None


def test_checkpoint_approve_calls_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    captured: dict[str, Any] = {}

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": []}

        async def approve_checkpoint_item(self, *, upstream_task_id: str):
            captured["task_id"] = upstream_task_id
            row = get_storage().run_get(run.id)
            assert row is not None
            row.status = RunStatus.running
            get_storage().run_update(row)

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(f"/api/runs/{run.id}/checkpoint/items/t1/approve")
    assert r.status_code == 200, r.text
    assert captured["task_id"] == "t1"
    assert r.json()["status"] == "running"


def test_checkpoint_approve_returns_unavailable_when_controller_absent(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: None)
    r = app_client.post(f"/api/runs/{run.id}/checkpoint/items/t1/approve")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "CHECKPOINT_UNAVAILABLE"


def test_checkpoint_rerun_requires_feedback(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty feedback is rejected by the CONTROLLER for local agents (the API
    forwards it as-is because external-node items legitimately rerun without
    feedback — one-click re-dispatch)."""
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": []}

        async def request_checkpoint_rerun(self, *, upstream_task_id: str, feedback: str):
            assert feedback == ""
            raise ValueError("checkpoint rerun feedback is required")

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(
        f"/api/runs/{run.id}/checkpoint/items/t1/rerun",
        json={"feedback": "   "},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_PAYLOAD"


def test_checkpoint_rerun_calls_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    captured: dict[str, Any] = {}

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": []}

        async def request_checkpoint_rerun(self, *, upstream_task_id: str, feedback: str):
            captured["task_id"] = upstream_task_id
            captured["feedback"] = feedback

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(
        f"/api/runs/{run.id}/checkpoint/items/t1/rerun",
        json={"feedback": "请补充风险评估"},
    )
    assert r.status_code == 200, r.text
    assert captured["task_id"] == "t1"
    assert captured["feedback"] == "请补充风险评估"


def test_checkpoint_rerun_conflict_maps_specific_error(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": []}

        async def request_checkpoint_rerun(self, *, upstream_task_id: str, feedback: str):
            del upstream_task_id, feedback
            raise RuntimeError(
                "owner agent already has an active checkpoint rerun: agent=alice task=t1"
            )

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(
        f"/api/runs/{run.id}/checkpoint/items/t1b/rerun",
        json={"feedback": "重跑"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "CHECKPOINT_RERUN_CONFLICT"


def test_external_task_redispatch_calls_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_external)
    captured: dict[str, Any] = {}

    class _Controller:
        async def redispatch_waiting_external_task(self, *, task_id: str):
            captured["task_id"] = task_id

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(f"/api/runs/{run.id}/external-tasks/t-remote/redispatch")
    assert r.status_code == 200, r.text
    assert captured["task_id"] == "t-remote"


def test_external_task_redispatch_rejects_human_channel(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)

    class _Controller:
        async def redispatch_waiting_external_task(self, *, task_id: str):
            raise ValueError(
                "redispatch is only available for webhook / remote_csflow "
                "(got 'human')"
            )

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(f"/api/runs/{run.id}/external-tasks/t-human/redispatch")
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_PAYLOAD"


def test_external_task_redispatch_terminal_run_409(
    app_client: TestClient,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)
    r = app_client.post(f"/api/runs/{run.id}/external-tasks/t1/redispatch")
    assert r.status_code == 409
    assert r.json()["error"] == "EXTERNAL_RUN_NOT_ACTIVE"


def test_checkpoint_mark_read_calls_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    captured: dict[str, Any] = {}

    class _Controller:
        def checkpoint_snapshot(self):
            return {"downstream_task_id": "t2", "items": []}

        async def mark_checkpoint_item_read(self, *, upstream_task_id: str):
            captured["task_id"] = upstream_task_id

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(f"/api/runs/{run.id}/checkpoint/items/t1/mark-read")
    assert r.status_code == 200, r.text
    assert captured["task_id"] == "t1"


def test_checkpoint_mark_read_requires_awaiting_status(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)

    class _Controller:
        def checkpoint_snapshot(self):
            return None

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(f"/api/runs/{run.id}/checkpoint/items/t1/mark-read")
    assert r.status_code == 409
    assert r.json()["error"] == "NOT_AWAITING_CHECKPOINT"


# ── Run diff (post-run merged-into-baseline module) -------------------


def _make_openclaw_flow(owner: str = "alice") -> Flow:
    spec = FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="ocw", kind=AgentKind.openclaw,
                      target_branch=None, is_leader=False,
                      merge_strategy=MergeStrategy.agent_self,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=True, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="t2", owner_agent_id="ocw", subject="x",
                     description="", depends_on=[]),
            FlowTask(id="ts", owner_agent_id="leader", subject="y",
                     description="", depends_on=["t1"], is_leader_summary=True),
        ],
    )
    flow = Flow(name="oc", description="", owner_user=owner).with_spec(spec)
    return get_storage().flow_create(flow)


def test_run_diff_lists_merged_agents_including_openclaw(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_openclaw_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    queried: list[str] = []

    class _FakeCli:
        async def run_merged_agent_patch(
            self, *, team, agent, repo, include_patch=True, **kw,
        ):
            del team, repo, include_patch, kw
            queried.append(agent)
            if agent == "alice":
                return {
                    "repo_root": "/tmp/r", "branch": f"clawteam/{run.team_name}/alice",
                    "merge_count": 1, "commit_count": 2, "files_changed": 3,
                    "insertions": 10, "deletions": 4, "patch": "", "patch_truncated": False,
                }
            if agent == "ocw":
                return {
                    "repo_root": "/tmp/r", "branch": f"clawteam/{run.team_name}/ocw",
                    "merge_count": 1, "commit_count": 1, "files_changed": 2,
                    "insertions": 5, "deletions": 0, "patch": "", "patch_truncated": False,
                }
            # leader has a merge commit but ZERO net file changes (empty merge)
            return {
                "repo_root": "/tmp/r", "branch": f"clawteam/{run.team_name}/{agent}",
                "merge_count": 1, "commit_count": 1, "files_changed": 0,
                "insertions": 0, "deletions": 0, "patch": "", "patch_truncated": False,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/run-diff")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    # leader is queried (it owns a worktree too) but filtered out here because
    # its merge brought zero net file changes.
    assert [i["agentId"] for i in items] == ["alice", "ocw"]
    assert items[0]["commitCount"] == 2
    assert "ocw" in queried
    assert "leader" in queried


def test_run_diff_resolves_openclaw_repo_to_agent_workspace(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (run f2d8b01c57b6): OpenClaw agents carry NO ``repo`` on the
    Flow spec — their merge lands in ``~/.clawsomeflow/agents/{id}/workspace``.
    The Run-diff endpoints must resolve the repo via ``_resolve_agent_repo_for_run``
    (like every other run endpoint), NOT read raw ``agent.repo`` (which is empty
    → ``None`` → ``run_merged_agent_patch`` returns None → the agent is dropped
    and the module renders "本次运行没有修改被并入项目"). Assert the OpenClaw
    agent is queried with its workspace repo, and a normal agent with its spec repo.
    """
    from app import paths

    flow = _make_openclaw_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    seen: dict[str, str | None] = {}

    class _FakeCli:
        async def run_merged_agent_patch(
            self, *, team, agent, repo, include_patch=True, **kw,
        ):
            del team, include_patch, kw
            seen[agent] = repo
            return {
                "repo_root": repo or "", "branch": f"clawteam/{run.team_name}/{agent}",
                "merge_count": 1, "commit_count": 1, "files_changed": 1,
                "insertions": 1, "deletions": 0, "patch": "", "patch_truncated": False,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/run-diff")
    assert r.status_code == 200, r.text
    # OpenClaw agent resolved to its per-agent workspace, NOT None.
    assert seen["ocw"] == str(paths.agent_dir("ocw") / "workspace")
    # A normal agent still resolves to its spec repo.
    assert seen["alice"] == "/tmp/r"


def test_run_agent_diff_returns_patch(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    class _FakeCli:
        async def run_merged_agent_patch(
            self, *, team, agent, repo, include_patch=True, **kw,
        ):
            del team, repo, kw
            assert include_patch is True
            return {
                "repo_root": "/tmp/r", "branch": f"clawteam/{run.team_name}/{agent}",
                "merge_count": 1, "commit_count": 1, "files_changed": 1,
                "insertions": 1, "deletions": 0,
                "patch": "diff --git a/a.txt b/a.txt\n+alice\n", "patch_truncated": False,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/run-diff/alice")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "alice"
    assert "+alice" in body["patch"]
    assert body["patchTruncated"] is False


def test_run_agent_diff_no_merged_changes_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    class _FakeCli:
        async def run_merged_agent_patch(self, *, team, agent, repo, **kw):
            del team, agent, repo, kw
            # A merge commit exists but brought zero net file changes → 404.
            return {"merge_count": 1, "files_changed": 0, "patch": ""}

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/run-diff/alice")
    assert r.status_code == 404
    assert r.json()["error"] == "NO_MERGED_CHANGES"


def test_run_agent_diff_openclaw_agent_eligible_no_merges_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenClaw agents ARE eligible for Run diff (feat run-diff includes OpenClaw
    # + leader). An OpenClaw agent that merged nothing yields NO_MERGED_CHANGES —
    # NOT AGENT_NOT_FOUND (which would mean it was excluded from the module).
    flow = _make_openclaw_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    class _FakeCli:
        async def run_merged_agent_patch(self, *, team, agent, repo, **kw):
            del team, agent, repo, kw
            return None  # no merge history for this agent

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/run-diff/ocw")
    assert r.status_code == 404
    assert r.json()["error"] == "NO_MERGED_CHANGES"


def test_run_agent_diff_unknown_agent_404(app_client: TestClient) -> None:
    # A genuinely unknown / ineligible agent id → AGENT_NOT_FOUND (short-circuits
    # before any CLI call).
    flow = _make_openclaw_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)
    r = app_client.get(f"/api/runs/{run.id}/run-diff/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"] == "AGENT_NOT_FOUND"


def test_run_diff_revert_success_then_agent_hidden(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    class _FakeCli:
        async def run_merged_agent_patch(self, *, team, agent, repo, **kw):
            del team, repo, kw
            return {
                "repo_root": "/tmp/r", "branch": f"clawteam/{run.team_name}/{agent}",
                "merge_count": 1, "commit_count": 1, "files_changed": 2,
                "insertions": 3, "deletions": 0, "patch": "diff\n+x\n",
                "patch_truncated": False,
            }

        async def revert_agent_merges(self, *, team, agent, repo, target_branch):
            del team, repo
            return {
                "ok": True, "target_branch": target_branch,
                "merge_shas": ["abc123"], "revert_head": "def456",
                "nothing_to_revert": False, "message": "ok",
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    # alice is present before revert.
    before = app_client.get(f"/api/runs/{run.id}/run-diff").json()["items"]
    assert "alice" in [i["agentId"] for i in before]

    r = app_client.post(f"/api/runs/{run.id}/run-diff/alice/revert")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # After revert, alice is excluded from the list and detail is 404.
    after = app_client.get(f"/api/runs/{run.id}/run-diff").json()["items"]
    assert "alice" not in [i["agentId"] for i in after]
    d = app_client.get(f"/api/runs/{run.id}/run-diff/alice")
    assert d.status_code == 404
    assert d.json()["error"] == "MERGE_REVERTED"


def test_run_diff_revert_failure_409(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    class _FakeCli:
        async def revert_agent_merges(self, *, team, agent, repo, target_branch):
            del team, agent, repo, target_branch
            return {
                "ok": False, "target_branch": "main", "merge_shas": [],
                "revert_head": "", "nothing_to_revert": False,
                "message": "git revert failed (conflict); rolled back",
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.post(f"/api/runs/{run.id}/run-diff/alice/revert")
    assert r.status_code == 409
    assert r.json()["error"] == "MERGE_REVERT_FAILED"


def test_run_diff_revert_openclaw_agent(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_openclaw_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.completed)

    class _FakeCli:
        async def revert_agent_merges(self, *, team, agent, repo, target_branch):
            del team, repo, target_branch
            assert agent == "ocw"
            return {
                "ok": True,
                "target_branch": "main",
                "merge_shas": ["abc123"],
                "revert_head": "def456",
                "message": "ok",
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.post(f"/api/runs/{run.id}/run-diff/ocw/revert")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.awaiting_user_complaint,
        RunStatus.complaint_processing,
        RunStatus.completed,
    ],
)
def test_run_diff_list_during_complaint_window_all_modes(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, status: RunStatus,
) -> None:
    # Run diff is mode-agnostic (easy / normal / dev): the API has no flow-mode
    # gate — only the frontend render window starts at awaiting_user_complaint.
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=status)

    class _FakeCli:
        async def run_merged_agent_patch(
            self, *, team, agent, repo, include_patch=True, **kw,
        ):
            del team, repo, include_patch, kw
            if agent != "alice":
                return None
            return {
                "repo_root": "/tmp/r", "branch": f"clawteam/{run.team_name}/alice",
                "merge_count": 1, "commit_count": 1, "files_changed": 1,
                "insertions": 1, "deletions": 0, "patch": "", "patch_truncated": False,
            }

    from app.api import runs as runs_mod
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _FakeCli())

    r = app_client.get(f"/api/runs/{run.id}/run-diff")
    assert r.status_code == 200, r.text
    assert [i["agentId"] for i in r.json()["items"]] == ["alice"]


def test_run_summary_exposes_is_scheduled(app_client: TestClient) -> None:
    flow = _make_flow()
    get_storage().run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-sched",
        status=RunStatus.completed, inputs={}, user="alice", is_scheduled=True,
    ))
    _make_run(flow_id=flow.id, status=RunStatus.completed, team_name="csflow-manual")
    rows = {row["teamName"]: row for row in app_client.get("/api/runs").json()["items"]}
    assert rows["csflow-sched"]["isScheduled"] is True
    assert rows["csflow-manual"]["isScheduled"] is False


def test_webhook_marker_key_hidden_from_run_inputs(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(
        flow_id=flow.id, status=RunStatus.completed,
        inputs={
            "topic": "real input",
            "csflow.terminal_webhook_notified_at": "2026-07-10T00:00:00Z",
            "_csflow_post_complaint_final_status": "completed",
        },
    )
    body = app_client.get(f"/api/runs/{run.id}").json()
    assert body["inputs"] == {"topic": "real input"}
