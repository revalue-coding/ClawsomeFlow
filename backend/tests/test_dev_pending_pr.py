"""Developer-mode "PR module" tests.

Covers the full pending-PR chain:

* ``compute_dev_pending_pr_agent_ids`` — dev-mode-only marker computation.
* ``finalize_run`` writes the marker for dev runs (manual + scheduled).
* Terminal tail cleanup preserves pending-PR worktrees (selective cleanup).
* ``/api/runs/{id}/pending-prs`` list gating (dev-mode-now, terminal,
  worktree-on-disk) + submit / discard actions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.main import create_app
from app.models import (
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    OnFailure,
    RunStatus,
)
from app.scheduler import finalize as fin
from app.scheduler.run_metadata import DEV_PENDING_PR_AGENT_IDS_KEY
from app.storage import get_storage


@pytest.fixture
def app_client(tmp_path: Path):
    cfg = load_config()
    cfg = cfg.model_copy(update={"default_user": "alice"})
    save_config(cfg)
    with TestClient(create_app()) as c:
        yield c


def _dev_spec(*, mode: str = "dev", alice_auto_merge: bool = False) -> FlowSpec:
    """alice = no-merge worker, bob = auto-merge worker, leader = summary."""
    variables: dict[str, str] = {}
    if mode == "dev":
        variables["csflow.dev_mode"] = "true"
    elif mode == "easy":
        variables["csflow.easy_mode"] = "true"
    return FlowSpec(
        agents=[
            FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="bob", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=False, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=True, merge_strategy=MergeStrategy.manual,
                      on_failure=OnFailure.retry, max_retries=2),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="x",
                     description="", depends_on=[],
                     dev_auto_merge=alice_auto_merge),
            FlowTask(id="t2", owner_agent_id="bob", subject="y",
                     description="", depends_on=[], dev_auto_merge=True),
            FlowTask(id="ts", owner_agent_id="leader", subject="s",
                     description="", depends_on=["t1", "t2"],
                     is_leader_summary=True),
        ],
        variables=variables,
    )


def _make_flow_and_run(
    *,
    mode: str = "dev",
    alice_auto_merge: bool = False,
    status: RunStatus = RunStatus.completed,
    inputs: dict[str, Any] | None = None,
    cleanup_team: bool = True,
) -> tuple[Flow, FlowRun]:
    storage = get_storage()
    flow = Flow(
        name="dev-pr", description="", owner_user="alice",
        cleanup_team_on_finish=cleanup_team,
    ).with_spec(_dev_spec(mode=mode, alice_auto_merge=alice_auto_merge))
    flow = storage.flow_create(flow)
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-devpr",
        status=status, inputs=inputs or {}, user="alice",
    ))
    return flow, run


# ── marker computation ────────────────────────────────────────────────


def test_compute_marker_dev_mode_only_no_merge_agents() -> None:
    flow, run = _make_flow_and_run(mode="dev", alice_auto_merge=False)
    ids = fin.compute_dev_pending_pr_agent_ids(flow=flow, run=run)
    assert ids == ["alice"]


def test_compute_marker_empty_when_all_tasks_auto_merge() -> None:
    flow, run = _make_flow_and_run(mode="dev", alice_auto_merge=True)
    assert fin.compute_dev_pending_pr_agent_ids(flow=flow, run=run) == []


@pytest.mark.parametrize("mode", ["easy", "normal"])
def test_compute_marker_empty_outside_dev_mode(mode: str) -> None:
    flow, run = _make_flow_and_run(mode=mode, alice_auto_merge=False)
    assert fin.compute_dev_pending_pr_agent_ids(flow=flow, run=run) == []


# ── finalize writes the marker ────────────────────────────────────────


class _NoopCli:
    async def team_cleanup(self, *, team: str, force: bool = True):
        return None

    async def workspace_list(self, *, team: str, repo: str | None = None):
        return []


@pytest.mark.asyncio
async def test_finalize_dev_manual_writes_marker_and_awaits_complaint() -> None:
    flow, run = _make_flow_and_run(
        mode="dev", alice_auto_merge=False, status=RunStatus.running,
    )
    spec = FlowSpec.model_validate(flow.spec)
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=_NoopCli(), mcp=object(),
    )
    assert out.final_status == RunStatus.awaiting_user_complaint
    assert run.inputs.get(DEV_PENDING_PR_AGENT_IDS_KEY) == ["alice"]


@pytest.mark.asyncio
async def test_finalize_scheduled_dev_writes_marker(monkeypatch) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", alice_auto_merge=False, status=RunStatus.running,
        cleanup_team=False,
    )
    run.is_scheduled = True
    get_storage().run_update(run)
    spec = FlowSpec.model_validate(flow.spec)
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=_NoopCli(), mcp=object(),
    )
    assert out.final_status == RunStatus.completed
    assert run.inputs.get(DEV_PENDING_PR_AGENT_IDS_KEY) == ["alice"]


@pytest.mark.asyncio
async def test_finalize_easy_manual_writes_no_marker() -> None:
    flow, run = _make_flow_and_run(
        mode="easy", alice_auto_merge=False, status=RunStatus.running,
    )
    spec = FlowSpec.model_validate(flow.spec)
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=_NoopCli(), mcp=object(),
    )
    assert out.final_status == RunStatus.awaiting_user_complaint
    assert DEV_PENDING_PR_AGENT_IDS_KEY not in (run.inputs or {})


# ── terminal tail cleanup preserves pending-PR worktrees ─────────────


class _RecordingCli:
    def __init__(self, workspace_rows: list[dict[str, str]] | None = None) -> None:
        self.workspace_rows = list(workspace_rows or [])
        self.cleanup_calls: list[dict] = []
        self.team_cleanup_calls: list[str] = []

    async def team_cleanup(self, *, team: str, force: bool = True):
        del force
        self.team_cleanup_calls.append(team)

    async def workspace_list(self, *, team: str, repo: str | None = None):
        del repo
        return [r for r in self.workspace_rows if r.get("team_name") == team]

    async def workspace_cleanup_with_diagnostics(self, *, team: str, agent: str, **kw):
        from app.integrations.clawteam_cli import (
            WorkspaceCleanupAttempt,
            WorkspaceCleanupResult,
        )
        self.cleanup_calls.append({"team": team, "agent": agent, **kw})
        attempt = WorkspaceCleanupAttempt(argv=["clawteam"], exit_code=0, stderr="")
        return WorkspaceCleanupResult(success=True, attempts=[attempt])

    async def workspace_cleanup(self, *, team: str, agent: str, **kw):
        self.cleanup_calls.append({"team": team, "agent": agent, **kw})
        return True


@pytest.mark.asyncio
async def test_tail_cleanup_preserves_dev_pending_pr_worktrees() -> None:
    flow, run = _make_flow_and_run(
        mode="dev", alice_auto_merge=False, status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    cli = _RecordingCli(workspace_rows=[
        {"team_name": run.team_name, "agent_name": "alice"},
        {"team_name": run.team_name, "agent_name": "bob"},
        {"team_name": run.team_name, "agent_name": "leader"},
    ])
    out = await fin.run_terminal_tail_cleanup(
        run=run, flow=flow, storage=get_storage(), cli=cli,
    )
    assert out.team_cleaned is False
    assert cli.team_cleanup_calls == []
    cleaned = {c["agent"] for c in cli.cleanup_calls}
    assert "alice" not in cleaned
    assert cleaned == {"bob", "leader"}
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    skipped = [e for e in events if e.type == "team_cleanup_skipped"]
    assert skipped
    assert skipped[-1].payload.get("reason") == "dev_pending_pr"
    assert skipped[-1].payload.get("preserved_agents") == ["alice"]


@pytest.mark.asyncio
async def test_tail_cleanup_runs_team_cleanup_once_marker_empty() -> None:
    flow, run = _make_flow_and_run(
        mode="dev", alice_auto_merge=False, status=RunStatus.completed,
        inputs={},
    )
    cli = _RecordingCli()
    out = await fin.run_terminal_tail_cleanup(
        run=run, flow=flow, storage=get_storage(), cli=cli,
    )
    assert out.team_cleaned is True
    assert cli.team_cleanup_calls == [run.team_name]


# ── API endpoints ─────────────────────────────────────────────────────


class _ApiStubCli:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    async def workspace_list(self, *, team: str, repo: str | None = None):
        del team, repo
        return list(self.rows)

    async def workspace_agent_patch(self, *, team: str, agent: str, repo=None, **kw):
        del team, repo, kw
        return {
            "branch": f"clawteam/csflow-devpr/{agent}",
            "base_branch": "main",
            "repo_root": "/tmp/r",
            "patch": "diff --git a/x b/x",
            "patch_truncated": False,
            "uncommitted_patch": "",
            "uncommitted_truncated": False,
            "base_ahead": 0,
            "branch_ahead": 1,
        }


def _stub_worktree_rows(tmp_path: Path, agents: list[str]) -> list[dict[str, str]]:
    rows = []
    for a in agents:
        wt = tmp_path / f"wt-{a}"
        wt.mkdir(parents=True, exist_ok=True)
        rows.append({
            "agent_name": a,
            "branch_name": f"clawteam/csflow-devpr/{a}",
            "base_branch": "main",
            "repo_root": "/tmp/r",
            "worktree_path": str(wt),
            "team_name": "csflow-devpr",
        })
    return rows


def test_pending_prs_list_dev_mode_terminal(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    r = app_client.get(f"/api/runs/{run.id}/pending-prs")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["agentId"] == "alice"
    assert items[0]["targetBranch"] == "main"
    assert items[0]["branch"] == "clawteam/csflow-devpr/alice"


def test_pending_prs_list_empty_when_flow_not_dev_mode(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Run has the marker (executed in dev mode), but the Flow was later
    # switched away from dev mode → module hidden.
    flow, run = _make_flow_and_run(
        mode="normal", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    r = app_client.get(f"/api/runs/{run.id}/pending-prs")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_pending_prs_list_empty_when_not_terminal(
    app_client: TestClient, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.awaiting_user_complaint,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    r = app_client.get(f"/api/runs/{run.id}/pending-prs")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_pending_prs_list_skips_missing_worktree(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    # Simulate an out-of-band deletion: path recorded but gone from disk.
    Path(rows[0]["worktree_path"]).rmdir()
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    r = app_client.get(f"/api/runs/{run.id}/pending-prs")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_pending_pr_diff(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    r = app_client.get(f"/api/runs/{run.id}/pending-prs/alice/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == "alice"
    assert body["patch"].startswith("diff --git")
    # Not in the marker → 404.
    r2 = app_client.get(f"/api/runs/{run.id}/pending-prs/bob/diff")
    assert r2.status_code == 404
    assert r2.json()["error"] == "PR_NOT_PENDING"


def test_submit_pending_pr_success_removes_marker_and_cleans_worktree(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))

    commands: list[list[str]] = []

    async def fake_run_pr_command(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        commands.append(list(argv))
        if argv[0] == "git":
            return 0, "", ""
        return 0, "https://github.com/acme/x/pull/7\n", ""

    monkeypatch.setattr(runs_mod, "_run_pr_command", fake_run_pr_command)

    cleaned: list[str] = []

    async def fake_cleanup(*, run, agent_id, storage, **kw):
        del run, storage, kw
        cleaned.append(agent_id)
        return True

    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
    )

    tail_calls: list[str] = []

    async def fake_tail(*, run, storage, flow=None, **kw):
        del storage, flow, kw
        tail_calls.append(run.id)

    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_tail)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/submit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["prUrl"] == "https://github.com/acme/x/pull/7"
    assert commands[0][:2] == ["git", "push"]
    assert commands[1][:3] == ["gh", "pr", "create"]
    refreshed = get_storage().run_get(run.id)
    assert DEV_PENDING_PR_AGENT_IDS_KEY not in (refreshed.inputs or {})
    assert cleaned == ["alice"]
    assert tail_calls == [run.id]
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=50)
    assert any(e.type == "dev_pr_submitted" for e in events)


def test_submit_pending_pr_push_failure_keeps_everything(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))

    async def fake_run_pr_command(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        return 1, "", "fatal: no configured push destination"

    monkeypatch.setattr(runs_mod, "_run_pr_command", fake_run_pr_command)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/submit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert "git push failed" in body["message"]
    refreshed = get_storage().run_get(run.id)
    assert refreshed.inputs.get(DEV_PENDING_PR_AGENT_IDS_KEY) == ["alice"]
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=50)
    assert any(e.type == "dev_pr_submit_failed" for e in events)


def test_discard_pending_pr_removes_marker_and_cleans(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice", "bob"]},
    )
    from app.api import runs as runs_mod

    cleaned: list[str] = []

    async def fake_cleanup(*, run, agent_id, storage, **kw):
        del run, storage, kw
        cleaned.append(agent_id)
        return True

    monkeypatch.setattr(
        runs_mod,
        "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
    )

    tail_calls: list[str] = []

    async def fake_tail(*, run, storage, flow=None, **kw):
        del storage, flow, kw
        tail_calls.append(run.id)

    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_tail)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/discard")
    assert r.status_code == 200, r.text
    refreshed = get_storage().run_get(run.id)
    assert refreshed.inputs.get(DEV_PENDING_PR_AGENT_IDS_KEY) == ["bob"]
    assert cleaned == ["alice"]
    # Marker not yet empty → deferred team cleanup NOT triggered.
    assert tail_calls == []

    r2 = app_client.post(f"/api/runs/{run.id}/pending-prs/bob/discard")
    assert r2.status_code == 200, r2.text
    refreshed = get_storage().run_get(run.id)
    assert DEV_PENDING_PR_AGENT_IDS_KEY not in (refreshed.inputs or {})
    assert cleaned == ["alice", "bob"]
    assert tail_calls == [run.id]
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=50)
    assert len([e for e in events if e.type == "dev_pr_discarded"]) == 2


def test_discard_pending_pr_unknown_agent_404(app_client: TestClient) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    r = app_client.post(f"/api/runs/{run.id}/pending-prs/ghost/discard")
    assert r.status_code == 404
    assert r.json()["error"] == "PR_NOT_PENDING"
