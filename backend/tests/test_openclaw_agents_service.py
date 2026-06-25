"""Tests for app.services.openclaw_agents — the core agent lifecycle."""

from __future__ import annotations

import json
import shutil
import subprocess
import asyncio
from pathlib import Path

import pytest

from app import paths
from app.config import load_config, save_config
from app.integrations import openclaw_json as oj
from app.models import Flow, FlowRun, OpenclawAgent, RunStatus
from app.services import openclaw_agents as svc


@pytest.fixture
def fake_openclaw_home(tmp_path: Path) -> Path:
    oc_home = tmp_path / "openclaw_home"
    oc_home.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(update={"openclaw_home": str(oc_home)})
    save_config(cfg)
    (oc_home / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {}, "list": []},
        "gateway": {"port": 18789, "auth": {"token": "T"}},
    }, indent=2))
    return oc_home


def _has_git() -> bool:
    return shutil.which("git") is not None


def _seed_managed_openclaw_entry_without_db(
    *,
    openclaw_home: Path,
    agent_id: str,
    workspace: Path,
    write_registry: bool = True,
) -> None:
    payload = oj.load_openclaw_json()
    payload.setdefault("agents", {}).setdefault("list", []).append({
        "id": agent_id,
        "name": f"{agent_id}-name",
        "description": "seeded from openclaw.json only",
        "workspace": str(workspace),
        "default": False,
        "identity": {"name": f"{agent_id}-name"},
    })
    (openclaw_home / "openclaw.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if not write_registry:
        return
    registry = oj.managed_registry_path()
    existing_ids: set[str] = set()
    if registry.exists():
        existing_payload = json.loads(registry.read_text(encoding="utf-8"))
        existing_ids = set(existing_payload.get("agent_ids", []))
    existing_ids.add(agent_id)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({"agent_ids": sorted(existing_ids)}, indent=2),
        encoding="utf-8",
    )


def _seed_unmanaged_runtime_agent(
    *,
    openclaw_home: Path,
    agent_id: str,
    name: str,
    workspace: Path,
    description: str = "",
) -> None:
    payload = oj.load_openclaw_json()
    payload.setdefault("agents", {}).setdefault("list", []).append({
        "id": agent_id,
        "name": name,
        "description": description,
        "workspace": str(workspace),
        "default": False,
        "identity": {"name": name},
    })
    (openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def test_reindex_backfills_db_for_registered_managed_agent(
    fake_openclaw_home: Path,
) -> None:
    aid = "rehydrated"
    workspace = paths.agent_dir(aid) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _seed_managed_openclaw_entry_without_db(
        openclaw_home=fake_openclaw_home,
        agent_id=aid,
        workspace=workspace,
    )
    from app.storage import get_storage
    storage = get_storage()
    assert storage.openclaw_get(aid) is None

    inserted = svc.reindex_registered_agents(storage=storage)
    assert aid in inserted
    row = storage.openclaw_get(aid)
    assert row is not None
    assert row.workspace_path == str(workspace)
    assert row.created_by_user == load_config().default_user
    assert row.team_id == ""
    assert oj.has_managed_agent(aid)


def test_reindex_backfills_even_without_managed_registry(
    fake_openclaw_home: Path,
) -> None:
    aid = "json-only-without-registry"
    workspace = paths.agent_dir(aid) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _seed_managed_openclaw_entry_without_db(
        openclaw_home=fake_openclaw_home,
        agent_id=aid,
        workspace=workspace,
        write_registry=False,
    )
    from app.storage import get_storage
    storage = get_storage()
    assert not oj.has_managed_agent(aid)

    inserted = svc.reindex_registered_agents(storage=storage)
    assert aid in inserted
    assert storage.openclaw_get(aid) is not None
    assert oj.has_managed_agent(aid)


def test_reindex_skips_workspace_outside_clawsomeflow_agents(
    fake_openclaw_home: Path,
    tmp_path: Path,
) -> None:
    aid = "outside-home"
    outside_workspace = tmp_path / "outside-home-workspace"
    outside_workspace.mkdir(parents=True, exist_ok=True)
    _seed_managed_openclaw_entry_without_db(
        openclaw_home=fake_openclaw_home,
        agent_id=aid,
        workspace=outside_workspace,
    )
    from app.storage import get_storage
    storage = get_storage()

    inserted = svc.reindex_registered_agents(storage=storage)
    assert aid not in inserted
    assert storage.openclaw_get(aid) is None


def test_reindex_skips_when_workspace_path_missing(
    fake_openclaw_home: Path,
) -> None:
    aid = "missing-workspace"
    missing_workspace = paths.clawsomeflow_home_path() / "agents" / aid / "workspace"
    _seed_managed_openclaw_entry_without_db(
        openclaw_home=fake_openclaw_home,
        agent_id=aid,
        workspace=missing_workspace,
    )
    from app.storage import get_storage

    storage = get_storage()
    inserted = svc.reindex_registered_agents(storage=storage)
    assert aid not in inserted
    assert storage.openclaw_get(aid) is None


@pytest.mark.asyncio
async def test_commit_agent_fails_fast_when_create_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate/concurrent create of the same id fails fast (no side effects)
    instead of racing into the shared workspace — whose files the loser's
    rollback would otherwise wipe. Stubs the heavy setup so no CLI runs."""

    async def _noop(*_a, **_k):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(svc, "reindex_registered_agents", lambda *_a, **_k: None)
    monkeypatch.setattr(svc.oj, "sanitize_managed_agent_entries", _noop)
    monkeypatch.setattr(svc, "ensure_runtime_timeout_defaults", _noop)
    monkeypatch.setattr(svc, "_resolve_default_source_agent_id", lambda **_k: None)
    monkeypatch.setattr(svc, "_validate_agent_id", lambda x: x)

    ran: list[str] = []

    async def _reserved(_cmd, aid, **_k):  # noqa: ANN001, ANN202
        ran.append(aid)
        assert aid in svc._CREATE_IN_PROGRESS  # reserved while the body runs
        return "ok"

    monkeypatch.setattr(svc, "_commit_agent_reserved", _reserved)
    cmd = svc.CommitInput(id="dupe", name="Dupe")

    # Normal create runs the body; reservation stays until finish_create_in_flight.
    assert await svc.commit_agent(cmd, user="u", storage=object(), config=object()) == "ok"
    assert "dupe" in svc._CREATE_IN_PROGRESS
    svc.finish_create_in_flight("dupe")
    assert "dupe" not in svc._CREATE_IN_PROGRESS

    # While a create for the id is in flight, a second one fails fast.
    svc._CREATE_IN_PROGRESS.add("dupe")
    try:
        with pytest.raises(svc.AgentAlreadyExists):
            await svc.commit_agent(cmd, user="u", storage=object(), config=object())
    finally:
        svc._CREATE_IN_PROGRESS.discard("dupe")
    assert ran == ["dupe"]  # the body ran exactly once (the first, valid create)


@pytest.mark.asyncio
async def test_commit_creates_workspace_skill_json_and_db(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")

    cmd = svc.CommitInput(
        id="myagent",
        name="My Agent",
        description="A test agent",
        identity=svc.AgentIdentity(emoji="🤖", theme="logical"),
        nl_prompt="please make me an agent",
    )
    agent = await svc.commit_agent(cmd, user="alice")

    # DB row
    assert agent.id == "myagent"
    assert agent.name == "My Agent"
    assert agent.workspace_path == str(paths.agent_dir("myagent") / "workspace")
    assert agent.team_id == ""

    # Workspace exists, is a git repo with one commit
    ws = Path(agent.workspace_path)
    assert (ws / ".git").exists()
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=ws, capture_output=True, text=True, check=True,
    ).stdout
    assert "[csflow] initial commit" in log

    # Skill installed
    assert (
        ws / "skills" / "self-skills-heartbeats-maintenance" / "SKILL.md"
    ).exists()
    assert (
        ws / "skills" / "self-definition-maintenance" / "SKILL.md"
    ).exists()
    assert (ws / ".env").exists()
    assert (ws / "my-desktop").is_dir()
    # Common rules were materialized into AGENTS.md.
    assert (ws / "AGENTS.md").exists()
    assert "Shared Rules for ClawsomeFlow Managed Agents" in (
        ws / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "AGENTS_USER_CUSTOM_SECTION" in (
        ws / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert not (ws / ".csflow-agent-tools").exists()

    # openclaw.json registers it and tracks it as managed
    entry = oj.find_agent("myagent")
    assert entry is not None
    assert "_managed_by" not in entry
    assert oj.has_managed_agent("myagent")
    assert entry["workspace"] == str(ws)
    assert entry["agentDir"] == str(fake_openclaw_home / "agents" / "myagent" / "agent")
    assert entry["identity"]["emoji"] == "🤖"
    assert (fake_openclaw_home / "agents" / "myagent" / "sessions").is_dir()


@pytest.mark.asyncio
async def test_commit_seeds_only_portable_static_auth_profiles(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    main_agent_dir = fake_openclaw_home / "agents" / "main" / "agent"
    main_agent_dir.mkdir(parents=True, exist_ok=True)
    (main_agent_dir / "auth-profiles.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "openai-static": {
                        "provider": "openai",
                        "type": "api_key",
                        "mode": "manual",
                        "key": "sk-openai-static",
                    },
                    "openrouter-static-token": {
                        "provider": "openrouter",
                        "type": "token",
                        "mode": "manual",
                        "token": "or-token",
                    },
                    "openai-oauth": {
                        "provider": "openai",
                        "type": "oauth",
                        "mode": "oauth",
                        "refresh": "refresh-default-skip",
                    },
                    "openai-oauth-opt-in": {
                        "provider": "openai",
                        "type": "oauth",
                        "mode": "oauth",
                        "copyToAgents": True,
                        "refresh": "refresh-allow-copy",
                    },
                    "static-opt-out": {
                        "provider": "openai",
                        "type": "api_key",
                        "key": "sk-opt-out",
                        "copyToAgents": False,
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    await svc.commit_agent(
        svc.CommitInput(id="seed-auth", name="Seed Auth"),
        user="alice",
    )

    target_path = fake_openclaw_home / "agents" / "seed-auth" / "agent" / "auth-profiles.json"
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    profiles = payload.get("profiles", {})
    assert "openai-static" in profiles
    assert "openrouter-static-token" in profiles
    assert "openai-oauth" not in profiles
    assert "openai-oauth-opt-in" in profiles
    assert "static-opt-out" not in profiles


@pytest.mark.asyncio
async def test_commit_auth_seed_skips_when_target_auth_store_already_exists(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    main_agent_dir = fake_openclaw_home / "agents" / "main" / "agent"
    main_agent_dir.mkdir(parents=True, exist_ok=True)
    (main_agent_dir / "auth-profiles.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "shared-profile": {
                        "provider": "openai",
                        "type": "api_key",
                        "key": "source-key",
                    },
                    "source-only-profile": {
                        "provider": "openai",
                        "type": "api_key",
                        "key": "source-only",
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    target_auth_dir = fake_openclaw_home / "agents" / "seed-merge" / "agent"
    target_auth_dir.mkdir(parents=True, exist_ok=True)
    (target_auth_dir / "auth-profiles.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "shared-profile": {
                        "provider": "openai",
                        "type": "api_key",
                        "key": "target-key",
                    },
                    "target-only-profile": {
                        "provider": "anthropic",
                        "type": "api_key",
                        "key": "target-only",
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    await svc.commit_agent(
        svc.CommitInput(id="seed-merge", name="Seed Merge"),
        user="alice",
    )

    payload = json.loads((target_auth_dir / "auth-profiles.json").read_text(encoding="utf-8"))
    profiles = payload.get("profiles", {})
    assert profiles["shared-profile"]["key"] == "target-key"
    assert "target-only-profile" in profiles
    assert "source-only-profile" not in profiles


@pytest.mark.asyncio
async def test_commit_auth_seed_uses_default_source_agent_id(
    fake_openclaw_home: Path,
    tmp_path: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    source_agent_id = "source-default"
    source_agent_dir = fake_openclaw_home / "agents" / source_agent_id / "agent"
    source_agent_dir.mkdir(parents=True, exist_ok=True)
    (source_agent_dir / "auth-profiles.json").write_text(
        json.dumps(
            {
                "profiles": {
                    "source-default-key": {
                        "provider": "openai",
                        "type": "api_key",
                        "key": "sk-from-default-agent",
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = oj.load_openclaw_json()
    payload["agents"]["list"] = [
        {
            "id": source_agent_id,
            "name": "Source Default",
            "default": True,
            "workspace": str(tmp_path / "source-default-workspace"),
            "agentDir": str(source_agent_dir),
        }
    ]
    (fake_openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    await svc.commit_agent(
        svc.CommitInput(id="seed-from-default", name="Seed From Default"),
        user="alice",
    )

    target_path = (
        fake_openclaw_home
        / "agents"
        / "seed-from-default"
        / "agent"
        / "auth-profiles.json"
    )
    seeded = json.loads(target_path.read_text(encoding="utf-8"))
    profiles = seeded.get("profiles", {})
    assert "source-default-key" in profiles


@pytest.mark.asyncio
async def test_commit_agent_triggers_default_entropy_schedule(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    called: list[str] = []

    def fake_schedule(*, agent_id: str, config=None) -> bool:
        del config
        called.append(agent_id)
        return True

    monkeypatch.setattr(svc, "_schedule_default_entropy_management_task", fake_schedule)
    await svc.commit_agent(
        svc.CommitInput(id="entropy-agent", name="Entropy Agent"),
        user="alice",
    )
    assert called == ["entropy-agent"]


def test_entropy_schedule_sync_updates_existing_job_definition(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_openclaw_home
    definition = svc._CommonCronJobDefinition(
        definition_id="entropy-management",
        name_template="csflow-entropy-management-{agent_id}",
        cron_expr="0 3 * * 1",
        cron_tz="UTC",
        session="isolated",
        message_template="weekly entropy + INDEX sync for {agent_id}",
    )
    monkeypatch.setattr(
        svc,
        "_load_common_cron_job_definitions",
        lambda: (definition,),
    )

    calls: list[list[str]] = []

    def fake_run_openclaw_cli(*, args, config):
        del config
        calls.append(list(args))
        if args[:2] == ["cron", "list"]:
            payload = {
                "jobs": [
                    {
                        "id": "job-1",
                        "agentId": "sync-agent",
                        "name": "csflow-entropy-management-sync-agent",
                    }
                ]
            }
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        if args[:2] == ["cron", "edit"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="ok",
                stderr="",
            )
        raise AssertionError(f"unexpected openclaw CLI call: {args}")

    monkeypatch.setattr(svc, "_run_openclaw_cli", fake_run_openclaw_cli)

    ok = svc._schedule_default_entropy_management_task(
        agent_id="sync-agent",
        config=load_config(),
    )
    assert ok is True
    cron_list_calls = [call for call in calls if call[:2] == ["cron", "list"]]
    assert cron_list_calls
    assert all("--agent" in call for call in cron_list_calls)
    assert all(call[call.index("--agent") + 1] == "sync-agent" for call in cron_list_calls)
    assert any(call[:2] == ["cron", "edit"] for call in calls)
    assert not any(call[:2] == ["cron", "add"] for call in calls)


def test_create_team_is_idempotent_by_name(fake_openclaw_home: Path) -> None:
    t1 = svc.create_team("研发团队", user="alice")
    t2 = svc.create_team("研发团队", user="alice")
    assert t1.id == t2.id
    assert t1.name == "研发团队"


def test_update_team_renames_for_owner(fake_openclaw_home: Path) -> None:
    created = svc.create_team("旧团队名", user="alice")
    updated = svc.update_team(created.id, "新团队名", user="alice")
    assert updated.id == created.id
    assert updated.name == "新团队名"


def test_list_teams_excludes_orphaned_teams(fake_openclaw_home: Path) -> None:
    from app.storage import get_storage

    storage = get_storage()
    team = svc.create_team("孤立团队", user="alice", storage=storage)
    # Team exists but has no members, so it should not appear in team enum list.
    assert svc.list_teams(user="alice", storage=storage) == []

    agent_id = "team-member-service"
    storage.openclaw_create(
        OpenclawAgent(
            id=agent_id,
            name="Team Member",
            description="",
            team_id=team.id,
            workspace_path=str(paths.agent_dir(agent_id) / "workspace"),
            openclaw_config_snapshot={},
            created_by_user="alice",
            nl_prompt="",
        )
    )
    listed = svc.list_teams(user="alice", storage=storage)
    assert [item.id for item in listed] == [team.id]

    row = storage.openclaw_get(agent_id)
    assert row is not None
    row.team_id = ""
    storage.openclaw_update(row)
    assert svc.list_teams(user="alice", storage=storage) == []


def test_list_teams_counts_hermes_membership(fake_openclaw_home: Path) -> None:
    """A team holding only Hermes agents is shared (OpenClaw + Hermes) and must
    still be listed — else a freshly-assigned Hermes agent falls back to
    'ungrouped' and its new group never appears (issue: change-team no-op)."""
    from app.models import HermesAgent
    from app.storage import get_storage

    storage = get_storage()
    team = svc.create_team("纯Hermes团队", user="alice", storage=storage)
    # No OpenClaw agent references it, only a Hermes agent does.
    assert svc.list_teams(user="alice", storage=storage) == []
    storage.hermes_create(
        HermesAgent(
            id="hteam",
            name="H Member",
            profile_root="x",
            team_id=team.id,
            created_by_user="alice",
        )
    )
    listed = svc.list_teams(user="alice", storage=storage)
    assert [item.id for item in listed] == [team.id]


@pytest.mark.asyncio
async def test_update_agent_can_change_team(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    t1 = svc.create_team("A组", user="alice")
    t2 = svc.create_team("B组", user="alice")
    await svc.commit_agent(
        svc.CommitInput(id="move-team-agent", name="Move Team"),
        user="alice",
        team_id=t1.id,
    )
    updated = await svc.update_agent(
        "move-team-agent",
        svc.UpdateInput(team_id=t2.id),
    )
    assert updated.team_id == t2.id


@pytest.mark.asyncio
async def test_update_agent_can_clear_team_to_ungrouped(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    t1 = svc.create_team("A组", user="alice")
    await svc.commit_agent(
        svc.CommitInput(id="clear-team-agent", name="Clear Team"),
        user="alice",
        team_id=t1.id,
    )
    updated = await svc.update_agent(
        "clear-team-agent",
        svc.UpdateInput(team_id=""),
    )
    assert updated.team_id == ""


@pytest.mark.asyncio
async def test_commit_repins_runtime_timeout_defaults(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    payload = oj.load_openclaw_json()
    payload.setdefault("agents", {})["defaults"] = {"timeoutSeconds": 120}
    payload["tools"] = {"exec": {"timeoutSec": 120}}
    (fake_openclaw_home / "openclaw.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    await svc.commit_agent(svc.CommitInput(id="timeout-pinned", name="Pinned"), user="u")
    after = oj.load_openclaw_json()
    assert after["agents"]["defaults"]["timeoutSeconds"] == 1800
    assert after["tools"]["exec"]["timeoutSec"] == 1800


@pytest.mark.asyncio
async def test_commit_rejects_invalid_id(fake_openclaw_home: Path) -> None:
    cmd = svc.CommitInput(id="bad id with space", name="x")
    with pytest.raises(svc.AgentIdInvalid):
        await svc.commit_agent(cmd, user="alice")


@pytest.mark.asyncio
async def test_commit_rejects_existing_id(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="dup", name="A"), user="u")
    with pytest.raises(svc.AgentAlreadyExists):
        await svc.commit_agent(svc.CommitInput(id="dup", name="B"), user="u")


@pytest.mark.asyncio
async def test_commit_refuses_when_user_already_has_same_id(
    fake_openclaw_home: Path,
) -> None:
    """If the user has a manually-created agent with the same id, refuse."""
    data = oj.load_openclaw_json()
    data["agents"]["list"].append({"id": "shared-id", "name": "User's"})
    (fake_openclaw_home / "openclaw.json").write_text(json.dumps(data, indent=2))
    with pytest.raises(svc.AgentAlreadyExists) as ei:
        await svc.commit_agent(svc.CommitInput(id="shared-id", name="x"), user="u")
    # Surfaces unmanaged ownership in details.
    assert ei.value.details.get("managed") is False


@pytest.mark.asyncio
async def test_commit_sanitizes_legacy_invalid_keys_in_managed_entries(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    legacy_id = "legacy-invalid"
    legacy_ws = paths.agent_dir(legacy_id) / "workspace"
    legacy_ws.mkdir(parents=True, exist_ok=True)
    _seed_managed_openclaw_entry_without_db(
        openclaw_home=fake_openclaw_home,
        agent_id=legacy_id,
        workspace=legacy_ws,
    )
    payload = oj.load_openclaw_json()
    for agent in payload["agents"]["list"]:
        if agent.get("id") != legacy_id:
            continue
        agent["_managed_by"] = "legacy"
        agent["timeoutSeconds"] = 1800
    (fake_openclaw_home / "openclaw.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    await svc.commit_agent(svc.CommitInput(id="new-after-sanitize", name="New"), user="u")
    updated = oj.find_agent(legacy_id)
    assert updated is not None
    assert "description" not in updated
    assert "_managed_by" not in updated
    assert "timeoutSeconds" not in updated


@pytest.mark.asyncio
async def test_update_keeps_db_and_json_in_sync(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="up1", name="Old"), user="u")
    updated = await svc.update_agent(
        "up1",
        svc.UpdateInput(
            name="New Name",
            description="changed",
            model="poe/GPT-5.4",
            identity=svc.AgentIdentity(emoji="🚀"),
        ),
    )
    assert updated.name == "New Name"
    assert updated.description == "changed"

    entry = oj.find_agent("up1")
    assert entry["name"] == "New Name"
    assert "description" not in entry
    assert entry["model"] == "poe/GPT-5.4"
    assert entry["identity"]["emoji"] == "🚀"


@pytest.mark.asyncio
async def test_update_refuses_unmanaged(fake_openclaw_home: Path) -> None:
    """update_agent must refuse to mutate non-managed entries."""
    if not _has_git():
        pytest.skip("git not available")
    # Manually plant an unmanaged agent and a DB row.
    data = oj.load_openclaw_json()
    data["agents"]["list"].append({"id": "manual", "name": "ManuallyAdded"})
    (fake_openclaw_home / "openclaw.json").write_text(json.dumps(data, indent=2))
    from app.models import OpenclawAgent
    from app.storage import get_storage
    get_storage().openclaw_create(
        OpenclawAgent(
            id="manual",
            name="ManuallyAdded",
            workspace_path="/tmp/x",
            created_by_user="u",
        )
    )
    with pytest.raises(svc.AgentUnmanaged):
        await svc.update_agent("manual", svc.UpdateInput(name="hacked"))


@pytest.mark.asyncio
async def test_delete_removes_json_db_keeps_workspace_by_default(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    agent = await svc.commit_agent(svc.CommitInput(id="del1", name="X"), user="u")
    ws = Path(agent.workspace_path)
    await svc.delete_agent("del1")

    assert oj.find_agent("del1") is None
    from app.storage import get_storage
    assert get_storage().openclaw_get("del1") is not None
    # Workspace data preserved by default
    assert ws.exists()


@pytest.mark.asyncio
async def test_delete_purge_removes_workspace(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    agent = await svc.commit_agent(svc.CommitInput(id="del2", name="X"), user="u")
    parent = Path(agent.workspace_path).parent
    await svc.delete_agent("del2", mode="purge")
    from app.storage import get_storage
    assert get_storage().openclaw_get("del2") is None
    assert oj.find_agent("del2") is None
    assert not parent.exists()


@pytest.mark.asyncio
async def test_delete_purge_workspace_orphan_without_db_row(
    fake_openclaw_home: Path,
) -> None:
    orphan_id = "orphan-purge-no-db"
    workspace = paths.agent_dir(orphan_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "marker.txt").write_text("x", encoding="utf-8")
    await svc.delete_agent(orphan_id, mode="purge")
    from app.storage import get_storage
    assert get_storage().openclaw_get(orphan_id) is None
    assert not paths.agent_dir(orphan_id).exists()


@pytest.mark.asyncio
async def test_delete_purge_workspace_orphan_is_idempotent(
    fake_openclaw_home: Path,
) -> None:
    orphan_id = "orphan-purge-repeat"
    workspace = paths.agent_dir(orphan_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    await svc.delete_agent(orphan_id, mode="purge")
    await svc.delete_agent(orphan_id, mode="purge")

    assert not paths.agent_dir(orphan_id).exists()


@pytest.mark.asyncio
async def test_delete_purge_workspace_orphan_concurrent_requests(
    fake_openclaw_home: Path,
) -> None:
    orphan_id = "orphan-purge-race"
    workspace = paths.agent_dir(orphan_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "marker.txt").write_text("x", encoding="utf-8")

    await asyncio.gather(
        svc.delete_agent(orphan_id, mode="purge"),
        svc.delete_agent(orphan_id, mode="purge"),
    )

    assert not paths.agent_dir(orphan_id).exists()


@pytest.mark.asyncio
async def test_delete_refuses_when_agent_exists_in_flow_template(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="busy", name="X"), user="u")
    from app.storage import get_storage
    storage = get_storage()
    storage.flow_create(
        Flow(
            id="flow-busy-template",
            name="Busy Template",
            owner_user="u",
            spec={
                "agents": [
                    {"id": "busy", "kind": "openclaw"},
                ],
                "tasks": [],
            },
        )
    )
    with pytest.raises(svc.AgentInUse) as ei:
        await svc.delete_agent("busy")
    assert ei.value.details["flow_names"] == ["Busy Template"]
    assert ei.value.details["template_flow_names"] == ["Busy Template"]


@pytest.mark.asyncio
async def test_delete_refuses_when_agent_exists_in_active_run(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="busy-active", name="X"), user="u")
    from app.storage import get_storage
    storage = get_storage()
    flow = storage.flow_create(
        Flow(
            id="flow-busy-active",
            name="Busy Active",
            owner_user="u",
            spec={
                "agents": [
                    {"id": "busy-active", "kind": "openclaw"},
                ],
                "tasks": [],
            },
        )
    )
    storage.run_create(
        FlowRun(
            id="run-busy-active",
            flow_id=flow.id,
            flow_version=flow.version,
            team_name="csflow-busy-active",
            status=RunStatus.running,
            user="u",
        )
    )
    with pytest.raises(svc.AgentInUse) as ei:
        await svc.delete_agent("busy-active")
    assert ei.value.details["flow_names"] == ["Busy Active"]
    assert ei.value.details["active_flow_names"] == ["Busy Active"]


@pytest.mark.asyncio
async def test_list_restorable_candidates_after_unregister(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="restore-candidate", name="Restore C"), user="u")
    await svc.delete_agent("restore-candidate", mode="unregister")
    items = svc.list_restorable_agents(user="u")
    assert any(item.id == "restore-candidate" for item in items)


@pytest.mark.asyncio
async def test_restore_agent_registration_re_registers_runtime(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    created = await svc.commit_agent(svc.CommitInput(id="restore-one", name="Restore Me"), user="u")
    await svc.delete_agent("restore-one", mode="unregister")
    assert oj.find_agent("restore-one") is None
    restored = await svc.restore_agent_registration("restore-one", user="u")
    assert restored.id == "restore-one"
    assert restored.workspace_path == created.workspace_path
    assert oj.find_agent("restore-one") is not None


@pytest.mark.asyncio
async def test_restore_agent_registration_is_idempotent_once_registered(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    created = await svc.commit_agent(svc.CommitInput(id="restore-repeat", name="Restore Twice"), user="u")
    await svc.delete_agent("restore-repeat", mode="unregister")

    first = await svc.restore_agent_registration("restore-repeat", user="u")
    second = await svc.restore_agent_registration("restore-repeat", user="u")

    assert first.id == second.id == "restore-repeat"
    assert second.workspace_path == created.workspace_path
    assert oj.find_agent("restore-repeat") is not None


@pytest.mark.asyncio
async def test_delete_unregister_backs_up_custom_cron_jobs_in_snapshot(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    created = await svc.commit_agent(
        svc.CommitInput(id="restore-cron-backup", name="Restore Cron Backup"),
        user="u",
    )
    from app.storage import get_storage

    storage = get_storage()

    def _fake_run_openclaw_cli(*, args, config):
        del config
        if args[:2] == ["cron", "list"]:
            payload = {
                "jobs": [
                    {
                        "id": "system-entropy",
                        "agentId": created.id,
                        "name": f"{svc._ENTROPY_CRON_NAME_PREFIX}-{created.id}",
                        "source": "system",
                        "schedule": {"expr": "0 3 * * 1", "tz": "UTC"},
                        "payload": {"message": "system entropy"},
                    },
                    {
                        "id": "custom-job-1",
                        "agentId": created.id,
                        "name": "daily-review",
                        "source": "workspace-custom",
                        "session": "isolated",
                        "enabled": True,
                        "schedule": {"expr": "0 9 * * *", "tz": "Asia/Shanghai"},
                        "payload": {"message": "请执行日报复盘"},
                    },
                ]
            }
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        raise AssertionError(f"unexpected openclaw CLI call: {args}")

    monkeypatch.setattr(svc, "_run_openclaw_cli", _fake_run_openclaw_cli)

    await svc.delete_agent("restore-cron-backup", mode="unregister", storage=storage)
    row = storage.openclaw_get("restore-cron-backup")
    assert row is not None
    snapshot = row.openclaw_config_snapshot
    assert isinstance(snapshot, dict)
    backup_jobs = snapshot.get(svc._CRON_BACKUP_SNAPSHOT_KEY)
    assert isinstance(backup_jobs, list)
    assert [item["name"] for item in backup_jobs] == ["daily-review"]
    assert snapshot.get(svc._CRON_BACKUP_CAPTURED_AT_KEY)


@pytest.mark.asyncio
async def test_restore_agent_registration_replays_custom_cron_backup(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(
        svc.CommitInput(id="restore-cron-replay", name="Restore Cron Replay"),
        user="u",
    )
    from app.storage import get_storage

    storage = get_storage()
    await svc.delete_agent("restore-cron-replay", mode="unregister", storage=storage)

    row = storage.openclaw_get("restore-cron-replay")
    assert row is not None
    row.openclaw_config_snapshot = {
        "identity": {"name": "Restore Cron Replay"},
        svc._CRON_BACKUP_SNAPSHOT_KEY: [
            {
                "name": "weekly-review",
                "cron_expr": "0 10 * * 1",
                "cron_tz": "Asia/Shanghai",
                "session": "isolated",
                "message": "请执行每周复盘",
                "enabled": True,
            }
        ],
    }
    storage.openclaw_update(row)

    cron_add_calls: list[list[str]] = []

    def _fake_run_openclaw_cli(*, args, config):
        del config
        if args[:2] == ["cron", "add"]:
            cron_add_calls.append(list(args))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")
        if args[:2] == ["cron", "list"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"jobs": []}),
                stderr="",
            )
        raise AssertionError(f"unexpected openclaw CLI call: {args}")

    monkeypatch.setattr(svc, "_run_openclaw_cli", _fake_run_openclaw_cli)
    monkeypatch.setattr(
        svc,
        "_schedule_default_entropy_management_task",
        lambda *, agent_id, config=None: True,
    )

    restored = await svc.restore_agent_registration("restore-cron-replay", user="u", storage=storage)
    assert restored.id == "restore-cron-replay"
    assert cron_add_calls
    first_add = cron_add_calls[0]
    assert "--name" in first_add
    assert first_add[first_add.index("--name") + 1] == "weekly-review"
    refreshed = storage.openclaw_get("restore-cron-replay")
    assert refreshed is not None
    snapshot = refreshed.openclaw_config_snapshot
    assert isinstance(snapshot, dict)
    assert snapshot.get(svc._CRON_BACKUP_RESTORED_AT_KEY)


@pytest.mark.asyncio
async def test_restore_agent_registration_edits_existing_same_name_cron(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(
        svc.CommitInput(id="restore-cron-edit", name="Restore Cron Edit"),
        user="u",
    )
    from app.storage import get_storage

    storage = get_storage()
    await svc.delete_agent("restore-cron-edit", mode="unregister", storage=storage)

    row = storage.openclaw_get("restore-cron-edit")
    assert row is not None
    row.openclaw_config_snapshot = {
        "identity": {"name": "Restore Cron Edit"},
        svc._CRON_BACKUP_SNAPSHOT_KEY: [
            {
                "name": "weekly-review",
                "cron_expr": "0 10 * * 1",
                "cron_tz": "Asia/Shanghai",
                "session": "isolated",
                "message": "请执行每周复盘",
                "enabled": True,
            }
        ],
    }
    storage.openclaw_update(row)

    calls: list[list[str]] = []

    def _fake_run_openclaw_cli(*, args, config):
        del config
        calls.append(list(args))
        if args[:2] == ["cron", "list"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-existing",
                                "agentId": "restore-cron-edit",
                                "name": "weekly-review",
                            }
                        ]
                    }
                ),
                stderr="",
            )
        if args[:2] == ["cron", "edit"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")
        if args[:2] == ["cron", "add"]:
            raise AssertionError("should not add duplicate cron job when same-name exists")
        raise AssertionError(f"unexpected openclaw CLI call: {args}")

    monkeypatch.setattr(svc, "_run_openclaw_cli", _fake_run_openclaw_cli)
    monkeypatch.setattr(
        svc,
        "_schedule_default_entropy_management_task",
        lambda *, agent_id, config=None: True,
    )

    restored = await svc.restore_agent_registration("restore-cron-edit", user="u", storage=storage)
    assert restored.id == "restore-cron-edit"
    assert any(call[:2] == ["cron", "edit"] for call in calls)
    assert not any(call[:2] == ["cron", "add"] for call in calls)


@pytest.mark.asyncio
async def test_restore_all_agent_registrations_re_registers_all_candidates(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="restore-all-a", name="A"), user="u")
    await svc.commit_agent(svc.CommitInput(id="restore-all-b", name="B"), user="u")
    await svc.delete_agent("restore-all-a", mode="unregister")
    await svc.delete_agent("restore-all-b", mode="unregister")

    restored, failed = await svc.restore_all_agent_registrations(user="u")
    assert set(restored) >= {"restore-all-a", "restore-all-b"}
    assert failed == {}
    assert oj.find_agent("restore-all-a") is not None
    assert oj.find_agent("restore-all-b") is not None


def test_probe_runtime_running_reports_cli_missing() -> None:
    # When the gateway URL is empty, strict falls back to the CLI health check;
    # if that CLI is also missing the probe reports ``cli_missing``.
    cfg = load_config().model_copy(update={"openclaw_gateway_url": ""})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            svc,
            "_probe_default_openclaw_gateway_health",
            lambda **_kwargs: (False, "gateway_unreachable"),
        )
        mp.setattr(svc, "_gateway_url_candidates", lambda **_kwargs: [])
        mp.setattr(svc, "resolve_openclaw_executable", lambda: None)
        ok, reason = svc.probe_runtime_running(config=cfg)
    assert ok is False
    assert reason == "cli_missing"


def test_resolve_runtime_gateway_url_prefers_openclaw_json_port_when_config_is_default(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (fake_openclaw_home / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {}, "list": []},
                "gateway": {"port": 19999, "auth": {"token": "T"}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        svc,
        "_probe_default_openclaw_gateway_health",
        lambda **_kwargs: (False, "gateway_unreachable"),
    )
    cfg = load_config().model_copy(update={"openclaw_gateway_url": "http://127.0.0.1:18789"})
    assert svc.resolve_runtime_gateway_url(config=cfg) == "http://127.0.0.1:19999"


def test_resolve_runtime_gateway_url_prefers_explicit_config_override(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (fake_openclaw_home / "openclaw.json").write_text(
        json.dumps(
            {
                "agents": {"defaults": {}, "list": []},
                "gateway": {"port": 19999, "auth": {"token": "T"}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        svc,
        "_probe_default_openclaw_gateway_health",
        lambda **_kwargs: (False, "gateway_unreachable"),
    )
    cfg = load_config().model_copy(update={"openclaw_gateway_url": "http://127.0.0.1:21111"})
    assert svc.resolve_runtime_gateway_url(config=cfg) == "http://127.0.0.1:21111"


def test_resolve_runtime_gateway_url_prefers_default_port_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config().model_copy(update={"openclaw_gateway_url": "http://127.0.0.1:21111"})
    monkeypatch.setattr(svc, "_probe_default_openclaw_gateway_health", lambda **_kwargs: (True, "ok"))
    assert svc.resolve_runtime_gateway_url(config=cfg) == "http://127.0.0.1:18789"


def test_probe_runtime_running_fast_returns_immediately_when_default_port_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc, "_probe_default_openclaw_gateway_health", lambda **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        svc,
        "_probe_runtime_running_strict_with_config",
        lambda **_kw: (_ for _ in ()).throw(
            AssertionError("strict probe should not run when default gateway is healthy")
        ),
    )
    ok, reason = svc.probe_runtime_running(config=load_config())
    assert ok is True
    assert reason == "ok"


def test_probe_runtime_running_reports_ok_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strict path: default-port pre-check fails so we fall through to the HTTP
    # gateway health probe. A successful ``{"ok": true}`` body yields ``ok``.
    monkeypatch.setattr(
        svc,
        "_probe_default_openclaw_gateway_health",
        lambda **_kwargs: (False, "gateway_unreachable"),
    )
    monkeypatch.setattr(
        svc,
        "_probe_openclaw_gateway_health",
        lambda **_kwargs: (True, "ok"),
    )
    ok, reason = svc.probe_runtime_running(config=load_config())
    assert ok is True
    assert reason == "ok"


def test_probe_runtime_running_retries_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HTTP probe times out on the first call, then succeeds on the
    # retry — strict probe should follow through and report ``ok``.
    monkeypatch.setattr(
        svc,
        "_probe_default_openclaw_gateway_health",
        lambda **_kwargs: (False, "gateway_unreachable"),
    )
    calls = {"count": 0}

    def _fake_health(**_kwargs: object) -> tuple[bool, str]:
        calls["count"] += 1
        if calls["count"] == 1:
            return False, "timeout"
        return True, "ok"

    monkeypatch.setattr(svc, "_probe_openclaw_gateway_health", _fake_health)
    ok, reason = svc.probe_runtime_running(config=load_config(), timeout_sec=0.2)
    assert ok is True
    assert reason == "ok"
    assert calls["count"] == 2


def test_probe_runtime_running_uses_strict_fallback_when_default_port_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc, "_probe_default_openclaw_gateway_health", lambda **_kwargs: (False, "gateway_unreachable"))
    calls = {"strict": 0}

    def _fake_strict(**_kwargs: object) -> tuple[bool, str]:
        calls["strict"] += 1
        return False, "not_running"

    monkeypatch.setattr(svc, "_probe_runtime_running_strict_with_config", _fake_strict)
    ok, reason = svc.probe_runtime_running(config=load_config())
    assert ok is False
    assert reason == "not_running"
    assert calls["strict"] == 1


def test_probe_runtime_running_strict_prefers_default_gateway_when_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc, "_probe_default_openclaw_gateway_health", lambda **_kwargs: (True, "ok"))
    monkeypatch.setattr(
        svc,
        "_probe_openclaw_gateway_health",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict fallback should not run when default gateway is healthy")
        ),
    )
    ok, reason = svc.probe_runtime_running_strict(config=load_config())
    assert ok is True
    assert reason == "ok"


def test_get_missing_raises(fake_openclaw_home: Path) -> None:
    with pytest.raises(svc.AgentNotFound):
        svc.get_agent("nope")


@pytest.mark.asyncio
async def test_list_filters_by_user(fake_openclaw_home: Path) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="alice1", name="A"), user="alice")
    await svc.commit_agent(svc.CommitInput(id="bob1", name="B"), user="bob")
    alice_only = svc.list_agents(user="alice")
    assert {a.id for a in alice_only} == {"alice1"}
    everyone = svc.list_agents()
    assert {a.id for a in everyone} == {"alice1", "bob1"}


@pytest.mark.asyncio
async def test_list_excludes_unregistered_agents_but_restore_candidates_keep_them(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    await svc.commit_agent(svc.CommitInput(id="hidden-after-unregister", name="Hide Me"), user="alice")
    await svc.delete_agent("hidden-after-unregister", mode="unregister")

    listed = svc.list_agents(user="alice")
    assert "hidden-after-unregister" not in {a.id for a in listed}

    restorable = svc.list_restorable_agents(user="alice")
    assert "hidden-after-unregister" in {a.id for a in restorable}


def test_list_external_import_candidates_excludes_managed(
    fake_openclaw_home: Path,
    tmp_path: Path,
) -> None:
    unmanaged_ws = tmp_path / "external-ws"
    unmanaged_ws.mkdir(parents=True, exist_ok=True)
    _seed_unmanaged_runtime_agent(
        openclaw_home=fake_openclaw_home,
        agent_id="legacy-importable",
        name="Legacy Importable",
        workspace=unmanaged_ws,
    )
    managed_id = "managed-existing"
    managed_ws = paths.agent_dir(managed_id) / "workspace"
    managed_ws.mkdir(parents=True, exist_ok=True)
    _seed_managed_openclaw_entry_without_db(
        openclaw_home=fake_openclaw_home,
        agent_id=managed_id,
        workspace=managed_ws,
    )

    items = svc.list_external_import_candidates()
    ids = {item.id for item in items}
    assert "legacy-importable" in ids
    assert managed_id not in ids


def test_list_external_import_candidates_excludes_imported_source_id_by_prefixed_managed_id(
    fake_openclaw_home: Path,
    tmp_path: Path,
) -> None:
    source_ws = tmp_path / "legacy-prefixed-source"
    source_ws.mkdir(parents=True, exist_ok=True)
    _seed_unmanaged_runtime_agent(
        openclaw_home=fake_openclaw_home,
        agent_id="legacy-prefixed-source",
        name="Legacy Prefixed Source",
        workspace=source_ws,
    )
    managed_prefixed_id = "csflow-legacy-prefixed-source"
    managed_ws = paths.agent_dir(managed_prefixed_id) / "workspace"
    managed_ws.mkdir(parents=True, exist_ok=True)
    _seed_managed_openclaw_entry_without_db(
        openclaw_home=fake_openclaw_home,
        agent_id=managed_prefixed_id,
        workspace=managed_ws,
    )

    items = svc.list_external_import_candidates()
    ids = {item.id for item in items}
    assert "legacy-prefixed-source" not in ids


@pytest.mark.asyncio
async def test_import_external_agent_copies_workspace_and_wraps_agents_md(
    fake_openclaw_home: Path,
    tmp_path: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    source_ws = tmp_path / "legacy-source"
    source_ws.mkdir(parents=True, exist_ok=True)
    (source_ws / "notes.md").write_text("legacy\n", encoding="utf-8")
    (source_ws / "AGENTS.md").write_text(
        "# Legacy Rules\n\n- old custom line\n",
        encoding="utf-8",
    )
    _seed_unmanaged_runtime_agent(
        openclaw_home=fake_openclaw_home,
        agent_id="legacy-source",
        name="Legacy Source",
        workspace=source_ws,
        description="legacy-desc",
    )

    imported = await svc.import_external_agent("legacy-source", user="alice")
    assert imported.source_agent_id == "legacy-source"
    assert imported.target_agent_id == "csflow-legacy-source"
    assert imported.target_agent_name == "csflow-Legacy Source"
    ws = Path(imported.target_workspace_path)
    assert (ws / "notes.md").read_text(encoding="utf-8") == "legacy\n"
    text = (ws / "AGENTS.md").read_text(encoding="utf-8")
    assert "Shared Rules for ClawsomeFlow Managed Agents" in text
    assert "Legacy Rules" in text
    assert "AGENTS_USER_CUSTOM_SECTION" in text
    assert (ws / "skills" / "self-definition-maintenance" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_reinstall_skills_redeploys_template_materials(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    agent = await svc.commit_agent(svc.CommitInput(id="retrofit", name="Retro"), user="u")
    ws = Path(agent.workspace_path)
    # Simulate drift: user removed AGENTS.md.
    (ws / "AGENTS.md").unlink()
    shutil.rmtree(ws / "skills" / "self-definition-maintenance")
    installed = svc.reinstall_skills("retrofit")
    assert "self-definition-maintenance" in installed
    assert (ws / "AGENTS.md").exists()
    assert (ws / "skills" / "self-definition-maintenance" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_reinstall_skills_updates_common_and_preserves_custom_agents_md(
    fake_openclaw_home: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    agent = await svc.commit_agent(svc.CommitInput(id="keep-agents", name="Keep"), user="u")
    ws = Path(agent.workspace_path)
    (ws / "AGENTS.md").write_text(
        "\n".join([
            "# 旧通用规则",
            "",
            "<!-- AGENTS_USER_CUSTOM_SECTION_START -->",
            "## AGENTS_USER_CUSTOM_SECTION",
            "",
            "- custom-agents",
            "<!-- AGENTS_USER_CUSTOM_SECTION_END -->",
            "",
        ]),
        encoding="utf-8",
    )
    svc.reinstall_skills("keep-agents")
    text = (ws / "AGENTS.md").read_text(encoding="utf-8")
    assert "旧通用规则" not in text
    assert "Shared Rules for ClawsomeFlow Managed Agents" in text
    assert "custom-agents" in text
