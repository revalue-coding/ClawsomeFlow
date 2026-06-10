"""Tests for the Hermes agent management module (service + scheduler + flow guard)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AgentKind, Flow, FlowAgent, FlowSpec, FlowTask, HermesAgent
from app.scheduler.sessions.tmux_live import _KIND_TO_CMD, TmuxLiveSession
from app.services import hermes_agents as svc
from app.services.task_decompose import _non_openclaw_dispatch_argv
from app.storage import get_storage
from app.validators import FlowValidationError, validate_flow_against_db
from app.validators.flow import ERROR_HERMES_AGENT_NOT_FOUND


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv(svc.HERMES_HOME_ENV, str(home))
    return home


def _fake_run(records: list[list[str]], rc: int = 0, out: str = "", err: str = ""):
    def _run(args, *, cwd=None, timeout=svc._CLI_TIMEOUT_SEC):  # noqa: ANN001
        records.append(list(args))
        return rc, out, err

    return _run


# ── id validation ────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["Upper", "has-dash", "with space", "a", "", "x_y"])
def test_validate_agent_id_rejects(bad: str) -> None:
    with pytest.raises(svc.AgentIdInvalid):
        svc._validate_agent_id(bad)


@pytest.mark.parametrize("ok", ["abc", "agent1", "backendhelper"])
def test_validate_agent_id_accepts(ok: str) -> None:
    assert svc._validate_agent_id(ok) == ok


# ── commit / delete / claim (mocked CLI) ─────────────────────────────


def test_commit_agent_creates_profile_and_row(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])

    row = svc.commit_agent(
        svc.CommitInput(id="helper", name="Helper", description="do things"),
        user="alice",
    )
    assert row.id == "helper"
    assert row.created_by_user == "alice"
    assert get_storage().hermes_get("helper") is not None
    # profile create + bootstrap (-p helper -z ...) both invoked
    assert ["profile", "create", "helper", "--description", "do things"] in calls
    assert any(a[:3] == ["-p", "helper", "--yolo"] for a in calls)


def test_commit_agent_rejects_duplicate(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    svc.commit_agent(svc.CommitInput(id="dup", name="Dup"), user="alice")
    with pytest.raises(svc.AgentAlreadyExists):
        svc.commit_agent(svc.CommitInput(id="dup", name="Dup"), user="alice")


def test_delete_agent_permanent(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    svc.commit_agent(svc.CommitInput(id="gone", name="Gone"), user="alice")

    svc.delete_agent("gone")
    assert get_storage().hermes_get("gone") is None
    assert ["profile", "delete", "gone", "-y"] in calls


def test_delete_blocked_by_flow(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    svc.commit_agent(svc.CommitInput(id="busy", name="Busy"), user="alice")

    storage = get_storage()
    flow = Flow(name="F1", description="g", owner_user="alice").with_spec(
        FlowSpec(
            agents=[
                FlowAgent(id="busy", kind=AgentKind.hermes, repo="/tmp/r", is_leader=True),
                FlowAgent(id="w", kind=AgentKind.claude, repo="/tmp/r", is_leader=False),
            ],
            tasks=[
                FlowTask(id="t0", owner_agent_id="w", subject="w"),
                FlowTask(id="t1", owner_agent_id="busy", subject="x",
                         depends_on=["t0"], is_leader_summary=True),
            ],
        )
    )
    storage.flow_create(flow)

    with pytest.raises(svc.AgentInUse):
        svc.delete_agent("busy")


def test_claim_existing_profile(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "list_profile_names", lambda: ["existing"])
    monkeypatch.setattr(svc, "read_profile_description", lambda _aid: "imported")
    row = svc.claim_profile(profile_name="existing", user="bob")
    assert row.id == "existing"
    assert row.description == "imported"
    assert get_storage().hermes_get("existing") is not None


def test_claimable_excludes_managed(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "list_profile_names", lambda: ["p1", "p2"])
    monkeypatch.setattr(svc, "read_profile_description", lambda _aid: "")
    get_storage().hermes_create(
        HermesAgent(id="p1", name="P1", profile_root="x", created_by_user="alice")
    )
    items = svc.list_claimable_profiles()
    assert [i["id"] for i in items] == ["p2"]


# ── settings: SOUL / secrets (filesystem) ────────────────────────────


def test_soul_read_write(hermes_home: Path) -> None:
    (hermes_home / "profiles" / "soulagent").mkdir(parents=True)
    svc.write_soul("soulagent", "I am a helper.")
    assert svc.read_soul("soulagent") == "I am a helper."


def test_secret_set_list_delete(hermes_home: Path) -> None:
    (hermes_home / "profiles" / "seca").mkdir(parents=True)
    svc.set_secret("seca", "OPENAI_API_KEY", "sk-test")
    secrets = svc.list_secrets("seca")
    assert any(s["key"] == "OPENAI_API_KEY" and s["is_set"] for s in secrets)
    # value is masked, never returned verbatim
    assert all("sk-test" not in s["preview"] for s in secrets)
    svc.delete_secret("seca", "OPENAI_API_KEY")
    assert svc.list_secrets("seca") == []


# ── scheduler -p injection ───────────────────────────────────────────


def test_tmux_live_injects_profile_for_hermes() -> None:
    agent = FlowAgent(id="myh", kind=AgentKind.hermes, repo="/tmp", target_branch="main")
    s = TmuxLiveSession(agent=agent, team_name="csflow-x", run_id="run-1", cli=object())
    assert s._spawn_cmd == ["hermes", "--yolo", "-p", "myh"]
    assert s._resume_cmd == ["hermes", "--yolo", "-c", "-p", "myh"]
    # The shared template must never be mutated.
    assert _KIND_TO_CMD[AgentKind.hermes] == (
        ["hermes", "--yolo"],
        ["hermes", "--yolo", "-c"],
    )


def test_decompose_argv_injects_profile() -> None:
    argv = _non_openclaw_dispatch_argv(kind=AgentKind.hermes, message="hi", profile="myh")
    assert argv == ["hermes", "--yolo", "-p", "myh", "-z", "hi"]
    # no profile → no -p (back-compat)
    assert _non_openclaw_dispatch_argv(kind=AgentKind.hermes, message="hi") == [
        "hermes", "--yolo", "-z", "hi",
    ]


# ── flow validation guard ────────────────────────────────────────────


def _git_repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return str(repo)


def _hermes_spec(repo: str) -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(id="myh", kind=AgentKind.hermes, repo=repo, is_leader=True),
            FlowAgent(id="w", kind=AgentKind.cursor, repo=repo, is_leader=False),
        ],
        tasks=[
            FlowTask(id="t0", owner_agent_id="w", subject="w"),
            FlowTask(id="t1", owner_agent_id="myh", subject="x",
                     depends_on=["t0"], is_leader_summary=True),
        ],
    )


def test_flow_rejects_unmanaged_hermes(tmp_path: Path) -> None:
    spec = _hermes_spec(_git_repo(tmp_path))
    with pytest.raises(FlowValidationError) as exc:
        validate_flow_against_db(spec, get_storage())
    assert exc.value.code == ERROR_HERMES_AGENT_NOT_FOUND


def test_flow_accepts_managed_hermes(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    get_storage().hermes_create(
        HermesAgent(id="myh", name="H", profile_root="x", created_by_user="alice")
    )
    validate_flow_against_db(_hermes_spec(repo), get_storage())


# ── API smoke ────────────────────────────────────────────────────────


def test_api_runtime_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "probe_runtime_running", lambda **_kw: (True, "ok"))
    r = client.get("/api/hermes/agents/runtime/status")
    assert r.status_code == 200
    assert r.json()["running"] is True


def test_write_skill_creates_and_lists(hermes_home: Path) -> None:
    """write_skill creates skills/<name>/SKILL.md (no CLI) and lists it."""
    out = svc.write_skill("agt", name="my-skill", description="d", content="# hi")
    assert out["name"] == "my-skill"
    assert any(s["name"] == "my-skill" for s in svc.list_skills("agt"))
    assert "# hi" in svc.read_skill("agt", "my-skill")
    with pytest.raises(svc.AgentAlreadyExists):
        svc.write_skill("agt", name="my-skill", content="x")
    with pytest.raises(svc.AgentIdInvalid):
        svc.write_skill("agt", name="bad name", content="x")
    with pytest.raises(svc.AgentIdInvalid):
        svc.write_skill("agt", name="ok", content="   ")


def test_api_list_empty(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Listing now auto-adopts on-disk profiles, so isolate the profile source.
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    r = client.get("/api/hermes/agents")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_api_list_auto_adopts_unmanaged_profiles(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing on-disk profiles show up in the management list without any
    separate "claim" step (treated uniformly regardless of origin)."""
    monkeypatch.setattr(svc, "list_profile_names", lambda: ["preexisting"])
    monkeypatch.setattr(svc, "read_profile_description", lambda _aid: "from disk")
    listing = client.get("/api/hermes/agents").json()
    assert any(a["id"] == "preexisting" for a in listing["items"])


def test_api_create_uses_id_as_profile_name(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single "Agent name (Profile id)" field is sent as ``id`` and used
    verbatim as the profile id; ``name`` defaults to it."""
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    r = client.post("/api/hermes/agents", json={"id": "myprofile"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "myprofile"
    assert body["name"] == "myprofile"


def test_api_create_and_list(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    r = client.post(
        "/api/hermes/agents",
        json={"name": "Backend Helper", "responsibility": "owns the API"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "backendhelper"  # derived from name
    listing = client.get("/api/hermes/agents").json()
    assert any(a["id"] == "backendhelper" for a in listing["items"])


def test_api_chat_requires_workdir(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_storage().hermes_create(
        HermesAgent(id="chatty", name="C", profile_root="x", created_by_user="")
    )
    r = client.post("/api/hermes/agents/chatty/chat", json={"message": "hi", "workdir": ""})
    assert r.status_code == 400
