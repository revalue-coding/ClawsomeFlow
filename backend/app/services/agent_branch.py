"""Normalize non-OpenClaw agent target branches against on-disk git repos."""

from __future__ import annotations

from typing import Any

from app.integrations.git_repo import resolve_target_branch
from app.models import AgentKind, FlowAgent, FlowSpec


def normalize_agent_branch_dict(agent: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *agent* with a validated ``targetBranch`` when applicable.

    Leader entries and agents without a repo are left unchanged here; decompose
    apply restores user-defined leader/worker bindings from the editor snapshot
    afterward (see ``task_decompose._merge_decompose_editor_snapshot``).
    """
    out = dict(agent)
    if bool(out.get("isLeader") or out.get("is_leader")):
        return out
    kind = str(out.get("kind") or "").strip().lower()
    if kind == AgentKind.openclaw.value:
        return out
    repo = str(out.get("repo") or "").strip()
    if not repo:
        return out
    raw = str(out.get("targetBranch") or out.get("target_branch") or "").strip()
    resolved = resolve_target_branch(repo, raw or None)
    out["targetBranch"] = resolved
    out.pop("target_branch", None)
    return out


def normalize_agent_branch_dicts(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_agent_branch_dict(a) for a in agents]


def normalize_flow_spec_branches(spec: FlowSpec) -> None:
    """In-place: align every non-OpenClaw agent target branch with the git repo."""
    for agent in spec.agents:
        _normalize_flow_agent_branch(agent)


def _normalize_flow_agent_branch(agent: FlowAgent) -> None:
    if agent.kind == AgentKind.openclaw or not agent.repo or agent.is_leader:
        return
    agent.target_branch = resolve_target_branch(agent.repo, agent.target_branch)
