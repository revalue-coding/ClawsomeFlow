"""Pre-accept Claude Code's folder-trust dialog for a worktree before spawn.

Claude Code 2.1.27x shows "Quick safety check … ❯ No, exit / Yes, I trust this
folder" in every untrusted directory, and the highlighted default is **No,
exit**. ClawTeam's spawn path answers that dialog with a bare Enter within its
``spawn_ready_timeout`` window, and the dialog appears ~0.4s after launch, so
ClawTeam almost always wins the race — and Enter makes Claude exit (rc=1).

Trust is looked up in ``~/.claude.json`` ``projects[<dir>].hasTrustDialogAccepted``,
walking up from the cwd but never past the cwd's git root. A ClawTeam worktree
is its own git root, so the exact worktree path must be trusted; a parent entry
does not help. We write that key before every claude spawn/resume, which keeps
the dialog from appearing at all. Claude re-reads the file and merges its own
changes on every save, so the entry survives later Claude writes.

Best-effort: a missing/unreadable config is left alone (``wait_tui_ready``
still navigates the dialog to "Yes" as a fallback).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unicodedata
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger("integrations.claude_trust")

_WRITE_LOCK = threading.Lock()


def claude_config_path() -> Path:
    return Path.home() / ".claude.json"


def _trust_keys(folder: str) -> list[str]:
    keys: list[str] = []
    for raw in (str(Path(folder).expanduser()), os.path.realpath(Path(folder).expanduser())):
        for key in (raw, unicodedata.normalize("NFC", raw)):
            if key not in keys:
                keys.append(key)
    return keys


def ensure_claude_folder_trusted(folder: str) -> bool:
    """Mark *folder* as trusted in Claude's global config. Returns True if written."""
    path = claude_config_path()
    with _WRITE_LOCK:
        try:
            if not path.is_file():
                return False
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            projects = data.setdefault("projects", {})
            if not isinstance(projects, dict):
                return False
            changed = False
            for key in _trust_keys(folder):
                entry = projects.get(key)
                if not isinstance(entry, dict):
                    entry = {}
                if entry.get("hasTrustDialogAccepted") is True:
                    continue
                projects[key] = {**entry, "hasTrustDialogAccepted": True}
                changed = True
            if not changed:
                return False
            mode = path.stat().st_mode & 0o777
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".claude.json.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
                os.chmod(tmp, mode)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            return True
        except Exception as exc:
            logger.warning("claude_folder_trust_seed_failed", folder=folder, error=str(exc))
            return False


__all__ = ["claude_config_path", "ensure_claude_folder_trusted"]
