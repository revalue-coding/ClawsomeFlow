"""Tests for /api/profiles."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.integrations.clawteam_cli import CliInvocationError, get_clawteam_cli
from app.main import create_app


@pytest.fixture
def app_client(tmp_path: Path):
    cfg = load_config()
    save_config(cfg)
    with TestClient(create_app()) as c:
        yield c


def test_list_profiles(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = get_clawteam_cli()

    async def fake_list():
        return {
            "claude-default": {
                "agent": "claude", "model": "sonnet-4",
                "base_url": None, "description": "Default",
            },
            "kimi": {"agent": "claude", "model": None, "base_url": "https://k.ai"},
        }

    monkeypatch.setattr(cli, "profile_list", fake_list)
    r = app_client.get("/api/profiles")
    assert r.status_code == 200
    items = r.json()["items"]
    # Sorted by name.
    assert [p["name"] for p in items] == ["claude-default", "kimi"]
    assert items[0]["agent"] == "claude"
    assert items[1]["baseUrl"] == "https://k.ai"


def test_show_profile_not_found(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = get_clawteam_cli()

    async def fake_show(name):
        raise CliInvocationError(
            argv=["clawteam", "profile", "show", name],
            exit_code=1, stderr=f"Profile {name!r} not found",
        )

    monkeypatch.setattr(cli, "profile_show", fake_show)
    r = app_client.get("/api/profiles/nope")
    assert r.status_code == 404
    assert r.json()["error"] == "PROFILE_NOT_FOUND"


def test_show_profile_returns_raw(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = get_clawteam_cli()

    async def fake_show(name):
        return {"agent": "claude", "model": "sonnet-4", "extra": {"x": 1}}

    monkeypatch.setattr(cli, "profile_show", fake_show)
    r = app_client.get("/api/profiles/claude-default")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "claude-default"
    assert body["raw"]["extra"] == {"x": 1}


def test_test_profile_success(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = get_clawteam_cli()

    captured = {}

    async def fake_test(name, *, prompt=None, cwd=None):
        captured.update(name=name, prompt=prompt, cwd=cwd)
        return True, "CLAWTEAM_PROFILE_OK\n"

    monkeypatch.setattr(cli, "profile_test", fake_test)
    r = app_client.post(
        "/api/profiles/claude-default/test",
        json={"prompt": "say hi"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert "CLAWTEAM_PROFILE_OK" in body["output"]
    assert captured == {"name": "claude-default", "prompt": "say hi", "cwd": None}


def test_test_profile_failure(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = get_clawteam_cli()

    async def fake_test(name, *, prompt=None, cwd=None):
        return False, "model unreachable"

    monkeypatch.setattr(cli, "profile_test", fake_test)
    r = app_client.post("/api/profiles/x/test", json={})
    body = r.json()
    assert body["success"] is False
    assert "unreachable" in body["output"]
