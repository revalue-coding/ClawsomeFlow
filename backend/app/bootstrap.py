"""Bootstrap the ``~/.clawsomeflow/`` data directory.

Public API:
* :func:`ensure_data_layout` — idempotently create the canonical directory
  structure described in plan §3.
* :func:`bootstrap_summary` — return a structured summary of what exists
  (used by ``csflow status`` and the ``/health`` endpoint).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app import paths


@dataclass(slots=True)
class BootstrapSummary:
    """Reports the state of the on-disk layout."""

    home: Path
    config_present: bool
    db_present: bool
    flows_count: int
    runs_count: int
    agents_count: int
    skills_source_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "config_present": self.config_present,
            "db_present": self.db_present,
            "flows_count": self.flows_count,
            "runs_count": self.runs_count,
            "agents_count": self.agents_count,
            "skills_source_count": self.skills_source_count,
        }


def ensure_data_layout() -> Path:
    """Idempotently create the canonical ``~/.clawsomeflow/`` layout.

    Returns the resolved home directory.
    """
    home = paths.clawsomeflow_home()
    # All sub-dir accessors are idempotent (mkdir parents=True, exist_ok=True).
    paths.flows_dir()
    paths.runs_dir()
    paths.agents_dir()
    paths.system_dir()
    paths.skills_source_dir()
    paths.common_agent_source_dir()
    paths.openclaw_agent_tools_dir()
    paths.logs_dir()
    return home


def bootstrap_summary() -> BootstrapSummary:
    """Snapshot the current on-disk state."""
    home = paths.clawsomeflow_home()
    flows_n = sum(1 for _ in paths.flows_dir().glob("*.json"))
    runs_n = sum(1 for p in paths.runs_dir().iterdir() if p.is_dir())
    agents_n = sum(1 for p in paths.agents_dir().iterdir() if p.is_dir())
    skills_n = sum(1 for p in paths.skills_source_dir().iterdir() if p.is_dir())
    return BootstrapSummary(
        home=home,
        config_present=paths.config_path().exists(),
        db_present=paths.db_path().exists(),
        flows_count=flows_n,
        runs_count=runs_n,
        agents_count=agents_n,
        skills_source_count=skills_n,
    )
