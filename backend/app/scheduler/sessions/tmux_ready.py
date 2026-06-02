"""tmux pane "ready" detection — small but critical helper.

Why this exists:

* After ``clawteam spawn tmux <cli>`` the tmux pane is created and the CLI
  binary is launched, but the CLI itself takes 1–10s to display its prompt
  (claude shows ``╭❯>``, codex shows ``codex>``, Cursor Agent often shows
  ``agent>`` / ``>``, bash shows ``$``).
* If we ``runtime inject`` (paste-buffer + Enter) before the prompt is up,
  the dispatch text is dropped on the floor — the CLI is still in startup.
* For OpenClaw we ``tmux send-keys`` into a bare bash; we still need to
  wait for the bash ``$`` prompt before sending the next command.

This helper is a Python port of ClawTeam's ``tmux_backend._wait_for_cli_ready``
(~30 lines): poll ``tmux capture-pane -p`` every 200ms and look for a
known prompt marker. Times out after ``timeout_sec`` and raises.

Public API:
* :func:`wait_tui_ready` — wait for an *agent* CLI prompt (claude/codex/cursor/...).
* :func:`wait_shell_ready` — wait for a bash/zsh/sh prompt.
* :func:`tmux_capture_pane` — small wrapper used by both, exposed for tests.
"""

from __future__ import annotations

import asyncio
import re

from app.logging_setup import get_logger

logger = get_logger("scheduler.tmux_ready")


# Prompt markers — kept liberal because CLI versions evolve; we only need
# to *know it's at a prompt*, not parse the prompt details.
_AGENT_PROMPT_PATTERNS = [
    re.compile(r"╭❯>"),                 # claude
    re.compile(r"^codex>", re.M),       # codex
    re.compile(r"^agent>", re.M),       # cursor agent
    re.compile(r"^>\s*$", re.M),        # cursor agent (minimal prompt)
    re.compile(r"gemini>"),             # gemini
    re.compile(r"^pi>", re.M),          # pi
    re.compile(r"^kimi>", re.M),        # kimi
    re.compile(r"^qwen>", re.M),        # qwen
    re.compile(r"opencode\s*>"),        # opencode
    re.compile(r"^\? "),                # nanobot
    re.compile(r"⚕"),                   # hermes (medical-staff icon in status line)
    re.compile(r"⏵|❯|⟫"),               # generic fancy prompts (also covers hermes input)
]
_AGENT_FATAL_PATTERNS = [
    # Typical resume failure when a TUI runtime has no persisted conversation.
    re.compile(r"No conversation found to continue", re.IGNORECASE),
]

_SHELL_PROMPT_PATTERNS = [
    re.compile(r"\$\s*$"),              # bash / sh
    re.compile(r"%\s*$"),               # zsh default
    re.compile(r"#\s*$"),               # root sh
    re.compile(r">\s*$"),               # cmd / generic
]


# ──────────────────────────────────────────────────────────────────────
# tmux invocation (kept as an injectable function for tests)
# ──────────────────────────────────────────────────────────────────────


async def tmux_capture_pane(target: str, *, history_lines: int = 60) -> str:
    """Run ``tmux capture-pane -p -S -N -t <target>`` and return its stdout.

    *target* is in tmux ``session:window.pane`` form (e.g.
    ``clawteam-csflow-abc:alice``). On any tmux failure (session gone /
    pane gone) returns an empty string instead of raising — the caller
    will simply keep polling until ``timeout_sec`` triggers.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux", "capture-pane", "-p", "-t", target, "-S", f"-{history_lines}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await proc.communicate()
    if proc.returncode != 0:
        return ""
    return out.decode("utf-8", errors="replace")


# ──────────────────────────────────────────────────────────────────────
# Wait helpers
# ──────────────────────────────────────────────────────────────────────


async def _wait_for(
    target: str,
    patterns: list[re.Pattern[str]],
    *,
    timeout_sec: float,
    poll_interval: float,
    capture: callable | None = None,
    label: str = "",
    fatal_patterns: list[re.Pattern[str]] | None = None,
) -> bool:
    """Poll *target* until one of *patterns* is in the captured output.

    Returns True on success; False if *timeout_sec* elapses without a match.
    """
    cap = capture or tmux_capture_pane
    deadline = asyncio.get_event_loop().time() + timeout_sec
    last_text = ""
    last_non_empty_text = ""
    attempts = 0
    while asyncio.get_event_loop().time() < deadline:
        attempts += 1
        text = await cap(target)
        last_text = text
        if text.strip():
            last_non_empty_text = text
        for pat in patterns:
            if pat.search(text):
                logger.debug(
                    "tmux_pane_ready",
                    target=target, label=label, attempts=attempts,
                )
                return True
        for pat in (fatal_patterns or []):
            if pat.search(text):
                logger.warning(
                    "tmux_pane_ready_fatal_signal",
                    target=target,
                    label=label,
                    attempts=attempts,
                    pattern=pat.pattern,
                    tail=text[-200:],
                )
                return False
        await asyncio.sleep(poll_interval)
    logger.warning(
        "tmux_pane_not_ready",
        target=target, label=label, attempts=attempts,
        tail=(last_non_empty_text or last_text)[-200:],
    )
    return False


async def wait_tui_ready(
    target: str,
    *,
    timeout_sec: float = 30.0,
    poll_interval: float = 0.25,
    capture: callable | None = None,
) -> bool:
    """Wait for a TUI-CLI agent's prompt to appear in *target* tmux pane."""
    return await _wait_for(
        target, _AGENT_PROMPT_PATTERNS,
        timeout_sec=timeout_sec, poll_interval=poll_interval,
        capture=capture,
        label="agent_prompt",
        fatal_patterns=_AGENT_FATAL_PATTERNS,
    )


async def wait_shell_ready(
    target: str,
    *,
    timeout_sec: float = 10.0,
    poll_interval: float = 0.2,
    capture: callable | None = None,
) -> bool:
    """Wait for a bash/zsh/sh prompt to appear in *target* tmux pane."""
    return await _wait_for(
        target, _SHELL_PROMPT_PATTERNS,
        timeout_sec=timeout_sec, poll_interval=poll_interval,
        capture=capture, label="shell_prompt",
    )


__all__ = [
    "tmux_capture_pane",
    "wait_shell_ready",
    "wait_tui_ready",
]
