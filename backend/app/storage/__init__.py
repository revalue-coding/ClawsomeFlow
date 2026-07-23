"""Storage backend abstraction.

Public API:
* :class:`StorageBackend` — Protocol describing every persistence operation
  the application layer is allowed to perform. The concrete implementation
  lives alongside (:mod:`app.storage.sqlite`).
* :func:`get_storage` — lazy singleton.
* :func:`reset_storage` — used by tests.

Why a Protocol (not a base class)?
* Lets the SQLite implementation use SQLModel sessions directly without
  needing to inherit from a half-empty parent.
* Makes test fakes trivial.
* Forces every method to have an explicit signature (no shared state to
  accidentally bypass).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.config import Config, load_config
from app.models import (
    AgentStoreOrder,
    AgentStoreOwnership,
    ChatMessageRow,
    Flow,
    FlowRun,
    FlowRunSchedule,
    FlowRunScheduleExecution,
    HermesAgent,
    OpenclawAgent,
    OpenclawAgentRequest,
    OpenclawTeam,
    RunEvent,
    TaskDecomposeRequest,
)


@runtime_checkable
class StorageBackend(Protocol):
    """All persistence operations exposed to the application."""

    # ---- Lifecycle ----
    def init_schema(self) -> None: ...
    def close(self) -> None: ...

    # ---- Flows ----
    def flow_create(self, flow: Flow) -> Flow: ...
    def flow_get(self, flow_id: str) -> Flow | None: ...
    def flow_list(
        self, *, owner_user: str | None = None, q: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[list[Flow], int]: ...
    def flow_update(self, flow: Flow, *, expected_version: int) -> Flow:
        """Optimistic-locking update.

        Bumps ``flow.version`` and ``flow.updated_at`` atomically.
        Raises :class:`StorageVersionConflict` if the on-disk version doesn't
        match ``expected_version``.
        """
        ...

    def flow_delete(self, flow_id: str) -> bool: ...

    # ---- FlowRuns ----
    def run_create(self, run: FlowRun) -> FlowRun: ...
    def run_get(self, run_id: str) -> FlowRun | None: ...
    def run_list(
        self, *, flow_id: str | None = None, status: str | None = None,
        user: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[list[FlowRun], int]: ...
    def run_update(self, run: FlowRun) -> FlowRun: ...
    def run_clear_history(self, *, user: str | None = None) -> dict[str, int]: ...
    def run_count_active_for_flow(self, flow_id: str) -> int: ...
    def run_count_active_for_openclaw_agent(self, agent_id: str) -> int: ...
    def list_active_driving_runs(self) -> list[FlowRun]: ...
    def count_active_driving_runs(self) -> int: ...
    def run_schedule_create(self, schedule: FlowRunSchedule) -> FlowRunSchedule: ...
    def run_schedule_get(self, schedule_id: str) -> FlowRunSchedule | None: ...
    def run_schedule_list(self, *, user: str | None = None) -> list[FlowRunSchedule]: ...
    def run_schedule_list_due(
        self, *, before: datetime, limit: int = 50,
    ) -> list[FlowRunSchedule]: ...
    def run_schedule_update(self, schedule: FlowRunSchedule) -> FlowRunSchedule: ...
    def run_schedule_delete(self, schedule_id: str) -> bool: ...
    def run_schedule_execution_create(
        self, execution: FlowRunScheduleExecution,
    ) -> FlowRunScheduleExecution: ...
    def run_schedule_execution_get(
        self, execution_id: str,
    ) -> FlowRunScheduleExecution | None: ...
    def run_schedule_execution_list(
        self,
        *,
        user: str | None = None,
        schedule_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FlowRunScheduleExecution], int]: ...
    def run_schedule_execution_clear(self, *, user: str | None = None) -> int: ...
    def run_schedule_execution_list_running(self) -> list[FlowRunScheduleExecution]: ...
    def run_schedule_execution_update(
        self, execution: FlowRunScheduleExecution,
    ) -> FlowRunScheduleExecution: ...

    # ---- RunEvents ----
    def event_append(self, event: RunEvent) -> RunEvent: ...
    def event_list(
        self, *, run_id: str, since_id: int | None = None, limit: int = 100,
    ) -> list[RunEvent]: ...
    def event_task_ids_with_type(self, *, run_id: str, type: str) -> set[str]: ...
    def history_cleanup(self, *, before: datetime | None = None) -> dict[str, object]: ...

    # ---- Chat history (single-agent direct chat) ----
    def chat_message_append(self, row: ChatMessageRow) -> ChatMessageRow: ...
    def chat_message_list(
        self, *, conversation_key: str, limit: int = 500,
    ) -> list[ChatMessageRow]: ...
    def chat_message_delete_conversation(self, *, conversation_key: str) -> int: ...
    def chat_message_pop_trailing_user(self, *, conversation_key: str) -> bool: ...

    # ---- OpenclawAgents ----
    def openclaw_team_create(self, team: OpenclawTeam) -> OpenclawTeam: ...
    def openclaw_team_get(self, team_id: str) -> OpenclawTeam | None: ...
    def openclaw_team_list(self, *, owner_user: str | None = None) -> list[OpenclawTeam]: ...
    def openclaw_team_update(self, team: OpenclawTeam) -> OpenclawTeam: ...
    def openclaw_create(self, agent: OpenclawAgent) -> OpenclawAgent: ...
    def openclaw_get(self, agent_id: str) -> OpenclawAgent | None: ...
    def openclaw_list(self, *, owner_user: str | None = None) -> list[OpenclawAgent]: ...
    def openclaw_update(self, agent: OpenclawAgent) -> OpenclawAgent: ...
    def openclaw_delete(self, agent_id: str) -> bool: ...

    # ---- HermesAgents ----
    def hermes_create(self, agent: HermesAgent) -> HermesAgent: ...
    def hermes_get(self, agent_id: str) -> HermesAgent | None: ...
    def hermes_list(self, *, owner_user: str | None = None) -> list[HermesAgent]: ...
    def hermes_update(self, agent: HermesAgent) -> HermesAgent: ...
    def hermes_delete(self, agent_id: str) -> bool: ...

    # ---- AgentStore ----
    def agent_store_ownership_create(self, row: AgentStoreOwnership) -> AgentStoreOwnership: ...
    def agent_store_ownership_get(
        self, *, owner_user: str, listing_id: str,
    ) -> AgentStoreOwnership | None: ...
    def agent_store_ownership_list(
        self, *, owner_user: str,
    ) -> list[AgentStoreOwnership]: ...
    def agent_store_ownership_update(self, row: AgentStoreOwnership) -> AgentStoreOwnership: ...
    def agent_store_order_create(self, row: AgentStoreOrder) -> AgentStoreOrder: ...
    def agent_store_order_list(
        self,
        *,
        owner_user: str,
        listing_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AgentStoreOrder], int]: ...
    def agent_store_order_update(self, row: AgentStoreOrder) -> AgentStoreOrder: ...

    # ---- OpenclawAgentRequests (async NL creation tracking) ----
    def openclaw_request_create(
        self, request: OpenclawAgentRequest,
    ) -> OpenclawAgentRequest: ...
    def openclaw_request_get(
        self, request_id: str,
    ) -> OpenclawAgentRequest | None: ...
    def openclaw_request_update(
        self, request: OpenclawAgentRequest,
    ) -> OpenclawAgentRequest: ...
    def openclaw_request_list(
        self, *, user: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[list[OpenclawAgentRequest], int]: ...

    # ---- TaskDecomposeRequests (async AI decompose tracking) ----
    def task_decompose_create(
        self, request: TaskDecomposeRequest,
    ) -> TaskDecomposeRequest: ...
    def task_decompose_get(
        self, request_id: str,
    ) -> TaskDecomposeRequest | None: ...
    def task_decompose_update(
        self, request: TaskDecomposeRequest,
    ) -> TaskDecomposeRequest: ...
    def task_decompose_list(
        self, *, user: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[list[TaskDecomposeRequest], int]: ...


class StorageVersionConflict(Exception):
    """Raised by ``flow_update`` when ``expected_version`` doesn't match."""

    def __init__(self, *, flow_id: str, expected: int, actual: int):
        super().__init__(f"version conflict on flow {flow_id}: expected {expected}, got {actual}")
        self.flow_id = flow_id
        self.expected = expected
        self.actual = actual


# ──────────────────────────────────────────────────────────────────────
# Singleton resolution
# ──────────────────────────────────────────────────────────────────────

_singleton: StorageBackend | None = None


def get_storage(config: Config | None = None) -> StorageBackend:
    """Return the process-wide :class:`StorageBackend`, creating it on demand."""
    global _singleton
    if _singleton is not None:
        return _singleton
    if config is None:
        load_config()  # ensure the config file exists / cache is primed
    from app.storage.sqlite import SqliteStorage  # local import -> break cycle

    _singleton = SqliteStorage()
    _singleton.init_schema()
    return _singleton


def reset_storage() -> None:
    """Drop the cached singleton (used by tests). Closes the existing one."""
    global _singleton
    if _singleton is not None:
        try:
            _singleton.close()
        except Exception:
            pass
    _singleton = None


__all__ = [
    "StorageBackend",
    "StorageVersionConflict",
    "get_storage",
    "reset_storage",
]
