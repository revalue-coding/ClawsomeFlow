"""Reconnectable elapsed progress for OpenClaw direct chat.

``openclaw agent … --json`` returns only the final result (no streaming). This
module tracks in-flight turn state (elapsed time + terminal final/error) for
``GET /{id}/chat/status`` reconnect after a tab switch / refresh.

Unlike the earlier trajectory follower, we do **not** spawn auxiliary
``openclaw sessions`` subprocesses during a turn — the WebUI ``PendingReply``
bubble handles the 10s+ thinking hint client-side.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.logging_setup import get_logger
from app.services import subprocess_registry as _subproc_registry

logger = get_logger("services.openclaw_chat")

_MAX_STEPS = 400


@dataclass
class ChatTurn:
    """Live progress state for one OpenClaw chat turn. Thread-safe via ``_lock``."""

    agent_id: str
    session_key: str
    started_at: float
    status: str = "running"  # running | done | error
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    error: str = ""
    agent_proc: Any = None
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append_step(self, kind: str, *, name: str = "") -> None:
        # Kept for API compatibility; direct chat no longer emits tool steps.
        with self._lock:
            self._seq += 1
            step: dict[str, Any] = {"kind": kind, "seq": self._seq}
            if name:
                step["name"] = name
            self.steps.append(step)
            if len(self.steps) > _MAX_STEPS:
                del self.steps[: len(self.steps) - _MAX_STEPS]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "steps": [],
                "progress": {
                    "toolCalls": 0,
                    "apiCalls": 0,
                    "messageCount": 0,
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
    if proc is None:
        return False
    try:
        signalled = _subproc_registry.kill_group(proc)
    finally:
        _subproc_registry.unregister(proc)
    return signalled


def kill_turn(session_key: str) -> bool:
    """Kill and forget any in-flight turn. Idempotent."""
    with _REG_LOCK:
        turn = _TURNS.pop(session_key, None)
    if turn is None:
        return False
    signalled = _kill_proc(turn.agent_proc)
    with turn._lock:
        if turn.status == "running":
            turn.status = "error"
    if signalled:
        logger.info("openclaw_chat_killed", session_key=session_key, agent_id=turn.agent_id)
    return signalled


def start_progress(agent_id: str, session_key: str) -> ChatTurn:
    """Register a turn for status/reconnect. Supersedes any prior in-flight turn."""
    kill_turn(session_key)
    turn = ChatTurn(agent_id=agent_id, session_key=session_key, started_at=time.monotonic())
    with _REG_LOCK:
        _TURNS[session_key] = turn
    return turn


def finish_progress(
    session_key: str, *, status: str, final: str = "", error: str = ""
) -> None:
    """Mark a turn done/error. The turn stays in the registry for reconnect."""
    turn = get_turn(session_key)
    if turn is None:
        return
    with turn._lock:
        if turn.status == "running":
            turn.status = status
            turn.final_text = final
            turn.error = error


def set_agent_proc(session_key: str, proc: Any) -> None:
    """Record the in-flight ``openclaw agent`` subprocess so kill_turn can stop it."""
    turn = get_turn(session_key)
    if turn is not None:
        turn.agent_proc = proc


__all__ = [
    "ChatTurn",
    "start_progress",
    "finish_progress",
    "kill_turn",
    "set_agent_proc",
    "get_turn",
]
