"""Path conventions for ~/.clawsomeflow/ data directory.

Public API:
* :data:`CSFLOW_HOME_ENV` — env var that overrides default home location.
* :func:`clawsomeflow_home` — resolve the home directory (creating it if missing).
* Convenience accessors: :func:`config_path`, :func:`db_path`, :func:`flows_dir`,
  :func:`runs_dir`, :func:`agents_dir`, :func:`system_dir`, :func:`skills_source_dir`,
  :func:`logs_dir`, :func:`run_dir`, :func:`agent_dir`,
  :func:`common_agent_source_dir`, :func:`openclaw_agent_tools_dir`.
* :func:`validate_identifier` / :func:`ensure_within_root` — path-safety helpers,
  mirroring ClawTeam conventions to keep behaviour consistent across tools.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Env var that overrides the default home location (used by tests + by ops
# who want to relocate ~/.clawsomeflow/ — e.g. to a dedicated data volume).
CSFLOW_HOME_ENV = "CSFLOW_HOME"

_DEFAULT_HOME = "~/.clawsomeflow"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ──────────────────────────────────────────────────────────────────────
# Identifier / path safety (mirrors ClawTeam to keep behaviour consistent)
# ──────────────────────────────────────────────────────────────────────


def validate_identifier(value: str, kind: str = "identifier", allow_empty: bool = False) -> str:
    """Validate a logical identifier used in filesystem-backed state."""
    if value == "" and allow_empty:
        return value
    if not value:
        raise ValueError(f"Invalid {kind}: value must not be empty")
    if value in {".", ".."}:
        raise ValueError(f"Invalid {kind}: '.' and '..' are not allowed")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"Invalid {kind}: only letters, digits, '.', '_' and '-' are allowed"
        )
    return value


def clawsomeflow_home_path() -> Path:
    """Resolve the configured data-home path without creating it."""
    raw = os.environ.get(CSFLOW_HOME_ENV) or _DEFAULT_HOME
    return Path(raw).expanduser()


def clawsomeflow_home_exists() -> bool:
    """Return whether the data-home directory already exists on disk."""
    return clawsomeflow_home_path().exists()


def ensure_within_root(root: Path, *parts: str) -> Path:
    """Join *parts* under *root* and reject escapes outside the root."""
    base = root.resolve()
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError("Resolved path escapes the configured data directory") from exc
    return candidate


# ──────────────────────────────────────────────────────────────────────
# ~/.clawsomeflow/ directory layout
# ──────────────────────────────────────────────────────────────────────


def clawsomeflow_home() -> Path:
    """Resolve the ClawsomeFlow data home, creating it if missing.

    Honours the ``CSFLOW_HOME`` environment variable for test isolation
    and container deployments.
    """
    home = clawsomeflow_home_path()
    home.mkdir(parents=True, exist_ok=True)
    return home


def config_path() -> Path:
    """Path to ``~/.clawsomeflow/config.json``."""
    return clawsomeflow_home() / "config.json"


def db_path() -> Path:
    """Path to ``~/.clawsomeflow/db.sqlite`` (local mode only)."""
    return clawsomeflow_home() / "db.sqlite"


def version_marker_path() -> Path:
    """Path to ``~/.clawsomeflow/.csflow-version``.

    Plain-text marker (one line, e.g. ``1.2.3``) recording the
    ClawsomeFlow version that last successfully wrote to this data dir.
    Used by :mod:`app.upgrade` to decide which migrations to apply.

    Missing marker = "never initialised by a versioned ClawsomeFlow"
    (could be an alpha pre-1.0 install, or a fresh dir).
    """
    return clawsomeflow_home() / ".csflow-version"


def migrations_ledger_path() -> Path:
    """Path to ``~/.clawsomeflow/.csflow-migrations.json``.

    JSON ledger of which migration ids have already been applied
    (``{"applied": ["0.1.12", ...]}``). This is the *authoritative* gate for
    "which migrations to run" — unlike the version marker it is direction-safe,
    so switching between stable and beta builds (or downgrading then
    re-upgrading) never re-applies or skips a migration. See :mod:`app.upgrade`.
    """
    return clawsomeflow_home() / ".csflow-migrations.json"


def flows_dir() -> Path:
    """Directory holding Flow definition JSON files."""
    p = clawsomeflow_home() / ".flows"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    """Directory holding per-Run metadata + event logs."""
    p = clawsomeflow_home() / ".runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def run_dir(run_id: str) -> Path:
    """Per-Run directory ``.runs/{run_id}/``."""
    return ensure_within_root(runs_dir(), validate_identifier(run_id, "run id"))


def agents_dir() -> Path:
    """Directory holding ClawsomeFlow-managed OpenClaw agent workspaces."""
    p = clawsomeflow_home() / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def agent_dir(agent_id: str) -> Path:
    """Per-OpenClaw-agent directory ``agents/{agent_id}/``."""
    return ensure_within_root(agents_dir(), validate_identifier(agent_id, "agent id"))


def system_dir() -> Path:
    """System directory for internal runtime artifacts."""
    p = clawsomeflow_home() / ".system"
    p.mkdir(parents=True, exist_ok=True)
    return p


def skills_source_dir() -> Path:
    """Directory containing the source skills shipped to OpenClaw on init."""
    p = clawsomeflow_home() / ".skills-source"
    p.mkdir(parents=True, exist_ok=True)
    return p


def common_agent_source_dir() -> Path:
    """Hidden common source payload used for managed-agent bootstrap."""
    p = clawsomeflow_home() / ".common-agent-source"
    p.mkdir(parents=True, exist_ok=True)
    return p


def openclaw_agent_tools_dir() -> Path:
    """Hidden runtime tool bundle for OpenClaw agents."""
    p = clawsomeflow_home() / ".clawsomeflow-agent-tools"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    """Directory holding structured JSONL log files (local mode)."""
    p = clawsomeflow_home() / ".logs"
    p.mkdir(parents=True, exist_ok=True)
    return p
