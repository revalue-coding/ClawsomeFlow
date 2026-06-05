"""Tests for :mod:`app.api.flows` (HTTP CRUD)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import paths
from app.config import load_config, save_config
from app.integrations import openclaw_json as oj
from app.main import create_app
from app.models import FlowRun, RunEvent, RunStatus
from app.storage import get_storage


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def repo(tmp_path: Path) -> str:
    """A real git repo with an initial commit, suitable as FlowAgent.repo."""
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    (r / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=r, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=tester@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=r,
        check=True,
    )
    return str(r)


def _flow_payload(repo_path: str, name: str = "test-flow") -> dict:
    """Minimal valid flow payload for POST /api/flows.

    Uses the canonical 2-agent shape (1 worker + 1 leader) and 2-task shape
    (worker task + leader summary). The leader-only-owns-summary constraint
    (DEV.md §6 / validators) forbids single-agent shapes.
    """
    return {
        "name": name,
        "description": "demo",
        "spec": {
            "agents": [
                {"id": "alice", "kind": "claude", "repo": repo_path,
                 "isLeader": False},
                {"id": "leader", "kind": "claude", "repo": repo_path,
                 "isLeader": True},
            ],
            "tasks": [
                {"id": "t1", "ownerAgentId": "alice", "subject": "do work"},
                {"id": "ts", "ownerAgentId": "leader", "subject": "summarise",
                 "dependsOn": ["t1"], "isLeaderSummary": True},
            ],
        },
    }


def _flow_payload_with_openclaw(
    repo_path: str,
    *,
    openclaw_agent_id: str,
    name: str = "test-flow-openclaw",
) -> dict:
    payload = _flow_payload(repo_path, name=name)
    payload["spec"]["agents"] = [
        {"id": openclaw_agent_id, "kind": "openclaw", "isLeader": False},
        {"id": "leader", "kind": "claude", "repo": repo_path, "isLeader": True},
    ]
    payload["spec"]["tasks"][0]["ownerAgentId"] = openclaw_agent_id
    return payload


def _seed_openclaw_agent_runtime_record(*, tmp_path: Path, agent_id: str) -> None:
    oc_home = tmp_path / "openclaw-home"
    oc_home.mkdir()
    cfg = load_config()
    save_config(cfg.model_copy(update={"openclaw_home": str(oc_home)}))
    workspace = paths.agent_dir(agent_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (oc_home / "openclaw.json").write_text(
        json.dumps({
            "agents": {
                "defaults": {},
                "list": [{
                    "id": agent_id,
                    "name": "JSON Only OpenClaw",
                    "workspace": str(workspace),
                    "default": False,
                }],
            },
            "gateway": {"port": 18789, "auth": {"token": "T"}},
        }),
        encoding="utf-8",
    )
    registry = oj.managed_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"agent_ids": [agent_id]}), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# Happy paths
# ──────────────────────────────────────────────────────────────────────


def test_create_then_get(client: TestClient, repo: str) -> None:
    payload = _flow_payload(repo)
    payload["cleanupTeamOnFinish"] = False
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    flow_id = body["id"]
    assert body["version"] == 1

    detail = client.get(f"/api/flows/{flow_id}").json()
    assert detail["id"] == flow_id
    assert detail["name"] == "test-flow"
    assert detail["cleanupTeamOnFinish"] is True
    assert detail["spec"]["agents"][0]["id"] == "alice"
    assert "isLeader" in detail["spec"]["agents"][0]
    assert "is_leader" not in detail["spec"]["agents"][0]
    assert "ownerAgentId" in detail["spec"]["tasks"][0]
    assert "owner_agent_id" not in detail["spec"]["tasks"][0]


def test_list(client: TestClient, repo: str) -> None:
    for n in ("F1", "F2", "F3"):
        client.post("/api/flows", json=_flow_payload(repo, name=n))
    body = client.get("/api/flows").json()
    assert body["total"] == 3
    assert {f["name"] for f in body["items"]} == {"F1", "F2", "F3"}


def test_list_q_filter(client: TestClient, repo: str) -> None:
    client.post("/api/flows", json=_flow_payload(repo, name="customer-flow"))
    client.post("/api/flows", json=_flow_payload(repo, name="risk-flow"))
    body = client.get("/api/flows?q=customer").json()
    assert body["total"] == 1


def test_update_with_version(client: TestClient, repo: str) -> None:
    created = client.post("/api/flows", json=_flow_payload(repo)).json()
    flow_id = created["id"]

    payload = _flow_payload(repo, name="renamed")
    payload["version"] = 1
    payload["cleanupTeamOnFinish"] = False
    resp = client.put(f"/api/flows/{flow_id}", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2

    detail = client.get(f"/api/flows/{flow_id}").json()
    assert detail["name"] == "renamed"
    assert detail["cleanupTeamOnFinish"] is True


def test_create_flow_with_openclaw_warns_when_runtime_not_running(
    client: TestClient,
    repo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = "json-only-oc-warning-create"
    _seed_openclaw_agent_runtime_record(tmp_path=tmp_path, agent_id=aid)
    monkeypatch.setattr(
        "app.services.openclaw_agents.probe_runtime_running",
        lambda *_a, **_kw: (False, "health_failed"),
    )
    payload = _flow_payload_with_openclaw(repo, openclaw_agent_id=aid)
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["warnings"]
    assert body["warnings"][0]["code"] == "OPENCLAW_RUNTIME_NOT_RUNNING"


def test_update_flow_with_openclaw_warns_when_runtime_not_running(
    client: TestClient,
    repo: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = "json-only-oc-warning-update"
    _seed_openclaw_agent_runtime_record(tmp_path=tmp_path, agent_id=aid)
    created = client.post("/api/flows", json=_flow_payload(repo)).json()
    flow_id = created["id"]
    monkeypatch.setattr(
        "app.services.openclaw_agents.probe_runtime_running",
        lambda *_a, **_kw: (False, "health_failed"),
    )
    payload = _flow_payload_with_openclaw(repo, openclaw_agent_id=aid, name="warn-update")
    payload["version"] = 1
    resp = client.put(f"/api/flows/{flow_id}", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["warnings"]
    assert body["warnings"][0]["code"] == "OPENCLAW_RUNTIME_NOT_RUNNING"


def test_update_invalid_dag_rejected_and_not_persisted(client: TestClient, repo: str) -> None:
    created = client.post("/api/flows", json=_flow_payload(repo)).json()
    flow_id = created["id"]

    payload = _flow_payload(repo, name="should-not-save")
    payload["version"] = 1
    payload["spec"]["tasks"] = [
        {"id": "t1", "ownerAgentId": "alice", "subject": "x", "dependsOn": ["ts"]},
        {"id": "ts", "ownerAgentId": "leader", "subject": "y",
         "dependsOn": ["t1"], "isLeaderSummary": True},
    ]
    resp = client.put(f"/api/flows/{flow_id}", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_DAG"

    detail = client.get(f"/api/flows/{flow_id}").json()
    assert detail["name"] == "test-flow"
    assert detail["version"] == 1


def test_delete(client: TestClient, repo: str) -> None:
    flow_id = client.post("/api/flows", json=_flow_payload(repo)).json()["id"]
    resp = client.delete(f"/api/flows/{flow_id}")
    assert resp.status_code == 204
    assert client.get(f"/api/flows/{flow_id}").status_code == 404


def test_delete_with_terminal_run_history(client: TestClient, repo: str) -> None:
    flow_id = client.post("/api/flows", json=_flow_payload(repo)).json()["id"]
    storage = get_storage()
    run = storage.run_create(
        FlowRun(
            flow_id=flow_id,
            flow_version=1,
            team_name=f"csflow-{flow_id[-8:]}",
            status=RunStatus.completed,
            user="alice",
        )
    )
    storage.event_append(
        RunEvent(
            run_id=run.id,
            type="run_started",
            payload={},
        )
    )

    resp = client.delete(f"/api/flows/{flow_id}")
    assert resp.status_code == 204
    assert client.get(f"/api/flows/{flow_id}").status_code == 404
    assert storage.run_get(run.id) is None


def test_get_other_user_forbidden(
    client: TestClient, repo: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_id = client.post("/api/flows", json=_flow_payload(repo)).json()["id"]
    monkeypatch.setenv("CSFLOW_USER", "bob")
    resp = client.get(f"/api/flows/{flow_id}")
    assert resp.status_code == 403
    assert resp.json()["error"] == "FORBIDDEN"


def test_update_other_user_forbidden(
    client: TestClient, repo: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_id = client.post("/api/flows", json=_flow_payload(repo)).json()["id"]
    payload = _flow_payload(repo, name="blocked")
    payload["version"] = 1
    monkeypatch.setenv("CSFLOW_USER", "bob")
    resp = client.put(f"/api/flows/{flow_id}", json=payload)
    assert resp.status_code == 403
    assert resp.json()["error"] == "FORBIDDEN"


def test_delete_other_user_forbidden(
    client: TestClient, repo: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow_id = client.post("/api/flows", json=_flow_payload(repo)).json()["id"]
    monkeypatch.setenv("CSFLOW_USER", "bob")
    resp = client.delete(f"/api/flows/{flow_id}")
    assert resp.status_code == 403
    assert resp.json()["error"] == "FORBIDDEN"


# ──────────────────────────────────────────────────────────────────────
# Error paths
# ──────────────────────────────────────────────────────────────────────


def test_get_nonexistent(client: TestClient) -> None:
    resp = client.get("/api/flows/flow-nope")
    assert resp.status_code == 404
    assert resp.json()["error"] == "NOT_FOUND"


def test_create_invalid_dag(client: TestClient, repo: str) -> None:
    payload = _flow_payload(repo)
    payload["spec"]["tasks"] = [
        {"id": "t1", "ownerAgentId": "alice", "subject": "x", "dependsOn": ["ts"]},
        {"id": "ts", "ownerAgentId": "leader", "subject": "y",
         "dependsOn": ["t1"], "isLeaderSummary": True},
    ]
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_DAG"


def test_create_summary_without_dependency_rejected(client: TestClient, repo: str) -> None:
    payload = _flow_payload(repo)
    payload["spec"]["tasks"][1]["dependsOn"] = []
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "SUMMARY_NO_DEPENDENCY"


def test_create_empty_overall_goal_rejected(client: TestClient, repo: str) -> None:
    payload = _flow_payload(repo)
    payload["description"] = "   "
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_FLOW_DESCRIPTION"


def test_create_no_leader(client: TestClient, repo: str) -> None:
    payload = _flow_payload(repo)
    for a in payload["spec"]["agents"]:
        a["isLeader"] = False
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_LEADER"


def test_create_invalid_repo(client: TestClient, tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    payload = _flow_payload(str(not_a_repo))
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_REPO"
    assert resp.json()["details"]["reason"] == "not_git_repo"


def test_create_repo_without_initial_commit(client: TestClient, tmp_path: Path) -> None:
    repo = tmp_path / "repo-no-init-commit"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    payload = _flow_payload(str(repo))
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_REPO"
    assert resp.json()["details"]["reason"] == "no_initial_commit"


def test_create_missing_agent_repo(client: TestClient, repo: str) -> None:
    payload = _flow_payload(repo)
    del payload["spec"]["agents"][0]["repo"]
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "MISSING_AGENT_REPO"


def test_create_repo_path_not_found(client: TestClient, tmp_path: Path) -> None:
    missing = tmp_path / "missing-repo"
    payload = _flow_payload(str(missing))
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_REPO"
    assert resp.json()["details"]["reason"] == "path_not_found"


def test_create_openclaw_unknown(client: TestClient, repo: str) -> None:
    payload = _flow_payload(repo)
    # Replace 'alice' with an OpenClaw worker that doesn't exist in the DB.
    payload["spec"]["agents"] = [
        {"id": "missing-oc", "kind": "openclaw", "isLeader": False},
        {"id": "leader", "kind": "claude", "repo": repo, "isLeader": True},
    ]
    payload["spec"]["tasks"][0]["ownerAgentId"] = "missing-oc"
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "OPENCLAW_AGENT_NOT_FOUND"


def test_update_empty_overall_goal_rejected(client: TestClient, repo: str) -> None:
    flow_id = client.post("/api/flows", json=_flow_payload(repo)).json()["id"]
    payload = _flow_payload(repo, name="renamed")
    payload["version"] = 1
    payload["description"] = ""
    resp = client.put(f"/api/flows/{flow_id}", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "INVALID_FLOW_DESCRIPTION"


def test_create_openclaw_registered_in_json_without_db_row(
    client: TestClient,
    repo: str,
    tmp_path: Path,
) -> None:
    aid = "json-only-oc"
    _seed_openclaw_agent_runtime_record(tmp_path=tmp_path, agent_id=aid)

    payload = _flow_payload_with_openclaw(repo, openclaw_agent_id=aid)
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 201, resp.text


def test_update_version_conflict(client: TestClient, repo: str) -> None:
    flow_id = client.post("/api/flows", json=_flow_payload(repo)).json()["id"]
    payload = _flow_payload(repo, name="renamed")
    payload["version"] = 999
    resp = client.put(f"/api/flows/{flow_id}", json=payload)
    assert resp.status_code == 409
    assert resp.json()["error"] == "VERSION_CONFLICT"


def test_pydantic_field_error_returns_422(client: TestClient, repo: str) -> None:
    """Field-level validation (negative timeout) → FastAPI 422."""
    payload = _flow_payload(repo)
    payload["spec"]["tasks"][0]["timeoutSeconds"] = -1
    resp = client.post("/api/flows", json=payload)
    assert resp.status_code == 422
