"""Tests for app.scheduler.finalize."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import load_config
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
from app.scheduler.naming import team_name_for_run
from app.storage import get_storage
from app.worktree.lookup import WorktreeInfo


# ── stubs --------------------------------------------------------------


class _StubCli:
    def __init__(self, *, merge_results: dict[str, tuple[bool, str]] | None = None,
                 cleanup_ok: bool = True,
                 cleanup_requires_repo: bool = False,
                 workspace_rows: list[dict[str, str]] | None = None,
                 dirty_worktrees: dict[str, list[str]] | None = None,
                 team_cleanup_error: Exception | None = None) -> None:
        self.merge_results = merge_results or {}
        self.cleanup_ok = cleanup_ok
        self.cleanup_requires_repo = cleanup_requires_repo
        self.workspace_rows = list(workspace_rows or [])
        self.dirty_worktrees = dirty_worktrees or {}
        self.team_cleanup_error = team_cleanup_error
        self.merge_calls: list[dict] = []
        self.cleanup_calls: list[dict] = []
        self.team_cleanup_calls: list[str] = []

    async def workspace_merge(self, *, team: str, agent: str, **kw):
        self.merge_calls.append({"team": team, "agent": agent, **kw})
        return self.merge_results.get(agent, (True, ""))

    async def workspace_cleanup(self, *, team: str, agent: str, **kw):
        call = {"team": team, "agent": agent, **kw}
        self.cleanup_calls.append(call)
        if not self.cleanup_ok:
            return False
        if self.cleanup_requires_repo and not kw.get("repo"):
            return False
        self.workspace_rows = [
            row for row in self.workspace_rows
            if not (row.get("team_name") == team and row.get("agent_name") == agent)
        ]
        return True

    async def workspace_cleanup_with_diagnostics(self, *, team: str, agent: str, **kw):
        from app.integrations.clawteam_cli import (
            WorkspaceCleanupAttempt,
            WorkspaceCleanupResult,
        )
        ok = await self.workspace_cleanup(team=team, agent=agent, **kw)
        cmd = ["clawteam", "workspace", "cleanup", team, "--agent", agent]
        if kw.get("repo"):
            cmd += ["--repo", str(kw["repo"])]
        attempt = WorkspaceCleanupAttempt(
            argv=cmd,
            exit_code=0 if ok else 1,
            stderr="" if ok else "cleanup failed",
        )
        return WorkspaceCleanupResult(success=ok, attempts=[attempt])

    async def team_cleanup(self, *, team: str, force: bool = True):
        del force
        self.team_cleanup_calls.append(team)
        if self.team_cleanup_error is not None:
            raise self.team_cleanup_error

    async def workspace_list(self, *, team: str, repo: str | None = None):
        rows = [row for row in self.workspace_rows if row.get("team_name") == team]
        if repo:
            rows = [row for row in rows if row.get("repo_root") == repo]
        return rows

    async def workspace_has_uncommitted_changes(self, *, worktree_path: str):
        entries = list(self.dirty_worktrees.get(worktree_path, []))
        return bool(entries), entries


class _StubMcp:
    def __init__(
        self,
        *,
        diffs: dict[str, dict] | None = None,
        diffs_by_repo: dict[tuple[str, str], dict] | None = None,
    ) -> None:
        self.diffs = diffs or {}
        self.diffs_by_repo = diffs_by_repo or {}

    async def workspace_agent_diff(self, team: str, agent_id: str, repo: str | None = None):
        del team
        if repo is not None and (agent_id, repo) in self.diffs_by_repo:
            return self.diffs_by_repo[(agent_id, repo)]
        return self.diffs.get(agent_id)


class _StubLookup:
    def __init__(self, items: list[WorktreeInfo]) -> None:
        self.items = items

    async def list_team(self, team: str, *, force: bool = False):
        return list(self.items)

    async def get(self, *a, **kw):
        return None


# ── fixtures ----------------------------------------------------------


def _make_run_and_spec(*, agents_kw: list[dict[str, Any]],
                       cleanup_team: bool = False) -> tuple[FlowRun, Flow, FlowSpec]:
    spec = FlowSpec(
        agents=[FlowAgent(**kw) for kw in agents_kw],
        tasks=[FlowTask(
            id="t1", owner_agent_id=agents_kw[0]["id"], subject="x",
            description="", depends_on=[], timeout_seconds=300,
        )],
    )
    storage = get_storage()
    flow = Flow(
        name="t", description="", owner_user="alice",
        cleanup_team_on_finish=cleanup_team,
    ).with_spec(spec)
    flow = storage.flow_create(flow)
    run = storage.run_create(FlowRun(
        id=f"run-fin-{len(agents_kw)}", flow_id=flow.id, flow_version=1,
        team_name=team_name_for_run("run-fin-x"),
        status=RunStatus.running, inputs={}, user="alice",
    ))
    return run, flow, spec


def _wt(name: str) -> WorktreeInfo:
    return WorktreeInfo(
        agent_name=name, branch_name=f"clawteam/x/{name}",
        worktree_path=f"/tmp/wt/{name}", repo_root="/tmp/main", base_branch="main",
    )


# ── tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_manual_creates_pending_merges_and_awaiting_review() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli()
    mcp = _StubMcp(diffs={"alice": {"files": 3}})
    lookup = _StubLookup(items=[_wt("alice")])

    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=mcp, worktree_lookup=lookup,
    )
    assert out.final_status == RunStatus.awaiting_user_review
    assert len(out.pending_merges) == 1
    assert out.pending_merges[0].agent_id == "alice"
    assert out.pending_merges[0].diff_summary["files"] == 3
    assert out.pending_merges[0].diff_summary["has_uncommitted_changes"] is False
    assert out.pending_merges[0].diff_summary["uncommitted_entry_count"] == 0
    assert out.pending_merges[0].target_branch == "master"
    # Business completion point is leader summary done; do not delay finished_at
    # to user merge decisions.
    assert run.finished_at is not None
    # No automatic merges issued for manual.
    assert cli.merge_calls == []


@pytest.mark.asyncio
async def test_tui_manual_includes_non_openclaw_leader_for_review() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli()
    mcp = _StubMcp(diffs={"alice": {"files": 3}, "leader": {"files": 1}})
    lookup = _StubLookup(items=[_wt("alice"), _wt("leader")])

    out = await fin.finalize_run(
        fin.FinalizeInput(
            run=run,
            flow=flow,
            agents=spec.agents,
            leader_agent_id="leader",
            has_failed_tasks=False,
        ),
        storage=get_storage(),
        cli=cli,
        mcp=mcp,
        worktree_lookup=lookup,
    )
    assert out.final_status == RunStatus.awaiting_user_review
    assert {p.agent_id for p in out.pending_merges} == {"alice", "leader"}
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_tui_auto_runs_merge_immediately() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.auto,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli()
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[]),
    )
    assert {c["agent"] for c in cli.merge_calls} == {"alice"}
    assert cli.merge_calls[0].get("target") == "master"
    assert cli.merge_calls[0].get("repo") == "/r"
    assert out.final_status == RunStatus.awaiting_user_complaint
    assert (run.inputs or {}).get("_csflow_post_complaint_final_status") == "completed"
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_tui_manual_without_changes_skips_pending_merge() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli()
    mcp = _StubMcp(diffs={"alice": {"files_changed": [], "commit_count": 0}})
    out = await fin.finalize_run(
        fin.FinalizeInput(
            run=run,
            flow=flow,
            agents=spec.agents,
            leader_agent_id="leader",
            has_failed_tasks=False,
        ),
        storage=get_storage(),
        cli=cli,
        mcp=mcp,
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.pending_merges == []
    assert cli.merge_calls == []
    assert out.final_status == RunStatus.awaiting_user_complaint


@pytest.mark.asyncio
async def test_tui_manual_uncommitted_changes_still_create_pending_merge() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    lookup = _StubLookup(items=[_wt("alice")])
    cli = _StubCli(dirty_worktrees={"/tmp/wt/alice": ["?? my-desktop/reports/result.md"]})
    mcp = _StubMcp(diffs={"alice": {"files_changed": [], "commit_count": 0}})
    out = await fin.finalize_run(
        fin.FinalizeInput(
            run=run,
            flow=flow,
            agents=spec.agents,
            leader_agent_id="leader",
            has_failed_tasks=False,
        ),
        storage=get_storage(),
        cli=cli,
        mcp=mcp,
        worktree_lookup=lookup,
    )
    assert out.final_status == RunStatus.awaiting_user_review
    assert len(out.pending_merges) == 1
    diff = out.pending_merges[0].diff_summary
    assert diff.get("has_uncommitted_changes") is True
    assert diff.get("uncommitted_entry_count") == 1


@pytest.mark.asyncio
async def test_tui_manual_diff_uses_worktree_repo_root_context() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    lookup = _StubLookup(items=[_wt("alice")])
    mcp = _StubMcp(
        diffs={"alice": {"files_changed": [], "commit_count": 0}},
        diffs_by_repo={("alice", "/tmp/main"): {"files": 2}},
    )
    out = await fin.finalize_run(
        fin.FinalizeInput(
            run=run,
            flow=flow,
            agents=spec.agents,
            leader_agent_id="leader",
            has_failed_tasks=False,
        ),
        storage=get_storage(),
        cli=_StubCli(),
        mcp=mcp,
        worktree_lookup=lookup,
    )
    assert out.final_status == RunStatus.awaiting_user_review
    assert len(out.pending_merges) == 1
    assert out.pending_merges[0].diff_summary["files"] == 2


@pytest.mark.asyncio
async def test_tui_auto_with_conflict_yields_completed_with_conflicts() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.auto,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli(merge_results={"alice": (False, "merge conflict")})
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.final_status == RunStatus.awaiting_user_complaint
    assert "alice" in out.auto_merge_conflicts
    assert out.auto_merge_errors == []
    assert (run.inputs or {}).get("_csflow_post_complaint_final_status") == "completed_with_conflicts"
    assert run.finished_at is not None
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    merge_ev = next((e for e in events if e.type == "merge_conflict"), None)
    assert merge_ev is not None
    payload = merge_ev.payload or {}
    assert payload.get("source_branch") == f"clawteam/{run.team_name}/alice"
    assert payload.get("target_branch") == "master"


@pytest.mark.asyncio
async def test_tui_auto_with_environment_error_emits_merge_error() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.auto,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli(merge_results={"alice": (False, "workspace metadata missing repo_root")})
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.final_status == RunStatus.awaiting_user_complaint
    assert out.auto_merge_conflicts == []
    assert "alice" in out.auto_merge_errors
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    merge_ev = next((e for e in events if e.type == "merge_error"), None)
    assert merge_ev is not None
    payload = merge_ev.payload or {}
    assert payload.get("source_branch") == f"clawteam/{run.team_name}/alice"
    assert payload.get("target_branch") == "master"


@pytest.mark.asyncio
async def test_openclaw_only_cleanup_no_merge_call() -> None:
    """Per plan §8.8: OpenClaw merge is prompt-owned; finalize never merges."""
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "ocA", "kind": AgentKind.openclaw,
         "is_leader": False, "merge_strategy": MergeStrategy.agent_self,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli()
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[]),
    )
    assert cli.merge_calls == []  # NEVER merge OpenClaw at finalize
    # OpenClaw no longer runs post-run workspace cleanup in finalize.
    assert cli.cleanup_calls == []
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_terminal_tail_cleanup_does_not_cleanup_openclaw_worktrees() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "ocA", "kind": AgentKind.openclaw,
         "is_leader": False, "merge_strategy": MergeStrategy.agent_self,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.openclaw,
         "is_leader": True, "merge_strategy": MergeStrategy.agent_self,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    run.status = RunStatus.completed
    get_storage().run_update(run)
    cli = _StubCli()
    out = await fin.run_terminal_tail_cleanup(
        run=run,
        flow=flow,
        agents=spec.agents,
        storage=get_storage(),
        cli=cli,
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.team_cleaned is False
    assert cli.cleanup_calls == []


@pytest.mark.asyncio
async def test_manual_review_cleanup_non_openclaw_emits_cleaned_event() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    run.status = RunStatus.awaiting_user_review
    get_storage().run_update(run)
    cli = _StubCli()
    ok = await fin.cleanup_non_openclaw_workspace_after_review_decision(
        run=run,
        agent_id="alice",
        storage=get_storage(),
        cli=cli,
    )
    assert ok is True
    assert cli.cleanup_calls == [{"team": run.team_name, "agent": "alice"}]
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(e.type == "manual_review_worktree_cleaned" for e in events)


@pytest.mark.asyncio
async def test_manual_review_cleanup_skips_openclaw_agent() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "ocA", "kind": AgentKind.openclaw,
         "is_leader": False, "merge_strategy": MergeStrategy.agent_self,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    run.status = RunStatus.awaiting_user_review
    get_storage().run_update(run)
    cli = _StubCli()
    ok = await fin.cleanup_non_openclaw_workspace_after_review_decision(
        run=run,
        agent_id="ocA",
        storage=get_storage(),
        cli=cli,
    )
    assert ok is False
    assert cli.cleanup_calls == []
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert all(
        e.type not in {"manual_review_worktree_cleaned", "manual_review_cleanup_failed"}
        for e in events
    )


@pytest.mark.asyncio
async def test_failed_run_with_pending_merges_skips_merge_and_forces_cleanup() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.abort, "max_retries": 0},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=True),
        storage=get_storage(), cli=_StubCli(), mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[_wt("alice")]),
    )
    assert out.final_status == RunStatus.failed
    assert out.pending_merges == []
    assert run.pending_merges is None
    assert (run.inputs or {}).get("_csflow_post_review_terminal_status") is None


@pytest.mark.asyncio
async def test_aborted_run_status_overrides_all() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False, aborted=True),
        storage=get_storage(), cli=_StubCli(), mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.final_status == RunStatus.aborted


@pytest.mark.asyncio
async def test_aborted_run_with_pending_merges_skips_merge_and_forces_cleanup() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    cli = _StubCli()
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False, aborted=True),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[_wt("alice")]),
    )
    assert out.final_status == RunStatus.aborted
    assert out.pending_merges == []
    assert run.pending_merges is None
    assert (run.inputs or {}).get("_csflow_post_review_terminal_status") is None
    assert out.team_cleaned is True
    assert cli.team_cleanup_calls == [run.team_name]


@pytest.mark.asyncio
async def test_cleanup_team_on_finish_runs_team_cleanup() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=True)
    cli = _StubCli()
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.team_cleaned is False
    assert cli.team_cleanup_calls == []


@pytest.mark.asyncio
async def test_terminal_tail_cleanup_removes_workspace_dir_via_rm_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=True)
    run.status = RunStatus.completed
    get_storage().run_update(run)

    clawteam_data = tmp_path / "clawteam-data"
    team_dir = clawteam_data / "workspaces" / run.team_name
    (team_dir / "alice").mkdir(parents=True, exist_ok=True)
    (team_dir / "alice" / "note.txt").write_text("x", encoding="utf-8")
    cfg = load_config().model_copy(update={"clawteam_data_dir": str(clawteam_data)})
    monkeypatch.setattr(fin, "load_config", lambda: cfg)

    cli = _StubCli()
    out = await fin.run_terminal_tail_cleanup(
        run=run,
        flow=flow,
        agents=spec.agents,
        storage=get_storage(),
        cli=cli,
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.team_cleaned is True
    assert cli.team_cleanup_calls == [run.team_name]
    assert not team_dir.exists()
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    assert any(e.type == "team_workspace_dir_removed" for e in events)


@pytest.mark.asyncio
async def test_failed_terminal_cleanup_team_not_found_uses_rm_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=False)
    run.status = RunStatus.failed
    get_storage().run_update(run)

    clawteam_data = tmp_path / "clawteam-data"
    team_dir = clawteam_data / "workspaces" / run.team_name
    (team_dir / "alice").mkdir(parents=True, exist_ok=True)
    (team_dir / "alice" / "note.txt").write_text("x", encoding="utf-8")
    cfg = load_config().model_copy(update={"clawteam_data_dir": str(clawteam_data)})
    monkeypatch.setattr(fin, "load_config", lambda: cfg)

    cli = _StubCli(team_cleanup_error=RuntimeError(f"Team '{run.team_name}' not found"))
    out = await fin.run_terminal_tail_cleanup(
        run=run,
        flow=flow,
        agents=spec.agents,
        storage=get_storage(),
        cli=cli,
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.team_cleaned is True
    assert cli.team_cleanup_calls == [run.team_name]
    assert not team_dir.exists()
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    team_cleaned = [e for e in events if e.type == "team_cleaned"]
    assert team_cleaned
    assert team_cleaned[-1].payload.get("source") == "rm_fallback"


@pytest.mark.asyncio
async def test_terminal_tail_cleanup_preserves_worktree_dirs_when_requested(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=False)
    run.status = RunStatus.aborted
    get_storage().run_update(run)

    clawteam_data = tmp_path / "clawteam-data"
    team_dir = clawteam_data / "workspaces" / run.team_name
    (team_dir / "alice").mkdir(parents=True, exist_ok=True)
    (team_dir / "alice" / "note.txt").write_text("x", encoding="utf-8")
    cfg = load_config().model_copy(update={"clawteam_data_dir": str(clawteam_data)})
    monkeypatch.setattr(fin, "load_config", lambda: cfg)

    cli = _StubCli()
    out = await fin.run_terminal_tail_cleanup(
        run=run,
        flow=flow,
        agents=spec.agents,
        storage=get_storage(),
        cli=cli,
        worktree_lookup=_StubLookup(items=[]),
        preserve_worktree_dirs=True,
    )
    assert out.team_cleaned is False
    assert cli.team_cleanup_calls == []
    assert team_dir.exists()
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    skipped = [e for e in events if e.type == "team_cleanup_skipped"]
    assert skipped
    assert skipped[-1].payload.get("reason") == "preserve_worktree_dirs"
    assert not any(e.type == "team_workspace_dir_removed" for e in events)


@pytest.mark.asyncio
async def test_cleanup_team_on_finish_skips_invalid_team_name() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.skip,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=True)
    run.team_name = "external-team"
    get_storage().run_update(run)
    cli = _StubCli()
    out = await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[]),
    )
    assert out.team_cleaned is False
    assert cli.team_cleanup_calls == []


@pytest.mark.asyncio
async def test_cleanup_team_on_finish_skipped_when_awaiting_review() -> None:
    """Don't clean up the team while users still need to review merges."""
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=True)
    cli = _StubCli()
    await fin.finalize_run(
        fin.FinalizeInput(run=run, flow=flow, agents=spec.agents,
                          leader_agent_id="leader", has_failed_tasks=False),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[_wt("alice")]),
    )
    assert cli.team_cleanup_calls == []  # held back


@pytest.mark.asyncio
async def test_failed_run_forces_team_cleanup_even_with_pending_merges() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.abort, "max_retries": 0},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=True)
    cli = _StubCli()
    out = await fin.finalize_run(
        fin.FinalizeInput(
            run=run,
            flow=flow,
            agents=spec.agents,
            leader_agent_id="leader",
            has_failed_tasks=True,
        ),
        storage=get_storage(), cli=cli, mcp=_StubMcp(),
        worktree_lookup=_StubLookup(items=[_wt("alice")]),
    )
    assert out.final_status == RunStatus.failed
    assert out.pending_merges == []
    assert run.pending_merges is None
    assert (run.inputs or {}).get("_csflow_post_review_terminal_status") is None
    assert out.team_cleaned is True
    assert cli.team_cleanup_calls == [run.team_name]


# ── perform_manual_merge ----------------------------------------------


@pytest.mark.asyncio
async def test_perform_manual_merge_drops_resolved_pending_entry() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    # Seed pending_merges as if finalize_run already ran.
    run.pending_merges = [{"agent_id": "alice", "branch": "x", "diff_summary": {}}]
    run.status = RunStatus.awaiting_user_review
    get_storage().run_update(run)

    cli = _StubCli()
    ok, _ = await fin.perform_manual_merge(
        run=run, agent_id="alice", storage=get_storage(), cli=cli,
    )
    assert ok is True
    assert cli.merge_calls[0].get("repo") == "/r"
    refreshed = get_storage().run_get(run.id)
    assert refreshed.pending_merges is None
    assert refreshed.status == RunStatus.completed


@pytest.mark.asyncio
async def test_perform_manual_merge_conflict_marks_completed_with_conflicts() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    run.pending_merges = [{"agent_id": "alice", "branch": "x", "diff_summary": {}}]
    run.status = RunStatus.awaiting_user_review
    get_storage().run_update(run)

    cli = _StubCli(merge_results={"alice": (False, "conflict")})
    ok, _ = await fin.perform_manual_merge(
        run=run, agent_id="alice", storage=get_storage(), cli=cli,
    )
    assert ok is False
    refreshed = get_storage().run_get(run.id)
    assert refreshed.status == RunStatus.completed_with_conflicts
    # Conflict path keeps worktree for manual user resolution.
    assert cli.cleanup_calls == []
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    merge_ev = next((e for e in events if e.type == "merge_conflict"), None)
    assert merge_ev is not None
    payload = merge_ev.payload or {}
    assert payload.get("source_branch") == "x"
    assert payload.get("target_branch") == "master"


@pytest.mark.asyncio
async def test_perform_manual_merge_environment_error_emits_merge_error() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ])
    run.pending_merges = [{"agent_id": "alice", "branch": "x", "diff_summary": {}}]
    run.status = RunStatus.awaiting_user_review
    get_storage().run_update(run)

    cli = _StubCli(merge_results={"alice": (False, "repo_root missing")})
    ok, _ = await fin.perform_manual_merge(
        run=run, agent_id="alice", storage=get_storage(), cli=cli,
    )
    assert ok is False
    events = get_storage().event_list(run_id=run.id, since_id=None, limit=200)
    merge_ev = next((e for e in events if e.type == "merge_error"), None)
    assert merge_ev is not None
    payload = merge_ev.payload or {}
    assert payload.get("source_branch") == "x"
    assert payload.get("target_branch") == "master"


@pytest.mark.asyncio
async def test_perform_manual_merge_triggers_team_cleanup_when_enabled() -> None:
    run, flow, spec = _make_run_and_spec(agents_kw=[
        {"id": "alice", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": False, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
        {"id": "leader", "kind": AgentKind.claude, "repo": "/r",
         "is_leader": True, "merge_strategy": MergeStrategy.manual,
         "on_failure": OnFailure.retry, "max_retries": 2},
    ], cleanup_team=True)
    run.pending_merges = [{"agent_id": "alice", "branch": "x", "diff_summary": {}}]
    run.status = RunStatus.awaiting_user_review
    get_storage().run_update(run)

    cli = _StubCli()
    ok, _ = await fin.perform_manual_merge(
        run=run, agent_id="alice", storage=get_storage(), cli=cli,
    )
    assert ok is True
    assert cli.team_cleanup_calls == [run.team_name]
