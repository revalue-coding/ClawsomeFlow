"""Tests for cross-process main-repo merge lock helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.repo_merge_lock import (
    build_flocked_baseline_merge_command,
    build_generic_locked_merge_command,
    main_repo_file_lock,
    main_repo_lock_path,
    merge_script_path,
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


def test_build_baseline_merge_command_invokes_locked_merge_tool() -> None:
    cmd = build_flocked_baseline_merge_command(
        repo_root="/tmp/repo",
        base_branch="main",
        feature_branch="clawteam/t/a",
        merge_message="csflow: scheduled merge clawteam/t/a",
    )
    # The agent only invokes the fixed tool with plain argv (path + 4 args) —
    # no inline locking/git apparatus to mangle on relay.
    assert "python3 " in cmd
    assert "csflow-locked-merge.py" in cmd
    # Order is <repo> <feature(src)> <base(dst)> <message>.
    assert "/tmp/repo clawteam/t/a main " in cmd
    # Message is quoted exactly once; no nested-shell / quote pyramid.
    assert "'csflow: scheduled merge clawteam/t/a'" in cmd
    assert "bash -c" not in cmd
    assert "'\"'\"'\"'\"'" not in cmd
    # No leftover inline flock / python -c locking in the agent-facing command.
    assert "flock" not in cmd
    assert "python3 -c" not in cmd


def test_build_generic_merge_command_invokes_tool_with_placeholders() -> None:
    cmd = build_generic_locked_merge_command()
    assert "csflow-locked-merge.py" in cmd
    assert "<abs-repo>" in cmd and "<source-branch>" in cmd and "<dest-branch>" in cmd
    assert "flock" not in cmd


def test_locked_merge_tool_performs_merge_under_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Real end-to-end: the deployed tool flocks the SAME lock file as the
    scheduler and merges src into dst."""
    import subprocess

    monkeypatch.setenv("CSFLOW_HOME", str(tmp_path / "csflow_home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": __import__("os").environ["PATH"],
        "CSFLOW_HOME": str(tmp_path / "csflow_home"),
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }

    def run(cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd, cwd=repo, env=env, shell=True, capture_output=True, text=True,
        )

    run("git -c init.defaultBranch=main init -q")
    (repo / "base.txt").write_text("base\n")
    run("git add -A && git commit -q -m base")
    run("git checkout -q -b feature")
    (repo / "feature.txt").write_text("feat\n")
    run("git add -A && git commit -q -m feat")
    run("git checkout -q main")

    # Resolve the repo-checkout source path of the tool (not the deployed copy).
    from app.integrations.openclaw_agent_source import bundled_agent_tools_source_dir

    tool = bundled_agent_tools_source_dir() / "scripts" / "git" / "csflow-locked-merge.py"
    res = run(
        f"python3 {tool} {repo} feature main 'csflow: test merge'"
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "result=success" in res.stdout
    # Merge landed on main, and the lock file path matches the Python helper.
    assert (repo / "feature.txt").exists()
    assert main_repo_lock_path(str(repo)).exists()


def test_main_repo_file_lock_times_out_when_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The cross-process file lock is bounded: a contended acquire raises
    TimeoutError instead of blocking forever (the old behaviour)."""
    import threading

    monkeypatch.setenv("CSFLOW_HOME", str(tmp_path))
    repo = "/tmp/repo"
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with main_repo_file_lock(repo, timeout=5.0):
            held.set()
            release.wait(5.0)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(5.0)
        with pytest.raises(TimeoutError):
            with main_repo_file_lock(repo, timeout=0.5):
                pass
    finally:
        release.set()
        t.join(5.0)

    # Once released, the lock is acquirable again (no leaked/stale state).
    with main_repo_file_lock(repo, timeout=5.0):
        pass


def test_self_merge_instruction_is_concise() -> None:
    text = self_merge_instruction(
        repo_root="/tmp/repo",
        base_branch="main",
        feature_branch="clawteam/t/a",
        merge_message="csflow: scheduled merge clawteam/t/a",
    )
    assert "csflow-locked-merge.py" in text
    assert "first priority" in text
    assert "re-run the SAME command" in text
    assert "Do not manually `git pull`" in text
    assert "run ONLY the locked-merge command" in text


def test_merge_lock_reference_discourages_manual_baseline_git() -> None:
    from app.repo_merge_lock import merge_lock_reference

    text = merge_lock_reference()
    assert "Do not manually `git pull`" in text
    assert "run ONLY the locked-merge command" in text
