"""Helpers for discovering the OpenClaw CLI executable.

The backend can run under environments (for example systemd user services)
where the interactive shell ``PATH`` is not inherited. We therefore try
multiple discovery strategies before declaring the CLI unavailable.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

_LOGIN_SHELL_PROBE_TIMEOUT_SEC = 2.0
_NPM_PREFIX_PROBE_TIMEOUT_SEC = 2.0
_PROCESS_CMD_TIMEOUT_SEC = 1.5
_DEFAULT_OPENCLAW_GATEWAY_PORT = 18789


def _is_executable(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def _prepend_path(path_entry: str) -> None:
    current = os.environ.get("PATH", "")
    entries = [item for item in current.split(os.pathsep) if item]
    if path_entry in entries:
        return
    os.environ["PATH"] = f"{path_entry}{os.pathsep}{current}" if current else path_entry


def _iter_fallback_candidates(home: Path) -> Iterable[Path]:
    yield home / ".npm-global" / "bin" / "openclaw"
    yield home / ".local" / "bin" / "openclaw"
    yield home / ".volta" / "bin" / "openclaw"
    nvm_root = home / ".nvm" / "versions" / "node"
    for candidate in sorted(nvm_root.glob("*/bin/openclaw"), reverse=True):
        yield candidate
    yield Path("/usr/local/bin/openclaw")
    yield Path("/usr/bin/openclaw")


def _probe_openclaw_from_login_shell() -> str | None:
    try:
        proc = subprocess.run(
            ["bash", "-lc", "command -v openclaw"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LOGIN_SHELL_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for raw_line in (proc.stdout or "").splitlines():
        candidate = Path(raw_line.strip()).expanduser()
        if _is_executable(candidate):
            return str(candidate)
    return None


def _probe_openclaw_from_npm_prefix() -> str | None:
    """Resolve ``openclaw`` from ``$(npm prefix -g)/bin`` in current env."""
    npm_executable = shutil.which("npm")
    if not npm_executable:
        return None
    try:
        proc = subprocess.run(
            [npm_executable, "prefix", "-g"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_NPM_PREFIX_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    for raw_line in (proc.stdout or "").splitlines():
        prefix = raw_line.strip()
        if not prefix:
            continue
        candidate = Path(prefix).expanduser() / "bin" / "openclaw"
        if _is_executable(candidate):
            return str(candidate)
    return None


def _listener_pids_for_port(port: int) -> list[int]:
    pids: set[int] = set()
    lsof = shutil.which("lsof")
    if lsof:
        try:
            proc = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_PROCESS_CMD_TIMEOUT_SEC,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc and proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                raw = line.strip()
                if raw.isdigit():
                    pids.add(int(raw))
    if pids:
        return sorted(pids)
    ss = shutil.which("ss")
    if ss:
        try:
            proc = subprocess.run(
                [ss, "-ltnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_PROCESS_CMD_TIMEOUT_SEC,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc and proc.returncode == 0:
            for pid_raw in re.findall(r"pid=(\d+)", proc.stdout or ""):
                pids.add(int(pid_raw))
    return sorted(pids)


def _process_argv(pid: int) -> list[str]:
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
        except OSError:
            raw = b""
        parts = [item.decode("utf-8", errors="replace") for item in raw.split(b"\x00") if item]
        if parts:
            return parts
    ps = shutil.which("ps")
    if not ps:
        return []
    try:
        proc = subprocess.run(
            [ps, "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROCESS_CMD_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    command = (proc.stdout or "").strip()
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return [command]


def _iter_openclaw_candidates_from_process_argv(argv: list[str]) -> Iterable[Path]:
    for item in argv:
        text = (item or "").strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if candidate.name == "openclaw":
            yield candidate

        # npm global install runtime: <prefix>/lib/node_modules/openclaw/dist/index.js
        marker = "/lib/node_modules/openclaw/dist/index.js"
        if marker in text:
            prefix = text.split(marker, 1)[0].strip()
            if prefix:
                yield Path(prefix).expanduser() / "bin" / "openclaw"

        # git/source checkout runtime: <repo>/dist/index.js
        if text.endswith("/openclaw/dist/index.js"):
            repo_root = text[: -len("/dist/index.js")]
            if repo_root:
                yield Path(repo_root).expanduser() / "openclaw.mjs"
                yield Path(repo_root).expanduser() / "openclaw"


def _probe_openclaw_from_default_gateway_service() -> str | None:
    """Resolve OpenClaw executable from the service listening on 18789."""
    for pid in _listener_pids_for_port(_DEFAULT_OPENCLAW_GATEWAY_PORT):
        argv = _process_argv(pid)
        for candidate in _iter_openclaw_candidates_from_process_argv(argv):
            if _is_executable(candidate):
                return str(candidate)
    return None


def resolve_openclaw_executable(
    *,
    home: Path | None = None,
    run_login_shell_probe: bool = True,
) -> str | None:
    """Best-effort resolution of ``openclaw`` executable path.

    Resolution order:
    1) executable inferred from the OpenClaw service currently running on
       default gateway port ``18789``.
    2) ``$(npm prefix -g)/bin/openclaw`` in current process environment
    3) ``PATH`` via :func:`shutil.which`
    4) well-known install locations (including NVM and Volta)
    5) login-shell probe (``bash -lc 'command -v openclaw'``)

    When a fallback path is found we also prepend its parent directory to the
    current process ``PATH`` so subsequent invocations can use normal lookup.
    """

    service_discovered = _probe_openclaw_from_default_gateway_service()
    if service_discovered:
        _prepend_path(str(Path(service_discovered).parent))
        return service_discovered

    npm_discovered = _probe_openclaw_from_npm_prefix()
    if npm_discovered:
        _prepend_path(str(Path(npm_discovered).parent))
        return npm_discovered

    executable = shutil.which("openclaw")
    if executable:
        return executable

    home_dir = (home or Path.home()).expanduser()
    for candidate in _iter_fallback_candidates(home_dir):
        if not _is_executable(candidate):
            continue
        _prepend_path(str(candidate.parent))
        return str(candidate)

    if run_login_shell_probe:
        shell_discovered = _probe_openclaw_from_login_shell()
        if shell_discovered:
            _prepend_path(str(Path(shell_discovered).parent))
            return shell_discovered

    return None

