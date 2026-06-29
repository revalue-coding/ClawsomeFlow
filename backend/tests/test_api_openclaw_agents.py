"""Tests for /api/openclaw/agents/* — public CRUD + chat."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import paths
from app.config import load_config, save_config
from app.integrations import openclaw_json as oj
from app.main import create_app
from app.models import Flow, FlowRun, RunStatus
from app.scheduler.naming import openclaw_session_id_for_run
from app.services import openclaw_agents as svc_agents


def _has_git() -> bool:
    return shutil.which("git") is not None


def _seed_registered_user_agent_without_db(
    openclaw_home: Path,
    agent_id: str = "json-only-agent",
    *,
    write_registry: bool = True,
) -> None:
    workspace = paths.agent_dir(agent_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    payload = json.loads((openclaw_home / "openclaw.json").read_text(encoding="utf-8"))
    payload.setdefault("agents", {}).setdefault("list", []).append({
        "id": agent_id,
        "name": "JSON Only Agent",
        "description": "registered in openclaw.json only",
        "workspace": str(workspace),
        "default": False,
    })
    (openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not write_registry:
        return
    registry = oj.managed_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({"agent_ids": [agent_id]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed_unmanaged_runtime_agent(
    openclaw_home: Path,
    *,
    agent_id: str,
    name: str,
    workspace: Path,
    description: str = "",
) -> None:
    payload = json.loads((openclaw_home / "openclaw.json").read_text(encoding="utf-8"))
    payload.setdefault("agents", {}).setdefault("list", []).append({
        "id": agent_id,
        "name": name,
        "description": description,
        "workspace": str(workspace),
        "default": False,
    })
    (openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@pytest.fixture
def fake_openclaw_home(tmp_path: Path) -> Path:
    oc_home = tmp_path / "openclaw_home"
    oc_home.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(update={
        "openclaw_home": str(oc_home),
        "internal_token_secret": "secret",
        "default_user": "alice",
    })
    save_config(cfg)
    (oc_home / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {}, "list": []},
        "gateway": {"port": 18789, "auth": {"token": "T"}},
    }))
    return oc_home


@pytest.fixture
def client(fake_openclaw_home: Path):
    with TestClient(create_app()) as c:
        yield c


# ─── CRUD ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_only_own_agents(client: TestClient, fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="alice1", name="A"), user="alice")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="bob1", name="B"), user="bob")
    r = client.get("/api/openclaw/agents")
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()["items"]]
    assert ids == ["alice1"]


def test_runtime_status_endpoint_supports_strict_probe_mode(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc_agents,
        "probe_runtime_running_strict",
        lambda **_kw: (False, "cli_missing"),
    )
    monkeypatch.setattr(
        svc_agents,
        "resolve_runtime_gateway_url",
        lambda **_kw: "http://127.0.0.1:18888",
    )
    r = client.get("/api/openclaw/agents/runtime/status?mode=strict")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "running": False,
        "reason": "cli_missing",
        "gatewayUrl": "http://127.0.0.1:18888",
    }


@pytest.mark.asyncio
async def test_list_all_users_query(client: TestClient, fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="a1", name="A"), user="alice")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="b1", name="B"), user="bob")
    r = client.get("/api/openclaw/agents?allUsers=true")
    assert r.status_code == 200
    ids = {a["id"] for a in r.json()["items"]}
    assert ids == {"a1", "b1"}


def test_list_all_users_forbidden_in_server_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.api.openclaw_agents.load_config",
        lambda: load_config().model_copy(update={"deployment_mode": "server"}),
    )
    r = client.get("/api/openclaw/agents?allUsers=true")
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_import_external_agents_creates_csflow_clone_and_triggers_optimization(
    client: TestClient,
    fake_openclaw_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    source_ws = tmp_path / "legacy-src"
    source_ws.mkdir(parents=True, exist_ok=True)
    (source_ws / "notes.md").write_text("# legacy notes\n", encoding="utf-8")
    (source_ws / "AGENTS.md").write_text(
        "# Legacy Agent Rules\n\n- keep old policy\n",
        encoding="utf-8",
    )
    _seed_unmanaged_runtime_agent(
        fake_openclaw_home,
        agent_id="legacy-src",
        name="LegacyName",
        workspace=source_ws,
        description="legacy desc",
    )

    from app.api import openclaw_agents as router_mod
    seen: dict[str, str] = {}

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del model_override, timeout_sec
        seen["agent_id"] = agent_id
        seen["session_key"] = session_key
        seen["message"] = message
        return {"id": "import-opt", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents/import",
        json={"agentIds": ["legacy-src"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requestedCount"] == 1
    assert len(body["imported"]) == 1
    assert body["failed"] == []
    imported = body["imported"][0]
    assert imported["sourceAgentId"] == "legacy-src"
    assert imported["targetAgentId"] == "csflow-legacy-src"
    assert imported["targetAgentName"] == "csflow-LegacyName"
    assert imported["targetTeamId"] == ""
    assert imported["targetTeamName"] == ""

    target_ws = Path(imported["targetWorkspacePath"])
    assert (target_ws / "notes.md").read_text(encoding="utf-8") == "# legacy notes\n"
    agents_text = (target_ws / "AGENTS.md").read_text(encoding="utf-8")
    assert "Shared Rules for ClawsomeFlow Managed Agents" in agents_text
    assert "Legacy Agent Rules" in agents_text
    assert "AGENTS_USER_CUSTOM_SECTION" in agents_text

    # Import flow schedules one post-import optimization turn.
    assert seen["agent_id"] == "csflow-legacy-src"
    assert seen["session_key"] == "user-chat-alice-csflow-legacy-src"
    assert seen["message"] == router_mod._IMPORT_OPTIMIZE_PROMPT


@pytest.mark.asyncio
async def test_import_external_agents_assigns_selected_team(
    client: TestClient,
    fake_openclaw_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    source_ws = tmp_path / "legacy-team-src"
    source_ws.mkdir(parents=True, exist_ok=True)
    _seed_unmanaged_runtime_agent(
        fake_openclaw_home,
        agent_id="legacy-team-src",
        name="Legacy Team Source",
        workspace=source_ws,
    )
    team = client.post("/api/openclaw/agents/teams", json={"name": "导入团队"})
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]

    from app.api import openclaw_agents as router_mod

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del agent_id, session_key, message, model_override, timeout_sec
        return {"id": "ok", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents/import",
        json={"agentIds": ["legacy-team-src"], "teamId": team_id},
    )
    assert r.status_code == 200, r.text
    imported = r.json()["imported"][0]
    assert imported["targetTeamId"] == team_id
    assert imported["targetTeamName"] == "导入团队"


@pytest.mark.asyncio
async def test_import_cancel_keeps_imported_and_stops_remaining(
    client: TestClient,
    fake_openclaw_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling mid-import keeps the already-imported agent and skips the rest."""
    if not _has_git():
        pytest.skip("git not available")
    for aid, nm in [("legacy-a", "A"), ("legacy-b", "B")]:
        ws = tmp_path / aid
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "notes.md").write_text("x\n", encoding="utf-8")
        _seed_unmanaged_runtime_agent(
            fake_openclaw_home, agent_id=aid, name=nm, workspace=ws, description="d"
        )

    from app.api import openclaw_agents as router_mod

    calls = {"n": 0}

    async def _fake_cli_chat_completion(
        *, agent_id: str, session_key: str, message: str,
        model_override: str | None, timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del agent_id, session_key, message, model_override, timeout_sec
        calls["n"] += 1
        # Simulate the user clicking cancel right after the first agent's
        # optimization turn — the loop must stop before the second agent.
        router_mod._request_import_cancellation("batch1")
        return {"id": "opt", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents/import",
        json={"agentIds": ["legacy-a", "legacy-b"], "batchId": "batch1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancelled"] is True
    assert len(body["imported"]) == 1  # first kept
    assert body["imported"][0]["sourceAgentId"] == "legacy-a"
    assert calls["n"] == 1  # second agent never processed
    # Batch op recorded as cancelled so the frontend cancel-verify converges.
    op = client.get("/api/operations/openclaw_import_batch:batch1").json()
    assert op["state"] == "failed"
    assert op["detail"] == "cancelled"
    # Flag cleared after the run (a fresh batch id would never re-match anyway).
    assert router_mod._is_import_cancelled("batch1") is False


def test_cancel_import_endpoint_marks_batch_op_cancelled(client: TestClient) -> None:
    from app.api import openclaw_agents as router_mod
    from app.operations import get_op_registry

    reg = get_op_registry()
    reg.start(op_id="openclaw_import_batch:b2", user="alice", kind="openclaw_import_batch")

    r = client.post("/api/openclaw/agents/import/b2/cancel")
    assert r.status_code == 202, r.text
    assert router_mod._is_import_cancelled("b2") is True
    op = client.get("/api/operations/openclaw_import_batch:b2").json()
    assert op["state"] == "failed"
    assert op["detail"] == "cancelled"


@pytest.mark.asyncio
async def test_get_returns_full_detail(client: TestClient, fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="full", name="F", description="d", nl_prompt="P"),
        user="alice",
    )
    r = client.get("/api/openclaw/agents/full")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "full"
    assert body["nlPrompt"] == "P"
    assert "openclawConfigSnapshot" in body


@pytest.mark.asyncio
async def test_get_other_user_forbidden(
    client: TestClient, fake_openclaw_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="bob-owned", name="B"),
        user="bob",
    )
    monkeypatch.setenv("CSFLOW_USER", "alice")
    r = client.get("/api/openclaw/agents/bob-owned")
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


def test_get_missing_404(client: TestClient, fake_openclaw_home: Path) -> None:
    r = client.get("/api/openclaw/agents/nope")
    assert r.status_code == 404
    assert r.json()["error"] == "OPENCLAW_AGENT_NOT_FOUND"


def test_list_includes_registered_managed_agent_without_db_row(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    _seed_registered_user_agent_without_db(fake_openclaw_home, "json-only-agent")
    r = client.get("/api/openclaw/agents")
    assert r.status_code == 200, r.text
    ids = [a["id"] for a in r.json()["items"]]
    assert "json-only-agent" in ids


def test_get_registered_agent_without_db_row_even_without_registry(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    _seed_registered_user_agent_without_db(
        fake_openclaw_home,
        "json-no-registry",
        write_registry=False,
    )
    r = client.get("/api/openclaw/agents/json-no-registry")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "json-no-registry"
    assert oj.has_managed_agent("json-no-registry")


@pytest.mark.asyncio
async def test_create_commits_agent_via_public_endpoint(
    client: TestClient,
    fake_openclaw_home: Path,
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
        del agent_id, session_key, message, model_override, timeout_sec
        return {"id": "bootstrap", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_wait_until_gateway_agent_ready", _fake_wait_until_gateway_agent_ready)
    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents",
        json={
            "id": "direct-create",
            "name": "Direct",
            "description": "created via public endpoint",
            "identityEmoji": "🧭",
            "identityTheme": "travel advisor",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "direct-create"
    assert body["name"] == "Direct"
    assert body["description"] == "created via public endpoint"
    assert body["workspacePath"].endswith("/direct-create/workspace")

    got = client.get("/api/openclaw/agents/direct-create")
    assert got.status_code == 200, got.text
    assert got.json()["id"] == "direct-create"


@pytest.mark.asyncio
async def test_create_assigns_selected_team(
    client: TestClient,
    fake_openclaw_home: Path,
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
        del agent_id, session_key, message, model_override, timeout_sec
        return {"id": "bootstrap", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_wait_until_gateway_agent_ready", _fake_wait_until_gateway_agent_ready)
    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    team = client.post("/api/openclaw/agents/teams", json={"name": "团队A"})
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]

    created = client.post(
        "/api/openclaw/agents",
        json={"id": "team-create", "name": "WithTeam", "teamId": team_id},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["teamId"] == team_id
    assert body["teamName"] == "团队A"


def test_create_rejects_agent_id_with_whitespace(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    r = client.post(
        "/api/openclaw/agents",
        json={
            "id": "bad id",
            "name": "NoSpaceName",
            "description": "created via public endpoint",
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "INVALID_PAYLOAD"
    assert "id" in (r.json().get("message") or "").lower()


@pytest.mark.asyncio
async def test_create_allows_agent_name_with_whitespace(
    client: TestClient,
    fake_openclaw_home: Path,
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
        del agent_id, session_key, message, model_override, timeout_sec
        return {"id": "bootstrap", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_wait_until_gateway_agent_ready", _fake_wait_until_gateway_agent_ready)
    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents",
        json={
            "id": "nospaceid",
            "name": "Bad Name",
            "description": "created via public endpoint",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Bad Name"


@pytest.mark.asyncio
async def test_create_waits_for_gateway_ready_before_bootstrap(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    from app.api import openclaw_agents as router_mod

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(router_mod, "_should_wait_until_gateway_agent_ready", lambda: True)
    calls: list[tuple[str, str]] = []

    async def _fake_wait_until_gateway_agent_ready(*, agent_id: str) -> None:
        calls.append(("wait", agent_id))

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del session_key, message, model_override, timeout_sec
        calls.append(("chat", agent_id))
        assert calls[0] == ("wait", agent_id)
        return {"id": "bootstrap", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_wait_until_gateway_agent_ready", _fake_wait_until_gateway_agent_ready)
    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    created = client.post(
        "/api/openclaw/agents",
        json={"id": "create-wait-order", "name": "CreateWaitOrder"},
    )
    assert created.status_code == 201, created.text
    assert calls[:2] == [
        ("wait", "create-wait-order"),
        ("chat", "create-wait-order"),
    ]


@pytest.mark.asyncio
async def test_create_skips_gateway_ready_probe_on_non_macos(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    from app.api import openclaw_agents as router_mod

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(router_mod, "_should_wait_until_gateway_agent_ready", lambda: False)
    calls: list[tuple[str, str]] = []

    async def _fake_wait_until_gateway_agent_ready(*, agent_id: str) -> None:
        raise AssertionError(f"unexpected wait call for {agent_id}")

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del session_key, message, model_override, timeout_sec
        calls.append(("chat", agent_id))
        return {"id": "bootstrap", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_wait_until_gateway_agent_ready", _fake_wait_until_gateway_agent_ready)
    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    created = client.post(
        "/api/openclaw/agents",
        json={"id": "create-no-ready-wait", "name": "CreateNoReadyWait"},
    )
    assert created.status_code == 201, created.text
    assert calls == [("chat", "create-no-ready-wait")]


@pytest.mark.asyncio
async def test_patch_updates_fields(client: TestClient, fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="up", name="Old"), user="alice")
    r = client.patch(
        "/api/openclaw/agents/up",
        json={"name": "New", "identityEmoji": "🚀", "model": "poe/GPT-5.4"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "New"


@pytest.mark.asyncio
async def test_patch_allows_name_with_whitespace(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="patch-name-space", name="PatchName"), user="alice")
    patched = client.patch(
        "/api/openclaw/agents/patch-name-space",
        json={"name": "Bad Name"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Bad Name"


@pytest.mark.asyncio
async def test_patch_other_user_forbidden(
    client: TestClient, fake_openclaw_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="other-up", name="Old"),
        user="bob",
    )
    monkeypatch.setenv("CSFLOW_USER", "alice")
    r = client.patch(
        "/api/openclaw/agents/other-up",
        json={"name": "Blocked"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_delete_removes_agent(client: TestClient, fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="rm", name="X"), user="alice")
    r = client.delete("/api/openclaw/agents/rm")
    assert r.status_code == 204
    r2 = client.get("/api/openclaw/agents/rm")
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_delete_purge_removes_agent(client: TestClient, fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="rm-purge", name="X"), user="alice")
    r = client.delete("/api/openclaw/agents/rm-purge?mode=purge")
    assert r.status_code == 204
    r2 = client.get("/api/openclaw/agents/rm-purge")
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_delete_purge_unregistered_agent(client: TestClient, fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="rm-unreg-purge", name="X"),
        user="alice",
    )
    unreg = client.delete("/api/openclaw/agents/rm-unreg-purge?mode=unregister")
    assert unreg.status_code == 204, unreg.text
    r = client.delete("/api/openclaw/agents/rm-unreg-purge?mode=purge")
    assert r.status_code == 204, r.text
    assert not paths.agent_dir("rm-unreg-purge").exists()


def test_delete_purge_workspace_orphan_without_db_row(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    orphan_id = "orphan-api-purge"
    workspace = paths.agent_dir(orphan_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "marker.txt").write_text("x", encoding="utf-8")
    r = client.delete(f"/api/openclaw/agents/{orphan_id}?mode=purge")
    assert r.status_code == 204, r.text
    assert not paths.agent_dir(orphan_id).exists()


def test_delete_purge_workspace_orphan_is_idempotent(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    orphan_id = "orphan-api-purge-repeat"
    workspace = paths.agent_dir(orphan_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    first = client.delete(f"/api/openclaw/agents/{orphan_id}?mode=purge")
    second = client.delete(f"/api/openclaw/agents/{orphan_id}?mode=purge")

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    assert not paths.agent_dir(orphan_id).exists()


def test_delete_purge_workspace_orphan_hidden_from_other_user(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan_id = "orphan-api-purge-other-user"
    workspace = paths.agent_dir(orphan_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CSFLOW_USER", "bob")

    r = client.delete(f"/api/openclaw/agents/{orphan_id}?mode=purge")

    assert r.status_code == 404, r.text
    assert r.json()["error"] == "OPENCLAW_AGENT_NOT_FOUND"
    assert paths.agent_dir(orphan_id).exists()


@pytest.mark.asyncio
async def test_cancel_create_endpoint_purges_registered_agent(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="cancel-create-agent", name="CancelCreateAgent"),
        user="alice",
    )
    cancelled = client.post("/api/openclaw/agents/cancel-create-agent/cancel-create")
    assert cancelled.status_code == 202, cancelled.text
    fetched = client.get("/api/openclaw/agents/cancel-create-agent")
    assert fetched.status_code == 404


def test_cancel_create_endpoint_cleans_residual_dir_without_db_row(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    orphan_id = "cancel-create-orphan"
    orphan_workspace = paths.agent_dir(orphan_id) / "workspace"
    orphan_workspace.mkdir(parents=True, exist_ok=True)
    (orphan_workspace / "tmp.txt").write_text("x", encoding="utf-8")
    assert orphan_workspace.exists()
    cancelled = client.post(f"/api/openclaw/agents/{orphan_id}/cancel-create")
    assert cancelled.status_code == 202, cancelled.text
    assert not paths.agent_dir(orphan_id).exists()


def test_create_rejected_while_cancel_pending(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as oc_api

    monkeypatch.setattr(oc_api, "_resolve_openclaw_executable", lambda: "/usr/bin/openclaw")
    oc_api._REQUESTED_AGENT_CREATE_CANCELLATIONS.add("pending-cancel")
    try:
        r = client.post(
            "/api/openclaw/agents",
            json={
                "id": "pending-cancel",
                "name": "Pending Cancel",
                "description": "x",
                "teamId": None,
            },
        )
    finally:
        oc_api._REQUESTED_AGENT_CREATE_CANCELLATIONS.discard("pending-cancel")
    assert r.status_code == 409
    assert r.json()["error"] == "AGENT_CREATE_CANCELLED"


@pytest.mark.asyncio
async def test_delete_other_user_forbidden(
    client: TestClient, fake_openclaw_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="other-rm", name="X"),
        user="bob",
    )
    monkeypatch.setenv("CSFLOW_USER", "alice")
    r = client.delete("/api/openclaw/agents/other-rm")
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_delete_in_use_409(
    client: TestClient, fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="busy", name="X"), user="alice")
    from app.storage import get_storage
    storage = get_storage()
    flow = storage.flow_create(
        Flow(
            id="flow-api-busy",
            name="Flow API Busy",
            owner_user="alice",
            spec={
                "agents": [{"id": "busy", "kind": "openclaw"}],
                "tasks": [],
            },
        )
    )
    storage.run_create(
        FlowRun(
            id="run-api-busy",
            flow_id=flow.id,
            flow_version=flow.version,
            team_name="csflow-api-busy",
            status=RunStatus.running,
            user="alice",
        )
    )
    r = client.delete("/api/openclaw/agents/busy")
    assert r.status_code == 409
    assert r.json()["error"] == "AGENT_IN_USE"
    assert r.json()["details"]["flow_names"] == ["Flow API Busy"]


@pytest.mark.asyncio
async def test_restore_candidates_and_restore_endpoint(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="restore-api", name="Restore API"), user="alice")
    rm = client.delete("/api/openclaw/agents/restore-api?mode=unregister")
    assert rm.status_code == 204, rm.text

    listed = client.get("/api/openclaw/agents")
    assert listed.status_code == 200, listed.text
    listed_ids = {item["id"] for item in listed.json()["items"]}
    assert "restore-api" not in listed_ids

    candidates = client.get("/api/openclaw/agents/restore/candidates")
    assert candidates.status_code == 200, candidates.text
    candidate_ids = {item["id"] for item in candidates.json()["items"]}
    assert "restore-api" in candidate_ids

    restored = client.post("/api/openclaw/agents/restore/restore-api")
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == "restore-api"


# ─── chat (non-streaming) -----------------------------------------------


def test_chat_session_key_is_isolated_from_flow_dispatch_channel() -> None:
    from app.api import openclaw_agents as router_mod

    chat_session = router_mod._session_key("alice", "agent-1")
    flow_session = openclaw_session_id_for_run("csflow-a1b2c3d4", "agent-1")
    assert chat_session.startswith("user-chat-")
    assert chat_session != flow_session


@pytest.mark.asyncio
async def test_chat_non_stream_returns_chat_payload(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat1", name="C"), user="alice")
    captured: dict[str, Any] = {}

    from app.api import openclaw_agents as router_mod

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del timeout_sec
        captured.update(
            {
                "agent_id": agent_id,
                "session_key": session_key,
                "message": message,
                "model_override": model_override,
            }
        )
        return {"id": "x", "choices": [{"message": {"content": "hi user"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents/chat1/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi user"
    assert captured["agent_id"] == "chat1"
    assert captured["message"] == "hi"


@pytest.mark.asyncio
async def test_chat_cli_timeout_has_30min_floor(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat-timeout", name="C"), user="alice")
    captured: dict[str, Any] = {}

    from app.api import openclaw_agents as router_mod

    monkeypatch.setenv("CSFLOW_OPENCLAW_CLI_TIMEOUT_SECONDS", "480")

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del agent_id, session_key, message, model_override
        captured["timeout_sec"] = timeout_sec
        return {"id": "x", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents/chat-timeout/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 200
    assert captured["timeout_sec"] == 1800.0


@pytest.mark.asyncio
async def test_chat_cli_auto_repairs_pending_scope_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    class _Proc:
        def __init__(self, *, returncode: int, stdout: str, stderr: str):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")

        def kill(self):
            return None

    procs = [
        _Proc(
            returncode=1,
            stdout="",
            stderr="scope upgrade pending approval; requestId=abc",
        ),
        _Proc(
            returncode=0,
            stdout=json.dumps(
                {"runId": "run-1", "result": {"payloads": [{"text": "ok"}]}},
                ensure_ascii=False,
            ),
            stderr="",
        ),
    ]
    calls = {"spawn": 0, "repair": 0}
    seen_argv: list[tuple[Any, ...]] = []

    async def _fake_spawn(*argv, **kwargs):
        del kwargs
        seen_argv.append(argv)
        idx = calls["spawn"]
        calls["spawn"] += 1
        return procs[idx]

    def _fake_repair_pending():
        calls["repair"] += 1
        return ["abc"]

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(router_mod, "repair_pending_scope_upgrades", _fake_repair_pending)
    monkeypatch.setattr(router_mod.asyncio, "create_subprocess_exec", _fake_spawn)

    completion = await router_mod._chat_completion_via_cli(
        agent_id="agent-a",
        session_key="sess-a",
        message="hello",
        model_override=None,
        timeout_sec=30.0,
    )
    assert completion["choices"][0]["message"]["content"] == "ok"
    assert calls["repair"] == 1
    assert calls["spawn"] == 2
    assert "--timeout" in seen_argv[0]
    timeout_idx = seen_argv[0].index("--timeout")
    assert seen_argv[0][timeout_idx + 1] == "30"


@pytest.mark.asyncio
async def test_chat_cli_auto_repairs_missing_operator_admin_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    class _Proc:
        def __init__(self, *, returncode: int, stdout: str, stderr: str):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")

        def kill(self):
            return None

    procs = [
        _Proc(
            returncode=1,
            stdout="",
            stderr="GatewayClientRequestError: missing scope: operator.admin",
        ),
        _Proc(
            returncode=0,
            stdout=json.dumps(
                {"runId": "run-1", "result": {"payloads": [{"text": "ok"}]}},
                ensure_ascii=False,
            ),
            stderr="",
        ),
    ]
    calls = {"spawn": 0, "repair": 0}

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        idx = calls["spawn"]
        calls["spawn"] += 1
        return procs[idx]

    def _fake_repair_pending():
        calls["repair"] += 1
        return ["req-op-admin"]

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(router_mod, "repair_pending_scope_upgrades", _fake_repair_pending)
    monkeypatch.setattr(router_mod.asyncio, "create_subprocess_exec", _fake_spawn)

    completion = await router_mod._chat_completion_via_cli(
        agent_id="agent-a",
        session_key="sess-a",
        message="/reset",
        model_override=None,
        timeout_sec=30.0,
    )
    assert completion["choices"][0]["message"]["content"] == "ok"
    assert calls["repair"] == 1
    assert calls["spawn"] == 2


@pytest.mark.asyncio
async def test_chat_cli_scope_repair_retry_failure_surfaces_final_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    class _Proc:
        def __init__(self, *, returncode: int, stdout: str, stderr: str):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")

        def kill(self):
            return None

    procs = [
        _Proc(
            returncode=1,
            stdout="",
            stderr="scope upgrade pending approval; requestId=req-old",
        ),
        _Proc(
            returncode=1,
            stdout="",
            stderr="GatewayClientRequestError: missing scope: operator.admin",
        ),
    ]
    calls = {"spawn": 0, "repair": 0}

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        idx = calls["spawn"]
        calls["spawn"] += 1
        return procs[idx]

    def _fake_repair_pending():
        calls["repair"] += 1
        return []

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(router_mod, "repair_pending_scope_upgrades", _fake_repair_pending)
    monkeypatch.setattr(router_mod.asyncio, "create_subprocess_exec", _fake_spawn)

    with pytest.raises(router_mod.ApiError) as exc_info:
        await router_mod._chat_completion_via_cli(
            agent_id="agent-a",
            session_key="sess-a",
            message="/reset",
            model_override=None,
            timeout_sec=30.0,
        )
    assert exc_info.value.code == "OPENCLAW_CLI_FAILED"
    assert "missing scope: operator.admin" in exc_info.value.message.lower()
    assert calls["repair"] == 1
    assert calls["spawn"] == 2


@pytest.mark.asyncio
async def test_chat_cli_retries_unknown_agent_during_bootstrap_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    class _Proc:
        def __init__(self, *, returncode: int, stdout: str, stderr: str):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")

        def kill(self):
            return None

    procs = [
        _Proc(
            returncode=1,
            stdout="",
            stderr='GatewayClientRequestError: invalid agent params: unknown agent id "xinli"',
        ),
        _Proc(
            returncode=0,
            stdout=json.dumps(
                {"runId": "run-2", "result": {"payloads": [{"text": "bootstrap-ok"}]}},
                ensure_ascii=False,
            ),
            stderr="",
        ),
    ]
    calls = {"spawn": 0}
    sleep_calls: list[float] = []

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        idx = calls["spawn"]
        calls["spawn"] += 1
        return procs[idx]

    async def _fake_sleep(delay: float):
        sleep_calls.append(delay)

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(router_mod.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(router_mod.asyncio, "sleep", _fake_sleep)

    completion = await router_mod._chat_completion_via_cli(
        agent_id="xinli",
        session_key="user-chat-alice-xinli-bootstrap-123",
        message="bootstrap prompt",
        model_override=None,
        timeout_sec=30.0,
    )
    assert completion["choices"][0]["message"]["content"] == "bootstrap-ok"
    assert calls["spawn"] == 2
    assert sleep_calls == [router_mod._NEW_AGENT_GATEWAY_RETRY_DELAYS_SEC[0]]


@pytest.mark.asyncio
async def test_chat_cli_unknown_agent_not_retried_for_regular_chat_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    class _Proc:
        def __init__(self, *, returncode: int, stdout: str, stderr: str):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")

        def kill(self):
            return None

    calls = {"spawn": 0}
    sleep_calls: list[float] = []

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        calls["spawn"] += 1
        return _Proc(
            returncode=1,
            stdout="",
            stderr='GatewayClientRequestError: invalid agent params: unknown agent id "xinli"',
        )

    async def _fake_sleep(delay: float):
        sleep_calls.append(delay)

    monkeypatch.setattr(router_mod, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(router_mod.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(router_mod.asyncio, "sleep", _fake_sleep)

    with pytest.raises(router_mod.ApiError) as exc_info:
        await router_mod._chat_completion_via_cli(
            agent_id="xinli",
            session_key="user-chat-alice-xinli",
            message="hello",
            model_override=None,
            timeout_sec=30.0,
        )
    assert exc_info.value.code == "OPENCLAW_CLI_FAILED"
    assert "unknown agent id" in exc_info.value.message.lower()
    assert calls["spawn"] == 1
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_wait_until_gateway_agent_ready_requires_consecutive_successes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    probes = [
        (False, "not yet"),
        (True, ""),
        (False, "flapping"),
        (True, ""),
        (True, ""),
    ]
    calls: list[str] = []
    sleep_calls: list[float] = []

    def _fake_probe_once(*, agent_id: str, probe_timeout_sec: float | None = None) -> tuple[bool, str]:
        del probe_timeout_sec
        calls.append(agent_id)
        return probes.pop(0)

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _fake_sleep(delay: float):
        sleep_calls.append(delay)

    monkeypatch.setattr(router_mod, "_agent_ready_timeout_seconds", lambda: 30.0)
    monkeypatch.setattr(router_mod, "_probe_gateway_agent_ready_once", _fake_probe_once)
    monkeypatch.setattr(router_mod.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(router_mod.asyncio, "sleep", _fake_sleep)

    await router_mod._wait_until_gateway_agent_ready(agent_id="probe-agent")
    assert calls == ["probe-agent"] * 5
    assert len(sleep_calls) == 4


@pytest.mark.asyncio
async def test_wait_until_gateway_agent_ready_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    def _fake_probe_once(*, agent_id: str, probe_timeout_sec: float | None = None) -> tuple[bool, str]:
        del agent_id, probe_timeout_sec
        return False, "agent missing in gateway list"

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(router_mod, "_agent_ready_timeout_seconds", lambda: 0.02)
    monkeypatch.setattr(router_mod, "_OPENCLAW_AGENT_READY_POLL_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(router_mod, "_probe_gateway_agent_ready_once", _fake_probe_once)
    monkeypatch.setattr(router_mod.asyncio, "to_thread", _fake_to_thread)

    with pytest.raises(router_mod.ApiError) as exc_info:
        await router_mod._wait_until_gateway_agent_ready(agent_id="timeout-agent")
    assert exc_info.value.code == "OPENCLAW_GATEWAY_AGENT_NOT_READY"
    assert "timeout-agent" in exc_info.value.message


@pytest.mark.asyncio
async def test_cancel_request_before_event_registration_still_cancels() -> None:
    from app.api import openclaw_agents as router_mod

    agent_id = "cancel-pre-register"
    router_mod._clear_agent_create_cancellation_request(agent_id)
    cancel_requested = router_mod._request_agent_create_cancellation(agent_id)
    assert cancel_requested is False

    event = router_mod._register_agent_create_cancellation(agent_id)
    assert event.is_set()

    router_mod._unregister_agent_create_cancellation(agent_id=agent_id, event=event)
    router_mod._clear_agent_create_cancellation_request(agent_id)


def test_agent_ready_timeout_seconds_clamped_to_15s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import openclaw_agents as router_mod

    monkeypatch.setenv(router_mod._OPENCLAW_AGENT_READY_TIMEOUT_ENV, "999")
    assert router_mod._agent_ready_timeout_seconds() == 15.0


def test_chat_404_for_missing_agent(client: TestClient) -> None:
    r = client.post(
        "/api/openclaw/agents/nope/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_chat_other_user_forbidden(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat-bob", name="C"), user="bob")
    monkeypatch.setenv("CSFLOW_USER", "alice")
    r = client.post(
        "/api/openclaw/agents/chat-bob/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_chat_only_forwards_latest_user_turn(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat-last-user", name="C"), user="alice")
    captured: dict[str, Any] = {}

    from app.api import openclaw_agents as router_mod

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del timeout_sec
        captured.update(
            {
                "agent_id": agent_id,
                "session_key": session_key,
                "message": message,
                "model_override": model_override,
            }
        )
        return {"id": "x", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    r = client.post(
        "/api/openclaw/agents/chat-last-user/chat",
        json={
            "messages": [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "old reply"},
                {"role": "user", "content": "new turn"},
            ],
            "stream": False,
        },
    )
    assert r.status_code == 200
    assert captured["agent_id"] == "chat-last-user"
    assert captured["message"] == "new turn"


@pytest.mark.asyncio
async def test_chat_history_returns_cached_messages(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat-history-api", name="C"), user="alice")

    from app.api import openclaw_agents as router_mod

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del agent_id, session_key, message, model_override, timeout_sec
        return {"id": "x", "choices": [{"message": {"content": "cached-reply"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)
    run = client.post(
        "/api/openclaw/agents/chat-history-api/chat",
        json={"messages": [{"role": "user", "content": "cached-turn"}], "stream": False},
    )
    assert run.status_code == 200

    hist = client.get("/api/openclaw/agents/chat-history-api/chat-history")
    assert hist.status_code == 200, hist.text
    assert hist.json()["messages"] == [
        {"role": "user", "content": "cached-turn"},
        {"role": "assistant", "content": "cached-reply"},
    ]


@pytest.mark.asyncio
async def test_chat_history_marks_no_text_reply(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat-empty-reply", name="C"), user="alice")

    from app.api import openclaw_agents as router_mod

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del agent_id, session_key, message, model_override, timeout_sec
        return {"id": "x", "choices": [{"message": {"content": ""}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)
    run = client.post(
        "/api/openclaw/agents/chat-empty-reply/chat",
        json={"messages": [{"role": "user", "content": "do-it"}], "stream": False},
    )
    assert run.status_code == 200

    hist = client.get("/api/openclaw/agents/chat-empty-reply/chat-history")
    assert hist.status_code == 200, hist.text
    assert hist.json()["messages"] == [
        {"role": "user", "content": "do-it"},
        {"role": "assistant", "content": "[[NO_TEXT_REPLY]]"},
    ]


@pytest.mark.asyncio
async def test_chat_attachment_upload_and_path_injection(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat-attach-path", name="C"), user="alice")
    from app.api import openclaw_agents as router_mod

    captured: dict[str, Any] = {}

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        attachment_paths: list[str] | None = None,
        native_attachment_flag: str | None = None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del agent_id, session_key, model_override, timeout_sec
        captured["message"] = message
        captured["attachment_paths"] = attachment_paths
        captured["native_attachment_flag"] = native_attachment_flag
        return {"id": "x", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)
    uploaded = client.post(
        "/api/openclaw/agents/chat-attach-path/chat/attachments?filename=brief.md",
        data=b"# Brief\n",
        headers={"Content-Type": "text/markdown"},
    )
    assert uploaded.status_code == 200, uploaded.text
    uploaded_body = uploaded.json()
    assert uploaded_body["attachment"]["route"] == "path_injection"
    attachment = uploaded_body["attachment"]
    run = client.post(
        "/api/openclaw/agents/chat-attach-path/chat",
        json={
            "messages": [{"role": "user", "content": "please read files"}],
            "attachments": [attachment],
            "stream": False,
        },
    )
    assert run.status_code == 200, run.text
    assert "ClawsomeFlow Uploaded Attachments" in captured["message"]
    assert captured["attachment_paths"] is None
    assert captured["native_attachment_flag"] is None
    hist = client.get("/api/openclaw/agents/chat-attach-path/chat-history")
    assert hist.status_code == 200, hist.text
    first = hist.json()["messages"][0]
    assert first["role"] == "user"
    assert first["attachments"][0]["name"] == "brief.md"


@pytest.mark.asyncio
async def test_reset_session_sends_plain_slash_reset_only(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(svc_agents.CommitInput(id="chat-reset", name="R"), user="alice")
    calls: list[dict[str, Any]] = []

    from app.api import openclaw_agents as router_mod

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del timeout_sec
        calls.append(
            {
                "agent_id": agent_id,
                "session_key": session_key,
                "message": message,
                "model_override": model_override,
            }
        )
        return {"id": "reset", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)
    warmup = client.post(
        "/api/openclaw/agents/chat-reset/chat",
        json={"messages": [{"role": "user", "content": "before-reset"}], "stream": False},
    )
    assert warmup.status_code == 200
    pre_hist = client.get("/api/openclaw/agents/chat-reset/chat-history")
    assert pre_hist.status_code == 200
    assert pre_hist.json()["messages"] == [
        {"role": "user", "content": "before-reset"},
        {"role": "assistant", "content": "ok"},
    ]
    r = client.post("/api/openclaw/agents/chat-reset/reset")
    assert r.status_code == 204, r.text
    assert calls[-1]["message"] == "/reset"
    assert calls[-1]["session_key"] == "user-chat-alice-chat-reset"
    post_hist = client.get("/api/openclaw/agents/chat-reset/chat-history")
    assert post_hist.status_code == 200
    assert post_hist.json()["messages"] == []


@pytest.mark.asyncio
async def test_reset_session_fallback_rotates_session_on_missing_operator_scope(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="chat-reset-fallback", name="RF"),
        user="alice",
    )
    calls: list[dict[str, Any]] = []

    from app.api import openclaw_agents as router_mod

    router_mod._CHAT_SESSION_REVISIONS.clear()

    async def _fake_cli_chat_completion(
        *,
        agent_id: str,
        session_key: str,
        message: str,
        model_override: str | None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        del timeout_sec
        calls.append(
            {
                "agent_id": agent_id,
                "session_key": session_key,
                "message": message,
                "model_override": model_override,
            }
        )
        if message == "/reset":
            raise router_mod.ApiError(
                "OPENCLAW_CLI_FAILED",
                "openclaw agent invocation failed: GatewayClientRequestError: missing scope: operator.admin",
                status_code=502,
            )
        return {"id": "ok", "choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router_mod, "_chat_completion_via_cli", _fake_cli_chat_completion)

    warmup = client.post(
        "/api/openclaw/agents/chat-reset-fallback/chat",
        json={"messages": [{"role": "user", "content": "before"}], "stream": False},
    )
    assert warmup.status_code == 200

    reset = client.post("/api/openclaw/agents/chat-reset-fallback/reset")
    assert reset.status_code == 204, reset.text

    after = client.post(
        "/api/openclaw/agents/chat-reset-fallback/chat",
        json={"messages": [{"role": "user", "content": "after"}], "stream": False},
    )
    assert after.status_code == 200

    reset_call = next((c for c in calls if c["message"] == "/reset"), None)
    assert reset_call is not None
    assert reset_call["session_key"] == "user-chat-alice-chat-reset-fallback"

    last_chat_call = calls[-1]
    assert last_chat_call["message"] == "after"
    assert last_chat_call["session_key"] == "user-chat-alice-chat-reset-fallback-r1"


def test_parse_json_object_from_mixed_output_accepts_mixed_stream() -> None:
    from app.api import openclaw_agents as router_mod

    mixed = (
        "warn: loading hooks cache\n"
        "\x1b[33mdebug\x1b[0m\n"
        '{"workspaceDir":"/tmp/demo","hooks":[]}\n'
        "note: fallback enabled"
    )
    parsed = router_mod._parse_json_object_from_mixed_output(mixed)
    assert parsed["workspaceDir"] == "/tmp/demo"
    assert parsed["hooks"] == []


@pytest.mark.asyncio
async def test_settings_snapshot_includes_skills_cron_hooks_and_custom_section(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="settings-api", name="Settings API"),
        user="alice",
    )
    row = svc_agents.get_agent("settings-api")
    workspace = Path(row.workspace_path)

    skill_dir = workspace / "skills" / "custom-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: custom-skill\n"
        "description: \"custom description\"\n"
        "---\n\n"
        "## custom content\n",
        encoding="utf-8",
    )

    hook_dir = workspace / "hooks" / "custom-hook"
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "HOOK.md").write_text("# custom hook\n", encoding="utf-8")
    (hook_dir / "handler.ts").write_text("export default async function handler() {}\n", encoding="utf-8")

    from app.api import openclaw_agents as router_mod

    calls: list[list[str]] = []

    def _fake_run_openclaw_cli(*, args, cwd=None, expect_json=False, timeout_sec=30.0):
        del cwd, expect_json, timeout_sec
        calls.append(list(args))
        if args[:2] == ["cron", "list"]:
            return {
                "jobs": [
                    {
                        "id": "cron-built-in",
                        "agentId": "settings-api",
                        "name": "csflow-entropy-management-settings-api",
                        "enabled": True,
                        "schedule": {"expr": "0 3 1 * *", "tz": "UTC"},
                        "payload": {"message": "builtin"},
                        "source": "system",
                    },
                    {
                        "id": "cron-custom",
                        "agentId": "settings-api",
                        "name": "weekly-review",
                        "enabled": False,
                        "schedule": {"expr": "0 9 * * 1", "tz": "UTC"},
                        "payload": {"message": "review workspace"},
                        "source": "user",
                    },
                ]
            }
        if args[:3] == ["hooks", "list", "--json"]:
            return {
                "hooks": [
                    {
                        "name": "boot-md",
                        "description": "boot",
                        "source": "openclaw-bundled",
                        "events": ["gateway:startup"],
                        "disabled": False,
                        "eligible": True,
                        "requirementsSatisfied": True,
                        "managedByPlugin": False,
                    }
                ]
            }
        raise AssertionError(f"unexpected openclaw call: {args}")

    monkeypatch.setattr(router_mod, "_run_openclaw_cli", _fake_run_openclaw_cli)

    r = client.get("/api/openclaw/agents/settings-api/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "settings-api"
    skill = next(item for item in body["skills"] if item["name"] == "custom-skill")
    assert skill["description"] == "custom description"
    assert "custom content" in skill["content"]

    cron_by_name = {item["name"]: item for item in body["cronJobs"]}
    assert cron_by_name["csflow-entropy-management-settings-api"]["canEdit"] is False
    assert cron_by_name["csflow-entropy-management-settings-api"]["canDelete"] is False
    assert cron_by_name["weekly-review"]["canEdit"] is True
    assert cron_by_name["weekly-review"]["enabled"] is False

    hooks_by_name = {item["name"]: item for item in body["hooks"]}
    assert hooks_by_name["boot-md"]["systemBuiltin"] is True
    assert hooks_by_name["custom-hook"]["systemBuiltin"] is False
    assert "custom hook" in hooks_by_name["custom-hook"]["hookMd"]
    assert body["agentsUserCustomSection"]

    skills_only = client.get("/api/openclaw/agents/settings-api/settings/skills")
    assert skills_only.status_code == 200, skills_only.text
    assert any(item["name"] == "custom-skill" for item in skills_only.json())

    cron_only = client.get("/api/openclaw/agents/settings-api/settings/cron")
    assert cron_only.status_code == 200, cron_only.text
    cron_only_by_name = {item["name"]: item for item in cron_only.json()}
    assert cron_only_by_name["csflow-entropy-management-settings-api"]["canEdit"] is False
    assert cron_only_by_name["weekly-review"]["canEdit"] is True

    hooks_only = client.get("/api/openclaw/agents/settings-api/settings/hooks")
    assert hooks_only.status_code == 200, hooks_only.text
    hooks_only_by_name = {item["name"]: item for item in hooks_only.json()}
    assert hooks_only_by_name["boot-md"]["systemBuiltin"] is True
    assert hooks_only_by_name["custom-hook"]["systemBuiltin"] is False

    custom_only = client.get("/api/openclaw/agents/settings-api/settings/agents-custom-section")
    assert custom_only.status_code == 200, custom_only.text
    assert custom_only.json()["content"]
    cron_list_calls = [call for call in calls if call[:2] == ["cron", "list"]]
    assert cron_list_calls
    assert all("--agent" in call for call in cron_list_calls)
    assert all(call[call.index("--agent") + 1] == "settings-api" for call in cron_list_calls)


@pytest.mark.asyncio
async def test_settings_snapshot_falls_back_when_cli_payload_is_not_parseable(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="settings-fallback", name="Settings Fallback"),
        user="alice",
    )
    row = svc_agents.get_agent("settings-fallback")
    workspace = Path(row.workspace_path)

    skill_dir = workspace / "skills" / "custom-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# custom skill\n", encoding="utf-8")
    hook_dir = workspace / "hooks" / "custom-hook"
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "HOOK.md").write_text("# custom hook\n", encoding="utf-8")
    (hook_dir / "handler.ts").write_text("export default async function handler() {}\n", encoding="utf-8")

    from app.api import openclaw_agents as router_mod

    calls: list[list[str]] = []

    def _fake_run_openclaw_cli(*, args, cwd=None, expect_json=False, timeout_sec=30.0):
        del cwd, expect_json, timeout_sec
        calls.append(list(args))
        if args[:2] == ["cron", "list"] or args[:3] == ["hooks", "list", "--json"]:
            raise router_mod.ApiError(
                "OPENCLAW_CLI_BAD_OUTPUT",
                "openclaw command returned non-JSON payload: garbage",
                status_code=502,
            )
        raise AssertionError(f"unexpected openclaw call: {args}")

    monkeypatch.setattr(router_mod, "_run_openclaw_cli", _fake_run_openclaw_cli)

    r = client.get("/api/openclaw/agents/settings-fallback/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "settings-fallback"
    assert body["cronJobs"] == []
    hooks_by_name = {item["name"]: item for item in body["hooks"]}
    assert hooks_by_name["custom-hook"]["systemBuiltin"] is False
    assert "custom hook" in hooks_by_name["custom-hook"]["hookMd"]
    assert any(item["name"] == "custom-skill" for item in body["skills"])

    cron_only = client.get("/api/openclaw/agents/settings-fallback/settings/cron")
    assert cron_only.status_code == 200, cron_only.text
    assert cron_only.json() == []

    hooks_only = client.get("/api/openclaw/agents/settings-fallback/settings/hooks")
    assert hooks_only.status_code == 200, hooks_only.text
    hooks_only_by_name = {item["name"]: item for item in hooks_only.json()}
    assert hooks_only_by_name["custom-hook"]["systemBuiltin"] is False
    cron_list_calls = [call for call in calls if call[:2] == ["cron", "list"]]
    assert cron_list_calls
    assert all("--agent" in call for call in cron_list_calls)
    assert all(call[call.index("--agent") + 1] == "settings-fallback" for call in cron_list_calls)


@pytest.mark.asyncio
async def test_create_skill_generates_frontmatter_and_returns_plain_content(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="settings-skill-create", name="Settings Skill Create"),
        user="alice",
    )
    created = client.post(
        "/api/openclaw/agents/settings-skill-create/settings/skills",
        json={
            "name": "market-analyst",
            "description": "Analyze market trends",
            "content": "## workflow\n\n- step 1",
        },
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["name"] == "market-analyst"
    assert payload["description"] == "Analyze market trends"
    assert "step 1" in payload["content"]

    skill_text = Path(payload["path"]).read_text(encoding="utf-8")
    assert skill_text.startswith(
        "---\n"
        "name: market-analyst\n"
        "description: \"Analyze market trends\"\n"
        "---\n"
    )
    assert "## workflow" in skill_text


@pytest.mark.asyncio
async def test_create_cron_job_accepts_schedule_fields_and_converts_to_cron(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="settings-cron-create", name="Settings Cron Create"),
        user="alice",
    )
    from app.api import openclaw_agents as router_mod

    calls: list[list[str]] = []

    def _fake_run_openclaw_cli(*, args, cwd=None, expect_json=False, timeout_sec=30.0):
        del cwd, expect_json, timeout_sec
        calls.append(list(args))
        if args[:2] == ["cron", "list"]:
            return {"jobs": []}
        if args[:2] == ["cron", "add"]:
            return {"job": {"id": "job-created"}}
        if args[:2] == ["cron", "get"]:
            return {
                "id": "job-created",
                "agentId": "settings-cron-create",
                "name": "weekly-review",
                "enabled": True,
                "schedule": {"expr": "15 9 * * 1", "tz": "Asia/Shanghai"},
                "payload": {"message": "review workspace"},
                "source": "user",
            }
        raise AssertionError(f"unexpected openclaw call: {args}")

    monkeypatch.setattr(router_mod, "_run_openclaw_cli", _fake_run_openclaw_cli)
    monkeypatch.setattr(router_mod, "_system_timezone_name", lambda: "Asia/Shanghai")

    created = client.post(
        "/api/openclaw/agents/settings-cron-create/settings/cron",
        json={
            "name": "weekly-review",
            "scheduleMode": "weekly",
            "scheduleTime": "09:15",
            "scheduleWeekday": 1,
            "message": "review workspace",
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text

    add_call = next(call for call in calls if call[:2] == ["cron", "add"])
    assert "--cron" in add_call
    assert add_call[add_call.index("--cron") + 1] == "15 9 * * 1"
    assert add_call[add_call.index("--tz") + 1] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_create_cron_job_rejects_duplicate_name(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="settings-cron-duplicate", name="Settings Cron Duplicate"),
        user="alice",
    )
    from app.api import openclaw_agents as router_mod

    calls: list[list[str]] = []

    def _fake_run_openclaw_cli(*, args, cwd=None, expect_json=False, timeout_sec=30.0):
        del cwd, expect_json, timeout_sec
        calls.append(list(args))
        if args[:2] == ["cron", "list"]:
            return {
                "jobs": [
                    {
                        "id": "job-existing",
                        "agentId": "settings-cron-duplicate",
                        "name": "weekly-review",
                    }
                ]
            }
        raise AssertionError(f"unexpected openclaw call: {args}")

    monkeypatch.setattr(router_mod, "_run_openclaw_cli", _fake_run_openclaw_cli)
    monkeypatch.setattr(router_mod, "_system_timezone_name", lambda: "Asia/Shanghai")

    created = client.post(
        "/api/openclaw/agents/settings-cron-duplicate/settings/cron",
        json={
            "name": "weekly-review",
            "scheduleMode": "weekly",
            "scheduleTime": "09:15",
            "scheduleWeekday": 1,
            "message": "review workspace",
            "enabled": True,
        },
    )
    assert created.status_code == 409, created.text
    assert created.json()["error"] == "CRON_JOB_EXISTS"
    assert all(call[:2] != ["cron", "add"] for call in calls)


@pytest.mark.asyncio
async def test_system_cron_job_is_readonly_for_edit_and_delete(
    client: TestClient,
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="settings-readonly", name="Settings Readonly"),
        user="alice",
    )

    from app.api import openclaw_agents as router_mod

    calls: list[list[str]] = []

    def _fake_run_openclaw_cli(*, args, cwd=None, expect_json=False, timeout_sec=30.0):
        del cwd, expect_json, timeout_sec
        calls.append(list(args))
        if args[:2] == ["cron", "get"]:
            return {
                "id": "cron-built-in",
                "agentId": "settings-readonly",
                "name": "csflow-entropy-management-settings-readonly",
                "enabled": True,
                "schedule": {"expr": "0 3 1 * *", "tz": "UTC"},
                "payload": {"message": "builtin"},
                "source": "system",
            }
        raise AssertionError(f"unexpected openclaw call: {args}")

    monkeypatch.setattr(router_mod, "_run_openclaw_cli", _fake_run_openclaw_cli)

    patched = client.patch(
        "/api/openclaw/agents/settings-readonly/settings/cron/cron-built-in",
        json={"name": "rename-not-allowed"},
    )
    assert patched.status_code == 409, patched.text
    assert patched.json()["error"] == "CRON_JOB_READONLY"

    deleted = client.delete(
        "/api/openclaw/agents/settings-readonly/settings/cron/cron-built-in",
    )
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["error"] == "CRON_JOB_READONLY"
    assert all(call[:2] != ["cron", "edit"] for call in calls)
    assert all(call[:2] != ["cron", "rm"] for call in calls)


@pytest.mark.asyncio
async def test_update_agents_custom_section_endpoint(
    client: TestClient,
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc_agents.commit_agent(
        svc_agents.CommitInput(id="settings-custom-section", name="Custom Section"),
        user="alice",
    )
    payload = {"content": "## AGENTS_USER_CUSTOM_SECTION\n\n- custom rule from settings panel"}
    resp = client.put(
        "/api/openclaw/agents/settings-custom-section/settings/agents-custom-section",
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    assert "custom rule from settings panel" in resp.json()["content"]

    row = svc_agents.get_agent("settings-custom-section")
    agents_md = Path(row.workspace_path) / "AGENTS.md"
    text = agents_md.read_text(encoding="utf-8")
    assert "custom rule from settings panel" in text
    assert "AGENTS_USER_CUSTOM_SECTION_START" in text
    assert "AGENTS_USER_CUSTOM_SECTION_END" in text

