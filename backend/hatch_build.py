"""Package the repo-root files without putting ``..`` in the sdist.

Shared files live above ``backend/``. PyPI rejects an sdist member whose
path is not already canonical (``posixpath.normpath`` would change it), so
the sdist copies those files under ordinary relative paths. A wheel built
from that sdist reads the copies; a wheel built from the git checkout still
reads the originals.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface

# Checkout path relative to backend/, path inside the sdist, wheel destination.
_WHEEL_TREES: tuple[tuple[str, str, str], ...] = (
    ("../frontend/dist", "frontend/dist", "app/_static"),
    (
        "../openclaw-agent-source",
        "openclaw-agent-source",
        "clawsomeflow_openclaw_agent_source",
    ),
    (
        "../clawsomeflow-agent-tools",
        "clawsomeflow-agent-tools",
        "clawsomeflow_agent_tools",
    ),
)


def _first_existing(root: Path, *relative: str) -> Path:
    for rel in relative:
        candidate = (root / rel).resolve()
        if candidate.exists():
            return candidate
    joined = ", ".join(relative)
    raise FileNotFoundError(f"none of these paths exist under {root}: {joined}")


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        force_include: dict[str, str] = build_data.setdefault("force_include", {})
        for outside, inside, destination in _WHEEL_TREES:
            source = _first_existing(root, outside, inside)
            force_include[os.fspath(source)] = destination


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict[str, Any]) -> None:
        readme = _first_existing(Path(self.root), "../readme.md", "readme.md")
        metadata["readme"] = {
            "content-type": "text/markdown",
            "text": readme.read_text(encoding="utf-8"),
        }
