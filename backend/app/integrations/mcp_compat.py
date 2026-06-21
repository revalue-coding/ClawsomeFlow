"""MCP Python SDK compatibility helpers.

ClawTeam's ``clawteam-mcp`` server targets the MCP 1.x SDK. MCP 2.x pre-releases
(e.g. ``2.0.0a2``) can be pulled when ``pip install --pre`` is used or when the
dependency is left unpinned — the subprocess then fails to start and csflow's
lifespan health probe times out.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Sequence

MCP_SDK_SPEC = "mcp>=1.0.0,<2.0.0"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_mcp_version(version: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(version.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def installed_mcp_version() -> str | None:
    try:
        import importlib.metadata as md
    except ImportError:  # pragma: no cover - py310+ always has this
        return None
    try:
        return md.version("mcp")
    except md.PackageNotFoundError:
        return None


def mcp_sdk_compatible(version: str | None = None) -> bool:
    raw = version if version is not None else installed_mcp_version()
    if raw is None:
        return False
    sem = parse_mcp_version(raw)
    return sem is not None and sem[0] < 2


def incompatible_mcp_detail(version: str | None = None) -> str:
    raw = version if version is not None else installed_mcp_version()
    if raw is None:
        return (
            "Python package `mcp` is not installed (required by clawteam-mcp). "
            f"Install with `pip install '{MCP_SDK_SPEC}'`."
        )
    sem = parse_mcp_version(raw)
    if sem is None:
        return f"Unparseable installed mcp version: {raw!r}"
    if sem[0] >= 2:
        return (
            f"Incompatible mcp {raw} installed — clawteam-mcp requires MCP 1.x. "
            f"Pin with `pip install '{MCP_SDK_SPEC}'`."
        )
    return ""


def pip_install_mcp_sdk(*, pip_cmd: Sequence[str] | None = None) -> tuple[bool, str]:
    cmd = list(pip_cmd or [sys.executable, "-m", "pip", "install", "--upgrade"])
    cmd.append(MCP_SDK_SPEC)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, output or "pip install mcp failed"
    return True, ""


def ensure_mcp_sdk_compatible(*, pip_cmd: Sequence[str] | None = None) -> tuple[bool, str]:
    """Return ``(ok, detail)``. Attempts a pip pin when the installed SDK is wrong."""
    if mcp_sdk_compatible():
        return True, ""
    detail = incompatible_mcp_detail()
    ok, pip_out = pip_install_mcp_sdk(pip_cmd=pip_cmd)
    if not ok:
        return False, f"{detail} Automatic repair failed: {pip_out}"
    if mcp_sdk_compatible():
        return True, ""
    return False, f"{detail} Automatic repair finished but mcp is still incompatible."


__all__ = [
    "MCP_SDK_SPEC",
    "ensure_mcp_sdk_compatible",
    "incompatible_mcp_detail",
    "installed_mcp_version",
    "mcp_sdk_compatible",
    "parse_mcp_version",
    "pip_install_mcp_sdk",
]
