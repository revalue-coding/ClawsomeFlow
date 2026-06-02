"""Tests for :mod:`app.integrations.clawteam_alive`.

Verifies the embedded probe behaves as documented:
* ``None`` for an unknown agent (no spawn record).
* Returns sensible result for a known one (we can't safely test ``True``/``False``
  in unit tests because that would require a real running tmux pane).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile

import pytest

from app.integrations.clawteam_alive import is_agent_alive, list_dead_agents


requires_clawteam_pkg = pytest.mark.skipif(
    not shutil.which("clawteam"),
    reason="clawteam package not installed",
)


@requires_clawteam_pkg
def test_unknown_agent_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    tmp = tempfile.mkdtemp(prefix="csflow_alive_")
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", tmp)
    # No team / no spawn registry → None.
    assert is_agent_alive("nope-team", "nope-agent") is None


@requires_clawteam_pkg
def test_list_dead_for_empty_team(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    tmp = tempfile.mkdtemp(prefix="csflow_alive_")
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", tmp)
    # Create a team with no spawned members. Pass full inherited env so
    # clawteam can find its own dependencies / config defaults.
    env = {**os.environ, "CLAWTEAM_DATA_DIR": tmp}
    subprocess.run(
        ["clawteam", "team", "spawn-team", "csflow-alive-empty",
         "-n", "leader", "-d", "test"],
        env=env, check=True, capture_output=True,
    )
    assert list_dead_agents("csflow-alive-empty") == []
