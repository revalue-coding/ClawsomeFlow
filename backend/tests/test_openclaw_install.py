"""Tests for app.integrations.openclaw_install."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import paths
from app.config import load_config, save_config
from app.integrations import openclaw_install as ins
from app.integrations import openclaw_json as oj


@pytest.fixture
def fake_openclaw_home(tmp_path: Path) -> Path:
    oc_home = tmp_path / "openclaw_home"
    oc_home.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(
        update={
            "openclaw_home": str(oc_home),
            "internal_token_secret": "test-secret",
        }
    )
    save_config(cfg)
    (oc_home / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {}, "list": []},
                "tools": {},
                "gateway": {
                    "bind": "loopback",
                    "auth": {"mode": "token", "token": "T"},
                },
            }
        ),
        encoding="utf-8",
    )
    return oc_home


@pytest.mark.asyncio
async def test_install_deploys_payloads_and_gateway_settings(fake_openclaw_home: Path) -> None:
    result = await ins.install_into_openclaw()
    assert result.common_agent_source_deployed_to.name == ".common-agent-source"
    assert result.agent_tools_deployed_to.name == ".clawsomeflow-agent-tools"
    assert result.skills_seeded_to.name == ".skills-source"
    assert result.gateway_chat_endpoint_enabled is True

    data = oj.load_openclaw_json()
    chat_cfg = (
        data["gateway"]["http"]["endpoints"]["chatCompletions"]
    )
    assert chat_cfg["enabled"] is True
    assert chat_cfg["images"]["allowUrl"] is False
    assert data["agents"]["defaults"]["timeoutSeconds"] == 1800
    assert data["tools"]["exec"]["timeoutSec"] == 1800
    # First-time integration must NOT pre-register any managed agents.
    assert data["agents"]["list"] == []
    assert oj.list_managed_agent_ids() == []
    assert not any(paths.agents_dir().iterdir())


@pytest.mark.asyncio
async def test_install_rejects_insecure_gateway_bind(fake_openclaw_home: Path) -> None:
    config_path = fake_openclaw_home / "openclaw.json"
    insecure = json.loads(config_path.read_text(encoding="utf-8"))
    insecure["gateway"]["bind"] = "0.0.0.0"
    config_path.write_text(json.dumps(insecure), encoding="utf-8")

    with pytest.raises(RuntimeError):
        await ins.install_into_openclaw()


@pytest.mark.asyncio
async def test_uninstall_removes_managed_agents_only(fake_openclaw_home: Path) -> None:
    payload = oj.load_openclaw_json()
    payload["agents"]["list"] = [
        {"id": "managed-a", "name": "Managed A", "workspace": "/tmp/a"},
        {"id": "external-b", "name": "External B", "workspace": "/tmp/b"},
    ]
    (fake_openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    registry = oj.managed_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({"agent_ids": ["managed-a"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = await ins.uninstall_from_openclaw(purge_data_dir=False)
    assert result.agents_removed == ["managed-a"]
    assert result.workspaces_removed == []
    assert result.purged_data_dir is False

    remaining = oj.load_openclaw_json()["agents"]["list"]
    assert {item["id"] for item in remaining} == {"external-b"}


@pytest.mark.asyncio
async def test_uninstall_also_removes_csflow_workspace_agents_without_registry(
    fake_openclaw_home: Path,
) -> None:
    payload = oj.load_openclaw_json()
    payload["agents"]["list"] = [
        {
            "id": "legacy-local",
            "name": "Legacy Local",
            "workspace": str(
                paths.clawsomeflow_home_path() / "agents" / "legacy-local" / "workspace"
            ),
        },
        {"id": "external-b", "name": "External B", "workspace": "/tmp/b"},
    ]
    (fake_openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = await ins.uninstall_from_openclaw(purge_data_dir=False)
    assert "legacy-local" in result.agents_removed
    remaining = oj.load_openclaw_json()["agents"]["list"]
    assert {item["id"] for item in remaining} == {"external-b"}


@pytest.mark.asyncio
async def test_uninstall_with_purge_removes_data_dir(fake_openclaw_home: Path) -> None:
    home = paths.clawsomeflow_home_path()
    (home / ".system").mkdir(parents=True, exist_ok=True)
    (home / ".system" / "marker.txt").write_text("x", encoding="utf-8")

    result = await ins.uninstall_from_openclaw(purge_data_dir=True)
    assert result.purged_data_dir is True
    assert not home.exists()


def test_install_summary_contains_key_paths(fake_openclaw_home: Path) -> None:
    summary = ins.install_summary(config=load_config())
    assert "managed_agents_in_openclaw" in summary
    assert summary["skills_source_dir"].endswith(".skills-source")
    assert summary["common_agent_source_dir"].endswith(".common-agent-source")
    assert summary["openclaw_agent_tools_dir"].endswith(".clawsomeflow-agent-tools")


def test_parse_json_from_mixed_output_handles_prefixed_logs() -> None:
    raw = "noise line\n{\"pending\": [{\"requestId\": \"r1\", \"isRepair\": true, \"clientId\": \"cli\"}]}"
    parsed = ins._parse_json_from_mixed_output(raw)
    assert parsed is not None
    assert parsed["pending"][0]["requestId"] == "r1"


def test_pending_scope_repair_request_filtering() -> None:
    payload = {
        "pending": [
            {"requestId": "r1", "isRepair": True, "clientId": "cli"},
            {"requestId": "r2", "isRepair": False, "clientId": "cli"},
            {"requestId": "r3", "isRepair": True, "clientId": "ui"},
            {"requestId": "", "isRepair": True, "clientId": "cli"},
        ]
    }
    assert ins._pending_scope_repair_request_ids(payload) == ["r1"]


def test_pending_scope_repair_request_filtering_falls_back_to_latest_pending() -> None:
    payload = {
        "pending": [
            {
                "requestId": "r-old",
                "isRepair": False,
                "clientId": "ui",
                "requestedAt": "2026-05-20T01:00:00Z",
            },
            {
                "requestId": "r-new",
                "isRepair": False,
                "clientId": "ui",
                "requestedAt": "2026-05-20T02:00:00Z",
            },
        ]
    }
    assert ins._pending_scope_repair_request_ids(payload) == ["r-new"]


def test_scope_pending_detection_includes_operator_admin_hint() -> None:
    assert ins.looks_like_pending_scope_approval(
        "GatewayClientRequestError: missing scope: operator.admin"
    )


def test_repair_pending_scope_upgrades_retries_with_request_rotation(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_openclaw_home
    cfg = load_config()
    calls = {"list": 0, "approve": []}
    slept: list[float] = []

    class _Proc:
        def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    payloads = [
        {
            "pending": [
                {
                    "requestId": "req-old",
                    "requestedAt": "2026-05-20T01:00:00Z",
                    "isRepair": False,
                    "clientId": "ui",
                }
            ]
        },
        {
            "pending": [
                {
                    "requestId": "req-new",
                    "requestedAt": "2026-05-20T01:00:10Z",
                    "isRepair": True,
                    "clientId": "cli",
                }
            ]
        },
    ]

    def _fake_run(argv, **kwargs):
        del kwargs
        if argv[:3] == ["/tmp/openclaw", "devices", "list"]:
            idx = calls["list"]
            calls["list"] += 1
            payload = payloads[min(idx, len(payloads) - 1)]
            return _Proc(stdout=json.dumps(payload))
        if argv[:3] == ["/tmp/openclaw", "devices", "approve"]:
            request_id = argv[3]
            calls["approve"].append(request_id)
            if request_id == "req-old":
                return _Proc(returncode=1, stderr="request expired")
            return _Proc(returncode=0, stdout='{"ok":true}')
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(ins.shutil, "which", lambda _: "/tmp/openclaw")
    monkeypatch.setattr(ins.subprocess, "run", _fake_run)
    monkeypatch.setattr(ins.time, "sleep", lambda sec: slept.append(sec))

    approved = ins._repair_pending_scope_upgrades(
        config=cfg,
        max_attempts=3,
        sleep_seconds=0.2,
    )
    assert approved == ["req-new"]
    assert calls["approve"] == ["req-old", "req-new"]
    assert calls["list"] == 2
    assert slept == [0.2]
