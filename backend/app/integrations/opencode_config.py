"""Idempotent opencode global-config seeding.

opencode (the ``opencode`` TUI CLI) is a *temporary* Flow agent platform. Unlike
claude/codex/gemini/kimi/qwen, opencode's **interactive** mode has NO permission
bypass CLI flag — auto-approval of tool calls is config-only
(``"permission": "allow"`` in opencode's global config). ClawsomeFlow dispatches
to the interactive TUI via tmux paste and cannot click an approval dialog, so
without this the agent would stall on the first tool call.

This module writes ``"permission": "allow"`` into opencode's global config
(``$XDG_CONFIG_HOME/opencode/opencode.json``, default ``~/.config/opencode``)
**idempotently** and **non-destructively**:

* Only when the ``opencode`` binary is actually installed (so we don't create
  config dirs for users who never touch opencode).
* Never clobbers an existing ``permission`` key — if the user set their own
  policy (even ``"ask"``), we leave it untouched.
* Preserves every other key in an existing config file.

Called from both the fresh-deploy path (``cli/init.py``) and the upgrade path
(``upgrade.py::run_upgrade``) so upgrade-only users converge with fresh deploys
(DEV.md upgrade-parity invariant).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger("integrations.opencode_config")

_OPENCODE_SCHEMA = "https://opencode.ai/config.json"


def opencode_config_path() -> Path:
    """Return opencode's global config path, honouring ``XDG_CONFIG_HOME``."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "opencode" / "opencode.json"


def ensure_opencode_permission_allow(*, force: bool = False) -> bool:
    """Idempotently set ``permission: allow`` in opencode's global config.

    Returns ``True`` if the file was created or modified, ``False`` otherwise
    (already set, opencode not installed, or an existing ``permission`` key is
    preserved). Best-effort: never raises — logs and returns ``False`` on error.

    ``force`` skips the ``which("opencode")`` gate (used by tests).
    """
    try:
        if not force and shutil.which("opencode") is None:
            return False

        path = opencode_config_path()
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError) as exc:
                # Don't overwrite a file we can't parse — the user may have a
                # comment-laden / non-standard config. Bail out safely.
                logger.warning("opencode_config_unreadable", path=str(path), error=str(exc))
                return False

        # Never clobber an explicit user policy (string "allow"/"ask"/"deny" or
        # an object form). Only seed when the key is entirely absent.
        if "permission" in data:
            return False

        data.setdefault("$schema", _OPENCODE_SCHEMA)
        data["permission"] = "allow"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("opencode_permission_seeded", path=str(path))
        return True
    except Exception as exc:  # pragma: no cover - defensive; never block init
        logger.warning("opencode_config_seed_failed", error=str(exc))
        return False


__all__ = ["ensure_opencode_permission_allow", "opencode_config_path"]
