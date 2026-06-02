"""In-memory chat history cache for OpenClaw direct chat UI.

This module stores chat messages *only* for frontend rendering convenience.
It is intentionally decoupled from OpenClaw runtime context:

* OpenClaw context is maintained by ``session_key`` server-side.
* We do NOT replay full history to OpenClaw on each turn.
* We only cache per-session messages for UI display and clear on reset.
"""

from __future__ import annotations

import asyncio
from typing import TypedDict


class ChatHistoryMessage(TypedDict):
    role: str
    content: str


_MAX_MESSAGES_PER_SESSION = 400
_history: dict[str, list[ChatHistoryMessage]] = {}
_lock = asyncio.Lock()


async def list_messages(session_key: str) -> list[ChatHistoryMessage]:
    """Return a copy of cached messages for one session."""
    async with _lock:
        rows = _history.get(session_key, [])
        return [{"role": m["role"], "content": m["content"]} for m in rows]


async def append_message(session_key: str, *, role: str, content: str) -> None:
    """Append one message into cached history (best-effort, bounded)."""
    if not content:
        return
    async with _lock:
        rows = _history.setdefault(session_key, [])
        rows.append({"role": role, "content": content})
        if len(rows) > _MAX_MESSAGES_PER_SESSION:
            del rows[: len(rows) - _MAX_MESSAGES_PER_SESSION]


async def clear_messages(session_key: str) -> None:
    """Drop cached messages for one session."""
    async with _lock:
        _history.pop(session_key, None)


__all__ = [
    "ChatHistoryMessage",
    "append_message",
    "clear_messages",
    "list_messages",
]
