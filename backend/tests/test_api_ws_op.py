"""Tests for WebSocket /ws/op/{op_id} — snapshot-on-connect + live transitions."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import operations as ops
from app.config import load_config, save_config
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    cfg = load_config().model_copy(update={"default_user": "alice"})
    save_config(cfg)
    ops.reset_op_registry()
    with TestClient(create_app()) as c:
        yield c
    ops.reset_op_registry()


def test_snapshot_on_connect(client) -> None:
    # An op already running before the client connects → first frame is the snapshot.
    ops.get_op_registry().start(op_id="hermes_create:math", user="alice", kind="hermes_create")
    with client.websocket_connect("/ws/op/hermes_create:math") as ws:
        frame = ws.receive_json()
        assert frame["type"] == "op_status"
        assert frame["state"] == "running"
        assert frame["opId"] == "hermes_create:math"


def test_live_transition(client) -> None:
    reg = ops.get_op_registry()
    reg.start(op_id="hermes_create:math", user="alice", kind="hermes_create")
    with client.websocket_connect("/ws/op/hermes_create:math") as ws:
        assert ws.receive_json()["state"] == "running"  # snapshot
        reg.succeed("hermes_create:math", result={"agentId": "math"})
        frame = ws.receive_json()
        assert frame["state"] == "succeeded"
        assert frame["result"] == {"agentId": "math"}


def test_no_snapshot_when_op_absent(client) -> None:
    # Connecting before start() → no snapshot, but a later transition still arrives.
    reg = ops.get_op_registry()
    with client.websocket_connect("/ws/op/hermes_create:later") as ws:
        reg.start(op_id="hermes_create:later", user="alice", kind="hermes_create")
        frame = ws.receive_json()
        assert frame["state"] == "running"


def test_ping_pong(client) -> None:
    with client.websocket_connect("/ws/op/hermes_create:math") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_unauthenticated_closed(client, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force auth failure → endpoint closes with 4401 before accepting.
    from app.api import ws as ws_mod
    from app.api.errors import ApiError

    def _boom(_conn=None):
        raise ApiError("UNAUTHENTICATED", "nope", status_code=401)

    monkeypatch.setattr(ws_mod, "resolve_current_user", _boom)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/op/hermes_create:math"):
            pass
