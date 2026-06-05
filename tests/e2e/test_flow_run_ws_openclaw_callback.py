from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import pytest
import websockets

from app.config import Config
from app.integrations import internal_token
from tests.common.e2e_resources import E2EResourceTracker
from tests.common.runtime_helpers import require_env

pytestmark = pytest.mark.e2e


def _api_base() -> str:
    return require_env("CSFLOW_E2E_BASE_URL").rstrip("/")


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def _flow_payload() -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    return {
        "name": f"e2e-flow-{suffix}",
        "description": "runtime e2e flow for run/ws chain",
        "spec": {
            "agents": [
                {
                    "id": f"worker-{suffix}",
                    "kind": "custom",
                    "command": ["bash", "-lc", "exit 0"],
                    "repo": _repo_root(),
                    "isLeader": True,
                    "mergeStrategy": "skip",
                    "onFailure": "skip",
                    "maxRetries": 0,
                },
            ],
            "tasks": [
                {
                    "id": f"task-{suffix}",
                    "ownerAgentId": f"worker-{suffix}",
                    "subject": "Runtime E2E smoke",
                    "description": "Force a short-lived worker and validate run/event APIs.",
                    "dependsOn": [],
                    "isLeaderSummary": True,
                    "timeoutSeconds": 30,
                },
            ],
            "variables": {},
        },
    }


def _wait_for_events(
    client: httpx.Client,
    run_id: str,
    *,
    timeout_sec: float = 30.0,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        resp = client.get(f"/api/runs/{run_id}/events")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("items", [])
        if items:
            return items
        time.sleep(0.5)
    raise AssertionError("run events did not appear within timeout")


async def _wait_for_events_async(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    timeout_sec: float = 30.0,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        resp = await client.get(f"/api/runs/{run_id}/events")
        assert resp.status_code == 200, resp.text
        items = resp.json().get("items", [])
        if items:
            return items
        await asyncio.sleep(0.5)
    raise AssertionError("run events did not appear within timeout")


def _ws_url(base_url: str, run_id: str, since_id: int) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode({"sinceId": since_id})
    return f"{scheme}://{parsed.netloc}/ws/{run_id}?{query}"


def _ensure_openclaw_json(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    agents = data.get("agents")
    if not isinstance(agents, dict):
        agents = {}
    agent_list = agents.get("list")
    if not isinstance(agent_list, list):
        agent_list = []
    agents["list"] = agent_list
    data["agents"] = agents
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_flow_run_and_events_chain(e2e_resources: E2EResourceTracker) -> None:
    base_url = _api_base()
    flow_payload = _flow_payload()

    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        create_resp = client.post("/api/flows", json=flow_payload)
        assert create_resp.status_code == 201, create_resp.text
        flow_id = e2e_resources.track_flow(create_resp.json()["id"])

        trigger_resp = client.post(f"/api/flows/{flow_id}/runs", json={"inputs": {"e2e": "1"}})
        assert trigger_resp.status_code == 202, trigger_resp.text
        run_id = e2e_resources.track_run(trigger_resp.json()["id"])

        detail_resp = client.get(f"/api/runs/{run_id}")
        assert detail_resp.status_code == 200, detail_resp.text
        detail = detail_resp.json()
        assert detail["flowId"] == flow_id
        assert isinstance(detail.get("teamName"), str) and detail["teamName"]

        events = _wait_for_events(client, run_id, timeout_sec=45.0)
        assert isinstance(events[0].get("id"), int)
        assert isinstance(events[0].get("type"), str)


@pytest.mark.asyncio
async def test_run_ws_ping_and_backfill(e2e_resources_async: E2EResourceTracker) -> None:
    base_url = _api_base()
    flow_payload = _flow_payload()

    async with httpx.AsyncClient(base_url=base_url, timeout=20.0) as client:
        create_resp = await client.post("/api/flows", json=flow_payload)
        assert create_resp.status_code == 201, create_resp.text
        flow_id = e2e_resources_async.track_flow(create_resp.json()["id"])

        trigger_resp = await client.post(
            f"/api/flows/{flow_id}/runs",
            json={"inputs": {"e2e": "ws"}},
        )
        assert trigger_resp.status_code == 202, trigger_resp.text
        run_id = e2e_resources_async.track_run(trigger_resp.json()["id"])

        events = await _wait_for_events_async(client, run_id, timeout_sec=45.0)
        since_id = max(0, int(events[-1]["id"]) - 1)
        ws_url = _ws_url(base_url, run_id, since_id)

        async with websockets.connect(ws_url, open_timeout=10, close_timeout=5) as ws:
            await ws.send(json.dumps({"type": "ping"}))
            got_pong = False
            got_backfill = False
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not (got_pong and got_backfill):
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(raw)
                if msg.get("type") == "pong":
                    got_pong = True
                if isinstance(msg.get("id"), int):
                    got_backfill = True
            assert got_pong, "ws ping/pong failed"
            assert got_backfill, "ws backfill did not deliver prior event"


def test_openclaw_internal_commit_callback_path(e2e_resources: E2EResourceTracker) -> None:
    base_url = _api_base()
    openclaw_home = Path(require_env("CSFLOW_RUNTIME_OPENCLAW_HOME"))
    _ensure_openclaw_json(openclaw_home / "openclaw.json")

    cfg_path = Path(require_env("CSFLOW_HOME")) / "config.json"
    cfg = Config.model_validate(json.loads(cfg_path.read_text(encoding="utf-8")))
    user = cfg.default_user
    agent_id = e2e_resources.track_agent(f"e2e-openclaw-{uuid.uuid4().hex[:8]}")

    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        start_resp = client.post(
            "/api/openclaw/agents/nl-create",
            json={"prompt": "Create a temporary e2e validation agent."},
        )
        assert start_resp.status_code == 202, start_resp.text
        request_id = start_resp.json()["requestId"]

        token = internal_token.mint_token(
            request_id=request_id,
            user=user,
            purpose="openclaw_agent_mgmt",
            ttl_seconds=300,
            config=cfg,
        )
        commit_resp = client.post(
            "/api/internal/openclaw/agents/commit",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "requestId": request_id,
                "id": agent_id,
                "name": f"E2E Agent {agent_id}",
                "description": "Temporary agent for internal callback smoke test.",
                "identity": {"theme": "e2e-smoke"},
            },
        )
        assert commit_resp.status_code == 201, commit_resp.text
        assert commit_resp.json()["agentId"] == agent_id

        status_resp = client.get(f"/api/openclaw/agents/nl-create/{request_id}")
        assert status_resp.status_code == 200, status_resp.text
        status_payload = status_resp.json()
        assert status_payload["status"] == "succeeded"
        assert status_payload["requestedAgentId"] == agent_id

        detail_resp = client.get(f"/api/openclaw/agents/{agent_id}")
        assert detail_resp.status_code == 200, detail_resp.text
        assert detail_resp.json()["id"] == agent_id
