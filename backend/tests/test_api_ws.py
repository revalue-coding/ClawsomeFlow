"""Tests for WebSocket /ws/{run_id}."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.events import get_event_broadcaster
from app.main import create_app
from app.models import Flow, FlowAgent, FlowRun, FlowSpec, FlowTask, MergeStrategy, OnFailure, RunEvent, RunStatus, AgentKind
from app.storage import get_storage


@pytest.fixture
def client_with_run(tmp_path: Path):
    cfg = load_config()
    cfg = cfg.model_copy(update={"default_user": "alice"})
    save_config(cfg)
    storage = get_storage()
    spec = FlowSpec(
        agents=[FlowAgent(
            id="leader", kind=AgentKind.claude, repo="/r",
            is_leader=True, merge_strategy=MergeStrategy.manual,
            on_failure=OnFailure.retry, max_retries=2,
        )],
        tasks=[FlowTask(
            id="t1", owner_agent_id="leader", subject="x",
            description="", depends_on=[], is_leader_summary=True,
        )],
    )
    flow = storage.flow_create(
        Flow(name="t", description="", owner_user="alice").with_spec(spec)
    )
    run = storage.run_create(FlowRun(
        id="run-ws-1", flow_id=flow.id, flow_version=1,
        team_name="csflow-ws", status=RunStatus.running,
        inputs={}, user="alice",
    ))
    with TestClient(create_app()) as c:
        yield c, run


def test_ws_unknown_run_closed(client_with_run) -> None:
    client, _ = client_with_run
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/missing"):
            pass


def test_ws_other_user_closed(
    client_with_run, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, run = client_with_run
    # Switch CURRENT user to bob via env override.
    monkeypatch.setenv("CSFLOW_USER", "bob")
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/{run.id}"):
            pass


def test_ws_receives_published_events(client_with_run) -> None:
    client, run = client_with_run
    bus = get_event_broadcaster()
    with client.websocket_connect(f"/ws/{run.id}") as ws:
        # Publish from server side (would normally come from controller).
        bus.publish(run.id, {
            "id": 1, "ts": "2026-05-07T00:00:00+00:00",
            "type": "task_dispatched", "agent_id": "leader",
            "task_id": "t1", "payload": {"x": 1},
        })
        msg = ws.receive_json()
        assert msg["type"] == "task_dispatched"
        assert msg["agent_id"] == "leader"


def test_ws_ping_pong(client_with_run) -> None:
    client, run = client_with_run
    with client.websocket_connect(f"/ws/{run.id}") as ws:
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg == {"type": "pong"}


def test_ws_backfills_missed_events_with_since_id(client_with_run) -> None:
    client, run = client_with_run
    storage = get_storage()
    # Pre-populate events.
    for i in range(3):
        storage.event_append(RunEvent(run_id=run.id, type=f"e{i}"))
    # Connect with since_id=0 to fetch them all.
    with client.websocket_connect(f"/ws/{run.id}?sinceId=0") as ws:
        rows = []
        for _ in range(3):
            rows.append(ws.receive_json())
    assert {r["type"] for r in rows} == {"e0", "e1", "e2"}
