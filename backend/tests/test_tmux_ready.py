"""Tests for app.scheduler.sessions.tmux_ready."""

from __future__ import annotations

import pytest

from app.scheduler.sessions import tmux_ready


@pytest.mark.asyncio
async def test_wait_tui_ready_succeeds_on_claude_prompt() -> None:
    capture = lambda target: _async_return("welcome to claude\n╭❯> ")
    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert result.ok is True
    assert result.reason_code == "composer_ready"


@pytest.mark.asyncio
async def test_wait_tui_ready_succeeds_on_codex_prompt() -> None:
    capture = lambda target: _async_return("loading\ncodex> ")
    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_wait_tui_ready_succeeds_on_modern_codex_tui() -> None:
    pane = (
        "╭───────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.136.0)    │\n"
        "│ model:       gpt-5.3-codex    │\n"
        "│ permissions: YOLO mode        │\n"
        "╰───────────────────────────────╯\n"
        "  Tip: New Build faster with Codex.\n"
        "› Explain this codebase\n"
        "gpt-5.3-codex xhigh · ~/work\n"
    )
    capture = lambda target: _async_return(pane)
    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_wait_tui_ready_succeeds_on_cursor_agent_prompt() -> None:
    capture = lambda target: _async_return("loading\nagent> ")
    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert result.ok is True


@pytest.mark.asyncio
async def test_wait_tui_ready_dismisses_cursor_workspace_trust_prompt() -> None:
    trust_pane = (
        "  ╭──────────────────────────────────────────────────────────────────────────╮\n"
        "  │                                                                          │\n"
        "  │  ⚠ Workspace Trust Required                                              │\n"
        "  │                                                                          │\n"
        "  │  Cursor Agent can execute code and access files in this directory.       │\n"
        "  │                                                                          │\n"
        "  │  Do you trust the contents of this directory?                            │\n"
        "  │                                                                          │\n"
        "  │    /home/user/project                                                    │\n"
        "  │                                                                          │\n"
        "  │  ▶ [a] Trust this workspace                                              │\n"
        "  │    [q] Quit                                                              │\n"
        "  │                                                                          │\n"
        "  │  Use arrow keys to navigate, Enter to select, or press the key shown     │\n"
        "  │                                                                          │\n"
        "  ╰──────────────────────────────────────────────────────────────────────────╯\n"
    )
    ready_pane = (
        "Cursor Agent\n"
        "v2026.06.19-20-24-33-653a7fb\n"
        "Use subagents to parallelize work and preserve context.\n"
        "\n"
        "→ Plan, search, build anything\n"
        "\n"
        "Composer 2.5 Fast                                             Run Everything\n"
        "/home/user/project · abc123\n"
    )
    panes = iter([trust_pane, ready_pane])
    sent: list[tuple[str, str, bool]] = []

    async def capture(_target: str) -> str:
        return next(panes)

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "team:cursor",
        trust_platform="cursor",
        timeout_sec=2.0,
        poll_interval=0.01,
        capture=capture,
        send_keys=send_keys,
    )

    assert result.ok is True
    assert sent == [("team:cursor", "a", False)]


@pytest.mark.asyncio
async def test_wait_tui_ready_accepts_cursor_model_action_tail() -> None:
    pane = (
        "\n"
        "  \u2192 Plan, search, build anything\n"
        "\n"
        "\n"
        "  GPT-5.5 1M High                                               Run Everything\n"
        "  ~/.clawteam/workspaces/csflow-2d4168f9/tester \u00b7\n"
        "  clawteam/csflow-2d4168f9/tester\n"
        "\n"
    )
    capture = lambda target: _async_return(pane)
    result = await tmux_ready.wait_tui_ready(
        "team:cursor",
        trust_platform="cursor",
        timeout_sec=1.0,
        poll_interval=0.01,
        capture=capture,
    )

    assert result.ok is True
    assert result.reason_code == "composer_ready"


@pytest.mark.asyncio
async def test_wait_tui_ready_ignores_stale_cursor_trust_text_in_scrollback() -> None:
    pane = (
        "Workspace Trust Required\n"
        "Cursor Agent can execute code and access files in this directory.\n"
        "Do you trust the contents of this directory?\n"
        "▶ [a] Trust this workspace\n"
        "Use arrow keys to navigate, Enter to select, or press the key shown\n"
        + "\n" * 25
        + "Cursor Agent\n"
        "→ Plan, search, build anything\n"
        "Composer 2.5 Fast                                             Run Everything\n"
        "/home/user/project · abc123\n"
    )
    sent: list[tuple[str, str, bool]] = []

    async def capture(_target: str) -> str:
        return pane

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "team:cursor",
        trust_platform="cursor",
        timeout_sec=1.0,
        poll_interval=0.01,
        capture=capture,
        send_keys=send_keys,
    )

    assert result.ok is True
    assert sent == []


@pytest.mark.asyncio
async def test_wait_tui_ready_times_out() -> None:
    capture = lambda target: _async_return("nothing useful here")
    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=0.2, poll_interval=0.05, capture=capture,
    )
    assert result.ok is False
    assert result.reason_code == "tui_timeout"


@pytest.mark.asyncio
async def test_wait_tui_ready_fails_fast_on_resume_fatal_signal() -> None:
    capture = lambda target: _async_return("No conversation found to continue")
    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=5.0, poll_interval=0.01, capture=capture,
    )
    assert result.ok is False
    assert result.reason_code == "fatal_signal"


@pytest.mark.asyncio
async def test_wait_tui_ready_dismisses_codex_trust_then_succeeds() -> None:
    trust_pane = (
        "Do you trust the contents of this directory?\n"
        "Press Enter to continue\n"
        "❯ Yes\n"
    )
    ready_pane = (
        "╭───────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.139.0)    │\n"
        "╰───────────────────────────────╯\n"
        "› Explain this codebase\n"
    )
    panes = iter([trust_pane, ready_pane])

    async def capture(_target: str) -> str:
        return next(panes)

    sent: list[tuple[str, str, bool]] = []

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "team:codex",
        trust_platform="codex",
        timeout_sec=2.0,
        poll_interval=0.01,
        capture=capture,
        send_keys=send_keys,
    )

    assert result.ok is True
    assert sent == [("team:codex", "Enter", False)]


@pytest.mark.asyncio
async def test_wait_tui_ready_does_not_match_trust_prompt_as_composer() -> None:
    trust_pane = (
        "Do you trust the contents of this directory?\n"
        "Press enter to continue\n"
        "❯ 1. Yes, continue\n"
    )
    capture = lambda target: _async_return(trust_pane)

    async def noop_send_keys(*args, **kwargs) -> None:
        del args, kwargs

    result = await tmux_ready.wait_tui_ready(
        "x:y",
        trust_platform="codex",
        timeout_sec=0.25,
        poll_interval=0.05,
        capture=capture,
        send_keys=noop_send_keys,
    )

    assert result.ok is False
    assert result.reason_code == "folder_trust_failed"


@pytest.mark.asyncio
async def test_wait_tui_ready_dismisses_claude_skip_permissions_prompt() -> None:
    skip_pane = (
        "Use dangerously-skip-permissions?\n"
        "Yes, I accept the risk\n"
        "❯ No\n"
    )
    ready_pane = "welcome\n╭❯> "
    panes = iter([skip_pane, ready_pane])
    sent: list[tuple[str, str, bool]] = []

    async def capture(_target: str) -> str:
        return next(panes)

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "team:alice",
        trust_platform="claude",
        timeout_sec=2.0,
        poll_interval=0.01,
        capture=capture,
        send_keys=send_keys,
    )

    assert result.ok is True
    assert sent == [
        ("team:alice", "\x1b[B", True),
        ("team:alice", "Enter", False),
    ]


@pytest.mark.asyncio
async def test_wait_tui_ready_dismisses_codex_update_prompt() -> None:
    update_pane = (
        "Update available! 0.139.0 -> 0.141.0\n"
        "› 1. Update now\n"
        "  2. Skip\n"
        "Press enter to continue\n"
    )
    ready_pane = (
        "╭───────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.139.0)    │\n"
        "╰───────────────────────────────╯\n"
        "› Explain this codebase\n"
    )
    panes = iter([update_pane, ready_pane])
    sent: list[tuple[str, str, bool]] = []

    async def capture(_target: str) -> str:
        return next(panes)

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "team:codex",
        trust_platform="codex",
        timeout_sec=2.0,
        poll_interval=0.01,
        capture=capture,
        send_keys=send_keys,
    )

    assert result.ok is True
    assert sent == [("team:codex", "Enter", False)]


@pytest.mark.asyncio
async def test_wait_tui_ready_ignores_stale_trust_text_in_scrollback() -> None:
    pane = (
        "Do you trust the contents of this directory?\n"
        "Press enter to continue\n"
        "❯ 1. Yes, continue\n"
        + "\n" * 20
        + "╭───────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.139.0)    │\n"
        "╰───────────────────────────────╯\n"
        "› Explain this codebase\n"
    )
    capture = lambda target: _async_return(pane)
    sent: list[tuple[str, str, bool]] = []

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "x:y",
        trust_platform="codex",
        timeout_sec=1.0,
        poll_interval=0.01,
        capture=capture,
        send_keys=send_keys,
    )

    assert result.ok is True
    assert sent == []


@pytest.mark.asyncio
async def test_resolve_trust_platform_includes_cursor_by_explicit_agent_kind() -> None:
    assert tmux_ready.resolve_trust_platform(agent_kind="claude") == "claude"
    assert tmux_ready.resolve_trust_platform(agent_kind="codex") == "codex"
    assert tmux_ready.resolve_trust_platform(agent_kind="cursor") == "cursor"
    assert tmux_ready.resolve_trust_platform(agent_kind="gemini") == "gemini"
    assert tmux_ready.resolve_trust_platform(agent_kind="kimi") is None
    assert tmux_ready.resolve_trust_platform(agent_kind="codebuddy") is None
    assert tmux_ready.resolve_trust_platform(spawn_command=["agent", "--force"]) is None
    assert tmux_ready.resolve_trust_platform(spawn_command=["cursor"]) is None
    assert tmux_ready.resolve_trust_platform(spawn_command=["gemini", "--yolo"]) == "gemini"
    assert tmux_ready.resolve_trust_platform(spawn_command=["qodercli"]) is None


@pytest.mark.asyncio
async def test_wait_tui_ready_skips_startup_handling_without_platform() -> None:
    trust_pane = (
        "Do you trust the contents of this directory?\n"
        "Press enter to continue\n"
        "❯ 1. Yes, continue\n"
    )
    capture = lambda target: _async_return(trust_pane)
    sent: list[tuple[str, str, bool]] = []

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "x:y",
        trust_platform=None,
        timeout_sec=0.25,
        poll_interval=0.05,
        capture=capture,
        send_keys=send_keys,
    )

    assert sent == []
    assert result.ok is False
    assert result.reason_code == "tui_timeout"


@pytest.mark.asyncio
async def test_wait_tui_ready_dismisses_gemini_trust_prompt() -> None:
    trust_pane = "Please trust folder before continuing\n trust parent folder \n"
    ready_pane = "loading\ngemini> "
    panes = iter([trust_pane, ready_pane])
    sent: list[tuple[str, str, bool]] = []

    async def capture(_target: str) -> str:
        return next(panes)

    async def send_keys(target: str, keys: str, *, literal: bool = False) -> None:
        sent.append((target, keys, literal))

    result = await tmux_ready.wait_tui_ready(
        "team:gem",
        trust_platform="gemini",
        timeout_sec=2.0,
        poll_interval=0.01,
        capture=capture,
        send_keys=send_keys,
    )

    assert result.ok is True
    assert sent == [("team:gem", "Enter", False)]


@pytest.mark.asyncio
async def test_gemini_trust_skipped_on_oauth_screen() -> None:
    pane = (
        "Please visit the following URL to authorize the application:\n"
        "https://accounts.google.com/o/oauth2/v2/auth?...\n"
        "Enter the authorization code:\n"
    )
    assert tmux_ready._looks_like_workspace_trust_prompt(pane, "gemini") is False
    assert tmux_ready._startup_prompt_action(pane, "gemini") is None


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
    ok = await tmux_ready.wait_shell_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
    )
    assert ok is True


# ── helpers -------------------------------------------------------------


async def _async_return(value):
    return value
