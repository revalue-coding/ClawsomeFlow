"""WorkerSession abstraction + state machine.

A :class:`WorkerSession` represents the lifecycle of a single ``FlowAgent``
within one :class:`FlowRun`. It hides the differences between TUI agents
(claude / codex / ...) and OpenClaw (which runs as a one-shot subprocess
inside a tmux ``bash``).

State machine (DEV.md scheduler section):

    Absent  ─spawn()──▶  Spawning  ─ready()─▶  Idle
                              │
                              └─spawn fails────▶  Crashed  ─resume()──▶  Spawning
    Idle  ─dispatch(task)──▶  Busy  ─task done──▶  Idle
    Busy  ─process dies──▶  Crashed
    *     ─shutdown()──▶  Exited (terminal)

Transitions are guarded; illegal moves raise :class:`InvalidStateTransition`.
This is *intentional* — the scheduler logs the violation and treats it as
a programmer bug (we'd rather crash early than ship a corrupted state machine).

The two concrete implementations live in:
* :mod:`app.scheduler.sessions.tmux_live` — TmuxLiveSession (claude/codex/...)
* :mod:`app.scheduler.sessions.openclaw_tmux` — OpenClawTmuxSession (bash + send-keys)

Both subclasses just implement the four abstract methods (``_do_spawn`` /
``_do_dispatch`` / ``_do_resume`` / ``_do_shutdown``) and let the base class
own the state machine + lock + logging.
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

from app.logging_setup import get_logger
from app.models import FlowAgent
from app.worktree.lookup import WorktreeInfo

logger = get_logger("scheduler.sessions")


# ──────────────────────────────────────────────────────────────────────
# State machine
# ──────────────────────────────────────────────────────────────────────


class SessionState(str, Enum):
    """Lifecycle states (see DEV.md)."""

    Absent = "absent"
    Spawning = "spawning"
    Idle = "idle"
    Busy = "busy"
    Crashed = "crashed"
    Exited = "exited"


# Allowed transitions; anything outside this set raises.
_ALLOWED: dict[SessionState, frozenset[SessionState]] = {
    SessionState.Absent: frozenset({SessionState.Spawning, SessionState.Exited}),
    SessionState.Spawning: frozenset({
        SessionState.Idle, SessionState.Crashed, SessionState.Exited,
    }),
    SessionState.Idle: frozenset({
        SessionState.Busy, SessionState.Crashed, SessionState.Exited,
        SessionState.Spawning,  # rare: explicit re-spawn after intentional shutdown
    }),
    SessionState.Busy: frozenset({
        SessionState.Idle, SessionState.Crashed, SessionState.Exited,
    }),
    SessionState.Crashed: frozenset({
        SessionState.Spawning, SessionState.Exited,
    }),
    SessionState.Exited: frozenset(),  # terminal
}


class InvalidStateTransition(Exception):
    """Raised when a state-machine guarantee is violated."""

    def __init__(self, agent_id: str, from_state: SessionState, to_state: SessionState):
        super().__init__(
            f"agent {agent_id!r}: illegal transition {from_state.value} → {to_state.value}"
        )
        self.agent_id = agent_id
        self.from_state = from_state
        self.to_state = to_state


# ──────────────────────────────────────────────────────────────────────
# Inputs / outputs
# ──────────────────────────────────────────────────────────────────────


@dataclass
class DispatchOutcome:
    """Returned by :meth:`WorkerSession.dispatch` so the controller can react."""

    success: bool
    detail: str = ""               # human-readable error/info
    error_type: str = ""
    exit_code: int | None = None
    stderr: str = ""
    stdout: str = ""
    argv: list[str] = field(default_factory=list)
    dispatched_at: float = field(default_factory=time.time)


# ──────────────────────────────────────────────────────────────────────
# Base class
# ──────────────────────────────────────────────────────────────────────


class WorkerSession(abc.ABC):
    """One worker's lifecycle within one Run.

    Subclasses provide the *how* (TUI inject / OpenClaw send-keys);
    this base class provides the *when* (state machine + serialisation).
    """

    def __init__(
        self,
        *,
        agent: FlowAgent,
        team_name: str,
        run_id: str,
    ) -> None:
        self.agent = agent
        self.team_name = team_name
        self.run_id = run_id
        self._state = SessionState.Absent
        self._lock = asyncio.Lock()  # serialises concurrent spawn/dispatch
        self._tmux_target_override: str | None = None
        self.worktree: WorktreeInfo | None = None
        self.last_dispatched_at: float | None = None
        self.last_dispatched_task_id: str | None = None
        self.spawn_attempts: int = 0

    # ── public surface (state-machine-guarded) ───────────────────────

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def tmux_target(self) -> str:
        """Conventional ``session:window`` address for tmux helpers.

        ClawTeam uses ``clawteam-<team>`` for the tmux session and the
        agent_name as the window name (see ClawTeam tmux_backend).
        """
        if self._tmux_target_override:
            return self._tmux_target_override
        return f"clawteam-{self.team_name}:{self.agent.id}"

    def set_tmux_target_override(self, target: str | None) -> None:
        """Override tmux target (used when duplicate window names exist)."""
        self._tmux_target_override = target

    async def spawn(self) -> None:
        """Bring the session from Absent/Crashed → Spawning → Idle.

        On ``_do_spawn`` failure the state moves to Crashed and the original
        exception is re-raised so the caller can decide whether to retry.
        """
        async with self._lock:
            self._transition(
                {SessionState.Absent, SessionState.Crashed, SessionState.Idle},
                SessionState.Spawning,
                reason="spawn",
            )
            self.spawn_attempts += 1
            try:
                await self._do_spawn()
            except Exception:
                self._transition_force(SessionState.Crashed, reason="spawn_failed")
                raise
            self._transition_force(SessionState.Idle, reason="spawn_ready")

    async def dispatch(
        self, *, task_id: str, message: str,
    ) -> DispatchOutcome:
        """Send a dispatch message to the live session (Idle → Busy)."""
        from app.integrations.clawteam_cli import CliInvocationError

        async with self._lock:
            self._transition({SessionState.Idle}, SessionState.Busy, reason="dispatch")
            self.last_dispatched_task_id = task_id
            try:
                await self._do_dispatch(message=message, task_id=task_id)
            except CliInvocationError as exc:
                self._transition_force(SessionState.Idle, reason="dispatch_failed")
                logger.warning(
                    "session_dispatch_failed",
                    agent_id=self.agent.id,
                    task_id=task_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exit_code=exc.exit_code,
                )
                return DispatchOutcome(
                    success=False,
                    detail=str(exc),
                    error_type=type(exc).__name__,
                    exit_code=exc.exit_code,
                    stderr=exc.stderr or "",
                    stdout=exc.stdout or "",
                    argv=list(exc.argv),
                )
            except Exception as exc:
                # We didn't even get the message off; revert to Idle so the
                # controller can try again next tick.
                self._transition_force(SessionState.Idle, reason="dispatch_failed")
                logger.warning(
                    "session_dispatch_failed",
                    agent_id=self.agent.id,
                    task_id=task_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                return DispatchOutcome(
                    success=False,
                    detail=str(exc),
                    error_type=type(exc).__name__,
                )
            self.last_dispatched_at = time.time()
            return DispatchOutcome(success=True)

    def mark_idle(self, *, reason: str = "task_done") -> None:
        """Called by the controller when the task this session was working on
        transitions to ``completed``/``blocked``. No I/O here."""
        # Tolerate stray calls (e.g. completed-event arriving for a session
        # that already crashed and was recreated).
        if self._state == SessionState.Busy:
            self._transition_force(SessionState.Idle, reason=reason)

    def mark_crashed(self, *, reason: str = "process_dead") -> None:
        """Called by the failure detector when the process is gone."""
        if self._state in {SessionState.Idle, SessionState.Busy, SessionState.Spawning}:
            self._transition_force(SessionState.Crashed, reason=reason)

    def adopt_existing(self, *, reason: str = "adopt_existing") -> None:
        """Adopt an already-running external session (Absent -> Idle).

        Used when a new controller is created (e.g. post-run complaint phase
        after backend restart) but the tmux pane for this worker is still alive.
        We model this as a synthetic Absent -> Spawning -> Idle transition so
        downstream state-machine invariants remain intact.
        """
        self._transition({SessionState.Absent}, SessionState.Spawning, reason="adopt_probe")
        self._transition_force(SessionState.Idle, reason=reason)

    async def resume(self) -> None:
        """Try to bring a Crashed session back to Idle without losing worktree."""
        async with self._lock:
            self._transition({SessionState.Crashed}, SessionState.Spawning, reason="resume")
            self.spawn_attempts += 1
            try:
                await self._do_resume()
            except Exception:
                self._transition_force(SessionState.Crashed, reason="resume_failed")
                raise
            self._transition_force(SessionState.Idle, reason="resume_ready")

    async def shutdown(self, *, reason: str = "run_finalize") -> None:
        """Terminal: ask the worker to stop. Idempotent."""
        async with self._lock:
            if self._state == SessionState.Exited:
                return
            try:
                await self._do_shutdown()
            except Exception as exc:
                logger.warning(
                    "session_shutdown_failed",
                    agent_id=self.agent.id, error=str(exc),
                )
            self._transition_force(SessionState.Exited, reason=reason)

    # ── subclass contract ────────────────────────────────────────────

    @abc.abstractmethod
    async def _do_spawn(self) -> None:
        """Bring up the underlying tmux session + CLI binary."""

    @abc.abstractmethod
    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        """Deliver the (already-rendered) dispatch text into the live session."""

    @abc.abstractmethod
    async def _do_resume(self) -> None:
        """Re-spawn against the existing worktree (no worktree recreation)."""

    @abc.abstractmethod
    async def _do_shutdown(self) -> None:
        """Politely stop the worker (worker process, NOT the worktree)."""

    # ── internal state-machine helpers ───────────────────────────────

    def _transition(
        self, allowed_from: set[SessionState], to: SessionState, *, reason: str,
    ) -> None:
        """Guarded transition; raises if current state isn't in *allowed_from*."""
        if self._state not in allowed_from:
            raise InvalidStateTransition(self.agent.id, self._state, to)
        self._transition_force(to, reason=reason)

    def _transition_force(self, to: SessionState, *, reason: str) -> None:
        """Unconditional transition (still validates the *to* set)."""
        if to != self._state and to not in _ALLOWED.get(self._state, frozenset()):
            raise InvalidStateTransition(self.agent.id, self._state, to)
        old = self._state
        self._state = to
        logger.info(
            "session_state_transition",
            agent_id=self.agent.id, team=self.team_name, run_id=self.run_id,
            **{"from": old.value, "to": to.value, "reason": reason},
        )


__all__ = [
    "DispatchOutcome",
    "InvalidStateTransition",
    "SessionState",
    "WorkerSession",
]
