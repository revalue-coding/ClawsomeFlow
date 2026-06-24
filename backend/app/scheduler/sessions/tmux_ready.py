"""tmux pane "ready" detection — small but critical helper.

Why this exists:

* After ``clawteam spawn tmux <cli>`` the tmux pane is created and the CLI
  binary is launched, but the CLI itself takes 1–10s to display its prompt
  (claude shows ``╭❯>``, modern codex shows a ``>_ OpenAI Codex`` banner and a
  ``›`` composer, Cursor Agent often shows ``agent>`` / ``>``, bash shows
  ``$``).
* If we ``runtime inject`` (paste-buffer + Enter) before the prompt is up,
  the dispatch text is dropped on the floor — the CLI is still in startup.
* For OpenClaw we ``tmux send-keys`` into a bare bash; we still need to
  wait for the bash ``$`` prompt before sending the next command.

ClawTeam's spawn path runs ``_confirm_workspace_trust_if_prompted`` **only**
for **claude**, **codex**, and **gemini** (plus Claude skip-permissions and
Codex update gate). We keep that scope intact and add one ClawsomeFlow-local
Cursor special case for its explicit workspace-trust menu, which cannot be
bypassed by ``--force``/``--approve-mcps`` in interactive TUI mode. All other
platforms skip startup prompt handling (qoder/codebuddy rely on
``temp_agent_trust`` seeding instead).

Public API:
* :data:`TRUST_HANDLED_PLATFORMS` — platforms with startup-prompt fallback.
* :func:`resolve_trust_platform` — map agent kind / spawn argv → platform or None.
* :class:`TuiReadyResult` — structured outcome from :func:`wait_tui_ready`.
* :func:`wait_tui_ready` — wait for an *agent* CLI prompt (claude/codex/cursor/...).
* :func:`wait_shell_ready` — wait for a bash/zsh/sh prompt.
* :func:`tmux_capture_pane` — small wrapper used by both, exposed for tests.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger("scheduler.tmux_ready")

# ClawTeam ``_confirm_workspace_trust_if_prompted`` scope (tmux_backend.py).
_CLAWTEAM_TRUST_HANDLED_PLATFORMS: frozenset[str] = frozenset({"claude", "codex", "gemini"})

# Cursor is deliberately a ClawsomeFlow-local exception, not a ClawTeam behavior
# change. Only explicit ``AgentKind.cursor`` enables it; a binary named
# ``agent``/``cursor`` is not inferred as Cursor from argv alone.
_CURSOR_TRUST_PLATFORM = "cursor"
TRUST_HANDLED_PLATFORMS: frozenset[str] = (
    _CLAWTEAM_TRUST_HANDLED_PLATFORMS | frozenset({_CURSOR_TRUST_PLATFORM})
)


@dataclass(frozen=True, slots=True)
class TuiReadyResult:
    """Outcome of :func:`wait_tui_ready`."""

    ok: bool
    reason_code: str = ""
    message: str = ""
    pane_tail: str = ""


def resolve_trust_platform(
    *,
    agent_kind: str | None = None,
    spawn_command: Sequence[str] | None = None,
) -> str | None:
    """Return the trust-handling platform key, or None when ClawTeam skips it."""
    if agent_kind == _CURSOR_TRUST_PLATFORM:
        return _CURSOR_TRUST_PLATFORM
    if agent_kind in _CLAWTEAM_TRUST_HANDLED_PLATFORMS:
        return agent_kind
    if spawn_command:
        exe = Path(spawn_command[0]).name.lower()
        if exe in _CLAWTEAM_TRUST_HANDLED_PLATFORMS:
            return exe
    return None


# Prompt markers — kept liberal because CLI versions evolve; we only need
# to *know it's at a prompt*, not parse the prompt details.
_AGENT_PROMPT_PATTERNS = [
    re.compile(r"╭❯>"),                 # claude
    re.compile(r"^codex>", re.M),       # codex (legacy prompt)
    re.compile(r"OpenAI Codex \(v"),    # codex (modern banner)
    re.compile(r"^agent>", re.M),       # cursor agent
    re.compile(r"^>\s*$", re.M),        # cursor agent (minimal prompt)
    re.compile(r"gemini>"),             # gemini
    re.compile(r"^pi>", re.M),          # pi
    re.compile(r"^kimi>", re.M),        # kimi
    re.compile(r"^qwen>", re.M),        # qwen
    re.compile(r"opencode\s*>"),        # opencode
    re.compile(r"^\? "),                # nanobot
    re.compile(r"⚕"),                   # hermes (medical-staff icon in status line)
    re.compile(r"⏵|❯|⟫|›"),             # generic fancy prompts (› = codex/modern composer)
]
_AGENT_FATAL_PATTERNS = [
    re.compile(r"No conversation found to continue", re.IGNORECASE),
]

_SHELL_PROMPT_PATTERNS = [
    re.compile(r"\$\s*$"),
    re.compile(r"%\s*$"),
    re.compile(r"#\s*$"),
    re.compile(r">\s*$"),
]

# Trust dialogs are dismissed with Enter; stale scrollback can still contain the
# prompt text after the composer appears. Match only the visible tail.
_ACTIVE_PANE_LINES = 25


def _pane_active_text(pane_text: str, *, lines: int = _ACTIVE_PANE_LINES) -> str:
    """Return the bottom *lines* of captured pane output (currently visible area)."""
    if not pane_text:
        return ""
    parts = pane_text.splitlines()
    if len(parts) <= lines:
        return pane_text
    return "\n".join(parts[-lines:])


# ──────────────────────────────────────────────────────────────────────
# Startup confirmation (mirrors ClawTeam tmux_backend, platform-scoped)
# ──────────────────────────────────────────────────────────────────────


def _looks_like_workspace_trust_prompt(pane_text: str, platform: str) -> bool:
    """Return True when the tmux pane is showing a folder-trust dialog."""
    lower = pane_text.lower()
    if not lower.strip():
        return False

    if platform == "claude":
        return ("trust this folder" in lower or "trust the contents" in lower) and (
            "enter to confirm" in lower
            or "press enter" in lower
            or "enter to continue" in lower
        )

    if platform == "codex":
        return (
            "trust the contents of this directory" in lower
            and "press enter to continue" in lower
        )

    if platform == "gemini":
        # OAuth / login gates are not folder-trust dialogs; ClawTeam cannot auto-dismiss
        # these either — do not spam Enter while waiting for an auth code.
        if "authorization code" in lower or "oauth2" in lower:
            return False
        return "trust folder" in lower or "trust parent folder" in lower

    if platform == "cursor":
        return (
            "workspace trust required" in lower
            and "cursor agent can execute code" in lower
            and "do you trust the contents of this directory" in lower
            and "trust this workspace" in lower
            and "press the key shown" in lower
        )

    return False


def _looks_like_codex_update_prompt(pane_text: str) -> bool:
    """Return True when Codex shows the version-update gate before the main TUI."""
    lower = pane_text.lower()
    if not lower.strip():
        return False
    return (
        "update available" in lower
        and "press enter to continue" in lower
        and ("update now" in lower or "skip until next version" in lower)
    )


def _looks_like_claude_skip_permissions_prompt(pane_text: str) -> bool:
    """Return True when Claude waits for the dangerous-permissions confirmation."""
    lower = pane_text.lower()
    if not lower.strip():
        return False

    has_accept_choice = "yes, i accept" in lower
    has_permissions_warning = (
        "dangerously-skip-permissions" in lower
        or "skip permissions" in lower
        or "permission" in lower
        or "approval" in lower
    )
    return has_accept_choice and has_permissions_warning


def _looks_like_cursor_composer(pane_text: str) -> bool:
    """Return True when Cursor Agent has reached its interactive composer."""
    lower = pane_text.lower()
    if not lower.strip():
        return False
    return (
        "cursor agent" in lower
        and "composer" in lower
        and (
            "run everything" in lower
            or "plan, search, build anything" in lower
        )
    )


def _startup_prompt_action(
    pane_text: str,
    trust_platform: str | None,
) -> str | None:
    """Return the key action needed to dismiss a startup confirmation prompt."""
    if trust_platform is None:
        return None

    if trust_platform == "claude":
        if _looks_like_claude_skip_permissions_prompt(pane_text):
            return "down-enter"
        if _looks_like_workspace_trust_prompt(pane_text, "claude"):
            return "enter"
        return None

    if trust_platform == "codex":
        if _looks_like_codex_update_prompt(pane_text):
            return "enter"
        if _looks_like_workspace_trust_prompt(pane_text, "codex"):
            return "enter"
        return None

    if trust_platform == "gemini":
        if _looks_like_workspace_trust_prompt(pane_text, "gemini"):
            return "enter"
        return None

    if trust_platform == "cursor":
        if _looks_like_workspace_trust_prompt(pane_text, "cursor"):
            return "press-a"
        return None

    return None


def _active_tail_looks_like_any_startup_prompt(pane_text: str) -> bool:
    """True when the visible tail matches any known startup gate (any platform)."""
    active = _pane_active_text(pane_text)
    return any(
        _startup_prompt_action(active, plat) is not None
        for plat in TRUST_HANDLED_PLATFORMS
    )


def _is_composer_ready(pane_text: str, *, trust_platform: str | None) -> bool:
    """True when the agent TUI shows a real composer prompt, not a startup gate."""
    active = _pane_active_text(pane_text)
    if _startup_prompt_action(active, trust_platform) is not None:
        return False
    # Platforms ClawTeam does not handle: never treat a known gate as composer-ready.
    if trust_platform is None and _active_tail_looks_like_any_startup_prompt(pane_text):
        return False
    if trust_platform == "cursor" and _looks_like_cursor_composer(active):
        return True
    return any(pat.search(pane_text) for pat in _AGENT_PROMPT_PATTERNS)


# ──────────────────────────────────────────────────────────────────────
# tmux invocation (kept as injectable functions for tests)
# ──────────────────────────────────────────────────────────────────────


async def tmux_capture_pane(target: str, *, history_lines: int = 60) -> str:
    """Run ``tmux capture-pane -p -S -N -t <target>`` and return its stdout."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "capture-pane", "-p", "-t", target, "-S", f"-{history_lines}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await proc.communicate()
    if proc.returncode != 0:
        return ""
    return out.decode("utf-8", errors="replace")


async def tmux_send_keys(
    target: str,
    keys: str,
    *,
    literal: bool = False,
) -> None:
    """Send keys to *target* via ``tmux send-keys``."""
    cmd = ["tmux", "send-keys", "-t", target]
    if literal:
        cmd.extend(["-l", keys])
    else:
        cmd.append(keys)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()


async def _send_trust_action(
    target: str,
    action: str,
    *,
    send_keys: callable | None = None,
) -> None:
    send = send_keys or tmux_send_keys
    if action == "enter":
        await send(target, "Enter")
        await asyncio.sleep(0.5)
        return
    if action == "down-enter":
        await send(target, "\x1b[B", literal=True)
        await asyncio.sleep(0.2)
        await send(target, "Enter")
        await asyncio.sleep(0.5)
        return
    if action == "press-a":
        await send(target, "a")
        await asyncio.sleep(0.5)


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
    """Poll *target* until one of *patterns* is in the captured output."""
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
    trust_platform: str | None = None,
    timeout_sec: float = 30.0,
    poll_interval: float = 0.25,
    capture: callable | None = None,
    send_keys: callable | None = None,
) -> TuiReadyResult:
    """Wait for a TUI-CLI agent's prompt, handling startup gates when scoped."""
    cap = capture or tmux_capture_pane
    deadline = asyncio.get_event_loop().time() + timeout_sec
    last_text = ""
    last_non_empty_text = ""
    attempts = 0
    startup_prompt_seen = False

    while asyncio.get_event_loop().time() < deadline:
        attempts += 1
        text = await cap(target)
        last_text = text
        if text.strip():
            last_non_empty_text = text

        for pat in _AGENT_FATAL_PATTERNS:
            if pat.search(text):
                tail = text[-200:]
                logger.warning(
                    "tmux_tui_ready_fatal_signal",
                    target=target,
                    attempts=attempts,
                    pattern=pat.pattern,
                    tail=tail,
                )
                return TuiReadyResult(
                    ok=False,
                    reason_code="fatal_signal",
                    message="TUI reported a fatal resume/startup error",
                    pane_tail=tail,
                )

        action = _startup_prompt_action(_pane_active_text(text), trust_platform)
        if action is not None:
            startup_prompt_seen = True
            logger.info(
                "tmux_tui_startup_prompt_detected",
                target=target,
                trust_platform=trust_platform,
                action=action,
                attempts=attempts,
            )
            await _send_trust_action(target, action, send_keys=send_keys)
            continue

        if _is_composer_ready(text, trust_platform=trust_platform):
            logger.debug(
                "tmux_tui_composer_ready",
                target=target,
                trust_platform=trust_platform,
                attempts=attempts,
            )
            return TuiReadyResult(ok=True, reason_code="composer_ready")

        await asyncio.sleep(poll_interval)

    tail = (last_non_empty_text or last_text)[-200:]
    if startup_prompt_seen:
        logger.warning(
            "tmux_tui_startup_prompt_timeout",
            target=target,
            trust_platform=trust_platform,
            attempts=attempts,
            tail=tail,
        )
        return TuiReadyResult(
            ok=False,
            reason_code="folder_trust_failed",
            message=(
                "Startup confirmation dialog could not be dismissed "
                f"within {timeout_sec}s"
            ),
            pane_tail=tail,
        )

    logger.warning(
        "tmux_tui_not_ready",
        target=target,
        trust_platform=trust_platform,
        attempts=attempts,
        tail=tail,
    )
    return TuiReadyResult(
        ok=False,
        reason_code="tui_timeout",
        message=f"TUI prompt never appeared within {timeout_sec}s",
        pane_tail=tail,
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
    "TRUST_HANDLED_PLATFORMS",
    "TuiReadyResult",
    "resolve_trust_platform",
    "tmux_capture_pane",
    "tmux_send_keys",
    "wait_shell_ready",
    "wait_tui_ready",
]
