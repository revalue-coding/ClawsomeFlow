"""In-process status registry for long-running user operations.

Long create/install operations (Hermes/OpenClaw agent create, OpenClaw import,
Agent Store load) used to keep their in-progress/outcome state ONLY in the
browser (sessionStorage + a 3s poll), so a page refresh or tab close lost it —
or worse, surfaced a false outcome. This module makes the *server* the source of
truth: each operation records ``running → succeeded|failed`` here, the frontend
queries the terminal state on mount (``GET /api/operations/{op_id}``) and
subscribes to live transitions over WebSocket (``/ws/op/{op_id}``).

Design (user decision: "只需终态可查" — live-only, no replay):

* **In-memory + capped** — a process-wide :class:`_OpRegistry` (bounded
  ``OrderedDict``, oldest evicted). We do NOT persist op events: the server is
  long-lived in local mode, and a restart kills the operation anyway. Recovery
  after eviction/restart is handled by the GET endpoint's entity-existence
  fallback layer (see :mod:`app.api.operations`).
* **Thread-safe** — Hermes create runs in a thread executor, so the registry is
  guarded by a ``threading.Lock``. (Status frames are still *published* only
  from the event loop — see :mod:`app.api` wiring — so the asyncio-based bus is
  never touched cross-thread.)
* **Live fanout reuses the Run event bus** (:mod:`app.events`) on a namespaced
  channel ``op:{op_id}`` so the WS layer gets the same bounded-queue fanout.

op_id convention: ``f"{kind}:{target}"`` — e.g. ``hermes_create:math``,
``openclaw_create:alice``, ``openclaw_import:src1``, ``store_load:lst_42``.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from app.logging_setup import get_logger

logger = get_logger("operations")

OpState = Literal["running", "succeeded", "failed"]

_MAX_OPS = 256


@dataclass(frozen=True, slots=True)
class OpStatus:
    """A single operation's current status (one transition snapshot)."""

    op_id: str
    user: str
    kind: str  # "hermes_create" | "openclaw_create" | "openclaw_import" | "store_load"
    state: OpState
    detail: str = ""  # human/error string; "cancelled" on user cancel
    result: dict[str, Any] = field(default_factory=dict)  # e.g. {"agentId": "..."}
    ts: float = 0.0  # time.time() of this transition

    def to_frame(self) -> dict[str, Any]:
        """WS/REST wire shape (camelCase to match the API's _CamelModel siblings)."""
        return {
            "opId": self.op_id,
            "kind": self.kind,
            "state": self.state,
            "detail": self.detail,
            "result": self.result,
            "ts": self.ts,
        }


def op_channel(op_id: str) -> str:
    """Event-bus channel key for an operation.

    Namespaced with ``op:`` so it can never collide with a Run channel (Run
    events publish under the bare ``run_id``).
    """
    return f"op:{op_id}"


class _OpRegistry:
    """Process-wide, bounded, thread-safe operation-status registry."""

    def __init__(self, *, max_ops: int = _MAX_OPS) -> None:
        self._lock = threading.Lock()
        self._ops: OrderedDict[str, OpStatus] = OrderedDict()
        self._max = max_ops

    def _publish(self, op: OpStatus) -> None:
        # Lazy import to avoid an events<->operations import cycle and so tests
        # that reset the bus singleton see the live instance.
        from app.events import get_event_broadcaster

        get_event_broadcaster().publish(op_channel(op.op_id), {"type": "op_status", **op.to_frame()})

    def start(self, *, op_id: str, user: str, kind: str, detail: str = "") -> OpStatus:
        """Record (or restart) an operation as ``running`` and publish it."""
        op = OpStatus(op_id=op_id, user=user, kind=kind, state="running", detail=detail, ts=time.time())
        with self._lock:
            self._ops.pop(op_id, None)  # restart of a retried op replaces the old entry
            self._ops[op_id] = op
            while len(self._ops) > self._max:
                self._ops.popitem(last=False)  # evict oldest
        self._publish(op)
        return op

    def succeed(
        self, op_id: str, *, result: dict[str, Any] | None = None, detail: str = ""
    ) -> OpStatus:
        return self._terminate(op_id, "succeeded", detail=detail, result=result)

    def fail(self, op_id: str, *, detail: str = "", result: dict[str, Any] | None = None) -> OpStatus:
        return self._terminate(op_id, "failed", detail=detail, result=result)

    def _terminate(
        self, op_id: str, state: OpState, *, detail: str, result: dict[str, Any] | None
    ) -> OpStatus:
        with self._lock:
            prev = self._ops.get(op_id)
            if prev is None:
                # Terminal recorded for an op we never start()'d (e.g. the process
                # restarted mid-op). Synthesize a bare entry so the frame still fans
                # out to any live subscriber.
                prev = OpStatus(op_id=op_id, user="", kind="", state="running")
            op = replace(
                prev,
                state=state,
                detail=detail or prev.detail,
                result=result if result is not None else prev.result,
                ts=time.time(),
            )
            self._ops[op_id] = op
            self._ops.move_to_end(op_id)
        self._publish(op)
        return op

    def get(self, op_id: str, *, user: str) -> OpStatus | None:
        """Return the op's status if it exists and is owned by *user*."""
        with self._lock:
            op = self._ops.get(op_id)
        if op is None or (op.user and op.user != user):
            return None
        return op


# ── singleton ──────────────────────────────────────────────────────

_singleton: _OpRegistry | None = None


def get_op_registry() -> _OpRegistry:
    """Return the process-wide :class:`_OpRegistry`."""
    global _singleton
    if _singleton is None:
        _singleton = _OpRegistry()
    return _singleton


def reset_op_registry() -> None:
    """Drop the cached singleton (used by tests)."""
    global _singleton
    _singleton = None


__all__ = [
    "OpStatus",
    "OpState",
    "get_op_registry",
    "reset_op_registry",
    "op_channel",
]
