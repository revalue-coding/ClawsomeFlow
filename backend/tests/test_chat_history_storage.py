"""Persisted chat-history storage (``ChatMessageRow``) + the DB-backed
``openclaw_chat_history`` service.

Covers the fixes for the single-agent chat UI:
* history survives a service restart (it is persisted, not in-memory);
* reset keeps the transcript and inserts a ``session_divider`` boundary;
* ``drop_trailing_unanswered_user`` never eats a divider.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import ChatMessageRow
from app.services import openclaw_chat_history as chat_history
from app.storage import get_storage, reset_storage
from app.storage.sqlite import SqliteStorage


def test_chatmessagerow_table_created_on_init(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    store = SqliteStorage(url=url)
    store.init_schema()
    with store._engine.begin() as conn:  # noqa: SLF001 - test reaches into engine
        tables = {
            str(r[0])
            for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "chatmessagerow" in tables
    # Idempotent: a second init must not raise.
    store.init_schema()
    store.close()


def test_chat_message_round_trip_and_pop(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'chat.db'}"
    store = SqliteStorage(url=url)
    store.init_schema()

    key = "user-chat-alice-agent1"
    store.chat_message_append(ChatMessageRow(conversation_key=key, role="user", content="hi", ts=1))
    store.chat_message_append(
        ChatMessageRow(conversation_key=key, role="assistant", content="hello", ts=2)
    )
    rows = store.chat_message_list(conversation_key=key)
    assert [(r.role, r.content) for r in rows] == [("user", "hi"), ("assistant", "hello")]
    assert all(isinstance(r.id, int) for r in rows)

    # A different conversation is isolated.
    assert store.chat_message_list(conversation_key="other") == []

    # pop removes only a trailing normal user row.
    store.chat_message_append(ChatMessageRow(conversation_key=key, role="user", content="q", ts=3))
    assert store.chat_message_pop_trailing_user(conversation_key=key) is True
    assert store.chat_message_pop_trailing_user(conversation_key=key) is False  # last is assistant

    store.close()


def test_pop_trailing_user_never_eats_divider(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'div.db'}"
    store = SqliteStorage(url=url)
    store.init_schema()
    key = "user-chat-alice-agent1"
    store.chat_message_append(ChatMessageRow(conversation_key=key, role="user", content="hi", ts=1))
    store.chat_message_append(
        ChatMessageRow(
            conversation_key=key, role="system", kind="session_divider", content="", ts=2
        )
    )
    # Trailing row is a divider → must NOT be popped.
    assert store.chat_message_pop_trailing_user(conversation_key=key) is False
    assert len(store.chat_message_list(conversation_key=key)) == 2
    store.close()


@pytest.mark.asyncio
async def test_service_persists_across_restart() -> None:
    """The service layer writes through to SQLite, so a fresh storage singleton
    (simulating a restart) still sees the transcript."""
    reset_storage()
    key = "user-chat-alice-agent1"
    await chat_history.append_message(key, role="user", content="remember me")
    await chat_history.append_message(key, role="assistant", content="ok")

    # Simulate a service restart: drop + rebuild the storage singleton.
    reset_storage()
    get_storage()
    rows = await chat_history.list_messages(key)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "remember me"),
        ("assistant", "ok"),
    ]
    reset_storage()


@pytest.mark.asyncio
async def test_reset_divider_keeps_history() -> None:
    reset_storage()
    key = "user-chat-alice-agent1"
    await chat_history.append_message(key, role="user", content="turn1")
    await chat_history.append_message(key, role="assistant", content="reply1")
    # Reset appends a divider instead of clearing.
    await chat_history.append_divider(key)
    rows = await chat_history.list_messages(key)
    assert len(rows) == 3
    assert rows[0]["content"] == "turn1"
    assert rows[-1].get("kind") == chat_history.SESSION_DIVIDER_KIND
    # A subsequent send drops no divider (orphan-guard skips it).
    dropped = await chat_history.drop_trailing_unanswered_user(key)
    assert dropped is False
    assert len(await chat_history.list_messages(key)) == 3
    reset_storage()
