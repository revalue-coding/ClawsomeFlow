"""Tests for the run webhook (app.services.run_notify).

Covers: opt-in no-op default, terminal dedupe marker semantics, the manual
checkpoint (waiting-for-user) transition notifications, the storage
``run_update`` choke-point wiring, the sync POST helper, upgrade
compatibility of the config field, and the /api/system/notify-webhook
endpoints.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import paths
from app.config import load_config, save_config
from app.main import create_app
from app.models import Flow, FlowRun, RunStatus
from app.services import run_notify
from app.services.run_notify import (
    NOTIFIED_MARKER_KEY,
    post_webhook,
    prepare_checkpoint_notification,
    prepare_terminal_notification,
)
from app.storage import get_storage


def _set_webhook_url(url: str | None) -> None:
    save_config(load_config().model_copy(update={"notify_webhook_url": url}))


def _make_run(status: RunStatus = RunStatus.completed, **kwargs) -> FlowRun:
    return FlowRun(
        flow_id="flow_x", flow_version=1, team_name=kwargs.pop("team_name", "csflow-x"),
        status=status, inputs=kwargs.pop("inputs", {}), user="alice", **kwargs,
    )


# ── prepare_terminal_notification ────────────────────────────────────


def test_prepare_noop_when_unconfigured() -> None:
    run = _make_run(RunStatus.completed)
    assert prepare_terminal_notification(run) is None
    # No marker stamped when the feature is off — a later config change may
    # still notify this run if it gets persisted again (documented behavior).
    assert NOTIFIED_MARKER_KEY not in (run.inputs or {})


def test_prepare_noop_for_non_terminal_and_orphaned() -> None:
    _set_webhook_url("http://127.0.0.1:9/hook")
    for status in (RunStatus.running, RunStatus.awaiting_user_review,
                   RunStatus.awaiting_user_complaint, RunStatus.orphaned):
        assert prepare_terminal_notification(_make_run(status)) is None


def test_prepare_stamps_marker_and_builds_payload() -> None:
    _set_webhook_url("http://127.0.0.1:9/hook")
    run = _make_run(RunStatus.completed_with_conflicts, inputs={"goal": "x"})
    prepared = prepare_terminal_notification(run)
    assert prepared is not None
    assert prepared["url"] == "http://127.0.0.1:9/hook"
    payload = prepared["payload"]
    assert payload["event"] == "run_terminal"
    assert payload["runId"] == run.id
    assert payload["flowId"] == "flow_x"
    assert payload["status"] == "completed_with_conflicts"
    assert payload["isScheduled"] is False
    # Marker stamped into a NEW inputs dict; user inputs preserved.
    assert run.inputs["goal"] == "x"
    assert NOTIFIED_MARKER_KEY in run.inputs
    # Dedupe: a second call is a no-op.
    assert prepare_terminal_notification(run) is None


# ── prepare_checkpoint_notification ──────────────────────────────────


def test_prepare_checkpoint_noop_when_unconfigured() -> None:
    run = _make_run(RunStatus.awaiting_user_review)
    assert prepare_checkpoint_notification(run, old_status=RunStatus.running) is None


def test_prepare_checkpoint_fires_only_on_transition() -> None:
    _set_webhook_url("http://127.0.0.1:9/hook")
    for status in (RunStatus.awaiting_user_checkpoint,
                   RunStatus.awaiting_user_review,
                   RunStatus.awaiting_user_complaint):
        run = _make_run(status)
        prepared = prepare_checkpoint_notification(run, old_status=RunStatus.running)
        assert prepared is not None, status
        payload = prepared["payload"]
        assert payload["event"] == "run_checkpoint"
        assert payload["status"] == status.value
        assert payload["runId"] == run.id
        # Same-status re-persist (pending merge updates etc.) stays silent.
        assert prepare_checkpoint_notification(run, old_status=status) is None


def test_prepare_checkpoint_noop_for_non_waiting_states() -> None:
    _set_webhook_url("http://127.0.0.1:9/hook")
    for status in (RunStatus.running, RunStatus.complaint_processing,
                   RunStatus.completed, RunStatus.failed):
        run = _make_run(status)
        assert prepare_checkpoint_notification(
            run, old_status=RunStatus.running,
        ) is None, status


# ── storage run_update wiring ────────────────────────────────────────


def test_run_update_fires_once_and_persists_marker(monkeypatch) -> None:
    _set_webhook_url("http://127.0.0.1:9/hook")
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    storage = get_storage()
    flow = storage.flow_create(Flow(name="notify-flow", description="", owner_user="alice"))
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-notify",
        status=RunStatus.running, inputs={}, user="alice",
    ))

    run.status = RunStatus.running
    storage.run_update(run)
    assert sent == []

    run.status = RunStatus.completed
    updated = storage.run_update(run)
    assert len(sent) == 1
    assert sent[0]["payload"]["status"] == "completed"
    assert NOTIFIED_MARKER_KEY in updated.inputs

    # A later update of the already-notified terminal run does NOT re-fire.
    storage.run_update(updated)
    assert len(sent) == 1


def test_run_update_checkpoint_transitions(monkeypatch) -> None:
    """run_update fires run_checkpoint on entry into each waiting state,
    stays silent on same-state re-persists, and re-fires on re-entry."""
    _set_webhook_url("http://127.0.0.1:9/hook")
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    storage = get_storage()
    flow = storage.flow_create(Flow(name="cp-flow", description="", owner_user="alice"))
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-cp",
        status=RunStatus.running, inputs={}, user="alice",
    ))

    # running → awaiting_user_checkpoint: fires.
    run.status = RunStatus.awaiting_user_checkpoint
    storage.run_update(run)
    assert [p["payload"]["status"] for p in sent] == ["awaiting_user_checkpoint"]
    assert sent[0]["payload"]["event"] == "run_checkpoint"

    # Re-persist while still waiting: silent.
    storage.run_update(run)
    assert len(sent) == 1

    # Checkpoint cleared, then a review pause: fires again.
    run.status = RunStatus.running
    storage.run_update(run)
    run.status = RunStatus.awaiting_user_review
    storage.run_update(run)
    assert [p["payload"]["status"] for p in sent] == [
        "awaiting_user_checkpoint", "awaiting_user_review",
    ]

    # review → complaint window: a distinct pause, fires again.
    run.status = RunStatus.awaiting_user_complaint
    storage.run_update(run)
    assert len(sent) == 3

    # Terminal flip still uses the run_terminal event (not run_checkpoint).
    run.status = RunStatus.completed
    storage.run_update(run)
    assert len(sent) == 4
    assert sent[3]["payload"]["event"] == "run_terminal"


def test_run_update_noop_without_config(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    storage = get_storage()
    flow = storage.flow_create(Flow(name="quiet-flow", description="", owner_user="alice"))
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-quiet",
        status=RunStatus.running, inputs={}, user="alice",
    ))
    run.status = RunStatus.failed
    updated = storage.run_update(run)
    assert sent == []
    assert NOTIFIED_MARKER_KEY not in (updated.inputs or {})


# ── post_webhook (real loopback HTTP) ────────────────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    received: list[dict] = []
    respond_status = 204

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).received.append(json.loads(body or b"{}"))
        self.send_response(type(self).respond_status)
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def webhook_server():
    _Handler.received = []
    _Handler.respond_status = 204
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", _Handler
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_post_webhook_success(webhook_server) -> None:
    url, handler = webhook_server
    ok, detail = post_webhook(url, {"event": "run_terminal", "runId": "r1"})
    assert ok, detail
    assert handler.received == [{"event": "run_terminal", "runId": "r1"}]


def test_post_webhook_http_error(webhook_server) -> None:
    url, handler = webhook_server
    handler.respond_status = 500
    ok, detail = post_webhook(url, {"event": "x"})
    assert not ok
    assert "500" in detail


def test_post_webhook_connect_error() -> None:
    ok, detail = post_webhook("http://127.0.0.1:9/unreachable", {"e": 1}, timeout=2.0)
    assert not ok
    assert detail


def test_send_run_notification_enriches_flow_name(webhook_server) -> None:
    url, handler = webhook_server
    storage = get_storage()
    flow = storage.flow_create(Flow(name="Enriched Flow", description="", owner_user="alice"))
    run = _make_run(RunStatus.completed)
    run.flow_id = flow.id
    _set_webhook_url(url)
    prepared = prepare_terminal_notification(run)
    assert prepared is not None
    thread = run_notify.send_run_notification(prepared)
    thread.join(timeout=10)
    assert len(handler.received) == 1
    assert handler.received[0]["flowName"] == "Enriched Flow"


# ── upgrade compatibility ────────────────────────────────────────────


def test_legacy_config_without_field_loads_with_none() -> None:
    """An upgrade-only user's old config.json (no notify field) must load."""
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"deployment_mode": "local"}), encoding="utf-8")
    from app import config as cfg_mod

    cfg_mod.reset_config_cache()
    cfg = load_config()
    assert cfg.notify_webhook_url is None
    # And saving with the field unset keeps the file parseable by old code
    # (exclude_none drops it entirely).
    save_config(cfg)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "notify_webhook_url" not in data


# ── API endpoints ────────────────────────────────────────────────────


def test_api_get_put_and_clear_webhook() -> None:
    with TestClient(create_app()) as client:
        r = client.get("/api/system/notify-webhook")
        assert r.status_code == 200
        assert r.json()["url"] is None

        r = client.put(
            "/api/system/notify-webhook", json={"url": "https://example.com/hook"},
        )
        assert r.status_code == 200
        assert r.json()["url"] == "https://example.com/hook"
        assert load_config().notify_webhook_url == "https://example.com/hook"

        r = client.get("/api/system/notify-webhook")
        assert r.json()["url"] == "https://example.com/hook"

        r = client.put("/api/system/notify-webhook", json={"url": ""})
        assert r.status_code == 200
        assert r.json()["url"] is None
        assert load_config().notify_webhook_url is None


def test_api_put_rejects_non_http_url() -> None:
    with TestClient(create_app()) as client:
        r = client.put("/api/system/notify-webhook", json={"url": "ftp://x"})
    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_WEBHOOK_URL"


def test_api_test_endpoint_requires_config() -> None:
    with TestClient(create_app()) as client:
        r = client.post("/api/system/notify-webhook/test")
    assert r.status_code == 409
    assert r.json()["error"] == "WEBHOOK_NOT_CONFIGURED"


def test_api_test_endpoint_posts_sample(webhook_server) -> None:
    url, handler = webhook_server
    _set_webhook_url(url)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/notify-webhook/test")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert handler.received[0]["event"] == "run_terminal_test"
