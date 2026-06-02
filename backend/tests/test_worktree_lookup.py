"""Tests for app.worktree.lookup."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.config import load_config, save_config
from app.worktree import lookup as wl


class _FakeCli:
    def __init__(self, payload: list[dict] | Exception) -> None:
        self._payload = payload
        self.calls = 0

    async def workspace_list(self, *, team: str, repo: str | None = None) -> list[dict]:
        self.calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return list(self._payload)


_SAMPLE = [
    {
        "agent_name": "alice",
        "agent_id": "alice",
        "team_name": "csflow-x",
        "branch_name": "clawteam/csflow-x/alice",
        "worktree_path": "/tmp/wt/alice",
        "repo_root": "/tmp/main",
        "base_branch": "main",
        "created_at": "2026-05-07T00:00:00Z",
    },
    {
        "agent_name": "bob",
        "agent_id": "bob",
        "team_name": "csflow-x",
        "branch_name": "clawteam/csflow-x/bob",
        "worktree_path": "/tmp/wt/bob",
        "repo_root": "/tmp/main",
        "base_branch": "main",
        "created_at": "2026-05-07T00:00:01Z",
    },
]


@pytest.mark.asyncio
async def test_list_team_returns_typed_workspaces() -> None:
    fake = _FakeCli(_SAMPLE)
    look = wl.WorktreeLookup(cli=fake, ttl_seconds=10.0)
    items = await look.list_team("csflow-x")
    assert {w.agent_name for w in items} == {"alice", "bob"}
    assert items[0].branch_name.startswith("clawteam/")


@pytest.mark.asyncio
async def test_get_finds_specific_agent() -> None:
    look = wl.WorktreeLookup(cli=_FakeCli(_SAMPLE), ttl_seconds=10.0)
    info = await look.get("csflow-x", "bob")
    assert info is not None
    assert info.worktree_path == "/tmp/wt/bob"


@pytest.mark.asyncio
async def test_get_returns_none_for_missing() -> None:
    look = wl.WorktreeLookup(cli=_FakeCli(_SAMPLE), ttl_seconds=10.0)
    assert await look.get("csflow-x", "nope") is None


@pytest.mark.asyncio
async def test_cache_avoids_repeated_cli_calls() -> None:
    fake = _FakeCli(_SAMPLE)
    look = wl.WorktreeLookup(cli=fake, ttl_seconds=10.0)
    await look.list_team("csflow-x")
    await look.list_team("csflow-x")
    await look.list_team("csflow-x")
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_force_bypasses_cache() -> None:
    fake = _FakeCli(_SAMPLE)
    look = wl.WorktreeLookup(cli=fake, ttl_seconds=10.0)
    await look.list_team("csflow-x")
    await look.list_team("csflow-x", force=True)
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_invalidate_specific_team() -> None:
    fake = _FakeCli(_SAMPLE)
    look = wl.WorktreeLookup(cli=fake, ttl_seconds=10.0)
    await look.list_team("csflow-x")
    look.invalidate("csflow-x")
    await look.list_team("csflow-x")
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_falls_back_to_registry_on_cli_error(tmp_path: Path) -> None:
    # Point CLAWTEAM_DATA_DIR (via Config.clawteam_data_dir) to a tmp tree
    # that has the registry file.
    cfg = load_config()
    cfg = cfg.model_copy(update={"clawteam_data_dir": str(tmp_path)})
    save_config(cfg)

    reg_dir = tmp_path / "workspaces" / "csflow-y"
    reg_dir.mkdir(parents=True)
    (reg_dir / "workspace-registry.json").write_text(json.dumps({
        "team_name": "csflow-y",
        "repo_root": "/tmp/main",
        "workspaces": _SAMPLE,
    }))

    fake = _FakeCli(RuntimeError("CLI down"))
    look = wl.WorktreeLookup(cli=fake, config=cfg, ttl_seconds=10.0)
    items = await look.list_team("csflow-y")
    assert len(items) == 2
