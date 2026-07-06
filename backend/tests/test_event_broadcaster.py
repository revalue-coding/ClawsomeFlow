"""Tests for app.events — in-process Run-event broadcaster."""

from __future__ import annotations

import asyncio

import pytest

from app.events import EventBroadcaster, get_event_broadcaster


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = EventBroadcaster()
    delivered = bus.publish("run-x", {"type": "noop"})
    assert delivered == 0


@pytest.mark.asyncio
async def test_subscribe_receives_events_in_order() -> None:
    bus = EventBroadcaster()
    async with bus.subscribe("run-1") as q:
        bus.publish("run-1", {"id": 1, "type": "a"})
        bus.publish("run-1", {"id": 2, "type": "b"})
        first = await asyncio.wait_for(q.get(), timeout=0.5)
        second = await asyncio.wait_for(q.get(), timeout=0.5)
    assert first["id"] == 1 and second["id"] == 2


@pytest.mark.asyncio
async def test_subscribe_isolates_runs() -> None:
    bus = EventBroadcaster()
    async with bus.subscribe("run-A") as a, bus.subscribe("run-B") as b:
        bus.publish("run-A", {"id": 1})
        # B's queue must stay empty.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(b.get(), timeout=0.05)
        ev = await asyncio.wait_for(a.get(), timeout=0.5)
        assert ev["id"] == 1


@pytest.mark.asyncio
async def test_multiple_subscribers_each_get_all_events() -> None:
    bus = EventBroadcaster()
    async with bus.subscribe("r") as q1, bus.subscribe("r") as q2:
        bus.publish("r", {"id": 1})
        e1 = await asyncio.wait_for(q1.get(), timeout=0.5)
        e2 = await asyncio.wait_for(q2.get(), timeout=0.5)
    assert e1 == e2 == {"id": 1}


@pytest.mark.asyncio
async def test_unsubscribe_removes_subscriber() -> None:
    bus = EventBroadcaster()
    async with bus.subscribe("r"):
        assert bus.subscriber_count("r") == 1
    assert bus.subscriber_count("r") == 0


@pytest.mark.asyncio
async def test_full_queue_drops_oldest_and_tags_drop() -> None:
    """When a subscriber is too slow, oldest event is dropped + flag set."""
    bus = EventBroadcaster(queue_size=2)
    async with bus.subscribe("r") as q:
        bus.publish("r", {"id": 1})
        bus.publish("r", {"id": 2})
        # Queue full. Publishing again should drop id=1 and tag the next as dropped.
        bus.publish("r", {"id": 3})
        a = await asyncio.wait_for(q.get(), timeout=0.5)
        b = await asyncio.wait_for(q.get(), timeout=0.5)
    # Exactly which item carries `dropped: True` is implementation detail
    # (we drop oldest *then* push the modified payload), but the queue
    # should never grow past 2 and one of them must signal drop.
    assert {a["id"], b["id"]} == {2, 3}
    assert any(e.get("dropped") for e in (a, b))


def test_get_event_broadcaster_singleton() -> None:
    a = get_event_broadcaster()
    b = get_event_broadcaster()
    assert a is b


# ──────────────────────────────────────────────────────────────────────
# publish_run_event — canonical persist + WS fanout helper
# ──────────────────────────────────────────────────────────────────────


class _FakeStorage:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.appended: list = []

    def event_append(self, event):
        if self.fail:
            raise RuntimeError("db down")
        event.id = len(self.appended) + 1
        self.appended.append(event)
        return event


@pytest.mark.asyncio
async def test_publish_run_event_persists_and_broadcasts() -> None:
    from app.events import publish_run_event

    storage = _FakeStorage()
    bus = get_event_broadcaster()
    async with bus.subscribe("run-e2e") as q:
        row = publish_run_event(
            storage,
            run_id="run-e2e",
            event_type="task_dispatched",
            agent_id="dev-a",
            task_id="t1",
            payload={"k": "v"},
        )
        frame = await asyncio.wait_for(q.get(), timeout=0.5)
    assert row is not None and storage.appended == [row]
    # camelCase frame matching GET /api/runs/{id}/events serialisation.
    assert frame["type"] == "task_dispatched"
    assert frame["agentId"] == "dev-a"
    assert frame["taskId"] == "t1"
    assert frame["payload"] == {"k": "v"}
    assert frame["id"] == row.id


@pytest.mark.asyncio
async def test_publish_run_event_persist_failure_is_swallowed() -> None:
    """A DB failure must not raise into the scheduler loop, and nothing may
    be broadcast for an unpersisted event."""
    from app.events import publish_run_event

    storage = _FakeStorage(fail=True)
    bus = get_event_broadcaster()
    async with bus.subscribe("run-fail") as q:
        row = publish_run_event(storage, run_id="run-fail", event_type="x")
        assert row is None
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)
