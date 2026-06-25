"""Persistent Hermes direct-chat session bindings.

Hermes ``-z`` chat turns are one-shot processes, but Hermes stores the logical
conversation in its own session DB.  The WebUI keeps a per-(user, agent) binding
here so future turns can use ``--resume <session_id>`` instead of the ambiguous
"most recent session" ``-c`` behavior.
"""

from __future__ import annotations

import json
from typing import Any

from app import paths
from app.fileutil import atomic_write_json, file_locked


_STATE_FILE = "hermes-chat-sessions.json"


def _state_path():
    return paths.system_dir() / _STATE_FILE


def _load_unlocked() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"sessions": {}}
    try:
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"sessions": {}}
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    return {"sessions": sessions if isinstance(sessions, dict) else {}}


def get_session_id(session_key: str) -> str | None:
    """Return the Hermes session id bound to a WebUI chat, if any."""
    with file_locked(_state_path()):
        value = _load_unlocked()["sessions"].get(session_key)
    return value if isinstance(value, str) and value.strip() else None


def set_session_id(session_key: str, hermes_session_id: str) -> None:
    """Persist a WebUI chat -> Hermes session binding."""
    sid = (hermes_session_id or "").strip()
    if not sid:
        return
    path = _state_path()
    with file_locked(path):
        data = _load_unlocked()
        data["sessions"][session_key] = sid
        atomic_write_json(path, data, indent=2, sort_keys=True)


def clear_session_id(session_key: str) -> str | None:
    """Forget a binding and return the removed Hermes session id, if present."""
    path = _state_path()
    with file_locked(path):
        data = _load_unlocked()
        old = data["sessions"].pop(session_key, None)
        atomic_write_json(path, data, indent=2, sort_keys=True)
    return old if isinstance(old, str) and old.strip() else None


__all__ = ["clear_session_id", "get_session_id", "set_session_id"]
