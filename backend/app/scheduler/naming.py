"""Naming conventions used across the scheduler (DEV.md §5.5)."""

from __future__ import annotations


_TEAM_NAME_RUN_ID_LEN = 8


def team_name_for_run(run_id: str) -> str:
    """ClawTeam team name = ``csflow-{run_id_short}`` (per DEV.md §5.5).

    The first ``_TEAM_NAME_RUN_ID_LEN`` characters of the Run id (UUID hex
    fragment after the ``run-`` prefix) are sufficient for cross-Run
    uniqueness while staying within tmux session-name length limits.
    """
    short = _short(run_id)
    return f"csflow-{short}"


def openclaw_session_id_for_run(team_name: str, agent_id: str) -> str:
    """OpenClaw Flow-dispatch session id (DEV.md §5.5)."""
    return f"{team_name}-{agent_id}"


def openclaw_user_chat_session_id(user: str, agent_id: str) -> str:
    """OpenClaw user-direct chat session id (DEV.md §5.5)."""
    return f"user-chat-{user}-{agent_id}"


def hermes_user_chat_session_id(user: str, agent_id: str) -> str:
    """Hermes user-direct chat history-cache key (UI display only).

    Hermes ``-z`` turns carry no session id; this only keys the in-process
    chat-history cache so the WebUI can render a conversation per user × agent.
    """
    return f"hermes-user-chat-{user}-{agent_id}"


def _short(run_id: str) -> str:
    """Return the suffix portion of a Run id usable in resource names.

    ``Run.id`` is already produced by ``models._new_id("run")`` →
    ``run-<12hex>``. We strip the prefix and keep ``_TEAM_NAME_RUN_ID_LEN``
    characters so the resulting team name fits in 31 chars (tmux limit).
    """
    if "-" in run_id:
        run_id = run_id.split("-", 1)[1]
    return run_id[:_TEAM_NAME_RUN_ID_LEN]


__all__ = [
    "hermes_user_chat_session_id",
    "openclaw_session_id_for_run",
    "openclaw_user_chat_session_id",
    "team_name_for_run",
]
