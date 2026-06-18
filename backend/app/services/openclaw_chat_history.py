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


class ChatAttachmentMeta(TypedDict):
    id: str
    name: str
    mime_type: str
    size_bytes: int
    absolute_path: str
    relative_path: str
    route: str


class ChatHistoryMessage(TypedDict, total=False):
    role: str
    content: str
    attachments: list[ChatAttachmentMeta]


_MAX_MESSAGES_PER_SESSION = 400
_history: dict[str, list[ChatHistoryMessage]] = {}
_lock = asyncio.Lock()


async def list_messages(session_key: str) -> list[ChatHistoryMessage]:
    """Return a copy of cached messages for one session."""
    async with _lock:
        rows = _history.get(session_key, [])
        out: list[ChatHistoryMessage] = []
        for row in rows:
            item: ChatHistoryMessage = {"role": row["role"], "content": row["content"]}
            attachments = row.get("attachments") or []
            if isinstance(attachments, list):
                cleaned = [
                    {
                        "id": str(a.get("id", "")),
                        "name": str(a.get("name", "")),
                        "mime_type": str(a.get("mime_type", "")),
                        "size_bytes": int(a.get("size_bytes", 0) or 0),
                        "absolute_path": str(a.get("absolute_path", "")),
                        "relative_path": str(a.get("relative_path", "")),
                        "route": str(a.get("route", "path_injection")),
                    }
                    for a in attachments
                    if isinstance(a, dict)
                ]
                if cleaned:
                    item["attachments"] = cleaned
            out.append(item)
        return out


async def append_message(
    session_key: str,
    *,
    role: str,
    content: str,
    attachments: list[ChatAttachmentMeta] | None = None,
) -> None:
    """Append one message into cached history (best-effort, bounded)."""
    if not content and not attachments:
        return
    cleaned_attachments = attachments or []
    async with _lock:
        rows = _history.setdefault(session_key, [])
        item: ChatHistoryMessage = {"role": role, "content": content}
        if cleaned_attachments:
            item["attachments"] = [
                {
                    "id": str(a.get("id", "")),
                    "name": str(a.get("name", "")),
                    "mime_type": str(a.get("mime_type", "")),
                    "size_bytes": int(a.get("size_bytes", 0) or 0),
                    "absolute_path": str(a.get("absolute_path", "")),
                    "relative_path": str(a.get("relative_path", "")),
                    "route": str(a.get("route", "path_injection")),
                }
                for a in cleaned_attachments
            ]
        rows.append(item)
        if len(rows) > _MAX_MESSAGES_PER_SESSION:
            del rows[: len(rows) - _MAX_MESSAGES_PER_SESSION]


async def clear_messages(session_key: str) -> None:
    """Drop cached messages for one session."""
    async with _lock:
        _history.pop(session_key, None)


__all__ = [
    "ChatAttachmentMeta",
    "ChatHistoryMessage",
    "append_message",
    "clear_messages",
    "list_messages",
]
