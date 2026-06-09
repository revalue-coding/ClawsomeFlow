"""End-to-end API chain test: create agent -> create flow -> trigger run."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.main import create_app
from app.models import RunEvent, RunStatus
from app.scheduler import engine as engine_mod


def _has_git() -> bool:
    return shutil.which("git") is not None


@pytest.fixture
def fake_openclaw_home(tmp_path: Path) -> Path:
    oc_home = tmp_path / "openclaw_home"
    oc_home.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(
        update={
            "openclaw_home": str(oc_home),
            "default_user": "alice",
        }
    )
    save_config(cfg)
    (oc_home / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {}, "list": []},
                "gateway": {"port": 18789, "auth": {"token": "T"}},
            }
        )
    )
    return oc_home


@pytest.fixture
def client(fake_openclaw_home: Path):
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def repo(tmp_path: Path) -> str:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=r, check=True, capture_output=True)
    (r / "README.md").write_text("# test repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=r, check=True, capture_output=True)
    return str(r)


def test_api_chain_from_agent_create_to_flow_run(
    client: TestClient,
    repo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")

    from app.api import openclaw_agents as router_mod

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")

    async def _fake_wait_until_gateway_agent_ready(*, agent_id: str) -> None:
        del agent_id

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del session_key, message, model_override, timeout_sec
        return {"id": f"bootstrap-{agent_id}", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_wait_until_gateway_agent_ready", _fake_wait_until_gateway_agent_ready)
    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    captured: dict[str, Any] = {}

    def fake_start_run(self, *, run, spec, flow=None, storage=None, **_kwargs):
        stored = storage.run_get(run.id)
        assert stored is not None
        stored.status = RunStatus.completed
        stored.finished_at = datetime.now(timezone.utc)
        storage.run_update(stored)
        storage.event_append(
            RunEvent(
                run_id=stored.id,
                type="run_completed",
                payload={"source": "test_api_full_chain"},
            )
        )
        captured["run_id"] = stored.id
        captured["agent_count"] = len(spec.agents)
        from app.scheduler.controller import RunController

        return RunController(run=stored, spec=spec, flow=flow, storage=storage)

    monkeypatch.setattr(engine_mod.FlowScheduler, "start_run", fake_start_run)

    agent_id = f"chain-agent-{uuid.uuid4().hex[:8]}"
    create_resp = client.post(
        "/api/openclaw/agents",
        json={
            "id": agent_id,
            "name": "Chain Agent",
            "description": "API full chain test agent",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    create_body = create_resp.json()
    assert create_body["id"] == agent_id

    flow_payload = {
        "name": f"chain-flow-{uuid.uuid4().hex[:8]}",
        "description": "full API chain test",
        "spec": {
            "agents": [
                {"id": agent_id, "kind": "openclaw", "isLeader": False},
                # cursor: non-OpenClaw (repo validated) but not managed-enforced,
                # so this end-to-end chain doesn't need a seeded managed agent.
                {"id": "leader", "kind": "cursor", "repo": repo, "isLeader": True},
            ],
            "tasks": [
                {"id": "t1", "ownerAgentId": agent_id, "subject": "Execute worker task"},
                {
                    "id": "ts",
                    "ownerAgentId": "leader",
                    "subject": "Summarize results",
                    "dependsOn": ["t1"],
                    "isLeaderSummary": True,
                },
            ],
            "variables": {},
        },
    }
    flow_resp = client.post("/api/flows", json=flow_payload)
    assert flow_resp.status_code == 201, flow_resp.text
    flow_id = flow_resp.json()["id"]

    run_resp = client.post(
        f"/api/flows/{flow_id}/runs",
        json={"inputs": {"scenario": "full-chain"}},
    )
    assert run_resp.status_code == 202, run_resp.text
    run_id = run_resp.json()["id"]
    assert captured["run_id"] == run_id
    assert captured["agent_count"] == 2

    run_detail_resp = client.get(f"/api/runs/{run_id}")
    assert run_detail_resp.status_code == 200, run_detail_resp.text
    run_detail = run_detail_resp.json()
    assert run_detail["flowId"] == flow_id
    assert run_detail["status"] == "completed"

    events_resp = client.get(f"/api/runs/{run_id}/events")
    assert events_resp.status_code == 200, events_resp.text
    event_types = {item["type"] for item in events_resp.json()["items"]}
    assert "run_completed" in event_types
