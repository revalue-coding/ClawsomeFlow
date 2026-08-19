"""WSL2 environment detection + Windows interop helpers.

ClawsomeFlow supports Windows end users exclusively through WSL2 (the whole
Linux stack — tmux, clawteam, openclaw — runs unchanged inside the WSL
distro). This module is the ONLY place that knows how to answer "are we
inside WSL?" and how to talk to the Windows side (explorer.exe, wslpath).

Zero-impact invariant: every function here must be a cheap no-op / falsy
answer on plain Linux and on macOS, so importing or calling it can never
change behaviour outside WSL. Detection reads /proc markers only (no
subprocesses), because a systemd-managed csflow service does not inherit
WSL_DISTRO_NAME and env vars are therefore not a reliable signal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePath

_OSRELEASE_PATH = "/proc/sys/kernel/osrelease"
# Interop plumbing mounted by the real WSL init. A Docker container running on
# a WSL2-backed Docker Desktop shares the "microsoft" kernel string but has
# NONE of these, which is exactly the false positive this check removes.
_INTEROP_MARKERS = ("/run/WSL", "/mnt/wsl", "/proc/sys/fs/binfmt_misc/WSLInterop")

_EXPLORER_FALLBACK = "/mnt/c/Windows/explorer.exe"


def _kernel_osrelease() -> str:
    """Kernel release string (monkeypatch point for tests)."""
    try:
        return Path(_OSRELEASE_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _has_wsl_interop_markers() -> bool:
    """Whether the WSL init's interop plumbing is present (tests monkeypatch)."""
    for marker in _INTEROP_MARKERS:
        try:
            if Path(marker).exists():
                return True
        except OSError:
            continue
    return False


def is_wsl() -> bool:
    """True only inside a real WSL distro userland.

    Requires BOTH the Microsoft kernel string AND the WSL interop filesystem
    markers, so a Docker container on Docker Desktop (Microsoft kernel, no
    interop mounts) and a plain Linux/macOS host both answer False.
    """
    if sys.platform != "linux":
        return False
    if "microsoft" not in _kernel_osrelease().lower():
        return False
    return _has_wsl_interop_markers()


def wsl_distro_name() -> str | None:
    """Distro name for ``\\\\wsl.localhost\\<distro>`` hints; best-effort.

    Only the interactive-shell env carries WSL_DISTRO_NAME (a systemd service
    does not), so callers must tolerate ``None`` and render a generic hint.
    """
    name = (os.environ.get("WSL_DISTRO_NAME") or "").strip()
    return name or None


def windows_path_for(path: Path) -> str | None:
    """Convert a WSL path to its Windows form via ``wslpath -w``; None on failure.

    For a path inside the distro this yields a ``\\\\wsl.localhost\\...`` UNC
    path; for ``/mnt/c/...`` it yields the native drive path.
    """
    wslpath = shutil.which("wslpath")
    if wslpath is None:
        return None
    try:
        proc = subprocess.run(
            [wslpath, "-w", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    converted = (proc.stdout or "").strip()
    return converted or None


def find_windows_explorer() -> str | None:
    """Locate explorer.exe (interop PATH first, then the canonical C: mount)."""
    found = shutil.which("explorer.exe")
    if found:
        return found
    if Path(_EXPLORER_FALLBACK).exists():
        return _EXPLORER_FALLBACK
    return None


def is_windows_drive_mount(path: PurePath) -> bool:
    """Whether *path* lives on a 9P-mounted Windows drive (``/mnt/<letter>/...``).

    Pure path-shape check (no filesystem access); only meaningful when
    :func:`is_wsl` is True — callers must combine the two.
    """
    parts = path.parts
    if len(parts) < 3 or parts[0] != "/" or parts[1] != "mnt":
        return False
    drive = parts[2]
    return len(drive) == 1 and drive.isalpha()
