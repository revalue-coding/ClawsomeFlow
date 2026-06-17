"""Tests for the WorkerSession state machine.

We exercise the base class with a tiny stub session that implements the
abstract hooks as no-ops, so we can drive every transition path.
"""

from __future__ import annotations

import asyncio

import pytest

import app.scheduler.sessions.tmux_live as tmux_live_mod
from app.models import AgentKind, FlowAgent, MergeStrategy, OnFailure
from app.scheduler.sessions.base import (
    DispatchOutcome,
    InvalidStateTransition,
    SessionState,
    WorkerSession,
)
from app.scheduler.sessions.tmux_live import TmuxLiveSession
from app.worktree.lookup import WorktreeInfo


class _StubSession(WorkerSession):
    def __init__(self, *, agent: FlowAgent, fail_spawn: bool = False,
                 fail_dispatch: bool = False) -> None:
        super().__init__(agent=agent, team_name="csflow-x", run_id="run-x")
        self.fail_spawn = fail_spawn
        self.fail_dispatch = fail_dispatch
        self.spawn_calls = 0
        self.dispatch_calls = 0
        self.resume_calls = 0
        self.shutdown_calls = 0

    async def _do_spawn(self) -> None:
        self.spawn_calls += 1
        if self.fail_spawn:
            raise RuntimeError("spawn boom")

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        self.dispatch_calls += 1
        if self.fail_dispatch:
            raise RuntimeError("dispatch boom")

    async def _do_resume(self) -> None:
        self.resume_calls += 1

    async def _do_shutdown(self) -> None:
        self.shutdown_calls += 1


def _agent() -> FlowAgent:
    return FlowAgent(
        id="alice", kind=AgentKind.claude, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )


@pytest.mark.asyncio
async def test_initial_state_is_absent() -> None:
    s = _StubSession(agent=_agent())
    assert s.state == SessionState.Absent


@pytest.mark.asyncio
async def test_spawn_transitions_absent_to_idle() -> None:
    s = _StubSession(agent=_agent())
    await s.spawn()
    assert s.state == SessionState.Idle
    assert s.spawn_attempts == 1


@pytest.mark.asyncio
async def test_spawn_failure_transitions_to_crashed() -> None:
    s = _StubSession(agent=_agent(), fail_spawn=True)
    with pytest.raises(RuntimeError):
        await s.spawn()
    assert s.state == SessionState.Crashed


@pytest.mark.asyncio
async def test_dispatch_idle_to_busy() -> None:
    s = _StubSession(agent=_agent())
    await s.spawn()
    out = await s.dispatch(task_id="t1", message="hello")
    assert out.success is True
    assert s.state == SessionState.Busy


@pytest.mark.asyncio
async def test_dispatch_failure_returns_to_idle() -> None:
    s = _StubSession(agent=_agent(), fail_dispatch=True)
    await s.spawn()
    out = await s.dispatch(task_id="t1", message="x")
    assert out.success is False
    assert s.state == SessionState.Idle


@pytest.mark.asyncio
async def test_dispatch_from_busy_is_illegal() -> None:
    s = _StubSession(agent=_agent())
    await s.spawn()
    await s.dispatch(task_id="t1", message="x")
    with pytest.raises(InvalidStateTransition):
        await s.dispatch(task_id="t2", message="x")


@pytest.mark.asyncio
async def test_mark_idle_releases_busy() -> None:
    s = _StubSession(agent=_agent())
    await s.spawn()
    await s.dispatch(task_id="t1", message="x")
    s.mark_idle()
    assert s.state == SessionState.Idle


@pytest.mark.asyncio
async def test_mark_crashed_then_resume() -> None:
    s = _StubSession(agent=_agent())
    await s.spawn()
    s.mark_crashed()
    assert s.state == SessionState.Crashed
    await s.resume()
    assert s.state == SessionState.Idle
    assert s.resume_calls == 1


@pytest.mark.asyncio
async def test_resume_from_idle_is_illegal() -> None:
    s = _StubSession(agent=_agent())
    await s.spawn()
    with pytest.raises(InvalidStateTransition):
        await s.resume()


@pytest.mark.asyncio
async def test_shutdown_terminal_idempotent() -> None:
    s = _StubSession(agent=_agent())
    await s.spawn()
    await s.shutdown()
    assert s.state == SessionState.Exited
    await s.shutdown()  # second call should not raise
    assert s.shutdown_calls == 1


@pytest.mark.asyncio
async def test_concurrent_spawn_serialised() -> None:
    """Two concurrent spawn() calls must serialise (lock prevents double-init)."""
    s = _StubSession(agent=_agent())
    # First call succeeds (Absent → Idle); second tries Idle → Spawning, which
    # IS legal per the table (rare re-spawn). Ensure both complete cleanly.
    await asyncio.gather(s.spawn(), s.spawn())
    assert s.spawn_attempts == 2
    assert s.state == SessionState.Idle


@pytest.mark.asyncio
async def test_tmux_target_naming() -> None:
    s = _StubSession(agent=_agent())
    assert s.tmux_target == "clawteam-csflow-x:alice"


def test_tmux_live_session_registers_hermes_with_continue_flag() -> None:
    """Hermes must be accepted by TmuxLiveSession with `--yolo` + `-c`
    in its (spawn, resume) command pair."""
    from app.scheduler.sessions.tmux_live import TmuxLiveSession

    hermes_agent = FlowAgent(
        id="alice", kind=AgentKind.hermes, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )
    session = TmuxLiveSession(
        agent=hermes_agent, team_name="csflow-x", run_id="run-x",
    )
    # Hermes binds its managed profile via `-p <agent.id>` on both spawn+resume
    # (profile name == FlowAgent.id).
    assert session._spawn_cmd == ["hermes", "--yolo", "-p", "alice"]
    assert session._resume_cmd == ["hermes", "--yolo", "-c", "-p", "alice"]


def test_tmux_live_session_temporary_hermes_skips_profile_binding() -> None:
    """A temporary (ad-hoc) Hermes agent has no managed profile → no `-p`
    binding on spawn/resume and no ClawTeam runtime profile applied."""
    from app.scheduler.sessions.tmux_live import TmuxLiveSession

    temp_hermes = FlowAgent(
        id="adhoc", kind=AgentKind.hermes, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2, is_temporary=True,
    )
    session = TmuxLiveSession(
        agent=temp_hermes, team_name="csflow-x", run_id="run-x",
    )
    assert session._spawn_cmd == ["hermes", "--yolo"]
    assert session._resume_cmd == ["hermes", "--yolo", "-c"]
    assert session._resolve_profile() is None


def test_tmux_live_session_temporary_claude_skips_managed_profile() -> None:
    """A temporary claude agent must not trigger managed-profile creation."""
    from app.scheduler.sessions.tmux_live import TmuxLiveSession

    temp_claude = FlowAgent(
        id="adhoc-claude", kind=AgentKind.claude, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2, is_temporary=True,
    )
    session = TmuxLiveSession(
        agent=temp_claude, team_name="csflow-x", run_id="run-x",
    )
    # No ClawTeam runtime profile for temporary agents (would otherwise be the
    # config-home env profile ``csflow-claude-adhoc-claude``).
    assert session._resolve_profile() is None


def test_tmux_live_session_registers_claude_with_bypass_permissions_flags() -> None:
    from app.scheduler.sessions.tmux_live import TmuxLiveSession

    claude_agent = FlowAgent(
        id="alice", kind=AgentKind.claude, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )
    session = TmuxLiveSession(
        agent=claude_agent, team_name="csflow-x", run_id="run-x",
    )
    # NOTE: --dangerously-skip-permissions is injected by ClawTeam's
    # DirectCliAdapter under --skip-permissions (non-root); we must NOT repeat
    # it here or it appears twice in the spawned argv.
    assert session._spawn_cmd == [
        "claude", "--permission-mode", "bypassPermissions",
    ]
    assert session._resume_cmd == [
        "claude", "--permission-mode", "bypassPermissions", "--continue",
    ]


def test_tmux_live_session_registers_codex_without_duplicate_bypass_flag() -> None:
    from app.scheduler.sessions.tmux_live import TmuxLiveSession

    codex_agent = FlowAgent(
        id="alice", kind=AgentKind.codex, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )
    session = TmuxLiveSession(
        agent=codex_agent, team_name="csflow-x", run_id="run-x",
    )
    # --dangerously-bypass-approvals-and-sandbox is injected by ClawTeam's
    # DirectCliAdapter under --skip-permissions. Repeating it here made codex's
    # clap parser reject the spawn ("cannot be used multiple times"), so the
    # command map must NOT carry it.
    #
    # The `-c` overrides (a) silence codex's startup model-migration / NUX
    # prompts so the unattended TUI reaches the composer instead of blocking on
    # an interactive menu (session_prewarm_failed), and (b) disable paste-burst
    # so ClawTeam's "paste-buffer + Enter" dispatch actually submits the prompt
    # instead of leaving it unsent in the composer.
    ov = [
        "-c", "notice.model_migrations={}",
        "-c", "tui.model_availability_nux={}",
        "-c", "disable_paste_burst=true",
    ]
    assert session._spawn_cmd == ["codex", *ov]
    assert "--dangerously-bypass-approvals-and-sandbox" not in session._spawn_cmd
    assert session._resume_cmd == ["codex", *ov, "resume", "--last"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in session._resume_cmd


def test_tmux_live_session_registers_cursor_with_force_flags() -> None:
    cursor_agent = FlowAgent(
        id="alice", kind=AgentKind.cursor, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )
    session = TmuxLiveSession(
        agent=cursor_agent, team_name="csflow-x", run_id="run-x",
    )
    assert session._spawn_cmd == [
        "agent", "--force", "--approve-mcps", "--sandbox", "disabled",
    ]
    assert session._resume_cmd == [
        "agent", "--force", "--approve-mcps", "--sandbox", "disabled", "--continue",
    ]


class _ResumeCliStub:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    async def spawn_resume(
        self,
        *,
        team: str,
        agent_name: str,
        existing_worktree: str,
        resume_command: list[str],
        profile: str | None,
        skills: tuple[str, ...],
        skip_permissions: bool = True,
    ) -> None:
        del team, agent_name, existing_worktree, profile, skills, skip_permissions
        self.commands.append(list(resume_command))


@pytest.mark.asyncio
async def test_tmux_live_resume_falls_back_to_fresh_cli_when_continue_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent()
    cli = _ResumeCliStub()
    session = TmuxLiveSession(
        agent=agent,
        team_name="csflow-x",
        run_id="run-x",
        cli=cli,  # type: ignore[arg-type]
    )
    session.worktree = WorktreeInfo(
        agent_name="alice",
        branch_name="clawteam/csflow-x/alice",
        worktree_path="/tmp/wt-alice",
        repo_root="/tmp/repo",
        base_branch="main",
    )
    readiness = iter([False, True])

    async def _fake_wait(_target: str, *, timeout_sec: float) -> bool:
        del timeout_sec
        return next(readiness)

    async def _fake_capture(_target: str, *, history_lines: int = 120) -> str:
        del history_lines
        return "No conversation found to continue"

    monkeypatch.setattr(tmux_live_mod, "wait_tui_ready", _fake_wait)
    monkeypatch.setattr(tmux_live_mod, "tmux_capture_pane", _fake_capture)

    await session._do_resume()

    assert cli.commands[0] == session._resume_cmd
    assert cli.commands[1] == session._spawn_cmd
    assert len(cli.commands) == 2


@pytest.mark.asyncio
async def test_tmux_live_resume_raises_when_primary_and_fallback_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent()
    cli = _ResumeCliStub()
    session = TmuxLiveSession(
        agent=agent,
        team_name="csflow-x",
        run_id="run-x",
        cli=cli,  # type: ignore[arg-type]
    )
    session.worktree = WorktreeInfo(
        agent_name="alice",
        branch_name="clawteam/csflow-x/alice",
        worktree_path="/tmp/wt-alice",
        repo_root="/tmp/repo",
        base_branch="main",
    )

    async def _fake_wait(_target: str, *, timeout_sec: float) -> bool:
        del timeout_sec
        return False

    async def _fake_capture(_target: str, *, history_lines: int = 120) -> str:
        del history_lines
        return "No conversation found to continue"

    monkeypatch.setattr(tmux_live_mod, "wait_tui_ready", _fake_wait)
    monkeypatch.setattr(tmux_live_mod, "tmux_capture_pane", _fake_capture)

    with pytest.raises(RuntimeError, match="fallback fresh spawn also failed"):
        await session._do_resume()
    assert cli.commands[0] == session._resume_cmd
    assert cli.commands[1] == session._spawn_cmd
