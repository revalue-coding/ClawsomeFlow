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


def test_list_runs_all_users_forbidden_in_server_mode(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow(owner="alice")
    _make_run(flow_id=flow.id, user="alice", team_name="csflow-A")
    _make_run(flow_id=flow.id, user="bob", team_name="csflow-B")
    monkeypatch.setattr(
        "app.api.runs.load_config",
        lambda: load_config().model_copy(update={"deployment_mode": "server"}),
    )
    r = app_client.get("/api/runs?allUsers=true")
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


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
    run = _make_run(flow_id=flow.id, status=RunStatus.complaint_processing)
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
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_review)
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


def test_checkpoint_approve_requires_awaiting_status(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
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


def test_checkpoint_rerun_requires_feedback(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    r = app_client.post(
        f"/api/runs/{run.id}/checkpoint/items/t1/rerun",
        json={"feedback": "   "},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_CHECKPOINT_FEEDBACK"


def test_checkpoint_rerun_calls_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    captured: dict[str, Any] = {}

    class _Controller:
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


def test_checkpoint_mark_read_calls_controller(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.awaiting_user_checkpoint)
    captured: dict[str, Any] = {}

    class _Controller:
        async def mark_checkpoint_item_read(self, *, upstream_task_id: str):
            captured["task_id"] = upstream_task_id

    sched = engine_mod.get_scheduler()
    monkeypatch.setattr(sched, "get_controller", lambda _rid: _Controller())
    r = app_client.post(f"/api/runs/{run.id}/checkpoint/items/t1/mark-read")
    assert r.status_code == 200, r.text
    assert captured["task_id"] == "t1"


def test_checkpoint_mark_read_requires_awaiting_status(app_client: TestClient) -> None:
    flow = _make_flow()
    run = _make_run(flow_id=flow.id, status=RunStatus.running)
    r = app_client.post(f"/api/runs/{run.id}/checkpoint/items/t1/mark-read")
    assert r.status_code == 409
    assert r.json()["error"] == "NOT_AWAITING_CHECKPOINT"
