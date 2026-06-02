"""Shared validator for AI task-decompose proposals."""

from __future__ import annotations

from typing import Any


class ProposalValidationError(Exception):
    """Raised when a decomposer payload violates Flow invariants."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def validate_decompose_proposal(
    agents: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    expected_leader: str,
) -> None:
    """Validate the minimal scheduler invariants for a proposed DAG."""
    if not agents:
        raise ProposalValidationError(
            "INVALID_PROPOSAL",
            "decomposer returned no agents.",
        )
    if not tasks:
        raise ProposalValidationError(
            "INVALID_PROPOSAL",
            "decomposer returned no tasks.",
        )

    agent_ids = {a.get("id") for a in agents if a.get("id")}
    task_ids: set[str] = set()
    leader_summary_owner: str | None = None
    leader_summary_count = 0

    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ProposalValidationError(
                "INVALID_PROPOSAL",
                f"task missing 'id': {task!r}",
            )
        if task_id in task_ids:
            raise ProposalValidationError(
                "INVALID_PROPOSAL",
                f"duplicate task id {task_id!r}",
            )
        task_ids.add(task_id)

        owner_raw = task.get("ownerAgentId")
        if owner_raw is None:
            owner_raw = task.get("owner_agent_id")
        owner = owner_raw if isinstance(owner_raw, str) else ""

        is_summary = bool(task.get("isLeaderSummary") or task.get("is_leader_summary"))
        if is_summary:
            if not owner:
                raise ProposalValidationError(
                    "INVALID_PROPOSAL",
                    f"leader-summary task {task_id!r} must have ownerAgentId set",
                )
            if owner not in agent_ids:
                raise ProposalValidationError(
                    "INVALID_PROPOSAL",
                    f"task {task_id!r} has unknown ownerAgentId={owner!r}",
                )
            leader_summary_count += 1
            leader_summary_owner = owner
        else:
            # Non-summary task may leave owner empty for manual selection.
            if owner and owner not in agent_ids:
                raise ProposalValidationError(
                    "INVALID_PROPOSAL",
                    f"task {task_id!r} has unknown ownerAgentId={owner!r}",
                )

    if leader_summary_count != 1:
        raise ProposalValidationError(
            "INVALID_PROPOSAL",
            "exactly one task must have isLeaderSummary=true "
            f"(got {leader_summary_count})",
        )
    if leader_summary_owner != expected_leader:
        raise ProposalValidationError(
            "INVALID_PROPOSAL",
            f"leader-summary task must be owned by leader {expected_leader!r}, "
            f"got {leader_summary_owner!r}",
        )

    # Leader cannot own non-summary tasks.
    for task in tasks:
        owner = task.get("ownerAgentId") or task.get("owner_agent_id") or ""
        if owner == expected_leader and not (
            task.get("isLeaderSummary") or task.get("is_leader_summary")
        ):
            raise ProposalValidationError(
                "INVALID_PROPOSAL",
                f"leader {expected_leader!r} cannot own non-summary task "
                f"{task.get('id')!r}",
            )
