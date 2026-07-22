"""Persisted chat-history store for the single-agent direct chat UI.

Shared by both the OpenClaw and Hermes chat pages. History is stored in SQLite
(``ChatMessageRow``) so it survives service restarts and a ``重置对话`` (reset)
can keep the prior conversation, separated by a ``session_divider`` row.

This is decoupled from the agent runtime context:

* OpenClaw context is maintained by the runtime ``--session-id`` server-side.
* Hermes context is maintained by its own session store (``--resume``).
* We do NOT replay this history to the agent on each turn; it exists for UI
  rendering. The ``conversation_key`` here is intentionally **revision-free**
  (see ``naming.*_user_chat_session_id``) so a reset that rotates the runtime
  session id does not hide the persisted transcript.
"""

from __future__ import annotations

import time
from typing import TypedDict

from app.models import ChatMessageRow
from app.storage import get_storage

# Persistent divider row inserted on reset ("start a new session, keep history").
SESSION_DIVIDER_KIND = "session_divider"

# Trailing window returned to the UI. History itself is never capped on write —
# the user wants it retained across restarts — but the read path is bounded so a
# very long transcript does not produce an unbounded payload.
_HISTORY_WINDOW = 500


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
    # Epoch milliseconds when the message was recorded. The UI renders this as a
    # per-message timestamp; it is the authoritative source (the client can only
    # guess a time for messages it streamed itself, and cannot for recovered
    # ones). Absent on rows appended before this field existed → the UI falls
    # back to its local cache / omits the time.
    ts: int
    # Server-assigned stable id (autoincrement). The UI keys render + dedup off
    # this so a windowed local cache reconciled against the full server list
    # can never render the same message twice.
    id: int
    # "" for a normal message, "session_divider" for the persistent reset marker.
    kind: str


def _clean_attachments(raw: object) -> list[ChatAttachmentMeta]:
    if not isinstance(raw, list):
        return []
    return [
        {
            "id": str(a.get("id", "")),
            "name": str(a.get("name", "")),
            "mime_type": str(a.get("mime_type", "")),
            "size_bytes": int(a.get("size_bytes", 0) or 0),
            "absolute_path": str(a.get("absolute_path", "")),
            "relative_path": str(a.get("relative_path", "")),
            "route": str(a.get("route", "path_injection")),
        }
        for a in raw
        if isinstance(a, dict)
    ]


def _row_to_message(row: ChatMessageRow) -> ChatHistoryMessage:
    item: ChatHistoryMessage = {"role": row.role, "content": row.content}
    if isinstance(row.id, int):
        item["id"] = row.id
    if isinstance(row.ts, int) and row.ts:
        item["ts"] = row.ts
    if row.kind:
        item["kind"] = row.kind
    cleaned = _clean_attachments(row.attachments)
    if cleaned:
        item["attachments"] = cleaned
    return item


async def list_messages(conversation_key: str) -> list[ChatHistoryMessage]:
    """Return the trailing window of persisted messages for one conversation."""
    rows = get_storage().chat_message_list(
        conversation_key=conversation_key, limit=_HISTORY_WINDOW
    )
    return [_row_to_message(r) for r in rows]


async def append_message(
    conversation_key: str,
    *,
    role: str,
    content: str,
    attachments: list[ChatAttachmentMeta] | None = None,
) -> None:
    """Append one message to the persisted history (best-effort)."""
    cleaned_attachments = attachments or []
    if not content and not cleaned_attachments:
        return
    row = ChatMessageRow(
        conversation_key=conversation_key,
        role=role,
        kind="",
        content=content,
        attachments=[dict(a) for a in cleaned_attachments],
        ts=int(time.time() * 1000),
    )
    get_storage().chat_message_append(row)


async def append_divider(conversation_key: str) -> None:
    """Append a persistent "new session" divider (reset keeps history)."""
    row = ChatMessageRow(
        conversation_key=conversation_key,
        role="system",
        kind=SESSION_DIVIDER_KIND,
        content="",
        attachments=[],
        ts=int(time.time() * 1000),
    )
    get_storage().chat_message_append(row)


async def clear_messages(conversation_key: str) -> None:
    """Delete all persisted messages for one conversation (genuine deletion)."""
    get_storage().chat_message_delete_conversation(conversation_key=conversation_key)


async def drop_trailing_unanswered_user(conversation_key: str) -> bool:
    """Drop a trailing normal user row that never got an assistant reply.

    Chat handlers append the user message *before* the agent runs; on delivery
    failure the assistant is never written. The next send must remove that
    orphan so it cannot resurface via ``chat-history`` reconcile or bias
    Hermes ``resume`` (which keys off non-empty UI history). Never removes a
    ``session_divider`` (guarded in storage), so a reset marker survives.
    """
    return get_storage().chat_message_pop_trailing_user(conversation_key=conversation_key)


__all__ = [
    "SESSION_DIVIDER_KIND",
    "ChatAttachmentMeta",
    "ChatHistoryMessage",
    "append_divider",
    "append_message",
    "clear_messages",
    "drop_trailing_unanswered_user",
    "list_messages",
]
