"""Developer-mode "PR module" tests.

Covers the full pending-PR chain:

* ``compute_dev_pending_pr_agent_ids`` — dev-mode-only marker computation.
* ``finalize_run`` writes the marker for dev runs (manual + scheduled).
* Terminal tail cleanup preserves pending-PR worktrees (selective cleanup).
* ``/api/runs/{id}/pending-prs`` list gating (dev-mode-now, terminal,
  worktree-on-disk) + submit / discard actions.
"""

from __future__ import annotations

import itertools
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


_TEAM_SEQ = itertools.count(1)


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
    # team_name is UNIQUE — some tests create several runs.
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1,
        team_name=f"csflow-devpr{next(_TEAM_SEQ)}",
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
    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        dirty: tuple[bool, list[str]] = (False, []),
    ) -> None:
        self.rows = rows
        self.dirty = dirty
        self.dirty_checks: list[str] = []

    async def workspace_list(self, *, team: str, repo: str | None = None):
        del team, repo
        return list(self.rows)

    async def workspace_has_uncommitted_changes(self, *, worktree_path: str):
        self.dirty_checks.append(worktree_path)
        return self.dirty

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


def test_pending_prs_list_shows_at_awaiting_user_complaint(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # The module surfaces as soon as the run parks on complaint input —
    # worktrees are alive (cleanup deferred to complaint end).
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.awaiting_user_complaint,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    r = app_client.get(f"/api/runs/{run.id}/pending-prs")
    assert r.status_code == 200
    assert [i["agentId"] for i in r.json()["items"]] == ["alice"]


def test_pending_prs_list_visible_during_complaint_processing(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # List stays visible during complaint_processing (read-only); actions are
    # gated separately via _PR_MODULE_ACTIONABLE_STATUSES.
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.complaint_processing,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    r = app_client.get(f"/api/runs/{run.id}/pending-prs")
    assert r.status_code == 200
    assert [i["agentId"] for i in r.json()["items"]] == ["alice"]


def test_pending_prs_list_hidden_while_running(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.running,
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


# ── direct merge into baseline (item 4) ───────────────────────────────


class _MergeStubCli(_ApiStubCli):
    def __init__(self, rows: list[dict[str, str]], merge_result=(True, "")) -> None:
        super().__init__(rows)
        self.merge_result = merge_result
        self.merge_calls: list[dict] = []

    async def workspace_merge(self, *, team: str, agent: str, repo=None, target=None):
        self.merge_calls.append({"team": team, "agent": agent, "repo": repo, "target": target})
        return self.merge_result


def test_merge_pending_pr_success(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    cli = _MergeStubCli(rows, merge_result=(True, ""))
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: cli)

    cleaned: list[str] = []

    async def fake_cleanup(*, run, agent_id, storage, **kw):
        del run, storage, kw
        cleaned.append(agent_id)
        return True

    monkeypatch.setattr(
        runs_mod, "cleanup_non_openclaw_workspace_after_review_decision", fake_cleanup,
    )

    tail: list[str] = []

    async def fake_tail(*, run, storage, flow=None, **kw):
        del storage, flow, kw
        tail.append(run.id)

    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_tail)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/merge")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert cli.merge_calls and cli.merge_calls[0]["target"] == "main"
    refreshed = get_storage().run_get(run.id)
    assert DEV_PENDING_PR_AGENT_IDS_KEY not in (refreshed.inputs or {})
    assert cleaned == ["alice"]
    assert tail == [run.id]
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=50)
    assert any(e.type == "dev_pr_merged" for e in events)


def test_merge_pending_pr_failure_keeps_marker(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    cli = _MergeStubCli(rows, merge_result=(False, "CONFLICT (content): merge conflict in x"))
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: cli)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/merge")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is False
    refreshed = get_storage().run_get(run.id)
    assert refreshed.inputs.get(DEV_PENDING_PR_AGENT_IDS_KEY) == ["alice"]
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=50)
    assert any(e.type == "merge_conflict" for e in events)


def test_merge_pending_pr_rejected_when_not_dev_mode(
    app_client: TestClient, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="normal", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/merge")
    assert r.status_code == 409
    assert r.json()["error"] == "NOT_DEV_MODE"


# ── abort / abnormal-terminal robustness ──────────────────────────────


@pytest.mark.parametrize("status", [RunStatus.aborted, RunStatus.failed])
def test_pending_prs_hidden_for_abnormal_terminal(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    status: RunStatus,
) -> None:
    # Even with a lingering marker + on-disk worktree, an aborted/failed run
    # must never show the PR module.
    flow, run = _make_flow_and_run(
        mode="dev", status=status,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))
    r = app_client.get(f"/api/runs/{run.id}/pending-prs")
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_finalize_abort_clears_marker_and_force_cleans() -> None:
    flow, run = _make_flow_and_run(
        mode="dev", alice_auto_merge=False, status=RunStatus.running,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    spec = FlowSpec.model_validate(flow.spec)
    cli = _RecordingCli(workspace_rows=[
        {"team_name": run.team_name, "agent_name": "alice"},
    ])
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False,
                          aborted=True),
        storage=get_storage(), cli=cli, mcp=object(),
    )
    assert out.final_status == RunStatus.aborted
    # Marker cleared and full team cleanup ran (no preservation on abort).
    assert DEV_PENDING_PR_AGENT_IDS_KEY not in (run.inputs or {})
    assert cli.team_cleanup_calls == [run.team_name]


@pytest.mark.asyncio
async def test_tail_cleanup_ignores_marker_on_aborted_status() -> None:
    # A run that reached aborted with a stale marker still force-cleans.
    flow, run = _make_flow_and_run(
        mode="dev", alice_auto_merge=False, status=RunStatus.aborted,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    cli = _RecordingCli(workspace_rows=[
        {"team_name": run.team_name, "agent_name": "alice"},
    ])
    out = await fin.run_terminal_tail_cleanup(
        run=run, flow=flow, storage=get_storage(), cli=cli,
    )
    assert out.team_cleaned is True
    assert cli.team_cleanup_calls == [run.team_name]


# ── complaint-window semantics (deferred cleanup + action gating) ─────


def _patch_pr_pipeline(monkeypatch, runs_mod):
    """Track per-agent worktree cleanup + tail cleanup + git subprocess calls."""
    calls = {"cleanup": [], "tail": [], "cmds": []}

    async def fake_cleanup(*, run, agent_id, storage, **kw):
        del run, storage, kw
        calls["cleanup"].append(agent_id)
        return True

    async def fake_tail(*, run, storage, flow=None, **kw):
        del storage, flow, kw
        calls["tail"].append(run.id)

    async def fake_cmd(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        calls["cmds"].append(list(argv))
        if argv[0] == "gh":
            return 0, "https://github.com/acme/x/pull/9\n", ""
        return 0, "", ""

    monkeypatch.setattr(
        runs_mod, "cleanup_non_openclaw_workspace_after_review_decision", fake_cleanup,
    )
    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_tail)
    monkeypatch.setattr(runs_mod, "_run_pr_command", fake_cmd)
    return calls


def test_actions_during_awaiting_complaint_defer_worktree_cleanup(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # merge + discard while the run awaits complaint input: marker updates,
    # but NO worktree is deleted (complaint fix agents need them as cwd);
    # everything is swept once the complaint phase ends.
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.awaiting_user_complaint,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice", "bob"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice", "bob"])
    cli = _MergeStubCli(rows, merge_result=(True, ""))
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: cli)
    calls = _patch_pr_pipeline(monkeypatch, runs_mod)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/merge")
    assert r.status_code == 200 and r.json()["success"] is True
    r2 = app_client.post(f"/api/runs/{run.id}/pending-prs/bob/discard")
    assert r2.status_code == 200

    refreshed = get_storage().run_get(run.id)
    assert DEV_PENDING_PR_AGENT_IDS_KEY not in (refreshed.inputs or {})
    # Deferred: no per-agent cleanup, no tail cleanup while awaiting complaint.
    assert calls["cleanup"] == []
    assert calls["tail"] == []


def test_actions_rejected_during_complaint_processing(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.complaint_processing,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _MergeStubCli(rows))
    for action in ("merge", "submit", "discard"):
        r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/{action}")
        assert r.status_code == 409, action
        assert r.json()["error"] == "PR_NOT_ACTIONABLE", action


def test_submit_clears_uncommitted_and_instruments(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # A dirty worktree (agent skipped its mandatory commit step) is logged,
    # a RunEvent is emitted, and the dirt is reset before push.
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    cli = _ApiStubCli(rows, dirty=(True, [" M foo.py", "?? scratch.txt"]))
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: cli)
    calls = _patch_pr_pipeline(monkeypatch, runs_mod)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/submit")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    # Reset + clean ran BEFORE push / pr-create.
    assert calls["cmds"][0] == ["git", "reset", "--hard", "HEAD"]
    assert calls["cmds"][1] == ["git", "clean", "-fd"]
    assert calls["cmds"][2][:2] == ["git", "push"]
    assert calls["cmds"][3][:3] == ["gh", "pr", "create"]
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=50)
    cleared = [e for e in events if e.type == "worktree_uncommitted_cleared"]
    assert len(cleared) == 1
    assert cleared[0].payload.get("entries") == [" M foo.py", "?? scratch.txt"]


def test_merge_clean_worktree_skips_reset(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    flow, run = _make_flow_and_run(
        mode="dev", status=RunStatus.completed,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"]},
    )
    from app.api import runs as runs_mod

    rows = _stub_worktree_rows(tmp_path, ["alice"])
    cli = _MergeStubCli(rows, merge_result=(True, ""))
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: cli)
    calls = _patch_pr_pipeline(monkeypatch, runs_mod)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/merge")
    assert r.status_code == 200 and r.json()["success"] is True
    # Clean worktree → no git reset/clean subprocesses at all.
    assert calls["cmds"] == []
    assert cli.dirty_checks  # the check itself DID run
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=50)
    assert not any(e.type == "worktree_uncommitted_cleared" for e in events)


def test_revert_blocked_while_active_allowed_awaiting_complaint(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from app.api import runs as runs_mod

    class _RevertCli:
        async def run_merged_agent_patch(self, *, team, agent, repo, **kw):
            del team, repo, kw
            return {
                "repo_root": "/tmp/r", "branch": f"x/{agent}", "merge_count": 1,
                "commit_count": 1, "files_changed": 1, "insertions": 1,
                "deletions": 0, "patch": "", "patch_truncated": False,
            }

        async def revert_agent_merges(self, *, team, agent, repo, target_branch):
            del team, agent, repo
            return {
                "ok": True, "target_branch": target_branch,
                "merge_shas": ["abc"], "revert_head": "def",
                "nothing_to_revert": False, "message": "ok",
            }

    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _RevertCli())

    # Active statuses → 409.
    flow, run = _make_flow_and_run(mode="dev", status=RunStatus.complaint_processing)
    r = app_client.post(f"/api/runs/{run.id}/run-diff/alice/revert")
    assert r.status_code == 409
    assert r.json()["error"] == "MERGE_REVERT_NOT_ALLOWED"

    # awaiting_user_complaint → allowed; revert must NOT touch the pending-PR
    # marker (bob stays) so terminal cleanup later removes alice's worktree
    # (not preserved) and keeps bob's.
    flow2, run2 = _make_flow_and_run(
        mode="dev", status=RunStatus.awaiting_user_complaint,
        inputs={DEV_PENDING_PR_AGENT_IDS_KEY: ["bob"]},
    )
    r2 = app_client.post(f"/api/runs/{run2.id}/run-diff/alice/revert")
    assert r2.status_code == 200, r2.text
    assert r2.json()["ok"] is True
    refreshed = get_storage().run_get(run2.id)
    assert refreshed.inputs.get(DEV_PENDING_PR_AGENT_IDS_KEY) == ["bob"]
    from app.scheduler.run_metadata import PRESERVE_WORKTREE_AGENT_IDS_KEY
    assert PRESERVE_WORKTREE_AGENT_IDS_KEY not in (refreshed.inputs or {})


# ── failed in-task auto-merge module ──────────────────────────────────


@pytest.mark.asyncio
async def test_compute_failed_auto_merge_skips_effective_baseline_merge() -> None:
    flow, run = _make_flow_and_run(mode="easy", alice_auto_merge=True)

    class _Cli:
        async def run_merged_agent_patch(self, *, agent, **kw):
            del kw
            if agent == "alice":
                return {"files_changed": 2, "merge_count": 1}
            return {"files_changed": 0, "merge_count": 0}

        async def workspace_agent_patch(self, *, agent, **kw):
            del kw
            if agent == "bob":
                return {"patch": "+line", "uncommitted_patch": "", "branch_ahead": 1}
            return None

    ids = await fin.compute_failed_auto_merge_agent_ids(
        flow=flow, run=run, cli=_Cli(), storage=get_storage(),
    )
    assert ids == ["bob"]


@pytest.mark.asyncio
async def test_compute_failed_auto_merge_includes_leader() -> None:
    """The leader summary task is treated like any other: a missed self-merge
    with leftover worktree content surfaces the leader too."""
    flow, run = _make_flow_and_run(mode="easy", alice_auto_merge=True)

    class _Cli:
        async def run_merged_agent_patch(self, *, agent, **kw):
            del kw
            # everyone effectively merged EXCEPT the leader
            if agent == "leader":
                return {"files_changed": 0, "merge_count": 0}
            return {"files_changed": 3, "merge_count": 1}

        async def workspace_agent_patch(self, *, agent, **kw):
            del kw
            if agent == "leader":
                return {"patch": "+summary", "uncommitted_patch": "", "branch_ahead": 1}
            return None

    ids = await fin.compute_failed_auto_merge_agent_ids(
        flow=flow, run=run, cli=_Cli(), storage=get_storage(),
    )
    assert ids == ["leader"]


def test_list_failed_auto_merges_api(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import runs as runs_mod
    from app.scheduler.run_metadata import FAILED_AUTO_MERGE_AGENT_IDS_KEY

    flow, run = _make_flow_and_run(
        mode="easy",
        status=RunStatus.awaiting_user_complaint,
        inputs={FAILED_AUTO_MERGE_AGENT_IDS_KEY: ["alice"]},
    )

    async def _fake_row(**kw):
        del kw
        return {
            "branch_name": f"clawteam/{run.team_name}/alice",
            "base_branch": "main",
            "repo_root": "/tmp/r",
            "worktree_path": "/tmp/wt-alice",
        }

    monkeypatch.setattr(runs_mod, "_find_pending_pr_workspace_row", _fake_row)
    r = app_client.get(f"/api/runs/{run.id}/failed-auto-merges")
    assert r.status_code == 200, r.text
    assert [i["agentId"] for i in r.json()["items"]] == ["alice"]
