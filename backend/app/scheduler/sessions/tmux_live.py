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
from app.scheduler.sessions.tmux_ready import (
    resolve_trust_platform,
    tmux_capture_pane,
    wait_tui_ready,
)

logger = get_logger("scheduler.sessions.tmux_live")


# Per-process `-c` config overrides applied to every codex spawn so the
# unattended TUI behaves. We override at spawn time rather than editing the
# user's ~/.codex/config.toml (which lives outside ClawsomeFlow's data home, and
# which codex may rewrite). The configured model is unchanged. Three fixes:
#
#  1/2. notice.model_migrations / tui.model_availability_nux → empty TOML tables.
#       Otherwise codex blocks on startup onboarding ("Choose how you'd like
#       Codex to proceed: 1. Try new model  2. Use existing model" / a model-
#       availability tip). Nobody answers the menu, the composer never appears,
#       and `wait_tui_ready` times out → session_prewarm_failed.
#
#  3.   disable_paste_burst=true. ClawTeam dispatches by pasting the prompt into
#       the pane (`tmux paste-buffer`) then sending a bare Enter (`send-keys
#       Enter`). With codex's default paste-burst heuristic ON, that trailing
#       `\r` is treated as part of the just-pasted burst (a literal newline) and
#       NOT as submit — so the prompt sits unsent in the composer and re-dispatch
#       ticks pile up as multiple "[Pasted Content N chars]" chips, stalling the
#       run. Disabling paste-burst makes the injected Enter submit. The pasted
#       prompt's own embedded newlines stay intact (bracketed paste keeps the
#       block atomic), so multi-line dispatch prompts are NOT submitted early.
#       Verified empirically against codex 0.136 (a 2s Enter delay does NOT help;
#       only disabling paste-burst does).
_CODEX_TUI_OVERRIDES: tuple[str, ...] = (
    "-c", "notice.model_migrations={}",
    "-c", "tui.model_availability_nux={}",
    "-c", "disable_paste_burst=true",
)


# Map AgentKind → (fresh-spawn command, native --resume command).
#
# Two permission-flag strategies coexist here (see _SELF_PERMISSION_KINDS):
#
# (A) ClawTeam-injected (claude/codex): we pass ``--skip-permissions`` and let
#     ClawTeam's NativeCliAdapter append the bypass flag (claude →
#     --dangerously-skip-permissions only when NOT root; codex →
#     --dangerously-bypass-approvals-and-sandbox). We therefore must NOT repeat
#     those here (codex's clap parser hard-errors on a duplicate). We only carry
#     flags ClawTeam does NOT inject (claude → --permission-mode bypassPermissions,
#     which is also root-safe).
#
# (B) Self-controlled (cursor/hermes + the five temporary CLIs below): we carry
#     the EXACT permission flag ourselves and pass ``--no-skip-permissions`` so
#     ClawTeam injects nothing. Required because ClawTeam's ``--yolo`` injection
#     is wrong for current CLI versions (verified against installed binaries):
#       gemini 0.46 → ``--yolo`` and ``--approval-mode`` HARD-CONFLICT, so we use
#                     the non-deprecated ``--approval-mode yolo``; resume is
#                     ``--resume latest`` (gemini has NO ``--continue``).
#       qwen 0.18   → gemini fork: ``--approval-mode yolo``; ``--continue`` only
#                     works when ``--chat-recording`` was set at spawn, so we add
#                     it to both fresh and resume.
#       kimi 1.47   → ``--yolo``; resume ``--continue``.
#       opencode 1.17 → has NO ``--yolo`` (rejects unknown top-level flags → spawn
#                     would exit 1); interactive auto-approval is config-only
#                     (``"permission": "allow"`` written by opencode_config), so we
#                     carry no flag. Resume ``--continue``.
#       nanobot     → ``nanobot agent``; no permission flag (auto-executes). No
#                     ``--continue``; resumed via a stable ``-s`` (injected per
#                     agent in __init__) which also isolates each agent's session
#                     (the default ``cli:direct`` would collide across agents).
_KIND_TO_CMD: dict[AgentKind, tuple[list[str], list[str]]] = {
    AgentKind.claude:   (
        ["claude", "--permission-mode", "bypassPermissions"],
        ["claude", "--permission-mode", "bypassPermissions", "--continue"],
    ),
    AgentKind.codex:    (
        ["codex", *_CODEX_TUI_OVERRIDES],
        ["codex", *_CODEX_TUI_OVERRIDES, "resume", "--last"],
    ),
    AgentKind.cursor:   (
        ["agent", "--force", "--approve-mcps", "--sandbox", "disabled"],
        ["agent", "--force", "--approve-mcps", "--sandbox", "disabled", "--continue"],
    ),
    AgentKind.gemini:   (
        ["gemini", "--approval-mode", "yolo"],
        ["gemini", "--approval-mode", "yolo", "--resume", "latest"],
    ),
    AgentKind.qwen:     (
        ["qwen", "--approval-mode", "yolo", "--chat-recording"],
        ["qwen", "--approval-mode", "yolo", "--chat-recording", "--continue"],
    ),
    AgentKind.kimi:     (["kimi", "--yolo"], ["kimi", "--yolo", "--continue"]),
    AgentKind.opencode: (["opencode"], ["opencode", "--continue"]),
    AgentKind.pi:       (["pi"],       ["pi", "--continue"]),
    # nanobot: runtime mapping kept ready, but the platform is temporarily NOT
    # exposed to users (absent from the Flow editor + AI decomposer + deps probe).
    AgentKind.nanobot:  (["nanobot", "agent"], ["nanobot", "agent"]),
    # qoder (binary `qodercli`) and codebuddy are Claude-style clones: interactive
    # tool approval via `--permission-mode bypass_permissions/bypassPermissions`
    # (verified vs installed binaries), resume via `--continue`. Neither is in
    # ClawTeam's adapter, so we self-control flags (skip_permissions=False below).
    # NOTE the per-folder "trust this folder" gate is NOT skippable by any flag —
    # it is handled out-of-band by seeding each CLI's global config (trustAll /
    # trustDirectories) in qoder_config / codebuddy_config (init + upgrade).
    AgentKind.qoder:    (
        ["qodercli", "--permission-mode", "bypass_permissions"],
        ["qodercli", "--permission-mode", "bypass_permissions", "--continue"],
    ),
    AgentKind.codebuddy: (
        ["codebuddy", "--permission-mode", "bypassPermissions"],
        ["codebuddy", "--permission-mode", "bypassPermissions", "--continue"],
    ),
    AgentKind.hermes:   (["hermes", "--yolo"],   ["hermes", "--yolo", "-c"]),
}

# Kinds where ClawsomeFlow carries the permission flag itself and must tell
# ClawTeam NOT to inject one (strategy B above). Passed as
# ``skip_permissions=False`` to spawn_fresh/spawn_resume.
_SELF_PERMISSION_KINDS: frozenset[AgentKind] = frozenset({
    AgentKind.gemini,
    AgentKind.qwen,
    AgentKind.kimi,
    AgentKind.opencode,
    AgentKind.nanobot,
    AgentKind.qoder,
    AgentKind.codebuddy,
})

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
            # Copy the shared template lists — we may append per-agent flags
            # below and must never mutate the module-level _KIND_TO_CMD entries.
            base_spawn, base_resume = _KIND_TO_CMD[agent.kind]
            self._spawn_cmd = list(base_spawn)
            self._resume_cmd = list(base_resume)
            if agent.kind == AgentKind.hermes and not agent.is_temporary:
                # Bind the executor to its managed Hermes profile. The profile
                # name IS the agent id (== HermesAgent.id). ``-p`` is a global,
                # position-independent flag, so appending is safe for both the
                # fresh and ``-c`` (continue) commands. This is the Hermes
                # equivalent of OpenClaw's session-id binding and is REQUIRED so
                # the agent's own identity/memory/skills are used.
                # Temporary Hermes agents have NO managed profile → no ``-p``.
                self._spawn_cmd += ["-p", agent.id]
                self._resume_cmd += ["-p", agent.id]
            if agent.kind == AgentKind.nanobot:
                # nanobot keys sessions by ``-s`` (default ``cli:direct``), NOT by
                # cwd. Inject a stable, per-agent session id so (1) concurrent
                # nanobot agents don't share one conversation, and (2) a crash
                # resume reattaches to the same session instead of starting over.
                session_id = f"{self.team_name}-{agent.id}"
                self._spawn_cmd += ["-s", session_id]
                self._resume_cmd += ["-s", session_id]

    # ── concrete state-machine actions ───────────────────────────────

    def _skip_permissions(self) -> bool:
        """Whether ClawTeam should inject its per-CLI permission-bypass flag.

        False for kinds that carry the exact flag themselves (see
        _SELF_PERMISSION_KINDS) — passing both would duplicate/conflict (gemini)
        or fail outright (opencode rejects ClawTeam's ``--yolo``).
        """
        return self.agent.kind not in _SELF_PERMISSION_KINDS

    def _resolve_profile(self) -> str | None:
        """ClawTeam runtime profile to apply at spawn.

        Temporary (ad-hoc) agents carry no profile. Persistent agents keep the
        author-provided ``FlowAgent.profile`` — only Hermes uses it today (it
        also binds identity via ``-p``; see __init__). Claude/Codex/Cursor are
        temporary-only (no persistent management platform), so they fall through
        to ``None`` and spawn against the operator's own default CLI config.
        """
        if self.agent.is_temporary:
            return None
        return self.agent.profile

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
            profile=self._resolve_profile(),
            skills=(),  # explicitly NO skills (no clawteam, no opt-ins)
            skip_permissions=self._skip_permissions(),
        )
        # Wait for the CLI to finish booting before any dispatch.
        result = await wait_tui_ready(
            self.tmux_target,
            trust_platform=resolve_trust_platform(
                agent_kind=self.agent.kind.value,
                spawn_command=self._spawn_cmd,
            ),
            timeout_sec=self._ready_timeout,
        )
        if not result.ok:
            raise CliInvocationError(
                argv=["wait_tui_ready", result.reason_code],
                exit_code=1,
                stderr=result.message
                or f"TUI prompt never appeared in {self._ready_timeout}s "
                f"on tmux pane {self.tmux_target}",
                stdout=result.pane_tail,
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
            profile=self._resolve_profile(),
            skills=(),
            skip_permissions=self._skip_permissions(),
        )
        result = await wait_tui_ready(
            self.tmux_target,
            trust_platform=resolve_trust_platform(
                agent_kind=self.agent.kind.value,
                spawn_command=self._spawn_cmd,
            ),
            timeout_sec=self._ready_timeout,
        )
        if result.ok:
            return
        pane_text = await tmux_capture_pane(self.tmux_target, history_lines=120)
        stderr = result.message or (
            f"TUI prompt never appeared after {phase} on {self.tmux_target}"
        )
        raise CliInvocationError(
            argv=["wait_tui_ready", result.reason_code, self.tmux_target, phase],
            exit_code=1,
            stderr=stderr,
            stdout=pane_text[-2000:] or result.pane_tail,
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
