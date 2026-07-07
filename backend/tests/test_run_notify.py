"""Tests for the per-Flow run webhook (app.services.run_notify).

Covers: per-Flow channel parsing/serialization, opt-in no-op default, terminal
dedupe marker semantics, the manual checkpoint (waiting-for-user) transition
notifications, the storage ``run_update`` choke-point wiring (channels read
from the Flow's spec.variables), multi-channel fan-out, the sync POST helper +
message formats, content enrichment, and the /api/flows/{id}/notify-webhooks
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
from app.models import Flow, FlowRun, RunEvent, RunStatus
from app.services import run_notify
from app.services.run_notify import (
    FLOW_NOTIFY_WEBHOOKS_VAR,
    NOTIFIED_MARKER_KEY,
    WEBHOOK_FORMATS,
    build_webhook_request,
    detect_webhook_format,
    enrich_run_content,
    flow_channels_for_run,
    parse_flow_channels,
    post_webhook,
    prepare_checkpoint_notification,
    prepare_terminal_notification,
    render_message_text,
    resolve_webhook_format,
    serialize_flow_channels,
)
from app.storage import get_storage

_HOOK = "http://127.0.0.1:9/hook"


def _ch(url: str = _HOOK, fmt: str | None = None) -> list[dict]:
    """One-channel list convenience for prepare_* unit tests."""
    return [{"url": url, "format": fmt}]


def _make_run(status: RunStatus = RunStatus.completed, **kwargs) -> FlowRun:
    return FlowRun(
        flow_id="flow_x", flow_version=1, team_name=kwargs.pop("team_name", "csflow-x"),
        status=status, inputs=kwargs.pop("inputs", {}), user="alice", **kwargs,
    )


def _make_flow(
    *,
    name: str = "notify-flow",
    owner: str | None = None,
    channels: list[dict] | None = None,
) -> Flow:
    """Create + persist a Flow, optionally with webhook channels in variables."""
    storage = get_storage()
    variables: dict[str, str] = {}
    if channels:
        variables[FLOW_NOTIFY_WEBHOOKS_VAR] = serialize_flow_channels(channels)
    flow = Flow(
        name=name,
        description="",
        owner_user=owner or load_config().default_user,
        spec={"agents": [], "tasks": [], "variables": variables},
    )
    return storage.flow_create(flow)


# ── channel parsing / serialization ──────────────────────────────────


def test_parse_flow_channels_roundtrip_and_dedupe() -> None:
    channels = [
        {"url": "https://a.example/hook", "format": "feishu"},
        {"url": "https://b.example/hook", "format": None},
        {"url": "https://a.example/hook", "format": "slack"},  # dup url dropped
    ]
    variables = {FLOW_NOTIFY_WEBHOOKS_VAR: serialize_flow_channels(channels)}
    parsed = parse_flow_channels(variables)
    assert parsed == [
        {"url": "https://a.example/hook", "format": "feishu"},
        {"url": "https://b.example/hook", "format": None},
    ]


def test_parse_flow_channels_handles_missing_and_malformed() -> None:
    assert parse_flow_channels(None) == []
    assert parse_flow_channels({}) == []
    assert parse_flow_channels({FLOW_NOTIFY_WEBHOOKS_VAR: "not json"}) == []
    assert parse_flow_channels({FLOW_NOTIFY_WEBHOOKS_VAR: '{"url":"x"}'}) == []  # not a list
    # Entries without a url are skipped; blank format normalizes to None.
    v = {FLOW_NOTIFY_WEBHOOKS_VAR: json.dumps([{"format": "feishu"}, {"url": "https://x/h", "format": ""}])}
    assert parse_flow_channels(v) == [{"url": "https://x/h", "format": None}]


def test_flow_channels_for_run_reads_current_flow() -> None:
    flow = _make_flow(channels=_ch("https://live.example/hook", "feishu"))
    run = _make_run(RunStatus.completed)
    run.flow_id = flow.id
    assert flow_channels_for_run(run) == [
        {"url": "https://live.example/hook", "format": "feishu"},
    ]
    # Unknown flow → empty (never raises).
    run.flow_id = "flow_missing"
    assert flow_channels_for_run(run) == []


# ── prepare_terminal_notification ────────────────────────────────────


def test_prepare_noop_without_channels() -> None:
    run = _make_run(RunStatus.completed)
    assert prepare_terminal_notification(run, channels=[]) is None
    # No marker stamped when there are no channels.
    assert NOTIFIED_MARKER_KEY not in (run.inputs or {})


def test_prepare_noop_for_non_terminal_and_orphaned() -> None:
    for status in (RunStatus.running, RunStatus.awaiting_user_review,
                   RunStatus.awaiting_user_complaint, RunStatus.orphaned):
        assert prepare_terminal_notification(_make_run(status), channels=_ch()) is None


def test_prepare_stamps_marker_and_builds_payload() -> None:
    run = _make_run(RunStatus.completed_with_conflicts, inputs={"goal": "x"})
    prepared = prepare_terminal_notification(run, channels=_ch())
    assert prepared is not None
    assert prepared["channels"] == _ch()
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
    assert prepare_terminal_notification(run, channels=_ch()) is None


def test_prepare_carries_all_channels() -> None:
    channels = [
        {"url": "https://a/h", "format": "feishu"},
        {"url": "https://b/h", "format": None},
    ]
    prepared = prepare_terminal_notification(_make_run(RunStatus.completed), channels=channels)
    assert prepared is not None
    assert prepared["channels"] == channels


# ── prepare_checkpoint_notification ──────────────────────────────────


def test_prepare_checkpoint_noop_without_channels() -> None:
    run = _make_run(RunStatus.awaiting_user_review)
    assert prepare_checkpoint_notification(run, old_status=RunStatus.running, channels=[]) is None


def test_prepare_checkpoint_fires_only_on_transition() -> None:
    for status in (RunStatus.awaiting_user_checkpoint,
                   RunStatus.awaiting_user_review,
                   RunStatus.awaiting_user_complaint):
        run = _make_run(status)
        prepared = prepare_checkpoint_notification(
            run, old_status=RunStatus.running, channels=_ch(),
        )
        assert prepared is not None, status
        payload = prepared["payload"]
        assert payload["event"] == "run_checkpoint"
        assert payload["status"] == status.value
        assert payload["runId"] == run.id
        # Same-status re-persist (pending merge updates etc.) stays silent.
        assert prepare_checkpoint_notification(run, old_status=status, channels=_ch()) is None


def test_prepare_checkpoint_noop_for_non_waiting_states() -> None:
    for status in (RunStatus.running, RunStatus.complaint_processing,
                   RunStatus.completed, RunStatus.failed):
        run = _make_run(status)
        assert prepare_checkpoint_notification(
            run, old_status=RunStatus.running, channels=_ch(),
        ) is None, status


# ── storage run_update wiring ────────────────────────────────────────


def test_run_update_fires_once_and_persists_marker(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    flow = _make_flow(channels=_ch())
    storage = get_storage()
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
    assert sent[0]["channels"] == _ch()
    assert NOTIFIED_MARKER_KEY in updated.inputs

    # A later update of the already-notified terminal run does NOT re-fire.
    storage.run_update(updated)
    assert len(sent) == 1


def test_scheduled_run_fires_terminal_webhook(monkeypatch) -> None:
    """Scheduled runs skip the review + complaint phases and go straight to
    ``completed`` — the terminal webhook must still fire (no is_scheduled gate),
    reading the Flow's channels, with ``isScheduled: true`` in the payload."""
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    flow = _make_flow(name="sched-flow", channels=_ch())
    storage = get_storage()
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-sched",
        status=RunStatus.running, inputs={}, user="alice", is_scheduled=True,
    ))
    run.status = RunStatus.completed
    storage.run_update(run)
    assert len(sent) == 1
    assert sent[0]["payload"]["event"] == "run_terminal"
    assert sent[0]["payload"]["status"] == "completed"
    assert sent[0]["payload"]["isScheduled"] is True


def test_scheduled_run_fires_checkpoint_webhook(monkeypatch) -> None:
    """A scheduled (unattended) run still PAUSES at a mid-DAG manual checkpoint,
    so the ``run_checkpoint`` webhook must fire per the Flow's channels."""
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    flow = _make_flow(name="sched-cp", channels=_ch())
    storage = get_storage()
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-schedcp",
        status=RunStatus.running, inputs={}, user="alice", is_scheduled=True,
    ))
    run.status = RunStatus.awaiting_user_checkpoint
    storage.run_update(run)
    assert len(sent) == 1
    assert sent[0]["payload"]["event"] == "run_checkpoint"
    assert sent[0]["payload"]["status"] == "awaiting_user_checkpoint"
    assert sent[0]["payload"]["isScheduled"] is True


def test_run_update_checkpoint_transitions(monkeypatch) -> None:
    """run_update fires run_checkpoint on entry into each waiting state,
    stays silent on same-state re-persists, and re-fires on re-entry."""
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    flow = _make_flow(name="cp-flow", channels=_ch())
    storage = get_storage()
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-cp",
        status=RunStatus.running, inputs={}, user="alice",
    ))

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


def test_run_update_never_broken_by_webhook_failure(monkeypatch) -> None:
    """Requirement: a webhook-path exception must NEVER abort the run update
    or raise into the caller (scheduler). The run row must still persist."""
    def _boom(_prepared):
        raise RuntimeError("webhook subsystem exploded")

    monkeypatch.setattr(run_notify, "send_run_notification", _boom)
    flow = _make_flow(name="robust-flow", channels=_ch())
    storage = get_storage()
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-robust",
        status=RunStatus.running, inputs={}, user="alice",
    ))
    run.status = RunStatus.completed
    # Must NOT raise despite the sender blowing up …
    updated = storage.run_update(run)
    # … and the status flip must be durably persisted.
    assert updated.status == RunStatus.completed
    assert storage.run_get(run.id).status == RunStatus.completed


def test_run_update_noop_without_channels(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(
        run_notify, "send_run_notification", lambda prepared: sent.append(prepared),
    )
    flow = _make_flow(name="quiet-flow", channels=None)
    storage = get_storage()
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
    raw: list[dict] = []
    respond_status = 204
    respond_body: dict | None = None

    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).raw.append({"body": body, "headers": dict(self.headers)})
        try:
            type(self).received.append(json.loads(body or b"{}"))
        except json.JSONDecodeError:
            # Plain-text platforms (ntfy) — keep a marker entry.
            type(self).received.append({"_raw": body.decode("utf-8", "replace")})
        self.send_response(type(self).respond_status)
        if type(self).respond_body is not None:
            resp = json.dumps(type(self).respond_body).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.end_headers()

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def webhook_server():
    _Handler.received = []
    _Handler.raw = []
    _Handler.respond_status = 204
    _Handler.respond_body = None
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
    flow = _make_flow(name="Enriched Flow", channels=_ch(url))
    run = _make_run(RunStatus.completed)
    run.flow_id = flow.id
    prepared = prepare_terminal_notification(run, channels=_ch(url))
    assert prepared is not None
    thread = run_notify.send_run_notification(prepared)
    thread.join(timeout=10)
    assert len(handler.received) == 1
    assert handler.received[0]["flowName"] == "Enriched Flow"


def test_send_run_notification_fans_out_to_all_channels(webhook_server) -> None:
    """Multiple channels → one enrichment, one POST per channel."""
    url, handler = webhook_server
    prepared = {
        "channels": [
            {"url": url, "format": "generic"},
            {"url": url, "format": "generic"},
        ],
        "payload": {"event": "run_terminal", "runId": "run_fan",
                    "flowId": "flow_fan", "status": "completed"},
    }
    run_notify.send_run_notification(prepared).join(timeout=10)
    assert len(handler.received) == 2
    assert all(m["runId"] == "run_fan" for m in handler.received)


# ── message formats: detection + rendering ───────────────────────────


def test_webhook_formats_registry() -> None:
    assert "generic" in WEBHOOK_FORMATS
    assert len(set(WEBHOOK_FORMATS)) == len(WEBHOOK_FORMATS)
    # "auto" is API-level sugar for null, never a stored format value.
    assert "auto" not in WEBHOOK_FORMATS


def test_detect_webhook_format_known_hosts() -> None:
    cases = {
        "https://open.feishu.cn/open-apis/bot/v2/hook/abc": "feishu",
        "https://open.larksuite.com/open-apis/bot/v2/hook/abc": "feishu",
        "https://oapi.dingtalk.com/robot/send?access_token=x": "dingtalk",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x": "wecom",
        "https://hooks.slack.com/services/T0/B0/xyz": "slack",
        "https://discord.com/api/webhooks/123/token": "discord",
        "https://discordapp.com/api/webhooks/123/token": "discord",
        "https://foo.webhook.office.com/webhookb2/xyz": "teams",
        "https://prod-01.westus.logic.azure.com/workflows/x/triggers/y": "teams",
        "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k": "googlechat",
        "https://api.telegram.org/bot123:token/sendMessage?chat_id=42": "telegram",
        "https://ntfy.sh/my-topic": "ntfy",
        "https://api.day.app/devicekey": "bark",
        "https://sctapi.ftqq.com/SCT123.send": "serverchan",
        # Unknown / self-hosted → generic (historical raw-JSON behavior).
        "https://example.com/webhook": "generic",
        "http://127.0.0.1:8787/csflow-webhook": "generic",
        "https://my-mattermost.corp/hooks/abc": "generic",
    }
    for url, expected in cases.items():
        assert detect_webhook_format(url) == expected, url


def test_resolve_webhook_format_explicit_overrides_detection() -> None:
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
    assert resolve_webhook_format(url, None) == "feishu"
    assert resolve_webhook_format(url, "auto") == "feishu"
    assert resolve_webhook_format(url, "generic") == "generic"
    # Self-hosted Mattermost picks slack explicitly.
    assert resolve_webhook_format("https://mm.corp/hooks/x", "slack") == "slack"
    # Bogus configured value falls back to detection.
    assert resolve_webhook_format(url, "nope") == "feishu"


def _sample_payload(**overrides) -> dict:
    payload = {
        "event": "run_terminal",
        "runId": "run_1",
        "flowId": "flow_1",
        "flowName": "My Flow",
        "teamName": "csflow-1",
        "status": "completed",
        "isScheduled": False,
        "startedAt": "2026-07-07T10:00:00Z",
        "finishedAt": "2026-07-07T10:20:00Z",
    }
    payload.update(overrides)
    return payload


def test_render_message_text_terminal_and_checkpoint() -> None:
    text = render_message_text(_sample_payload())
    assert "ClawsomeFlow run finished" in text
    assert "Flow: My Flow" in text
    assert "Run: run_1" in text
    assert "Status: completed" in text
    cp = render_message_text(_sample_payload(
        event="run_checkpoint", status="awaiting_user_review", finishedAt=None,
    ))
    assert "action required" in cp
    assert "Finished:" not in cp


def test_build_webhook_request_shapes() -> None:
    payload = _sample_payload()
    text = render_message_text(payload)
    url = "https://example.com/hook"

    req = build_webhook_request(url, payload, "generic")
    assert req["json"] is payload  # raw passthrough, historical contract

    assert build_webhook_request(url, payload, "feishu")["json"] == {
        "msg_type": "text", "content": {"text": text},
    }
    for fmt in ("dingtalk", "wecom"):
        assert build_webhook_request(url, payload, fmt)["json"] == {
            "msgtype": "text", "text": {"content": text},
        }
    for fmt in ("slack", "googlechat"):
        assert build_webhook_request(url, payload, fmt)["json"] == {"text": text}

    teams = build_webhook_request(url, payload, "teams")["json"]
    assert teams["type"] == "message"
    card = teams["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["body"][0]["text"] == text

    bark = build_webhook_request(url, payload, "bark")["json"]
    assert bark["body"] == text and bark["title"].startswith("ClawsomeFlow")
    sc = build_webhook_request(url, payload, "serverchan")["json"]
    assert sc["desp"] == text
    gotify = build_webhook_request(url, payload, "gotify")["json"]
    assert gotify["message"] == text and gotify["priority"] == 5


def test_build_webhook_request_discord_truncates() -> None:
    payload = _sample_payload(flowName="x" * 5000)
    body = build_webhook_request("https://d/api/webhooks/1/t", payload, "discord")
    assert len(body["json"]["content"]) <= 1900


def test_build_webhook_request_telegram_chat_id_from_query() -> None:
    payload = _sample_payload()
    url = "https://api.telegram.org/bot123:tok/sendMessage?chat_id=-100987"
    body = build_webhook_request(url, payload, "telegram")["json"]
    assert body["chat_id"] == "-100987"
    assert "Run: run_1" in body["text"]
    # No chat_id in URL → text-only body (Telegram will report the error).
    no_id = build_webhook_request(
        "https://api.telegram.org/bot123:tok/sendMessage", payload, "telegram",
    )["json"]
    assert "chat_id" not in no_id


def test_build_webhook_request_ntfy_plain_text() -> None:
    payload = _sample_payload()
    req = build_webhook_request("https://ntfy.sh/topic", payload, "ntfy")
    assert "json" not in req
    assert req["content"].decode("utf-8") == render_message_text(payload)
    title = req["headers"]["Title"]
    title.encode("latin-1")  # header value must stay latin-1 safe
    assert "completed" in title


def test_post_webhook_formats_feishu_body(webhook_server) -> None:
    url, handler = webhook_server
    ok, detail = post_webhook(url, _sample_payload(), fmt="feishu")
    assert ok, detail
    body = handler.received[0]
    assert body["msg_type"] == "text"
    assert "Run: run_1" in body["content"]["text"]


def test_post_webhook_ntfy_sends_raw_text(webhook_server) -> None:
    url, handler = webhook_server
    ok, detail = post_webhook(url, _sample_payload(), fmt="ntfy")
    assert ok, detail
    raw = handler.raw[0]
    assert b"Run: run_1" in raw["body"]
    assert raw["headers"].get("Title", "").startswith("ClawsomeFlow")


def test_post_webhook_detects_feishu_error_in_http_200(webhook_server) -> None:
    """Feishu reports bad hooks inside an HTTP-200 JSON body — must fail."""
    url, handler = webhook_server
    handler.respond_status = 200
    handler.respond_body = {"code": 19001, "msg": "sign match fail"}
    ok, detail = post_webhook(url, _sample_payload(), fmt="feishu")
    assert not ok
    assert "19001" in detail
    # Same 200 body under generic format is still a success (custom receiver).
    ok2, _ = post_webhook(url, _sample_payload(), fmt="generic")
    assert ok2


# ── content enrichment: leader report / checkpoint output ────────────


def _seed_events(run_id: str, events: list[tuple[str, dict]]) -> None:
    storage = get_storage()
    for etype, payload in events:
        storage.event_append(RunEvent(run_id=run_id, type=etype, payload=payload))


def _run_with_events(status: RunStatus, events: list[tuple[str, dict]]) -> FlowRun:
    storage = get_storage()
    flow = storage.flow_create(Flow(name="c-flow", description="", owner_user="alice"))
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name=f"csflow-{status.value[:6]}",
        status=status, inputs={}, user="alice",
    ))
    _seed_events(run.id, events)
    return run


def test_enrich_terminal_extracts_leader_final_reply() -> None:
    run = _run_with_events(RunStatus.completed, [
        ("task_dispatched", {"foo": 1}),
        ("run_terminal_execution_log", {"worker_report_history": [
            {"from_agent": "bob", "summary": "task t1 done: built the widget"},
            {"from_agent": "leader", "summary": "LEADER FINAL REPLY: All 3 tasks done; report attached."},
        ]}),
    ])
    payload = {"event": "run_terminal", "runId": run.id,
               "status": "completed", "flowId": run.flow_id}
    enrich_run_content(payload)
    assert payload["content"] == "All 3 tasks done; report attached."
    # And it shows up in the rendered chat text under a Leader report header.
    text = render_message_text(payload)
    assert "Leader report" in text
    assert "All 3 tasks done" in text


def test_enrich_terminal_falls_back_to_last_report() -> None:
    run = _run_with_events(RunStatus.completed, [
        ("run_terminal_execution_log", {"worker_report_history": [
            {"from_agent": "bob", "summary": "task t1 done: first"},
            {"from_agent": "carol", "summary": "task t2 done: last one"},
        ]}),
    ])
    payload = {"event": "run_terminal", "runId": run.id, "status": "completed"}
    enrich_run_content(payload)
    assert payload["content"] == "task t2 done: last one"


def test_enrich_checkpoint_extracts_pending_output() -> None:
    run = _run_with_events(RunStatus.awaiting_user_checkpoint, [
        ("task_checkpoint_waiting", {"items": [
            {"task_id": "t1", "subject": "Research", "summary": "Found 3 options."},
            {"task_id": "t2", "subject": "Draft", "summary": "Draft ready for review."},
        ]}),
    ])
    payload = {"event": "run_checkpoint", "runId": run.id,
               "status": "awaiting_user_checkpoint"}
    enrich_run_content(payload)
    assert "Research: Found 3 options." in payload["content"]
    assert "Draft: Draft ready for review." in payload["content"]
    text = render_message_text(payload)
    assert "Checkpoint output" in text


def test_enrich_review_checkpoint_uses_leader_report() -> None:
    run = _run_with_events(RunStatus.awaiting_user_review, [
        ("run_terminal_execution_log", {"worker_report_history": [
            {"from_agent": "leader", "summary": "leader final reply: ready to merge"},
        ]}),
    ])
    payload = {"event": "run_checkpoint", "runId": run.id,
               "status": "awaiting_user_review"}
    enrich_run_content(payload)
    assert payload["content"] == "ready to merge"


def test_enrich_never_raises_and_skips_test_event() -> None:
    # Missing runId → no-op, no exception.
    p1: dict = {"event": "run_terminal"}
    enrich_run_content(p1)
    assert "content" not in p1
    # Test event is skipped even with a runId.
    p2 = {"event": "run_terminal_test", "runId": "run_test"}
    enrich_run_content(p2)
    assert "content" not in p2
    # Unknown run id (no events) → no content, no raise.
    p3 = {"event": "run_terminal", "runId": "run_does_not_exist", "status": "completed"}
    enrich_run_content(p3)
    assert "content" not in p3


def test_enrich_truncates_huge_content() -> None:
    run = _run_with_events(RunStatus.completed, [
        ("run_terminal_execution_log", {"worker_report_history": [
            {"from_agent": "leader", "summary": "leader final reply: " + "x" * 9000},
        ]}),
    ])
    payload = {"event": "run_terminal", "runId": run.id, "status": "completed"}
    enrich_run_content(payload)
    assert len(payload["content"]) <= 3000


def test_send_run_notification_includes_content(webhook_server) -> None:
    """End-to-end: the daemon-thread sender enriches + delivers the report."""
    url, handler = webhook_server
    run = _run_with_events(RunStatus.completed, [
        ("run_terminal_execution_log", {"worker_report_history": [
            {"from_agent": "leader", "summary": "leader final reply: done and dusted"},
        ]}),
    ])
    prepared = {
        "channels": [{"url": url, "format": "feishu"}],
        "payload": {"event": "run_terminal", "runId": run.id,
                    "flowId": run.flow_id, "status": "completed"},
    }
    run_notify.send_run_notification(prepared).join(timeout=10)
    assert "done and dusted" in handler.received[0]["content"]["text"]


# ── upgrade compatibility ────────────────────────────────────────────


def test_legacy_config_without_field_loads_with_none() -> None:
    """An upgrade-only user's old config.json (no notify field) must load; the
    now-deprecated global fields default to None and are dropped on save."""
    path = paths.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"deployment_mode": "local"}), encoding="utf-8")
    from app import config as cfg_mod

    cfg_mod.reset_config_cache()
    cfg = load_config()
    assert cfg.notify_webhook_url is None
    assert cfg.notify_webhook_format is None
    save_config(cfg)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "notify_webhook_url" not in data
    assert "notify_webhook_format" not in data


# ── API endpoints (per-Flow) ─────────────────────────────────────────


def test_api_get_put_and_clear_flow_webhooks() -> None:
    flow = _make_flow(name="api-flow")
    with TestClient(create_app()) as client:
        r = client.get(f"/api/flows/{flow.id}/notify-webhooks")
        assert r.status_code == 200, r.text
        assert r.json()["channels"] == []

        r = client.put(
            f"/api/flows/{flow.id}/notify-webhooks",
            json={"channels": [
                {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc"},
                {"url": "https://example.com/hook", "format": "generic"},
            ]},
        )
        assert r.status_code == 200, r.text
        chans = r.json()["channels"]
        assert [c["url"] for c in chans] == [
            "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            "https://example.com/hook",
        ]
        assert chans[0]["effectiveFormat"] == "feishu"  # auto-detected
        assert chans[1]["format"] == "generic"

        # Persisted + surfaced on the summary count.
        r = client.get(f"/api/flows/{flow.id}/notify-webhooks")
        assert len(r.json()["channels"]) == 2
        items = {f["id"]: f for f in client.get("/api/flows").json()["items"]}
        assert items[flow.id]["notifyChannelCount"] == 2

        # Clearing (empty list) disables notifications.
        r = client.put(f"/api/flows/{flow.id}/notify-webhooks", json={"channels": []})
        assert r.status_code == 200
        assert r.json()["channels"] == []
        items = {f["id"]: f for f in client.get("/api/flows").json()["items"]}
        assert items[flow.id]["notifyChannelCount"] == 0


def test_api_put_dedupes_urls() -> None:
    flow = _make_flow(name="dedupe-flow")
    with TestClient(create_app()) as client:
        r = client.put(
            f"/api/flows/{flow.id}/notify-webhooks",
            json={"channels": [
                {"url": "https://example.com/hook", "format": "generic"},
                {"url": "https://example.com/hook", "format": "slack"},
            ]},
        )
        assert r.status_code == 200
        assert len(r.json()["channels"]) == 1


def test_api_put_rejects_unknown_format() -> None:
    flow = _make_flow(name="badfmt-flow")
    with TestClient(create_app()) as client:
        r = client.put(
            f"/api/flows/{flow.id}/notify-webhooks",
            json={"channels": [{"url": "https://example.com/hook", "format": "carrier-pigeon"}]},
        )
    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_WEBHOOK_FORMAT"


def test_api_put_rejects_non_http_url() -> None:
    flow = _make_flow(name="badurl-flow")
    with TestClient(create_app()) as client:
        r = client.put(
            f"/api/flows/{flow.id}/notify-webhooks",
            json={"channels": [{"url": "ftp://x"}]},
        )
    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_WEBHOOK_URL"


def test_api_test_endpoint_requires_config() -> None:
    flow = _make_flow(name="notest-flow")
    with TestClient(create_app()) as client:
        r = client.post(f"/api/flows/{flow.id}/notify-webhooks/test")
    assert r.status_code == 409
    assert r.json()["error"] == "WEBHOOK_NOT_CONFIGURED"


def test_api_test_endpoint_posts_to_adhoc_channel(webhook_server) -> None:
    url, handler = webhook_server
    flow = _make_flow(name="adhoc-test-flow")
    with TestClient(create_app()) as client:
        r = client.post(
            f"/api/flows/{flow.id}/notify-webhooks/test",
            json={"url": url, "format": "feishu"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    body = handler.received[0]
    assert body["msg_type"] == "text"
    assert "webhook test" in body["content"]["text"]


def test_api_test_endpoint_posts_to_all_saved_channels(webhook_server) -> None:
    url, handler = webhook_server
    # Distinct URLs (same loopback server) so channel de-dup keeps both.
    flow = _make_flow(name="saved-test-flow", channels=[
        {"url": url, "format": "generic"},
        {"url": f"{url}-b", "format": "generic"},
    ])
    with TestClient(create_app()) as client:
        r = client.post(f"/api/flows/{flow.id}/notify-webhooks/test")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert len(handler.received) == 2
    assert handler.received[0]["event"] == "run_terminal_test"
