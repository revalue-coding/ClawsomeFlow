"""Flow → ClawTeam compilation (per plan §8.3).

Compiles a :class:`FlowSpec` into a freshly-created ClawTeam team + tasks.
The compiler is **idempotent over a fresh team_name** — every Run uses a
unique team_name (``csflow-{run_id_short}``) so we never need "upsert"
semantics. If compilation fails partway, the partial state is left for
``clawteam team cleanup`` to wipe (called from the failure path).

What this module does NOT do:
* It does NOT spawn worker processes — that's :class:`WorkerSession`.
* It does NOT issue dispatch messages — that's :func:`build_worker_dispatch`
  + :class:`RunController`.
* It does NOT mutate ``Flow`` / ``FlowRun`` rows — caller's job.

Returned :class:`CompileResult` carries the **task-id mapping**:

* ``flow_to_clawteam[FlowTask.id]`` → assigned 8-char hex ClawTeam task id.
* ``clawteam_to_flow[clawteam_id]`` → original FlowTask.id (used by the
  snapshot translator to keep the controller's bookkeeping keyed on the
  Flow-side ids the user authored).

We also stamp ``metadata.csflow_task_id = FlowTask.id`` on every ClawTeam
task so a snapshot can recover the mapping even when our in-memory state
is gone (e.g. server-mode failover).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.integrations.clawteam_cli import ClawTeamCli, get_clawteam_cli
from app.integrations.clawteam_mcp import ClawTeamMcpClient, get_mcp_client
from app.logging_setup import get_logger
from app.models import AgentKind, FlowSpec, FlowTask
from app.user_context import get_request_user, set_request_user

logger = get_logger("scheduler.compiler")


CSFLOW_TASK_ID_KEY = "csflow_task_id"     # metadata key on ClawTeam tasks
CSFLOW_KIND_KEY = "csflow_kind"           # tags an internal task (e.g. self_merge)


@dataclass
class CompileResult:
    """Return value of :func:`compile_flow_to_clawteam`."""

    team_name: str
    leader_agent_id: str
    flow_to_clawteam: dict[str, str] = field(default_factory=dict)
    clawteam_to_flow: dict[str, str] = field(default_factory=dict)
    member_count: int = 0


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


async def compile_flow_to_clawteam(
    *,
    spec: FlowSpec,
    team_name: str,
    user: str,
    flow_description: str = "",
    cli: ClawTeamCli | None = None,
    mcp: ClawTeamMcpClient | None = None,
) -> CompileResult:
    """Materialise *spec* into a brand-new ClawTeam team + tasks.

    Steps (in order, per plan §8.3):

    1. ``clawteam team spawn-team {team}`` (CLI) — registers leader metadata
       (no process). Using the CLI here matches the public surface plan §5.1
       table prescribes.
    2. ``team_member_add`` (MCP) — register every other agent's metadata.
    3. ``task_create`` (MCP) — in topological order so ``blocked_by`` IDs are
       known. We tag every task with ``metadata.csflow_task_id = FlowTask.id``.

    Returns a :class:`CompileResult` with the bidirectional id mapping the
    controller needs to translate ClawTeam task statuses into its own
    bookkeeping.
    """
    prev_user = get_request_user()
    set_request_user(user)
    try:
        cli = cli or get_clawteam_cli()
        if mcp is None:
            mcp = await get_mcp_client(user=user)

        leader_agent = next(a for a in spec.agents if a.is_leader)

        logger.info(
            "compile_start",
            team=team_name, agent_count=len(spec.agents),
            task_count=len(spec.tasks),
        )

        # Step 1: create team + register leader (CLI per plan §5.1).
        await cli.team_spawn_team(
            team=team_name,
            agent_name=leader_agent.id,
            agent_type=_clawteam_agent_type(leader_agent.kind),
            description=flow_description or f"ClawsomeFlow Run team {team_name}",
        )

        # Step 2: register other agent metadata via MCP.
        for agent in spec.agents:
            if agent.id == leader_agent.id:
                continue
            await mcp.team_member_add(
                team_name=team_name,
                member_name=agent.id,
                agent_id=agent.id,
                agent_type=_clawteam_agent_type(agent.kind),
                user=user,
            )

        # Step 3: create all tasks in dependency order so blocked_by ids resolve.
        flow_to_clawteam: dict[str, str] = {}
        clawteam_to_flow: dict[str, str] = {}
        for ftask in _toposort_tasks(spec.tasks):
            blocked_by_ct = [flow_to_clawteam[d] for d in ftask.depends_on
                             if d in flow_to_clawteam]
            meta = {
                CSFLOW_TASK_ID_KEY: ftask.id,
                "timeout_seconds": ftask.timeout_seconds,
            }
            result = await mcp.task_create(
                team_name=team_name,
                subject=ftask.subject,
                description=ftask.description,
                owner=ftask.owner_agent_id,
                blocked_by=blocked_by_ct or None,
                metadata=meta,
            )
            # task_create returns the created task. ClawTeam's CLI used to wrap it
            # in `{"task": {...}}` for some versions; tolerate both.
            ct_id = _extract_task_id(result)
            flow_to_clawteam[ftask.id] = ct_id
            clawteam_to_flow[ct_id] = ftask.id

        res = CompileResult(
            team_name=team_name,
            leader_agent_id=leader_agent.id,
            flow_to_clawteam=flow_to_clawteam,
            clawteam_to_flow=clawteam_to_flow,
            member_count=len(spec.agents),
        )
        logger.info(
            "compile_complete",
            team=team_name, tasks_created=len(flow_to_clawteam),
            member_count=res.member_count,
        )
        return res
    finally:
        set_request_user(prev_user)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _clawteam_agent_type(kind: AgentKind) -> str:
    """ClawTeam's ``agent_type`` is informational; map our enum to a stable string.

    The leader uses ``"leader"``; everything else maps 1:1 onto the kind name
    (which ClawTeam treats as opaque tags).
    """
    return kind.value


def _toposort_tasks(tasks: list[FlowTask]) -> list[FlowTask]:
    """Stable topological sort: dependencies first, ties broken by author order.

    The Flow validator (Phase 1) already rejected cycles, so a single Kahn
    pass suffices. We preserve the user's authored order among siblings so
    log diffing (and snapshot tests) stay deterministic.
    """
    by_id = {t.id: t for t in tasks}
    in_deg = {t.id: sum(1 for d in t.depends_on if d in by_id) for t in tasks}
    # Walk in original order to keep stable.
    out: list[FlowTask] = []
    queue = [t for t in tasks if in_deg[t.id] == 0]
    while queue:
        head = queue.pop(0)
        out.append(head)
        for t in tasks:
            if head.id in t.depends_on:
                in_deg[t.id] -= 1
                if in_deg[t.id] == 0:
                    queue.append(t)
    if len(out) != len(tasks):  # pragma: no cover — validators forbid cycles
        missing = [t.id for t in tasks if t not in out]
        raise RuntimeError(f"toposort failed; possible cycle around {missing}")
    return out


def _extract_task_id(result: dict | None) -> str:
    """Tolerate both ``{"id": "..."}`` and ``{"task": {"id": "..."}}``."""
    if not result:
        raise RuntimeError("task_create returned no payload")
    if "id" in result and isinstance(result["id"], str):
        return result["id"]
    nested = result.get("task")
    if isinstance(nested, dict) and isinstance(nested.get("id"), str):
        return nested["id"]
    raise RuntimeError(f"task_create payload missing 'id': {result!r}")


__all__ = [
    "CSFLOW_KIND_KEY",
    "CSFLOW_TASK_ID_KEY",
    "CompileResult",
    "compile_flow_to_clawteam",
]
