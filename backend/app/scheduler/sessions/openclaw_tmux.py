"""OpenClawTmuxSession — OpenClaw agent dispatched via tmux + send-keys.

Why this is special (DEV.md §4.3 + §9):

* OpenClaw is a one-shot subprocess CLI (``openclaw agent --message ...``);
  it does **not** stay alive between dispatches.
* But we still want each FlowRun's invocations to:
   - share a worktree (so commits accumulate on a single branch)
   - share a session-id (so OpenClaw replays its conversational history)
   - be visible to ``tmux attach`` for live debugging

The trick: spawn a long-lived **tmux bash** via ClawTeam (which creates the
worktree for us as a side effect of ``--workspace``), and then for each
dispatch inject one command line via ``tmux send-keys``.

Platform-specific dispatch mode:
* Linux / non-macOS: keep the historical inline ``--message '<payload>'`` mode.
* macOS: for risky payloads (long or multi-line), write prompt to a local temp
  file and send a short command line that expands
  ``--message "$(cat <temp-file>)"``.

This avoids shell deadlocks from ultra-long inline quoted payloads on macOS.
The bash holds the
``cwd = worktree_path`` so any user inspecting the pane sees commits in the
right place; OpenClaw still writes per its ``openclaw.json`` workspace —
dispatch message hard constraints + post-task audit ensure writes stay
in the worktree.

Spawn:
    clawteam spawn tmux bash --workspace --repo <agent_main_repo> --no-keepalive

Dispatch (per task):
    Linux:
        tmux send-keys "... --message '<quoted prompt>'" Enter
    macOS risky payload:
        1) write prompt to /tmp/csflow-openclaw-*.txt
        2) tmux send-keys "__msg_file=...; openclaw ... --message \"$(cat ...)\"; rm -f"
           Enter

Resume re-spawn (after pane crash):
    clawteam spawn tmux bash --no-workspace --repo <existing_worktree>
        --no-keepalive

Shutdown:
    clawteam lifecycle request-shutdown <team> <agent>
"""

from __future__ import annotations

import os
import platform
import shlex
import tempfile

from app.integrations.clawteam_cli import (
    ClawTeamCli,
    CliInvocationError,
    get_clawteam_cli,
)
from app.logging_setup import get_logger
from app.models import AgentKind, FlowAgent
from app.scheduler.naming import openclaw_session_id_for_run
from app.scheduler.sessions.base import WorkerSession
from app.scheduler.sessions.tmux_ready import (
    tmux_capture_pane,
    wait_shell_ready,
)

logger = get_logger("scheduler.sessions.openclaw_tmux")

_INLINE_LONG_MESSAGE_THRESHOLD_CHARS = 4096


class OpenClawTmuxSession(WorkerSession):
    """OpenClaw worker hosted inside a long-lived tmux bash session."""

    def __init__(
        self,
        *,
        agent: FlowAgent,
        team_name: str,
        run_id: str,
        agent_main_repo: str,        # = ~/.clawsomeflow/agents/{id}/workspace/
        cli: ClawTeamCli | None = None,
        ready_timeout_sec: float = 15.0,
        host_platform: str | None = None,
    ) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        if agent.kind != AgentKind.openclaw:
            raise ValueError(
                f"OpenClawTmuxSession only supports kind=openclaw, got {agent.kind.value}"
            )
        self._cli = cli or get_clawteam_cli()
        self._ready_timeout = ready_timeout_sec
        self._main_repo = agent_main_repo
        self._host_platform = (host_platform or platform.system()).strip()

    # ── conventions ──────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """OpenClaw session id — DEV.md §5.5: ``{team_name}-{agent_id}``.

        team_name already contains the run id short, so this is automatically
        unique per Run × OpenClaw agent (no two Runs leak history).
        """
        return openclaw_session_id_for_run(self.team_name, self.agent.id)

    # ── concrete state-machine actions ───────────────────────────────

    async def _do_spawn(self) -> None:
        await self._cli.spawn_fresh(
            team=self.team_name,
            agent_name=self.agent.id,
            repo=self._main_repo,
            command=["bash"],
            profile=self.agent.profile,
            skills=(),
        )
        ok = await wait_shell_ready(self.tmux_target, timeout_sec=self._ready_timeout)
        if not ok:
            raise CliInvocationError(
                argv=["wait_shell_ready"], exit_code=1,
                stderr=f"bash prompt never appeared in {self._ready_timeout}s "
                       f"on tmux pane {self.tmux_target}",
            )

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        mode = "inline_quoted_message"
        message_long = self._is_message_long(message)
        use_message_file = self._should_use_message_file_injection(message=message)
        if use_message_file:
            message_file = self._write_dispatch_message_file(message)
            line = self._build_openclaw_dispatch_line(message_file=message_file)
            mode = "message_file_substitution"
            await self._inject_shell_line_with_optional_cleanup(
                line=line,
                cleanup_path=message_file,
            )
        else:
            line = self._build_openclaw_inline_line(message=message)
            await self._inject_shell_line_with_optional_cleanup(line=line)
        logger.info(
            "openclaw_send_keys",
            target=self.tmux_target,
            session_id=self.session_id,
            message_len=len(message),
            message_is_long=message_long,
            host_platform=self._host_platform,
            worktree_path=self.worktree.worktree_path if self.worktree else None,
            task_id=task_id,
            injection_mode=mode,
        )

    async def _do_resume(self) -> None:
        # The bash shell may simply have exited — re-spawn against the
        # existing worktree so OpenClaw's session history (kept by OpenClaw
        # itself, keyed by --session-id) is reused on the next dispatch.
        if self.worktree is None:
            raise RuntimeError(
                f"agent {self.agent.id!r}: cannot resume without recorded worktree"
            )
        await self._cli.spawn_resume(
            team=self.team_name,
            agent_name=self.agent.id,
            existing_worktree=self.worktree.worktree_path,
            resume_command=["bash"],
            profile=self.agent.profile,
            skills=(),
        )
        ok = await wait_shell_ready(self.tmux_target, timeout_sec=self._ready_timeout)
        if not ok:
            raise CliInvocationError(
                argv=["wait_shell_ready"], exit_code=1,
                stderr=f"bash prompt never appeared after resume on {self.tmux_target}",
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

    # ── tmux glue ───────────────────────────────────────────────────

    def _is_message_long(self, message: str) -> bool:
        return len(message) >= _INLINE_LONG_MESSAGE_THRESHOLD_CHARS

    def _should_use_message_file_injection(self, *, message: str) -> bool:
        if self._host_platform.lower() != "darwin":
            return False
        if self._is_message_long(message):
            return True
        # Multi-line payloads are also risky for inline shell quoting on macOS.
        return ("\n" in message) or ("\r" in message)

    def _write_dispatch_message_file(self, message: str) -> str:
        fd, path = tempfile.mkstemp(
            prefix=f"csflow-openclaw-{self.team_name}-{self.agent.id}-",
            suffix=".txt",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(message)
        except Exception:
            self._cleanup_dispatch_message_file(path)
            raise
        return path

    def _build_openclaw_dispatch_line(self, *, message_file: str) -> str:
        base_cmd = " ".join(
            shlex.quote(a)
            for a in [
                "openclaw",
                "agent",
                "--local",
                "--agent",
                self.agent.id,
                "--session-id",
                self.session_id,
            ]
        )
        file_quoted = shlex.quote(message_file)
        return (
            f"__csflow_msg_file={file_quoted}; "
            f"{base_cmd} --message \"$(cat \"$__csflow_msg_file\")\"; "
            "__csflow_rc=$?; "
            "rm -f \"$__csflow_msg_file\"; "
            "unset __csflow_msg_file __csflow_rc"
        )

    def _build_openclaw_inline_line(self, *, message: str) -> str:
        argv = [
            "openclaw",
            "agent",
            "--local",
            "--agent",
            self.agent.id,
            "--session-id",
            self.session_id,
            "--message",
            message,
        ]
        return " ".join(shlex.quote(a) for a in argv)

    async def _inject_shell_line_with_optional_cleanup(
        self,
        *,
        line: str,
        cleanup_path: str | None = None,
    ) -> None:
        # tmux send-keys: 1st invocation pastes the line, 2nd sends Enter.
        # Splitting them avoids tmux interpreting characters in the prompt.
        try:
            await self._send_keys(line)
            await self._send_keys("Enter", literal=False)
        except Exception:
            if cleanup_path:
                # If injection itself fails, best-effort cleanup the temp payload
                # file here (normal path self-cleans inside the injected shell line).
                self._cleanup_dispatch_message_file(cleanup_path)
            raise

    def _cleanup_dispatch_message_file(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    async def _send_keys(self, payload: str, *, literal: bool = True) -> None:
        """Run ``tmux send-keys -t <target> -l? <payload>``.

        ``literal=True`` (default) passes ``-l`` so tmux doesn't interpret
        special characters; ``literal=False`` lets us send named keys
        like ``Enter``.
        """
        import asyncio
        argv = ["tmux", "send-keys", "-t", self.tmux_target]
        if literal:
            argv.append("-l")
        argv.append(payload)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise CliInvocationError(
                argv=argv, exit_code=proc.returncode or 1,
                stderr=stderr.decode("utf-8", errors="replace"),
            )

    # Exposed for tests + diagnostics
    async def capture_pane(self, *, history_lines: int = 80) -> str:
        return await tmux_capture_pane(self.tmux_target, history_lines=history_lines)


__all__ = ["OpenClawTmuxSession"]
