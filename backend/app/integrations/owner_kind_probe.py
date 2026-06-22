"""Maximum-tolerance CLI presence probes for Flow editor owner-kind dropdowns.

The WebUI only needs to know whether a platform *might* be installed — not
whether it is authenticated or currently running. We therefore union every
cheap signal (PATH, well-known install dirs, npm global prefix, login-shell
PATH, OpenClaw's dedicated resolver, …) and treat a kind as available when
*any* probe succeeds. False positives are acceptable; false negatives are not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from app.integrations.openclaw_cli import resolve_openclaw_executable
from app.runtime_bins import resolve_binary

_LOGIN_SHELL_TIMEOUT_SEC = 2.0
_NPM_PREFIX_TIMEOUT_SEC = 2.0


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _iter_common_bindirs(home: Path) -> Iterable[Path]:
    yield home / ".local" / "bin"
    yield home / ".npm-global" / "bin"
    yield home / ".volta" / "bin"
    yield Path("/usr/local/bin")
    yield Path("/usr/bin")
    nvm_root = home / ".nvm" / "versions" / "node"
    for bindir in sorted(nvm_root.glob("*/bin"), reverse=True):
        yield bindir
    pipx_venvs = home / ".local" / "pipx" / "venvs"
    if pipx_venvs.is_dir():
        for bindir in pipx_venvs.glob("*/bin"):
            yield bindir


def _probe_login_shell(binary: str) -> bool:
    try:
        proc = subprocess.run(
            ["bash", "-lc", f"command -v {binary}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LOGIN_SHELL_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    for raw in (proc.stdout or "").splitlines():
        candidate = Path(raw.strip()).expanduser()
        if _is_executable(candidate):
            return True
    return False


def _probe_npm_global_bin(binary: str) -> bool:
    npm = resolve_binary("npm") or shutil.which("npm")
    if not npm:
        return False
    try:
        proc = subprocess.run(
            [npm, "prefix", "-g"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_NPM_PREFIX_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    for raw in (proc.stdout or "").splitlines():
        prefix = raw.strip()
        if not prefix:
            continue
        candidate = Path(prefix).expanduser() / "bin" / binary
        if _is_executable(candidate):
            return True
    return False


def _cursor_extra_paths() -> tuple[Path, ...]:
    if sys.platform != "darwin":
        return ()
    app_bin = Path("/Applications/Cursor.app/Contents/Resources/app/bin")
    return (
        app_bin / "cursor",
        app_bin / "agent",
    )


def probe_binary_installed(
    *names: str,
    extra_paths: Iterable[str | Path] = (),
) -> bool:
    """Return True when any probe suggests one of *names* is installed."""
    if not names:
        return False
    for name in names:
        if resolve_binary(name) or shutil.which(name):
            return True
    home = Path.home().expanduser()
    for name in names:
        for bindir in _iter_common_bindirs(home):
            candidate = bindir / name
            if _is_executable(candidate):
                return True
    for raw in extra_paths:
        candidate = Path(raw).expanduser()
        if _is_executable(candidate):
            return True
    for name in names:
        if _probe_npm_global_bin(name) or _probe_login_shell(name):
            return True
    return False


def _probe_openclaw() -> bool:
    return resolve_openclaw_executable() is not None


# Order defines dropdown order in the frontend.
_PERSISTENT_PROBES: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("hermes", lambda: probe_binary_installed("hermes")),
    ("openclaw", _probe_openclaw),
)

_TEMP_PROBES: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("claude", lambda: probe_binary_installed("claude")),
    ("codex", lambda: probe_binary_installed("codex")),
    ("cursor", lambda: probe_binary_installed("agent", "cursor", extra_paths=_cursor_extra_paths())),
    ("gemini", lambda: probe_binary_installed("gemini")),
    ("kimi", lambda: probe_binary_installed("kimi")),
    ("qwen", lambda: probe_binary_installed("qwen")),
    ("opencode", lambda: probe_binary_installed("opencode")),
    ("qoder", lambda: probe_binary_installed("qodercli", "qoder")),
    ("codebuddy", lambda: probe_binary_installed("codebuddy")),
    ("hermes", lambda: probe_binary_installed("hermes")),
)


def detect_persistent_owner_kinds() -> list[str]:
    return [kind for kind, probe in _PERSISTENT_PROBES if probe()]


def detect_temporary_owner_kinds() -> list[str]:
    return [kind for kind, probe in _TEMP_PROBES if probe()]
