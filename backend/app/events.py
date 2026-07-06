"""In-process Run-event broadcaster.

ClawsomeFlow's WebSocket layer (Phase 7) needs to push every
:class:`RunEvent` the controller / finalizer emits to all clients
subscribed to that Run. We keep the broker in-process for local mode (one
``asyncio.Queue`` per subscriber, fanout via :meth:`publish`) and design
the public surface so the server-mode (Phase 9) can swap it for a Redis
pub/sub backend without touching call sites.

Usage::

    bus = get_event_broadcaster()

    # Producer (controller / finalize):
    bus.publish(run_id, {"id": ev.id, "type": ev.type, "payload": ev.payload, ...})

    # Consumer (websocket route):
    async with bus.subscribe(run_id) as queue:
        while True:
            event = await queue.get()
            ...

Properties:

* **Bounded queues** — each subscriber gets a ``maxsize=512`` queue. If a
  consumer is too slow we drop the OLDEST event in the queue (FIFO drop)
  and tag the next message with ``dropped: True`` so the client can call
  ``GET /api/runs/{id}/events?since=<id>`` to catch up. Plan §10.2 says
  the WebSocket fanout is the broadcaster's job and clients may need to
  reconcile via the events endpoint anyway.
* **Subscribe is a context manager** — guarantees unsubscription even if
  the consumer raises.
* **Publish is sync + non-blocking** — never awaits, so producers don't
  pay for slow subscribers; this is the whole point of having queues.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import TYPE_CHECKING, Any, AsyncIterator

from app.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover — typing only, avoids import cycles
    from app.models import RunEvent
    from app.storage import StorageBackend

logger = get_logger("events")


_DEFAULT_QUEUE_SIZE = 512


class EventBroadcaster:
    """Process-wide Run-event fanout (one logical channel per ``run_id``)."""

    def __init__(self, *, queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        # We use a lock-free dict-of-sets and rely on asyncio's single-thread
        # nature to keep mutations safe (publish + subscribe both run on the
        # event loop).
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    # ── consumer surface ────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def subscribe(
        self, run_id: str,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Async context manager yielding the per-subscriber queue.

        ``maxsize=queue_size`` so a slow consumer can be detected and
        gracefully degraded (FIFO drop + ``dropped: True`` tag on next
        publish, see :meth:`publish`).
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subs[run_id].add(queue)
        logger.debug("event_subscribe", run_id=run_id, total=len(self._subs[run_id]))
        try:
            yield queue
        finally:
            self._subs[run_id].discard(queue)
            if not self._subs[run_id]:
                self._subs.pop(run_id, None)
            logger.debug("event_unsubscribe", run_id=run_id)

    # ── producer surface ────────────────────────────────────────────

    def publish(self, run_id: str, payload: dict[str, Any]) -> int:
        """Fan *payload* out to every subscriber of *run_id*.

        Returns the number of subscribers reached. Never awaits — slow
        subscribers get a dropped queue head + ``dropped: True`` flag.
        """
        subs = self._subs.get(run_id)
        if not subs:
            return 0
        delivered = 0
        for q in list(subs):
            if q.full():
                # Drop the oldest event to make room for the latest.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — race
                    pass
                payload = {**payload, "dropped": True}
            try:
                q.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:  # pragma: no cover — drop succeeded above
                logger.warning(
                    "event_publish_dropped",
                    run_id=run_id, event_type=payload.get("type"),
                )
        logger.debug(
            "event_publish",
            run_id=run_id, event_type=payload.get("type"),
            sub_count=len(subs), delivered=delivered,
        )
        return delivered

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))


# ── persist + broadcast helper ─────────────────────────────────────


def publish_run_event(
    storage: StorageBackend,
    *,
    run_id: str,
    event_type: str,
    agent_id: str | None = None,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> RunEvent | None:
    """Persist one :class:`RunEvent` and fan it out to WS subscribers.

    Canonical helper shared by the controller / finalize / engine emitters
    (they used to carry near-identical copies of this block). Semantics:

    * Persist first (DB is the durable record); a persist failure is logged
      and returns ``None`` — nothing is broadcast for an unpersisted event.
    * Broadcast is best-effort: a failure is logged but never raised, so
      event emission can never crash a scheduling loop.
    * Frame field names are camelCase to match ``GET /api/runs/{id}/events``.
    """
    from app.models import RunEvent, iso_utc

    try:
        row = storage.event_append(RunEvent(
            run_id=run_id,
            type=event_type,
            agent_id=agent_id,
            task_id=task_id,
            payload=payload or {},
        ))
    except Exception as exc:
        logger.warning("event_append_failed", run_id=run_id, error=str(exc))
        return None
    try:
        get_event_broadcaster().publish(run_id, {
            "id": row.id,
            "ts": iso_utc(row.ts),
            "type": row.type,
            "agentId": row.agent_id,
            "taskId": row.task_id,
            "payload": row.payload,
        })
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("event_publish_failed", run_id=run_id, error=str(exc))
    return row


# ── singleton ──────────────────────────────────────────────────────

_singleton: EventBroadcaster | None = None


def get_event_broadcaster() -> EventBroadcaster:
    """Return the process-wide :class:`EventBroadcaster`."""
    global _singleton
    if _singleton is None:
        _singleton = EventBroadcaster()
    return _singleton


def reset_event_broadcaster() -> None:
    """Drop the cached singleton (used by tests)."""
    global _singleton
    _singleton = None


__all__ = [
    "EventBroadcaster",
    "get_event_broadcaster",
    "publish_run_event",
    "reset_event_broadcaster",
]
