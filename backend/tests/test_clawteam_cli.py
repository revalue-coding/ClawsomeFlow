"""Tests for :mod:`app.integrations.clawteam_cli`.

Two tiers:
* Unit: argv builder + anti-loop enforcement (no subprocess).
* Integration: real ``clawteam`` CLI calls — gated by ``CLAWTEAM_INTEGRATION``
  env var so CI without the binary still passes.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

import app.integrations.clawteam_cli as cli_mod
from app.integrations.clawteam_cli import (
    AntiLoopViolation,
    BANNED_SKILLS,
    CliInvocationError,
    ClawTeamCli,
    _ensure_repo_on_target_branch,
    _enforce_anti_loop,
    _is_empty_git_revert_output,
    _SpawnArgs,
)
from app.user_context import set_request_user


# ──────────────────────────────────────────────────────────────────────
# Unit tests — argv shape + defences
# ──────────────────────────────────────────────────────────────────────


class TestSpawnArgvBuilder:
    def test_minimal_argv_has_no_keepalive_no_task_no_skill(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["claude"],
            team="csflow-x", agent_name="alice", repo="/tmp/r",
        )
        argv = args.to_argv()
        assert argv[:3] == ["clawteam", "spawn", "tmux"]
        # Agent command is passed after `--` so clawteam does not consume its flags.
        assert argv[-2:] == ["--", "claude"]
        assert "--no-keepalive" in argv         # gate ③
        assert "--skip-permissions" in argv
        assert "--task" not in argv             # gate ①
        # `--skill` must not appear before the `--` separator (we explicitly
        # opt-out of clawteam skills); tokens after `--` are agent argv only.
        sep = argv.index("--")
        assert "--skill" not in argv[:sep]      # gate ② (no skill at all by default)
        assert "--workspace" in argv
        assert "--repo" in argv
        assert "/tmp/r" in argv

    def test_repo_tilde_expanded_in_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # macOS regression: a leading ``~`` must be expanded so neither the
        # ``--repo`` argv nor the git branch-check cwd receives a literal tilde.
        monkeypatch.setenv("HOME", "/tmp/fakehome")
        args = _SpawnArgs(
            backend="tmux", command=["hermes"],
            team="csflow-x", agent_name="alice", repo="~/342test",
        )
        assert args.repo == "/tmp/fakehome/342test"
        argv = args.to_argv()
        assert "/tmp/fakehome/342test" in argv
        assert "~/342test" not in argv

    def test_resume_argv_uses_no_workspace(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["claude", "--continue"],
            team="csflow-x", agent_name="alice", repo="/tmp/wt",
            workspace=False,
        )
        argv = args.to_argv()
        assert "--no-workspace" in argv
        assert "--workspace" not in argv
        assert "--skip-permissions" in argv
        # `--continue` must live AFTER the `--` separator so clawteam doesn't
        # parse it as an unknown option.
        sep = argv.index("--")
        assert argv[sep + 1:] == ["claude", "--continue"]

    def test_hermes_spawn_argv_keeps_anti_loop_invariants(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["hermes"],
            team="csflow-x", agent_name="alice", repo="/tmp/r",
        )
        argv = args.to_argv()
        assert argv[:3] == ["clawteam", "spawn", "tmux"]
        assert argv[-2:] == ["--", "hermes"]
        assert "--no-keepalive" in argv         # gate ③
        assert "--skip-permissions" in argv
        assert "--task" not in argv             # gate ①
        sep = argv.index("--")
        assert "--skill" not in argv[:sep]      # gate ② (no skill at all)
        assert "--workspace" in argv

    def test_hermes_resume_argv_uses_continue_flag(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["hermes", "-c"],
            team="csflow-x", agent_name="alice", repo="/tmp/wt",
            workspace=False,
        )
        argv = args.to_argv()
        assert "--no-workspace" in argv
        assert "--workspace" not in argv
        # `-c` must live AFTER the `--` separator so clawteam doesn't try to
        # parse it as its own `-c` option.
        sep = argv.index("--")
        assert argv[sep + 1:] == ["hermes", "-c"]

    def test_skills_passed_through(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["claude"],
            team="t", agent_name="a", repo="/r",
            skills=["my-custom-skill"],
        )
        argv = args.to_argv()
        # `--skill <name>` must precede the `--` separator so clawteam consumes
        # it as its own option (NOT pass it to the agent).
        sep = argv.index("--")
        skill_idx = argv.index("--skill")
        assert skill_idx < sep
        assert argv[skill_idx:skill_idx + 2] == ["--skill", "my-custom-skill"]


class TestAntiLoopEnforcement:
    def test_banned_skill_rejected(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["claude"],
            team="t", agent_name="a", repo="/r",
            skills=["clawteam"],  # forbidden!
        )
        with pytest.raises(AntiLoopViolation, match="BANNED_SKILLS"):
            _enforce_anti_loop(args)

    def test_extra_task_flag_rejected(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["claude"],
            team="t", agent_name="a", repo="/r",
            extra_flags=["--task", "do something"],
        )
        with pytest.raises(AntiLoopViolation, match="--task is forbidden"):
            _enforce_anti_loop(args)

    def test_safe_skill_allowed(self) -> None:
        args = _SpawnArgs(
            backend="tmux", command=["claude"],
            team="t", agent_name="a", repo="/r",
            skills=["my-safe-skill"],
        )
        _enforce_anti_loop(args)  # no exception

    def test_banned_skills_set_includes_clawteam(self) -> None:
        assert "clawteam" in BANNED_SKILLS


class TestCliInvocationErrorMessage:
    def test_truncates_long_stderr(self) -> None:
        err = "x" * 10_000
        e = CliInvocationError(argv=["clawteam", "x"], exit_code=2, stderr=err)
        assert "exited 2" in str(e)
        # Sanity: the str doesn't dump the entire stderr
        assert len(str(e)) < 2000


@pytest.mark.asyncio
async def test_ensure_repo_on_target_branch_no_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        seen.append(argv)
        if argv[:3] == ["git", "symbolic-ref", "--quiet"]:
            return 0, "main\n", ""
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    current, switched = await _ensure_repo_on_target_branch(
        repo="/tmp/repo",
        target_branch="main",
        env={},
    )
    assert current == "main"
    assert switched is False
    # Already on target with a clean tree → only HEAD + status probes, no commit.
    assert seen == [
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        ["git", "status", "--porcelain"],
    ]
    assert not any(a[:2] == ["git", "commit"] for a in seen)


@pytest.mark.asyncio
async def test_run_in_cwd_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive chokepoint: a ``~`` cwd must be expanded before reaching the
    # shell-less subprocess (otherwise FileNotFoundError on macOS/Linux).
    monkeypatch.setenv("HOME", "/tmp/fakehome")
    seen: dict[str, str | None] = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def _fake_exec(*argv: str, cwd: str | None = None, env=None, stdout=None, stderr=None, **kwargs):
        del argv, env, stdout, stderr, kwargs
        seen["cwd"] = cwd
        return _FakeProc()

    monkeypatch.setattr(cli_mod.asyncio, "create_subprocess_exec", _fake_exec)
    code, _out, _err = await cli_mod._run_in_cwd(["git", "status"], cwd="~/342test", env={})
    assert code == 0
    assert seen["cwd"] == "/tmp/fakehome/342test"


@pytest.mark.asyncio
async def test_ensure_repo_on_target_branch_switches_clean_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        seen.append(argv)
        if argv[:3] == ["git", "symbolic-ref", "--quiet"]:
            return 0, "dev\n", ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            return 0, "", ""
        if argv[:3] == ["git", "show-ref", "--verify"]:
            return 0, "ok", ""
        if argv[:2] == ["git", "checkout"]:
            return 0, "Switched", ""
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    current, switched = await _ensure_repo_on_target_branch(
        repo="/tmp/repo",
        target_branch="main",
        env={},
    )
    assert current == "dev"
    assert switched is True
    assert ["git", "checkout", "main"] in seen
    # Clean tree → no auto-commit on either branch.
    assert not any(a[:2] == ["git", "commit"] for a in seen)


@pytest.mark.asyncio
async def test_ensure_repo_on_target_branch_auto_commits_dirty_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dirty main repo is auto-committed ("csflow auto commit"), then switched."""
    seen: list[list[str]] = []
    status_calls = {"n": 0}

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        seen.append(argv)
        if argv[:3] == ["git", "symbolic-ref", "--quiet"]:
            return 0, "dev\n", ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            status_calls["n"] += 1
            # Dirty before the switch, clean after the checkout.
            return (0, " M a.py\n", "") if status_calls["n"] == 1 else (0, "", "")
        if argv[:2] == ["git", "add"]:
            return 0, "", ""
        if argv[:3] == ["git", "diff", "--cached"]:
            return 1, "", ""  # rc 1 == staged changes present
        if argv[:2] == ["git", "commit"]:
            return 0, "", ""
        if argv[:3] == ["git", "show-ref", "--verify"]:
            return 0, "ok", ""
        if argv[:2] == ["git", "checkout"]:
            return 0, "Switched", ""
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    current, switched = await _ensure_repo_on_target_branch(
        repo="/tmp/repo",
        target_branch="main",
        env={},
    )
    assert current == "dev"
    assert switched is True
    assert ["git", "commit", "--no-verify", "-m", "csflow auto commit"] in seen
    assert ["git", "add", "-A"] in seen
    # Commit happens BEFORE the checkout (so the switch is never blocked).
    assert seen.index(["git", "commit", "--no-verify", "-m", "csflow auto commit"]) < seen.index(
        ["git", "checkout", "main"]
    )


@pytest.mark.asyncio
async def test_ensure_repo_on_target_branch_auto_commits_without_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenClaw-style spawn (no target branch): commit the dirty tree, no checkout."""
    seen: list[list[str]] = []

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        seen.append(argv)
        if argv[:3] == ["git", "symbolic-ref", "--quiet"]:
            return 0, "dev\n", ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            return 0, " M a.py\n", ""
        if argv[:2] == ["git", "add"]:
            return 0, "", ""
        if argv[:3] == ["git", "diff", "--cached"]:
            return 1, "", ""
        if argv[:2] == ["git", "commit"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    current, switched = await _ensure_repo_on_target_branch(
        repo="/tmp/repo",
        target_branch=None,
        env={},
    )
    assert current == "dev"
    assert switched is False
    assert ["git", "commit", "--no-verify", "-m", "csflow auto commit"] in seen
    assert not any(a[:2] == ["git", "checkout"] for a in seen)


def test_cli_env_prefers_request_scoped_user() -> None:
    from app.config import load_config

    cfg = load_config().model_copy(update={"default_user": "alice"})
    cli = ClawTeamCli(config=cfg)
    set_request_user("bob")
    try:
        env = cli._env()
    finally:
        set_request_user(None)
    assert env["CLAWTEAM_USER"] == "bob"


def test_cli_env_falls_back_to_default_user() -> None:
    from app.config import load_config

    cfg = load_config().model_copy(update={"default_user": "alice"})
    cli = ClawTeamCli(config=cfg)
    set_request_user(None)
    env = cli._env()
    assert env["CLAWTEAM_USER"] == "alice"


@pytest.mark.asyncio
async def test_workspace_list_expands_tilde_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/tmp/fakehome")
    seen: list[list[str]] = []

    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        seen.append(argv)
        return 0, '{"workspaces": []}', ""

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    await ClawTeamCli().workspace_list(team="csflow-x", repo="~/342test")
    assert seen
    assert "/tmp/fakehome/342test" in seen[0]
    assert "~/342test" not in seen[0]


@pytest.mark.asyncio
async def test_workspace_cleanup_uses_agent_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        seen.append(argv)
        return 0, "", ""

    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, team, repo
        return []

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    ok = await ClawTeamCli().workspace_cleanup(team="csflow-x", agent="alice")
    assert ok is True
    assert seen == [["clawteam", "workspace", "cleanup", "csflow-x", "--agent", "alice"]]


@pytest.mark.asyncio
async def test_workspace_cleanup_deletes_agent_branch_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=repo,
        env={
            **dict(__import__("os").environ),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
        check=True,
    )
    agent_branch = "clawteam/csflow-x/alice"
    subprocess.run(["git", "branch", agent_branch], cwd=repo, check=True)

    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        return 0, "", ""

    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, team, repo
        return [
            {
                "agent_name": "alice",
                "branch_name": agent_branch,
                "repo_root": str(repo),
            }
        ]

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    ok = await ClawTeamCli().workspace_cleanup(
        team="csflow-x",
        agent="alice",
        repo=str(repo),
    )
    assert ok is True
    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert agent_branch not in [b.strip() for b in branches]
    assert "main" in [b.strip() for b in branches]


@pytest.mark.asyncio
async def test_spawn_resume_uses_replace_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str]] = {}

    async def _fake_spawn(
        self,
        args: cli_mod._SpawnArgs,
        *,
        main_repo_for_lock: str,
        target_branch: str | None = None,
    ):
        del self, main_repo_for_lock, target_branch
        seen["argv"] = args.to_argv()
        return cli_mod.SpawnResult(
            argv=seen["argv"],
            exit_code=0,
            stdout="",
            stderr="",
            json_payload=None,
        )

    monkeypatch.setattr(ClawTeamCli, "_spawn", _fake_spawn)
    out = await ClawTeamCli().spawn_resume(
        team="csflow-x",
        agent_name="alice",
        existing_worktree="/tmp/wt",
        resume_command=["claude", "--continue"],
    )
    argv = seen["argv"]
    sep = argv.index("--")
    replace_idx = argv.index("--replace")
    assert replace_idx < sep
    assert argv[sep + 1:] == ["claude", "--continue"]
    assert out.exit_code == 0


def test_truncate_utf8() -> None:
    assert cli_mod._truncate_utf8("hello", 100) == ("hello", False)
    text, truncated = cli_mod._truncate_utf8("abcdef", 3)
    assert text == "abc"
    assert truncated is True
    # 0/negative max = no truncation.
    assert cli_mod._truncate_utf8("abc", 0) == ("abc", False)


@pytest.mark.asyncio
async def test_workspace_agent_patch_returns_committed_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, repo
        return [{
            "team_name": team,
            "agent_name": "alice",
            "repo_root": "/tmp/repo",
            "branch_name": "clawteam/csflow-x/alice",
            "base_branch": "main",
            "worktree_path": "/tmp/does-not-exist/alice",
        }]

    seen: list[list[str]] = []

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del env
        seen.append(argv)
        assert cwd == "/tmp/repo"
        if argv[:3] == ["git", "diff", "--no-color"]:
            assert argv[3] == "main...clawteam/csflow-x/alice"
            return 0, "diff --git a/x b/x\n+added\n", ""
        if argv[:2] == ["git", "rev-list"]:
            assert argv == [
                "git", "rev-list", "--left-right", "--count",
                "main...clawteam/csflow-x/alice",
            ]
            return 0, "2\t3\n", ""
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)

    result = await ClawTeamCli().workspace_agent_patch(
        team="csflow-x", agent="alice", repo="/tmp/repo",
    )
    assert result is not None
    assert result["base_branch"] == "main"
    assert result["branch"] == "clawteam/csflow-x/alice"
    assert "+added" in result["patch"]
    assert result["patch_truncated"] is False
    # Worktree path doesn't exist → uncommitted diff skipped.
    assert result["uncommitted_patch"] == ""
    # Divergence: left=base ahead, right=branch ahead.
    assert result["base_ahead"] == 2
    assert result["branch_ahead"] == 3
    # committed diff + rev-list (uncommitted skipped: worktree absent).
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_workspace_agent_patch_missing_workspace_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, team, repo
        return []

    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    result = await ClawTeamCli().workspace_agent_patch(
        team="csflow-x", agent="ghost", repo="/tmp/repo",
    )
    assert result is None


@pytest.mark.asyncio
async def test_workspace_has_uncommitted_changes_true(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        assert argv == ["git", "status", "--porcelain"]
        return 0, " M foo.py\n?? notes.md\n", ""

    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    dirty, entries = await ClawTeamCli().workspace_has_uncommitted_changes(
        worktree_path="/tmp/wt/alice",
    )
    assert dirty is True
    assert entries == [" M foo.py", "?? notes.md"]


@pytest.mark.asyncio
async def test_workspace_merge_runs_git_and_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    cleanup_calls: list[tuple[str, str, str | None]] = []

    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self
        assert team == "csflow-x"
        assert repo == "/tmp/repo"
        return [{
            "team_name": team,
            "agent_name": "alice",
            "repo_root": "/tmp/repo",
            "branch_name": "clawteam/csflow-x/alice",
            "base_branch": "main",
        }]

    async def _fake_workspace_cleanup(
        self,
        *,
        team: str,
        agent: str,
        repo: str | None = None,
    ):
        del self
        cleanup_calls.append((team, agent, repo))
        return True

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del env
        seen.append(argv)
        assert cwd == "/tmp/repo"
        if argv == ["git", "fetch", "origin", "test"]:
            return 0, "", ""
        if argv[:4] == ["git", "rev-parse", "-q", "--verify"]:
            if len(argv) > 4 and argv[4] == "MERGE_HEAD":
                return 1, "", ""
            return 1, "", ""
        if argv[:2] == ["git", "checkout"]:
            return 0, "Switched", ""
        if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
            return 0, "test", ""
        if argv == ["git", "merge", "--ff-only", "origin/test"]:
            return 0, "Already up to date.", ""
        if argv[:3] == ["git", "merge", "--no-ff"]:
            return 0, "Merge made by the 'ort' strategy.", ""
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    monkeypatch.setattr(ClawTeamCli, "workspace_cleanup", _fake_workspace_cleanup)
    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)

    ok, output = await ClawTeamCli().workspace_merge(
        team="csflow-x",
        agent="alice",
        repo="/tmp/repo",
        target="test",
    )
    assert ok is True
    assert "Merge made by the 'ort' strategy." in output
    assert seen[0] == ["git", "fetch", "origin", "test"]
    assert seen[1][:4] == ["git", "rev-parse", "-q", "--verify"]
    assert ["git", "checkout", "test"] in seen
    assert not any(argv[:3] == ["git", "status", "--porcelain"] for argv in seen)
    assert ["git", "merge", "--no-ff", "clawteam/csflow-x/alice", "-m", "[csflow] merge clawteam/csflow-x/alice for csflow-x/alice"] in seen
    assert cleanup_calls == [("csflow-x", "alice", "/tmp/repo")]


@pytest.mark.asyncio
async def test_workspace_merge_does_not_precheck_dirty_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, team, repo
        return [{
            "team_name": "csflow-x",
            "agent_name": "alice",
            "repo_root": "/tmp/repo",
            "branch_name": "clawteam/csflow-x/alice",
            "base_branch": "main",
        }]

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        seen.append(argv)
        if argv == ["git", "fetch", "origin", "main"]:
            return 0, "", ""
        if argv[:4] == ["git", "rev-parse", "-q", "--verify"]:
            if len(argv) > 4 and argv[4] == "MERGE_HEAD":
                return 1, "", ""
            return 1, "", ""
        if argv[:2] == ["git", "checkout"]:
            return 0, "", ""
        if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main", ""
        if argv == ["git", "merge", "--ff-only", "origin/main"]:
            return 0, "", ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            raise AssertionError("workspace_merge must not precheck baseline dirty state")
        if argv[:3] == ["git", "merge", "--no-ff"]:
            return 0, "ok", ""
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)

    ok, _output = await ClawTeamCli().workspace_merge(
        team="csflow-x",
        agent="alice",
        repo="/tmp/repo",
        target="main",
        cleanup=False,
    )
    assert ok is True
    assert not any(argv[:3] == ["git", "status", "--porcelain"] for argv in seen)


@pytest.mark.asyncio
async def test_workspace_merge_conflict_runs_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    cleanup_called = False

    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, team, repo
        return [{
            "team_name": "csflow-x",
            "agent_name": "alice",
            "repo_root": "/tmp/repo",
            "branch_name": "clawteam/csflow-x/alice",
            "base_branch": "main",
        }]

    async def _fake_workspace_cleanup(
        self,
        *,
        team: str,
        agent: str,
        repo: str | None = None,
    ):
        del self, team, agent, repo
        nonlocal cleanup_called
        cleanup_called = True
        return True

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        seen.append(argv)
        if argv == ["git", "fetch", "origin", "main"]:
            return 0, "", ""
        if argv[:4] == ["git", "rev-parse", "-q", "--verify"]:
            if len(argv) > 4 and argv[4] == "MERGE_HEAD":
                return 1, "", ""
            return 1, "", ""
        if argv[:2] == ["git", "checkout"]:
            return 0, "", ""
        if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main", ""
        if argv == ["git", "merge", "--ff-only", "origin/main"]:
            return 0, "", ""
        if argv[:3] == ["git", "merge", "--no-ff"] and argv[-1] != "--abort":
            return 1, "", "CONFLICT (content): Merge conflict in README.md"
        if argv == ["git", "merge", "--abort"]:
            return 0, "", ""
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    monkeypatch.setattr(ClawTeamCli, "workspace_cleanup", _fake_workspace_cleanup)
    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)

    ok, output = await ClawTeamCli().workspace_merge(
        team="csflow-x",
        agent="alice",
        repo="/tmp/repo",
        target="main",
    )
    assert ok is False
    assert "CONFLICT" in output
    assert ["git", "merge", "--abort"] in seen
    assert not any(argv[:3] == ["git", "status", "--porcelain"] for argv in seen)
    assert cleanup_called is False


@pytest.mark.asyncio
async def test_workspace_merge_fetches_before_lock_and_ff_only_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    lock_entered = False

    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, team, repo
        return [{
            "team_name": "csflow-x",
            "agent_name": "alice",
            "repo_root": "/tmp/repo",
            "branch_name": "clawteam/csflow-x/alice",
            "base_branch": "main",
        }]

    class _TrackingLock:
        async def __aenter__(self):
            nonlocal lock_entered
            lock_entered = True
            return None

        async def __aexit__(self, *args):
            return False

    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        seen.append(argv)
        if argv == ["git", "fetch", "origin", "main"]:
            assert lock_entered is False
            return 0, "", ""
        if argv[:4] == ["git", "rev-parse", "-q", "--verify"]:
            if len(argv) > 4 and argv[4] == "MERGE_HEAD":
                assert lock_entered is True
                return 1, "", ""
            return 1, "", ""
        if argv[:2] == ["git", "checkout"]:
            assert lock_entered is True
            return 0, "", ""
        if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main", ""
        if argv == ["git", "merge", "--ff-only", "origin/main"]:
            assert lock_entered is True
            return 0, "", ""
        if argv[:3] == ["git", "merge", "--no-ff"]:
            return 0, "ok", ""
        raise AssertionError(f"unexpected argv: {argv}")

    cli = ClawTeamCli()
    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    monkeypatch.setattr(
        cli._locks,
        "lock",
        lambda *args, **kwargs: _TrackingLock(),
    )

    ok, _output = await cli.workspace_merge(
        team="csflow-x",
        agent="alice",
        repo="/tmp/repo",
        target="main",
        cleanup=False,
    )
    assert ok is True
    assert seen[0] == ["git", "fetch", "origin", "main"]
    assert ["git", "merge", "--ff-only", "origin/main"] in seen


@pytest.mark.asyncio
async def test_workspace_has_uncommitted_changes_raises_on_git_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del argv, cwd, env
        return 128, "", "fatal: not a git repository"

    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    with pytest.raises(CliInvocationError, match="not a git repository"):
        await ClawTeamCli().workspace_has_uncommitted_changes(
            worktree_path="/tmp/wt/alice",
        )


@pytest.mark.asyncio
async def test_workspace_cleanup_falls_back_when_agent_flag_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        seen.append(argv)
        if len(seen) == 1:
            return 2, "", "No such option: --agent"
        return 0, "", ""

    async def _fake_workspace_list(self, *, team: str, repo: str | None = None):
        del self, team, repo
        return []

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    monkeypatch.setattr(ClawTeamCli, "workspace_list", _fake_workspace_list)
    ok = await ClawTeamCli().workspace_cleanup(team="csflow-x", agent="alice")
    assert ok is True
    assert seen == [
        ["clawteam", "workspace", "cleanup", "csflow-x", "--agent", "alice"],
        ["clawteam", "workspace", "cleanup", "csflow-x", "alice"],
    ]


@pytest.mark.asyncio
async def test_workspace_cleanup_with_diagnostics_exposes_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        return 2, "", f"failed: {' '.join(argv)}"

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    result = await ClawTeamCli().workspace_cleanup_with_diagnostics(
        team="csflow-x",
        agent="alice",
    )
    assert result.success is False
    assert len(result.attempts) == 1
    assert result.attempts[0].argv == [
        "clawteam", "workspace", "cleanup", "csflow-x", "--agent", "alice",
    ]
    assert "failed:" in result.attempts[0].stderr


@pytest.mark.asyncio
async def test_lifecycle_request_shutdown_uses_from_to_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        seen.append(argv)
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    await ClawTeamCli().lifecycle_request_shutdown(
        team="csflow-x",
        from_agent="csflow-scheduler",
        to_agent="alice",
        reason="run_finalize",
    )
    assert seen == [[
        "clawteam",
        "lifecycle",
        "request-shutdown",
        "csflow-x",
        "csflow-scheduler",
        "alice",
        "--reason",
        "run_finalize",
    ]]


@pytest.mark.asyncio
async def test_tmux_kill_agent_windows_kills_all_name_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        seen.append(argv)
        if argv[:2] == ["tmux", "list-windows"]:
            return 0, "0:web\n1:alice\n2:alice\n", ""
        if argv[:2] == ["tmux", "kill-window"]:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    killed = await ClawTeamCli().tmux_kill_agent_windows(
        team="csflow-x", agent="alice",
    )
    assert killed == 2
    assert seen[0][:3] == ["tmux", "list-windows", "-t"]


# ──────────────────────────────────────────────────────────────────────
# Integration tests — real ``clawteam`` CLI
# ──────────────────────────────────────────────────────────────────────

requires_clawteam = pytest.mark.skipif(
    not subprocess.run(
        ["which", "clawteam"], capture_output=True
    ).stdout.strip(),
    reason="clawteam CLI not installed",
)


@requires_clawteam
def test_team_spawn_team_via_real_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: spawn-team registers a team that we can re-discover via CLI."""
    import asyncio

    tmp = tempfile.mkdtemp(prefix="csflow_ct_int_")
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", tmp)

    cli = ClawTeamCli()
    team_name = "csflow-test-int"

    async def go() -> dict | None:
        return await cli.team_spawn_team(
            team=team_name, agent_name="leader-a",
            agent_type="leader", description="integration test",
        )

    payload = asyncio.run(go())
    assert payload is not None
    assert payload["team"] == team_name
    assert payload["leaderName"] == "leader-a"

    # Verify we can read it back via raw CLI
    r = subprocess.run(
        ["clawteam", "--json", "team", "discover"],
        env={**os.environ, "CLAWTEAM_DATA_DIR": tmp},
        capture_output=True, text=True, check=True,
    )
    import json
    discovered = json.loads(r.stdout)
    assert any(t["name"] == team_name for t in discovered)


# ──────────────────────────────────────────────────────────────────────
# Control-plane subprocess ceiling (_exec_capture)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_capture_times_out_and_kills_process() -> None:
    """A hung CLI subprocess is killed at the ceiling and reported as exit
    code 124 (stderr carries the reason) — the tick loop can then treat it
    as a normal failed command instead of hanging forever.

    NOTE: this ceiling applies ONLY to control-plane commands (spawn/inject/
    peek…, seconds-scale); agent task execution runs inside tmux and is never
    subject to it.
    """
    import time as _time

    started = _time.monotonic()
    code, out, err = await cli_mod._exec_capture(
        ["sh", "-c", "sleep 30"], env={**os.environ}, timeout_sec=0.3,
    )
    elapsed = _time.monotonic() - started
    assert code == cli_mod.CLI_TIMEOUT_EXIT_CODE
    assert "timed out" in err
    assert elapsed < 5.0, f"kill was not prompt: {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_exec_capture_kills_whole_process_group_on_timeout() -> None:
    """Grandchildren (sh -> sleep) must die with the group, not survive as
    orphans that keep the pipe open."""
    import tempfile as _tempfile
    import time as _time

    with _tempfile.NamedTemporaryFile(mode="r", suffix=".pid") as pidfile:
        code, _out, _err = await cli_mod._exec_capture(
            ["sh", "-c", f"sleep 30 & echo $! > {pidfile.name}; wait"],
            env={**os.environ}, timeout_sec=0.5,
        )
        assert code == cli_mod.CLI_TIMEOUT_EXIT_CODE
        pid_raw = pidfile.read().strip()
    assert pid_raw, "child pid was not recorded"
    child_pid = int(pid_raw)

    def _alive(pid: int) -> bool:
        """True only for a genuinely running process.

        A killed orphan may linger as a zombie until PID 1 reaps it (common
        inside the Docker test container) — ``os.kill(pid, 0)`` still succeeds
        on zombies, so consult ``/proc/<pid>/stat`` state ``Z`` as "dead".
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat.rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False

    # After group-kill the grandchild must be gone (allow a beat for reaping).
    deadline = _time.monotonic() + 3.0
    while _time.monotonic() < deadline:
        if not _alive(child_pid):
            break
        _time.sleep(0.1)
    else:
        os.kill(child_pid, 9)  # cleanup before failing
        pytest.fail("grandchild survived the process-group kill")


@pytest.mark.asyncio
async def test_exec_capture_cancellation_kills_process() -> None:
    """Cancelling the awaiting task (run abort) must kill the subprocess."""
    import asyncio as _asyncio
    import tempfile as _tempfile
    import time as _time

    with _tempfile.NamedTemporaryFile(mode="r", suffix=".pid") as pidfile:
        task = _asyncio.create_task(cli_mod._exec_capture(
            ["sh", "-c", f"echo $$ > {pidfile.name}; sleep 30"],
            env={**os.environ},
        ))
        # Wait until the shell has written its pid (i.e. it is running).
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline and not pidfile.read().strip():
            pidfile.seek(0)
            await _asyncio.sleep(0.05)
        pidfile.seek(0)
        shell_pid = int(pidfile.read().strip())
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task
    deadline = _time.monotonic() + 3.0
    while _time.monotonic() < deadline:
        try:
            os.kill(shell_pid, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.1)
    else:
        os.kill(shell_pid, 9)
        pytest.fail("subprocess survived task cancellation")


def test_cli_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CSFLOW_CLAWTEAM_CLI_TIMEOUT_SEC", "42.5")
    assert cli_mod._cli_timeout_seconds() == 42.5
    monkeypatch.setenv("CSFLOW_CLAWTEAM_CLI_TIMEOUT_SEC", "not-a-number")
    assert cli_mod._cli_timeout_seconds() == cli_mod.DEFAULT_CLI_TIMEOUT_SECONDS
    monkeypatch.delenv("CSFLOW_CLAWTEAM_CLI_TIMEOUT_SEC")
    assert cli_mod._cli_timeout_seconds() == cli_mod.DEFAULT_CLI_TIMEOUT_SECONDS


def test_default_cli_timeout_far_above_control_plane_latency() -> None:
    """15min default: generous for control-plane calls (normally <10s), yet
    bounded so one hung CLI can no longer freeze a run loop forever."""
    assert cli_mod.DEFAULT_CLI_TIMEOUT_SECONDS == 15 * 60.0


def test_resolve_argv_pins_clawteam_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import runtime_bins

    monkeypatch.setattr(
        runtime_bins, "resolve_binary", lambda name: f"/opt/bin/{name}",
    )
    assert cli_mod._resolve_argv(["clawteam", "team", "discover"]) == [
        "/opt/bin/clawteam", "team", "discover",
    ]
    # Non-clawteam argv (git/tmux…) is left untouched.
    assert cli_mod._resolve_argv(["git", "status"]) == ["git", "status"]


# ──────────────────────────────────────────────────────────────────────
# run_merged_agent_patch — post-run Run-diff history reconstruction
# ──────────────────────────────────────────────────────────────────────

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def _make_baseline_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _agent_merge(
    repo: Path, *, team: str, agent: str, filename: str, content: str, message: str,
) -> None:
    """Simulate one csflow agent worktree branch merged --no-ff into main."""
    branch = f"clawteam/{team}/{agent}"
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / filename).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"work by {agent}")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", branch, "-m", message)


@pytest.mark.asyncio
async def test_run_merged_agent_patch_isolates_this_agent(tmp_path: Path) -> None:
    repo = _make_baseline_repo(tmp_path)
    team = "csflow-abc12345"
    # Two agents of THIS run merge; plus an unrelated later commit on main.
    _agent_merge(
        repo, team=team, agent="alice", filename="a.txt", content="alice\n",
        message=f"[csflow] merge clawteam/{team}/alice for {team}/alice",
    )
    _agent_merge(
        repo, team=team, agent="bob", filename="b.txt", content="bob\n",
        message=f"csflow: scheduled merge clawteam/{team}/bob",
    )
    (repo / "later.txt").write_text("later unrelated commit\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated later work")

    result = await ClawTeamCli().run_merged_agent_patch(
        team=team, agent="alice", repo=str(repo),
    )
    assert result is not None
    assert result["merge_count"] == 1
    assert result["commit_count"] == 1
    assert result["files_changed"] == 1
    assert result["insertions"] == 1
    # alice's file is in the diff; bob's and the unrelated file are NOT.
    assert "a.txt" in result["patch"]
    assert "alice" in result["patch"]
    assert "b.txt" not in result["patch"]
    assert "later.txt" not in result["patch"]


@pytest.mark.asyncio
async def test_run_merged_agent_patch_other_run_same_branch_excluded(
    tmp_path: Path,
) -> None:
    """A different run (different team) touching the same baseline must not leak."""
    repo = _make_baseline_repo(tmp_path)
    _agent_merge(
        repo, team="csflow-run1aaaa", agent="alice", filename="r1.txt",
        content="run1\n",
        message="[csflow] merge clawteam/csflow-run1aaaa/alice for csflow-run1aaaa/alice",
    )
    _agent_merge(
        repo, team="csflow-run2bbbb", agent="alice", filename="r2.txt",
        content="run2\n",
        message="[csflow] merge clawteam/csflow-run2bbbb/alice for csflow-run2bbbb/alice",
    )
    result = await ClawTeamCli().run_merged_agent_patch(
        team="csflow-run2bbbb", agent="alice", repo=str(repo),
    )
    assert result is not None
    assert result["merge_count"] == 1
    assert "r2.txt" in result["patch"]
    assert "r1.txt" not in result["patch"]


@pytest.mark.asyncio
async def test_run_merged_agent_patch_prefix_collision(tmp_path: Path) -> None:
    """Agent 'worker' must not match merges of 'worker2' (token boundary)."""
    repo = _make_baseline_repo(tmp_path)
    team = "csflow-abc12345"
    _agent_merge(
        repo, team=team, agent="worker2", filename="w2.txt", content="w2\n",
        message=f"csflow: merge clawteam/{team}/worker2 after run",
    )
    result = await ClawTeamCli().run_merged_agent_patch(
        team=team, agent="worker", repo=str(repo),
    )
    assert result is not None
    assert result["merge_count"] == 0
    assert result["patch"] == ""


@pytest.mark.asyncio
async def test_run_merged_agent_patch_no_merge_returns_zero(tmp_path: Path) -> None:
    repo = _make_baseline_repo(tmp_path)
    result = await ClawTeamCli().run_merged_agent_patch(
        team="csflow-abc12345", agent="ghost", repo=str(repo),
    )
    assert result is not None
    assert result["merge_count"] == 0
    assert result["patch"] == ""


@pytest.mark.asyncio
async def test_run_merged_agent_patch_multiple_merges_concatenated(
    tmp_path: Path,
) -> None:
    """Two merges of the same branch (e.g. initial + complaint re-merge) sum up."""
    repo = _make_baseline_repo(tmp_path)
    team = "csflow-abc12345"
    branch = f"clawteam/{team}/alice"
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "one.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", branch, "-m",
         f"[csflow] merge {branch} for {team}/alice")
    # second batch on the same branch, merged again
    _git(repo, "checkout", "-q", branch)
    (repo / "two.txt").write_text("two\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", branch, "-m",
         f"csflow: merge {branch} after run")

    result = await ClawTeamCli().run_merged_agent_patch(
        team=team, agent="alice", repo=str(repo),
    )
    assert result is not None
    assert result["merge_count"] == 2
    assert result["commit_count"] == 2
    assert "one.txt" in result["patch"]
    assert "two.txt" in result["patch"]


@pytest.mark.asyncio
async def test_run_merged_agent_patch_invalid_repo_returns_none(
    tmp_path: Path,
) -> None:
    assert await ClawTeamCli().run_merged_agent_patch(
        team="csflow-x", agent="alice", repo=str(tmp_path / "nope"),
    ) is None
    assert await ClawTeamCli().run_merged_agent_patch(
        team="csflow-x", agent="alice", repo=None,
    ) is None


@pytest.mark.asyncio
async def test_run_merged_agent_patch_include_patch_false_skips_body(
    tmp_path: Path,
) -> None:
    repo = _make_baseline_repo(tmp_path)
    team = "csflow-abc12345"
    _agent_merge(
        repo, team=team, agent="alice", filename="a.txt", content="alice\n",
        message=f"[csflow] merge clawteam/{team}/alice for {team}/alice",
    )
    result = await ClawTeamCli().run_merged_agent_patch(
        team=team, agent="alice", repo=str(repo), include_patch=False,
    )
    assert result is not None
    assert result["merge_count"] == 1
    assert result["files_changed"] == 1
    assert result["patch"] == ""


def test_is_empty_git_revert_output_detects_already_undone() -> None:
    assert _is_empty_git_revert_output(
        "Auto-merging docs/grassland_destinations_research.md\n"
        "On branch main\nnothing to commit, working tree clean\n",
    )
    assert not _is_empty_git_revert_output(
        "CONFLICT (content): Merge conflict in docs/x.md\n",
    )


@pytest.mark.asyncio
async def test_revert_agent_merges_treats_empty_rerevert_as_success(
    tmp_path: Path,
) -> None:
    """Second 撤销合入 after a clean first revert must not look like a conflict."""
    repo = _make_baseline_repo(tmp_path)
    team = "csflow-abc12345"
    agent = "alice"
    _agent_merge(
        repo, team=team, agent=agent, filename="a.txt", content="alice\n",
        message=f"csflow: unattended merge clawteam/{team}/{agent}",
    )
    cli = ClawTeamCli()
    first = await cli.revert_agent_merges(
        team=team, agent=agent, repo=str(repo), target_branch="main",
    )
    assert first["ok"] is True
    assert first["nothing_to_revert"] is False
    assert first["merge_shas"]

    second = await cli.revert_agent_merges(
        team=team, agent=agent, repo=str(repo), target_branch="main",
    )
    assert second["ok"] is True
    assert second["nothing_to_revert"] is True
    assert "already reverted" in second["message"]
