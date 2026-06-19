"""Dispatch message templates (DEV.md §5.2 + §9 first layer).

This module is **the only place** that composes the text we send into a
worker session. It enforces three guarantees:

1. **Anti-loop block first.** Every message starts with
   ``## ClawsomeFlow Dispatch Context`` which overrides any in-skill polling
   protocol (see DEV.md §3 / §4). Workers are taught: do *exactly* what
   this message says, then stop and wait for the next one.

2. **Worktree boundary explicit.** The ``## Workspace Context`` block declares
   the absolute worktree path the worker must use. For OpenClaw this
   block additionally lists the baseline-branch workspace and a "no writes here"
   rule (DEV.md §9 layer 1).

3. **Completion checklist.** The ``## Completion Checklist`` block enumerates the
   exact CLI calls the worker must run (status update, inbox send,
   git commit). Skipping a step is treated as incomplete by the
   scheduler's failure detector (Phase 5 §failure).

The three public builders are pure (string in, string out). They take a
:class:`DispatchContext` snapshot and never touch I/O. Tests rely on
their determinism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.models import AgentKind, FlowAgent, FlowTask
from app.repo_merge_lock import merge_lock_reference, self_merge_instruction
from app.worktree.lookup import WorktreeInfo


# Canonical anti-loop header used by all scheduler dispatch messages.
_CONTEXT_HEADER = "## ClawsomeFlow Dispatch Context"


# ──────────────────────────────────────────────────────────────────────
# Inputs (snapshots, not live objects)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkerReport:
    """One worker's inbox message to the leader."""

    from_agent: str
    summary: str
    task_id: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class UpstreamOutput:
    """A *direct* (1-hop) upstream task's hand-off info, as shown to the
    downstream worker in its dispatch prompt.

    Only first-level ``depends_on`` parents are passed through — never
    transitively further upstream. The downstream worker is expected to
    treat the upstream worker's worktree as its source of truth (via
    ``git log`` / ``git diff``) if it needs to read changes; the
    ``summary`` is the strict-match upstream completion message for this
    task id (sender + task_id both match). If absent, downstream should
    inspect the upstream worktree history directly.
    """

    task_id: str
    subject: str
    from_agent: str
    worktree_path: str | None
    branch_name: str | None
    base_branch: str | None
    summary: str | None  # strict-match upstream completion summary, if known
    repo_root: str | None = None  # baseline repo path (shown only when merge_reference)


@dataclass(frozen=True)
class DispatchContext:
    """Per-task snapshot used to render a dispatch message.

    All fields are immutable so passing this around is trivial; populated
    by the RunController right before dispatching.
    """

    # Run / Flow context
    run_id: str
    team_name: str
    flow_description: str
    flow_inputs: dict[str, object]
    user: str

    # Target agent + task
    agent: FlowAgent
    task: FlowTask
    leader_agent_id: str

    # ClawTeam-side task id (the opaque value returned by ``task_create``).
    # Required for the worker's `clawteam task update` step — ClawTeam
    # tracks its own ids, not our user-facing ``FlowTask.id``. ``None`` is
    # tolerated only for pre-compile contexts (tests / fallbacks); the
    # worker prompt will fall back to ``task.id`` then so existing test
    # snapshots still resolve.
    clawteam_task_id: str | None = None

    # Worktree (None for OpenClaw self-merge tasks where worktree may not exist yet)
    worktree: WorktreeInfo | None = None

    # Leader-only extras (filled by build_leader_dispatch())
    worker_worktrees: list[WorktreeInfo] = field(default_factory=list)
    worker_reports: list[WorkerReport] = field(default_factory=list)

    # Worker-only extras (filled when task.depends_on is non-empty);
    # see ``RunController._compose_dispatch_context``. **First-level only.**
    upstream_outputs: list[UpstreamOutput] = field(default_factory=list)

    # True when THIS task must self-merge its worktree branch into the baseline
    # branch in-task (resolved per task by app/flow_modes.py::task_self_merges —
    # easy mode, dev-mode auto-merge tasks, OpenClaw under dev mode, and every
    # task of a scheduled normal run). When set, the dispatch adds the self-merge
    # block and tells the worker to cite post-merge baseline absolute paths
    # (the worktree is deleted at run end).
    self_merge: bool = False

    # True in dev/easy modes only (flow_modes.merge_reference_enabled): inject the
    # generic merge + repo-lock reference and the upstream/worker repo_root values
    # needed to target a cross-worktree merge. Normal mode keeps it False (even a
    # scheduled-normal auto-merge task — that only self-merges its own branch via
    # the existing self-merge step), so normal-mode prompts stay lean.
    merge_reference: bool = False


# ──────────────────────────────────────────────────────────────────────
# Public builders
# ──────────────────────────────────────────────────────────────────────


def build_worker_dispatch(ctx: DispatchContext) -> str:
    """Render the worker dispatch message (DEV.md §5.2: 4–5 blocks).

    Layout (in order):
      1. ``## ClawsomeFlow Dispatch Context``
      2. ``## Your Role``
      3. ``## Workspace Context`` (worktree path; OpenClaw extras)
      4. ``## Direct Upstream Outputs`` (only when ``ctx.upstream_outputs`` is non-empty;
         **first-level depends_on only** — never transitively further upstream)
      5. ``## Git Merge & Repo Lock Reference`` (generic, non-mandatory how-to —
         only when ``ctx.merge_reference``; the *mandate* to merge stays in the
         checklist)
      6. ``## Task #{id}: {subject}`` + description + ``## Completion Checklist``
    """
    blocks = [
        _scheduling_context_block(ctx),
        _identity_block(ctx),
        _work_context_block(ctx),
        _upstream_outputs_block(ctx),
        _git_merge_reference_block(ctx),
        _task_block(ctx),
        _worker_completion_steps(ctx),
    ]
    # Drop empty blocks (e.g. _upstream_outputs_block returns "" when no parents).
    return "\n\n".join(b for b in blocks if b).strip() + "\n"


def build_leader_dispatch(ctx: DispatchContext) -> str:
    """Render the leader (summary node) dispatch — 6 blocks.

    Same as worker, plus:
      5. ``## Flow Goal``
      6. ``## Worker Worktrees and Branches`` (only summary dependencies)
      7. ``## Worker Reports`` (only summary dependencies)
      8. ``## Git Merge & Repo Lock Reference`` (generic how-to; only when
         ``ctx.merge_reference``)
    """
    blocks = [
        _scheduling_context_block(ctx),
        _identity_block(ctx),
        _work_context_block(ctx),
        _flow_goal_block(ctx),
        _worker_worktrees_block(ctx),
        _worker_reports_block(ctx),
        _git_merge_reference_block(ctx),
        _task_block(ctx),
        _leader_completion_steps(ctx),
    ]
    # Drop empty blocks (e.g. _git_merge_reference_block when merge_reference is off).
    return "\n\n".join(b for b in blocks if b).strip() + "\n"


def build_openclaw_self_merge(ctx: DispatchContext) -> str:
    """[deprecated, kept as escape hatch]

    The canonical design dispatches merge requirements during
    complaint/satisfaction stage. Worker-task prompts no longer include merge
    steps. This builder is retained only as a fallback for exceptional recovery
    paths that still need an explicit self-merge instruction.

    This builder is retained for the rare case where a Run's last regular
    task didn't include the self-merge steps (e.g. a previous version was
    used or a recovery flow needs an explicit catch-up). Production paths
    should not call it.
    """
    if ctx.worktree is None:
        raise ValueError(
            "build_openclaw_self_merge requires ctx.worktree to be set"
        )
    blocks = [
        _scheduling_context_block(ctx),
        _identity_block(ctx),
        _openclaw_main_repo_block(ctx),
        _self_merge_task_block(ctx),
        _self_merge_completion_steps(ctx),
    ]
    return "\n\n".join(blocks).strip() + "\n"


# ──────────────────────────────────────────────────────────────────────
# Block helpers (one per section so unit tests can assert on each)
# ──────────────────────────────────────────────────────────────────────


def _scheduling_context_block(ctx: DispatchContext) -> str:
    """The all-important header — overrides any skill's worker loop."""
    return (
        f"{_CONTEXT_HEADER}\n"
        "This message is dispatched by the ClawsomeFlow scheduler.\n"
        "**Execute only this task**. Do not self-discover tasks "
        "(for example: `clawteam task list`, `clawteam task next-available`).\n"
        "After finishing the checklist, **stay idle and wait** for the next dispatch.\n"
        f"Run ID: `{ctx.run_id}`  ·  Team: `{ctx.team_name}`"
    )


def _identity_block(ctx: DispatchContext) -> str:
    role = "Leader" if ctx.agent.is_leader else "Worker"
    kind = ctx.agent.kind.value
    return (
        "## Your Role\n"
        f"- agent_name: `{ctx.agent.id}`\n"
        f"- team: `{ctx.team_name}`\n"
        f"- role: **{role}**\n"
        f"- agent_kind: `{kind}`\n"
        f"- leader: `{ctx.leader_agent_id}`\n"
        f"- user: `{ctx.user}`"
    )


def _work_context_block(ctx: DispatchContext) -> str:
    if ctx.worktree is None:
        # Should be rare for regular tasks; helpful for diagnostics if it happens.
        body = (
            "_(worktree is not available yet; scheduler will refresh it after spawn)_"
        )
    else:
        lines = [
            f"- worktree absolute path: `{ctx.worktree.worktree_path}`",
            f"- branch: `{ctx.worktree.branch_name}`",
            f"- base branch: `{ctx.worktree.base_branch}`",
        ]
        # OpenClaw runtime workspace (openclaw.json) differs from task worktree;
        # only OpenClaw needs explicit "never write in baseline workspace" constraints.
        if ctx.agent.kind == AgentKind.openclaw:
            lines.extend([
                f"- baseline-branch workspace: `{ctx.worktree.repo_root}`",
                "- OpenClaw agent note: `openclaw.json` workspace is different from this task worktree.",
                "- **All writes must stay under this worktree path**; never write in the baseline-branch workspace.",
            ])
        body = "\n".join(lines)
    return f"## Workspace Context\n{body}"


def _upstream_outputs_block(ctx: DispatchContext) -> str:
    """Render the **first-level** upstream task summaries.

    Returns ``""`` (suppressed by the join in :func:`build_worker_dispatch`)
    when this task has no immediate dependencies. We deliberately do NOT
    walk the DAG transitively — only ``task.depends_on`` parents are
    surfaced. The downstream worker can always read further history via
    ``git log`` if it wants to.

    The block lists, per direct upstream:

    * upstream task id + subject + owning agent
    * upstream worktree path + branch + base branch (if known) — so the worker
      can ``cd <path> && git diff <base>...HEAD`` to inspect changes
    * the upstream's strict-match completion summary (from leader inbox;
      sender + task_id must both match this upstream task)
    """
    if not ctx.upstream_outputs:
        return ""
    n = len(ctx.upstream_outputs)
    lines: list[str] = [f"## Direct Upstream Outputs ({n} item(s), first-level dependencies only)"]
    for u in ctx.upstream_outputs:
        lines.append(
            f"- task `{u.task_id}` \"{u.subject}\" by agent `{u.from_agent}`"
        )
        if u.worktree_path:
            quals: list[str] = []
            if u.branch_name:
                quals.append(f"branch `{u.branch_name}`")
            if u.base_branch:
                quals.append(f"base branch `{u.base_branch}`")
            suffix = f" ({', '.join(quals)})" if quals else ""
            lines.append(f"  - worktree: `{u.worktree_path}`{suffix}")
            # repo_root is only useful for targeting a merge — surface it only when
            # this dispatch may merge (see Git Merge & Repo Lock Reference).
            if ctx.merge_reference and u.repo_root:
                lines.append(f"  - repo root (merge target): `{u.repo_root}`")
        else:
            lines.append("  - worktree: _(unknown — agent session may have been disposed)_")
        if u.summary:
            lines.append(f"  - completion summary: {u.summary}")
        else:
            lines.append("  - completion summary: _(missing; inspect upstream worktree git history)_")

    return "\n".join(lines)


def _git_merge_reference_block(ctx: DispatchContext) -> str:
    """Generic merge + repo-lock convention — injected only when
    ``ctx.merge_reference`` (dev/easy modes only).

    Returns ``""`` (dropped by the block join) otherwise, so normal-mode
    prompts stay lean and merge-free. Pure reference material (see
    :func:`app.repo_merge_lock.merge_lock_reference`): it documents the *generic
    method* for merging any upstream branch into any developer-specified repo —
    it NEVER mandates a merge (the obligation lives only in the auto-merge task's
    completion checklist).
    """
    if not ctx.merge_reference:
        return ""
    return "## Git Merge & Repo Lock Reference\n" + merge_lock_reference()


def _task_block(ctx: DispatchContext) -> str:
    desc = ctx.task.description.strip() or "_(no additional description)_"
    return (
        f"## Task #{ctx.task.id}: {ctx.task.subject}\n"
        f"{desc}"
    )


def _scheduled_self_merge_steps(
    ctx: DispatchContext, start_no: int,
) -> tuple[list[str], int]:
    """Self-merge + post-merge-path steps for scheduled runs (whole block).

    Scheduled runs are unattended: there is no user merge-review or complaint
    phase, so every task must merge its own worktree branch into the baseline
    branch itself, and must reference deliverables by their post-merge absolute
    path under the baseline workspace (not the worktree). Inserted as one
    cohesive block so it never tangles with the non-scheduled wording.
    """
    wt = ctx.worktree.worktree_path if ctx.worktree else "<worktree-path>"
    repo_root = ctx.worktree.repo_root if ctx.worktree else "<baseline-workspace>"
    branch = ctx.worktree.branch_name if ctx.worktree else "<branch>"
    base = ctx.worktree.base_branch if ctx.worktree else "<base>"
    merge_line = self_merge_instruction(
        repo_root=repo_root,
        base_branch=base,
        feature_branch=branch,
        merge_message=f"csflow: scheduled merge {branch}",
    )
    steps = [
        f"{start_no}. **Self-merge:** {merge_line}",
        f"{start_no + 1}. **MUST:** after the merge, cite paths under `{repo_root}` on `{base}` "
        f"ONLY — NEVER the worktree `{wt}` (it is deleted when the run ends, so worktree paths "
        f"become dead links).",
    ]
    return steps, start_no + 2


def _worker_completion_steps(ctx: DispatchContext) -> str:
    """Worker completion checklist.

    Two flavours (per plan §8.5):

    * **Non-OpenClaw workers** — concise checklist (commit → inbox → task update).
    * **OpenClaw workers** — keep extra workspace/write constraints.
      Merge is deferred to complaint/satisfaction stage; worker-task dispatch
      MUST NOT include baseline merge instructions.
    """
    leader = ctx.leader_agent_id
    team = ctx.team_name
    task_id = ctx.task.id
    # ClawTeam stores tasks under its own opaque id assigned at
    # ``task_create`` time — NOT our human-readable ``FlowTask.id``.
    # The worker must use the ClawTeam id in ``clawteam task update``,
    # otherwise ClawTeam responds with ``Task 'X' not found`` and the
    # task is stuck "in-flight" from the scheduler's perspective.
    # Fall back to the FlowTask.id when no compile mapping is wired
    # (tests / fallback paths) so existing snapshots still resolve.
    ct_task_id = ctx.clawteam_task_id or ctx.task.id
    subject = ctx.task.subject
    wt = ctx.worktree.worktree_path if ctx.worktree else "<worktree-path>"
    steps: list[str] = []
    next_no = 1

    if ctx.agent.kind == AgentKind.openclaw:
        steps.append(
            f"{next_no}. If this task requires changes to workspace content, make those changes in the "
            f"worktree (`{wt}`), not in the baseline-branch workspace. Use absolute paths for paths under "
            "the worktree."
        )
        next_no += 1
        steps.append(
            f"{next_no}. If this task involves work-document output, use your professional judgment and place files under "
            f"`{wt}/my-desktop/` using a fitting folder; if the task involves updates to existing documents under "
            "`my-desktop/`, edit those files directly. Do not leave deliverable docs at worktree root."
        )
        next_no += 1

    steps.append(
        f"{next_no}. `cd {wt} && git add -A && git commit -m 'task {task_id}: {subject}'`"
    )
    next_no += 1

    if ctx.self_merge:
        merge_steps, next_no = _scheduled_self_merge_steps(ctx, next_no)
        steps.extend(merge_steps)

    if not ctx.task.is_leader_summary:
        steps.append(
            f"{next_no}. `clawteam inbox send {team} {leader} "
            f"\"task {task_id} done: <completion-summary>\"` "
            "(send to leader; this is the standard message header and MUST start "
            f"with the exact literal prefix `task {task_id} done:`. The `<completion-summary>` MUST include "
            "a concise summary of your work and absolute paths of important changed "
            "docs/files (one line is fine). If task "
            "\"Output summary requirement\" defines a format, apply it after this "
            "header. **Send this before "
            "marking task completed**.)"
        )
        next_no += 1

    steps.append(
        f"{next_no}. **VERY IMPORTANT: you MUST execute** "
        f"`clawteam task update {team} {ct_task_id} --status completed` "
        "**before ending this turn.**"
    )
    next_no += 1

    steps.append(
        f"{next_no}. **End this turn**"
    )

    # ── Failure path (mandatory) ────────────────────────────────────────
    # Even if any step above fails (commit conflict, merge can't resolve,
    # external tool down, etc.) we MUST mark the ClawTeam task completed
    # and let the leader decide what to do — otherwise the scheduler sits
    # waiting on a pending task and the whole Run stalls. The leader gets
    # the failure details via inbox and surfaces them in the summary.
    #
    # For a leader-summary task, ``build_leader_dispatch`` is the normal
    # entry — but tests also exercise ``build_worker_dispatch`` with a
    # summary task, and that path must not generate an inbox send (the
    # leader inboxing itself is pointless). Drop the inbox line in that
    # corner case while still emitting the mark-complete instruction.
    if ctx.task.is_leader_summary:
        failure_section = (
            "\n\n## On Failure (mandatory — do not skip)\n"
            "If you cannot complete the summary task, do not leave it pending. Instead:\n"
            "1. Commit a partial deliverable with a clear blocker note.\n"
            f"2. **VERY IMPORTANT: you MUST execute** "
            f"`clawteam task update {team} {ct_task_id} --status completed` "
            "(so scheduler can continue finalize).\n"
            "3. End your turn."
        )
    else:
        failure_section = (
            "\n\n## On Failure (mandatory — do not skip)\n"
            "If this task is still blocked after reasonable attempts, do not leave it "
            "in `pending`/`in_progress`. Instead:\n"
            f"1. `clawteam inbox send {team} {leader} "
            f"\"task {task_id} done: FAILED — <one-line reason + what you tried>\"`"
            " (keep the same exact header prefix).\n"
            f"2. **VERY IMPORTANT: you MUST execute** "
            f"`clawteam task update {team} {ct_task_id} --status completed` "
            "(so scheduler can continue downstream/finalize).\n"
            "3. End your turn. Do not retry on your own."
        )
    return "## Completion Checklist\n" + "\n".join(steps) + failure_section


def _flow_goal_block(ctx: DispatchContext) -> str:
    desc = ctx.flow_description.strip() or "_(Flow has no description)_"
    inputs_lines = [
        f"  - **{k}**: `{v}`" for k, v in (ctx.flow_inputs or {}).items()
    ]
    inputs_section = "\n".join(inputs_lines) if inputs_lines else "  _(none)_"
    return (
        "## Flow Goal\n"
        f"{desc}\n"
        "Runtime inputs:\n"
        f"{inputs_section}"
    )


def _worker_worktrees_block(ctx: DispatchContext) -> str:
    if not ctx.worker_worktrees:
        if ctx.task.depends_on:
            return (
                "## Worker Worktrees and Branches\n"
                "_(no dependency worker worktree found; check summary dependencies/session state)_"
            )
        return (
            "## Worker Worktrees and Branches\n"
            "_(summary task has no dependencies configured)_"
        )
    # Only surface each worker's worktree path + branch — that's what the leader
    # needs to read their outputs. We deliberately do NOT list the baseline
    # workspace: the leader has no business reading or writing there (the merge
    # is ClawsomeFlow's job), and naming it only tempts the leader to reference /
    # copy into baseline paths (see run-40aaf5dde2c5).
    lines = []
    for w in ctx.worker_worktrees:
        base = f", base branch `{w.base_branch}`" if w.base_branch else ""
        lines.append(
            f"- **{w.agent_name}** → worktree `{w.worktree_path}` "
            f"(branch `{w.branch_name}`{base})"
        )
        # repo_root only matters for targeting a merge — surface it only when this
        # dispatch may merge (keeps the original anti pre-copy guard for the rest).
        if ctx.merge_reference and w.repo_root:
            lines.append(f"  - repo root (merge target): `{w.repo_root}`")
    return "## Worker Worktrees and Branches\n" + "\n".join(lines)


def _worker_reports_block(ctx: DispatchContext) -> str:
    if not ctx.worker_reports:
        if ctx.task.depends_on:
            return (
                "## Worker Reports\n"
                "_(no report matched the configured summary dependencies yet)_"
            )
        return "## Worker Reports\n_(summary task has no dependencies configured)_"
    lines = []
    for r in ctx.worker_reports:
        prefix = f"`{r.task_id}` " if r.task_id else ""
        lines.append(f"- {prefix}**{r.from_agent}**: {r.summary}")
    return "## Worker Reports\n" + "\n".join(lines)


def _leader_completion_steps(ctx: DispatchContext) -> str:
    """Leader summary task completion steps (per plan §8.5 leader task block).

    The leader is expected to:

    1. **Produce the final deliverable** in its worktree (report / integrated
       code / deployment manifest — depending on the Flow's domain).
    2. **Commit it** so the deliverable is reviewable by the user via the
       worktree branch.
    3. Emit a user-facing final reply to leader inbox using the fixed
       prefix ``leader final reply:`` so the Run page can display it.
    """
    team = ctx.team_name
    task_id = ctx.task.id
    # See _worker_completion_steps for why we must pass the ClawTeam id.
    ct_task_id = ctx.clawteam_task_id or ctx.task.id
    wt = ctx.worktree.worktree_path if ctx.worktree else "<your-worktree>"
    repo_root = ctx.worktree.repo_root if ctx.worktree else "<baseline-workspace>"
    base = ctx.worktree.base_branch if ctx.worktree else "<base>"

    # Step 2 — produce + place the deliverable. The ONLY kind-specific difference
    # is the `my-desktop/` convention: it is an OpenClaw-workspace thing. A TUI
    # agent works directly inside the Flow's repo worktree, where dumping a
    # `my-desktop/` folder would be wrong — it just writes a fitting structure.
    #
    # For BOTH kinds the deliverable stays in the worktree and ClawsomeFlow merges
    # it into the project later (manual review for TUI, satisfaction stage for
    # OpenClaw — neither merges during this task). The agent must therefore NEVER
    # copy/move files out of the worktree: a stray file in the project working
    # tree aborts `clawteam workspace merge` ("untracked working tree files would
    # be overwritten by merge") and the run ends completed_with_conflicts. We do
    # NOT name the baseline workspace at all — mentioning it is what drove the
    # leader to pre-copy its report there (run-40aaf5dde2c5).
    if ctx.agent.kind == AgentKind.openclaw:
        deliverable_step = (
            f"1. In `{wt}`, **produce the final deliverable**:\n"
            f"   - If this task involves work-document output, place it under `{wt}/my-desktop/` using a "
            f"fitting folder; if it involves modifications to existing documents under `{wt}/my-desktop/`, "
            "edit them directly. Do not leave deliverable docs at the worktree root.\n"
            "   - When reporting reference paths, keep worker files under each worker's own "
            "path; never rewrite worker files as leader workspace paths."
        )
    else:
        deliverable_step = (
            f"1. In `{wt}`, **produce the final deliverable**:\n"
            f"   - Write every output inside your worktree (`{wt}`) using a structure that fits the repo. "
            "Use absolute worktree paths so nothing lands outside the worktree.\n"
            "   - When reporting reference paths, keep worker files under each worker's own "
            "path; never rewrite worker files as leader workspace paths."
        )

    steps: list[str] = [
        deliverable_step,
        f"2. `cd {wt} && git add -A && "
        f"git commit -m 'task {task_id}: leader summary'`.",
    ]
    next_no = 3

    if ctx.self_merge:
        merge_steps, next_no = _scheduled_self_merge_steps(ctx, next_no)
        steps.extend(merge_steps)

    if ctx.self_merge:
        steps.append(
            f"{next_no}. **MUST self-check:** every absolute path in your final reply MUST be a "
            f"post-merge path under the baseline workspace `{repo_root}` on `{base}` (NEVER the "
            f"worktree). Confirm each one exists with `test -f <baseline-absolute-path>` "
            "(identical command on Linux and macOS) before sending."
        )
    else:
        steps.append(
            f"{next_no}. Verify every absolute path you plan to mention in final reply actually "
            "exists before sending (e.g. `test -f <absolute-path>`)."
        )
    next_no += 1

    reply_paths = (
        "post-merge baseline absolute paths" if ctx.self_merge else "absolute paths"
    )
    steps.append(
        f"{next_no}. `clawteam inbox send {team} {ctx.agent.id} "
        f"\"leader final reply: <concise summary + {reply_paths}>\"` "
        "(required — keep the literal prefix `leader final reply:`.)"
    )
    next_no += 1

    steps.append(
        f"{next_no}. **VERY IMPORTANT: you MUST execute** "
        f"`clawteam task update {team} {ct_task_id} --status completed` "
        "**before ending this turn.**"
    )
    next_no += 1
    steps.append(
        f"{next_no}. **End this turn**"
    )

    return (
        "## Completion Checklist\n"
        + "\n".join(steps)
        + "\n\n"
        + "## On Failure (mandatory — do not skip)\n"
        + "If you can't produce the deliverable for any reason (worker outputs "
        + "missing / conflicting / tool failure / etc.), DO NOT leave the "
        + "summary task pending — that stalls Run finalization. Instead:\n"
        + f"1. Commit whatever partial deliverable you can plus a `## Failure` "
        + "section explaining what went wrong and which worker outputs were missing.\n"
        + f"2. `clawteam inbox send {team} {ctx.agent.id} "
        + "\"leader final reply: FAILED — <one-line blocker and current status>\"`.\n"
        + f"3. **VERY IMPORTANT: you MUST execute** "
        + f"`clawteam task update {team} {ct_task_id} --status completed` "
        + "(scheduler can then enter finalize).\n"
        + "4. End your turn. The user will see your `## Failure` section in "
        + "ClawsomeFlow's review UI and decide next steps."
    )


# ── OpenClaw self-merge specific ──────────────────────────────────────


def _openclaw_main_repo_block(ctx: DispatchContext) -> str:
    return (
        "## Baseline Workspace (self-merge)\n"
        f"- repo: `{ctx.worktree.repo_root}` branch `{ctx.worktree.base_branch}`\n"
        f"- worktree: `{ctx.worktree.worktree_path}` branch `{ctx.worktree.branch_name}`"
    )


def _self_merge_task_block(ctx: DispatchContext) -> str:
    return (
        f"## Task #{ctx.task.id}: Self-merge wrap-up\n"
        "Merge this run's worktree branch into the baseline branch."
    )


def _self_merge_completion_steps(ctx: DispatchContext) -> str:
    team = ctx.team_name
    task_id = ctx.task.id
    ct_task_id = ctx.clawteam_task_id or ctx.task.id
    wt = ctx.worktree.worktree_path
    baseline_workspace = ctx.worktree.repo_root
    branch = ctx.worktree.branch_name
    base = ctx.worktree.base_branch
    merge_line = self_merge_instruction(
        repo_root=baseline_workspace,
        base_branch=base,
        feature_branch=branch,
        merge_message=f"csflow: merge {branch} after run",
    )
    return (
        "## Completion Checklist\n"
        f"1. `cd {wt} && git add -A && git commit -m 'task {task_id}: final checkpoint'` if needed.\n"
        f"2. {merge_line}\n"
        f"3. `git log --oneline | head -5`\n"
        f"4. **VERY IMPORTANT:** `clawteam task update {team} {ct_task_id} --status completed` "
        "(or `--status blocked` + inbox leader on failure).\n"
        "5. **End this turn**"
    )


__all__ = [
    "DispatchContext",
    "UpstreamOutput",
    "WorkerReport",
    "build_leader_dispatch",
    "build_openclaw_self_merge",
    "build_worker_dispatch",
]
