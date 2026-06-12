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

import pytest

import app.integrations.clawteam_cli as cli_mod
from app.integrations.clawteam_cli import (
    AntiLoopViolation,
    BANNED_SKILLS,
    CliInvocationError,
    ClawTeamCli,
    _ensure_repo_on_target_branch,
    _enforce_anti_loop,
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
    assert seen == [["git", "symbolic-ref", "--quiet", "--short", "HEAD"]]


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

    async def _fake_exec(*argv: str, cwd: str | None = None, env=None, stdout=None, stderr=None):
        del argv, env, stdout, stderr
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
    assert seen[-1] == ["git", "checkout", "main"]


@pytest.mark.asyncio
async def test_ensure_repo_on_target_branch_rejects_dirty_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_run_in_cwd(argv: list[str], *, cwd: str, env: dict[str, str]):
        del cwd, env
        if argv[:3] == ["git", "symbolic-ref", "--quiet"]:
            return 0, "dev\n", ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            return 0, " M a.py\n", ""
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run_in_cwd", _fake_run_in_cwd)
    with pytest.raises(CliInvocationError, match="uncommitted changes"):
        await _ensure_repo_on_target_branch(
            repo="/tmp/repo",
            target_branch="main",
            env={},
        )


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
async def test_workspace_cleanup_uses_agent_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    async def _fake_run(argv: list[str], *, env: dict[str, str]):
        del env
        seen.append(argv)
        return 0, "", ""

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
    ok = await ClawTeamCli().workspace_cleanup(team="csflow-x", agent="alice")
    assert ok is True
    assert seen == [["clawteam", "workspace", "cleanup", "csflow-x", "--agent", "alice"]]


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
        if argv[:4] == ["git", "rev-parse", "-q", "--verify"]:
            return 1, "", ""
        if argv[:2] == ["git", "checkout"]:
            return 0, "Switched", ""
        if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
            return 0, "test", ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            return 0, "", ""
        if argv[:3] == ["git", "pull", "--ff-only"]:
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
    assert seen[0][:4] == ["git", "rev-parse", "-q", "--verify"]
    assert ["git", "checkout", "test"] in seen
    assert ["git", "merge", "--no-ff", "clawteam/csflow-x/alice", "-m", "[csflow] merge clawteam/csflow-x/alice for csflow-x/alice"] in seen
    assert cleanup_calls == [("csflow-x", "alice", "/tmp/repo")]


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
        if argv[:4] == ["git", "rev-parse", "-q", "--verify"]:
            return 1, "", ""
        if argv[:2] == ["git", "checkout"]:
            return 0, "", ""
        if argv[1:3] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main", ""
        if argv[:3] == ["git", "status", "--porcelain"]:
            return 0, "", ""
        if argv[:3] == ["git", "pull", "--ff-only"]:
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
    assert cleanup_called is False


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

    monkeypatch.setattr(cli_mod, "_run", _fake_run)
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
