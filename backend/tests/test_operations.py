"""Unit tests for the in-memory operation-status registry (app.operations)."""

from __future__ import annotations

import pytest

from app import operations as ops


@pytest.fixture(autouse=True)
def _fresh_registry():
    ops.reset_op_registry()
    yield
    ops.reset_op_registry()


def test_start_then_get_running() -> None:
    reg = ops.get_op_registry()
    reg.start(op_id="hermes_create:math", user="alice", kind="hermes_create")
    op = reg.get("hermes_create:math", user="alice")
    assert op is not None
    assert op.state == "running"
    assert op.kind == "hermes_create"


def test_get_wrong_user_returns_none() -> None:
    reg = ops.get_op_registry()
    reg.start(op_id="hermes_create:math", user="alice", kind="hermes_create")
    assert reg.get("hermes_create:math", user="bob") is None


def test_succeed_and_fail_transitions() -> None:
    reg = ops.get_op_registry()
    reg.start(op_id="o:1", user="alice", kind="hermes_create")
    reg.succeed("o:1", result={"agentId": "x"}, detail="done")
    op = reg.get("o:1", user="alice")
    assert op.state == "succeeded"
    assert op.result == {"agentId": "x"}
    assert op.detail == "done"

    reg.start(op_id="o:2", user="alice", kind="hermes_create")
    reg.fail("o:2", detail="boom")
    op2 = reg.get("o:2", user="alice")
    assert op2.state == "failed"
    assert op2.detail == "boom"


def test_terminate_unknown_op_synthesizes_entry() -> None:
    # A terminal recorded for an op we never start()'d (e.g. process restart).
    reg = ops.get_op_registry()
    op = reg.fail("ghost:1", detail="late")
    assert op.state == "failed"
    # user is empty on a synthesized entry → get() with any user still returns it
    # (the empty-owner short-circuit in get()).
    assert reg.get("ghost:1", user="anyone") is not None


def test_eviction_caps_size() -> None:
    reg = ops._OpRegistry(max_ops=3)
    for i in range(5):
        reg.start(op_id=f"o:{i}", user="alice", kind="k")
    # Oldest two evicted, newest three retained.
    assert reg.get("o:0", user="alice") is None
    assert reg.get("o:1", user="alice") is None
    assert reg.get("o:4", user="alice") is not None


def test_op_channel_prefix() -> None:
    assert ops.op_channel("hermes_create:math") == "op:hermes_create:math"


def test_start_publishes_to_bus() -> None:
    from app.events import get_event_broadcaster, reset_event_broadcaster

    reset_event_broadcaster()
    reg = ops.get_op_registry()
    bus = get_event_broadcaster()
    # No subscriber → 0 delivered, but publish must not raise.
    assert bus.subscriber_count(ops.op_channel("o:1")) == 0
    reg.start(op_id="o:1", user="alice", kind="k")
    reset_event_broadcaster()
