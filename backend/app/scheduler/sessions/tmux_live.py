"""TmuxLiveSession — TUI-CLI agents (claude / codex / cursor / ...).

Spawn:
    clawteam spawn tmux <cli> --workspace --repo <Flow.repo> --no-keepalive

Dispatch (live):
    clawteam runtime inject <team> <agent> --summary "<message>"
    (uses tmux paste-buffer + Enter inside ClawTeam)

Resume re-spawn (after crash):
    clawteam spawn tmux <cli + native --continue/--resume flags>
        --no-workspace --repo <existing_worktree> --no-keepalive

Shutdown:
    clawteam lifecycle request-shutdown <team> <agent> --reason "..."

The ``--workspace`` argument is what causes ClawTeam to create the git
worktree at ``~/.clawteam/workspaces/{team}/{agent}/``; resume must NOT
pass it (otherwise ClawTeam recreates the worktree, dropping all work).
"""

from __future__ import annotations

import asyncio

from app.integrations.clawteam_cli import (
    ClawTeamCli,
    CliInvocationError,
    get_clawteam_cli,
)
from app.logging_setup import get_logger
from app.models import AgentKind, FlowAgent
from app.scheduler.sessions.base import WorkerSession
from app.scheduler.sessions.tmux_ready import tmux_capture_pane, wait_tui_ready

logger = get_logger("scheduler.sessions.tmux_live")


# Map AgentKind → (binary command, native --resume command).
# Adjust as new TUI-CLI agents land in ClawTeam's NativeCliAdapter coverage.
_KIND_TO_CMD: dict[AgentKind, tuple[list[str], list[str]]] = {
    AgentKind.claude:   (
        ["claude", "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"],
        [
            "claude", "--permission-mode", "bypassPermissions",
            "--dangerously-skip-permissions", "--continue",
        ],
    ),
    AgentKind.codex:    (
        ["codex", "--dangerously-bypass-approvals-and-sandbox"],
        ["codex", "--dangerously-bypass-approvals-and-sandbox", "resume", "--last"],
    ),
    AgentKind.cursor:   (
        ["agent", "--force", "--approve-mcps", "--sandbox", "disabled"],
        ["agent", "--force", "--approve-mcps", "--sandbox", "disabled", "--continue"],
    ),
    AgentKind.gemini:   (["gemini"],   ["gemini", "--continue"]),
    AgentKind.kimi:     (["kimi"],     ["kimi", "--continue"]),
    AgentKind.qwen:     (["qwen"],     ["qwen", "--continue"]),
    AgentKind.opencode: (["opencode"], ["opencode", "--continue"]),
    AgentKind.pi:       (["pi"],       ["pi", "--continue"]),
    AgentKind.nanobot:  (["nanobot"],  ["nanobot", "--continue"]),
    AgentKind.hermes:   (["hermes", "--yolo"],   ["hermes", "--yolo", "-c"]),
}

class UnsupportedAgentKind(Exception):
    """Raised when this session class can't host the requested agent kind."""


class TmuxLiveSession(WorkerSession):
    """Persistent tmux + native CLI session, dispatched via runtime inject."""

    def __init__(
        self,
        *,
        agent: FlowAgent,
        team_name: str,
        run_id: str,
        cli: ClawTeamCli | None = None,
        ready_timeout_sec: float = 30.0,
    ) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        if agent.kind not in _KIND_TO_CMD and agent.kind != AgentKind.custom:
            raise UnsupportedAgentKind(
                f"agent {agent.id!r}: kind={agent.kind.value} not supported by TmuxLiveSession"
            )
        if agent.kind == AgentKind.custom and not agent.command:
            raise UnsupportedAgentKind(
                f"agent {agent.id!r}: kind=custom requires explicit 'command'"
            )
        self._cli = cli or get_clawteam_cli()
        self._ready_timeout = ready_timeout_sec
        if agent.kind == AgentKind.custom:
            # For custom, fresh==resume==agent.command (caller knows the binary).
            self._spawn_cmd = list(agent.command or [])
            self._resume_cmd = list(agent.command or [])
        else:
            self._spawn_cmd, self._resume_cmd = _KIND_TO_CMD[agent.kind]

    # ── concrete state-machine actions ───────────────────────────────

    async def _do_spawn(self) -> None:
        if not self.agent.repo:
            # Defence in depth — Flow validators forbid this for non-OpenClaw,
            # but we re-check so a misconfigured caller fails loudly here.
            raise ValueError(
                f"agent {self.agent.id!r}: spawn requires 'repo' to be set"
            )
        await self._cli.spawn_fresh(
            team=self.team_name,
            agent_name=self.agent.id,
            repo=self.agent.repo,
            target_branch=self.agent.target_branch,
            command=self._spawn_cmd,
            profile=self.agent.profile,
            skills=(),  # explicitly NO skills (no clawteam, no opt-ins)
        )
        # Wait for the CLI to finish booting before any dispatch.
        ok = await wait_tui_ready(self.tmux_target, timeout_sec=self._ready_timeout)
        if not ok:
            raise CliInvocationError(
                argv=["wait_tui_ready"], exit_code=1,
                stderr=f"TUI prompt never appeared in {self._ready_timeout}s "
                       f"on tmux pane {self.tmux_target}",
            )

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        await self._cli.runtime_inject(
            team=self.team_name,
            agent=self.agent.id,
            summary=message,
            source="csflow-scheduler",
            channel="task-dispatch",
        )

    async def _do_resume(self) -> None:
        if self.worktree is None:
            # Without a known worktree path we can't safely resume; surface
            # immediately so the failure handler escalates instead of looping.
            raise RuntimeError(
                f"agent {self.agent.id!r}: cannot resume without recorded worktree"
            )
        resume_error: Exception | None = None
        try:
            await self._spawn_resume_in_existing_worktree(
                command=self._resume_cmd,
                phase="resume",
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            resume_error = exc
            logger.warning(
                "session_resume_primary_failed",
                team=self.team_name,
                agent_id=self.agent.id,
                error=str(exc),
            )

        # Fallback: if native `--continue/--resume` has no conversation state,
        # keep the same worktree but relaunch without resume flags so the run
        # can proceed instead of hard-failing the whole controller loop.
        try:
            await self._spawn_resume_in_existing_worktree(
                command=self._spawn_cmd,
                phase="resume_fallback_fresh_cli",
            )
            logger.info(
                "session_resume_fallback_succeeded",
                team=self.team_name,
                agent_id=self.agent.id,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as fallback_exc:
            raise RuntimeError(
                "resume command failed and fallback fresh spawn also failed: "
                f"resume_error={resume_error}; fallback_error={fallback_exc}"
            ) from fallback_exc

    async def _spawn_resume_in_existing_worktree(
        self,
        *,
        command: list[str],
        phase: str,
    ) -> None:
        if self.worktree is None:
            raise RuntimeError(
                f"agent {self.agent.id!r}: cannot resume without recorded worktree"
            )
        await self._cli.spawn_resume(
            team=self.team_name,
            agent_name=self.agent.id,
            existing_worktree=self.worktree.worktree_path,
            resume_command=command,
            profile=self.agent.profile,
            skills=(),
        )
        ok = await wait_tui_ready(self.tmux_target, timeout_sec=self._ready_timeout)
        if ok:
            return
        pane_text = await tmux_capture_pane(self.tmux_target, history_lines=120)
        stderr = (
            f"TUI prompt never appeared after {phase} on {self.tmux_target}"
        )
        if pane_text.strip():
            stderr += f"; last_pane_tail={pane_text[-300:]}"
        raise CliInvocationError(
            argv=["wait_tui_ready", self.tmux_target, phase],
            exit_code=1,
            stderr=stderr,
            stdout=pane_text[-2000:],
        )

    async def _do_shutdown(self) -> None:
        try:
            await self._cli.lifecycle_request_shutdown(
                team=self.team_name,
                from_agent="csflow-scheduler",
                to_agent=self.agent.id,
                reason="run_finalize",
            )
        except CliInvocationError:
            # Best-effort: agent may already be dead.
            logger.debug(
                "lifecycle_shutdown_no_op",
                agent_id=self.agent.id, team=self.team_name,
            )
        killed = await self._cli.tmux_kill_agent_windows(
            team=self.team_name, agent=self.agent.id,
        )
        if killed > 0:
            logger.info(
                "tmux_windows_force_closed",
                team=self.team_name,
                agent_id=self.agent.id,
                window_count=killed,
            )


__all__ = ["TmuxLiveSession", "UnsupportedAgentKind"]
