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

# ── Remote-node parameter hand-off protocol (on-disk / wire contract) ───
# When a task's downstream includes one or more ``remote_csflow`` external
# nodes that declare param fields, each such node needs concrete values for
# ITS remote Flow's params. We collect them from the upstream executors via
# dedicated inbox message(s) so the normal ``task <id> done:`` completion
# summary stays untouched. Prefer ONE message PER downstream target:
#
#     csflow-remote-params: <upstream_task_id> <downstream_task_id>
#     {"<field>": "<value or empty>", ...}
#
# Legacy single-block form (still accepted by the parser) was:
#
#     csflow-remote-params: <upstream_task_id>
#     {"<field>": "<value or empty>", ...}
#
# Never rename the prefix — the scheduler parser and the dispatch prompts
# both key off it.
REMOTE_PARAMS_HEADER = "csflow-remote-params"

#: Prepended (only) to a summary that originated from a remote executor
#: (webhook / remote ClawsomeFlow) before it is handed to a LOCAL downstream
#: worker, so the worker does not mistake foreign absolute paths for local
#: ones. Kept short — it wraps just that one upstream's summary segment.
REMOTE_ORIGIN_NOTE = (
    "[来自远端执行方的产出；其中提到的绝对路径可能位于远端、在本机不存在，"
    "请勿据此直接访问本地文件]"
)

#: Value written for a remote param field that no upstream / user / leader
#: could fill. Sent verbatim so the remote Flow sees an explicit "unknown".
EMPTY_PARAM_PLACEHOLDER = "参数为空"


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
    transitively further upstream. For local-agent upstreams the worker may
    treat the upstream worktree as its source of truth (via ``git log`` /
    ``git diff``). External-node upstreams own no worktree — never invent
    paths for them; the ``summary`` is the only hand-off.
    """

    task_id: str
    subject: str
    from_agent: str
    worktree_path: str | None
    branch_name: str | None
    base_branch: str | None
    summary: str | None  # strict-match upstream completion summary, if known
    repo_root: str | None = None  # baseline repo path (shown only when merge_reference)
    #: True when the upstream owner is ``AgentKind.external`` (no worktree).
    is_external: bool = False


@dataclass(frozen=True)
class RemoteParamTarget:
    """One downstream ``remote_csflow`` node that needs param values from
    the task currently being dispatched.

    Identity is the downstream FlowTask id (header's second token). Flow
    name + description come from the pasted remote-call-info blob so the
    upstream agent can judge what each field should contain.
    """

    downstream_task_id: str
    agent_id: str
    flow_name: str
    flow_description: str
    param_fields: tuple[str, ...]


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

    # Non-empty ONLY when THIS task has one or more downstream
    # ``remote_csflow`` nodes whose target Flows declare param fields.
    # Empty — including "downstream is remote_csflow but zero param fields"
    # — means no special hand-off. When non-empty, dispatch asks for ONE
    # ``csflow-remote-params: <this_task> <downstream_task>`` JSON block
    # (local: separate inbox message; external: appended sections) per target.
    remote_param_targets: tuple[RemoteParamTarget, ...] = ()

    # True when THIS task must self-merge its worktree branch into the baseline
    # branch in-task (resolved per task by app/flow_modes.py::task_self_merges —
    # easy mode, dev-mode auto-merge tasks, OpenClaw under dev mode, and every
    # task of an unattended normal run — timed schedule or MCP/--unattended).
    # When set, the dispatch adds the self-merge block and tells the worker to
    # cite post-merge baseline absolute paths (the worktree is deleted at run end).
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
      4. ``## Runtime inputs`` (user-defined Flow parameter values for this run)
      5. ``## Direct Upstream Outputs`` (only when ``ctx.upstream_outputs`` is non-empty;
         **first-level depends_on only** — never transitively further upstream)
      6. ``## Git Merge & Repo Lock Reference`` (generic, non-mandatory how-to —
         only when ``ctx.merge_reference``; the *mandate* to merge stays in the
         checklist)
      7. ``## Task #{id}: {subject}`` + description + ``## Completion Checklist``
    """
    blocks = [
        _scheduling_context_block(ctx),
        _identity_block(ctx),
        _work_context_block(ctx),
        _runtime_inputs_block(ctx),
        _upstream_outputs_block(ctx),
        _git_merge_reference_block(ctx),
        _task_block(ctx),
        _remote_param_collection_block(ctx),
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
        # External upstreams own no worktree/branch — never invent paths for
        # the downstream (would be empty or misleading).
        if u.is_external:
            pass
        elif u.worktree_path:
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
        elif u.is_external:
            lines.append(
                "  - completion summary: _(missing — wait for the external "
                "executor's result)_"
            )
        else:
            lines.append("  - completion summary: _(missing; inspect upstream worktree git history)_")

    return "\n".join(lines)


def _format_remote_param_target_heading(target: RemoteParamTarget) -> str:
    """One-line identity for a downstream remote target in the prompt."""
    name = (target.flow_name or "").strip() or "(unnamed Flow)"
    return (
        f"downstream task `{target.downstream_task_id}` "
        f"(node `{target.agent_id}`) → Flow **{name}**"
    )


def _remote_param_collection_block(ctx: DispatchContext) -> str:
    """Ask a LOCAL worker to emit per-downstream inbox messages with params.

    Rendered only when ``ctx.remote_param_targets`` is non-empty. The worker
    keeps sending its normal ``task <id> done:`` completion unchanged; each
    target gets an ADDITIONAL inbox message so consumers stay isolated.
    Returns ``""`` (dropped by the block join) otherwise.
    """
    targets = ctx.remote_param_targets
    if not targets:
        return ""
    leader = ctx.leader_agent_id
    team = ctx.team_name
    task_id = ctx.task.id
    n = len(targets)
    lines = [
        "## Remote Parameter Report (extra messages — do NOT skip)",
        (
            f"{n} downstream remote ClawsomeFlow node(s) need parameter "
            "values from your result. AFTER sending your normal completion "
            "message above, send ONE separate inbox message to the leader "
            "for EACH target below. Never invent values; never include local "
            "absolute paths; use an empty string for any field you cannot fill."
        ),
        "",
    ]
    for i, target in enumerate(targets, start=1):
        schema = ", ".join(
            f'"{f}": "<value or empty>"' for f in target.param_fields
        )
        field_list = ", ".join(f"`{f}`" for f in target.param_fields)
        goal = (target.flow_description or "").strip() or "(no overall goal provided)"
        header = (
            f"{REMOTE_PARAMS_HEADER}: {task_id} {target.downstream_task_id}"
        )
        lines.append(f"### Target {i} — {_format_remote_param_target_heading(target)}")
        lines.append(f"- Overall goal: {goal}")
        lines.append(f"- Parameter fields: {field_list}")
        lines.append(
            f"- First line of the inbox message MUST be exactly `{header}` "
            "followed by a single JSON object with those fields."
        )
        lines.append(
            f"- Command: `clawteam inbox send {team} {leader} "
            f"\"{header}\\n{{{schema}}}\"`"
        )
        lines.append("")
    lines.append(
        "(These messages are separate from and in addition to your "
        "completion message.)"
    )
    return "\n".join(lines).rstrip()


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


def _unattended_self_merge_steps(
    ctx: DispatchContext, start_no: int,
) -> tuple[list[str], int]:
    """Self-merge + post-merge-path steps for unattended runs (whole block).

    Unattended runs (timed schedule OR MCP / ``--unattended``) have no user
    merge-review or complaint phase, so every task must merge its own worktree
    branch into the baseline branch itself, and must reference deliverables by
    their post-merge absolute path under the baseline workspace (not the
    worktree). Inserted as one cohesive block so it never tangles with the
    attended (review) wording.
    """
    wt = ctx.worktree.worktree_path if ctx.worktree else "<worktree-path>"
    repo_root = ctx.worktree.repo_root if ctx.worktree else "<baseline-workspace>"
    branch = ctx.worktree.branch_name if ctx.worktree else "<branch>"
    base = ctx.worktree.base_branch if ctx.worktree else "<base>"
    merge_line = self_merge_instruction(
        repo_root=repo_root,
        base_branch=base,
        feature_branch=branch,
        merge_message=f"csflow: unattended merge {branch}",
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
        merge_steps, next_no = _unattended_self_merge_steps(ctx, next_no)
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


def _public_flow_inputs(flow_inputs: dict[str, object] | None) -> dict[str, object]:
    """User-facing run inputs only — hide internal ``_csflow_*`` scheduler keys."""
    if not flow_inputs:
        return {}
    out: dict[str, object] = {}
    for key, value in flow_inputs.items():
        k = str(key).strip()
        if not k or k.startswith("_csflow_"):
            continue
        out[k] = value
    return out


def _runtime_inputs_lines(ctx: DispatchContext) -> str:
    inputs_lines = [
        f"  - **{k}**: `{v}`" for k, v in _public_flow_inputs(ctx.flow_inputs).items()
    ]
    return "\n".join(inputs_lines) if inputs_lines else "  _(none)_"


def _runtime_inputs_block(ctx: DispatchContext) -> str:
    return f"## Runtime inputs\n{_runtime_inputs_lines(ctx)}"


def _flow_goal_block(ctx: DispatchContext) -> str:
    desc = ctx.flow_description.strip() or "_(Flow has no description)_"
    return (
        "## Flow Goal\n"
        f"{desc}\n"
        f"{_runtime_inputs_block(ctx)}"
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
        merge_steps, next_no = _unattended_self_merge_steps(ctx, next_no)
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


# ── External execution nodes (AgentKind.external) ────────────────────


#: Injected into webhook outbound packages + task sheets only. Reminds a
#: partner system that upstream absolute paths are foreign to its host.
WEBHOOK_REMOTE_NOTES = (
    "This is a remote task. Absolute paths mentioned in upstream outputs "
    "may not exist on your machine — do not open or fetch them locally. "
    "In your callback summary, do not include local file paths; describe "
    "necessary results in plain text (links or references are fine)."
)


def _upstream_outputs_block_external(ctx: DispatchContext) -> str:
    """Upstream hand-offs for an external executor (human / webhook / remote).

    External nodes own no worktree. Never invent or surface local worktree /
    branch paths here — an external executor cannot use them, and a remote
    machine would only be misled. Summaries + task identity are enough.
    """
    if not ctx.upstream_outputs:
        return ""
    n = len(ctx.upstream_outputs)
    lines: list[str] = [
        f"## Direct Upstream Outputs ({n} item(s), first-level dependencies only)",
        "Upstream executors may be local agents or other external nodes. "
        "Use the completion summary below as context; there is no shared "
        "worktree path for this node.",
    ]
    for u in ctx.upstream_outputs:
        lines.append(
            f"- task `{u.task_id}` \"{u.subject}\" by agent `{u.from_agent}`"
        )
        if u.summary:
            lines.append(f"  - completion summary: {u.summary}")
        else:
            lines.append(
                "  - completion summary: _(not available yet — "
                "ask the run operator if you need prior deliverables)_"
            )
    return "\n".join(lines)


def build_external_task_text(
    ctx: DispatchContext, *, lang: str | None = None,
) -> str:
    """Human-readable task sheet for an external executor.

    Unlike the worker/leader builders this contains NO ClawTeam protocol
    steps (an external executor never talks to ClawTeam — the completion
    round-trip happens through the /api/external receipt endpoint or the
    WebUI card). Upstream summaries are included without worktree/branch
    paths — external nodes do not own a local workspace.

    ``lang`` is ``zh`` / ``en`` (default ``en``). Webhook channel additionally
    gets :data:`WEBHOOK_REMOTE_NOTES` so a partner host does not chase foreign
    absolute paths.
    """
    from app.models import ExternalChannel

    resolved = "zh" if (lang or "").strip().lower() == "zh" else "en"
    if resolved == "zh":
        intro = (
            "## ClawsomeFlow 外部任务\n"
            "请完成下方任务后，通过 Run 详情页的任务卡片或回调 API 回传结果。\n"
            f"Run：`{ctx.run_id}`  ·  团队：`{ctx.team_name}`"
        )
        submit = (
            "## 结果提交\n"
            "- 提交简要完成摘要（含交付物链接/引用）。\n"
            "- 若无法完成，请提交失败原因，不要一直挂起。"
        )
        notes_h = "## 远程执行备注"
        goal_h = "## Flow 目标"
        inputs_h = "## 运行参数"
        task_h = f"## 任务 #{ctx.task.id}：{ctx.task.subject}"
        none_desc = "_(无额外说明)_"
        none_goal = "_(Flow 无描述)_"
        none_inputs = "  _(无)_"
    else:
        intro = (
            "## ClawsomeFlow External Task\n"
            "Complete the work below, then submit the result via the Run "
            "detail card or the callback API.\n"
            f"Run ID: `{ctx.run_id}`  ·  Team: `{ctx.team_name}`"
        )
        submit = (
            "## Result Submission\n"
            "- Provide a concise completion summary (links/refs when useful).\n"
            "- If blocked, submit a failure with the reason — do not leave it open."
        )
        notes_h = "## Notes for remote executors"
        goal_h = "## Flow Goal"
        inputs_h = "## Runtime inputs"
        task_h = f"## Task #{ctx.task.id}: {ctx.task.subject}"
        none_desc = "_(no additional description)_"
        none_goal = "_(Flow has no description)_"
        none_inputs = "  _(none)_"

    desc = ctx.flow_description.strip() or none_goal
    inputs_lines = [
        f"  - **{k}**: `{v}`" for k, v in _public_flow_inputs(ctx.flow_inputs).items()
    ]
    inputs_body = "\n".join(inputs_lines) if inputs_lines else none_inputs
    task_desc = ctx.task.description.strip() or none_desc
    blocks = [
        intro,
        f"{goal_h}\n{desc}\n{inputs_h}\n{inputs_body}",
        _upstream_outputs_block_external(ctx),
        f"{task_h}\n{task_desc}",
        submit,
    ]
    ext = getattr(ctx.agent, "external", None)
    if ext is not None and ext.channel == ExternalChannel.webhook:
        blocks.append(f"{notes_h}\n{WEBHOOK_REMOTE_NOTES}")
    remote_block = _remote_param_report_block_external(ctx)
    if remote_block:
        blocks.append(remote_block)
    return "\n\n".join(b for b in blocks if b).strip() + "\n"


def build_external_notify_brief(
    package: dict[str, object], *, lang: str | None = None,
) -> str:
    """Compact, task-focused webhook body (zh/en) — no nested sheets.

    Prefer structured package fields over the full task sheet so Feishu/
    Telegram messages stay short and do not repeat origin-delegate wrappers.
    """
    resolved = "zh" if (lang or "").strip().lower() == "zh" else "en"
    subject = str(package.get("subject") or "").strip()
    task_id = str(package.get("taskId") or "").strip()
    description = str(package.get("description") or "").strip()
    # Strip accidentally nested origin sheets from description.
    for marker in (
        "## ClawsomeFlow External Task",
        "## ClawsomeFlow 外部任务",
        "## Run-time User Parameters",
    ):
        if marker in description:
            description = description.split(marker, 1)[0].strip()
    requirement = str(package.get("outputRequirement") or "").strip()
    flow_goal = str(package.get("flowDescription") or "").strip()
    for marker in (
        "## ClawsomeFlow External Task",
        "## ClawsomeFlow 外部任务",
        "## Run-time User Parameters",
    ):
        if marker in flow_goal:
            flow_goal = flow_goal.split(marker, 1)[0].strip()

    upstream = package.get("upstreamOutputs")
    upstream_lines: list[str] = []
    if isinstance(upstream, list):
        for item in upstream:
            if not isinstance(item, dict):
                continue
            u_subj = str(item.get("subject") or item.get("taskId") or "").strip()
            u_sum = str(item.get("summary") or "").strip()
            if u_subj or u_sum:
                upstream_lines.append(
                    f"- {u_subj}: {u_sum}" if u_sum else f"- {u_subj}"
                )

    if resolved == "zh":
        lines = []
        if task_id or subject:
            lines.append(f"**任务** {task_id}{' · ' if task_id and subject else ''}{subject}".strip())
        if flow_goal:
            lines.extend(["", f"**目标** {flow_goal}"])
        if description:
            lines.extend(["", description])
        if requirement:
            lines.extend(["", f"**输出要求** {requirement}"])
        if upstream_lines:
            lines.extend(["", "**上游产出**", *upstream_lines])
        lines.extend(["", "完成后请在 Run 详情页提交结果。"])
        return "\n".join(lines).strip()

    lines = []
    if task_id or subject:
        lines.append(
            f"**Task** {task_id}{' · ' if task_id and subject else ''}{subject}".strip()
        )
    if flow_goal:
        lines.extend(["", f"**Goal** {flow_goal}"])
    if description:
        lines.extend(["", description])
    if requirement:
        lines.extend(["", f"**Output** {requirement}"])
    if upstream_lines:
        lines.extend(["", "**Upstream**", *upstream_lines])
    lines.extend(["", "Submit the result on the Run detail page when done."])
    return "\n".join(lines).strip()


def build_delegate_runtime_prompt(package: dict[str, object]) -> str:
    """Slim origin brief for a remote_csflow peer (not the full task sheet).

    Injecting the full external sheet as ``runtimePrompt`` nested it into every
    peer task description and produced duplicated webhook noise.
    """
    subject = str(package.get("subject") or "").strip()
    description = str(package.get("description") or "").strip()
    requirement = str(package.get("outputRequirement") or "").strip()
    parts: list[str] = []
    if subject:
        parts.append(subject)
    if description:
        parts.append(description)
    if requirement:
        parts.append(f"Output requirement: {requirement}")
    return "\n\n".join(parts).strip()


def _remote_param_report_block_external(ctx: DispatchContext) -> str:
    """Ask an EXTERNAL executor to append per-downstream params to its result.

    External executors report a single free-text completion summary (via the
    receipt API / WebUI card), so unlike a local worker they cannot send
    separate inbox messages. Instead we ask them to append ONE section PER
    downstream target into that summary; the scheduler parses each
    ``csflow-remote-params:`` header out of the completion text.
    """
    targets = ctx.remote_param_targets
    if not targets:
        return ""
    task_id = ctx.task.id
    lines = [
        "## Remote Parameter Report (include in your result)",
        (
            f"{len(targets)} downstream remote ClawsomeFlow node(s) need "
            "parameter values. At the END of your completion summary, append "
            "ONE block per target below (header line + JSON object). Use an "
            "empty string for any field you cannot provide; do not invent "
            "values or include local absolute paths."
        ),
        "",
    ]
    for i, target in enumerate(targets, start=1):
        schema = ", ".join(
            f'"{f}": "<value or empty>"' for f in target.param_fields
        )
        field_list = ", ".join(f"`{f}`" for f in target.param_fields)
        goal = (target.flow_description or "").strip() or "(no overall goal provided)"
        header = f"{REMOTE_PARAMS_HEADER}: {task_id} {target.downstream_task_id}"
        lines.append(f"### Target {i} — {_format_remote_param_target_heading(target)}")
        lines.append(f"- Overall goal: {goal}")
        lines.append(f"- Parameter fields: {field_list}")
        lines.append(f"- Example: `{header}` `{{{schema}}}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_external_task_package(ctx: DispatchContext) -> dict[str, object]:
    """Structured fields for the outbound dispatch package + dispatch event.

    ``clawteamTaskId`` / ``leaderAgentId`` are recorded so the receipt path
    (services/external_tasks.complete_external_task) can push the result into
    ClawTeam without needing a live controller lookup.

    ``description`` / ``outputRequirement`` are the two UI fields split back
    out of the canonical merged description, so an integrated system gets the
    task briefing and the expected deliverable shape as separate fields.

    Upstream entries intentionally omit worktree/branch — external nodes
    never share a local git workspace with the origin scheduler.
    """
    from app.models import split_description

    body, requirement = split_description(ctx.task.description)
    return {
        "subject": ctx.task.subject,
        "description": body,
        "outputRequirement": requirement,
        "flowDescription": ctx.flow_description,
        "runtimeInputs": _public_flow_inputs(ctx.flow_inputs),
        "leaderAgentId": ctx.leader_agent_id,
        "clawteamTaskId": ctx.clawteam_task_id,
        "timeoutSeconds": ctx.task.timeout_seconds,
        "upstreamOutputs": [
            {
                "taskId": u.task_id,
                "subject": u.subject,
                "fromAgent": u.from_agent,
                "summary": u.summary,
            }
            for u in ctx.upstream_outputs
        ],
    }


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
    "EMPTY_PARAM_PLACEHOLDER",
    "REMOTE_ORIGIN_NOTE",
    "REMOTE_PARAMS_HEADER",
    "RemoteParamTarget",
    "UpstreamOutput",
    "WEBHOOK_REMOTE_NOTES",
    "WorkerReport",
    "build_external_task_package",
    "build_delegate_runtime_prompt",
    "build_external_notify_brief",
    "build_external_task_text",
    "build_leader_dispatch",
    "build_openclaw_self_merge",
    "build_worker_dispatch",
]
