"""Gemini CLI policy that denies ``ask_user`` for unattended temporary agents.

Gemini's built-in ``yolo.toml`` keeps ``ask_user`` on ``decision = "ask_user"``
even in YOLO mode, so a worker can pop an option menu mid-task that nobody in
the tmux pane will answer. Policies passed via ``--policy`` load in the USER
tier (4.x), which outranks the built-in default tier (1.x), so this deny wins.

``--policy`` REPLACES the default user policies dir (``~/.gemini/policies``)
instead of adding to it, so :func:`gemini_policy_args` re-passes that dir when
it exists to keep the operator's own policies in force.

The file content is code-owned and rewritten whenever it differs, right before
each gemini spawn — fresh and upgrade-only installs converge without a
dedicated init/migration step.
"""

from __future__ import annotations

from pathlib import Path

from app.logging_setup import get_logger
from app.paths import clawsomeflow_home_path

logger = get_logger("integrations.gemini_policy")

_POLICY_FILENAME = "gemini-unattended-policy.toml"

_POLICY_TOML = """\
# Auto-generated — rewritten on every gemini spawn; do not edit.
[[rule]]
toolName = "ask_user"
decision = "deny"
priority = 999
denyMessage = "Unattended run: no human can answer. Decide yourself and continue."
"""


def gemini_policy_path() -> Path:
    return clawsomeflow_home_path() / ".system" / _POLICY_FILENAME


def gemini_user_policies_dir() -> Path:
    return Path.home() / ".gemini" / "policies"


def gemini_policy_args() -> list[str]:
    """``--policy`` argv for a gemini spawn (no I/O beyond a stat)."""
    args = ["--policy", str(gemini_policy_path())]
    user_dir = gemini_user_policies_dir()
    if user_dir.is_dir():
        args += ["--policy", str(user_dir)]
    return args


def ensure_gemini_policy() -> None:
    """Write the policy file when missing or stale. Best-effort: never raises."""
    path = gemini_policy_path()
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == _POLICY_TOML:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_POLICY_TOML, encoding="utf-8")
    except OSError as exc:
        logger.warning("gemini_policy_write_failed", path=str(path), error=str(exc))


__all__ = [
    "ensure_gemini_policy",
    "gemini_policy_args",
    "gemini_policy_path",
    "gemini_user_policies_dir",
]
