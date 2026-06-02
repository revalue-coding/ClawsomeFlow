"""Tests for app.scheduler.sessions.tmux_ready."""

from __future__ import annotations

import pytest

from app.scheduler.sessions import tmux_ready


@pytest.mark.asyncio
async def test_wait_tui_ready_succeeds_on_claude_prompt() -> None:
    capture = lambda target: _async_return("welcome to claude\n╭❯> ")
    ok = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_wait_tui_ready_succeeds_on_codex_prompt() -> None:
    capture = lambda target: _async_return("loading\ncodex> ")
    ok = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_wait_tui_ready_succeeds_on_cursor_agent_prompt() -> None:
    capture = lambda target: _async_return("loading\nagent> ")
    ok = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_wait_tui_ready_times_out() -> None:
    capture = lambda target: _async_return("nothing useful here")
    ok = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=0.2, poll_interval=0.05, capture=capture,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_wait_tui_ready_fails_fast_on_resume_fatal_signal() -> None:
    capture = lambda target: _async_return("No conversation found to continue")
    ok = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=5.0, poll_interval=0.01, capture=capture,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_wait_shell_ready_succeeds_on_bash_prompt() -> None:
    capture = lambda target: _async_return("user@host:~/$ ")
    ok = await tmux_ready.wait_shell_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert ok is True


@pytest.mark.asyncio
async def test_wait_shell_ready_succeeds_on_zsh_prompt() -> None:
    capture = lambda target: _async_return("hello\n%   ")
    # zsh uses %; should still match the % pattern
    ok = await tmux_ready.wait_shell_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert ok is True


# ── helpers -------------------------------------------------------------


async def _async_return(value):
    return value
