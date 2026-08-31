"""Credentials for the password-protected release index.

The enterprise source (index, release metadata, install/upgrade scripts) sits
behind HTTP basic auth, so every self-driving upgrade path needs the pair:

* the in-app update check fetches the release metadata,
* ``hyflow upgrade`` and the WebUI one-click upgrade fetch ``upgrade.sh``,
* the fetched script then hands the pair to pip.

Two sources, in priority order:

1. the environment — the managed service unit carries the pair, and it lets a
   one-off run override it;
2. a file in the data home, written by the installer, so ``hyflow upgrade``
   also works from a bare interactive shell where nothing is exported.

Absent both, callers fall back to unauthenticated requests: that is the right
behaviour for a source served without auth.
"""

from __future__ import annotations

import os
from pathlib import Path

from app import paths

ENV_USER = "HYFLOW_PYPI_USER"
ENV_PASSWORD = "HYFLOW_PYPI_PASSWORD"

#: ``user:password`` on a single line, mode 0600.
AUTH_FILENAME = ".index-auth"


def auth_file_path() -> Path:
    return paths.clawsomeflow_home() / AUTH_FILENAME


def _from_env() -> tuple[str, str] | None:
    user = os.environ.get(ENV_USER, "").strip()
    if not user:
        return None
    return (user, os.environ.get(ENV_PASSWORD, ""))


def _from_file() -> tuple[str, str] | None:
    try:
        raw = auth_file_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    user = user.strip()
    if not user:
        return None
    return (user, password)


def credentials() -> tuple[str, str] | None:
    """Return ``(user, password)`` for the release index, or None."""
    return _from_env() or _from_file()


def curl_auth_args() -> list[str]:
    """``curl`` arguments that authenticate against the source (may be empty)."""
    pair = credentials()
    if pair is None:
        return []
    return ["-u", f"{pair[0]}:{pair[1]}"]


def child_env() -> dict[str, str]:
    """Environment additions so a spawned upgrade script can reach the index."""
    pair = credentials()
    if pair is None:
        return {}
    return {ENV_USER: pair[0], ENV_PASSWORD: pair[1]}
