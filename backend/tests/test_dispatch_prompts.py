"""Tests for app.scheduler.prompts — dispatch message templates."""

from __future__ import annotations

import pytest

from app.models import (
    AgentKind,
    FlowAgent,
    FlowTask,
    MergeStrategy,
    OnFailure,
)
from app.scheduler import prompts
from app.worktree.lookup import WorktreeInfo


def _wt(agent="alice", branch="clawteam/csflow-x/alice",
        path="/tmp/wt/alice", main="/tmp/main", base="main") -> WorktreeInfo:
    return WorktreeInfo(
        agent_name=agent, branch_name=branch, worktree_path=path,
        repo_root=main, base_branch=base,
    )


def _agent(id="alice", *, leader=False, kind=AgentKind.claude,
           merge=MergeStrategy.manual, repo="/tmp/main") -> FlowAgent:
    return FlowAgent(
        id=id, kind=kind, repo=None if kind == AgentKind.openclaw else repo,
        is_leader=leader, merge_strategy=merge,
        on_failure=OnFailure.retry, max_retries=2,
    )


def _task(id="t1", subject="Do the thing", description="Detailed.",
          owner="alice", deps=(), is_summary=False) -> FlowTask:
    return FlowTask(
        id=id, owner_agent_id=owner, subject=subject,
        description=description, depends_on=list(deps),
        is_leader_summary=is_summary,
    )


def _ctx(**overrides) -> prompts.DispatchContext:
    base = dict(
        run_id="run-abc",
        team_name="csflow-abc",
        flow_description="Build a tiny widget.",
        flow_inputs={"target_user": "alice"},
        user="alice",
        agent=_agent(),
        task=_task(),
        leader_agent_id="leader",
        worktree=_wt(),
        worker_worktrees=[],
        worker_reports=[],
        upstream_outputs=[],
    )
    base.update(overrides)
    return prompts.DispatchContext(**base)


def _upstream(task_id="t-upstream", subject="Crawl data",
              from_agent="crawler", path="/tmp/wt/crawler",
              branch="clawteam/csflow-x/crawler", base="main", repo_root="/tmp/main",
              summary="抓了 12,438 条样本到 data/raw.csv (commit a7f3e2c)") -> prompts.UpstreamOutput:
    return prompts.UpstreamOutput(
        task_id=task_id, subject=subject, from_agent=from_agent,
        worktree_path=path, branch_name=branch, base_branch=base, repo_root=repo_root,
        summary=summary,
    )


# ── worker dispatch -----------------------------------------------------


def test_worker_dispatch_starts_with_context_block() -> None:
    msg = prompts.build_worker_dispatch(_ctx())
    assert msg.startswith("## ClawsomeFlow Dispatch Context")


def test_worker_dispatch_includes_required_blocks_in_order() -> None:
    msg = prompts.build_worker_dispatch(_ctx())
    expected = [
        "## ClawsomeFlow Dispatch Context",
        "## Your Role",
        "## Workspace Context",
        "## Task #t1: Do the thing",
        "## Completion Checklist",
    ]
    seen = [msg.find(h) for h in expected]
    assert all(p >= 0 for p in seen), msg
    assert seen == sorted(seen), "blocks out of order"


def test_worker_dispatch_non_openclaw_omits_strict_main_repo_constraint() -> None:
    msg = prompts.build_worker_dispatch(_ctx())
    assert "/tmp/wt/alice" in msg
    assert "never write in the baseline-branch workspace" not in msg


def test_worker_dispatch_for_openclaw_includes_worktree_guardrails() -> None:
    msg = prompts.build_worker_dispatch(_ctx(
        agent=_agent(kind=AgentKind.openclaw, merge=MergeStrategy.agent_self),
        worktree=_wt(),
    ))
    assert "OpenClaw agent" in msg
    assert "never write in the baseline-branch workspace" in msg
    assert "If this task requires changes to workspace content" in msg
    assert "my-desktop/" in msg


def test_worker_dispatch_openclaw_self_does_not_merge_in_task_prompt() -> None:
    """OpenClaw merge happens in complaint/satisfaction stage, not per-task."""
    msg = prompts.build_worker_dispatch(_ctx(
        agent=_agent(kind=AgentKind.openclaw, merge=MergeStrategy.agent_self),
    ))
    assert "git checkout main" not in msg
    # The generic merge reference is present, but no auto-merge MANDATE for this task.
    assert "**Self-merge:**" not in msg
    assert "If merge conflicts occur, resolve them yourself" not in msg


def test_worker_dispatch_tui_does_not_merge() -> None:
    """TUI agents (claude/codex/...) never run merge steps in worker prompt;
    that's owned by finalize_run (user merge review for non-OpenClaw)."""
    msg = prompts.build_worker_dispatch(_ctx())  # default = claude / manual
    assert "**Self-merge:**" not in msg  # no auto-merge mandate (manual run)
    assert "If merge conflicts occur, resolve them yourself" not in msg


def test_worker_dispatch_hermes_uses_generic_tui_shape() -> None:
    """Hermes is a TUI agent — its dispatch must match the generic TUI shape,
    with no OpenClaw-specific worktree/merge guardrails."""
    msg = prompts.build_worker_dispatch(_ctx(
        agent=_agent(kind=AgentKind.hermes, merge=MergeStrategy.manual),
    ))
    assert msg.startswith("## ClawsomeFlow Dispatch Context")
    # No OpenClaw-only blocks.
    assert "OpenClaw agent" not in msg
    assert "never write in the baseline-branch workspace" not in msg
    assert "If this task requires changes to workspace content" not in msg
    assert "my-desktop/" not in msg
    # No auto-merge MANDATE in the worker prompt (manual strategy owns merge at finalize);
    # the generic merge reference may still appear.
    assert "**Self-merge:**" not in msg


def test_worker_dispatch_openclaw_skip_does_not_merge() -> None:
    """OpenClaw with merge_strategy=skip should NOT include merge steps."""
    msg = prompts.build_worker_dispatch(_ctx(
        agent=_agent(kind=AgentKind.openclaw, merge=MergeStrategy.skip),
    ))
    assert "**Self-merge:**" not in msg


def test_leader_completion_steps_focus_on_deliverable_without_merge_guidance() -> None:
    """Non-OpenClaw (TUI) leader: deliverable in worktree, NO my-desktop convention,
    and NO baseline-workspace wording anywhere (that drove the pre-copy bug)."""
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(id="leader", leader=True),  # default kind=claude (TUI)
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
    ))
    # Normal-mode leader (merge_reference defaults False) → no generic merge block.
    assert "## Git Merge & Repo Lock Reference" not in msg
    assert "## Merge Suggestions" not in msg
    assert "merge suggestion" not in msg.lower()
    # TUI agents work directly in the repo worktree — no my-desktop/ dumping.
    assert "my-desktop/" not in msg
    assert "Write every output inside your worktree (`/tmp/wt/alice`)" in msg
    assert "keep worker files under each worker's own path" in msg
    assert "exists before sending (e.g. `test -f <absolute-path>`)." in msg
    assert "git commit" in msg
    assert "clawteam inbox send" in msg
    assert "leader final reply:" in msg
    assert "VERY IMPORTANT: you MUST execute" in msg
    assert "1. Review all worker reports and worktree states above." not in msg
    assert "Focus on solution outcome, risks, and verification evidence" not in msg
    assert "Do NOT copy, move, or write any file outside it" not in msg
    assert "Those paths MUST be inside your worktree" not in msg
    assert "ClawsomeFlow can surface it in Run detail" not in msg
    assert "1. In `/tmp/wt/alice`, **produce the final deliverable**:" in msg
    assert "2. `cd /tmp/wt/alice && git add -A && git commit -m 'task ts: leader summary'`." in msg
    assert "(required — keep the literal prefix `leader final reply:`.)" in msg
    # No baseline-workspace wording at all for a TUI leader (worker_worktrees empty
    # here, so the only possible source would be the completion steps we fixed).
    assert "in the corresponding baseline workspace" not in msg
    assert "baseline" not in msg.lower()
    assert "machine-safe" not in msg
    assert "ASCII punctuation as separators" not in msg
    assert msg.find("clawteam inbox send") < msg.find("clawteam task update")


def test_leader_completion_steps_openclaw_has_my_desktop_and_no_baseline_ref() -> None:
    """OpenClaw leader: keeps the my-desktop/ convention, but the final-reply step
    must NOT tell it to reference baseline-workspace paths (OpenClaw also merges
    only at the satisfaction stage)."""
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(
            id="leader", leader=True, kind=AgentKind.openclaw,
            merge=MergeStrategy.agent_self,
        ),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
    ))
    # my-desktop/ is the OpenClaw-only distinction.
    assert "`/tmp/wt/alice/my-desktop/`" in msg
    assert "1. Review all worker reports and worktree states above." not in msg
    assert "Focus on solution outcome, risks, and verification evidence" not in msg
    assert "Do NOT copy, move, or write any file outside it" not in msg
    assert "Those paths MUST be inside your worktree" not in msg
    assert "ClawsomeFlow can surface it in Run detail" not in msg
    assert "1. In `/tmp/wt/alice`, **produce the final deliverable**:" in msg
    assert "(required — keep the literal prefix `leader final reply:`.)" in msg
    # The harmful "reference baseline workspace paths" wording must be gone.
    assert "in the corresponding baseline workspace" not in msg


def test_worker_dispatch_includes_task_completion_steps() -> None:
    msg = prompts.build_worker_dispatch(_ctx())
    assert "clawteam task update" in msg
    assert "VERY IMPORTANT: you MUST execute" in msg
    assert "clawteam inbox send" in msg
    assert "End this turn" in msg
    assert "**End this turn** — stay idle and wait for the next dispatch message." not in msg
    assert "If this task requires changes to workspace content" not in msg
    assert "my-desktop/" not in msg


def test_leader_openclaw_self_merge_omits_merge_steps_in_summary_prompt() -> None:
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(
            id="leader",
            leader=True,
            kind=AgentKind.openclaw,
            merge=MergeStrategy.agent_self,
        ),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
        worktree=_wt(
            agent="leader",
            branch="clawteam/csflow-x/leader",
            path="/tmp/wt/leader",
            main="/tmp/main/leader",
            base="main",
        ),
    ))
    assert "**Self-merge:**" not in msg  # no auto-merge mandate in summary prompt
    assert "git checkout main" not in msg
    assert "test -f <absolute-path>" in msg


def test_worker_dispatch_sends_inbox_before_mark_completed() -> None:
    msg = prompts.build_worker_dispatch(_ctx())
    inbox_pos = msg.find("clawteam inbox send")
    update_pos = msg.find("clawteam task update")
    assert inbox_pos > 0 and update_pos > 0
    assert inbox_pos < update_pos


def test_worker_dispatch_requires_standard_task_id_header_for_inbox_output() -> None:
    msg = prompts.build_worker_dispatch(_ctx())
    assert '"task t1 done: <completion-summary>"' in msg
    assert "MUST start with the exact literal prefix `task t1 done:`" in msg
    assert "strict-match by task id" not in msg
    assert "strict task-id matching" not in msg
    assert "ASCII punctuation as separators" not in msg
    assert "not `/abs/path/file.md。`" not in msg
    assert "concise summary of your work" in msg
    assert "absolute paths of important changed docs/files" in msg


def test_worker_leader_summary_omits_inbox_send() -> None:
    """The leader-summary task itself shouldn't ask the leader to inbox the leader."""
    ctx = _ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Summarise", owner="leader", is_summary=True),
    )
    msg = prompts.build_worker_dispatch(ctx)
    assert "clawteam inbox send" not in msg


# ── leader dispatch -----------------------------------------------------


def test_leader_dispatch_includes_extra_blocks() -> None:
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
        worker_worktrees=[_wt(agent="alice"), _wt(agent="bob", path="/tmp/wt/bob")],
        worker_reports=[
            prompts.WorkerReport(from_agent="alice", summary="done", task_id="t1"),
        ],
    ))
    for h in [
        "## Flow Goal",
        "## Worker Worktrees and Branches",
        "## Worker Reports",
    ]:
        assert h in msg, h
    assert "alice" in msg and "bob" in msg
    assert "base branch `main`" in msg  # baseline branch surfaced per worker worktree
    assert "Build a tiny widget." in msg
    assert "target_user" in msg


def test_leader_dispatch_handles_no_workers() -> None:
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Solo", owner="leader", is_summary=True),
    ))
    assert "summary task has no dependencies configured" in msg


# ── self-merge ---------------------------------------------------------


def test_self_merge_dispatch_requires_worktree() -> None:
    with pytest.raises(ValueError):
        prompts.build_openclaw_self_merge(_ctx(worktree=None))


def test_self_merge_dispatch_includes_main_repo_block_and_steps() -> None:
    ctx = _ctx(
        agent=_agent(kind=AgentKind.openclaw, merge=MergeStrategy.agent_self),
        task=_task(id="m1", subject="self merge"),
        worktree=_wt(),
    )
    msg = prompts.build_openclaw_self_merge(ctx)
    assert "## Baseline Workspace (self-merge)" in msg
    # Locked merge is delegated to the fixed tool (no inline git/locking).
    assert "csflow-locked-merge.py" in msg
    assert "resolve conflicts yourself" in msg
    assert "VERY IMPORTANT:" in msg
    assert "5. **End this turn**" in msg


# ── scheduled runs (self-merge in-task) ───────────────────────────────


def test_worker_dispatch_non_scheduled_omits_self_merge() -> None:
    """Default (manual) runs must NOT carry self-merge steps (zero regression)."""
    msg = prompts.build_worker_dispatch(_ctx())
    assert "Scheduled run — self-merge" not in msg
    assert "**Self-merge:**" not in msg  # mandate absent; generic reference may appear
    assert "post-merge absolute path" not in msg


def test_worker_dispatch_scheduled_includes_self_merge_and_post_merge_paths() -> None:
    ctx = _ctx(self_merge=True)
    msg = prompts.build_worker_dispatch(ctx)
    assert "**Self-merge:**" in msg
    assert "resolve conflicts yourself" in msg
    assert "csflow-locked-merge.py" in msg
    # Feature branch is passed to the tool as the src arg.
    assert "clawteam/csflow-x/alice" in msg
    # Post-merge absolute path requirement points at the baseline workspace.
    assert "Cite paths under `/tmp/main`" in msg or "paths under `/tmp/main`" in msg
    merge_pos = msg.find("**Self-merge:**")
    inbox_pos = msg.find("clawteam inbox send")
    update_pos = msg.find("clawteam task update")
    assert 0 < merge_pos < inbox_pos < update_pos


def test_leader_dispatch_scheduled_includes_self_merge() -> None:
    ctx = _ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
        worktree=_wt(agent="leader", branch="clawteam/csflow-x/leader",
                     path="/tmp/wt/leader", main="/tmp/main", base="main"),
        self_merge=True,
    )
    msg = prompts.build_leader_dispatch(ctx)
    assert "**Self-merge:**" in msg
    assert "resolve conflicts yourself" in msg
    assert "clawteam/csflow-x/leader" in msg  # feature branch passed to the merge tool
    assert "paths under `/tmp/main`" in msg
    merge_pos = msg.find("**Self-merge:**")
    reply_pos = msg.find("leader final reply:")
    assert 0 < merge_pos < reply_pos


def test_leader_scheduled_final_reply_must_cite_baseline_paths() -> None:
    """Scheduled leader summary: MUST self-check that reported paths are the
    post-merge baseline paths (run-ac4ec5fbfe7b), with a cross-platform check."""
    ctx = _ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
        worktree=_wt(agent="leader", branch="clawteam/csflow-x/leader",
                     path="/tmp/wt/leader", main="/tmp/main", base="main"),
        self_merge=True,
    )
    msg = prompts.build_leader_dispatch(ctx)
    assert "MUST self-check" in msg
    assert "post-merge path under the baseline workspace `/tmp/main` on `main`" in msg
    assert "test -f <baseline-absolute-path>" in msg
    assert "identical command on Linux and macOS" in msg
    assert "<concise summary + post-merge baseline absolute paths>" in msg


def test_leader_non_scheduled_final_reply_keeps_worktree_path_check() -> None:
    """Manual (non-scheduled) leader summary stays unchanged: no baseline wording
    (merge happens later via review), worktree-path verification preserved."""
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
    ))
    assert "exists before sending (e.g. `test -f <absolute-path>`)." in msg
    assert "<concise summary + absolute paths>" in msg
    assert "MUST self-check" not in msg
    assert "baseline" not in msg.lower()


# ── upstream-outputs block ────────────────────────────────────────────


def test_no_upstream_block_when_no_dependencies() -> None:
    """Header must NOT appear when task has no first-level dependencies."""
    msg = prompts.build_worker_dispatch(_ctx(upstream_outputs=[]))
    assert "## Direct Upstream Outputs" not in msg


def test_upstream_block_renders_one_dependency() -> None:
    upstreams = [_upstream()]
    msg = prompts.build_worker_dispatch(_ctx(
        task=_task(id="t-down", deps=("t-upstream",)),
        upstream_outputs=upstreams,
    ))
    assert "## Direct Upstream Outputs (1 item(s), first-level dependencies only)" in msg
    assert 'task `t-upstream` "Crawl data" by agent `crawler`' in msg
    assert "/tmp/wt/crawler" in msg
    assert "clawteam/csflow-x/crawler" in msg
    assert "base branch `main`" in msg  # baseline branch must be surfaced
    assert "12,438 条样本" in msg
    # The "To inspect upstream changes" git-log/git-diff footer was removed.
    assert "To inspect upstream changes" not in msg
    assert "git log --oneline" not in msg


def test_upstream_block_handles_missing_summary() -> None:
    upstreams = [_upstream(summary=None)]
    msg = prompts.build_worker_dispatch(_ctx(
        task=_task(id="t-down", deps=("t-upstream",)),
        upstream_outputs=upstreams,
    ))
    assert "completion summary: _(missing; inspect upstream worktree git history)_" in msg


def test_upstream_block_handles_missing_worktree() -> None:
    """Session may have been disposed already → worktree path unknown."""
    upstreams = [_upstream(path=None, branch=None, base=None)]
    msg = prompts.build_worker_dispatch(_ctx(
        task=_task(id="t-down", deps=("t-upstream",)),
        upstream_outputs=upstreams,
    ))
    assert "_(unknown — agent session may have been disposed)_" in msg


def test_upstream_block_renders_multiple_dependencies() -> None:
    upstreams = [
        _upstream(task_id="t-a", from_agent="alice", path="/tmp/wt/alice",
                  summary="A done."),
        _upstream(task_id="t-b", from_agent="bob", path="/tmp/wt/bob",
                  summary="B done."),
    ]
    msg = prompts.build_worker_dispatch(_ctx(
        task=_task(id="t-down", deps=("t-a", "t-b")),
        upstream_outputs=upstreams,
    ))
    assert "(2 item(s), first-level dependencies only)" in msg
    assert "task `t-a`" in msg
    assert "task `t-b`" in msg


def test_upstream_block_does_not_assume_openclaw_already_merged() -> None:
    upstreams = [_upstream()]
    msg = prompts.build_worker_dispatch(_ctx(
        agent=_agent(kind=AgentKind.openclaw, merge=MergeStrategy.agent_self),
        task=_task(id="t-down", deps=("t-upstream",)),
        upstream_outputs=upstreams,
    ))
    assert "already merged to baseline branch" not in msg


def test_upstream_block_appears_before_task_block() -> None:
    """Layout: upstream block must come before the task block."""
    msg = prompts.build_worker_dispatch(_ctx(
        task=_task(id="t-down", deps=("t-upstream",)),
        upstream_outputs=[_upstream()],
    ))
    upstream_pos = msg.find("## Direct Upstream Outputs")
    task_pos = msg.find("## Task #t-down")
    assert upstream_pos != -1 and task_pos != -1
    assert upstream_pos < task_pos


def test_leader_dispatch_does_not_duplicate_upstream_block() -> None:
    """Leader summary task already has its own worker reports block;
    we shouldn't ALSO emit direct-upstream block (avoid duplication)."""
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Sum", owner="leader", is_summary=True,
                   deps=("t-upstream",)),
        upstream_outputs=[_upstream()],   # populated by mistake
        worker_reports=[],
    ))
    # The leader builder doesn't render the upstream block at all.
    assert "## Direct Upstream Outputs" not in msg
    assert "## Worker Reports" in msg


# ── generic git-merge / repo-lock reference (dev/easy modes only) ──────


def test_generic_merge_reference_absent_by_default() -> None:
    """Normal mode (merge_reference defaults False) → no generic merge block."""
    assert "## Git Merge & Repo Lock Reference" not in prompts.build_worker_dispatch(_ctx())
    assert "## Git Merge & Repo Lock Reference" not in prompts.build_leader_dispatch(_ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
    ))


def test_worker_dispatch_includes_generic_merge_reference_when_enabled() -> None:
    """dev/easy dispatch (merge_reference=True) carries the generic how-to."""
    msg = prompts.build_worker_dispatch(_ctx(merge_reference=True))
    assert "## Git Merge & Repo Lock Reference" in msg
    assert "Reference only" in msg
    # Runtime-generic invocation: agent fills REPO/SRC/DST placeholders; the tool
    # computes the correct per-repo lock at runtime.
    assert "csflow-locked-merge.py" in msg
    assert "<abs-repo>" in msg and "<source-branch>" in msg and "<dest-branch>" in msg
    assert "merge target only" in msg
    assert msg.find("## Git Merge & Repo Lock Reference") < msg.find("## Task #")


def test_leader_dispatch_includes_generic_merge_reference_when_enabled() -> None:
    msg = prompts.build_leader_dispatch(_ctx(
        agent=_agent(id="leader", leader=True),
        task=_task(id="ts", subject="Final", owner="leader", is_summary=True),
        merge_reference=True,
    ))
    assert "## Git Merge & Repo Lock Reference" in msg
    assert "<abs-repo>" in msg and "<source-branch>" in msg and "<dest-branch>" in msg


def test_generic_reference_is_not_a_mandate() -> None:
    """The block alone never mandates a merge (no self-merge step)."""
    msg = prompts.build_worker_dispatch(_ctx(merge_reference=True))  # self_merge False
    assert "## Git Merge & Repo Lock Reference" in msg
    assert "**Self-merge:**" not in msg


def test_generic_reference_coexists_with_mandate() -> None:
    """dev auto-merge task: generic reference AND the mandatory self-merge step."""
    msg = prompts.build_worker_dispatch(_ctx(merge_reference=True, self_merge=True))
    assert "## Git Merge & Repo Lock Reference" in msg
    assert "**Self-merge:**" in msg
    # The mandate targets the agent's OWN branch (concrete, no placeholder).
    assert "clawteam/csflow-x/alice" in msg


def test_upstream_repo_root_shown_only_when_merge_reference() -> None:
    """repo_root (merge target) appears in the upstream block only when enabled."""
    base = dict(task=_task(id="t-down", deps=("t-upstream",)),
                upstream_outputs=[_upstream()])
    off = prompts.build_worker_dispatch(_ctx(**base))
    on = prompts.build_worker_dispatch(_ctx(merge_reference=True, **base))
    assert "repo root (merge target): `/tmp/main`" not in off
    assert "repo root (merge target): `/tmp/main`" in on
