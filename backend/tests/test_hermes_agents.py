"""Tests for the Hermes agent management module (service + scheduler + flow guard)."""

from __future__ import annotations

import json
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
def _stub_hermes_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bootstrap is a real (killable) subprocess; stub it out by default so
    `commit_agent` tests don't spawn `hermes`. Tests exercising bootstrap or
    cancellation override ``svc._run_bootstrap`` themselves.

    Settings helpers first ask the Hermes CLI for profile-local config/env
    paths; default that lookup to "unavailable" so tests use the filesystem
    fallback under the isolated ``HERMES_HOME``.
    """
    def _profile_fallback(agent_id: str, args: list[str], **kw):  # noqa: ANN202
        if args in (["config", "path"], ["config", "env-path"]):
            return 1, "", ""
        return svc._run_hermes(["-p", agent_id, *args], **kw)

    monkeypatch.setattr(svc, "_run_bootstrap", lambda *_a, **_kw: 0)
    monkeypatch.setattr(svc, "_hermes_profile", _profile_fallback)


# ── id validation ────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["Upper", "_start", "-start", "with space", "has.dot", ""])
def test_validate_agent_id_rejects(bad: str) -> None:
    with pytest.raises(svc.AgentIdInvalid):
        svc._validate_agent_id(bad)


@pytest.mark.parametrize("ok", ["a", "abc", "agent1", "backend-helper", "backend_helper"])
def test_validate_agent_id_accepts(ok: str) -> None:
    assert svc._validate_agent_id(ok) == ok


def test_validate_agent_id_accepts_64_chars() -> None:
    aid = "a" * 64
    assert svc._validate_agent_id(aid) == aid


def test_validate_agent_id_rejects_over_64_chars() -> None:
    with pytest.raises(svc.AgentIdInvalid):
        svc._validate_agent_id("a" * 65)


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


def test_commit_agent_reports_bootstrap_failure_but_still_creates(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed self-definition bootstrap is NON-fatal (the agent is still
    created + persisted) but must be reported via the ``outcome`` so the create
    response can warn instead of silently claiming a fully-ready agent."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])

    def _failing_bootstrap(aid, _args, *, outcome=None, **_k):  # noqa: ANN001, ANN202
        if outcome is not None:
            outcome.ok = False
            outcome.error = "No inference provider configured"
        return 1

    monkeypatch.setattr(svc, "_run_bootstrap", _failing_bootstrap)

    outcome = svc.BootstrapOutcome()
    row = svc.commit_agent(
        svc.CommitInput(id="halfbaked", name="Half"), user="alice", outcome=outcome
    )
    # Agent still created + persisted (best-effort bootstrap never fails create).
    assert row.id == "halfbaked"
    assert get_storage().hermes_get("halfbaked") is not None
    # …but the failure is visible.
    assert outcome.ran is True
    assert outcome.ok is False
    assert "No inference provider" in outcome.error


def test_commit_agent_outcome_ok_on_success(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default autouse stub returns 0 → outcome stays ok."""
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    outcome = svc.BootstrapOutcome()
    svc.commit_agent(svc.CommitInput(id="okagent", name="OK"), user="alice", outcome=outcome)
    assert outcome.ok is True
    assert outcome.ran is True


def test_commit_agent_light_clone_from_other_profile(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clone_from=<id> without clone_all → light clone (`--clone --clone-from`);
    no separate model seed because the clone already populated config."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    seeds: list[tuple] = []
    monkeypatch.setattr(
        svc, "_seed_profile_inference_config",
        lambda *a, **k: seeds.append((a, k)),
    )

    svc.commit_agent(
        svc.CommitInput(id="c1", name="C1", clone_from="src"), user="alice"
    )
    assert ["profile", "create", "c1", "--clone", "--clone-from", "src"] in calls
    assert seeds == []  # cloned + no model inherit → no extra seed


def test_commit_agent_full_clone(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    svc.commit_agent(
        svc.CommitInput(id="c2", name="C2", clone_from="src", clone_all=True),
        user="alice",
    )
    assert ["profile", "create", "c2", "--clone-all", "--clone-from", "src"] in calls


def test_commit_agent_clone_from_default_uses_active_profile(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clone_from='default' clones the active profile, so no --clone-from arg."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    svc.commit_agent(
        svc.CommitInput(id="c3", name="C3", clone_from="default"), user="alice"
    )
    assert ["profile", "create", "c3", "--clone"] in calls
    assert not any("--clone-from" in c for c in calls)


def test_commit_agent_clone_then_model_inherit(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clone_from + model_inherit_from → clone first, then a model inheritance ON
    TOP via import_model_from_profile (not a wholesale seed)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    imports: list[tuple[str, str]] = []
    monkeypatch.setattr(
        svc, "import_model_from_profile",
        lambda aid, *, source_profile: imports.append((aid, source_profile)),
    )
    seeds: list[tuple] = []
    monkeypatch.setattr(
        svc, "_seed_profile_inference_config",
        lambda *a, **k: seeds.append((a, k)),
    )

    svc.commit_agent(
        svc.CommitInput(
            id="c4", name="C4", clone_from="src", model_inherit_from="m1"
        ),
        user="alice",
    )
    assert ["profile", "create", "c4", "--clone", "--clone-from", "src"] in calls
    assert imports == [("c4", "m1")]  # model inherit applied after clone
    assert seeds == []  # cloned → no wholesale seed


def test_commit_agent_model_inherit_only_seeds_config(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """model_inherit_from without clone → wholesale config seed (legacy path)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    seeds: list[tuple] = []
    monkeypatch.setattr(
        svc, "_seed_profile_inference_config",
        lambda *a, **k: seeds.append((a, k)),
    )
    svc.commit_agent(
        svc.CommitInput(id="c5", name="C5", model_inherit_from="m1"), user="alice"
    )
    assert ["profile", "create", "c5"] in calls
    assert not any("--clone" in c for c in calls)
    assert seeds == [(("c5",), {"source_profile": "m1"})]


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
    # no profile → no -p (temporary Hermes runs under the default profile)
    assert _non_openclaw_dispatch_argv(kind=AgentKind.hermes, message="hi") == [
        "hermes", "--yolo", "-z", "hi",
    ]


# ── flow validation guard ────────────────────────────────────────────


def _git_repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo, check=True)
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


def test_start_gateway_runs_install_then_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_profile(agent_id: str, args: list[str], **kw):  # noqa: ANN001
        calls.append(([agent_id, *args], kw))
        if args == ["gateway", "install"]:
            return 0, "installed", ""
        if args == ["gateway", "start"]:
            return 0, "gateway listening at http://127.0.0.1:9120", ""
        raise AssertionError(f"unexpected args: {args}")

    monkeypatch.setattr(svc, "_hermes_profile", _fake_profile)
    msg = svc.start_gateway("helper")
    assert calls[0][0] == ["helper", "gateway", "install"]
    assert calls[0][1]["stdin"] == "y\ny\n"
    assert calls[1][0] == ["helper", "gateway", "start"]
    assert "stdin" not in calls[1][1]
    assert "gateway listening" in msg


def test_start_gateway_start_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _fake_profile(agent_id: str, args: list[str], **_kw):  # noqa: ANN001
        calls.append([agent_id, *args])
        if args == ["gateway", "install"]:
            return 0, "installed", ""
        if args == ["gateway", "start"]:
            return 1, "", "boom"
        return 1, "", "unexpected"

    monkeypatch.setattr(svc, "_hermes_profile", _fake_profile)
    with pytest.raises(svc.ProfileOpFailed) as exc:
        svc.start_gateway("helper")
    assert "gateway start" in str(exc.value)
    assert calls == [
        ["helper", "gateway", "install"],
        ["helper", "gateway", "start"],
    ]


def test_api_start_gateway(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="gw1", name="Gateway Agent", profile_root="x", created_by_user=owner)
    )
    monkeypatch.setattr(svc, "start_gateway", lambda _aid: "gateway started")
    r = client.post("/api/hermes/agents/gw1/gateway/start")
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "gateway started"


def test_read_gateway_cwd_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_root = tmp_path / "profiles" / "helper"
    profile_root.mkdir(parents=True)
    (profile_root / "config.yaml").write_text(
        "terminal:\n  cwd: /data/projects/foo\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(svc, "_config_path", lambda _aid: profile_root / "config.yaml")
    assert svc.read_gateway_cwd("helper") == {"cwd": "/data/projects/foo"}


def test_write_gateway_cwd_sets_config_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    profile_root = tmp_path / "profiles" / "helper"
    profile_root.mkdir(parents=True)
    cfg = profile_root / "config.yaml"
    cfg.write_text("terminal:\n  cwd: /old\n", encoding="utf-8")

    calls: list[list[str]] = []

    def _fake_profile(agent_id: str, args: list[str], **_kw):  # noqa: ANN001
        calls.append([agent_id, *args])
        if args == ["config", "set", "terminal.cwd", str(workdir.resolve())]:
            cfg.write_text(
                f"terminal:\n  cwd: {workdir.resolve()}\n",
                encoding="utf-8",
            )
            return 0, "ok", ""
        if args == ["gateway", "restart"]:
            return 1, "", "restart failed"
        return 0, "", ""

    monkeypatch.setattr(svc, "_hermes_profile", _fake_profile)
    monkeypatch.setattr(svc, "_config_path", lambda _aid: cfg)

    out = svc.write_gateway_cwd("helper", cwd=str(workdir))
    assert out["cwd"] == str(workdir.resolve())
    assert calls[0] == ["helper", "config", "set", "terminal.cwd", str(workdir.resolve())]
    assert calls[1] == ["helper", "gateway", "restart"]


def test_write_gateway_cwd_invalid_directory_raises() -> None:
    with pytest.raises(svc.AgentIdInvalid) as exc:
        svc.write_gateway_cwd("helper", cwd="/no/such/directory")
    assert "cwd path does not exist" in str(exc.value)


def test_api_gateway_settings(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="gw2", name="Gateway Agent 2", profile_root="x", created_by_user=owner)
    )
    workdir = tmp_path / "cwd"
    workdir.mkdir()

    monkeypatch.setattr(svc, "read_gateway_cwd", lambda _aid: {"cwd": ""})
    monkeypatch.setattr(
        svc,
        "write_gateway_cwd",
        lambda _aid, *, cwd: {"cwd": cwd},
    )

    r = client.get("/api/hermes/agents/gw2/settings/gateway")
    assert r.status_code == 200, r.text
    assert r.json()["cwd"] == ""

    r = client.put(
        "/api/hermes/agents/gw2/settings/gateway",
        json={"cwd": str(workdir)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["cwd"] == str(workdir)


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


def test_seed_profile_inherits_auth_json_credential(hermes_home: Path) -> None:
    """A fresh profile must also inherit ``auth.json`` — when the operator logs
    in via ``hermes model`` / OAuth the live credential lives there (NOT as an
    API key in .env), and Hermes reads ONLY the profile-local auth.json. Omitting
    it left a keyless profile whose first ``hermes -z`` bootstrap failed with
    "No inference provider configured" (the market-strategist regression)."""
    (hermes_home / "config.yaml").write_text("model:\n  provider: auto\n")
    (hermes_home / ".env").write_text("PLACEHOLDER=\n")
    (hermes_home / "auth.json").write_text('{"token": "secret-oauth"}')

    profile = hermes_home / "profiles" / "agt"
    profile.mkdir(parents=True)

    svc._seed_profile_inference_config("agt")

    assert (profile / "auth.json").read_text() == '{"token": "secret-oauth"}'
    # credential file must stay private
    assert (profile / "auth.json").stat().st_mode & 0o777 == 0o600


def test_backfill_copies_missing_auth_json_into_existing_profile(
    hermes_home: Path,
) -> None:
    """Upgrade parity: a managed profile created before ``auth.json`` was seeded
    (has config.yaml but no credential) must gain auth.json on the upgrade path.
    ``upgrade.py`` calls this backfill, so this proves the upgrade-only repair
    for the market-strategist regression."""
    (hermes_home / "config.yaml").write_text("model:\n  provider: auto\n")
    (hermes_home / ".env").write_text("PLACEHOLDER=\n")
    (hermes_home / "auth.json").write_text('{"token": "root-cred"}')

    prof = hermes_home / "profiles" / "legacy"
    prof.mkdir(parents=True)
    (prof / "config.yaml").write_text("model:\n  provider: auto\n")  # already seeded

    storage = get_storage()
    storage.hermes_create(
        HermesAgent(
            id="legacy",
            name="Legacy",
            profile_root=str(prof),
            created_by_user="alice",
        )
    )
    assert not (prof / "auth.json").exists()

    svc.backfill_hermes_inference_config(storage=storage)

    assert (prof / "auth.json").read_text() == '{"token": "root-cred"}'
    assert (prof / "auth.json").stat().st_mode & 0o777 == 0o600


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


def test_update_skill_overwrites_existing(hermes_home: Path) -> None:
    svc.write_skill("agt", name="my-skill", description="old", content="# old body")
    out = svc.update_skill("agt", name="my-skill", description="new", content="# new body")
    assert out["description"] == "new"
    md = svc.read_skill("agt", "my-skill")
    assert "# new body" in md
    assert "# old body" not in md
    assert "description: \"new\"" in md
    listed = [s for s in svc.list_skills("agt") if s["name"] == "my-skill"]
    assert listed and listed[0]["description"] == "new"


def test_update_skill_requires_existing(hermes_home: Path) -> None:
    with pytest.raises(svc.AgentNotFound):
        svc.update_skill("agt", name="ghost", content="# x")


def test_upsert_mcp_server_preserves_env_when_none(hermes_home: Path) -> None:
    (hermes_home / "profiles" / "mcpagent").mkdir(parents=True)
    svc.upsert_mcp_server(
        "mcpagent", name="srv", transport="sse",
        url="https://a.example/sse", environment="API_KEY=secret\nDEBUG=1",
    )
    # Edit with environment=None must keep the existing env block.
    svc.upsert_mcp_server(
        "mcpagent", name="srv", transport="http_sse",
        url="https://b.example/mcp", environment=None,
    )
    listed = svc.list_mcp_servers("mcpagent")
    assert listed[0]["url"] == "https://b.example/mcp"
    assert listed[0]["transport"] == "http_sse"
    assert listed[0]["env_keys"] == ["API_KEY", "DEBUG"]
    # Edit with empty string clears env.
    svc.upsert_mcp_server(
        "mcpagent", name="srv", transport="http_sse",
        url="https://b.example/mcp", environment="",
    )
    assert svc.list_mcp_servers("mcpagent")[0]["env_keys"] == []


def test_upsert_mcp_server_supports_local_stdio(hermes_home: Path) -> None:
    profile = hermes_home / "profiles" / "mcpagent"
    profile.mkdir(parents=True)
    row = svc.upsert_mcp_server(
        "mcpagent",
        name="local-tools",
        transport="local",
        url="",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp/work"],
        environment="ALLOW=1",
    )
    assert row["transport"] == "local"
    assert row["url"] == ""
    assert row["command"] == "npx"
    assert row["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/work"]
    assert row["env_keys"] == ["ALLOW"]
    cfg = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    entry = cfg["mcp_servers"]["local-tools"]
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/work"]
    assert "url" not in entry
    assert "transport" not in entry

    svc.upsert_mcp_server(
        "mcpagent",
        name="local-tools",
        transport="http_sse",
        url="https://example.com/mcp",
        environment=None,
    )
    cfg = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    entry = cfg["mcp_servers"]["local-tools"]
    assert entry["url"] == "https://example.com/mcp"
    assert "command" not in entry
    assert "args" not in entry


def test_list_profile_names_from_fs(hermes_home: Path) -> None:
    profiles = hermes_home / "profiles"
    (profiles / "alpha").mkdir(parents=True)
    (profiles / "beta").mkdir(parents=True)
    (profiles / "default").mkdir(parents=True)  # reserved — excluded
    (profiles / "Bad-Name").mkdir(parents=True)  # invalid id — excluded
    (profiles / "notes.txt").write_text("not a profile")
    assert svc.list_profile_names_from_fs() == ["alpha", "beta"]


def test_list_agents_fast_reconcile_uses_fs_not_cli(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = hermes_home / "profiles"
    (profiles / "fsagent").mkdir(parents=True)
    cli_called: list[bool] = []

    def _cli_checked() -> tuple[bool, list[str]]:
        cli_called.append(True)
        return True, []

    monkeypatch.setattr(svc, "list_profile_names_checked", _cli_checked)
    storage = get_storage()
    rows = svc.list_agents(user="alice", storage=storage, reconcile=svc.RECONCILE_FAST)
    assert [r.id for r in rows] == ["fsagent"]
    assert cli_called == []


def test_api_list_mode_fast(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (hermes_home / "profiles" / "quick").mkdir(parents=True)
    cli_called: list[bool] = []

    def _cli_checked() -> tuple[bool, list[str]]:
        cli_called.append(True)
        return True, []

    monkeypatch.setattr(svc, "list_profile_names_checked", _cli_checked)
    r = client.get("/api/hermes/agents?mode=fast")
    assert r.status_code == 200
    assert [a["id"] for a in r.json()["items"]] == ["quick"]
    assert cli_called == []


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


def test_api_create_surfaces_bootstrap_warning(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the self-definition bootstrap fails, the create still returns 201 but
    the response carries a non-empty ``bootstrapWarning`` so the WebUI can warn
    the user (regression: market-strategist reported success with empty SOUL)."""
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, ["mp"]))

    def _failing_bootstrap(aid, _args, *, outcome=None, **_k):  # noqa: ANN001, ANN202
        if outcome is not None:
            outcome.ok = False
            outcome.error = "No inference provider configured"
        return 1

    monkeypatch.setattr(svc, "_run_bootstrap", _failing_bootstrap)
    r = client.post("/api/hermes/agents", json={"id": "mp", "name": "MP"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == "mp"
    assert "No inference provider" in body["bootstrapWarning"]


def test_api_create_no_bootstrap_warning_on_success(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean bootstrap yields an empty ``bootstrapWarning``."""
    monkeypatch.setattr(svc, "_run_hermes", _fake_run([]))
    monkeypatch.setattr(svc, "list_profile_names", lambda: [])
    monkeypatch.setattr(svc, "list_profile_names_checked", lambda: (True, ["ok"]))
    r = client.post("/api/hermes/agents", json={"id": "ok", "name": "OK"})
    assert r.status_code == 201, r.text
    assert r.json()["bootstrapWarning"] == ""


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

    def _fake_commit(cmd, *, user, storage=None, config=None, outcome=None):  # noqa: ANN001, ANN202
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


def test_api_create_passes_clone_params(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def _fake_commit(cmd, *, user, storage=None, config=None, outcome=None):  # noqa: ANN001, ANN202
        seen["clone_from"] = cmd.clone_from
        seen["clone_all"] = cmd.clone_all
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
            "cloneFrom": "src",
            "cloneAll": True,
            "modelInheritFrom": "",
        },
    )
    assert r.status_code == 201, r.text
    assert seen["clone_from"] == "src"
    assert seen["clone_all"] is True
    # "" passes through verbatim → "do not inherit"
    assert seen["model_inherit_from"] == ""


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


def test_create_cron_rejects_missing_workdir(
    hermes_home: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "missing-workdir"
    with pytest.raises(svc.AgentIdInvalid, match="workdir path does not exist"):
        svc.create_cron(
            "cron1",
            schedule="30m",
            prompt="do",
            workdir=str(missing),
        )


def test_create_cron_passes_resolved_workdir(
    hermes_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    wd = tmp_path / "cron-wd"
    wd.mkdir()

    svc.create_cron(
        "cron1",
        schedule="30m",
        prompt="do",
        name="n1",
        workdir=str(wd),
    )

    assert calls == [[
        "-p",
        "cron1",
        "cron",
        "create",
        "30m",
        "do",
        "--name",
        "n1",
        "--workdir",
        str(wd.resolve()),
        "--profile",
        "cron1",
    ]]


def test_create_cron_passes_deliver_target(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))

    svc.create_cron(
        "cron1",
        schedule="30m",
        prompt="do",
        deliver="telegram:8940342611",
    )

    assert calls == [[
        "-p",
        "cron1",
        "cron",
        "create",
        "30m",
        "do",
        "--deliver",
        "telegram:8940342611",
        "--profile",
        "cron1",
    ]]


def test_create_cron_omits_deliver_when_local(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))

    svc.create_cron("cron1", schedule="30m", prompt="do", deliver="local")

    assert calls == [[
        "-p",
        "cron1",
        "cron",
        "create",
        "30m",
        "do",
        "--profile",
        "cron1",
    ]]


def _write_cron_jobs(home: Path, agent_id: str, jobs: list[dict]) -> None:
    cron_dir = home / "profiles" / agent_id / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs}), encoding="utf-8"
    )


def test_list_cron_parses_jobs_json_one_entry_per_job(hermes_home: Path) -> None:
    _write_cron_jobs(hermes_home, "cron1", [
        {
            "id": "abc123",
            "name": "morning",
            "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
            "enabled": True,
            "prompt": "say hi",
            "deliver": "telegram",
            "workdir": "/tmp/wd",
            "next_run_at": "2026-06-18T09:00:00+08:00",
            "last_run_at": "2026-06-17T09:00:00+08:00",
            "last_status": "ok",
            "paused_at": None,
        },
        {
            "id": "def456",
            "name": "weekly",
            "schedule": {"expr": "0 18 * * 1"},
            "enabled": True,
            "prompt": "review",
            "paused_at": "2026-06-17T00:00:00+08:00",
        },
    ])

    jobs = svc.list_cron("cron1")

    assert [j["id"] for j in jobs] == ["abc123", "def456"]
    first = jobs[0]
    assert first["name"] == "morning"
    assert first["schedule"] == "0 9 * * *"
    assert first["enabled"] is True
    assert first["prompt"] == "say hi"
    assert first["deliver"] == "telegram"
    assert first["workdir"] == "/tmp/wd"
    assert first["next_run"] == "2026-06-18T09:00:00+08:00"
    assert first["last_run"] == "2026-06-17T09:00:00+08:00 ok"
    # paused_at set => not enabled even though enabled flag is True
    assert jobs[1]["enabled"] is False
    assert jobs[1]["schedule"] == "0 18 * * 1"


def test_list_cron_missing_file_returns_empty(hermes_home: Path) -> None:
    assert svc.list_cron("cron1") == []


def test_edit_cron_forwards_only_changed_fields(
    hermes_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))
    wd = tmp_path / "edit-wd"
    wd.mkdir()

    svc.edit_cron(
        "cron1", "abc123",
        schedule="0 8 * * *", prompt="new prompt", name="renamed",
        deliver="telegram", workdir=str(wd),
    )

    assert calls == [[
        "-p", "cron1", "cron", "edit", "abc123",
        "--schedule", "0 8 * * *",
        "--prompt", "new prompt",
        "--name", "renamed",
        "--deliver", "telegram",
        "--workdir", str(wd.resolve()),
    ]]


def test_edit_cron_skips_blank_fields_and_clears_workdir(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))

    # Only schedule provided; workdir explicitly cleared with empty string.
    svc.edit_cron("cron1", "abc123", schedule="30m", workdir="")

    assert calls == [[
        "-p", "cron1", "cron", "edit", "abc123",
        "--schedule", "30m",
        "--workdir", "",
    ]]


def test_edit_cron_noop_when_nothing_changes(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_run_hermes", _fake_run(calls))

    svc.edit_cron("cron1", "abc123")

    assert calls == []


def test_list_cron_delivery_targets_parses_status_and_send_list(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def _run(args, *, cwd=None, timeout=svc._CLI_TIMEOUT_SEC):  # noqa: ANN001
        calls.append(list(args))
        if args[-2:] == ["--list", "--json"] or args[-1:] == ["--json"]:
            return 0, (
                '{"platforms":{"telegram":["-100123"],"discord":[]}}'
            ), ""
        if args[-1:] == ["--all"]:
            return 0, (
                "◆ Messaging Platforms\n"
                "  Telegram      ✓ configured (home: 8940342611)\n"
                "  Discord       ✗ not configured\n"
                "◆ Gateway Service\n"
            ), ""
        return 1, "", ""

    monkeypatch.setattr(svc, "_run_hermes", _run)
    targets = svc.list_cron_delivery_targets("cron1")

    assert calls[0] == ["-p", "cron1", "send", "--list", "--json"]
    assert calls[1] == ["-p", "cron1", "status", "--all"]
    values = [t["value"] for t in targets]
    assert values[0] == "local"
    # Bare platform = deliver to home chat; the extra send-list channel that
    # differs from home is offered as an explicit platform:chat_id target.
    assert "telegram" in values
    assert "telegram:-100123" in values
    # Home chat is represented by the bare slug, never duplicated as
    # ``telegram:<home>``.
    assert "telegram:8940342611" not in values
    # Unconfigured platform never becomes an option.
    assert "discord" not in values
    assert not any(v.startswith("discord") for v in values)


def test_list_cron_delivery_targets_object_channels_dedup_home(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Hermes emits channel *objects*; a single home-only channel must
    collapse to one bare ``telegram`` option (regression for the garbage
    ``telegram:{'id': ...}`` dropdown entry)."""

    def _run(args, *, cwd=None, timeout=svc._CLI_TIMEOUT_SEC):  # noqa: ANN001
        if args[-2:] == ["--list", "--json"] or args[-1:] == ["--json"]:
            return 0, json.dumps({
                "platforms": {
                    "telegram": [
                        {
                            "id": "8940342611",
                            "name": "Jingjing Chen",
                            "type": "dm",
                            "thread_id": None,
                        }
                    ],
                    "discord": [],
                }
            }), ""
        if args[-1:] == ["--all"]:
            return 0, (
                "◆ Messaging Platforms\n"
                "  Telegram      ✓ configured (home: 8940342611)\n"
                "◆ Gateway Service\n"
            ), ""
        return 1, "", ""

    monkeypatch.setattr(svc, "_run_hermes", _run)
    targets = svc.list_cron_delivery_targets("cron1")

    values = [t["value"] for t in targets]
    assert values == ["local", "telegram"]
    # No stringified dict ever leaks into a target value or label.
    assert not any("{" in t["value"] or "{" in t["label"] for t in targets)


def test_list_cron_delivery_targets_object_channel_uses_name_label(
    hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovered chat that differs from home surfaces as
    ``platform:chat_id`` with a friendly name label."""

    def _run(args, *, cwd=None, timeout=svc._CLI_TIMEOUT_SEC):  # noqa: ANN001
        if args[-2:] == ["--list", "--json"] or args[-1:] == ["--json"]:
            return 0, json.dumps({
                "platforms": {
                    "telegram": [
                        {"id": "111", "name": "Alice", "type": "dm"},
                        {"id": "222", "name": "Bob", "type": "dm"},
                    ]
                }
            }), ""
        if args[-1:] == ["--all"]:
            return 0, (
                "◆ Messaging Platforms\n"
                "  Telegram      ✓ configured (home: 111)\n"
                "◆ Gateway Service\n"
            ), ""
        return 1, "", ""

    monkeypatch.setattr(svc, "_run_hermes", _run)
    targets = svc.list_cron_delivery_targets("cron1")

    values = [t["value"] for t in targets]
    assert values == ["local", "telegram", "telegram:222"]
    label_222 = next(t["label"] for t in targets if t["value"] == "telegram:222")
    assert label_222 == "telegram (Bob)"


def test_api_get_cron_includes_delivery_targets(
    client: TestClient, hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="cron1", name="Cron", profile_root="x", created_by_user=owner)
    )
    monkeypatch.setattr(svc, "cron_available", lambda: True)
    monkeypatch.setattr(svc, "list_cron", lambda _id: [])
    monkeypatch.setattr(
        svc,
        "list_cron_delivery_targets",
        lambda _id: [{"value": "local", "label": "local"}, {"value": "telegram", "label": "telegram"}],
    )

    r = client.get("/api/hermes/agents/cron1/settings/cron")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deliveryTargets"] == [
        {"value": "local", "label": "local"},
        {"value": "telegram", "label": "telegram"},
    ]


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


def test_api_chat_without_attachments_unaffected(
    client: TestClient,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="chat-no-attach-probe", name="C", profile_root="x", created_by_user=owner)
    )
    workdir = tmp_path / "hermes-chat-no-attach-probe"
    workdir.mkdir(parents=True, exist_ok=True)
    from app.api import hermes_agents as router_mod

    class _DoneJob:
        def snapshot(self) -> dict[str, object]:
            return {
                "status": "done",
                "steps": [],
                "progress": {"toolCalls": 0, "apiCalls": 0, "messageCount": 0, "elapsedSec": 0.0},
                "final": "ok",
                "error": "",
                "startedAtMono": 0.0,
            }

    def _fake_start_chat(
        agent_id: str,
        *,
        message: str,
        workdir: str,
        resume: bool,
        session_key: str,
        resume_session_id: str | None = None,
        attachment_paths: list[str] | None = None,
        native_attachment_flag: str | None = None,
    ):
        del (
            agent_id,
            workdir,
            resume,
            session_key,
            resume_session_id,
            attachment_paths,
            native_attachment_flag,
        )
        assert message == "plain message"
        return _DoneJob()

    monkeypatch.setattr(router_mod.chat_svc, "start_chat", _fake_start_chat)
    r = client.post(
        "/api/hermes/agents/chat-no-attach-probe/chat",
        json={"message": "plain message", "workdir": str(workdir)},
    )
    assert r.status_code == 200, r.text


def test_api_chat_attachment_upload_and_path_injection(
    client: TestClient,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="chat-att-path", name="C", profile_root="x", created_by_user=owner)
    )
    workdir = tmp_path / "hermes-chat-path"
    workdir.mkdir(parents=True, exist_ok=True)
    from app.api import hermes_agents as router_mod

    captured: dict[str, object] = {}

    class _DoneJob:
        def snapshot(self) -> dict[str, object]:
            return {
                "status": "done",
                "steps": [],
                "progress": {"toolCalls": 0, "apiCalls": 0, "messageCount": 0, "elapsedSec": 0.0},
                "final": "ok",
                "error": "",
                "startedAtMono": 0.0,
            }

    def _fake_start_chat(
        agent_id: str,
        *,
        message: str,
        workdir: str,
        resume: bool,
        session_key: str,
        resume_session_id: str | None = None,
        attachment_paths: list[str] | None = None,
        native_attachment_flag: str | None = None,
    ):
        captured.update(
            {
                "agent_id": agent_id,
                "message": message,
                "workdir": workdir,
                "resume": resume,
                "session_key": session_key,
                "resume_session_id": resume_session_id,
                "attachment_paths": attachment_paths,
                "native_attachment_flag": native_attachment_flag,
            }
        )
        return _DoneJob()

    monkeypatch.setattr(router_mod.chat_svc, "start_chat", _fake_start_chat)
    uploaded = client.post(
        "/api/hermes/agents/chat-att-path/chat/attachments",
        params={"filename": "note.md", "workdir": str(workdir)},
        data=b"# note\n",
        headers={"Content-Type": "text/markdown"},
    )
    assert uploaded.status_code == 200, uploaded.text
    uploaded_body = uploaded.json()
    assert uploaded_body["attachment"]["route"] == "path_injection"
    attachment = uploaded_body["attachment"]
    run = client.post(
        "/api/hermes/agents/chat-att-path/chat",
        json={
            "message": "use this file",
            "workdir": str(workdir),
            "attachments": [attachment],
        },
    )
    assert run.status_code == 200, run.text
    assert "ClawsomeFlow Uploaded Attachments" in str(captured["message"])
    assert captured["attachment_paths"] is None


def test_api_chat_attachment_uses_path_injection_only(
    client: TestClient,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="chat-att-force-path", name="C", profile_root="x", created_by_user=owner)
    )
    workdir = tmp_path / "hermes-chat-force-path"
    workdir.mkdir(parents=True, exist_ok=True)
    from app.api import hermes_agents as router_mod

    captured: dict[str, object] = {}

    class _DoneJob:
        def snapshot(self) -> dict[str, object]:
            return {
                "status": "done",
                "steps": [],
                "progress": {"toolCalls": 0, "apiCalls": 0, "messageCount": 0, "elapsedSec": 0.0},
                "final": "ok",
                "error": "",
                "startedAtMono": 0.0,
            }

    def _fake_start_chat(
        agent_id: str,
        *,
        message: str,
        workdir: str,
        resume: bool,
        session_key: str,
        resume_session_id: str | None = None,
        attachment_paths: list[str] | None = None,
        native_attachment_flag: str | None = None,
    ):
        captured.update(
            {
                "agent_id": agent_id,
                "message": message,
                "workdir": workdir,
                "resume": resume,
                "session_key": session_key,
                "resume_session_id": resume_session_id,
                "attachment_paths": attachment_paths,
                "native_attachment_flag": native_attachment_flag,
            }
        )
        return _DoneJob()

    monkeypatch.setattr(router_mod.chat_svc, "start_chat", _fake_start_chat)
    uploaded = client.post(
        "/api/hermes/agents/chat-att-force-path/chat/attachments",
        params={"filename": "photo.png", "workdir": str(workdir)},
        data=b"png",
        headers={"Content-Type": "image/png"},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    run = client.post(
        "/api/hermes/agents/chat-att-force-path/chat",
        json={
            "message": "force path injection",
            "workdir": str(workdir),
            "attachments": [attachment],
        },
    )
    assert run.status_code == 200, run.text
    assert "ClawsomeFlow Uploaded Attachments" in str(captured["message"])
    assert captured["native_attachment_flag"] is None
    assert captured["attachment_paths"] is None


def test_api_chat_accepts_attachments_without_text_message(
    client: TestClient,
    hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="chat-att-empty", name="C", profile_root="x", created_by_user=owner)
    )
    workdir = tmp_path / "hermes-chat-empty"
    workdir.mkdir(parents=True, exist_ok=True)
    from app.api import hermes_agents as router_mod

    captured: dict[str, object] = {}

    class _DoneJob:
        def snapshot(self) -> dict[str, object]:
            return {
                "status": "done",
                "steps": [],
                "progress": {"toolCalls": 0, "apiCalls": 0, "messageCount": 0, "elapsedSec": 0.0},
                "final": "ok",
                "error": "",
                "startedAtMono": 0.0,
            }

    def _fake_start_chat(
        agent_id: str,
        *,
        message: str,
        workdir: str,
        resume: bool,
        session_key: str,
        resume_session_id: str | None = None,
        attachment_paths: list[str] | None = None,
        native_attachment_flag: str | None = None,
    ):
        del (
            agent_id,
            workdir,
            resume,
            session_key,
            resume_session_id,
            attachment_paths,
            native_attachment_flag,
        )
        captured["message"] = message
        return _DoneJob()

    monkeypatch.setattr(router_mod.chat_svc, "start_chat", _fake_start_chat)
    uploaded = client.post(
        "/api/hermes/agents/chat-att-empty/chat/attachments",
        params={"filename": "doc.md", "workdir": str(workdir)},
        data=b"doc",
        headers={"Content-Type": "text/markdown"},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()["attachment"]
    run = client.post(
        "/api/hermes/agents/chat-att-empty/chat",
        json={
            "message": "",
            "workdir": str(workdir),
            "attachments": [attachment],
        },
    )
    assert run.status_code == 200, run.text
    assert "Please inspect the uploaded files." in str(captured["message"])


def test_api_create_cron_rejects_missing_workdir(
    client: TestClient, hermes_home: Path, tmp_path: Path
) -> None:
    owner = svc.load_config().default_user
    get_storage().hermes_create(
        HermesAgent(id="cron1", name="Cron", profile_root="x", created_by_user=owner)
    )
    r = client.post(
        "/api/hermes/agents/cron1/settings/cron",
        json={
            "schedule": "30m",
            "prompt": "do it",
            "workdir": str(tmp_path / "missing-dir"),
        },
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "INVALID_PAYLOAD"


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
    local_put = client.put(
        "/api/hermes/agents/mcpagent/settings/mcp",
        json={
            "name": "local-tools",
            "transport": "local",
            "command": "python",
            "args": ["-m", "my_mcp_server"],
        },
    )
    assert local_put.status_code == 200, local_put.text
    assert local_put.json()["transport"] == "local"
    assert local_put.json()["command"] == "python"
    listed = client.get("/api/hermes/agents/mcpagent/settings/mcp")
    assert listed.status_code == 200
    local = next(item for item in listed.json() if item["name"] == "local-tools")
    assert local["url"] == ""
    assert local["args"] == ["-m", "my_mcp_server"]
    rm = client.delete("/api/hermes/agents/mcpagent/settings/mcp/remote-api")
    assert rm.status_code == 204


# ──────────────────────────────────────────────────────────────────────
# Global HermesAgentError exception handler (app.api.errors)
# ──────────────────────────────────────────────────────────────────────


def test_hermes_error_escaping_route_returns_canonical_json() -> None:
    """A HermesAgentError that escapes a route WITHOUT a local try/except must
    still surface as the canonical error JSON (same codes as _map_service_error),
    not a bare 500 — the mapping is single-sourced in app.api.errors."""
    from fastapi import FastAPI

    from app.api.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    async def _boom() -> None:
        raise svc.AgentNotFound("no such agent", details={"agentId": "ghost"})

    app.add_api_route("/api/_test/hermes-boom", _boom, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/_test/hermes-boom")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "AGENT_NOT_FOUND"
    assert body["message"] == "no such agent"
    assert body["details"] == {"agentId": "ghost"}


def test_hermes_error_mapping_single_source_matches_route_mapper() -> None:
    from app.api.errors import map_hermes_agent_error
    from app.api.hermes_agents import _map_service_error

    for exc_cls, (code, status) in {
        svc.AgentIdInvalid: ("INVALID_PAYLOAD", 400),
        svc.AgentAlreadyExists: ("AGENT_ALREADY_EXISTS", 409),
        svc.AgentNotFound: ("AGENT_NOT_FOUND", 404),
        svc.AgentInUse: ("AGENT_IN_USE", 409),
        svc.HermesUnavailable: ("HERMES_UNAVAILABLE", 503),
        svc.ProfileOpFailed: ("HERMES_CLI_FAILED", 502),
        svc.AgentCreateCancelled: ("AGENT_CREATE_CANCELLED", 409),
        svc.HermesAgentError: ("HERMES_ERROR", 500),
    }.items():
        exc = exc_cls("msg")
        for mapped in (map_hermes_agent_error(exc), _map_service_error(exc)):
            assert (mapped.code, mapped.status_code) == (code, status)
