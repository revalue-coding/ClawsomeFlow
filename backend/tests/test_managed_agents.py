"""Real-environment tests for the managed agent module (Claude/Codex env-home).

Run directly (not sandboxed) — they invoke the real ``clawteam``/``claude``/``codex``
CLIs. Each test cleans up its ClawTeam profile + config home via delete_agent.
Config homes live under the per-test isolated CSFLOW_HOME (conftest autouse).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AgentKind, Flow, FlowAgent, FlowSpec, FlowTask, ManagedAgent
from app.scheduler import managed_runtime as rt
from app.scheduler.sessions.tmux_live import TmuxLiveSession
from app.services import managed_agents as svc
from app.storage import get_storage
from app.validators import FlowValidationError, validate_flow_against_db
from app.validators.flow import ERROR_MANAGED_AGENT_NOT_FOUND

_CLAUDE = pytest.mark.skipif(not svc.cli_available("claude"), reason="claude CLI absent")


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def _profile_env(name: str) -> str:
    out = subprocess.run(["clawteam", "profile", "show", name], capture_output=True, text=True)
    return out.stdout + out.stderr


# ── validation ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["Up", "a b", "_x", "", "a"])
def test_invalid_id(bad: str) -> None:
    with pytest.raises(svc.AgentIdInvalid):
        svc._validate_id(bad)


def test_invalid_kind() -> None:
    with pytest.raises(svc.KindUnsupported):
        svc.commit_agent(svc.CommitInput(id="x1", kind="gpt", name="X"), user="alice")


# ── lifecycle (real clawteam profile + config home) ───────────────────


@_CLAUDE
def test_commit_creates_home_profile_and_row() -> None:
    st = get_storage()
    aid = "mgsmoke"
    try:
        row = svc.commit_agent(
            svc.CommitInput(id=aid, kind="claude", name="MG", description="r"),
            user="alice", storage=st,
        )
        assert row.kind == "claude"
        assert row.clawteam_profile == "csflow-claude-mgsmoke"
        assert Path(row.config_home).is_dir()
        assert (Path(row.config_home) / "CLAUDE.md").exists()
        assert "CLAUDE_CONFIG_DIR" in _profile_env(row.clawteam_profile)
        assert st.managed_get(aid) is not None
    finally:
        try:
            svc.delete_agent(aid, storage=st)
        except svc.AgentNotFound:
            pass


@_CLAUDE
def test_delete_removes_profile_home_row() -> None:
    st = get_storage()
    aid = "mgdel"
    row = svc.commit_agent(svc.CommitInput(id=aid, kind="claude", name="D"), user="alice", storage=st)
    home = row.config_home
    svc.delete_agent(aid, storage=st)
    assert st.managed_get(aid) is None
    assert not Path(home).exists()
    assert "Unknown profile" in _profile_env(row.clawteam_profile)


@_CLAUDE
def test_mcp_add_list_remove_recognized() -> None:
    st = get_storage()
    aid = "mgmcp"
    try:
        svc.commit_agent(svc.CommitInput(id=aid, kind="claude", name="M"), user="alice", storage=st)
        svc.add_mcp(aid, name="demo", command=["echo", "hi"], storage=st)
        assert any(s["name"] == "demo" for s in svc.list_mcp(aid, storage=st))
        svc.remove_mcp(aid, "demo", storage=st)
        assert all(s["name"] != "demo" for s in svc.list_mcp(aid, storage=st))
    finally:
        svc.delete_agent(aid, storage=st)


# ── flow guard ────────────────────────────────────────────────────────


def _git_repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)
    return str(repo)


def _claude_spec(repo: str) -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(id="cc", kind=AgentKind.claude, repo=repo, is_leader=True),
            FlowAgent(id="w", kind=AgentKind.claude, repo=repo, is_leader=False),
        ],
        tasks=[
            FlowTask(id="t0", owner_agent_id="w", subject="w"),
            FlowTask(id="t1", owner_agent_id="cc", subject="x", depends_on=["t0"], is_leader_summary=True),
        ],
    )


def test_flow_rejects_unmanaged_claude(tmp_path: Path) -> None:
    with pytest.raises(FlowValidationError) as exc:
        validate_flow_against_db(_claude_spec(_git_repo(tmp_path)), get_storage())
    assert exc.value.code == ERROR_MANAGED_AGENT_NOT_FOUND


def test_write_skill_creates_and_lists(tmp_path: Path) -> None:
    """write_skill creates a SKILL.md (no CLI needed) and shows up in list."""
    st = get_storage()
    aid = "mgskill"
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    st.managed_create(ManagedAgent(
        id=aid, kind="claude", name=aid, config_home=str(home),
        clawteam_profile=f"csflow-claude-{aid}", created_by_user="alice",
    ))
    out = svc.write_skill(aid, name="my-skill", description="d", content="# hi", storage=st)
    assert out["name"] == "my-skill"
    assert any(s["name"] == "my-skill" for s in svc.list_skills(aid, storage=st))
    assert "# hi" in svc.read_skill(aid, "my-skill", storage=st)
    with pytest.raises(svc.AgentAlreadyExists):
        svc.write_skill(aid, name="my-skill", content="x", storage=st)
    with pytest.raises(svc.AgentIdInvalid):
        svc.write_skill(aid, name="bad name", content="x", storage=st)
    with pytest.raises(svc.AgentIdInvalid):
        svc.write_skill(aid, name="ok", content="   ", storage=st)


def test_flow_accepts_managed_claude(tmp_path: Path) -> None:
    st = get_storage()
    repo = _git_repo(tmp_path)
    for aid in ("cc", "w"):
        st.managed_create(ManagedAgent(
            id=aid, kind="claude", name=aid, config_home="x",
            clawteam_profile=f"csflow-claude-{aid}", created_by_user="alice",
        ))
    validate_flow_against_db(_claude_spec(repo), st)


# ── scheduler profile injection ───────────────────────────────────────


@_CLAUDE
def test_scheduler_resolves_managed_profile() -> None:
    agent = FlowAgent(id="ccres", kind=AgentKind.claude, repo="/tmp", target_branch="main")
    s = TmuxLiveSession(agent=agent, team_name="t", run_id="run-1", cli=object())
    try:
        assert s._resolve_profile() == "csflow-claude-ccres"
        assert "CLAUDE_CONFIG_DIR" in _profile_env("csflow-claude-ccres")
    finally:
        rt.remove_profile("claude", "ccres")
        import shutil
        shutil.rmtree(rt.managed_home("claude", "ccres").parent, ignore_errors=True)


def test_delete_blocked_by_flow(tmp_path: Path) -> None:
    st = get_storage()
    st.managed_create(ManagedAgent(
        id="busy", kind="claude", name="B", config_home="x",
        clawteam_profile="csflow-claude-busy", created_by_user="alice",
    ))
    flow = Flow(name="F", description="g", owner_user="alice").with_spec(
        FlowSpec(
            agents=[
                FlowAgent(id="busy", kind=AgentKind.claude, repo="/tmp/r", is_leader=True),
                FlowAgent(id="w", kind=AgentKind.openclaw, is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="busy", subject="x", depends_on=["t0"], is_leader_summary=True),
            ],
        )
    )
    st.flow_create(flow)
    with pytest.raises(svc.AgentInUse):
        svc.delete_agent("busy", storage=st)


# ── API smoke ─────────────────────────────────────────────────────────


def test_api_runtime_status(client: TestClient) -> None:
    r = client.get("/api/managed/agents/runtime/status?kind=claude")
    assert r.status_code == 200
    assert set(r.json()) == {"running", "reason"}


def test_api_list_empty(client: TestClient) -> None:
    r = client.get("/api/managed/agents?kind=claude")
    assert r.status_code == 200
    assert r.json()["items"] == []


@_CLAUDE
def test_api_create_and_list(client: TestClient) -> None:
    r = client.post("/api/managed/agents", json={"kind": "claude", "name": "API CC", "responsibility": "x"})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    try:
        assert any(a["id"] == aid for a in client.get("/api/managed/agents?kind=claude").json()["items"])
    finally:
        client.delete(f"/api/managed/agents/{aid}")
