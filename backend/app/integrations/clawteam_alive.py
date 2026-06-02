"""Agent liveness probe — the only place we still embed ClawTeam internals.

Public API:
* :func:`is_agent_alive` — True / False / None per ClawTeam's own probe.
* :func:`list_dead_agents` — list of agent names whose processes have died.

Why embedded (DEV.md §4 / plan §3.1)?
* ``spawn_registry.is_agent_alive`` and ``list_dead_agents`` are the only
  ClawTeam capabilities not exposed via CLI nor MCP today. Re-implementing
  the tmux pane / PID / wsh probing logic ourselves would duplicate ~60 lines
  of ClawTeam internals; instead we depend on the public Python module
  (``clawteam.spawn.registry``) and ringfence that dependency to this single
  file so anyone reading the codebase finds it once and knows where to look
  if a future ClawTeam version moves it.
* If ClawTeam later ships ``clawteam spawn alive ...`` we swap the
  implementation here without touching call sites.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Local import deferred to call-time so ``import app.integrations`` doesn't
# hard-require the clawteam wheel during early test discovery.
if TYPE_CHECKING:  # pragma: no cover
    pass


def is_agent_alive(team_name: str, agent_name: str) -> bool | None:
    """True if the agent process is alive, False if dead, None if no spawn record.

    Delegates to ``clawteam.spawn.registry.is_agent_alive``.
    """
    from clawteam.spawn.registry import is_agent_alive as _impl

    return _impl(team_name, agent_name)


def list_dead_agents(team_name: str) -> list[str]:
    """Return names of agents in *team_name* whose processes are dead."""
    from clawteam.spawn.registry import list_dead_agents as _impl

    return list(_impl(team_name))


__all__ = ["is_agent_alive", "list_dead_agents"]
