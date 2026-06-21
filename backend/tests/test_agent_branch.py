"""Tests for git repo branch resolution and agent branch normalization."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.integrations.git_repo import (
    branch_exists_in_repo,
    clawteam_agent_branch_name,
    conventional_branch,
    delete_clawteam_agent_branch,
    delete_clawteam_team_branches,
    delete_local_branch,
    is_clawteam_agent_branch,
    list_flow_target_branches,
    list_local_branches,
    resolve_target_branch,
    resolve_workspace_base_branch,
)
from app.models import AgentKind, FlowAgent, FlowSpec, FlowTask
from app.services.agent_branch import (
    normalize_agent_branch_dict,
    normalize_agent_branch_dicts,
    normalize_flow_spec_branches,
)


def _has_git() -> bool:
    import shutil

    return shutil.which("git") is not None


def _init_repo(path: Path, *, branch: str = "main") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=path,
        env={
            **dict(__import__("os").environ),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
        check=True,
    )


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_resolve_target_branch_uses_primary_when_requested_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="main")
    assert resolve_target_branch(repo, None) == "main"
    assert resolve_target_branch(repo, "") == "main"


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_resolve_target_branch_fixes_nonexistent_branch(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="main")
    assert resolve_target_branch(repo, "master") == "main"


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_resolve_target_branch_keeps_existing_branch(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="develop")
    assert resolve_target_branch(repo, "develop") == "develop"


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_resolve_workspace_base_branch_master_only(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="master")
    assert resolve_workspace_base_branch(repo) == "master"


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_resolve_workspace_base_branch_custom_head(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="develop")
    assert resolve_workspace_base_branch(repo) == "develop"


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_resolve_target_branch_empty_when_no_main_or_master(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="develop")
    assert resolve_target_branch(repo, "master") == ""
    assert resolve_target_branch(repo, None) == ""
    assert resolve_target_branch(repo, "develop") == "develop"


def test_normalize_agent_branch_dict_openclaw_unchanged() -> None:
    raw = {"id": "oc", "kind": "openclaw", "repo": "/tmp/x", "targetBranch": "main"}
    assert normalize_agent_branch_dict(raw) == raw


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_normalize_agent_branch_dicts_fixes_master_to_main(tmp_path: Path) -> None:
    repo = tmp_path / "wd"
    repo.mkdir()
    _init_repo(repo, branch="main")
    out = normalize_agent_branch_dicts([
        {"id": "w1", "kind": "claude", "repo": str(repo), "targetBranch": "master"},
    ])
    assert out[0]["targetBranch"] == "main"


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_normalize_flow_spec_branches(tmp_path: Path) -> None:
    repo = tmp_path / "wd"
    repo.mkdir()
    _init_repo(repo, branch="main")
    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="w1",
                kind=AgentKind.claude,
                repo=str(repo),
                target_branch="master",
            ),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="w1", subject="s", depends_on=[]),
        ],
    )
    normalize_flow_spec_branches(spec)
    assert spec.agents[0].target_branch == "main"


def test_is_clawteam_agent_branch() -> None:
    assert is_clawteam_agent_branch("clawteam/csflow-x/alice")
    assert not is_clawteam_agent_branch("main")
    assert not is_clawteam_agent_branch("feature/demo")


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_list_flow_target_branches_excludes_clawteam_refs(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="main")
    subprocess.run(["git", "branch", "clawteam/run-1/agent-a"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "feature/demo"], cwd=repo, check=True)
    all_branches = list_local_branches(repo)
    assert "clawteam/run-1/agent-a" in all_branches
    selectable = list_flow_target_branches(repo)
    assert "clawteam/run-1/agent-a" not in selectable
    assert "main" in selectable
    assert "feature/demo" in selectable


@pytest.mark.skipif(not _has_git(), reason="git not available")
def test_delete_local_branch_and_clawteam_helpers(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo, branch="main")
    agent_branch = clawteam_agent_branch_name("run-1", "agent-a")
    subprocess.run(["git", "branch", agent_branch], cwd=repo, check=True)
    assert agent_branch in list_local_branches(repo)
    assert delete_clawteam_agent_branch(repo, team="run-1", agent="agent-a")
    assert agent_branch not in list_local_branches(repo)
    subprocess.run(["git", "branch", "clawteam/run-1/agent-b"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "clawteam/run-1/agent-c"], cwd=repo, check=True)
    deleted = delete_clawteam_team_branches(repo, "run-1")
    assert "clawteam/run-1/agent-b" in deleted
    assert "clawteam/run-1/agent-c" in deleted
    assert "main" in list_local_branches(repo)
    assert delete_local_branch(repo, "does-not-exist")
