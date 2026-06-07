"""Tests for app.scheduler.failure signals + on_failure."""

from __future__ import annotations

from app.models import (
    AgentKind,
    FlowAgent,
    FlowTask,
    MergeStrategy,
    OnFailure,
)
from app.scheduler import failure as F


def _ftask(id="t1", owner="alice", timeout=1800) -> FlowTask:
    return FlowTask(
        id=id, owner_agent_id=owner, subject="x",
        description="", depends_on=[], timeout_seconds=timeout,
    )


def _snap(task_id="t1", owner="alice", *, status="in_progress",
          locked_by=None, metadata=None, dispatched=None) -> F.TaskSnapshot:
    return F.TaskSnapshot(
        task_id=task_id, owner_agent_id=owner, status=status,
        locked_by_agent=locked_by, metadata=metadata or {},
        dispatched_at_epoch=dispatched,
    )


def _agent(*, on_failure=OnFailure.retry, max_retries=2) -> FlowAgent:
    return FlowAgent(
        id="alice", kind=AgentKind.claude, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=on_failure, max_retries=max_retries,
    )


def test_completed_tasks_skipped() -> None:
    snaps = [_snap(status="completed", locked_by="alice")]
    failures = F.detect_failures(
        team_name="csflow-x",
        flow_tasks={"t1": _ftask()}, snapshots=snaps,
        leader_agent_id="leader",
    )
    assert failures == []


# ── signal 1: worker_reported ----------------------------------------


def test_worker_reported_via_metadata() -> None:
    snaps = [_snap(metadata={"csflow_failed": "build broken"})]
    failures = F.detect_failures(
        team_name="csflow-x",
        flow_tasks={"t1": _ftask()}, snapshots=snaps,
        leader_agent_id="leader",
    )
    assert len(failures) == 1
    assert failures[0].reason == F.FailureReason.worker_reported
    assert "build broken" in failures[0].detail


# ── signal 2: timeout ------------------------------------------------


def test_timeout_detected() -> None:
    snaps = [_snap(dispatched=100.0)]  # very old
    failures = F.detect_failures(
        team_name="csflow-x",
        flow_tasks={"t1": _ftask(timeout=10)}, snapshots=snaps,
        leader_agent_id="leader",
        now=3800.0,  # runtime lower bound is 3600s
    )
    assert len(failures) == 1
    assert failures[0].reason == F.FailureReason.timeout
    assert "min=3600s" in failures[0].detail


def test_timeout_not_triggered_within_limit() -> None:
    snaps = [_snap(dispatched=900.0)]
    failures = F.detect_failures(
        team_name="csflow-x",
        flow_tasks={"t1": _ftask(timeout=300)}, snapshots=snaps,
        leader_agent_id="leader",
        now=1000.0,
    )
    assert failures == []


# ── signal 3: leader inbox -------------------------------------------


def test_leader_inbox_failed() -> None:
    snaps = [_snap()]  # no other failure
    failures = F.detect_failures(
        team_name="csflow-x",
        flow_tasks={"t1": _ftask()}, snapshots=snaps,
        leader_agent_id="leader",
        leader_inbox_messages=["FAILED: t1: env broken"],
    )
    assert len(failures) == 1
    assert failures[0].reason == F.FailureReason.leader_inbox_failed
    assert "env broken" in failures[0].detail


def test_leader_inbox_dedup_with_other_signals() -> None:
    snaps = [_snap(metadata={"csflow_failed": "boom"})]
    failures = F.detect_failures(
        team_name="csflow-x",
        flow_tasks={"t1": _ftask()}, snapshots=snaps,
        leader_agent_id="leader",
        leader_inbox_messages=["FAILED: t1: dup"],
    )
    # 1 worker_reported + 1 leader_inbox_failed (different reasons → both kept).
    reasons = {r.reason for r in failures}
    assert F.FailureReason.worker_reported in reasons
    assert F.FailureReason.leader_inbox_failed in reasons


# ── on_failure ------------------------------------------------------


def test_apply_retry_increments_count() -> None:
    rec = F.FailureRecord(task_id="t1", agent_id="alice", reason=F.FailureReason.timeout)
    decision = F.apply_on_failure(record=rec, agent=_agent(max_retries=3), current_retry_count=0)
    assert decision.action == "retry"
    assert decision.new_retry_count == 1


def test_apply_retry_exhausted_aborts() -> None:
    rec = F.FailureRecord(task_id="t1", agent_id="alice", reason=F.FailureReason.timeout)
    decision = F.apply_on_failure(record=rec, agent=_agent(max_retries=2), current_retry_count=2)
    assert decision.action == "abort"


def test_apply_skip_policy() -> None:
    rec = F.FailureRecord(task_id="t1", agent_id="alice", reason=F.FailureReason.timeout)
    decision = F.apply_on_failure(
        record=rec, agent=_agent(on_failure=OnFailure.skip), current_retry_count=0,
    )
    assert decision.action == "skip"


def test_apply_abort_policy() -> None:
    rec = F.FailureRecord(task_id="t1", agent_id="alice", reason=F.FailureReason.timeout)
    decision = F.apply_on_failure(
        record=rec, agent=_agent(on_failure=OnFailure.abort), current_retry_count=0,
    )
    assert decision.action == "abort"


# ── parser ------------------------------------------------------------


def test_parse_failed_inbox_lenient() -> None:
    assert F._parse_failed_inbox("FAILED: t1: stuff") == ("t1", "stuff")
    assert F._parse_failed_inbox("FAILED: t2") == ("t2", "")
    assert F._parse_failed_inbox("not it") is None
    assert F._parse_failed_inbox(None) is None  # type: ignore[arg-type]
