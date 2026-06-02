"""Tests for :mod:`app.storage.sqlite` (the SQLite ``StorageBackend``)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    OpenclawAgentRequest,
    OpenclawRequestStatus,
    FlowSpec,
    FlowTask,
    OpenclawAgent,
    RunEvent,
    RunStatus,
    TaskDecomposeRequest,
    TaskDecomposeStatus,
)
from app.storage import StorageVersionConflict, get_storage
from app.storage.sqlite import SqliteStorage


def _spec() -> FlowSpec:
    return FlowSpec(
        agents=[FlowAgent(id="a", kind=AgentKind.claude, repo="/r", is_leader=True)],
        tasks=[FlowTask(id="t1", owner_agent_id="a", subject="x", is_leader_summary=True)],
    )


def _flow(name: str = "F", user: str = "alice") -> Flow:
    f = Flow(name=name, owner_user=user)
    return f.with_spec(_spec())


class TestFlowCRUD:
    def test_init_schema_backfills_cleanup_policy_to_true(self, tmp_path) -> None:
        db = tmp_path / "legacy-flows.db"
        s = SqliteStorage(url=f"sqlite:///{db}")
        s.init_schema()
        legacy = Flow(name="legacy", owner_user="alice", cleanup_team_on_finish=False).with_spec(_spec())
        created = s.flow_create(legacy)
        assert created.cleanup_team_on_finish is False
        # Simulate restart/migrate path.
        s.init_schema()
        loaded = s.flow_get(created.id)
        assert loaded is not None
        assert loaded.cleanup_team_on_finish is True

    def test_create_and_get(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        assert flow.id and flow.id.startswith("flow-")
        assert flow.version == 1
        loaded = s.flow_get(flow.id)
        assert loaded is not None
        assert loaded.name == flow.name

    def test_list_filters_by_user(self) -> None:
        s = get_storage()
        s.flow_create(_flow(name="F1", user="alice"))
        s.flow_create(_flow(name="F2", user="bob"))
        s.flow_create(_flow(name="F3", user="alice"))

        items, total = s.flow_list(owner_user="alice")
        assert total == 2
        assert all(f.owner_user == "alice" for f in items)
        assert {f.name for f in items} == {"F1", "F3"}

    def test_list_filters_by_q(self) -> None:
        s = get_storage()
        s.flow_create(_flow(name="customer-onboarding"))
        s.flow_create(_flow(name="risk-analysis"))
        items, total = s.flow_list(owner_user="alice", q="customer")
        assert total == 1 and items[0].name == "customer-onboarding"

    def test_update_bumps_version(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow(name="orig"))
        flow.name = "renamed"
        updated = s.flow_update(flow, expected_version=1)
        assert updated.version == 2
        assert updated.name == "renamed"

    def test_update_optimistic_conflict(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        flow.name = "x"
        with pytest.raises(StorageVersionConflict) as exc:
            s.flow_update(flow, expected_version=42)
        assert exc.value.expected == 42
        assert exc.value.actual == 1

    def test_delete(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        assert s.flow_delete(flow.id) is True
        assert s.flow_get(flow.id) is None
        assert s.flow_delete(flow.id) is False

    def test_active_run_count(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1,
            team_name="csflow-r1", user="alice",
            status=RunStatus.running,
        ))
        s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1,
            team_name="csflow-r2", user="alice",
            status=RunStatus.completed,
        ))
        assert s.run_count_active_for_flow(flow.id) == 1


class TestFlowRunCRUD:
    def test_create_and_list(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        run = s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1,
            team_name="csflow-abcd", user="alice",
        ))
        assert run.id.startswith("run-")
        items, total = s.run_list(flow_id=flow.id)
        assert total == 1
        assert items[0].id == run.id

    def test_update_status(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        run = s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1,
            team_name="csflow-T1", user="alice",
        ))
        run.status = RunStatus.running
        updated = s.run_update(run)
        assert updated.status == RunStatus.running


class TestEventLog:
    def test_append_and_list(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        run = s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1,
            team_name="csflow-evt", user="alice",
        ))
        for i in range(5):
            s.event_append(RunEvent(
                run_id=run.id, type="task_dispatched",
                task_id=f"t{i}", payload={"i": i},
            ))
        events = s.event_list(run_id=run.id)
        assert len(events) == 5
        # Pagination via since_id
        partial = s.event_list(run_id=run.id, since_id=events[1].id)
        assert len(partial) == 3

    def test_history_cleanup_removes_old_terminal_data(self) -> None:
        s = get_storage()
        flow = s.flow_create(_flow())
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=60)
        cutoff = now - timedelta(days=30)

        old_run = s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1, team_name="csflow-old",
            user="alice", status=RunStatus.completed,
            started_at=old, finished_at=old,
        ))
        recent_run = s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1, team_name="csflow-recent",
            user="alice", status=RunStatus.completed,
            started_at=now, finished_at=now,
        ))
        active_run = s.run_create(FlowRun(
            flow_id=flow.id, flow_version=1, team_name="csflow-active",
            user="alice", status=RunStatus.running,
            started_at=old,
        ))
        s.event_append(RunEvent(run_id=old_run.id, type="e-old"))
        s.event_append(RunEvent(run_id=recent_run.id, type="e-recent"))
        s.event_append(RunEvent(run_id=active_run.id, type="e-active"))

        req_old = s.openclaw_request_create(OpenclawAgentRequest(
            request_id="req-old",
            user="alice",
            nl_prompt="old",
            status=OpenclawRequestStatus.succeeded,
            created_at=old,
            updated_at=old,
            expires_at=old,
        ))
        req_live = s.openclaw_request_create(OpenclawAgentRequest(
            request_id="req-live",
            user="alice",
            nl_prompt="live",
            status=OpenclawRequestStatus.pending,
            created_at=old,
            updated_at=old,
            expires_at=old,
        ))
        td_old = s.task_decompose_create(TaskDecomposeRequest(
            request_id="td-old",
            user="alice",
            goal="g",
            leader_agent_id="leader",
            status=TaskDecomposeStatus.failed,
            created_at=old,
            updated_at=old,
            expires_at=old,
        ))
        td_live = s.task_decompose_create(TaskDecomposeRequest(
            request_id="td-live",
            user="alice",
            goal="g",
            leader_agent_id="leader",
            status=TaskDecomposeStatus.dispatched,
            created_at=old,
            updated_at=old,
            expires_at=old,
        ))

        summary = s.history_cleanup(before=cutoff)
        assert old_run.id in summary["deleted_run_ids"]
        assert summary["runs_deleted"] == 1
        assert summary["events_deleted"] == 1
        assert summary["openclaw_requests_deleted"] == 1
        assert summary["task_decompose_requests_deleted"] == 1

        assert s.run_get(old_run.id) is None
        assert s.run_get(recent_run.id) is not None
        assert s.run_get(active_run.id) is not None
        assert len(s.event_list(run_id=old_run.id)) == 0
        assert len(s.event_list(run_id=recent_run.id)) == 1
        assert s.openclaw_request_get(req_old.request_id) is None
        assert s.openclaw_request_get(req_live.request_id) is not None
        assert s.task_decompose_get(td_old.request_id) is None
        assert s.task_decompose_get(td_live.request_id) is not None


class TestOpenclawAgentCRUD:
    def test_full_cycle(self) -> None:
        s = get_storage()
        agent = OpenclawAgent(
            id="oc1", name="My Agent",
            workspace_path="/home/u/.clawsomeflow/agents/oc1/workspace",
            created_by_user="alice",
        )
        s.openclaw_create(agent)
        assert s.openclaw_get("oc1").name == "My Agent"
        assert len(s.openclaw_list(owner_user="alice")) == 1
        agent.name = "Renamed"
        s.openclaw_update(agent)
        assert s.openclaw_get("oc1").name == "Renamed"
        assert s.openclaw_delete("oc1") is True
        assert s.openclaw_get("oc1") is None
