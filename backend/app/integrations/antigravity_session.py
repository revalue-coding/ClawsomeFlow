"""Per-agent session settings for Antigravity CLI (``agy``) workers.

``agy --settings <file>`` overlays settings for that session only and never
saves them; every other key keeps coming from the operator's own
``~/.gemini/antigravity-cli/settings.json`` (sign-in, model, permission rules).
Two unattended blockers are handled there instead of in the operator's global
config:

* **Folder trust** — ``agy`` asks "Do you trust the contents of this project?"
  in every directory not listed in ``trustedWorkspaces``; neither
  ``--dangerously-skip-permissions`` nor trusting a parent directory skips it,
  and ClawTeam creates a fresh worktree per agent per run. The session file
  lists the exact worktree (literal + resolved path).
* **Artifact review** — the default ``artifactReviewPolicy`` makes the agent
  stop and wait for a human review of plans it presents.

The file is rewritten before every spawn/resume (fresh and upgrade-only
installs converge without an init/migration step) and removed on shutdown.
``agy`` exits immediately when the ``--settings`` file is missing, so a failed
write raises instead of being swallowed.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.paths import clawsomeflow_home_path

UNATTENDED_SETTINGS: dict[str, str] = {"artifactReviewPolicy": "always-proceed"}


def antigravity_session_settings_path(team: str, agent_id: str) -> Path:
    return clawsomeflow_home_path() / ".system" / "antigravity-sessions" / team / f"{agent_id}.json"


def _trust_keys(folder: str) -> list[str]:
    keys: list[str] = []
    for raw in (str(Path(folder).expanduser()), os.path.realpath(Path(folder).expanduser())):
        if raw not in keys:
            keys.append(raw)
    return keys


def write_antigravity_session_settings(path: Path, worktree: str) -> None:
    """Write the session overlay trusting *worktree*. Raises on I/O failure."""
    data = {**UNATTENDED_SETTINGS, "trustedWorkspaces": _trust_keys(worktree)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def remove_antigravity_session_settings(path: Path) -> None:
    """Best-effort removal of the session overlay (and its empty team dir)."""
    try:
        path.unlink(missing_ok=True)
        path.parent.rmdir()
    except OSError:
        pass


__all__ = [
    "UNATTENDED_SETTINGS",
    "antigravity_session_settings_path",
    "remove_antigravity_session_settings",
    "write_antigravity_session_settings",
]
