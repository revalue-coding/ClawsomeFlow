"""Default MCP-backed snapshot / leader-inbox providers.

These are the production implementations of the DI hooks
:class:`RunController.snapshot_provider` and ``leader_inbox_provider``
that Phase 5 introduced for testability. With them wired in,
:class:`RunController` can talk to a live ClawTeam without any test stubs.

What they do:

* :class:`McpSnapshotProvider` — calls
  :meth:`ClawTeamMcpClient.task_list` once per controller tick and translates
  every ClawTeam task dict into a :class:`TaskSnapshot`. Translation:
    - Status string passes through (matches our ``TaskSnapshot.status``).
    - ``locked_by`` reads either snake_case or camelCase (``lockedBy``) field.
    - ``metadata`` is the per-task metadata dict (already includes our
      ``csflow_task_id`` tag from the compiler).
    - ``task_id`` is mapped back to ``FlowTask.id`` via the compiler's
      ``clawteam_to_flow`` table; tasks created by ClawsomeFlow always have
      ``metadata.csflow_task_id`` so the lookup is reliable even after a
      controller restart (ClawTeam ids are not stable across cleanup).
    - ``dispatched_at_epoch`` is pulled from a small in-process bookkeeping
      table the controller updates on every dispatch — this is the only
      piece MCP can't provide because dispatch is a ClawsomeFlow-side
      concept.

* :class:`McpLeaderInboxProvider` — calls
  :meth:`ClawTeamMcpClient.mailbox_peek` (non-destructive) for the leader and returns the
  raw payload list. The controller's structured-inbox helper then unpacks
  ``from_agent`` / ``content`` / ``task_id`` from each row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.integrations.clawteam_mcp import ClawTeamMcpClient
from app.logging_setup import get_logger
from app.scheduler.compiler import CSFLOW_TASK_ID_KEY, CompileResult
from app.scheduler.failure import TaskSnapshot

logger = get_logger("scheduler.providers")


@dataclass
class DispatchClock:
    """In-process map of task_id (Flow-side) → epoch dispatched_at.

    Owned by the controller; the snapshot provider reads it to populate
    :attr:`TaskSnapshot.dispatched_at_epoch` (failure detection's timeout
    signal needs it). The controller updates it whenever a task moves
    pending → dispatched.
    """

    table: dict[str, float] = field(default_factory=dict)

    def mark(self, flow_task_id: str, epoch: float) -> None:
        self.table[flow_task_id] = epoch

    def reset(self, flow_task_id: str) -> None:
        self.table.pop(flow_task_id, None)


# ──────────────────────────────────────────────────────────────────────
# Snapshot provider
# ──────────────────────────────────────────────────────────────────────


class McpSnapshotProvider:
    """Async callable that returns the live :class:`TaskSnapshot` set."""

    def __init__(
        self,
        *,
        team_name: str,
        compile_result: CompileResult,
        mcp: ClawTeamMcpClient,
        dispatch_clock: DispatchClock,
    ) -> None:
        self.team_name = team_name
        self.compile_result = compile_result
        self.mcp = mcp
        self.dispatch_clock = dispatch_clock

    async def __call__(self) -> list[TaskSnapshot]:
        try:
            rows = await self.mcp.task_list(self.team_name)
        except Exception as exc:
            logger.warning("snapshot_fetch_failed", team=self.team_name, error=str(exc))
            return []
        return [self._convert(r) for r in rows if self._convert(r) is not None]

    # ── conversion ────────────────────────────────────────────────────

    def _convert(self, row: dict[str, Any]) -> TaskSnapshot | None:
        flow_id = self._resolve_flow_id(row)
        if flow_id is None:
            return None
        return TaskSnapshot(
            task_id=flow_id,
            owner_agent_id=row.get("owner") or "",
            status=row.get("status") or "pending",
            locked_by_agent=row.get("locked_by") or row.get("lockedBy") or None,
            metadata=row.get("metadata") or {},
            dispatched_at_epoch=self.dispatch_clock.table.get(flow_id),
        )

    def _resolve_flow_id(self, row: dict[str, Any]) -> str | None:
        """Prefer the metadata tag (compiler stamped it); fall back to map.

        Skips tasks we didn't create (defensive — e.g. a future feature
        adds out-of-band ClawTeam tasks; we ignore them silently).
        """
        meta = row.get("metadata") or {}
        tagged = meta.get(CSFLOW_TASK_ID_KEY)
        if isinstance(tagged, str):
            return tagged
        ct_id = row.get("id")
        if isinstance(ct_id, str):
            mapped = self.compile_result.clawteam_to_flow.get(ct_id)
            if mapped:
                return mapped
        return None


# ──────────────────────────────────────────────────────────────────────
# Leader inbox provider
# ──────────────────────────────────────────────────────────────────────


class McpLeaderInboxProvider:
    """Async callable returning leader's inbox messages as raw dicts.

    The controller's :meth:`_fetch_leader_inbox_structured` understands both
    dict and string forms; we always emit dicts so ``from_agent`` survives.
    Falls back to ``[]`` on transient MCP errors so the loop keeps running.
    """

    def __init__(
        self,
        *,
        team_name: str,
        leader_agent_id: str,
        mcp: ClawTeamMcpClient,
        peek: bool = True,
        limit: int = 20,
    ) -> None:
        self.team_name = team_name
        self.leader_agent_id = leader_agent_id
        self.mcp = mcp
        self.peek = peek
        self.limit = limit

    async def __call__(self) -> list[dict[str, Any]]:
        try:
            if self.peek:
                rows = await self.mcp.mailbox_peek(self.team_name, self.leader_agent_id)
            else:
                rows = await self.mcp.mailbox_receive(
                    self.team_name, self.leader_agent_id, limit=self.limit,
                )
        except Exception as exc:
            logger.warning(
                "leader_inbox_fetch_failed",
                team=self.team_name, leader=self.leader_agent_id, error=str(exc),
            )
            return []
        return list(rows)


__all__ = [
    "DispatchClock",
    "McpLeaderInboxProvider",
    "McpSnapshotProvider",
]
