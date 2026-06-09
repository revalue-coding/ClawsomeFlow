"""Tests for :mod:`app.validators.flow`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.models import AgentKind, FlowAgent, FlowSpec, FlowTask, MergeStrategy, OpenclawAgent
from app.storage import get_storage
from app.validators import FlowValidationError, validate_flow_against_db, validate_flow_spec
from app.validators.flow import (
    ERROR_DUPLICATE_AGENT_ID,
    ERROR_DUPLICATE_TASK_ID,
    ERROR_INVALID_AGENT_REF,
    ERROR_INVALID_DAG,
    ERROR_INVALID_LEADER,
    ERROR_INVALID_REPO,
    ERROR_LEADER_OWNS_WORKER_TASK,
    ERROR_MISSING_AGENT_REPO,
    ERROR_MISSING_LEADER_SUMMARY,
    ERROR_OPENCLAW_AGENT_NOT_FOUND,
    ERROR_SUMMARY_NO_DEPENDENCY,
)


def _make_spec(*, leader: bool = True, summary: bool = True) -> FlowSpec:
    # A summary task now requires ≥1 dependency, so the minimal valid spec is a
    # worker task + a leader summary that depends on it.
    return FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/r", is_leader=leader),
            FlowAgent(id="worker", kind=AgentKind.claude, repo="/tmp/r", is_leader=False),
        ],
        tasks=[
            FlowTask(id="t0", owner_agent_id="worker", subject="w"),
            FlowTask(
                id="t1", owner_agent_id="alice", subject="x",
                depends_on=["t0"], is_leader_summary=summary,
            ),
        ],
    )


class TestPureValidation:
    def test_minimal_valid_spec(self) -> None:
        validate_flow_spec(_make_spec())  # no exception

    def test_no_leader(self) -> None:
        spec = _make_spec(leader=False)
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_INVALID_LEADER

    def test_two_leaders(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="a", kind=AgentKind.claude, repo="/r", is_leader=True),
                FlowAgent(id="b", kind=AgentKind.claude, repo="/r", is_leader=True),
            ],
            tasks=[FlowTask(id="t1", owner_agent_id="a", subject="x", is_leader_summary=True)],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_INVALID_LEADER
        assert exc.value.details["leader_count"] == 2

    def test_no_leader_summary(self) -> None:
        spec = _make_spec(summary=False)
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_MISSING_LEADER_SUMMARY

    def test_dangling_owner_ref(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="alice", kind=AgentKind.claude, repo="/r", is_leader=True),
            ],
            tasks=[FlowTask(id="t1", owner_agent_id="bob", subject="x", is_leader_summary=True)],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_INVALID_AGENT_REF

    def test_dangling_dependency(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="leader", kind=AgentKind.claude, repo="/r", is_leader=True),
                FlowAgent(id="worker", kind=AgentKind.claude, repo="/r", is_leader=False),
            ],
            tasks=[
                FlowTask(id="t1", owner_agent_id="worker", subject="x"),
                FlowTask(
                    id="t2", owner_agent_id="leader", subject="y",
                    depends_on=["t1", "missing"], is_leader_summary=True,
                ),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_INVALID_AGENT_REF

    def test_leader_cannot_own_worker_task(self) -> None:
        """Per design: the leader only owns leader-summary tasks. A flow that
        gives the leader a regular worker task is rejected (UI also enforces)."""
        spec = FlowSpec(
            agents=[
                FlowAgent(id="leader", kind=AgentKind.claude, repo="/r", is_leader=True),
                FlowAgent(id="worker", kind=AgentKind.claude, repo="/r", is_leader=False),
            ],
            tasks=[
                FlowTask(id="t1", owner_agent_id="leader", subject="worker job"),
                FlowTask(id="ts", owner_agent_id="leader", subject="summary",
                         depends_on=["t1"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_LEADER_OWNS_WORKER_TASK
        assert exc.value.details["task_ids"] == ["t1"]

    def test_dag_cycle_detected(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="leader", kind=AgentKind.claude, repo="/r", is_leader=True),
                FlowAgent(id="worker", kind=AgentKind.claude, repo="/r", is_leader=False),
            ],
            tasks=[
                FlowTask(id="t1", owner_agent_id="worker", subject="x", depends_on=["t2"]),
                FlowTask(id="t2", owner_agent_id="worker", subject="y", depends_on=["t3"]),
                FlowTask(
                    id="t3", owner_agent_id="leader", subject="z",
                    depends_on=["t1"], is_leader_summary=True,
                ),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_INVALID_DAG
        assert set(exc.value.details["cycle"]) >= {"t1", "t2", "t3"}

    def test_leader_summary_must_be_owned_by_leader(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="alice", kind=AgentKind.claude, repo="/r", is_leader=True),
                FlowAgent(id="bob", kind=AgentKind.claude, repo="/r", is_leader=False),
            ],
            tasks=[
                FlowTask(
                    id="t1", owner_agent_id="bob", subject="x", is_leader_summary=True
                ),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_INVALID_AGENT_REF

    def test_summary_without_dependency_rejected(self) -> None:
        """A leader summary must depend on at least one upstream task."""
        spec = FlowSpec(
            agents=[
                FlowAgent(id="leader", kind=AgentKind.claude, repo="/r", is_leader=True),
                FlowAgent(id="worker", kind=AgentKind.claude, repo="/r", is_leader=False),
            ],
            tasks=[
                FlowTask(id="t1", owner_agent_id="worker", subject="x"),
                FlowTask(id="ts", owner_agent_id="leader", subject="summary",
                         depends_on=[], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_SUMMARY_NO_DEPENDENCY
        assert exc.value.details["task_id"] == "ts"


class TestStorageAwareValidation:
    def test_repo_must_be_git_repo(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        spec = FlowSpec(
            agents=[
                # cursor is non-OpenClaw (repo validation applies) but not
                # managed-enforced, so this still exercises the repo checks.
                FlowAgent(id="a", kind=AgentKind.cursor, repo=str(not_a_repo), is_leader=True),
                FlowAgent(id="w", kind=AgentKind.cursor, repo=str(not_a_repo), is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="a", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, get_storage())
        assert exc.value.code == ERROR_INVALID_REPO

    def test_repo_without_initial_commit_rejected(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        spec = FlowSpec(
            agents=[
                FlowAgent(id="a", kind=AgentKind.cursor, repo=str(repo), is_leader=True),
                FlowAgent(id="w", kind=AgentKind.cursor, repo=str(repo), is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="a", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, get_storage())
        assert exc.value.code == ERROR_INVALID_REPO
        assert exc.value.details["reason"] == "no_initial_commit"

    def test_real_git_repo_with_initial_commit_accepted(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Tester",
                "-c",
                "user.email=tester@example.com",
                "commit",
                "-m",
                "init",
            ],
            cwd=repo,
            check=True,
        )
        spec = FlowSpec(
            agents=[
                FlowAgent(id="a", kind=AgentKind.cursor, repo=str(repo), is_leader=True),
                FlowAgent(id="w", kind=AgentKind.cursor, repo=str(repo), is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="a", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        validate_flow_against_db(spec, get_storage())

    def test_openclaw_agent_must_exist(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="missing-oc", kind=AgentKind.openclaw, is_leader=True),
                FlowAgent(id="missing-oc-w", kind=AgentKind.openclaw, is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="missing-oc-w", subject="w"),
                FlowTask(id="t1", owner_agent_id="missing-oc", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, get_storage())
        assert exc.value.code == ERROR_OPENCLAW_AGENT_NOT_FOUND

    def test_openclaw_agent_present(self) -> None:
        storage = get_storage()
        storage.openclaw_create(OpenclawAgent(
            id="oc-real",
            name="Test agent",
            workspace_path="/tmp/oc-real/workspace",
            created_by_user="alice",
        ))
        storage.openclaw_create(OpenclawAgent(
            id="oc-worker",
            name="Worker agent",
            workspace_path="/tmp/oc-worker/workspace",
            created_by_user="alice",
        ))
        spec = FlowSpec(
            agents=[
                FlowAgent(id="oc-real", kind=AgentKind.openclaw, is_leader=True),
                FlowAgent(id="oc-worker", kind=AgentKind.openclaw, is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="oc-worker", subject="w"),
                FlowTask(id="t1", owner_agent_id="oc-real", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        validate_flow_against_db(spec, storage)


class TestUniqueness:
    def test_duplicate_agent_id(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="dup", kind=AgentKind.claude, repo="/r", is_leader=True),
                FlowAgent(id="dup", kind=AgentKind.codex, repo="/r"),
            ],
            tasks=[FlowTask(id="t1", owner_agent_id="dup", subject="x", is_leader_summary=True)],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_DUPLICATE_AGENT_ID

    def test_duplicate_task_id(self) -> None:
        spec = FlowSpec(
            agents=[FlowAgent(id="a", kind=AgentKind.claude, repo="/r", is_leader=True)],
            tasks=[
                FlowTask(id="dup", owner_agent_id="a", subject="x"),
                FlowTask(id="dup", owner_agent_id="a", subject="y", is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_DUPLICATE_TASK_ID
