"""Helpers for resolving runtime CLI binaries deterministically.

Goal: keep deployment-time and runtime command resolution on the same
Python environment whenever possible, even if user PATH contains
multiple historical installs.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def current_python_bindir() -> Path:
    """Directory that contains the currently running Python executable."""
    return Path(sys.executable).resolve().parent


def current_entrypoint_bindir() -> Path | None:
    """Best-effort directory of the current CLI entrypoint script."""
    argv0 = (sys.argv[0] or "").strip()
    if not argv0:
        return None
    candidate: Path | None = None
    if "/" in argv0:
        candidate = Path(argv0).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
    else:
        resolved = shutil.which(argv0)
        if resolved:
            candidate = Path(resolved)
    if candidate and candidate.is_file():
        return candidate.resolve().parent
    return None


def _managed_venv_bindir() -> Path | None:
    """``~/.clawsomeflow/.venv/bin`` when present (upgrade/doctor preflight)."""
    override = os.environ.get("CSFLOW_VENV_DIR", "").strip()
    if override:
        bindir = Path(override).expanduser() / "bin"
        if bindir.is_dir():
            return bindir
    home = os.environ.get("CSFLOW_HOME", "").strip()
    if not home:
        home = str(Path.home() / ".clawsomeflow")
    bindir = Path(home).expanduser() / ".venv" / "bin"
    return bindir if bindir.is_dir() else None


def resolve_binary(name: str) -> str | None:
    """Resolve *name* with stable precedence.

    Priority:
      1) explicit env override ``CSFLOW_<NAME>_BIN``
      2) sibling executable next to current ``sys.executable``
      3) normal PATH lookup
    """

    env_key = f"CSFLOW_{name.upper()}_BIN"
    overridden = os.environ.get(env_key, "").strip()
    if overridden:
        override_path = Path(overridden).expanduser()
        if _is_executable(override_path):
            return str(override_path.resolve())
        found = shutil.which(overridden)
        if found:
            return found

    managed_dir = _managed_venv_bindir()
    if managed_dir is not None:
        managed_sibling = managed_dir / name
        if _is_executable(managed_sibling):
            return str(managed_sibling.resolve())

    entrypoint_dir = current_entrypoint_bindir()
    if entrypoint_dir:
        sibling = entrypoint_dir / name
        if _is_executable(sibling):
            return str(sibling)

    sibling = current_python_bindir() / name
    if _is_executable(sibling):
        return str(sibling)

    return shutil.which(name)

