from __future__ import annotations

from pathlib import Path

import pytest

from app.integrations.clawteam_cli import CliInvocationError
from app.models import AgentKind, FlowAgent, MergeStrategy, OnFailure
from app.scheduler.sessions.openclaw_tmux import (
    OpenClawTmuxSession,
    _INLINE_LONG_MESSAGE_THRESHOLD_CHARS,
)


def _openclaw_agent() -> FlowAgent:
    return FlowAgent(
        id="sci",
        kind=AgentKind.openclaw,
        is_leader=False,
        merge_strategy=MergeStrategy.agent_self,
        on_failure=OnFailure.retry,
        max_retries=2,
    )


@pytest.mark.asyncio
async def test_dispatch_uses_message_file_substitution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = OpenClawTmuxSession(
        agent=_openclaw_agent(),
        team_name="csflow-abc123",
        run_id="run-abc123",
        agent_main_repo=str(tmp_path),
        host_platform="Darwin",
    )
    payload_file = tmp_path / "payload with spaces.txt"
    sent: list[tuple[str, bool]] = []

    def fake_write(message: str) -> str:
        payload_file.write_text(message, encoding="utf-8")
        return str(payload_file)

    async def fake_send(payload: str, *, literal: bool = True) -> None:
        sent.append((payload, literal))

    monkeypatch.setattr(session, "_write_dispatch_message_file", fake_write)
    monkeypatch.setattr(session, "_send_keys", fake_send)

    message = "line1\nline2 with 'quotes' and \"double\" and $(shell)"
    await session._do_dispatch(message=message, task_id="task-1")

    assert len(sent) == 2
    line, literal = sent[0]
    assert literal is True
    assert sent[1] == ("Enter", False)
    assert '--message "$(cat "$__csflow_msg_file")"' in line
    assert "payload with spaces.txt" in line
    assert "rm -f \"$__csflow_msg_file\"" in line
    # Ensure we no longer inline the full prompt in the shell command.
    assert message not in line
    assert payload_file.read_text(encoding="utf-8") == message


@pytest.mark.asyncio
async def test_dispatch_uses_inline_message_on_linux(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = OpenClawTmuxSession(
        agent=_openclaw_agent(),
        team_name="csflow-abc123",
        run_id="run-abc123",
        agent_main_repo=str(tmp_path),
        host_platform="Linux",
    )
    sent: list[tuple[str, bool]] = []

    def _should_not_write(_message: str) -> str:
        raise AssertionError("linux path should not write message temp file")

    async def fake_send(payload: str, *, literal: bool = True) -> None:
        sent.append((payload, literal))

    monkeypatch.setattr(session, "_write_dispatch_message_file", _should_not_write)
    monkeypatch.setattr(session, "_send_keys", fake_send)

    message = "line1\nline2 with 'quotes'"
    await session._do_dispatch(message=message, task_id="task-1")

    assert len(sent) == 2
    line, literal = sent[0]
    assert literal is True
    assert sent[1] == ("Enter", False)
    assert "--message" in line
    # Linux keeps the historical inline quoted payload strategy.
    assert message not in line  # quoted/escaped form instead of raw payload
    assert "__csflow_msg_file" not in line


@pytest.mark.asyncio
async def test_dispatch_injection_failure_cleans_temp_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = OpenClawTmuxSession(
        agent=_openclaw_agent(),
        team_name="csflow-abc123",
        run_id="run-abc123",
        agent_main_repo=str(tmp_path),
        host_platform="Darwin",
    )
    payload_file = tmp_path / "payload.txt"
    calls: list[tuple[str, bool]] = []

    def fake_write(message: str) -> str:
        payload_file.write_text(message, encoding="utf-8")
        return str(payload_file)

    async def fake_send(payload: str, *, literal: bool = True) -> None:
        calls.append((payload, literal))
        raise CliInvocationError(
            argv=["tmux", "send-keys"],
            exit_code=1,
            stderr="tmux send failed",
        )

    monkeypatch.setattr(session, "_write_dispatch_message_file", fake_write)
    monkeypatch.setattr(session, "_send_keys", fake_send)

    with pytest.raises(CliInvocationError):
        await session._do_dispatch(message="hello", task_id="task-1")

    assert len(calls) == 1
    assert not payload_file.exists()


def test_long_message_threshold_definition() -> None:
    session = OpenClawTmuxSession(
        agent=_openclaw_agent(),
        team_name="csflow-abc123",
        run_id="run-abc123",
        agent_main_repo="/tmp",
        host_platform="Linux",
    )
    assert session._is_message_long("x" * (_INLINE_LONG_MESSAGE_THRESHOLD_CHARS - 1)) is False
    assert session._is_message_long("x" * _INLINE_LONG_MESSAGE_THRESHOLD_CHARS) is True
