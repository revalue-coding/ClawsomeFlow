"""Tests for cross-process main-repo merge lock helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.repo_merge_lock import (
    build_flocked_baseline_merge_command,
    main_repo_lock_path,
    self_merge_instruction,
)


def test_main_repo_lock_path_is_stable_for_same_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CSFLOW_HOME", str(tmp_path))
    p1 = main_repo_lock_path("/tmp/repo")
    p2 = main_repo_lock_path("/tmp/repo")
    assert p1 == p2
    assert p1.parent.name == "clawteam_main_repo"
    assert p1.suffix == ".lock"


def test_main_repo_lock_path_expands_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CSFLOW_HOME", str(tmp_path))
    home = Path.home()
    assert main_repo_lock_path("~/proj") == main_repo_lock_path(str(home / "proj"))


def test_build_flocked_baseline_merge_command_wraps_git_steps() -> None:
    cmd = build_flocked_baseline_merge_command(
        repo_root="/tmp/repo",
        base_branch="main",
        feature_branch="clawteam/t/a",
        merge_message="csflow: scheduled merge clawteam/t/a",
    )
    assert cmd.startswith("flock -x ")
    assert "bash -c" in cmd
    assert "git checkout main" in cmd
    assert "git pull --ff-only" in cmd
    assert "git merge --no-ff clawteam/t/a" in cmd


def test_self_merge_instruction_is_concise() -> None:
    text = self_merge_instruction(
        repo_root="/tmp/repo",
        base_branch="main",
        feature_branch="clawteam/t/a",
        merge_message="csflow: scheduled merge clawteam/t/a",
    )
    assert "flock -x" in text
    assert "resolve conflicts yourself (no lock)" in text
    assert "re-run the locked command" in text
