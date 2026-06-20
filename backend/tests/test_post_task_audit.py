"""Tests for app.worktree.audit — post-task audit (DEV.md §9 layer 3)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.models import (
    AgentKind,
    FlowAgent,
    FlowTask,
    MergeStrategy,
    OnFailure,
)
from app.worktree import audit


def _has_git() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _has_git(), reason="git binary required")


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the repo file lock (Check 1) at an isolated CSFLOW_HOME so the audit
    never writes a lock under the real ``~/.clawsomeflow`` during tests."""
    monkeypatch.setenv("CSFLOW_HOME", str(tmp_path / "_csflow_home"))


# ── helpers ----------------------------------------------------------


def _agent(kind=AgentKind.openclaw) -> FlowAgent:
    return FlowAgent(
        id="alice", kind=kind,
        repo=None if kind == AgentKind.openclaw else "/tmp/x",
        is_leader=False,
        merge_strategy=(MergeStrategy.agent_self if kind == AgentKind.openclaw
                        else MergeStrategy.manual),
        on_failure=OnFailure.retry, max_retries=2,
    )


def _task(id="t1") -> FlowTask:
    return FlowTask(
        id=id, owner_agent_id="alice", subject="x",
        description="", depends_on=[], timeout_seconds=300,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "csflow@local")
    _git(path, "config", "user.name", "csflow")
    (path / ".keep").write_text("seed")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


# ── tests ------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_non_openclaw(tmp_path: Path) -> None:
    res = await audit.run_post_task_audit(
        agent=_agent(kind=AgentKind.claude), task=_task(),
        main_workspace=str(tmp_path), worktree_path=None,
    )
    assert res.skipped == "not_openclaw"


@pytest.mark.asyncio
async def test_skips_when_main_not_git(tmp_path: Path) -> None:
    res = await audit.run_post_task_audit(
        agent=_agent(), task=_task(),
        main_workspace=str(tmp_path / "missing"), worktree_path=None,
    )
    assert res.skipped == "main_not_a_git_repo"


@pytest.mark.asyncio
async def test_clean_main_no_violations(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "main")
    res = await audit.run_post_task_audit(
        agent=_agent(), task=_task(),
        main_workspace=str(main), worktree_path=None,
    )
    assert res.main_dirty is False
    assert res.error == ""


@pytest.mark.asyncio
async def test_dirty_main_gets_auto_committed(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "main")
    # Pollute the main repo (simulating the bug we defend against).
    (main / "leak.txt").write_text("oops")
    res = await audit.run_post_task_audit(
        agent=_agent(), task=_task(),
        main_workspace=str(main), worktree_path=None,
    )
    assert res.main_dirty is True
    assert "leak.txt" in res.main_dirty_files
    assert res.main_stash_ref is None
    assert res.main_auto_commit_sha is not None
    # Working dir should be clean now.
    status = _git(main, "status", "--porcelain").stdout
    assert status.strip() == ""
    head_msg = _git(main, "log", "-1", "--format=%s").stdout.strip()
    assert "main-write checkpoint task t1" in head_msg


@pytest.mark.asyncio
async def test_worktree_with_task_commit_no_action(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "main")
    wt = _init_repo(tmp_path / "wt")
    (wt / "feature.py").write_text("hi")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "task t1: implement feature")
    res = await audit.run_post_task_audit(
        agent=_agent(), task=_task(id="t1"),
        main_workspace=str(main), worktree_path=str(wt),
    )
    assert res.worktree_missing_commit is False
    assert res.auto_checkpoint_sha is None


@pytest.mark.asyncio
async def test_worktree_missing_task_commit_auto_checkpoints(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "main")
    wt = _init_repo(tmp_path / "wt")
    # Worker forgot to commit; left changes uncommitted.
    (wt / "draft.txt").write_text("WIP")
    res = await audit.run_post_task_audit(
        agent=_agent(), task=_task(id="t99"),
        main_workspace=str(main), worktree_path=str(wt),
    )
    assert res.worktree_missing_commit is True
    assert res.auto_checkpoint_sha is not None
    # Verify the auto-commit landed on HEAD.
    head_msg = _git(wt, "log", "-1", "--format=%s").stdout.strip()
    assert "auto-checkpoint task t99" in head_msg


@pytest.mark.asyncio
async def test_worktree_finds_task_commit_in_recent_history(tmp_path: Path) -> None:
    """If the task's commit isn't HEAD but is in the last 20 commits → no action."""
    main = _init_repo(tmp_path / "main")
    wt = _init_repo(tmp_path / "wt")
    (wt / "a.txt").write_text("a")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "task t5: stage 1")
    (wt / "b.txt").write_text("b")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "follow-up tweak")  # head doesn't mention task
    res = await audit.run_post_task_audit(
        agent=_agent(), task=_task(id="t5"),
        main_workspace=str(main), worktree_path=str(wt),
    )
    assert res.worktree_missing_commit is False


@pytest.mark.asyncio
async def test_audit_emits_run_event_on_violation(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "main")
    (main / "leak.txt").write_text("x")  # dirty

    from app.storage import get_storage
    from app.models import Flow, FlowRun, RunStatus
    storage = get_storage()
    flow = Flow(name="t", description="", owner_user="alice").with_spec(
        __import__("app.models", fromlist=["FlowSpec"]).FlowSpec(
            agents=[_agent(AgentKind.claude)._replace_kind() if False else _agent(AgentKind.claude)],
            tasks=[_task()],
        ) if False else
        # Use a minimal spec just to satisfy not-null on table.
        __import__("app.models", fromlist=["FlowSpec"]).FlowSpec(
            agents=[FlowAgent(
                id="leader", kind=AgentKind.claude, repo="/r",
                is_leader=True, merge_strategy=MergeStrategy.manual,
                on_failure=OnFailure.retry, max_retries=2,
            )],
            tasks=[FlowTask(id="t1", owner_agent_id="leader", subject="x",
                            description="", depends_on=[], is_leader_summary=True,
                            timeout_seconds=300)],
        )
    )
    saved = storage.flow_create(flow)
    run = storage.run_create(FlowRun(
        id="run-audit", flow_id=saved.id, flow_version=1,
        team_name="csflow-audit", status=RunStatus.running,
        inputs={}, user="alice",
    ))

    await audit.run_post_task_audit(
        agent=_agent(), task=_task(),
        main_workspace=str(main), worktree_path=None,
        storage=storage, run_id=run.id,
    )
    events = storage.event_list(run_id=run.id)
    assert any(e.type == "main_repo_autocommit" for e in events)
