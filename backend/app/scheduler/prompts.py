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

    # True for runs triggered by a timed schedule. Scheduled runs require every
    # task (worker + leader) to self-merge into the baseline branch and to
    # report deliverables using post-merge absolute paths; the user merge-review
    # and complaint phases are skipped (see scheduler/finalize.py).
    is_scheduled: bool = False


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
      5. ``## Task #{id}: {subject}`` + description + ``## Completion Checklist``
    """
    blocks = [
        _scheduling_context_block(ctx),
        _identity_block(ctx),
        _work_context_block(ctx),
        _upstream_outputs_block(ctx),
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
    """
    blocks = [
        _scheduling_context_block(ctx),
        _identity_block(ctx),
        _work_context_block(ctx),
        _flow_goal_block(ctx),
        _worker_worktrees_block(ctx),
        _worker_reports_block(ctx),
        _task_block(ctx),
        _leader_completion_steps(ctx),
    ]
    return "\n\n".join(blocks).strip() + "\n"


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
    * upstream worktree path + branch (if known) — so the worker can
      ``cd <path> && git diff <base>...HEAD`` to inspect changes
    * the upstream's strict-match completion summary (from leader inbox;
      sender + task_id must both match this upstream task)
    * a short "How to inspect upstream changes" footer with the two canonical commands
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
            branch = f" (branch `{u.branch_name}`)" if u.branch_name else ""
            lines.append(f"  - worktree: `{u.worktree_path}`{branch}")
        else:
            lines.append("  - worktree: _(unknown — agent session may have been disposed)_")
        if u.summary:
            lines.append(f"  - completion summary: {u.summary}")
        else:
            lines.append("  - completion summary: _(missing; inspect upstream worktree git history)_")

    lines.append("")
    lines.append("To inspect upstream changes:")
    # Use concrete base when single-source; otherwise keep a generic placeholder.
    bases = {u.base_branch for u in ctx.upstream_outputs if u.base_branch}
    base_hint = next(iter(bases)) if len(bases) == 1 else "<base>"
    lines.append(
        f"- `cd <upstream-worktree> && git log --oneline {base_hint}..HEAD | head`"
    )
    lines.append(
        f"- or `cd <upstream-worktree> && git diff {base_hint}...HEAD <optional-path>`"
    )
    return "\n".join(lines)


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
    steps = [
        f"{start_no}. **Scheduled run — self-merge into the baseline branch yourself**: "
        f"`cd {repo_root} && git checkout {base} && git pull --ff-only || true && "
        f"git merge --no-ff {branch} -m 'csflow: scheduled merge {branch}'`. "
        "If conflicts occur, resolve them (keep your changes plus unrelated "
        "baseline changes), then `git add -A && git commit`.",
        f"{start_no + 1}. After merging, your deliverables live under the baseline "
        f"workspace `{repo_root}` on `{base}`. **Every output path you mention MUST "
        f"be the post-merge absolute path under `{repo_root}` — never a worktree "
        f"path under `{wt}`.**",
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

    if ctx.is_scheduled:
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
    lines = [
        f"- **{w.agent_name}** → worktree `{w.worktree_path}` (branch `{w.branch_name}`)"
        for w in ctx.worker_worktrees
    ]
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

    if ctx.is_scheduled:
        merge_steps, next_no = _scheduled_self_merge_steps(ctx, next_no)
        steps.extend(merge_steps)

    steps.append(
        f"{next_no}. Verify every absolute path you plan to mention in final reply actually "
        "exists before sending (e.g. `test -f <absolute-path>`)."
    )
    next_no += 1

    steps.append(
        f"{next_no}. `clawteam inbox send {team} {ctx.agent.id} "
        "\"leader final reply: <concise summary + absolute paths>\"` "
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
    """For self-merge: the baseline workspace IS the OpenClaw agent's workspace_path.

    For OpenClaw agents managed by ClawsomeFlow, ``ctx.worktree.repo_root``
    is exactly ``~/.clawsomeflow/agents/{id}/workspace/``.
    """
    return (
        "## Baseline Workspace Context (self-merge)\n"
        f"- baseline workspace: `{ctx.worktree.repo_root}` (registered workspace in openclaw.json)\n"
        f"- base branch: `{ctx.worktree.base_branch}`\n"
        f"- your worktree: `{ctx.worktree.worktree_path}` (branch `{ctx.worktree.branch_name}`)\n"
        "Goal: merge all commits from the worktree branch into the base branch."
    )


def _self_merge_task_block(ctx: DispatchContext) -> str:
    return (
        f"## Task #{ctx.task.id}: Self-merge wrap-up\n"
        "Merge all commits produced in this run's worktree branch into the baseline branch. "
        "If conflicts occur due to new unrelated commits on the baseline branch, resolve them yourself."
    )


def _self_merge_completion_steps(ctx: DispatchContext) -> str:
    team = ctx.team_name
    task_id = ctx.task.id
    ct_task_id = ctx.clawteam_task_id or ctx.task.id
    wt = ctx.worktree.worktree_path
    baseline_workspace = ctx.worktree.repo_root
    branch = ctx.worktree.branch_name
    base = ctx.worktree.base_branch
    return (
        "## Completion Checklist\n"
        f"1. `cd {wt} && git status` to ensure all changes are committed; if needed, "
        f"run `git add -A && git commit -m 'task {task_id}: final checkpoint'`.\n"
        f"2. `cd {baseline_workspace} && git checkout {base} && git pull --ff-only || true`.\n"
        f"3. `cd {baseline_workspace} && git merge --no-ff {branch} -m 'csflow: merge {branch} after run'`.\n"
        "4. **If conflicts occur**: resolve files by preserving your intended changes plus "
        "unrelated baseline-branch changes, then `git add <file>` and `git commit`.\n"
        "5. Verify with `git log --oneline | head -5` to confirm task + merge commits.\n"
        f"6. **VERY IMPORTANT: you MUST execute** "
        f"`clawteam task update {team} {ct_task_id} --status completed` "
        "on success; otherwise use `--status blocked` and inbox the leader with the blocker.\n"
        "7. **End this turn**"
    )


__all__ = [
    "DispatchContext",
    "UpstreamOutput",
    "WorkerReport",
    "build_leader_dispatch",
    "build_openclaw_self_merge",
    "build_worker_dispatch",
]
