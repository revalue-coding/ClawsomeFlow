"""Tests for GET /api/operations/{op_id} — the 4-layer recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import operations as ops
from app.config import load_config, save_config
from app.main import create_app
from app.models import HermesAgent
from app.services import hermes_agents as hermes_svc
from app.storage import get_storage


@pytest.fixture
def client(tmp_path: Path):
    cfg = load_config().model_copy(update={"default_user": "alice"})
    save_config(cfg)
    ops.reset_op_registry()
    with TestClient(create_app()) as c:
        yield c
    ops.reset_op_registry()


def test_registry_running(client) -> None:
    ops.get_op_registry().start(op_id="hermes_create:math", user="alice", kind="hermes_create")
    r = client.get("/api/operations/hermes_create:math")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "running"
    assert body["source"] == "registry"


def test_registry_succeeded_with_result(client) -> None:
    reg = ops.get_op_registry()
    reg.start(op_id="hermes_create:math", user="alice", kind="hermes_create")
    reg.succeed("hermes_create:math", result={"agentId": "math"})
    body = client.get("/api/operations/hermes_create:math").json()
    assert body["state"] == "succeeded"
    assert body["result"] == {"agentId": "math"}


def test_registry_failed(client) -> None:
    reg = ops.get_op_registry()
    reg.start(op_id="hermes_create:math", user="alice", kind="hermes_create")
    reg.fail("hermes_create:math", detail="cancelled")
    body = client.get("/api/operations/hermes_create:math").json()
    assert body["state"] == "failed"
    assert body["detail"] == "cancelled"


def test_entity_exists_fallback(client) -> None:
    # No registry entry, but the agent row exists → succeeded via entity layer.
    storage = get_storage()
    storage.hermes_create(
        HermesAgent(id="math", name="Math", profile_root="/x", created_by_user="alice")
    )
    body = client.get("/api/operations/hermes_create:math").json()
    assert body["state"] == "succeeded"
    assert body["source"] == "entity"


def test_in_flight_fallback(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hermes_svc, "is_create_in_flight", lambda aid: aid == "math")
    body = client.get("/api/operations/hermes_create:math").json()
    assert body["state"] == "running"
    assert body["source"] == "in_flight"
    assert body["inFlight"] is True


def test_not_found_reports_in_flight_false(client) -> None:
    body = client.get("/api/operations/hermes_create:nope").json()
    assert body["state"] == "not_found"
    assert body["inFlight"] is False


def test_registry_running_recovers_when_entity_complete(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models import OpenclawAgent
    from app.services import openclaw_agents as oc_svc

    storage = get_storage()
    storage.openclaw_create(
        OpenclawAgent(
            id="done",
            name="Done",
            workspace_path="/tmp/done-workspace",
            created_by_user="alice",
        )
    )
    monkeypatch.setattr(oc_svc, "is_bootstrap_complete", lambda aid, **_: aid == "done")
    monkeypatch.setattr(oc_svc, "is_create_in_flight", lambda aid: False)
    reg = ops.get_op_registry()
    reg.start(op_id="openclaw_create:done", user="alice", kind="openclaw_create")
    body = client.get("/api/operations/openclaw_create:done").json()
    assert body["state"] == "succeeded"
    assert body["source"] == "entity"
    assert body["inFlight"] is False
    storage.openclaw_delete("done")


def test_openclaw_entity_fallback_requires_bootstrap_complete(client) -> None:
    from app.models import OpenclawAgent
    from app.storage import get_storage

    storage = get_storage()
    storage.openclaw_create(
        OpenclawAgent(
            id="half",
            name="Half",
            workspace_path="/tmp/half-workspace",
            created_by_user="alice",
        )
    )
    body = client.get("/api/operations/openclaw_create:half").json()
    assert body["state"] == "failed"
    assert body["detail"] == "bootstrap_incomplete"
    assert body["source"] == "entity"

    storage.openclaw_delete("half")


def test_wrong_user_op_is_not_found(client, monkeypatch: pytest.MonkeyPatch) -> None:
    ops.get_op_registry().start(op_id="hermes_create:secret", user="bob", kind="hermes_create")
    # Current user is alice → bob's op is invisible → falls through to not_found.
    body = client.get("/api/operations/hermes_create:secret").json()
    assert body["state"] == "not_found"


def test_hermes_create_endpoint_records_op_succeeded(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the (slow) service create so the endpoint just exercises op wiring.
    def _fake_commit(cmd, *, user, storage, outcome=None):  # noqa: ANN001, ANN202
        row = HermesAgent(id=cmd.id, name=cmd.name, profile_root="/x", created_by_user=user)
        return storage.hermes_create(row)

    monkeypatch.setattr(hermes_svc, "commit_agent", _fake_commit)
    r = client.post("/api/hermes/agents", json={"id": "math", "name": "Math"})
    assert r.status_code == 201
    body = client.get("/api/operations/hermes_create:math").json()
    assert body["state"] == "succeeded"
    assert body["source"] == "registry"
    assert body["result"] == {"agentId": "math"}


def test_hermes_create_endpoint_records_op_failed_on_cancel(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(cmd, *, user, storage, outcome=None):  # noqa: ANN001, ANN202
        raise hermes_svc.AgentCreateCancelled("cancelled by user")

    monkeypatch.setattr(hermes_svc, "commit_agent", _boom)
    r = client.post("/api/hermes/agents", json={"id": "math", "name": "Math"})
    assert r.status_code == 409
    body = client.get("/api/operations/hermes_create:math").json()
    assert body["state"] == "failed"
    assert body["detail"] == "cancelled"


async def test_detached_work_records_op_after_request_cancelled() -> None:
    """A client disconnect cancels the request coroutine (which awaits the work
    via ``asyncio.shield``), but the detached task must still run to completion
    and record the op's terminal state — otherwise the recovery UI is stuck
    "running" forever after a tab switch / refresh mid-create."""
    from app.api import hermes_agents as api_hermes

    ops.reset_op_registry()
    reg = ops.get_op_registry()
    reg.start(op_id="hermes_create:slow", user="alice", kind="hermes_create")
    try:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _commit() -> str:
            started.set()
            await release.wait()  # stand in for the slow executor work
            reg.succeed("hermes_create:slow", result={"agentId": "slow"})
            return "slow"

        task = api_hermes._spawn_detached(_commit())
        req = asyncio.ensure_future(asyncio.shield(task))
        await started.wait()

        req.cancel()  # simulate the client disconnecting mid-create
        with pytest.raises(asyncio.CancelledError):
            await req

        # The detached task survived the cancel and is still running.
        assert reg.get("hermes_create:slow", user="alice").state == "running"

        # Once the work finishes, the op transitions despite the dead request.
        release.set()
        assert await task == "slow"
        assert reg.get("hermes_create:slow", user="alice").state == "succeeded"
    finally:
        ops.reset_op_registry()
