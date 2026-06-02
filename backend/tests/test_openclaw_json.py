"""Tests for app.integrations.openclaw_json — the safe editor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import paths
from app.config import Config, load_config, save_config
from app.integrations import openclaw_json as oj


@pytest.fixture
def fake_openclaw_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Config.openclaw_home at a fresh tmp dir with a stub openclaw.json."""
    oc_home = tmp_path / "openclaw_home"
    oc_home.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(update={"openclaw_home": str(oc_home)})
    save_config(cfg)

    seed = {
        "agents": {
            "defaults": {"workspace": str(oc_home / "workspace")},
            "list": [
                {"id": "user-existing", "name": "Pre-existing"},
            ],
        },
        "gateway": {"port": 18789, "auth": {"mode": "token", "token": "TKN"}},
    }
    (oc_home / "openclaw.json").write_text(json.dumps(seed, indent=2))
    return oc_home


def test_load_returns_full_dict(fake_openclaw_home: Path) -> None:
    data = oj.load_openclaw_json()
    assert data["gateway"]["auth"]["token"] == "TKN"
    assert len(data["agents"]["list"]) == 1


def test_load_missing_raises(tmp_path: Path) -> None:
    cfg = load_config()
    cfg = cfg.model_copy(update={"openclaw_home": str(tmp_path / "nope")})
    save_config(cfg)
    with pytest.raises(oj.OpenclawJsonError):
        oj.load_openclaw_json()


def test_load_invalid_json_raises(fake_openclaw_home: Path) -> None:
    (fake_openclaw_home / "openclaw.json").write_text("not json {")
    with pytest.raises(oj.OpenclawJsonError):
        oj.load_openclaw_json()


@pytest.mark.asyncio
async def test_append_managed_agent(fake_openclaw_home: Path) -> None:
    await oj.append_managed_agent({
        "id": "csf-test",
        "name": "Test",
        "description": "should-be-stripped",
        "_managed_by": "legacy",
    })
    data = oj.load_openclaw_json()
    ids = [a["id"] for a in data["agents"]["list"]]
    assert "csf-test" in ids
    assert "user-existing" in ids
    new = next(a for a in data["agents"]["list"] if a["id"] == "csf-test")
    assert "_managed_by" not in new
    assert "description" not in new
    assert "csf-test" in oj.list_managed_agent_ids()
    # backup file should now exist
    assert (fake_openclaw_home / "openclaw.json.bak").exists()


@pytest.mark.asyncio
async def test_append_refuses_duplicate(fake_openclaw_home: Path) -> None:
    await oj.append_managed_agent({"id": "csf-x"})
    with pytest.raises(oj.OpenclawJsonError):
        await oj.append_managed_agent({"id": "csf-x"})


@pytest.mark.asyncio
async def test_remove_managed_agent_succeeds(fake_openclaw_home: Path) -> None:
    await oj.append_managed_agent({"id": "csf-del"})
    assert await oj.remove_managed_agent("csf-del") is True
    assert oj.find_agent("csf-del") is None
    assert "csf-del" not in oj.list_managed_agent_ids()


@pytest.mark.asyncio
async def test_remove_refuses_unmanaged(fake_openclaw_home: Path) -> None:
    """Critical: never delete user agents we didn't create."""
    with pytest.raises(oj.OpenclawJsonError):
        await oj.remove_managed_agent("user-existing")
    assert oj.find_agent("user-existing") is not None


@pytest.mark.asyncio
async def test_remove_all_managed(fake_openclaw_home: Path) -> None:
    await oj.append_managed_agent({"id": "csf-a"})
    await oj.append_managed_agent({"id": "csf-b"})
    removed = await oj.remove_all_managed_agents()
    assert set(removed) == {"csf-a", "csf-b"}
    assert oj.find_agent("user-existing") is not None


@pytest.mark.asyncio
async def test_remove_all_managed_removes_csflow_workspace_entries_without_registry(
    fake_openclaw_home: Path,
) -> None:
    payload = oj.load_openclaw_json()
    payload["agents"]["list"].append(
        {
            "id": "legacy-csflow",
            "name": "Legacy Csflow",
            "workspace": str(
                paths.clawsomeflow_home_path() / "agents" / "legacy-csflow" / "workspace"
            ),
        }
    )
    (fake_openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    removed = await oj.remove_all_managed_agents()
    assert "legacy-csflow" in removed
    assert oj.find_agent("legacy-csflow") is None
    assert oj.find_agent("user-existing") is not None


@pytest.mark.asyncio
async def test_sanitize_managed_agent_entries_strips_legacy_invalid_keys(
    fake_openclaw_home: Path,
) -> None:
    await oj.append_managed_agent({"id": "csf-sanitize", "name": "S"})
    payload = oj.load_openclaw_json()
    for agent in payload["agents"]["list"]:
        if agent.get("id") != "csf-sanitize":
            continue
        agent["description"] = "legacy-field"
        agent["_managed_by"] = "legacy"
    (fake_openclaw_home / "openclaw.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    cleaned = await oj.sanitize_managed_agent_entries()
    assert cleaned.get("csf-sanitize") == ["_managed_by", "description"]
    agent = oj.find_agent("csf-sanitize")
    assert agent is not None
    assert "description" not in agent
    assert "_managed_by" not in agent


@pytest.mark.asyncio
async def test_atomic_write_creates_backup(fake_openclaw_home: Path) -> None:
    original = (fake_openclaw_home / "openclaw.json").read_text()
    await oj.append_managed_agent({"id": "x"})
    bak = fake_openclaw_home / "openclaw.json.bak"
    assert bak.exists()
    assert bak.read_text() == original


@pytest.mark.asyncio
async def test_concurrent_appends_serialize(fake_openclaw_home: Path) -> None:
    """Two concurrent appends must both succeed (asyncio lock prevents races)."""
    import asyncio
    await asyncio.gather(
        oj.append_managed_agent({"id": "csf-conc-1"}),
        oj.append_managed_agent({"id": "csf-conc-2"}),
    )
    data = oj.load_openclaw_json()
    ids = {a["id"] for a in data["agents"]["list"]}
    assert {"csf-conc-1", "csf-conc-2"}.issubset(ids)


def test_mark_agent_managed_sync_adds_id(fake_openclaw_home: Path) -> None:
    assert "user-existing" not in oj.list_managed_agent_ids()
    oj.mark_agent_managed_sync("user-existing")
    assert "user-existing" in oj.list_managed_agent_ids()


@pytest.mark.asyncio
async def test_append_managed_agent_tries_gateway_config_sync_on_supported_platform(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _fake_sync(data: dict[str, object], *, config: Config | None = None) -> bool:
        calls.append(data)
        return True

    monkeypatch.setattr(oj, "_should_sync_gateway_config", lambda: True)
    monkeypatch.setattr(oj, "_sync_gateway_config_unlocked", _fake_sync)

    await oj.append_managed_agent({"id": "csf-macos-sync", "name": "Mac Sync"})

    assert calls, "expected gateway config sync attempt on supported platforms"
    latest = calls[-1]
    raw_agents = latest.get("agents", {}).get("list", [])
    assert isinstance(raw_agents, list)
    assert any(
        isinstance(item, dict) and item.get("id") == "csf-macos-sync"
        for item in raw_agents
    )


@pytest.mark.asyncio
async def test_append_managed_agent_keeps_file_persistence_when_gateway_sync_fails(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oj, "_should_sync_gateway_config", lambda: True)
    monkeypatch.setattr(oj, "_sync_gateway_config_unlocked", lambda *_a, **_kw: False)

    await oj.append_managed_agent({"id": "csf-macos-fallback"})
    assert oj.find_agent("csf-macos-fallback") is not None


def test_should_sync_gateway_config_true_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oj.sys, "platform", "linux")
    assert oj._should_sync_gateway_config() is True


def test_should_sync_gateway_config_true_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oj.sys, "platform", "darwin")
    assert oj._should_sync_gateway_config() is True


def test_should_sync_gateway_config_false_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oj.sys, "platform", "win32")
    assert oj._should_sync_gateway_config() is False


def test_extract_gateway_base_hash_from_top_level() -> None:
    payload = {"hash": "abc123", "raw": None, "parsed": {"agents": {"list": []}}}
    assert oj._extract_gateway_base_hash(payload) == "abc123"


def test_extract_gateway_base_hash_from_nested_result() -> None:
    payload = {
        "ok": True,
        "result": {
            "config": {
                "baseHash": "nested-hash",
            }
        },
    }
    assert oj._extract_gateway_base_hash(payload) == "nested-hash"


def test_extract_gateway_base_hash_returns_none_when_missing() -> None:
    payload = {"ok": True, "parsed": {"agents": {"list": []}}}
    assert oj._extract_gateway_base_hash(payload) is None


def test_sync_gateway_config_uses_raw_json_string(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_openclaw_home  # fixture keeps config path realistic for env wiring
    captured: dict[str, object] = {}

    def _fake_run_gateway_call(
        *,
        executable: str,
        method: str,
        params: dict[str, object],
        config: Config,
    ) -> tuple[bool, dict[str, object] | None, str]:
        del executable, config
        if method == "config.get":
            return True, {"hash": "h-1", "raw": None, "parsed": {"agents": {"list": []}}}, ""
        if method == "config.set":
            captured.update(params)
            return True, {"ok": True}, ""
        raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(oj, "_should_sync_gateway_config", lambda: True)
    monkeypatch.setattr(oj, "_run_gateway_call", _fake_run_gateway_call)
    monkeypatch.setattr(
        "app.integrations.openclaw_cli.resolve_openclaw_executable",
        lambda: "/tmp/openclaw",
    )

    data = {"agents": {"list": [{"id": "demo"}]}}
    ok = oj._sync_gateway_config_unlocked(data, config=load_config())
    assert ok is True
    assert captured.get("baseHash") == "h-1"
    raw = captured.get("raw")
    assert isinstance(raw, str)
    assert json.loads(raw) == data

