"""Tests for the Hermes agent management module (service + scheduler + flow guard)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
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


@pytest.fixture(autouse=True)
def _stub_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bootstrap is a real (killable) subprocess; stub it out by default so
    `commit_agent` tests don't spawn `hermes`. Tests exercising bootstrap or
    cancellation override ``svc._run_bootstrap`` themselves."""
    monkeypatch.setattr(svc, "_run_bootstrap", lambda *_a, **_kw: 0)


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
    boots: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "_run_bootstrap", lambda _aid, args, **_k: boots.append(args) or 0)
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])

    row = svc.commit_agent(
        svc.CommitInput(id="helper", name="Helper", description="do things"),
        user="alice",
    )
    assert row.id == "helper"
    assert row.created_by_user == "alice"
    assert get_storage().hermes_get("helper") is not None
    # profile create via _run_hermes; bootstrap (-p helper --yolo -z ...) via _run_bootstrap
    assert ["profile", "create", "helper", "--description", "do things"] in calls
    assert any(a[:3] == ["-p", "helper", "--yolo"] for a in boots)


def test_commit_agent_rejects_duplicate(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    svc.commit_agent(svc.CommitInput(id="dup", name="Dup"), user="alice")
    with pytest.raises(svc.AgentAlreadyExists):
        svc.commit_agent(svc.CommitInput(id="dup", name="Dup"), user="alice")


def test_commit_agent_lost_race_preserves_winner_profile(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate/concurrent create that loses the insert race (UNIQUE
    violation) must raise AgentAlreadyExists and NOT delete the winner's
    profile — the bug behind the vanishing agent card."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    storage = get_storage()
    # Pre-check sees no row, but the insert hits a UNIQUE violation because the
    # winner already inserted, and the row then exists (TOCTOU race).
    seq = {"n": 0}

    def fake_get(aid: str):  # noqa: ANN202
        seq["n"] += 1
        if seq["n"] == 1:
            return None
        return HermesAgent(id=aid, name="winner", profile_root="x", created_by_user="alice")

    def boom(_row):  # noqa: ANN001, ANN202
        raise RuntimeError("UNIQUE constraint failed: hermesagent.id")

    monkeypatch.setattr(storage, "hermes_get", fake_get)
    monkeypatch.setattr(storage, "hermes_create", boom)
    with pytest.raises(svc.AgentAlreadyExists):
        svc.commit_agent(svc.CommitInput(id="racer", name="语文"), user="alice", storage=storage)
    assert not any(c[:2] == ["profile", "delete"] for c in calls), (
        "loser must not delete the winner's profile"
    )


def test_commit_agent_fails_fast_when_create_in_progress(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a create for an id holds the per-id lock, a second create of the
    same id fails fast (no CLI work) instead of racing the in-flight one."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    lock = svc._create_id_lock("inflight")
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(svc.AgentAlreadyExists):
            svc.commit_agent(svc.CommitInput(id="inflight", name="X"), user="alice")
    finally:
        lock.release()
    assert calls == []  # fail-fast: nothing touched the CLI


def test_reconcile_skips_in_flight_create(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list/reconcile poll landing mid-bootstrap must NOT adopt the profile
    whose create is still in flight.

    Regression for the "已存在 Profile ID 为「math」的 Agent" bug on a brand-new
    id: `hermes profile create` lands the profile on disk *before* the long
    bootstrap, and the frontend polls `list_agents` every few seconds during the
    create. Without the in-flight guard, that poll's reconcile adopts the
    half-created profile into a (nameless) DB row, and the create's own
    `hermes_create` then collides with it → false AgentAlreadyExists.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    # Pre-check (list_profile_names) sees nothing → create proceeds; the reconcile
    # path (list_profile_names_checked) sees the profile on disk post-`create`.
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, ["math"]))
    storage = get_storage()

    # Simulate the frontend poll firing DURING bootstrap: list_agents runs the
    # reconcile while "math" is still in flight.
    def boot_then_poll(_aid, _args, **_kw):  # noqa: ANN001, ANN202
        svc.list_agents(user="alice", storage=storage)
        return 0

    monkeypatch.setattr(svc, "_run_bootstrap", boot_then_poll)

    row = svc.commit_agent(
        svc.CommitInput(id="math", name="Math"), user="alice", storage=storage
    )
    assert row.id == "math"
    rows = storage.hermes_list(owner_user="alice")
    assert [r.id for r in rows] == ["math"], "exactly one row; no ghost adopt"
    # The surviving row is the real create's (proper display name), not a
    # nameless adopt (which would have name == id).
    assert rows[0].name == "Math"
    assert "math" not in svc._CREATES_IN_FLIGHT, "in-flight marker cleaned up"


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


def test_api_open_dashboard(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import hermes_dashboard as dash_svc

    monkeypatch.setattr(
        dash_svc,
        "ensure_hermes_dashboard_url",
        lambda **kw: "http://127.0.0.1:9119/chat",
    )
    r = client.post("/api/hermes/agents/dashboard/open")
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "http://127.0.0.1:9119/chat"


def test_api_runtime_status_mode_passthrough(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def _probe(**kw):  # noqa: ANN003
        seen.append(kw.get("level"))
        return True, "ok"

    monkeypatch.setattr(svc, "probe_runtime_running", _probe)
    client.get("/api/hermes/agents/runtime/status?mode=fast")
    client.get("/api/hermes/agents/runtime/status?mode=full")
    client.get("/api/hermes/agents/runtime/status")  # default → full
    assert seen == [svc.PROBE_FAST, svc.PROBE_FULL, svc.PROBE_FULL]


def test_probe_runtime_fast_is_presence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """fast level must NOT shell out — presence on PATH is enough."""
    monkeypatch.setattr(svc, "hermes_executable", lambda: "/usr/local/bin/hermes")

    def _boom(*_a, **_kw):  # any subprocess call would be a bug
        raise AssertionError("fast probe must not run a subprocess")

    monkeypatch.setattr(svc, "_run_hermes", _boom)
    running, reason = svc.probe_runtime_running(level=svc.PROBE_FAST)
    assert running is True
    assert reason == "/usr/local/bin/hermes"


def test_probe_runtime_full_detects_broken_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """full level re-blocks only when the present binary can't run a version cmd."""
    monkeypatch.setattr(svc, "hermes_executable", lambda: "/usr/local/bin/hermes")
    monkeypatch.setattr(svc, "_run_hermes", lambda *_a, **_kw: (1, "", "boom"))
    running, _reason = svc.probe_runtime_running(level=svc.PROBE_FULL)
    assert running is False


def test_probe_runtime_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "hermes_executable", lambda: None)
    assert svc.probe_runtime_running(level=svc.PROBE_FAST)[0] is False
    assert svc.probe_runtime_running(level=svc.PROBE_FULL)[0] is False


# ── availability probe (regression: slow `hermes --version`) ──────────


def test_check_hermes_available_when_present_but_version_probe_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binary on PATH whose `--version` is slow (update-check) must still be
    reported usable — gating on the version probe wrongly showed 'Hermes 不可用'."""
    from app.cli import deps

    monkeypatch.setattr(deps.shutil, "which", lambda _name: "/usr/local/bin/hermes")
    # Simulate every version probe hitting the timeout (`_run` returns None).
    monkeypatch.setattr(deps, "_run", lambda *_a, **_kw: None)

    status = deps.check_hermes()
    assert status.ok is True
    assert status.found_version == "hermes available"


def test_check_hermes_not_found_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli import deps

    monkeypatch.setattr(deps.shutil, "which", lambda _name: None)
    status = deps.check_hermes()
    assert status.ok is False


# ── new-profile inference config seeding (fixes SOUL.md bootstrap) ────


def test_seed_profile_inherits_config_copies_model_and_keys(hermes_home: Path) -> None:
    """A fresh profile must inherit config.yaml + .env (model + API keys) from
    the root profile so the bootstrap `hermes -p <id> -z` has a provider; it
    must NOT inherit SOUL.md or memories (operator identity stays private)."""
    # Root profile: model config + keys + a personal SOUL/memory.
    (hermes_home / "config.yaml").write_text("model:\n  provider: custom:poe\n")
    (hermes_home / ".env").write_text("OPENAI_API_KEY=sk-secret\n")
    (hermes_home / "SOUL.md").write_text("operator personal persona")

    profile = hermes_home / "profiles" / "agt"
    profile.mkdir(parents=True)

    svc._seed_profile_inference_config("agt")

    assert (profile / "config.yaml").read_text() == "model:\n  provider: custom:poe\n"
    assert (profile / ".env").read_text() == "OPENAI_API_KEY=sk-secret\n"
    assert (profile / ".env").stat().st_mode & 0o777 == 0o600
    assert not (profile / "SOUL.md").exists()  # never leak operator identity


def test_seed_profile_inference_config_is_idempotent(hermes_home: Path) -> None:
    """Re-seeding must never clobber a profile's own config/keys."""
    (hermes_home / "config.yaml").write_text("model:\n  provider: root\n")
    profile = hermes_home / "profiles" / "agt"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  provider: customised\n")

    svc._seed_profile_inference_config("agt")

    assert (profile / "config.yaml").read_text() == "model:\n  provider: customised\n"


def test_seed_profile_inherits_from_selected_profile(hermes_home: Path) -> None:
    """When a source profile is selected, seed from that profile instead of default."""
    src = hermes_home / "profiles" / "source1"
    src.mkdir(parents=True)
    (src / "config.yaml").write_text("model:\n  provider: from_source\n")
    (src / ".env").write_text("SRC_KEY=abc\n")
    dest = hermes_home / "profiles" / "dest1"
    dest.mkdir(parents=True)

    svc._seed_profile_inference_config("dest1", source_profile="source1")

    assert (dest / "config.yaml").read_text() == "model:\n  provider: from_source\n"
    assert (dest / ".env").read_text() == "SRC_KEY=abc\n"


def test_import_model_from_profile_preserves_non_model_settings(hermes_home: Path) -> None:
    src = hermes_home / "profiles" / "source2"
    src.mkdir(parents=True)
    (src / "config.yaml").write_text(
        "model:\n  default: gpt-4o\n  provider: openai\n  base_url: https://api.example.com/v1\n"
    )
    (src / ".env").write_text("SRC_KEY=abc\nKEEP=2\n")

    dest = hermes_home / "profiles" / "dest2"
    dest.mkdir(parents=True)
    (dest / "config.yaml").write_text("mcp_servers:\n  keep:\n    url: https://example.com/mcp\n")
    (dest / ".env").write_text("KEEP=1\n")

    out = svc.import_model_from_profile("dest2", source_profile="source2")
    assert out["default"] == "gpt-4o"
    assert out["provider"] == "openai"
    cfg = yaml.safe_load((dest / "config.yaml").read_text())
    assert cfg["mcp_servers"]["keep"]["url"] == "https://example.com/mcp"
    assert cfg["model"]["default"] == "gpt-4o"
    env = (dest / ".env").read_text()
    assert "SRC_KEY=abc" in env
    assert "KEEP=2" in env


# ── create cancellation + rollback ───────────────────────────────────


def test_cancel_create_rolls_back_when_cancelled_during_bootstrap(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel flag set while the bootstrap runs must roll back the profile and
    raise AgentCreateCancelled instead of persisting a half-built agent."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])

    # Simulate the user cancelling mid-bootstrap: the bootstrap sets the flag.
    def _bootstrap(aid, _args, **_k):  # noqa: ANN001
        with svc._CREATE_LOCK:
            svc._CANCELLED_CREATES.add(aid)
        return 0

    monkeypatch.setattr(svc, "_run_bootstrap", _bootstrap)

    with pytest.raises(svc.AgentCreateCancelled):
        svc.commit_agent(svc.CommitInput(id="cancelme", name="X"), user="alice")

    assert get_storage().hermes_get("cancelme") is None  # no row persisted
    assert ["profile", "delete", "cancelme", "-y"] in calls  # profile rolled back
    assert not svc._is_create_cancelled("cancelme")  # flag cleared for retry


def test_cancel_create_agent_kills_live_bootstrap(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel_create_agent kills the bootstrap's whole process group + rolls back."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    # The bootstrap is started in its own process group; cancel must killpg it
    # (so children die too), not just proc.kill() the parent.
    group_kills: list[object] = []
    monkeypatch.setattr(
        svc._subproc_registry, "kill_group",
        lambda proc, **_k: group_kills.append(proc) or True,
    )

    class _FakeProc:
        pid = 4242

    proc = _FakeProc()
    with svc._CREATE_LOCK:
        svc._BOOTSTRAP_PROCS["live"] = proc  # type: ignore[assignment]

    killed = svc.cancel_create_agent("live")
    assert killed is True
    assert proc in group_kills
    assert svc._is_create_cancelled("live")
    assert ["profile", "delete", "live", "-y"] in calls
    with svc._CREATE_LOCK:
        svc._CANCELLED_CREATES.discard("live")
        svc._BOOTSTRAP_PROCS.pop("live", None)


def test_api_cancel_create_endpoint(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        svc, "cancel_create_agent", lambda aid, **_k: seen.append(aid) or False
    )
    r = client.post("/api/hermes/agents/foo/cancel-create")
    assert r.status_code == 202, r.text
    assert seen == ["foo"]


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
    # Listing reconciles against on-disk profiles, so isolate the profile source.
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, []))
    r = client.get("/api/hermes/agents")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_api_list_prunes_orphan_whose_profile_is_gone(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A managed row whose Hermes profile no longer exists must not be shown —
    Hermes is the source of truth (the reported 'ghost agent' bug)."""
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="ghost", name="Ghost", profile_root="x", created_by_user=owner)
    )
    # Hermes reports the profile is gone (query succeeded, name absent).
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, []))
    listing = client.get("/api/hermes/agents").json()
    assert all(a["id"] != "ghost" for a in listing["items"])
    assert get_storage().hermes_get("ghost") is None  # pruned from DB


def test_api_list_keeps_rows_when_hermes_query_fails(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient Hermes failure must NOT prune valid rows (query_ok=False)."""
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="keep", name="Keep", profile_root="x", created_by_user=owner)
    )
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (False, []))
    listing = client.get("/api/hermes/agents").json()
    assert any(a["id"] == "keep" for a in listing["items"])
    assert get_storage().hermes_get("keep") is not None


def test_api_list_auto_adopts_unmanaged_profiles(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing on-disk profiles show up in the management list without any
    separate "claim" step (treated uniformly regardless of origin)."""
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, ["preexisting"]))
    monkeypatch.setattr(svc, "read_profile_description", lambda _aid: "from disk")
    listing = client.get("/api/hermes/agents").json()
    assert any(a["id"] == "preexisting" for a in listing["items"])


def test_api_create_keeps_name_and_profile_id_distinct(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``name`` (Agent Name) and ``id`` (Profile id) are stored separately."""
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, ["myprofile"]))
    r = client.post(
        "/api/hermes/agents", json={"id": "myprofile", "name": "Backend Helper"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "myprofile"
    assert body["name"] == "Backend Helper"


def test_api_create_requires_name(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    r = client.post("/api/hermes/agents", json={"id": "myprofile", "name": "   "})
    assert r.status_code == 400


def test_api_create_passes_model_inherit_from(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def _fake_commit(cmd, *, user, storage=None, config=None):  # noqa: ANN001, ANN202
        seen["model_inherit_from"] = cmd.model_inherit_from
        return HermesAgent(
            id=cmd.id,
            name=cmd.name,
            description=cmd.description,
            team_id=cmd.team_id or "",
            profile_root=str(hermes_home / "profiles" / cmd.id),
            created_by_user=user,
            nl_prompt=cmd.nl_prompt or "",
        )

    monkeypatch.setattr(svc, "commit_agent", _fake_commit)
    monkeypatch.setattr(svc, "list_agents", lambda **_kw: [])
    r = client.post(
        "/api/hermes/agents",
        json={
            "id": "myprofile",
            "name": "Backend Helper",
            "modelInheritFrom": "source1",
        },
    )
    assert r.status_code == 201, r.text
    assert seen["model_inherit_from"] == "source1"


def test_api_import_model_from_profile(client: TestClient, hermes_home: Path) -> None:
    owner = svc.load_config().default_user
    src = hermes_home / "profiles" / "source3"
    src.mkdir(parents=True)
    (src / "config.yaml").write_text(
        "model:\n  default: claude-sonnet-4.5\n  provider: anthropic\n  base_url: https://x\n"
    )
    (src / ".env").write_text("MODEL_KEY=abc\n")

    dest = hermes_home / "profiles" / "dest3"
    dest.mkdir(parents=True)
    (dest / "config.yaml").write_text("mcp_servers:\n  keep:\n    url: https://example.com/mcp\n")
    get_storage().hermes_create(
        HermesAgent(id="dest3", name="D", profile_root="x", created_by_user=owner)
    )

    r = client.post(
        "/api/hermes/agents/dest3/settings/model/import",
        json={"inheritFrom": "source3"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default"] == "claude-sonnet-4.5"
    cfg = yaml.safe_load((dest / "config.yaml").read_text())
    assert cfg["mcp_servers"]["keep"]["url"] == "https://example.com/mcp"
    assert cfg["model"]["provider"] == "anthropic"
    assert "MODEL_KEY=abc" in (dest / ".env").read_text()


def test_chat_once_resume_adds_continue_flag(
    hermes_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Always enters the role via ``-p``; resume turns add ``-c`` to stay in the
    same session, first turns do not."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls, out="ok"))

    svc.chat_once("chatty", message="hi", workdir=str(tmp_path), resume=False)
    svc.chat_once("chatty", message="more", workdir=str(tmp_path), resume=True)

    assert calls[0] == ["-p", "chatty", "--yolo", "-z", "hi"]
    assert calls[1] == ["-p", "chatty", "--yolo", "-c", "-z", "more"]


def test_chat_once_resume_failure_falls_back_to_fresh(
    hermes_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``-c`` cannot continue the CLI session, retry without it — no error."""
    calls: list[list[str]] = []

    def _run(args, *, cwd=None, timeout=svc._CLI_TIMEOUT_SEC):  # noqa: ANN001
        calls.append(list(args))
        if "-c" in args:
            return 1, "", "no session to continue"
        return 0, "fresh ok", ""

    monkeypatch.setattr(svc, "_run_hermes", _run)
    out = svc.chat_once("chatty", message="more", workdir=str(tmp_path), resume=True)
    assert out == "fresh ok"
    assert calls[0] == ["-p", "chatty", "--yolo", "-c", "-z", "more"]
    assert calls[1] == ["-p", "chatty", "--yolo", "-z", "more"]


def test_api_create_and_list(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    # After creation the profile exists on disk, so the reconcile keeps the row.
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, ["backendhelper"]))
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


def test_mcp_server_upsert_list_delete(hermes_home: Path) -> None:
    profile = hermes_home / "profiles" / "mcpagent"
    profile.mkdir(parents=True)
    row = svc.upsert_mcp_server(
        "mcpagent",
        name="my-server",
        transport="sse",
        url="https://example.com/sse",
        environment="API_KEY=secret\nDEBUG=1",
    )
    assert row["name"] == "my-server"
    assert row["transport"] == "sse"
    listed = svc.list_mcp_servers("mcpagent")
    assert listed and listed[0]["env_keys"] == ["API_KEY", "DEBUG"]
    cfg = (profile / "config.yaml").read_text(encoding="utf-8")
    assert "mcp_servers" in cfg
    assert "my-server" in cfg
    assert "transport: sse" in cfg
    svc.delete_mcp_server("mcpagent", "my-server")
    assert svc.list_mcp_servers("mcpagent") == []


def test_api_mcp_settings_crud(client: TestClient, hermes_home: Path) -> None:
    owner = svc.load_config().default_user
    (hermes_home / "profiles" / "mcpagent").mkdir(parents=True)
    get_storage().hermes_create(
        HermesAgent(id="mcpagent", name="M", profile_root="x", created_by_user=owner)
    )
    put = client.put(
        "/api/hermes/agents/mcpagent/settings/mcp",
        json={
            "name": "remote-api",
            "transport": "http_sse",
            "url": "https://example.com/mcp",
            "environment": "API_KEY=secret",
        },
    )
    assert put.status_code == 200, put.text
    listed = client.get("/api/hermes/agents/mcpagent/settings/mcp")
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert body[0]["name"] == "remote-api"
    assert body[0]["envKeys"] == ["API_KEY"]
    rm = client.delete("/api/hermes/agents/mcpagent/settings/mcp/remote-api")
    assert rm.status_code == 204
