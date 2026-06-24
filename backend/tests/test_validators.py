"""Tests for :mod:`app.validators.flow`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import paths
from app.config import load_config
from app.integrations import openclaw_json as oj
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
    ERROR_INVALID_TARGET_BRANCH,
    ERROR_LEADER_OWNS_WORKER_TASK,
    ERROR_MISSING_AGENT_REPO,
    ERROR_MISSING_LEADER_SUMMARY,
    ERROR_OPENCLAW_AGENT_NOT_FOUND,
    ERROR_OPENCLAW_AGENT_UNREGISTERED,
    ERROR_SUMMARY_NO_DEPENDENCY,
)


def _register_runtime_openclaw(
    storage,
    agent_id: str,
    *,
    user: str = "alice",
) -> None:
    workspace = paths.agent_dir(agent_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    if storage.openclaw_get(agent_id) is None:
        storage.openclaw_create(
            OpenclawAgent(
                id=agent_id,
                name=agent_id,
                workspace_path=str(workspace),
                created_by_user=user,
            )
        )
    cfg = load_config()
    oc_home = Path(cfg.openclaw_home).expanduser()
    oc_home.mkdir(parents=True, exist_ok=True)
    payload_path = oc_home / "openclaw.json"
    if payload_path.exists():
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    else:
        payload = {
            "agents": {"defaults": {}, "list": []},
            "gateway": {"port": 18789, "auth": {"token": "T"}},
        }
    agents_list = payload.setdefault("agents", {}).setdefault("list", [])
    if not any(
        isinstance(item, dict) and item.get("id") == agent_id
        for item in agents_list
    ):
        agents_list.append(
            {
                "id": agent_id,
                "name": agent_id,
                "workspace": str(workspace),
                "default": False,
            }
        )
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    registry = oj.managed_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    if registry.exists():
        reg = json.loads(registry.read_text(encoding="utf-8"))
    else:
        reg = {"agent_ids": []}
    ids = reg.setdefault("agent_ids", [])
    if agent_id not in ids:
        ids.append(agent_id)
    registry.write_text(json.dumps(reg), encoding="utf-8")


def _git_default_branch(repo: Path) -> str:
    out = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() or "main"


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
    @staticmethod
    def _register_openclaw_leader(storage, agent_id: str = "oc-lead") -> None:
        _register_runtime_openclaw(storage, agent_id)

    def test_repo_must_be_git_repo(self, tmp_path: Path) -> None:
        storage = get_storage()
        self._register_openclaw_leader(storage)
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        spec = FlowSpec(
            agents=[
                FlowAgent(id="oc-lead", kind=AgentKind.openclaw, is_leader=True),
                # cursor is non-OpenClaw (repo validation applies) but not
                # managed-enforced, so this still exercises the repo checks.
                FlowAgent(id="w", kind=AgentKind.cursor, repo=str(not_a_repo), is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="oc-lead", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, storage)
        assert exc.value.code == ERROR_INVALID_REPO

    def test_repo_without_initial_commit_rejected(self, tmp_path: Path) -> None:
        storage = get_storage()
        self._register_openclaw_leader(storage)
        repo = tmp_path / "empty-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        spec = FlowSpec(
            agents=[
                FlowAgent(id="oc-lead", kind=AgentKind.openclaw, is_leader=True),
                FlowAgent(id="w", kind=AgentKind.cursor, repo=str(repo), is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="oc-lead", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, storage)
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
        branch = _git_default_branch(repo)
        spec = FlowSpec(
            agents=[
                FlowAgent(
                    id="a",
                    kind=AgentKind.cursor,
                    repo=str(repo),
                    target_branch=branch,
                    is_leader=True,
                ),
                FlowAgent(id="w", kind=AgentKind.cursor, repo=str(repo), is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="a", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        validate_flow_against_db(spec, get_storage())

    def test_leader_target_branch_must_exist(self, tmp_path: Path) -> None:
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
        branch = _git_default_branch(repo)
        spec = FlowSpec(
            agents=[
                FlowAgent(
                    id="a",
                    kind=AgentKind.cursor,
                    repo=str(repo),
                    target_branch="missing-branch",
                    is_leader=True,
                ),
                FlowAgent(id="w", kind=AgentKind.cursor, repo=str(repo), is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(
                    id="t1",
                    owner_agent_id="a",
                    subject="x",
                    depends_on=["t0"],
                    is_leader_summary=True,
                ),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, get_storage())
        assert exc.value.code == ERROR_INVALID_TARGET_BRANCH
        assert exc.value.details["target_branch"] == "missing-branch"

        spec.agents[0].target_branch = branch
        validate_flow_against_db(spec, get_storage())

    def test_worker_target_branch_must_be_nonempty(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(
                    id="a",
                    kind=AgentKind.claude,
                    repo="/tmp/r",
                    target_branch="main",
                    is_leader=True,
                ),
                FlowAgent(
                    id="w",
                    kind=AgentKind.claude,
                    repo="/tmp/r",
                    target_branch="",
                    is_leader=False,
                ),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(
                    id="t1",
                    owner_agent_id="a",
                    subject="x",
                    depends_on=["t0"],
                    is_leader_summary=True,
                ),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, get_storage())
        assert exc.value.code == ERROR_INVALID_TARGET_BRANCH
        assert exc.value.details["agent_id"] == "w"

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
        _register_runtime_openclaw(storage, "oc-real")
        _register_runtime_openclaw(storage, "oc-worker")
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

    def test_openclaw_agent_unregistered_rejected(self, tmp_path: Path) -> None:
        storage = get_storage()
        _register_runtime_openclaw(storage, "oc-lead")
        _register_runtime_openclaw(storage, "oc-unreg")
        cfg = load_config()
        oc_home = Path(cfg.openclaw_home).expanduser()
        payload = json.loads((oc_home / "openclaw.json").read_text(encoding="utf-8"))
        payload["agents"]["list"] = [
            item
            for item in payload["agents"]["list"]
            if not (isinstance(item, dict) and item.get("id") == "oc-unreg")
        ]
        (oc_home / "openclaw.json").write_text(json.dumps(payload), encoding="utf-8")
        spec = FlowSpec(
            agents=[
                FlowAgent(id="oc-lead", kind=AgentKind.openclaw, is_leader=True),
                FlowAgent(id="oc-unreg", kind=AgentKind.openclaw, is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="oc-unreg", subject="w"),
                FlowTask(id="t1", owner_agent_id="oc-lead", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, storage)
        assert exc.value.code == ERROR_OPENCLAW_AGENT_UNREGISTERED
        assert exc.value.details.get("agent_id") == "oc-unreg"


class TestTemporaryAgents:
    @staticmethod
    def _git_repo(tmp_path: Path) -> Path:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@e.com",
             "commit", "-m", "init"],
            cwd=repo, check=True,
        )
        return repo

    def test_temporary_managed_kind_skips_db_lookup(self, tmp_path: Path) -> None:
        """A temporary claude/hermes agent passes db validation WITHOUT any
        managed/Hermes registration (only the working-dir repo is required)."""
        repo = self._git_repo(tmp_path)
        branch = _git_default_branch(repo)
        spec = FlowSpec(
            agents=[
                FlowAgent(
                    id="tmp-lead",
                    kind=AgentKind.hermes,
                    repo=str(repo),
                    target_branch=branch,
                    is_leader=True,
                    is_temporary=True,
                ),
                FlowAgent(id="tmp-worker", kind=AgentKind.claude, repo=str(repo),
                          is_leader=False, is_temporary=True),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="tmp-worker", subject="w"),
                FlowTask(id="t1", owner_agent_id="tmp-lead", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        # No DB rows created → would raise for persistent agents, but temporary
        # agents skip the lookup.
        validate_flow_against_db(spec, get_storage())

    def test_temporary_agent_still_requires_repo(self) -> None:
        spec = FlowSpec(
            agents=[
                FlowAgent(id="a", kind=AgentKind.claude, repo="", is_leader=True,
                          is_temporary=True),
                FlowAgent(id="w", kind=AgentKind.claude, repo="", is_leader=False,
                          is_temporary=True),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="a", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_against_db(spec, get_storage())
        assert exc.value.code == ERROR_MISSING_AGENT_REPO

    def test_openclaw_cannot_be_temporary(self) -> None:
        with pytest.raises(Exception):
            FlowAgent(id="oc", kind=AgentKind.openclaw, is_leader=True,
                      is_temporary=True)


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

    def test_duplicate_agent_id_cross_platform(self) -> None:
        # An OpenClaw "alice" and a Hermes "alice" are genuinely distinct agents
        # but still collide on FlowAgent.id (which is global within a Flow). The
        # error must call out the cross-platform clash, not just "appears twice".
        spec = FlowSpec(
            agents=[
                FlowAgent(id="alice", kind=AgentKind.openclaw, is_leader=True),
                FlowAgent(id="alice", kind=AgentKind.hermes, repo="/r"),
            ],
            tasks=[
                FlowTask(id="t1", owner_agent_id="alice", subject="x", is_leader_summary=True)
            ],
        )
        with pytest.raises(FlowValidationError) as exc:
            validate_flow_spec(spec)
        assert exc.value.code == ERROR_DUPLICATE_AGENT_ID
        assert exc.value.details.get("kinds") == ["openclaw", "hermes"]
        # message names both platforms (English + Chinese halves present)
        assert "openclaw" in exc.value.message and "hermes" in exc.value.message
        assert "platforms" in exc.value.message  # English half
        assert "跨平台" in exc.value.message  # Chinese half

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
