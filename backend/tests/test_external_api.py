"""/api/external surface + the WebUI human completion endpoint + guard rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.main import create_app
from app.models import (
    AgentKind,
    ExternalChannel,
    ExternalNodeConfig,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    RunEvent,
    RunStatus,
)
from app.scheduler import engine as engine_mod
from app.scheduler.run_metadata import EXTERNAL_CALLBACK_KEY, UNATTENDED_KEY
from app.services.external_tasks import (
    EXTERNAL_TASK_DISPATCHED_EVENT,
    mint_ticket,
)
from app.storage import get_storage


@pytest.fixture
def app_client(tmp_path: Path):
    cfg = load_config()
    save_config(cfg.model_copy(update={"default_user": "alice"}))
    # /api/external enforces a loopback-Host rule even when the api_token
    # guard is inactive, so tests must present a loopback Host (the default
    # "testserver" base_url would be rejected with HOST_NOT_ALLOWED).
    with TestClient(create_app(), base_url="http://127.0.0.1:17017") as c:
        yield c


class _FakeMcp:
    def __init__(self) -> None:
        self.mailbox_calls: list[dict[str, Any]] = []
        self.task_updates: list[dict[str, Any]] = []

    async def mailbox_send(self, **kw: Any) -> None:
        self.mailbox_calls.append(kw)

    async def task_update(self, **kw: Any) -> dict[str, Any]:
        self.task_updates.append(kw)
        return {}


def _fake_mcp(monkeypatch: pytest.MonkeyPatch) -> _FakeMcp:
    from app.integrations import clawteam_mcp as mcp_mod

    fake = _FakeMcp()

    async def fake_get(**kw: Any) -> _FakeMcp:
        return fake

    monkeypatch.setattr(mcp_mod, "get_mcp_client", fake_get)
    return fake


def _mk_flow(owner: str = "alice") -> Flow:
    storage = get_storage()
    spec = FlowSpec(agents=[
        FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r", is_leader=True),
        FlowAgent(id="ext-node", kind=AgentKind.external,
                  external=ExternalNodeConfig(channel=ExternalChannel.human)),
    ], tasks=[
        FlowTask(id="t1", owner_agent_id="ext-node", subject="s"),
        FlowTask(id="ts", owner_agent_id="leader", subject="sum",
                 depends_on=["t1"], is_leader_summary=True),
    ])
    return storage.flow_create(
        Flow(name="f", owner_user=owner).with_spec(spec),
    )


def _mk_run_with_dispatch(
    *, nonce: str = "n-1", status: RunStatus = RunStatus.running,
) -> FlowRun:
    storage = get_storage()
    flow = _mk_flow()
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name=f"csflow-{nonce}",
        status=status, inputs={}, user="alice",
    ))
    storage.event_append(RunEvent(
        run_id=run.id, type=EXTERNAL_TASK_DISPATCHED_EVENT,
        agent_id="ext-node", task_id="t1",
        payload={
            "channel": "human", "nonce": nonce,
            "clawteamTaskId": "CT-77", "leaderAgentId": "leader",
            "subject": "s",
        },
    ))
    return run


# ── ticket completion endpoint ──────────────────────────────────────────


def test_complete_requires_ticket(app_client: TestClient) -> None:
    run = _mk_run_with_dispatch()
    r = app_client.post(
        f"/api/external/tasks/{run.id}/t1/complete",
        json={"status": "success", "summary": "done"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "EXTERNAL_TICKET_MISSING"


def test_complete_rejects_bad_ticket(app_client: TestClient) -> None:
    run = _mk_run_with_dispatch()
    r = app_client.post(
        f"/api/external/tasks/{run.id}/t1/complete",
        json={"status": "success", "summary": "done", "token": "nope.bad"},
        headers={},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "EXTERNAL_TICKET_INVALID"


def test_complete_success_roundtrip(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch()
    ticket = mint_ticket(run.id, "t1", "n-1")
    r = app_client.post(
        f"/api/external/tasks/{run.id}/t1/complete",
        json={"status": "success", "summary": "external work done"},
        headers={"Authorization": f"Bearer {ticket}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "recorded"
    assert fake.task_updates[0]["task_id"] == "CT-77"
    assert fake.mailbox_calls[0]["content"] == "task t1 done: external work done"
    # Idempotent: replaying the same ticket is a 200 no-op.
    r2 = app_client.post(
        f"/api/external/tasks/{run.id}/t1/complete",
        json={"status": "success", "summary": "dup", "token": ticket},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "already_recorded"
    assert len(fake.task_updates) == 1


def test_complete_stale_ticket_conflict(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch(nonce="n-latest")
    stale = mint_ticket(run.id, "t1", "n-old")  # valid signature, old attempt
    r = app_client.post(
        f"/api/external/tasks/{run.id}/t1/complete",
        json={"status": "success", "summary": "late", "token": stale},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "EXTERNAL_TICKET_STALE"


def test_complete_rejected_on_terminal_run(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch(nonce="n-t", status=RunStatus.completed)
    ticket = mint_ticket(run.id, "t1", "n-t")
    r = app_client.post(
        f"/api/external/tasks/{run.id}/t1/complete",
        json={"status": "success", "summary": "late", "token": ticket},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "EXTERNAL_RUN_NOT_ACTIVE"


# ── WebUI human path (main /api guard, no ticket) ───────────────────────


def test_webui_complete_no_ticket_needed(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch(nonce="n-web")
    r = app_client.post(
        f"/api/runs/{run.id}/external-tasks/t1/complete",
        json={"status": "success", "summary": "human did it"},
    )
    assert r.status_code == 200, r.text
    assert fake.mailbox_calls[0]["content"] == "task t1 done: human did it"


def test_webui_complete_failed_reports_failure(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch(nonce="n-web2")
    r = app_client.post(
        f"/api/runs/{run.id}/external-tasks/t1/complete",
        json={"status": "failed", "summary": "cannot access lab"},
    )
    assert r.status_code == 200, r.text
    assert fake.mailbox_calls[0]["content"] == "FAILED: t1: cannot access lab"
    assert fake.task_updates == []


def test_webui_complete_requires_outstanding_dispatch(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_mcp(monkeypatch)
    storage = get_storage()
    flow = _mk_flow()
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-nodispatch",
        status=RunStatus.running, inputs={}, user="alice",
    ))
    r = app_client.post(
        f"/api/runs/{run.id}/external-tasks/t1/complete",
        json={"status": "success", "summary": "x"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "EXTERNAL_TASK_NOT_DISPATCHED"


# ── delegate endpoint ───────────────────────────────────────────────────


def _stub_start_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_start_run(self, *, run, spec, flow=None, **kw):
        captured["run_id"] = run.id
        from app.scheduler.controller import RunController
        return RunController(run=run, spec=spec)

    monkeypatch.setattr(engine_mod.FlowScheduler, "start_run", fake_start_run)
    return captured


def test_delegate_requires_pair_token(app_client: TestClient) -> None:
    flow = _mk_flow()
    body = {
        "flowId": flow.id, "callbackUrl": "http://origin/cb",
        "callbackToken": "tok",
    }
    r = app_client.post("/api/external/delegate", json=body)
    assert r.status_code == 401
    assert r.json()["error"] == "EXTERNAL_PAIR_TOKEN_MISSING"
    r2 = app_client.post(
        "/api/external/delegate", json=body,
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert r2.status_code == 401
    assert r2.json()["error"] == "EXTERNAL_PAIR_TOKEN_INVALID"


def test_delegate_triggers_unattended_run_with_callback_marker(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _stub_start_run(monkeypatch)
    cfg = load_config()
    save_config(cfg.model_copy(
        update={"external_pair_tokens": {"machine-a": "s3cret"}},
    ))
    flow = _mk_flow()
    r = app_client.post(
        "/api/external/delegate",
        json={
            "flowId": flow.id,
            "runtimePrompt": "delegated task sheet",
            "callbackUrl": "http://origin/api/external/tasks/r/t/complete",
            "callbackToken": "tok-123",
            "sourceRunId": "run-remote", "sourceTaskId": "t-remote",
        },
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    assert captured["run_id"] == run_id
    row = get_storage().run_get(run_id)
    assert row is not None
    # Unattended contract + callback marker stamped in run.inputs.
    assert row.inputs[UNATTENDED_KEY] == "true"
    import json as _json
    marker = _json.loads(row.inputs[EXTERNAL_CALLBACK_KEY])
    assert marker["url"].endswith("/complete")
    assert marker["token"] == "tok-123"
    assert row.is_scheduled is False  # is_scheduled stays "timed trigger" only


def test_delegate_unknown_flow_404(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    save_config(cfg.model_copy(
        update={"external_pair_tokens": {"machine-a": "s3cret"}},
    ))
    r = app_client.post(
        "/api/external/delegate",
        json={"flowId": "missing", "callbackUrl": "u", "callbackToken": "t"},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 404


# ── guard: /api/external Host rule ──────────────────────────────────────


def test_external_prefix_allows_remote_host_by_default() -> None:
    # Default-open: non-loopback Host reaches the endpoint (ticket auth decides).
    cfg = load_config()
    assert cfg.external_api_expose is True
    with TestClient(create_app(), base_url="http://203.0.113.5:17017") as c:
        r = c.post(
            "/api/external/tasks/r1/t1/complete",
            json={"status": "success", "summary": "x", "token": "a.b"},
        )
        assert r.status_code == 401
        assert r.json()["error"] == "EXTERNAL_TICKET_INVALID"


def test_external_prefix_blocked_for_remote_host_when_locked_down() -> None:
    # Opt-out: ``csflow external expose off`` re-locks to loopback-only.
    cfg = load_config()
    save_config(cfg.model_copy(update={"external_api_expose": False}))
    try:
        with TestClient(create_app(), base_url="http://203.0.113.5:17017") as c:
            r = c.post(
                "/api/external/tasks/r1/t1/complete",
                json={"status": "success", "summary": "x", "token": "a.b"},
            )
            assert r.status_code == 403
            assert r.json()["error"] == "HOST_NOT_ALLOWED"
    finally:
        save_config(load_config().model_copy(
            update={"external_api_expose": True},
        ))


def test_external_lockdown_blocks_remote_client_with_forged_loopback_host() -> None:
    # Lockdown must hold against a remote socket presenting "Host: 127.0.0.1".
    inner = create_app()

    async def remote_client_app(scope, receive, send):
        scope = dict(scope)
        scope["client"] = ("203.0.113.5", 55555)
        await inner(scope, receive, send)

    cfg = load_config()
    save_config(cfg.model_copy(update={"external_api_expose": False}))
    try:
        with TestClient(remote_client_app, base_url="http://127.0.0.1:17017") as c:
            r = c.post(
                "/api/external/tasks/r1/t1/complete",
                json={"status": "success", "summary": "x", "token": "a.b"},
            )
            assert r.status_code == 403
            assert r.json()["error"] == "HOST_NOT_ALLOWED"
    finally:
        save_config(load_config().model_copy(
            update={"external_api_expose": True},
        ))


def test_main_api_still_loopback_only_when_external_open() -> None:
    # Default-open /api/external must NOT loosen the main /api surface.
    cfg = load_config()
    save_config(cfg.model_copy(
        update={"external_api_expose": True, "api_token": "tok-guard"},
    ))
    try:
        with TestClient(create_app(), base_url="http://203.0.113.5:17017") as c:
            r = c.get("/api/flows")
            assert r.status_code == 403
            assert r.json()["error"] == "HOST_NOT_ALLOWED"
    finally:
        save_config(load_config().model_copy(
            update={"api_token": None},
        ))


# ── Loopback "remote" round-trips (local URL mimics the peer) ────────────


def test_webhook_local_endpoint_dispatch_then_complete(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Webhook channel: dispatch POSTs to a local URL, partner completes via ticket.

    The "remote" partner is simulated by the same process: ``_post_outbound``
    is stubbed to capture the package, then we hit the receipt endpoint just
    like an integrated system would.
    """
    import asyncio

    from app.models import ExternalNodeConfig
    from app.services import external_tasks as ext_svc

    fake = _fake_mcp(monkeypatch)
    storage = get_storage()
    flow = _mk_flow()
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-wh-loop",
        status=RunStatus.running, inputs={}, user="alice",
    ))
    agent = FlowAgent(
        id="ext-node", kind=AgentKind.external,
        external=ExternalNodeConfig(
            channel=ExternalChannel.webhook,
            # Local URL standing in for a partner system.
            endpoint_url="http://127.0.0.1:17017/partner/hook",
        ),
    )
    captured: dict[str, Any] = {}

    async def fake_post(url: str, body: dict[str, Any]) -> None:
        captured["url"] = url
        captured["body"] = body

    monkeypatch.setattr(ext_svc, "_post_outbound", fake_post)
    cfg = load_config()
    save_config(cfg.model_copy(
        update={"external_callback_base_url": "http://127.0.0.1:17017"},
    ))

    asyncio.run(ext_svc.dispatch_external_task(
        storage=storage, run_id=run.id, team_name=run.team_name,
        agent=agent, task_id="t1", message="sheet",
        package={
            "subject": "s", "description": "do the thing",
            "outputRequirement": None, "clawteamTaskId": "CT-wh",
            "leaderAgentId": "leader",
        },
    ))
    assert captured["url"] == "http://127.0.0.1:17017/partner/hook"
    body = captured["body"]
    assert body["schemaVersion"] == 1
    assert body["event"] == "external_task_dispatch"
    assert body["callbackUrl"].startswith("http://127.0.0.1:17017/api/external/tasks/")
    assert body["callbackUrl"].endswith("/complete")
    ticket = body["callbackToken"]

    # Partner system (local) submits the result with the one-time ticket.
    r = app_client.post(
        body["callbackUrl"].replace("http://127.0.0.1:17017", ""),
        json={"status": "success", "summary": "partner finished"},
        headers={"Authorization": f"Bearer {ticket}"},
    )
    assert r.status_code == 200, r.text
    assert fake.mailbox_calls[0]["content"] == "task t1 done: partner finished"
    assert fake.task_updates[0]["task_id"] == "CT-wh"


def test_remote_csflow_loopback_delegate_then_callback_complete(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote ClawsomeFlow channel: same local instance plays origin + peer.

    Origin dispatches ``remote_csflow`` with ``base_url`` pointing at itself;
    the peer ``/delegate`` accepts via pair-token; a terminal callback then
    completes the origin task through the absolute callback URL.
    """
    import asyncio
    import json as _json

    import httpx

    from app.models import ExternalNodeConfig
    from app.services import external_tasks as ext_svc

    fake = _fake_mcp(monkeypatch)
    captured_start = _stub_start_run(monkeypatch)

    cfg = load_config()
    save_config(cfg.model_copy(update={
        "external_pair_tokens": {"peer-local": "pair-secret"},
        "external_remote_targets": {"peer-local": "pair-secret"},
        "external_callback_base_url": "http://127.0.0.1:17017",
    }))

    storage = get_storage()
    # Peer-side Flow that will be delegated to (same machine).
    peer_flow = _mk_flow()
    # Origin run whose external node points at the local peer.
    origin_flow = storage.flow_create(Flow(
        name="origin", owner_user="alice",
    ).with_spec(FlowSpec(agents=[
        FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r", is_leader=True),
        FlowAgent(
            id="remote-node", kind=AgentKind.external,
            external=ExternalNodeConfig(
                channel=ExternalChannel.remote_csflow,
                base_url="http://127.0.0.1:17017",
                flow_id=peer_flow.id,
                pair_token_ref="peer-local",
            ),
        ),
    ], tasks=[
        FlowTask(id="t1", owner_agent_id="remote-node", subject="delegate me"),
        FlowTask(id="ts", owner_agent_id="leader", subject="sum",
                 depends_on=["t1"], is_leader_summary=True),
    ])))
    origin_run = storage.run_create(FlowRun(
        flow_id=origin_flow.id, flow_version=1, team_name="csflow-origin-loop",
        status=RunStatus.running, inputs={}, user="alice",
    ))

    # Route both async (delegate outbound) and sync (delegate callback)
    # httpx calls to the same FastAPI app — local URL stands in for "remote".
    from urllib.parse import urlparse

    def _loopback_response(url: str, *, json: dict[str, Any] | None = None,
                           headers: dict[str, str] | None = None) -> httpx.Response:
        parsed = urlparse(url)
        r = app_client.post(parsed.path, json=json or {}, headers=headers or {})
        return httpx.Response(
            r.status_code,
            content=r.content,
            headers={"content-type": "application/json"},
            request=httpx.Request("POST", url),
        )

    class _LoopbackClient:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_LoopbackClient":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(
            self, url: str, *, json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            return _loopback_response(url, json=json, headers=headers)

    monkeypatch.setattr(httpx, "AsyncClient", _LoopbackClient)
    monkeypatch.setattr(
        httpx, "post",
        lambda url, **kw: _loopback_response(
            url, json=kw.get("json"), headers=kw.get("headers"),
        ),
    )

    agent = FlowAgent(
        id="remote-node", kind=AgentKind.external,
        external=ExternalNodeConfig(
            channel=ExternalChannel.remote_csflow,
            base_url="http://127.0.0.1:17017",
            flow_id=peer_flow.id,
            pair_token_ref="peer-local",
        ),
    )

    asyncio.run(ext_svc.dispatch_external_task(
        storage=storage, run_id=origin_run.id, team_name=origin_run.team_name,
        agent=agent, task_id="t1", message="do remote work",
        package={
            "subject": "delegate me", "description": "brief",
            "outputRequirement": None, "clawteamTaskId": "CT-rem",
            "leaderAgentId": "leader",
        },
    ))
    # Peer accepted the delegation (stubbed start_run).
    assert captured_start["run_id"]
    peer_run = storage.run_get(captured_start["run_id"])
    assert peer_run is not None
    assert peer_run.inputs[UNATTENDED_KEY] == "true"
    marker = _json.loads(peer_run.inputs[EXTERNAL_CALLBACK_KEY])
    assert marker["token"]
    assert "/api/external/tasks/" in marker["url"]
    assert origin_run.id in marker["url"]

    # Peer finishes → run_update fires the delegate callback on a daemon
    # thread; the loopback httpx client routes it to origin /complete.
    storage.event_append(RunEvent(
        run_id=peer_run.id, type="run_terminal_execution_log",
        payload={"worker_report_history": [
            {"from_agent": "leader", "summary": "leader final reply: peer done"},
        ]},
    ))
    peer_run.status = RunStatus.completed
    storage.run_update(peer_run)

    # Wait briefly for the daemon callback thread.
    import time as _time
    for _ in range(50):
        if fake.mailbox_calls:
            break
        _time.sleep(0.05)

    assert fake.mailbox_calls, "origin never received the peer callback"
    assert "peer done" in fake.mailbox_calls[0]["content"]
    assert fake.task_updates[0]["task_id"] == "CT-rem"


# ── remote-node one-click wiring (remote-call-info / register-remote) ──


def _mk_flow_with_params(owner: str = "alice") -> Flow:
    storage = get_storage()
    spec = FlowSpec(
        agents=[
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r", is_leader=True),
        ],
        tasks=[
            FlowTask(id="ts", owner_agent_id="leader", subject="sum",
                     is_leader_summary=True),
        ],
        variables={"csflow.runtime.param_fields": '["需求描述", "目标目录"]'},
    )
    return storage.flow_create(
        Flow(
            name="target",
            description="Assemble a travel itinerary from upstream notes.",
            owner_user=owner,
        ).with_spec(spec),
    )


def test_remote_call_info_mints_token_and_returns_param_fields(app_client) -> None:
    flow = _mk_flow_with_params()
    resp = app_client.post(f"/api/flows/{flow.id}/remote-call-info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["flowId"] == flow.id
    assert body["flowName"] == "target"
    assert body["flowDescription"] == "Assemble a travel itinerary from upstream notes."
    assert body["paramFields"] == ["需求描述", "目标目录"]
    assert body["pairTokenName"] == f"remote-{flow.id}"
    assert body["pairSecret"]
    # baseUrl is omitted from the API blob — origin operator types reachability
    # on the subtask form (SSH tunnels poison request Host).
    assert "baseUrl" not in body
    # The inbound pairing credential is now stored in config (idempotent).
    cfg = load_config()
    assert cfg.external_pair_tokens.get(f"remote-{flow.id}") == body["pairSecret"]
    # Host-derived URL must never be persisted as the callback base.
    assert cfg.external_callback_base_url in (None, "")
    # Second call reuses the same secret (idempotent).
    resp2 = app_client.post(f"/api/flows/{flow.id}/remote-call-info")
    assert resp2.json()["pairSecret"] == body["pairSecret"]
    assert "baseUrl" not in resp2.json()


def test_remote_call_info_never_echoes_callback_base_url(app_client) -> None:
    """external_callback_base_url is for inbound callbacks, not peer reachability."""
    flow = _mk_flow_with_params()
    save_config(
        load_config().model_copy(
            update={"external_callback_base_url": "http://peer.example:17017"},
        ),
    )
    resp = app_client.post(f"/api/flows/{flow.id}/remote-call-info")
    assert resp.status_code == 200, resp.text
    assert "baseUrl" not in resp.json()


def test_register_remote_target_stores_secret_off_spec(app_client) -> None:
    info = {
        "kind": "csflow.remote_call_info",
        "baseUrl": "http://peer-host:17017/",
        "flowId": "flow-remote-1",
        "flowName": "Peer Flow",
        "paramFields": ["需求描述", "目标目录"],
        "pairTokenName": "remote-flow-remote-1",
        "pairSecret": "s3cr3t-value",
    }
    resp = app_client.post("/api/flows/remote-targets", json=info)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["baseUrl"] == "http://peer-host:17017"  # trailing slash trimmed
    assert body["flowId"] == "flow-remote-1"
    assert body["paramFields"] == ["需求描述", "目标目录"]
    assert body["pairTokenRef"] == "remote-flow-remote-1"
    # Secret lands in config.external_remote_targets, never returned back.
    assert "pairSecret" not in body and "s3cr3t-value" not in resp.text
    cfg = load_config()
    assert cfg.external_remote_targets.get("remote-flow-remote-1") == "s3cr3t-value"


def test_register_remote_target_rejects_incomplete_info(app_client) -> None:
    resp = app_client.post(
        "/api/flows/remote-targets",
        json={"baseUrl": "", "flowId": "", "pairTokenName": "x", "pairSecret": ""},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_PAYLOAD"
