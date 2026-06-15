"""Tests for the AI task-decompose feature.

Covers four layers:
    1. Service (`start_decompose_request`, status transitions, reap)
    2. Public API (POST /api/flows/decompose, GET .../{request_id})
    3. Internal API (commit + fail; loopback + token + purpose check)
    4. Self-contained dispatch prompt (no csflow-task-decomposer skill; the
       prompt carries the persistent/temporary owner model + result-delivery)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import paths
from app.config import load_config, save_config
from app.integrations import internal_token as it
from app.integrations import openclaw_json as oj
from app.integrations.openclaw_bridge import OpenclawGatewayUnavailable
from app.main import create_app
from app.models import AgentKind, OpenclawAgent, TaskDecomposeStatus
from app.services import task_decompose as svc
from app.storage import get_storage


# ── shared fixtures ----------------------------------------------------


def _has_git() -> bool:
    return shutil.which("git") is not None


@pytest.fixture
def fake_openclaw_home(tmp_path: Path) -> Path:
    oc = tmp_path / "openclaw_home"
    oc.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(update={
        "openclaw_home": str(oc),
        "internal_token_secret": "test-secret",
        "default_user": "alice",
    })
    save_config(cfg)
    (oc / "openclaw.json").write_text(json.dumps({
        "agents": {"defaults": {}, "list": []},
        "gateway": {"port": 18789, "auth": {"token": "T"}},
    }))
    return oc


@pytest.fixture
def app_client(fake_openclaw_home: Path):
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def disable_decompose_cli_dispatch(monkeypatch: pytest.MonkeyPatch):
    """Service tests default to bridge stubs unless a test opts into CLI path."""
    monkeypatch.setattr(svc, "_resolve_openclaw_executable", lambda: None)


def _seed_openclaw_agent(agent_id: str = "leader-bot",
                         user: str = "alice") -> OpenclawAgent:
    storage = get_storage()
    agent = OpenclawAgent(
        id=agent_id, name=agent_id.title(),
        workspace_path=f"/tmp/{agent_id}",
        created_by_user=user,
    )
    return storage.openclaw_create(agent)


def _seed_registered_openclaw_entry_without_db(
    agent_id: str = "leader-json",
    *,
    write_registry: bool = True,
) -> None:
    cfg = load_config()
    oc_home = Path(cfg.openclaw_home).expanduser()
    oc_home.mkdir(parents=True, exist_ok=True)
    workspace = paths.agent_dir(agent_id) / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (oc_home / "openclaw.json").write_text(json.dumps({
        "agents": {
            "defaults": {},
            "list": [{
                "id": agent_id,
                "name": agent_id.title(),
                "workspace": str(workspace),
                "default": False,
            }],
        },
        "gateway": {"port": 18789, "auth": {"token": "T"}},
    }), encoding="utf-8")
    if not write_registry:
        return
    registry = oj.managed_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"agent_ids": [agent_id]}), encoding="utf-8")


# ── ① skill removed + self-contained dispatch prompt -----------------


def test_decomposer_skill_removed_from_repo() -> None:
    repo = Path(__file__).resolve().parents[2]
    skill_dir = (
        repo
        / "openclaw-agent-source"
        / "common-agent-source"
        / "skills"
        / "csflow-task-decomposer"
    )
    assert not skill_dir.exists()


def _compose_body(*, kind: AgentKind, persistent=None, platforms=None) -> str:
    target = svc._LeaderTarget(
        id="leader-x", kind=kind,
        repo=None if kind == AgentKind.openclaw else "/tmp/repo",
        target_branch="main",
    )
    msgs = svc._compose_messages(
        request_id="req-1", user="alice", goal="Build a newsletter pipeline.",
        leader_target=target, api_base="http://127.0.0.1:17017", token="tok-123",
        result_language="en",
        persistent_agents=persistent or [],
        available_platforms=platforms or [],
        temp_workdir="~/csflow-ai-decompose",
        existing_agents=[], existing_tasks=[],
    )
    return msgs[0]["content"]


def test_prompt_lists_persistent_agents_and_platforms() -> None:
    body = _compose_body(
        kind=AgentKind.openclaw,
        persistent=[
            {"id": "writer", "name": "Writer", "kind": "openclaw", "isLeader": False},
            {"id": "sage", "name": "Sage", "kind": "hermes", "isLeader": False},
        ],
        platforms=["claude", "hermes"],
    )
    # Persistent owner source (OpenClaw + Hermes) listed.
    assert "Persistent agents you may assign" in body
    assert "id=writer" in body and "kind=openclaw" in body
    assert "id=sage" in body and "kind=hermes" in body
    assert "non-OpenClaw persistent agent" in body
    assert "repo` to `~/csflow-ai-decompose` by default" in body
    # Available temporary platforms (probed) listed.
    assert "Temporary-agent platforms available" in body
    assert "  - claude" in body and "  - hermes" in body
    # Temporary fallback + default workdir + never-openclaw rule.
    assert "~/csflow-ai-decompose" in body
    assert "NEVER be a temporary agent" in body


def test_openclaw_delivery_uses_curl_callback() -> None:
    body = _compose_body(kind=AgentKind.openclaw)
    # OpenClaw stdout is not read → it must curl the result back.
    assert "/api/internal/task-decompose/commit" in body
    assert "/api/internal/task-decompose/fail" in body
    assert "Authorization: Bearer tok-123" in body
    assert "req-1" in body


def test_non_openclaw_delivery_uses_stdout_json() -> None:
    body = _compose_body(kind=AgentKind.claude, platforms=["claude"])
    # Non-OpenClaw stdout is captured → emit one JSON object, never curl.
    assert "exactly one JSON object" in body
    assert "do **not**" in body
    assert "/api/internal/task-decompose/commit" not in body


def test_ensure_ai_temp_agent_workdir_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    if not _has_git():
        pytest.skip("git not available")
    wd = tmp_path / "csflow-ai-decompose"
    monkeypatch.setattr(svc, "_AI_TEMP_AGENT_WORKDIR", str(wd))
    out = svc._ensure_ai_temp_agent_workdir()
    assert out == str(wd)
    assert (wd / ".git").is_dir()
    head = shutil.which("git") and __import__("subprocess").run(
        ["git", "-C", str(wd), "rev-parse", "HEAD"], capture_output=True,
    )
    assert head.returncode == 0  # has at least one commit
    # Second call is a no-op and must not raise.
    svc._ensure_ai_temp_agent_workdir()
    assert (wd / ".git").is_dir()


@pytest.mark.asyncio
async def test_resolve_leader_target_temporary_hermes_uses_default_profile() -> None:
    """A temporary Hermes leader (no managed profile) must resolve to
    is_temporary=True and dispatch with NO ``-p`` so it runs under the default
    profile — never ``hermes -p <id>`` which fails 'Profile does not exist'."""

    class _St:
        def hermes_get(self, _aid):
            return None  # not a registered/managed Hermes agent

    target = await svc._resolve_leader_target(
        leader_agent_id="777", user="alice", storage=_St(), config=None,
        existing_agents=[{
            "id": "777", "kind": "hermes", "repo": "/tmp/x",
            "isLeader": True, "isTemporary": True,
        }],
        leader_kind="hermes", leader_repo="/tmp/x", leader_target_branch="main",
    )
    assert target.kind == AgentKind.hermes
    assert target.is_temporary is True
    argv = svc._non_openclaw_dispatch_argv(
        kind=target.kind, message="hi",
        profile=None if target.is_temporary else target.id,
    )
    # No -p (default profile) + --ignore-rules so the operator's personal
    # SOUL/memory does not bias the decomposition toward reusing agents.
    assert argv == ["hermes", "--yolo", "--ignore-rules", "-z", "hi"]


@pytest.mark.asyncio
async def test_resolve_leader_target_registered_hermes_keeps_profile() -> None:
    """A registered/managed Hermes leader keeps its ``-p <id>`` profile binding."""

    class _St:
        def hermes_get(self, _aid):
            return object()  # a managed Hermes agent row exists

    target = await svc._resolve_leader_target(
        leader_agent_id="sage", user="alice", storage=_St(), config=None,
        existing_agents=[{
            "id": "sage", "kind": "hermes", "repo": "/tmp/x", "isLeader": True,
        }],
        leader_kind="hermes", leader_repo="/tmp/x", leader_target_branch="main",
    )
    assert target.is_temporary is False
    argv = svc._non_openclaw_dispatch_argv(
        kind=target.kind, message="hi",
        profile=None if target.is_temporary else target.id,
    )
    assert argv == ["hermes", "--yolo", "-p", "sage", "-z", "hi"]


# ── ② service layer --------------------------------------------------


class _FakeBridge:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}
        self.aclose_called = False

    async def chat_completion(self, *, agent_id, messages,
                              session_key=None, **kw):
        self.captured = dict(
            agent_id=agent_id, messages=messages, session_key=session_key,
        )
        return {"choices": [{"message": {"content": "ok"}}]}

    async def aclose(self):
        self.aclose_called = True


class _FailingBridge(_FakeBridge):
    async def chat_completion(self, **kw):
        raise OpenclawGatewayUnavailable("simulated gateway down")


@pytest.mark.asyncio
async def test_start_request_persists_and_dispatches(fake_openclaw_home: Path) -> None:
    _seed_openclaw_agent("leader-1")
    fake = _FakeBridge()
    res = await svc.start_decompose_request(
        goal="Build a daily newsletter pipeline.",
        leader_agent_id="leader-1",
        user="alice",
        api_base="http://127.0.0.1:17017",
        bridge_factory=lambda cfg: fake,
        background=False,
    )
    assert res.status == TaskDecomposeStatus.pending
    row = svc.get_request(res.request_id)
    assert row.status == TaskDecomposeStatus.dispatched
    assert row.leader_agent_id == "leader-1"
    assert row.goal.startswith("Build a daily newsletter")
    # System prompt must contain the trigger string + token + leader id.
    msg = fake.captured["messages"][0]["content"]
    assert "## ClawsomeFlow Task Decomposition Request" in msg
    assert "leader-1" in msg
    assert res.request_id in msg
    assert "http://127.0.0.1:17017" in msg
    assert fake.aclose_called


@pytest.mark.asyncio
async def test_start_reuses_existing_active_request_for_same_payload(
    fake_openclaw_home: Path,
) -> None:
    _seed_openclaw_agent("leader-dedupe")
    calls = {"bridge": 0}

    class _CountingBridge(_FakeBridge):
        async def chat_completion(self, **kw):
            calls["bridge"] += 1
            return await super().chat_completion(**kw)

    payload = dict(
        goal="Build a dedupe-safe flow.",
        leader_agent_id="leader-dedupe",
        user="alice",
        api_base="http://127.0.0.1:17017",
        bridge_factory=lambda cfg: _CountingBridge(),
        existing_agents=[{"id": "leader-dedupe", "kind": "openclaw", "isLeader": True}],
        existing_tasks=[{"id": "sum", "ownerAgentId": "leader-dedupe", "isLeaderSummary": True}],
        background=False,
    )
    first = await svc.start_decompose_request(**payload)
    second = await svc.start_decompose_request(**payload)

    assert first.request_id == second.request_id
    assert second.status == TaskDecomposeStatus.dispatched
    assert calls["bridge"] == 1


@pytest.mark.asyncio
async def test_start_concurrent_duplicate_requests_dispatch_only_once(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_openclaw_agent("leader-race")
    dispatch_entered = asyncio.Event()
    dispatch_release = asyncio.Event()
    dispatch_calls = 0

    async def _fake_dispatch(**kw):
        nonlocal dispatch_calls
        del kw
        dispatch_calls += 1
        dispatch_entered.set()
        await dispatch_release.wait()

    monkeypatch.setattr(svc, "_dispatch_to_leader", _fake_dispatch)

    payload = dict(
        goal="dedupe race",
        leader_agent_id="leader-race",
        user="alice",
        api_base="http://127.0.0.1:17017",
        background=False,
    )
    first_task = asyncio.create_task(svc.start_decompose_request(**payload))
    await asyncio.wait_for(dispatch_entered.wait(), timeout=1.0)
    second = await asyncio.wait_for(svc.start_decompose_request(**payload), timeout=1.0)
    dispatch_release.set()
    first = await asyncio.wait_for(first_task, timeout=1.0)

    assert first.request_id == second.request_id
    assert dispatch_calls == 1
    storage = get_storage()
    rows, _ = storage.task_decompose_list(user="alice", limit=20)
    same_goal_rows = [
        row for row in rows
        if row.leader_agent_id == "leader-race" and row.goal == "dedupe race"
    ]
    assert len(same_goal_rows) == 1


@pytest.mark.asyncio
async def test_start_refuses_unknown_leader(fake_openclaw_home: Path) -> None:
    with pytest.raises(svc.LeaderAgentNotFound):
        await svc.start_decompose_request(
            goal="x", leader_agent_id="ghost", user="alice",
            api_base="http://x", bridge_factory=lambda cfg: _FakeBridge(),
            background=False,
        )


@pytest.mark.asyncio
async def test_start_accepts_registered_leader_without_db_row(
    fake_openclaw_home: Path,
) -> None:
    _seed_registered_openclaw_entry_without_db("leader-json")
    fake = _FakeBridge()
    res = await svc.start_decompose_request(
        goal="Build an RSS triage flow.",
        leader_agent_id="leader-json",
        user="alice",
        api_base="http://127.0.0.1:17017",
        bridge_factory=lambda cfg: fake,
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.dispatched


@pytest.mark.asyncio
async def test_start_accepts_registered_leader_without_registry_row(
    fake_openclaw_home: Path,
) -> None:
    _seed_registered_openclaw_entry_without_db(
        "leader-json-no-registry",
        write_registry=False,
    )
    fake = _FakeBridge()
    res = await svc.start_decompose_request(
        goal="Build a docs QA flow.",
        leader_agent_id="leader-json-no-registry",
        user="alice",
        api_base="http://127.0.0.1:17017",
        bridge_factory=lambda cfg: fake,
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.dispatched


@pytest.mark.asyncio
async def test_start_refuses_leader_owned_by_other_user(fake_openclaw_home: Path) -> None:
    _seed_openclaw_agent("leader-foreign", user="bob")
    with pytest.raises(svc.LeaderAgentForbidden):
        await svc.start_decompose_request(
            goal="x", leader_agent_id="leader-foreign", user="alice",
            api_base="http://x", bridge_factory=lambda cfg: _FakeBridge(),
            background=False,
        )


@pytest.mark.asyncio
async def test_start_dispatches_non_openclaw_leader_via_direct_cli(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-claude-repo")
    repo_path.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            payload = {
                "agents": [{
                    "id": "leader-claude",
                    "kind": "claude",
                    "repo": str(repo_path),
                    "targetBranch": "main",
                    "isLeader": True,
                }],
                "tasks": [{
                    "id": "summary",
                    "ownerAgentId": "leader-claude",
                    "subject": "Summary",
                    "description": "Produce final summary.",
                    "dependsOn": [],
                    "isLeaderSummary": True,
                }],
            }
            return json.dumps(payload).encode("utf-8"), b""

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="Build a QA review flow.",
        leader_agent_id="leader-claude",
        leader_kind="claude",
        leader_repo=str(repo_path),
        leader_target_branch="main",
        user="alice",
        api_base="http://127.0.0.1:17017",
        existing_agents=[{
            "id": "leader-claude",
            "kind": "claude",
            "repo": str(repo_path),
            "targetBranch": "main",
            "isLeader": True,
        }],
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.succeeded
    assert row.result_tasks is not None
    assert row.result_tasks[0]["ownerAgentId"] == "leader-claude"

    argv = list(seen["argv"])
    assert argv[0] == "claude"
    assert "--permission-mode" in argv
    assert "bypassPermissions" in argv
    assert "--dangerously-skip-permissions" in argv
    assert "-p" in argv
    msg = str(argv[-1])
    assert "exactly one JSON object" in msg
    assert "/api/internal/task-decompose/commit" not in msg  # stdout, not curl
    assert "leader-claude" in msg
    assert seen["cwd"] == str(repo_path)


@pytest.mark.asyncio
async def test_non_openclaw_leader_dispatch_expands_tilde_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS regression: a leader repo like ``~/342test`` must be expanded before
    it becomes the (shell-less) subprocess cwd."""
    from app.models import AgentKind

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "342test").mkdir()
    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            payload = {
                "agents": [{
                    "id": "L", "kind": "claude",
                    "repo": str(tmp_path / "342test"),
                    "targetBranch": "main", "isLeader": True,
                }],
                "tasks": [{
                    "id": "s", "ownerAgentId": "L", "subject": "x",
                    "description": "d", "dependsOn": [], "isLeaderSummary": True,
                }],
            }
            return json.dumps(payload).encode("utf-8"), b""

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        del argv
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    await svc._dispatch_to_non_openclaw_leader_via_cli(
        request_id="req-tilde",
        leader_target=svc._LeaderTarget(
            id="L", kind=AgentKind.claude, repo="~/342test", target_branch="main",
        ),
        message="decompose this",
    )
    assert seen["cwd"] == str(tmp_path / "342test")


@pytest.mark.asyncio
async def test_start_dispatches_cursor_leader_with_force_flags(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-cursor-repo")
    repo_path.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            payload = {
                "agents": [{
                    "id": "leader-cursor",
                    "kind": "cursor",
                    "repo": str(repo_path),
                    "targetBranch": "main",
                    "isLeader": True,
                }],
                "tasks": [{
                    "id": "summary",
                    "ownerAgentId": "leader-cursor",
                    "subject": "Summary",
                    "description": "Produce final summary.",
                    "dependsOn": [],
                    "isLeaderSummary": True,
                }],
            }
            return json.dumps(payload).encode("utf-8"), b""

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="Build a QA review flow.",
        leader_agent_id="leader-cursor",
        leader_kind="cursor",
        leader_repo=str(repo_path),
        leader_target_branch="main",
        user="alice",
        api_base="http://127.0.0.1:17017",
        existing_agents=[{
            "id": "leader-cursor",
            "kind": "cursor",
            "repo": str(repo_path),
            "targetBranch": "main",
            "isLeader": True,
        }],
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.succeeded
    assert row.result_tasks is not None
    assert row.result_tasks[0]["ownerAgentId"] == "leader-cursor"

    argv = list(seen["argv"])
    assert argv[:5] == [
        "agent",
        "--force",
        "--approve-mcps",
        "--sandbox",
        "disabled",
    ]
    assert argv[5] == "-p"
    msg = str(argv[-1])
    assert "exactly one JSON object" in msg
    assert "/api/internal/task-decompose/commit" not in msg  # stdout, not curl
    assert "leader-cursor" in msg
    assert seen["cwd"] == str(repo_path)


@pytest.mark.asyncio
async def test_start_dispatches_codex_leader_with_exec_flags(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-codex-repo")
    repo_path.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            payload = {
                "agents": [{
                    "id": "leader-codex",
                    "kind": "codex",
                    "repo": str(repo_path),
                    "targetBranch": "main",
                    "isLeader": True,
                }],
                "tasks": [{
                    "id": "summary",
                    "ownerAgentId": "leader-codex",
                    "subject": "Summary",
                    "description": "Produce final summary.",
                    "dependsOn": [],
                    "isLeaderSummary": True,
                }],
            }
            return json.dumps(payload).encode("utf-8"), b""

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="Build a QA review flow.",
        leader_agent_id="leader-codex",
        leader_kind="codex",
        leader_repo=str(repo_path),
        leader_target_branch="main",
        user="alice",
        api_base="http://127.0.0.1:17017",
        existing_agents=[{
            "id": "leader-codex",
            "kind": "codex",
            "repo": str(repo_path),
            "targetBranch": "main",
            "isLeader": True,
        }],
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.succeeded
    assert row.result_tasks is not None
    assert row.result_tasks[0]["ownerAgentId"] == "leader-codex"

    argv = list(seen["argv"])
    assert argv[:3] == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "exec",
    ]
    assert "claude" not in argv[0]
    msg = str(argv[-1])
    assert "exactly one JSON object" in msg
    assert "/api/internal/task-decompose/commit" not in msg  # stdout, not curl
    assert "leader-codex" in msg
    assert seen["cwd"] == str(repo_path)


@pytest.mark.asyncio
async def test_start_non_openclaw_marks_failed_on_invalid_proposal_output(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-claude-invalid")
    repo_path.mkdir(parents=True, exist_ok=True)

    class _Proc:
        returncode = 0

        async def communicate(self):
            # Missing tasks[] -> should fail schema/validation in service.
            return b'{"agents":[{"id":"leader-claude-invalid","kind":"claude"}]}', b""

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        return _Proc()

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="Build a QA review flow.",
        leader_agent_id="leader-claude-invalid",
        leader_kind="claude",
        leader_repo=str(repo_path),
        user="alice",
        api_base="http://127.0.0.1:17017",
        existing_agents=[{
            "id": "leader-claude-invalid",
            "kind": "claude",
            "repo": str(repo_path),
            "isLeader": True,
        }],
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "DISPATCH_ERROR"
    assert "missing list field 'tasks'" in (row.error_message or "")


@pytest.mark.asyncio
async def test_start_non_openclaw_leader_requires_repo(
    fake_openclaw_home: Path,
) -> None:
    with pytest.raises(svc.LeaderContextMissing):
        await svc.start_decompose_request(
            goal="x",
            leader_agent_id="leader-claude",
            leader_kind="claude",
            leader_repo="",
            user="alice",
            api_base="http://x",
            bridge_factory=lambda cfg: _FailingBridge(),
            background=False,
        )


@pytest.mark.asyncio
async def test_start_non_openclaw_marks_failed_when_cli_missing(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-claude-missing")
    repo_path.mkdir(parents=True, exist_ok=True)

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        raise FileNotFoundError("missing")

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="x",
        leader_agent_id="leader-claude-missing",
        leader_kind="claude",
        leader_repo=str(repo_path),
        user="alice",
        api_base="http://127.0.0.1:17017",
        existing_agents=[{
            "id": "leader-claude-missing",
            "kind": "claude",
            "repo": str(repo_path),
            "isLeader": True,
        }],
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "DISPATCH_ERROR"
    assert "not available in PATH" in (row.error_message or "")


@pytest.mark.asyncio
async def test_start_marks_failed_on_bridge_error(fake_openclaw_home: Path) -> None:
    _seed_openclaw_agent("leader-2")
    res = await svc.start_decompose_request(
        goal="x", leader_agent_id="leader-2", user="alice",
        api_base="http://x", bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "BRIDGE_ERROR"
    assert "simulated" in row.error_message


@pytest.mark.asyncio
async def test_start_dispatches_via_cli_before_bridge(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_openclaw_agent("leader-cli")
    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'{"ok":true}', b""

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        del kwargs
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(svc, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="cli dispatch",
        leader_agent_id="leader-cli",
        user="alice",
        api_base="http://127.0.0.1:17017",
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row.status == TaskDecomposeStatus.dispatched
    assert seen["argv"][0] == "/tmp/openclaw"
    assert "--agent" in seen["argv"]
    assert "leader-cli" in seen["argv"]
    assert "--timeout" in seen["argv"]


@pytest.mark.asyncio
async def test_start_marks_failed_when_cli_and_bridge_both_fail(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_openclaw_agent("leader-cli-fail")

    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"cli exploded"

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        return _Proc()

    monkeypatch.setattr(svc, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="x",
        leader_agent_id="leader-cli-fail",
        user="alice",
        api_base="http://127.0.0.1:17017",
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "DISPATCH_ERROR"
    assert "CLI dispatch failed" in row.error_message
    assert "bridge fallback failed" in row.error_message


@pytest.mark.asyncio
async def test_start_cli_dispatch_auto_repairs_scope_pending(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_openclaw_agent("leader-cli-scope")
    calls = {"spawn": 0, "repair": 0}

    class _Proc:
        def __init__(self, *, returncode: int, stdout: str = "", stderr: str = "") -> None:
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
            stderr="GatewayClientRequestError: missing scope: operator.admin",
        ),
        _Proc(returncode=0, stdout='{"ok":true}'),
    ]

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        idx = calls["spawn"]
        calls["spawn"] += 1
        return procs[idx]

    def _fake_repair_pending(*, config):
        del config
        calls["repair"] += 1
        return ["req-op-admin"]

    monkeypatch.setattr(svc, "_resolve_openclaw_executable", lambda: "/tmp/openclaw")
    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(svc, "repair_pending_scope_upgrades", _fake_repair_pending)

    res = await svc.start_decompose_request(
        goal="auto repair scope",
        leader_agent_id="leader-cli-scope",
        user="alice",
        api_base="http://127.0.0.1:17017",
        bridge_factory=lambda cfg: _FailingBridge(),
        background=False,
    )
    row = svc.get_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.dispatched
    assert calls["repair"] == 1
    assert calls["spawn"] == 2


@pytest.mark.asyncio
async def test_empty_goal_rejected(fake_openclaw_home: Path) -> None:
    _seed_openclaw_agent("leader-3")
    with pytest.raises(svc.TaskDecomposeError):
        await svc.start_decompose_request(
            goal="   ", leader_agent_id="leader-3", user="alice",
            api_base="http://x", bridge_factory=lambda cfg: _FakeBridge(),
            background=False,
        )


@pytest.mark.asyncio
async def test_mark_succeeded_persists_result(fake_openclaw_home: Path) -> None:
    _seed_openclaw_agent("leader-4")
    res = await svc.start_decompose_request(
        goal="x", leader_agent_id="leader-4", user="alice",
        api_base="http://x", bridge_factory=lambda cfg: _FakeBridge(),
        background=False,
    )
    out = svc.mark_request_succeeded(
        res.request_id,
        agents=[{"id": "w1", "kind": "openclaw", "isLeader": False}],
        tasks=[{"id": "t1", "ownerAgentId": "w1", "subject": "x"}],
    )
    assert out.status == TaskDecomposeStatus.succeeded
    assert out.result_tasks[0]["id"] == "t1"
    assert out.result_agents[0]["id"] == "w1"


@pytest.mark.asyncio
async def test_mark_request_failed_falls_back_to_error_code_message(
    fake_openclaw_home: Path,
) -> None:
    _seed_openclaw_agent("leader-4b")
    res = await svc.start_decompose_request(
        goal="x",
        leader_agent_id="leader-4b",
        user="alice",
        api_base="http://x",
        bridge_factory=lambda cfg: _FakeBridge(),
        background=False,
    )
    svc.mark_request_failed(res.request_id, code="EMPTY_MSG", message="")
    row = svc.get_request(res.request_id)
    assert row.error_message == "EMPTY_MSG"


@pytest.mark.asyncio
async def test_reap_marks_pending_timed_out(fake_openclaw_home: Path) -> None:
    _seed_openclaw_agent("leader-5")
    res = await svc.start_decompose_request(
        goal="x", leader_agent_id="leader-5", user="alice",
        api_base="http://x", bridge_factory=lambda cfg: _FakeBridge(),
        background=False,
    )
    storage = get_storage()
    row = storage.task_decompose_get(res.request_id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    storage.task_decompose_update(row)
    assert svc.reap_expired_requests(storage=storage) == 1
    assert svc.get_request(res.request_id).status == TaskDecomposeStatus.timed_out


@pytest.mark.asyncio
async def test_cancel_non_openclaw_request_cancels_running_cli_process(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-claude-cancel")
    repo_path.mkdir(parents=True, exist_ok=True)
    spawned = asyncio.Event()

    class _BlockingProc:
        def __init__(self) -> None:
            self.returncode = 0
            self.killed = False
            self._done = asyncio.Event()

        async def communicate(self):
            await self._done.wait()
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9
            self._done.set()

    proc = _BlockingProc()

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        spawned.set()
        return proc

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="cancel me",
        leader_agent_id="leader-claude-cancel",
        leader_kind="claude",
        leader_repo=str(repo_path),
        user="alice",
        api_base="http://127.0.0.1:17017",
        background=True,
    )
    await asyncio.wait_for(spawned.wait(), timeout=1.0)

    row = await svc.cancel_decompose_request(res.request_id)
    assert row is not None
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "USER_CANCELLED"
    assert proc.killed


@pytest.mark.asyncio
async def test_reap_timeout_cancels_running_non_openclaw_cli_process(
    fake_openclaw_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-claude-timeout")
    repo_path.mkdir(parents=True, exist_ok=True)
    spawned = asyncio.Event()

    class _BlockingProc:
        def __init__(self) -> None:
            self.returncode = 0
            self.killed = False
            self._done = asyncio.Event()

        async def communicate(self):
            await self._done.wait()
            return b"", b""

        def kill(self):
            self.killed = True
            self.returncode = -9
            self._done.set()

    proc = _BlockingProc()

    async def _fake_spawn(*argv, **kwargs):
        del argv, kwargs
        spawned.set()
        return proc

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)

    res = await svc.start_decompose_request(
        goal="timeout me",
        leader_agent_id="leader-claude-timeout",
        leader_kind="claude",
        leader_repo=str(repo_path),
        user="alice",
        api_base="http://127.0.0.1:17017",
        background=True,
    )
    await asyncio.wait_for(spawned.wait(), timeout=1.0)

    storage = get_storage()
    row = storage.task_decompose_get(res.request_id)
    assert row is not None
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    storage.task_decompose_update(row)

    assert svc.reap_expired_requests(storage=storage) == 1
    for _ in range(20):
        if proc.killed:
            break
        await asyncio.sleep(0.05)
    assert proc.killed
    assert svc.get_request(res.request_id).status == TaskDecomposeStatus.timed_out


# ── ③ public API -----------------------------------------------------


@pytest.fixture
def patched_bridge_for_app(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the service-level default bridge factory used by API calls."""
    captured: dict[str, Any] = {}

    class _B:
        async def chat_completion(self, *, agent_id, messages,
                                  session_key=None, **kw):
            captured["agent_id"] = agent_id
            captured["messages"] = messages
            captured["session_key"] = session_key
            return {}

        async def aclose(self):
            captured["closed"] = True

    monkeypatch.setattr(svc, "_default_bridge_factory", lambda cfg: _B())
    return captured


def test_decompose_start_returns_request_id(
    app_client: TestClient, patched_bridge_for_app: dict,
) -> None:
    _seed_openclaw_agent("leader-pub")
    r = app_client.post(
        "/api/flows/decompose",
        json={
            "goal": "make a release-notes bot",
            "leaderAgentId": "leader-pub",
            "existingAgents": [],
            "existingTasks": [],
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["statusUrl"].startswith("/api/flows/decompose/")
    assert body["requestId"]
    # The dispatch is fire-and-forget; poll for up to 1s for the bridge
    # call to land (TestClient returns before the asyncio task runs).
    for _ in range(20):
        if "agent_id" in patched_bridge_for_app:
            break
        time.sleep(0.05)
    assert patched_bridge_for_app["agent_id"] == "leader-pub"
    # Status row should be `dispatched` after the task completes.
    rid = body["requestId"]
    s = app_client.get(f"/api/flows/decompose/{rid}").json()
    assert s["status"] in {"dispatched", "pending"}


def test_decompose_start_passes_result_language_to_prompt(
    app_client: TestClient, patched_bridge_for_app: dict,
) -> None:
    _seed_openclaw_agent("leader-lang")
    r = app_client.post(
        "/api/flows/decompose",
        json={
            "goal": "拆成日报流程",
            "leaderAgentId": "leader-lang",
            "resultLanguage": "zh",
        },
    )
    assert r.status_code == 202, r.text
    for _ in range(20):
        if "messages" in patched_bridge_for_app:
            break
        time.sleep(0.05)
    msg = patched_bridge_for_app["messages"][0]["content"]
    assert "required output language" in msg
    assert "Chinese" in msg


def test_decompose_start_404_for_unknown_leader(
    app_client: TestClient, patched_bridge_for_app: dict,
) -> None:
    r = app_client.post(
        "/api/flows/decompose",
        json={"goal": "x", "leaderAgentId": "missing"},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "OPENCLAW_AGENT_NOT_FOUND"


def test_decompose_start_403_for_other_users_leader(
    app_client: TestClient, patched_bridge_for_app: dict,
) -> None:
    _seed_openclaw_agent("leader-foreign-api", user="bob")
    r = app_client.post(
        "/api/flows/decompose",
        json={"goal": "x", "leaderAgentId": "leader-foreign-api"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "FORBIDDEN"


def test_decompose_status_returns_state(
    app_client: TestClient, patched_bridge_for_app: dict,
) -> None:
    _seed_openclaw_agent("leader-pub2")
    rid = app_client.post(
        "/api/flows/decompose",
        json={"goal": "x", "leaderAgentId": "leader-pub2"},
    ).json()["requestId"]
    r = app_client.get(f"/api/flows/decompose/{rid}")
    assert r.status_code == 200
    body = r.json()
    assert body["requestId"] == rid
    assert body["leaderAgentId"] == "leader-pub2"
    assert body["status"] in {"pending", "dispatched"}


def test_decompose_status_403_other_user(
    app_client: TestClient, patched_bridge_for_app: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_openclaw_agent("leader-pub3")
    rid = app_client.post(
        "/api/flows/decompose",
        json={"goal": "x", "leaderAgentId": "leader-pub3"},
    ).json()["requestId"]
    monkeypatch.setenv("CSFLOW_USER", "bob")
    r = app_client.get(f"/api/flows/decompose/{rid}")
    assert r.status_code == 403


def test_decompose_cancel_marks_request_failed(
    app_client: TestClient,
    patched_bridge_for_app: dict,
) -> None:
    _seed_openclaw_agent("leader-cancel")
    rid = app_client.post(
        "/api/flows/decompose",
        json={"goal": "x", "leaderAgentId": "leader-cancel"},
    ).json()["requestId"]
    cancelled = app_client.post(f"/api/flows/decompose/{rid}/cancel")
    assert cancelled.status_code == 202, cancelled.text
    row = svc.get_request(rid)
    assert row is not None
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "USER_CANCELLED"


def test_decompose_cancel_403_other_user(
    app_client: TestClient,
    patched_bridge_for_app: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_openclaw_agent("leader-cancel-403")
    rid = app_client.post(
        "/api/flows/decompose",
        json={"goal": "x", "leaderAgentId": "leader-cancel-403"},
    ).json()["requestId"]
    monkeypatch.setenv("CSFLOW_USER", "bob")
    cancelled = app_client.post(f"/api/flows/decompose/{rid}/cancel")
    assert cancelled.status_code == 403


def test_decompose_start_accepts_non_openclaw_leader(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = Path("/tmp/leader-cli-repo")
    repo_path.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

        def kill(self):
            return None

    async def _fake_spawn(*argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs.get("cwd")
        return _Proc()

    monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", _fake_spawn)
    r = app_client.post(
        "/api/flows/decompose",
        json={
            "goal": "拆成多步骤执行流",
            "leaderAgentId": "leader-cli",
            "leaderKind": "claude",
            "leaderRepo": str(repo_path),
            "leaderTargetBranch": "main",
            "existingAgents": [{
                "id": "leader-cli",
                "kind": "claude",
                "repo": str(repo_path),
                "targetBranch": "main",
                "isLeader": True,
            }],
        },
    )
    assert r.status_code == 202, r.text
    for _ in range(20):
        if "argv" in seen:
            break
        time.sleep(0.05)
    argv = list(seen["argv"])
    assert argv[0] == "claude"
    assert "--permission-mode" in argv
    assert "bypassPermissions" in argv
    assert "--dangerously-skip-permissions" in argv
    assert "-p" in argv
    assert seen["cwd"] == str(repo_path)


# ── ④ internal API ---------------------------------------------------


def _seed_request(user: str = "alice", leader: str = "leader-i") -> str:
    storage = get_storage()
    from app.models import TaskDecomposeRequest
    row = TaskDecomposeRequest(
        user=user, goal="x", leader_agent_id=leader,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return storage.task_decompose_create(row).request_id


def test_commit_rejects_missing_token(app_client: TestClient) -> None:
    r = app_client.post(
        "/api/internal/task-decompose/commit",
        json={"requestId": "x", "agents": [], "tasks": []},
    )
    assert r.status_code == 401


def test_commit_rejects_wrong_purpose(app_client: TestClient) -> None:
    _seed_openclaw_agent("leader-i")
    rid = _seed_request(leader="leader-i")
    # Mint a token with the OTHER purpose (openclaw_agent_mgmt).
    bad_tok = it.mint_token(request_id=rid, user="alice")  # default purpose
    r = app_client.post(
        "/api/internal/task-decompose/commit",
        json={"requestId": rid, "agents": [
            {"id": "leader-i", "kind": "openclaw", "isLeader": True},
            {"id": "w", "kind": "claude", "isLeader": False},
        ], "tasks": [
            {"id": "do", "ownerAgentId": "w", "subject": "x"},
            {"id": "sum", "ownerAgentId": "leader-i", "subject": "y",
             "isLeaderSummary": True, "dependsOn": ["do"]},
        ]},
        headers={"Authorization": f"Bearer {bad_tok}"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "TOKEN_PURPOSE_MISMATCH"


def test_commit_rejects_request_id_mismatch(app_client: TestClient) -> None:
    _seed_openclaw_agent("leader-i2")
    rid = _seed_request(leader="leader-i2")
    bad_tok = it.mint_token(
        request_id="other", user="alice", purpose="task_decompose",
    )
    r = app_client.post(
        "/api/internal/task-decompose/commit",
        json={"requestId": rid, "agents": [], "tasks": []},
        headers={"Authorization": f"Bearer {bad_tok}"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "TOKEN_REQUEST_MISMATCH"


def test_commit_happy_path(app_client: TestClient) -> None:
    _seed_openclaw_agent("leader-i3")
    rid = _seed_request(leader="leader-i3")
    tok = it.mint_token(
        request_id=rid, user="alice", purpose="task_decompose",
    )
    r = app_client.post(
        "/api/internal/task-decompose/commit",
        json={
            "requestId": rid,
            "agents": [
                {"id": "leader-i3", "kind": "openclaw", "isLeader": True},
                {"id": "writer", "kind": "openclaw", "isLeader": False},
            ],
            "tasks": [
                {"id": "draft", "ownerAgentId": "writer",
                 "subject": "Draft", "dependsOn": []},
                {"id": "sum", "ownerAgentId": "leader-i3",
                 "subject": "Final", "dependsOn": ["draft"],
                 "isLeaderSummary": True},
            ],
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["acceptedTasks"] == 2
    assert body["acceptedAgents"] == 2
    # And the row reflects it.
    row = svc.get_request(rid)
    assert row.status == TaskDecomposeStatus.succeeded
    assert len(row.result_tasks) == 2


def test_commit_validates_leader_only_summary(app_client: TestClient) -> None:
    """Decomposer can't return a Flow where leader owns a non-summary task."""
    _seed_openclaw_agent("leader-i4")
    rid = _seed_request(leader="leader-i4")
    tok = it.mint_token(
        request_id=rid, user="alice", purpose="task_decompose",
    )
    r = app_client.post(
        "/api/internal/task-decompose/commit",
        json={
            "requestId": rid,
            "agents": [
                {"id": "leader-i4", "kind": "openclaw", "isLeader": True},
            ],
            "tasks": [
                # Leader owns a worker task — invalid.
                {"id": "draft", "ownerAgentId": "leader-i4",
                 "subject": "Draft", "dependsOn": []},
                {"id": "sum", "ownerAgentId": "leader-i4",
                 "subject": "Final", "dependsOn": ["draft"],
                 "isLeaderSummary": True},
            ],
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "INVALID_PROPOSAL"
    # The request row also reflects the failure.
    row = svc.get_request(rid)
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "INVALID_PROPOSAL"


def test_commit_requires_leader_summary_owner(app_client: TestClient) -> None:
    _seed_openclaw_agent("leader-i5")
    rid = _seed_request(leader="leader-i5")
    tok = it.mint_token(
        request_id=rid, user="alice", purpose="task_decompose",
    )
    r = app_client.post(
        "/api/internal/task-decompose/commit",
        json={
            "requestId": rid,
            "agents": [
                {"id": "leader-i5", "kind": "openclaw", "isLeader": True},
                {"id": "w", "kind": "claude", "isLeader": False},
            ],
            "tasks": [
                # Summary owned by NON-leader — invalid.
                {"id": "sum", "ownerAgentId": "w", "subject": "x",
                 "isLeaderSummary": True},
            ],
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_PROPOSAL"


def test_fail_endpoint_marks_request_failed(app_client: TestClient) -> None:
    _seed_openclaw_agent("leader-i6")
    rid = _seed_request(leader="leader-i6")
    tok = it.mint_token(
        request_id=rid, user="alice", purpose="task_decompose",
    )
    r = app_client.post(
        "/api/internal/task-decompose/fail",
        json={
            "requestId": rid, "code": "INSUFFICIENT_INPUT",
            "message": "goal too vague",
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    row = svc.get_request(rid)
    assert row.status == TaskDecomposeStatus.failed
    assert row.error_code == "INSUFFICIENT_INPUT"


# ── ⑤ cross-purpose token protection ---------------------------------


def test_task_decompose_commit_rejects_openclaw_purpose_token(
    app_client: TestClient,
) -> None:
    """Defence in depth: an openclaw token must not call task-decompose commit."""
    _seed_openclaw_agent("leader-x1")
    rid = _seed_request(leader="leader-x1")
    tok = it.mint_token(request_id=rid, user="alice", purpose="openclaw_agent_mgmt")
    r = app_client.post(
        "/api/internal/task-decompose/commit",
        json={"requestId": rid, "tasks": [{"subject": "x"}]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "TOKEN_PURPOSE_MISMATCH"


# ── ⑥ reinstall_skills service -------------------------------------


def test_reinstall_skills_excludes_removed_decomposer(
    fake_openclaw_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reinstall_skills`` no longer installs the removed decomposer skill."""
    if not _has_git():
        pytest.skip("git not available")
    from app.services.openclaw_agents import (
        CommitInput, commit_agent, reinstall_skills,
    )
    import asyncio

    agent = asyncio.run(commit_agent(
        CommitInput(id="fresh", name="Fresh"), user="alice",
    ))

    installed = reinstall_skills("fresh")
    # The decomposer skill is gone; a still-bundled common skill is present.
    assert "csflow-task-decomposer" not in installed
    assert not (
        Path(agent.workspace_path) / "skills" / "csflow-task-decomposer"
    ).exists()
    assert "self-definition-maintenance" in installed
