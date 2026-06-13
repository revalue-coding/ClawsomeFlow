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
    # Cross-platform: flock on Linux, mkdir spinlock fallback on macOS.
    assert "command -v flock" in cmd
    assert "flock -x " in cmd
    assert "mkdir" in cmd
    assert "git checkout main" in cmd
    assert "git pull --ff-only" in cmd
    assert "git merge --no-ff clawteam/t/a" in cmd


def test_build_flocked_baseline_merge_command_has_flat_quoting() -> None:
    """Regression: the command must NOT nest ``bash -c`` inside ``bash -c``.

    The old form double-wrapped the git steps, producing the unreadable
    ``'"'"'"'"'"'"'"'"'`` quote pyramid that agents mangled into a shell quote
    parse error. The merge message must be quoted exactly once and the command
    must be a plain compound statement (no outer ``bash -c`` wrapper)."""
    cmd = build_flocked_baseline_merge_command(
        repo_root="/tmp/repo",
        base_branch="main",
        feature_branch="clawteam/t/a",
        merge_message="csflow: scheduled merge clawteam/t/a",
    )
    # No nested-shell wrapping at all.
    assert "bash -c" not in cmd
    # The single-quote escape pyramid (more than one level of '"'"') must be gone.
    assert "'\"'\"'\"'\"'" not in cmd
    # The merge message survives as a single, plain single-quoted token.
    assert "-m 'csflow: scheduled merge clawteam/t/a'" in cmd
    # Lock is held via an fd subshell, not a nested shell command.
    assert "( flock -x 9" in cmd


def test_baseline_merge_command_works_without_flock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """macOS parity: with no `flock` on PATH the mkdir-spinlock fallback still
    performs the merge. The run-ac4ec5fbfe7b host was macOS, which ships no
    `flock`, so a bare `flock -x …` would never have run the merge."""
    import shutil
    import subprocess

    needed = ["bash", "sh", "git", "mkdir", "sleep", "rmdir"]
    resolved = {b: shutil.which(b) for b in needed}
    if any(v is None for v in resolved.values()):
        pytest.skip("missing core utilities for the merge-without-flock test")

    # A PATH that contains the essentials but NOT flock → forces the fallback.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, real in resolved.items():
        (bindir / name).symlink_to(real)  # type: ignore[arg-type]
    assert shutil.which("flock", path=str(bindir)) is None

    monkeypatch.setenv("CSFLOW_HOME", str(tmp_path / "csflow_home"))

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }

    def run(cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd, cwd=repo, env=env, shell=True,
            capture_output=True, text=True,
        )

    run("git -c init.defaultBranch=main init -q")
    (repo / "base.txt").write_text("base\n")
    run("git add -A && git commit -q -m base")
    run("git checkout -q -b feature")
    (repo / "feature.txt").write_text("feat\n")
    run("git add -A && git commit -q -m feat")
    run("git checkout -q main")

    cmd = build_flocked_baseline_merge_command(
        repo_root=str(repo),
        base_branch="main",
        feature_branch="feature",
        merge_message="csflow: test merge",
    )
    res = run(cmd)
    assert res.returncode == 0, res.stderr

    # The merge landed on main: the feature file is present on the base branch.
    assert (repo / "feature.txt").exists()
    log = run("git log --oneline main")
    assert "csflow: test merge" in log.stdout


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
