"""Idempotent folder-trust seeding for Claude-style temporary-agent CLIs.

Qoder (`qodercli`) and CodeBuddy (`codebuddy`) are Claude-Code clones exposed as
temporary Flow agents. Both gate startup behind a per-folder "Do you trust the
files in this folder?" dialog that **no CLI flag skips** (verified against the
installed binaries — neither `--permission-mode` nor `--dangerously-skip-permissions`
bypasses it). Because ClawTeam creates a fresh git worktree per agent per run, that
dialog would fire on every spawn and hang `wait_tui_ready`.

Each CLI exposes a **global** config that pre-trusts directories, so we seed it
once (idempotent, non-destructive), gated on the CLI being installed:

* CodeBuddy → ``~/.codebuddy/settings.json`` ``{"trustAll": true}`` (trust every
  directory; verified to suppress the dialog).
* Qoder → ``~/.qoder/settings.json`` ``permissions.trustDirectories`` containing the
  user's home dir. Qoder's match is prefix-based (verified: trusting a parent dir
  trusts its children), so the home entry covers all ClawTeam worktrees.

Tool-call approval is still handled per-spawn via ``--permission-mode`` (these
settings ONLY cover folder trust). Operator auth is a one-time prerequisite
(CodeBuddy interactive ``login``; Qoder ``QODER_PERSONAL_ACCESS_TOKEN``) — we do
not automate it, same as claude/codex/gemini API credentials.

Called from ``cli/init.py`` (fresh) and ``upgrade.py::run_upgrade`` (upgrade path)
so upgrade-only users converge with fresh deploys (DEV.md upgrade-parity).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger("integrations.temp_agent_trust")


def codebuddy_config_path() -> Path:
    return Path.home() / ".codebuddy" / "settings.json"


def qoder_config_path() -> Path:
    return Path.home() / ".qoder" / "settings.json"


def _load_json_obj(path: Path) -> dict | None:
    """Load a JSON object, or None if unreadable/unparsable (caller bails)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("temp_agent_trust_unreadable", path=str(path), error=str(exc))
        return None


def ensure_codebuddy_trust_all(*, force: bool = False) -> bool:
    """Seed ``trustAll: true`` into CodeBuddy's global settings.

    Returns True if written/changed. Best-effort: never raises. ``force`` skips
    the ``which("codebuddy")`` gate (tests). Never clobbers an existing
    ``trustAll`` value (respects an explicit operator choice).
    """
    try:
        if not force and shutil.which("codebuddy") is None:
            return False
        path = codebuddy_config_path()
        data = _load_json_obj(path)
        if data is None or "trustAll" in data:
            return False
        data["trustAll"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("codebuddy_trust_all_seeded", path=str(path))
        return True
    except Exception as exc:  # pragma: no cover - defensive; never block init
        logger.warning("codebuddy_trust_seed_failed", error=str(exc))
        return False


def ensure_qoder_trust_dirs(*, force: bool = False) -> bool:
    """Add the user's home dir to Qoder's ``permissions.trustDirectories``.

    Returns True if written/changed. Best-effort: never raises. ``force`` skips
    the ``which("qodercli")`` gate (tests). Merges (preserves existing entries);
    a no-op once the home dir is present.
    """
    try:
        if not force and shutil.which("qodercli") is None:
            return False
        path = qoder_config_path()
        data = _load_json_obj(path)
        if data is None:
            return False
        home = str(Path.home())
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        trusted = perms.get("trustDirectories")
        if not isinstance(trusted, list):
            trusted = []
        if home in trusted:
            return False
        trusted.append(home)
        perms["trustDirectories"] = trusted
        data["permissions"] = perms
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("qoder_trust_dirs_seeded", path=str(path), dir=home)
        return True
    except Exception as exc:  # pragma: no cover - defensive; never block init
        logger.warning("qoder_trust_seed_failed", error=str(exc))
        return False


__all__ = [
    "codebuddy_config_path",
    "ensure_codebuddy_trust_all",
    "ensure_qoder_trust_dirs",
    "qoder_config_path",
]
