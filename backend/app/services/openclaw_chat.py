"""Live step-level progress + reconnectable turn state for OpenClaw chat.

``openclaw agent … --json`` returns only the FINAL result (no streaming), so the
chat box looks frozen for the whole turn and a refresh past the frontend's poll
window loses the in-flight view. This module adds a progress layer WITHOUT
touching the proven turn-execution / reset / import paths in
:mod:`app.api.openclaw_agents`:

* When a turn starts, :func:`start_progress` registers a :class:`ChatTurn` keyed
  by ``session_key`` and spawns a daemon **follower**:
  ``openclaw sessions tail --session-key <key> --follow`` — a first-class live
  "trajectory progress" stream that reads the session store (independent of the
  gateway). Each emitted line becomes a :class:`StepEvent`.
* The API records the in-flight ``openclaw agent`` subprocess on the turn so
  :func:`kill_turn` (reset / supersede) can stop a runaway turn.
* The registry snapshot powers ``GET /{id}/chat/status`` for unbounded reconnect
  after a tab switch / refresh.

We pass our own ``--session-id user-chat-<user>-<agent>`` to ``openclaw agent``;
the session store scopes it to a full ``agent:<id>:…`` key, so the follower
resolves the real key via ``sessions list --agent <id> --json`` (matching the
``user-chat-…`` suffix), falling back to ``--agent`` tail (latest active) if the
session has not materialised yet.

Mirrors :mod:`app.services.hermes_chat` (same ``StepEvent`` / snapshot shape).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.integrations.openclaw_cli import resolve_openclaw_executable
from app.logging_setup import get_logger
from app.services import subprocess_registry as _subproc_registry

logger = get_logger("services.openclaw_chat")

# How long the follower retries resolving the real session key before falling
# back to an agent-scoped tail.
_RESOLVE_TIMEOUT_SEC = 20.0
_RESOLVE_INTERVAL_SEC = 1.5
_MAX_STEPS = 400


class StepEvent(TypedDict, total=False):
    kind: str  # "info" | "tool"
    name: str
    seq: int


@dataclass
class ChatTurn:
    """Live progress state for one OpenClaw chat turn. Thread-safe via ``_lock``."""

    agent_id: str
    session_key: str
    started_at: float
    status: str = "running"  # running | done | error
    steps: list[StepEvent] = field(default_factory=list)
    final_text: str = ""
    error: str = ""
    # The in-flight ``openclaw agent`` subprocess (set by the API once spawned)
    # and the ``sessions tail --follow`` follower; both killed by kill_turn.
    # ``agent_proc`` may be an asyncio subprocess (no ``poll()``), so killing
    # goes through the pid-based ``subprocess_registry.kill_group``.
    agent_proc: Any = None
    follower: subprocess.Popen | None = None
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append_step(self, kind: str, *, name: str = "") -> None:
        with self._lock:
            self._seq += 1
            step: StepEvent = {"kind": kind, "seq": self._seq}
            if name:
                step["name"] = name
            self.steps.append(step)
            if len(self.steps) > _MAX_STEPS:
                del self.steps[: len(self.steps) - _MAX_STEPS]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "steps": [dict(s) for s in self.steps],
                "progress": {
                    "toolCalls": 0,
                    "apiCalls": 0,
                    "messageCount": len(self.steps),
                    "elapsedSec": round(time.monotonic() - self.started_at, 1),
                },
                "final": self.final_text,
                "error": self.error,
                "startedAtMono": self.started_at,
            }


_TURNS: dict[str, ChatTurn] = {}
_REG_LOCK = threading.Lock()


def get_turn(session_key: str) -> ChatTurn | None:
    with _REG_LOCK:
        return _TURNS.get(session_key)


def _kill_proc(proc: Any) -> bool:
    """Kill a process group by pid (works for both ``subprocess.Popen`` and
    ``asyncio`` subprocesses; the latter has no ``poll()``)."""
    if proc is None:
        return False
    try:
        signalled = _subproc_registry.kill_group(proc)
    finally:
        _subproc_registry.unregister(proc)
    return signalled


def kill_turn(session_key: str) -> bool:
    """Kill and forget any in-flight turn (agent + follower). Idempotent."""
    with _REG_LOCK:
        turn = _TURNS.pop(session_key, None)
    if turn is None:
        return False
    a = _kill_proc(turn.agent_proc)
    _kill_proc(turn.follower)
    with turn._lock:
        if turn.status == "running":
            turn.status = "error"
    if a:
        logger.info("openclaw_chat_killed", session_key=session_key, agent_id=turn.agent_id)
    return a


def start_progress(agent_id: str, session_key: str) -> ChatTurn:
    """Register a turn and start its trajectory follower. Supersedes any prior
    in-flight turn for the same ``session_key``."""
    kill_turn(session_key)
    turn = ChatTurn(agent_id=agent_id, session_key=session_key, started_at=time.monotonic())
    with _REG_LOCK:
        _TURNS[session_key] = turn
    # Tests set this to keep the suite from forking real ``openclaw sessions``
    # subprocesses (mirrors CSFLOW_DISABLE_BOARD etc. in the autouse fixture).
    if not os.getenv("CSFLOW_DISABLE_OPENCLAW_CHAT_FOLLOWER"):
        threading.Thread(
            target=_run_follower, args=(turn,), name="openclaw-chat-follow", daemon=True
        ).start()
    return turn


def finish_progress(
    session_key: str, *, status: str, final: str = "", error: str = ""
) -> None:
    """Mark a turn done/error (carrying the final answer / error) and stop its
    follower. The turn stays in the registry so a reconnecting client can read
    the terminal status + final once."""
    turn = get_turn(session_key)
    if turn is None:
        return
    _kill_proc(turn.follower)
    with turn._lock:
        if turn.status == "running":
            turn.status = status
            turn.final_text = final
            turn.error = error


def set_agent_proc(session_key: str, proc: subprocess.Popen) -> None:
    """Record the in-flight ``openclaw agent`` subprocess so kill_turn can stop
    it. No-op if the turn was already superseded/killed."""
    turn = get_turn(session_key)
    if turn is not None:
        turn.agent_proc = proc


# ── follower ──────────────────────────────────────────────────────────


def _resolve_session_key(exe: str, agent_id: str, session_key: str) -> str | None:
    """Find the full stored session key (``agent:<id>:…user-chat-…``) for this
    turn by matching the ``user-chat-…`` suffix we passed as ``--session-id``."""
    try:
        proc = subprocess.run(  # noqa: S603 — args constructed, not shell
            [exe, "sessions", "list", "--agent", agent_id, "--json", "--limit", "50"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    items = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    for it in items:
        if isinstance(it, dict) and session_key in str(it.get("key", "")):
            return str(it["key"])
    return None


def _run_follower(turn: ChatTurn) -> None:
    exe = resolve_openclaw_executable()
    if not exe:
        return
    # Resolve the real session key (retry until the session materialises).
    full_key: str | None = None
    deadline = time.monotonic() + _RESOLVE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if turn.status != "running":
            return
        full_key = _resolve_session_key(exe, turn.agent_id, turn.session_key)
        if full_key:
            break
        time.sleep(_RESOLVE_INTERVAL_SEC)

    # The turn may have finished (or been killed) while we were resolving — don't
    # spawn a long-lived ``--follow`` that nothing would reap.
    if turn.status != "running":
        return

    argv = [exe, "sessions", "tail", "--follow", "--tail", "0"]
    if full_key:
        argv += ["--session-key", full_key]
    else:
        argv += ["--agent", turn.agent_id]  # fallback: latest active for this agent
    try:
        proc = subprocess.Popen(  # noqa: S603 — args constructed, not shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return
    turn.follower = proc
    _subproc_registry.register(proc)
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            if turn.status != "running":
                break
            line = raw.strip()
            if line:
                turn.append_step("info", name=line[:200])
    except Exception:  # pragma: no cover - defensive
        pass
    finally:
        _kill_proc(proc)


__all__ = [
    "ChatTurn",
    "StepEvent",
    "start_progress",
    "finish_progress",
    "kill_turn",
    "set_agent_proc",
    "get_turn",
]
