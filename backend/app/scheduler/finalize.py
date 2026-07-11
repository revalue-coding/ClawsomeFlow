"""finalize_run — end-of-Run merge dispatch + ``finished_at`` stamping.

When the controller enters finalize, this module decides:

* which non-OpenClaw agents need user merge review (normal manual runs),
* and the next Run status (`awaiting_user_review`, `awaiting_user_complaint`,
  or terminal failure/abort).

In-task self-merge (easy / dev / scheduled-normal) is **prompt-driven only** via
``flow_modes.task_self_merges`` — this module never calls ``workspace_merge``
except when the user resolves a pending merge via ``perform_manual_merge``.

Cleanup policy:

* **OpenClaw**: no post-run `workspace cleanup` in ClawsomeFlow.
* **Non-OpenClaw manual review agents**: after successful merge or explicit
  dismiss, ClawsomeFlow runs `clawteam workspace cleanup` for that agent
  workspace.
* **Merge conflict** keeps the worktree intact for manual conflict resolution.
* Team-level cleanup follows `Flow.cleanup_team_on_finish` for normal terminal
  runs once no pending merge decisions remain, except
  `completed_with_conflicts` runs which preserve worktrees for follow-up.
* Abnormal terminal runs (`failed` / `aborted`) force `team_cleanup(force=true)`
  and skip all merge decision branches.
* After team cleanup, ClawsomeFlow runs a direct `rm -rf` fallback on
  `~/.clawteam/workspaces/{team}` to prevent stale workspace directories.
* Whenever a worktree is cleaned up, ClawsomeFlow also deletes the matching
  local ``clawteam/{team}/{agent}`` branch ref (and sweeps all
  ``clawteam/{team}/…`` refs on team-level cleanup).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import load_config
from app.flow_modes import flow_mode, task_self_merges
from app.integrations.clawteam_cli import (
    ClawTeamCli,
    get_clawteam_cli,
)
from app.integrations.clawteam_mcp import (
    ClawTeamMcpClient,
    get_mcp_client,
)
from app.integrations.git_repo import delete_clawteam_team_branches
from app.logging_setup import get_logger
from app.models import (
    DEFAULT_TARGET_BRANCH,
    TERMINAL_RUN_STATUSES,
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    MergeStrategy,
    PendingMerge,
    RunStatus,
)
from app.scheduler.run_metadata import (
    DEV_PENDING_PR_AGENT_IDS_KEY,
    POST_COMPLAINT_STATUS_KEY,
    POST_REVIEW_TERMINAL_STATUS_KEY,
    PRESERVE_WORKTREE_AGENT_IDS_KEY,
)
from app.storage import StorageBackend
from app.user_context import get_request_user, set_request_user
from app.worktree.lookup import WorktreeInfo, WorktreeLookup

logger = get_logger("scheduler.finalize")


# Terminal RunStatus values. Used to decide when to stamp ``finished_at``.
# Single source of truth lives in app.models (TERMINAL_RUN_STATUSES).
_TERMINAL_STATUSES: frozenset[RunStatus] = TERMINAL_RUN_STATUSES
_CSFLOW_TEAM_PREFIX = "csflow-"
# Single-sourced in app.scheduler.run_metadata; the private aliases are kept
# because engine/tests historically imported them from this module.
_POST_COMPLAINT_STATUS_KEY = POST_COMPLAINT_STATUS_KEY
_POST_REVIEW_TERMINAL_STATUS_KEY = POST_REVIEW_TERMINAL_STATUS_KEY
_PRESERVE_WORKTREE_AGENT_IDS_KEY = PRESERVE_WORKTREE_AGENT_IDS_KEY


# ──────────────────────────────────────────────────────────────────────
# Inputs / outputs
# ──────────────────────────────────────────────────────────────────────


@dataclass
class FinalizeInput:
    """Snapshot the controller hands to :func:`finalize_run`."""

    run: FlowRun
    flow: Flow                                   # for cleanup_team_on_finish
    agents: list[FlowAgent]
    leader_agent_id: str
    has_failed_tasks: bool                       # propagated from controller
    aborted: bool = False                        # cancel was requested


@dataclass
class FinalizeOutcome:
    """Returned by :func:`finalize_run`."""

    final_status: RunStatus
    pending_merges: list[PendingMerge] = field(default_factory=list)
    team_cleaned: bool = False
    detail: str = ""


@dataclass
class TerminalTailCleanupOutcome:
    """Unified tail-cleanup result after run enters terminal status."""

    team_cleaned: bool = False


async def finalize_run(
    ipt: FinalizeInput,
    *,
    storage: StorageBackend,
    cli: ClawTeamCli | None = None,
    mcp: ClawTeamMcpClient | None = None,
    worktree_lookup: WorktreeLookup | None = None,
) -> FinalizeOutcome:
    """Drive end-of-Run merge / cleanup logic. Persists Run updates inline."""
    prev_user = get_request_user()
    set_request_user(ipt.run.user)
    try:
        cli = cli or get_clawteam_cli()
        if mcp is None:
            mcp = await get_mcp_client(user=ipt.run.user)

        # Merge review applies to every non-OpenClaw agent (leader included).
        # OpenClaw remains self-merge / in-task merge per runtime policy.
        non_openclaw_agents = [
            a for a in ipt.agents if a.kind != AgentKind.openclaw
        ]
        out = FinalizeOutcome(final_status=RunStatus.completed)

        # Aborted/failed runs are force-terminated: skip all merge decision
        # branches and immediately cleanup run worktrees.
        if ipt.aborted or ipt.has_failed_tasks:
            out.final_status = RunStatus.aborted if ipt.aborted else RunStatus.failed
            out.detail = "run aborted" if ipt.aborted else "task failure(s) detected"
            ipt.run.status = out.final_status
            ipt.run.pending_merges = None
            merged_inputs = dict(ipt.run.inputs or {})
            merged_inputs.pop(_POST_COMPLAINT_STATUS_KEY, None)
            merged_inputs.pop(_POST_REVIEW_TERMINAL_STATUS_KEY, None)
            # Abnormal terminal: no PR module for aborted/failed runs, and all
            # worktrees are force-cleaned below — drop any pending-PR marker so
            # no stale entry can survive.
            merged_inputs.pop(DEV_PENDING_PR_AGENT_IDS_KEY, None)
            ipt.run.inputs = merged_inputs
            if ipt.run.finished_at is None:
                ipt.run.finished_at = datetime.now(timezone.utc)
            tail_out = await run_terminal_tail_cleanup(
                run=ipt.run,
                flow=ipt.flow,
                agents=ipt.agents,
                storage=storage,
                cli=cli,
                worktree_lookup=worktree_lookup,
            )
            out.team_cleaned = tail_out.team_cleaned
            logger.info(
                "finalize_run_abnormal_terminated",
                run_id=ipt.run.id,
                team=ipt.run.team_name,
                final_status=out.final_status.value,
                team_cleaned=out.team_cleaned,
            )
            return out

        # Scheduled (unattended) runs: every task self-merges into the baseline
        # branch in-task (see scheduler/prompts.py). There is no human in the
        # loop, so skip both the user merge-review (awaiting_user_review) and the
        # user complaint (awaiting_user_complaint) phases entirely and go straight
        # to terminal ``completed``.
        if getattr(ipt.run, "is_scheduled", False):
            out.final_status = RunStatus.completed
            out.detail = "scheduled run: in-task self-merge; review + complaint skipped"
            ipt.run.status = RunStatus.completed
            ipt.run.pending_merges = None
            merged_inputs = dict(ipt.run.inputs or {})
            merged_inputs.pop(_POST_COMPLAINT_STATUS_KEY, None)
            merged_inputs.pop(_POST_REVIEW_TERMINAL_STATUS_KEY, None)
            # Scheduled dev runs still honour per-task devAutoMerge=false —
            # record those agents so their worktrees survive tail cleanup
            # for the Run-detail PR module.
            dev_pending_pr = compute_dev_pending_pr_agent_ids(
                flow=ipt.flow, run=ipt.run,
            )
            if dev_pending_pr:
                merged_inputs[DEV_PENDING_PR_AGENT_IDS_KEY] = dev_pending_pr
            ipt.run.inputs = merged_inputs
            if ipt.run.finished_at is None:
                ipt.run.finished_at = datetime.now(timezone.utc)
            tail_out = await run_terminal_tail_cleanup(
                run=ipt.run,
                flow=ipt.flow,
                agents=ipt.agents,
                storage=storage,
                cli=cli,
                worktree_lookup=worktree_lookup,
            )
            out.team_cleaned = tail_out.team_cleaned
            logger.info(
                "finalize_run_scheduled_autocomplete",
                run_id=ipt.run.id,
                team=ipt.run.team_name,
                team_cleaned=out.team_cleaned,
            )
            return out

        # Easy ("省心") / developer ("开发者") mode, manual trigger: every merge
        # was performed in-task (easy → all tasks self-merge; dev → auto-merge
        # tasks + OpenClaw self-merge, no-merge task worktrees are discarded by
        # terminal cleanup). So there is no user merge-review (awaiting_user_review)
        # — but, unlike a scheduled run, a manual run STILL enters the user
        # complaint phase. Go to awaiting_user_complaint with a completed terminal
        # marker; the worktree of every no-merge dev task is removed by the
        # terminal team cleanup at run end.
        mode = flow_mode((ipt.flow.spec or {}).get("variables") or {})
        if mode in ("easy", "dev"):
            out.final_status = RunStatus.awaiting_user_complaint
            out.detail = (
                f"{mode} mode: in-task self-merge; review skipped, awaiting complaint input"
            )
            ipt.run.status = RunStatus.awaiting_user_complaint
            ipt.run.pending_merges = None
            merged_inputs = dict(ipt.run.inputs or {})
            merged_inputs[_POST_COMPLAINT_STATUS_KEY] = RunStatus.completed.value
            merged_inputs.pop(_POST_REVIEW_TERMINAL_STATUS_KEY, None)
            # Dev mode: agents with no-merge (devAutoMerge=false) tasks keep
            # their worktrees at terminal cleanup for the PR module
            # (easy mode self-merges everything → helper returns []).
            dev_pending_pr = compute_dev_pending_pr_agent_ids(
                flow=ipt.flow, run=ipt.run,
            )
            if dev_pending_pr:
                merged_inputs[DEV_PENDING_PR_AGENT_IDS_KEY] = dev_pending_pr
            ipt.run.inputs = merged_inputs
            if ipt.run.finished_at is None:
                ipt.run.finished_at = datetime.now(timezone.utc)
            # Run is not terminal yet (awaiting complaint), so tail cleanup is a
            # no-op now; team/worktree cleanup happens when the complaint phase
            # finishes (controller._finish_after_complaint_phase).
            logger.info(
                "finalize_run_mode_awaiting_complaint",
                run_id=ipt.run.id,
                team=ipt.run.team_name,
                mode=mode,
            )
            return out

        # Snapshot every agent's current worktree info for diff/UI consumption.
        agent_worktrees = await _gather_worktrees(
            ipt.run.team_name, non_openclaw_agents, worktree_lookup,
        )

        # ── Non-OpenClaw: user merge review vs skip -----------------------
        # Normal manual runs: every non-skipped TUI/Hermes agent goes to
        # ``awaiting_user_review`` — never scheduler ``workspace_merge`` here.
        # (``merge_strategy=auto`` is legacy; ``dev_auto_merge`` only affects
        # dispatch prompts in dev mode via ``flow_modes.task_self_merges``.)
        review_agents = [
            a for a in non_openclaw_agents
            if a.merge_strategy != MergeStrategy.skip
        ]
        skipped_agents = [
            a for a in non_openclaw_agents
            if a.merge_strategy == MergeStrategy.skip
        ]

        for a in skipped_agents:
            logger.info("finalize_skip_tui", agent_id=a.id, reason="merge_strategy=skip")

        pending: list[PendingMerge] = []
        for a in review_agents:
            wt = agent_worktrees.get(a.id)
            raw_diff = await _safe_diff(
                mcp,
                ipt.run.team_name,
                a.id,
                repo_root=wt.repo_root if wt else None,
            )
            dirty = await _safe_worktree_dirty(
                cli,
                agent_id=a.id,
                worktree_path=wt.worktree_path if wt else None,
            )
            if raw_diff is None and wt is None:
                logger.info(
                    "finalize_manual_merge_skipped_no_workspace",
                    agent_id=a.id,
                    team=ipt.run.team_name,
                )
                _emit(
                    storage,
                    ipt.run.id,
                    "manual_merge_skipped_no_workspace",
                    agent_id=a.id,
                    payload={},
                )
                continue
            if not _has_mergeable_changes(diff=raw_diff, dirty=dirty):
                logger.info(
                    "finalize_manual_merge_skipped_no_changes",
                    agent_id=a.id,
                    team=ipt.run.team_name,
                )
                _emit(
                    storage,
                    ipt.run.id,
                    "manual_merge_skipped_no_changes",
                    agent_id=a.id,
                    payload={"diff": _compose_diff_summary(diff=raw_diff, dirty=dirty)},
                )
                continue
            diff = _compose_diff_summary(diff=raw_diff, dirty=dirty)
            branch = wt.branch_name if wt else f"clawteam/{ipt.run.team_name}/{a.id}"
            target_branch = (a.target_branch or DEFAULT_TARGET_BRANCH).strip() or DEFAULT_TARGET_BRANCH
            pending.append(PendingMerge(
                agent_id=a.id, branch=branch,
                target_branch=target_branch,
                diff_summary=diff,
                leader_suggestion="",  # filled later from leader's deliverable (Phase 7+)
            ))
        out.pending_merges = pending

        # ── Compute final RunStatus ---------------------------------------
        post_complaint_status: RunStatus | None = None
        post_review_terminal_status: RunStatus | None = None
        if pending:
            out.final_status = RunStatus.awaiting_user_review
            if ipt.aborted:
                post_review_terminal_status = RunStatus.aborted
                out.detail = (
                    f"{len(pending)} agent(s) awaiting manual merge "
                    "(run was aborted early)"
                )
            elif ipt.has_failed_tasks:
                post_review_terminal_status = RunStatus.failed
                out.detail = (
                    f"{len(pending)} agent(s) awaiting manual merge "
                    "(task failure(s) detected)"
                )
            else:
                out.detail = f"{len(pending)} agent(s) awaiting manual merge"
        elif ipt.aborted:
            out.final_status = RunStatus.aborted
            out.detail = "run aborted"
        elif ipt.has_failed_tasks:
            out.final_status = RunStatus.failed
            out.detail = "task failure(s) detected"
        else:
            post_complaint_status = RunStatus.completed
            out.final_status = RunStatus.awaiting_user_complaint
            out.detail = "awaiting complaint input before final cleanup"

        # Persist Run updates inline. ``finished_at`` records when orchestration
        # work is done (leader summary done), so it must not be delayed by
        # manual-merge review or complaint-feedback time.
        ipt.run.status = out.final_status
        ipt.run.pending_merges = (
            [p.model_dump(mode="json") for p in pending] if pending else None
        )
        merged_inputs = dict(ipt.run.inputs or {})
        if post_complaint_status is not None:
            merged_inputs[_POST_COMPLAINT_STATUS_KEY] = post_complaint_status.value
        else:
            merged_inputs.pop(_POST_COMPLAINT_STATUS_KEY, None)
        if post_review_terminal_status is not None:
            merged_inputs[_POST_REVIEW_TERMINAL_STATUS_KEY] = (
                post_review_terminal_status.value
            )
        else:
            merged_inputs.pop(_POST_REVIEW_TERMINAL_STATUS_KEY, None)
        ipt.run.inputs = merged_inputs
        if (
            out.final_status in {
                RunStatus.awaiting_user_review,
                RunStatus.awaiting_user_complaint,
            }
            and ipt.run.finished_at is None
        ):
            ipt.run.finished_at = datetime.now(timezone.utc)
        elif out.final_status in _TERMINAL_STATUSES and ipt.run.finished_at is None:
            ipt.run.finished_at = datetime.now(timezone.utc)

        # Tail cleanup only handles team-level policy once run is terminal.
        tail_out = await run_terminal_tail_cleanup(
            run=ipt.run,
            flow=ipt.flow,
            agents=ipt.agents,
            storage=storage,
            cli=cli,
            worktree_lookup=worktree_lookup,
        )
        out.team_cleaned = tail_out.team_cleaned

        logger.info(
            "finalize_run_done",
            run_id=ipt.run.id, team=ipt.run.team_name,
            final_status=out.final_status.value,
            pending_count=len(pending),
            team_cleaned=out.team_cleaned,
        )
        return out
    finally:
        set_request_user(prev_user)


# ──────────────────────────────────────────────────────────────────────
# Public helper (invoked by the Phase 7 ``POST /api/runs/{id}/merge`` endpoint)
# ──────────────────────────────────────────────────────────────────────


async def perform_manual_merge(
    *,
    run: FlowRun,
    agent_id: str,
    storage: StorageBackend,
    cli: ClawTeamCli | None = None,
    terminalize_when_resolved: bool = True,
) -> tuple[bool, str]:
    """Trigger one manual merge resolution for *agent_id*.

    Used by the API when the user clicks "Merge" in the awaiting-review UI.
    Updates ``run.pending_merges`` to drop the resolved entry and persists.
    When ``terminalize_when_resolved`` is False, the caller decides follow-up
    state transitions (e.g. awaiting_user_complaint stage).
    Merge failures keep worktree as-is for manual user resolution.
    Returns ``(ok, stderr)`` so the route can surface the result.
    """
    prev_user = get_request_user()
    set_request_user(run.user)
    try:
        cli = cli or get_clawteam_cli()
        target_branch = DEFAULT_TARGET_BRANCH
        source_branch = f"clawteam/{run.team_name}/{agent_id}"
        merge_repo: str | None = None
        if run.pending_merges:
            for item in run.pending_merges:
                if item.get("agent_id") == agent_id:
                    raw_target = str(item.get("target_branch") or DEFAULT_TARGET_BRANCH)
                    target_branch = raw_target.strip() or DEFAULT_TARGET_BRANCH
                    raw_branch = str(item.get("branch") or "")
                    parsed_branch = raw_branch.strip()
                    if parsed_branch:
                        source_branch = parsed_branch
                    raw_repo = str(item.get("repo_root") or item.get("repo") or "")
                    merge_repo = raw_repo.strip() or None
                    if merge_repo:
                        merge_repo = str(Path(merge_repo).expanduser())
                    break
        if merge_repo is None:
            merge_repo = _resolve_agent_repo_for_run(
                run=run,
                agent_id=agent_id,
                storage=storage,
            )
        ok, msg = await cli.workspace_merge(
            team=run.team_name,
            agent=agent_id,
            repo=merge_repo,
            target=target_branch,
        )
    finally:
        set_request_user(prev_user)
    if ok:
        _emit(storage, run.id, "manual_merge_ok",
              agent_id=agent_id, payload={
                  "team": run.team_name,
                  "source_branch": source_branch,
                  "target_branch": target_branch,
                  "repo_root": merge_repo,
              })
    else:
        failure_kind = classify_merge_failure(msg)
        event_type = "merge_conflict" if failure_kind == "conflict" else "merge_error"
        _emit(
            storage,
            run.id,
            event_type,
            agent_id=agent_id,
            payload={
                "team": run.team_name,
                "stderr": msg[:1000],
                "source_branch": source_branch,
                "target_branch": target_branch,
                "repo_root": merge_repo,
                "failure_kind": failure_kind,
            },
        )

    if run.pending_merges:
        run.pending_merges = [
            p for p in run.pending_merges if p.get("agent_id") != agent_id
        ]
        if not run.pending_merges:
            run.pending_merges = None
            if terminalize_when_resolved:
                run.status = (
                    RunStatus.completed_with_conflicts if not ok
                    else RunStatus.completed
                )
                if run.finished_at is None:
                    run.finished_at = datetime.now(timezone.utc)
        storage.run_update(run)
        if run.pending_merges is None and terminalize_when_resolved:
            # The run just entered a terminal status from awaiting_user_review.
            flow = storage.flow_get(run.flow_id)
            agents: list[FlowAgent] = []
            if flow is not None:
                try:
                    agents = list(FlowSpec.model_validate(flow.spec).agents)
                except Exception:
                    agents = []
            await run_terminal_tail_cleanup(
                run=run,
                flow=flow,
                agents=agents,
                storage=storage,
                cli=cli,
            )
    return ok, msg


async def run_terminal_tail_cleanup(
    *,
    run: FlowRun,
    storage: StorageBackend,
    flow: Flow | None = None,
    agents: list[FlowAgent] | None = None,
    cli: ClawTeamCli | None = None,
    worktree_lookup: WorktreeLookup | None = None,
    preserve_worktree_dirs: bool = False,
) -> TerminalTailCleanupOutcome:
    """Single entrypoint for terminal tail cleanup.

    Only performs team-level cleanup when policy allows.
    Agent-level worktree cleanup for manual-review non-OpenClaw agents is
    handled at merge/dismiss decision time.
    """
    prev_user = get_request_user()
    set_request_user(run.user)
    try:
        out = TerminalTailCleanupOutcome()
        if run.status not in _TERMINAL_STATUSES:
            return out
        # Compatibility: keep these args in signature for existing callers.
        _ = agents, worktree_lookup
        cli = cli or get_clawteam_cli()
        resolved_flow = flow or storage.flow_get(run.flow_id)
        if resolved_flow is None:
            logger.warning(
                "tail_cleanup_flow_missing",
                run_id=run.id,
                flow_id=run.flow_id,
            )
            return out
        out.team_cleaned = await _maybe_cleanup_team_after_terminal(
            run=run,
            flow=resolved_flow,
            storage=storage,
            cli=cli,
            preserve_worktree_dirs=preserve_worktree_dirs,
        )
        return out
    finally:
        set_request_user(prev_user)


async def maybe_cleanup_team_after_terminal(
    *,
    run: FlowRun,
    storage: StorageBackend,
    cli: ClawTeamCli | None = None,
) -> bool:
    """Best-effort team cleanup once *run* reaches terminal.

    Public helper for API endpoints that transition ``awaiting_user_review``
    runs to terminal (manual merge / dismiss paths).
    """
    prev_user = get_request_user()
    set_request_user(run.user)
    try:
        flow = storage.flow_get(run.flow_id)
        if flow is None:
            logger.warning("team_cleanup_flow_missing", run_id=run.id, flow_id=run.flow_id)
            return False
        cli = cli or get_clawteam_cli()
        return await _maybe_cleanup_team_after_terminal(
            run=run, flow=flow, storage=storage, cli=cli,
        )
    finally:
        set_request_user(prev_user)


async def cleanup_non_openclaw_workspace_after_review_decision(
    *,
    run: FlowRun,
    agent_id: str,
    storage: StorageBackend,
    cli: ClawTeamCli | None = None,
) -> bool:
    """Cleanup one manual-review workspace after user decision (merge/dismiss).

    OpenClaw agents are explicitly skipped by policy.
    """
    prev_user = get_request_user()
    set_request_user(run.user)
    try:
        cli = cli or get_clawteam_cli()
        if _is_openclaw_agent(run=run, agent_id=agent_id, storage=storage):
            logger.info(
                "manual_review_cleanup_skipped_openclaw",
                run_id=run.id,
                team=run.team_name,
                agent_id=agent_id,
            )
            return False
        try:
            repo = _resolve_agent_repo_for_run(
                run=run,
                agent_id=agent_id,
                storage=storage,
            )
            ok = await cli.workspace_cleanup(
                team=run.team_name,
                agent=agent_id,
                repo=repo,
            )
        except Exception as exc:
            logger.warning(
                "manual_review_cleanup_exception",
                run_id=run.id,
                team=run.team_name,
                agent_id=agent_id,
                error=str(exc),
            )
            _emit(
                storage,
                run.id,
                "manual_review_cleanup_failed",
                agent_id=agent_id,
                payload={"team": run.team_name, "error": str(exc)[:1000]},
            )
            return False
        if ok:
            _emit(
                storage,
                run.id,
                "manual_review_worktree_cleaned",
                agent_id=agent_id,
                payload={"team": run.team_name},
            )
            return True
        _emit(
            storage,
            run.id,
            "manual_review_cleanup_failed",
            agent_id=agent_id,
            payload={"team": run.team_name, "error": "workspace_cleanup_failed"},
        )
        return False
    finally:
        set_request_user(prev_user)


# ──────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────


async def _gather_worktrees(
    team_name: str,
    agents: list[FlowAgent],
    lookup: WorktreeLookup | None,
) -> dict[str, WorktreeInfo | None]:
    out: dict[str, WorktreeInfo | None] = {}
    if lookup is None:
        return {a.id: None for a in agents}
    try:
        rows = await lookup.list_team(team_name, force=True)
    except Exception:
        return {a.id: None for a in agents}
    by_name = {r.agent_name: r for r in rows}
    for a in agents:
        out[a.id] = by_name.get(a.id)
    return out


async def _safe_diff(
    mcp: ClawTeamMcpClient,
    team: str,
    agent_id: str,
    *,
    repo_root: str | None,
) -> dict[str, Any] | None:
    try:
        return await mcp.workspace_agent_diff(team, agent_id, repo=repo_root)
    except Exception as exc:
        logger.warning(
            "workspace_diff_failed",
            agent_id=agent_id,
            repo_root=repo_root,
            error=str(exc),
        )
        return None


async def _safe_worktree_dirty(
    cli: ClawTeamCli,
    *,
    agent_id: str,
    worktree_path: str | None,
) -> dict[str, Any]:
    if not worktree_path:
        return {"has_uncommitted_changes": False, "entry_count": 0, "entries": []}
    try:
        dirty, entries = await cli.workspace_has_uncommitted_changes(
            worktree_path=worktree_path,
        )
    except Exception as exc:
        logger.warning(
            "workspace_dirty_check_failed",
            agent_id=agent_id,
            worktree_path=worktree_path,
            error=str(exc),
        )
        return {
            "has_uncommitted_changes": False,
            "entry_count": 0,
            "entries": [],
            "error": str(exc)[:1000],
        }
    return {
        "has_uncommitted_changes": dirty,
        "entry_count": len(entries),
        "entries": entries[:100],
    }


def _compose_diff_summary(
    *,
    diff: dict[str, Any] | None,
    dirty: dict[str, Any] | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(diff or {})
    if dirty:
        merged.update({
            "has_uncommitted_changes": bool(dirty.get("has_uncommitted_changes")),
            "uncommitted_entry_count": int(dirty.get("entry_count") or 0),
        })
        entries = dirty.get("entries")
        if isinstance(entries, list):
            merged["uncommitted_entries"] = entries
        if dirty.get("error"):
            merged["uncommitted_check_error"] = str(dirty["error"])
    return merged


def _has_mergeable_changes(
    *,
    diff: dict[str, Any] | None,
    dirty: dict[str, Any] | None,
) -> bool:
    if _diff_has_changes(diff):
        return True
    if dirty and bool(dirty.get("has_uncommitted_changes")):
        return True
    return False


def _diff_has_changes(diff: dict[str, Any] | None) -> bool:
    """Return True when worktree branch contains any mergeable change."""
    if diff is None:
        # Diff lookup failed or unavailable — be conservative and keep merge path.
        return True
    if not diff:
        return False
    commit_count = diff.get("commit_count")
    if isinstance(commit_count, int) and commit_count > 0:
        return True
    files_changed = diff.get("files_changed")
    if isinstance(files_changed, list) and len(files_changed) > 0:
        return True
    # Backward-compatible fallback for older payload shapes.
    files = diff.get("files")
    if isinstance(files, int) and files > 0:
        return True
    return False


def classify_merge_failure(message: str) -> str:
    """Classify merge failure into conflict vs environment error."""
    text = (message or "").lower()
    if not text:
        return "environment_error"
    # Git merge conflicts usually include one of these markers.
    if (
        "automatic merge failed" in text
        or "conflict (" in text
        or "merge conflict" in text
        or "\nconflict" in text
        or text.strip() == "conflict"
        or ("conflict" in text and "merge" in text)
    ):
        return "conflict"
    return "environment_error"


def _resolve_agent_repo_for_run(
    *,
    run: FlowRun,
    agent_id: str,
    storage: StorageBackend,
) -> str | None:
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        return None
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        return None
    for agent in spec.agents:
        if agent.id != agent_id:
            continue
        repo = str(agent.repo or "").strip()
        if not repo:
            return None
        return str(Path(repo).expanduser())
    return None


def _is_openclaw_agent(
    *,
    run: FlowRun,
    agent_id: str,
    storage: StorageBackend,
) -> bool:
    flow = storage.flow_get(run.flow_id)
    if flow is None:
        return False
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        return False
    for agent in spec.agents:
        if agent.id == agent_id:
            return agent.kind == AgentKind.openclaw
    return False


def _read_preserved_worktree_agent_ids(run: FlowRun) -> set[str]:
    raw = (run.inputs or {}).get(_PRESERVE_WORKTREE_AGENT_IDS_KEY)
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        aid = str(item or "").strip()
        if aid:
            out.add(aid)
    return out


def read_dev_pending_pr_agent_ids(run: FlowRun) -> set[str]:
    """Agent ids awaiting a PR decision in the developer-mode PR module."""
    raw = (run.inputs or {}).get(DEV_PENDING_PR_AGENT_IDS_KEY)
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        aid = str(item or "").strip()
        if aid:
            out.add(aid)
    return out


def compute_dev_pending_pr_agent_ids(*, flow: Flow, run: FlowRun) -> list[str]:
    """Developer-mode runs only: non-OpenClaw agents owning ≥1 no-merge task.

    Those agents' branches never self-merged into the baseline, so their
    worktrees must survive terminal cleanup for the Run detail "PR" module
    (inspect / one-click PR / discard). Returns ``[]`` for every other mode —
    the resulting marker therefore also records "this run executed in dev
    mode". Order follows the spec's agent order (deterministic UI).
    """
    variables = (flow.spec or {}).get("variables") or {}
    if flow_mode(variables) != "dev":
        return []
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        return []
    agents_by_id = {a.id: a for a in spec.agents}
    pending: set[str] = set()
    for task in spec.tasks:
        agent = agents_by_id.get(task.owner_agent_id)
        if agent is None or agent.kind == AgentKind.openclaw:
            continue
        if task_self_merges(
            mode="dev",
            run_is_scheduled=bool(getattr(run, "is_scheduled", False)),
            task=task,
            agent=agent,
        ):
            continue
        pending.add(agent.id)
    return [a.id for a in spec.agents if a.id in pending]


def _emit(
    storage: StorageBackend, run_id: str, event_type: str, *,
    agent_id: str | None = None,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    from app.events import publish_run_event
    publish_run_event(
        storage,
        run_id=run_id,
        event_type=event_type,
        agent_id=agent_id,
        task_id=task_id,
        payload=payload,
    )


async def _maybe_cleanup_team_after_terminal(
    *,
    run: FlowRun,
    flow: Flow,
    storage: StorageBackend,
    cli: ClawTeamCli,
    preserve_worktree_dirs: bool = False,
) -> bool:
    """Run ``team_cleanup`` when policy allows and run is terminal."""
    if run.status not in _TERMINAL_STATUSES:
        return False
    abnormal_terminal = run.status in {
        RunStatus.complaint_failed,
        RunStatus.failed,
        RunStatus.aborted,
    }
    if not abnormal_terminal:
        if not flow.cleanup_team_on_finish:
            return False
        if run.pending_merges:
            return False
    # Safety guard: never attempt to clean a non-csflow team name.
    if not run.team_name.startswith(_CSFLOW_TEAM_PREFIX):
        logger.warning(
            "team_cleanup_skipped_invalid_team_name",
            run_id=run.id,
            team=run.team_name,
        )
        _emit(
            storage,
            run.id,
            "team_cleanup_skipped",
            payload={
                "team": run.team_name,
                "reason": "invalid_team_name",
                "forced": abnormal_terminal,
            },
        )
        return False
    preserved_agent_ids = _read_preserved_worktree_agent_ids(run)
    # Dev-mode PR module: unresolved pending-PR agents keep their worktrees
    # until the user submits a PR or discards them (api/runs.py pending-prs
    # endpoints remove ids from the marker, then re-trigger this cleanup).
    # NEVER preserve on an abnormal terminal (aborted / failed / complaint_failed):
    # those force a full cleanup, and the PR module is hidden for them — so a
    # lingering marker must not strand worktrees. Only healthy terminals honour it.
    dev_pending_pr_ids = (
        set() if abnormal_terminal else read_dev_pending_pr_agent_ids(run)
    )
    preserve_due_to_conflicts = run.status == RunStatus.completed_with_conflicts
    if preserve_worktree_dirs or preserve_due_to_conflicts or dev_pending_pr_ids:
        combined_preserve = preserved_agent_ids | dev_pending_pr_ids
        selective = not preserve_worktree_dirs and (
            (preserve_due_to_conflicts and bool(preserved_agent_ids))
            or bool(dev_pending_pr_ids)
        )
        if selective and combined_preserve:
            await _cleanup_non_openclaw_worktrees_except_preserved(
                run=run,
                flow=flow,
                storage=storage,
                cli=cli,
                preserved_agent_ids=combined_preserve,
            )
        preserved_agent_ids = combined_preserve
        if preserve_worktree_dirs:
            reason = "preserve_worktree_dirs"
        elif preserve_due_to_conflicts:
            reason = "completed_with_conflicts"
        else:
            reason = "dev_pending_pr"
        logger.info(
            "team_cleanup_skipped_preserve_worktree",
            run_id=run.id,
            team=run.team_name,
            forced=abnormal_terminal,
            reason=reason,
            preserved_agents=sorted(preserved_agent_ids),
        )
        _emit(
            storage,
            run.id,
            "team_cleanup_skipped",
            payload={
                "team": run.team_name,
                "reason": reason,
                "forced": abnormal_terminal,
                "preserved_agents": sorted(preserved_agent_ids),
            },
        )
        return False
    cleanup_repos = await _collect_team_cleanup_repos(
        run=run,
        flow=flow,
        cli=cli,
    )
    cleanup_error: str | None = None
    try:
        await cli.team_cleanup(team=run.team_name, force=True)
    except Exception as exc:
        cleanup_error = str(exc)
        logger.warning("team_cleanup_failed", team=run.team_name, error=cleanup_error)
        _emit(
            storage,
            run.id,
            "team_cleanup_failed",
            payload={
                "team": run.team_name,
                "error": cleanup_error[:1000],
                "forced": abnormal_terminal,
            },
        )

    # Fallback: directly remove clawteam workspaces/{team} to avoid stale dirs.
    fallback_removed = await _cleanup_team_workspace_dir_fallback(
        run=run,
        storage=storage,
    )
    if cleanup_error is None or fallback_removed:
        await _cleanup_clawteam_branch_refs_for_team(
            run=run,
            repos=cleanup_repos,
            storage=storage,
        )
    if cleanup_error is None:
        _emit(
            storage,
            run.id,
            "team_cleaned",
            payload={
                "team": run.team_name,
                "forced": abnormal_terminal,
                "workspace_dir_removed": fallback_removed,
            },
        )
        return True
    if "not found" in cleanup_error.lower() and fallback_removed:
        _emit(
            storage,
            run.id,
            "team_cleaned",
            payload={
                "team": run.team_name,
                "forced": abnormal_terminal,
                "source": "rm_fallback",
            },
        )
        return True
    return False


async def _cleanup_non_openclaw_worktrees_except_preserved(
    *,
    run: FlowRun,
    flow: Flow,
    storage: StorageBackend,
    cli: ClawTeamCli,
    preserved_agent_ids: set[str],
) -> None:
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        return
    existing_agents: set[str] | None = None
    try:
        rows = await cli.workspace_list(team=run.team_name)
    except Exception as exc:
        logger.warning(
            "workspace_list_failed_before_preserve_cleanup",
            run_id=run.id,
            team=run.team_name,
            error=str(exc),
        )
    else:
        existing_agents = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("agent_name") or "").strip()
            if name:
                existing_agents.add(name)
    for agent in spec.agents:
        if agent.kind == AgentKind.openclaw:
            continue
        agent_id = str(agent.id or "").strip()
        if not agent_id or agent_id in preserved_agent_ids:
            continue
        if existing_agents is not None and agent_id not in existing_agents:
            continue
        await cleanup_non_openclaw_workspace_after_review_decision(
            run=run,
            agent_id=agent_id,
            storage=storage,
            cli=cli,
        )


async def _collect_team_cleanup_repos(
    *,
    run: FlowRun,
    flow: Flow,
    cli: ClawTeamCli,
) -> set[str]:
    repos: set[str] = set()
    try:
        spec = FlowSpec.model_validate(flow.spec)
    except Exception:
        spec = None
    if spec is not None:
        for agent in spec.agents:
            repo = str(agent.repo or "").strip()
            if repo:
                repos.add(repo)
    try:
        rows = await cli.workspace_list(team=run.team_name)
    except Exception as exc:
        logger.debug(
            "team_branch_cleanup_workspace_list_failed",
            run_id=run.id,
            team=run.team_name,
            error=str(exc),
        )
    else:
        for row in rows:
            if not isinstance(row, dict):
                continue
            repo_root = str(row.get("repo_root") or row.get("repo") or "").strip()
            if repo_root:
                repos.add(repo_root)
    return repos


async def _cleanup_clawteam_branch_refs_for_team(
    *,
    run: FlowRun,
    repos: set[str],
    storage: StorageBackend,
) -> None:
    if not repos:
        return
    deleted_by_repo: dict[str, list[str]] = {}
    for repo_raw in sorted(repos):
        repo_path = Path(repo_raw).expanduser()
        deleted = await asyncio.to_thread(
            delete_clawteam_team_branches,
            repo_path,
            run.team_name,
        )
        if deleted:
            deleted_by_repo[str(repo_path)] = deleted
    if deleted_by_repo:
        _emit(
            storage,
            run.id,
            "team_clawteam_branches_deleted",
            payload={
                "team": run.team_name,
                "repos": deleted_by_repo,
            },
        )


async def _cleanup_team_workspace_dir_fallback(
    *,
    run: FlowRun,
    storage: StorageBackend,
) -> bool:
    cfg = load_config()
    clawteam_data = (
        Path(cfg.clawteam_data_dir).expanduser()
        if cfg.clawteam_data_dir
        else Path.home() / ".clawteam"
    )
    workspaces_root = clawteam_data / "workspaces"
    team_dir = workspaces_root / run.team_name
    resolved_root = workspaces_root.resolve(strict=False)
    resolved_team = team_dir.resolve(strict=False)
    try:
        resolved_team.relative_to(resolved_root)
    except ValueError:
        logger.warning(
            "team_workspace_fallback_cleanup_skipped",
            run_id=run.id,
            team=run.team_name,
            team_dir=str(resolved_team),
            reason="path_escape",
        )
        _emit(
            storage,
            run.id,
            "team_workspace_dir_remove_skipped",
            payload={
                "team": run.team_name,
                "team_dir": str(resolved_team),
                "reason": "path_escape",
            },
        )
        return False
    if not resolved_team.exists():
        return True
    argv = ["rm", "-rf", "--", str(resolved_team)]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        _emit(
            storage,
            run.id,
            "team_workspace_dir_removed",
            payload={
                "team": run.team_name,
                "team_dir": str(resolved_team),
            },
        )
        return True
    err = (
        (stderr.decode("utf-8", errors="replace") or "").strip()
        or (stdout.decode("utf-8", errors="replace") or "").strip()
        or f"rm exit code {proc.returncode}"
    )
    logger.warning(
        "team_workspace_fallback_cleanup_failed",
        run_id=run.id,
        team=run.team_name,
        team_dir=str(resolved_team),
        error=err[:1000],
    )
    _emit(
        storage,
        run.id,
        "team_workspace_dir_remove_failed",
        payload={
            "team": run.team_name,
            "team_dir": str(resolved_team),
            "error": err[:1000],
        },
    )
    return False


__all__ = [
    "FinalizeInput",
    "FinalizeOutcome",
    "TerminalTailCleanupOutcome",
    "classify_merge_failure",
    "cleanup_non_openclaw_workspace_after_review_decision",
    "finalize_run",
    "maybe_cleanup_team_after_terminal",
    "perform_manual_merge",
    "run_terminal_tail_cleanup",
]
