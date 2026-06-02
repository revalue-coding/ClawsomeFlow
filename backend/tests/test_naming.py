"""Tests for app.scheduler.naming — convention helpers (DEV.md §5.5)."""

from __future__ import annotations

import pytest

from app.scheduler import naming


def test_team_name_for_run_strips_run_prefix() -> None:
    assert naming.team_name_for_run("run-abcdef123456").startswith("csflow-")


def test_team_name_for_run_truncates_to_8_hex() -> None:
    name = naming.team_name_for_run("run-0123456789abcdef")
    assert name == "csflow-01234567"


def test_team_name_for_run_handles_no_prefix() -> None:
    name = naming.team_name_for_run("0123456789abcdef")
    assert name == "csflow-01234567"


@pytest.mark.parametrize("team,agent,expected", [
    ("csflow-abc", "alice", "csflow-abc-alice"),
    ("t", "x", "t-x"),
])
def test_openclaw_session_id_for_run(team: str, agent: str, expected: str) -> None:
    assert naming.openclaw_session_id_for_run(team, agent) == expected


def test_openclaw_user_chat_session_id() -> None:
    assert naming.openclaw_user_chat_session_id("alice", "agA") == "user-chat-alice-agA"
