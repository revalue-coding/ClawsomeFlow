"""Flow execution-mode resolution (省心 / 开发者 modes).

A Flow carries at most one of two mutually-exclusive *modes*, persisted in the
spec's ``variables`` dict (not a typed field, so it round-trips through an
un-upgraded backend exactly like ``csflow.easy_mode`` always has):

* ``csflow.easy_mode`` = ``"true"``  → **easy mode** ("省心模式"): every task
  self-merges its worktree in-task; the user merge-review phase is skipped.
* ``csflow.dev_mode``  = ``"true"``  → **developer mode** ("开发者模式"): each
  task decides for itself whether to self-merge (``FlowTask.dev_auto_merge``);
  the user merge-review phase is skipped.

These helpers are the **single source of truth** for two derived questions used
across the scheduler:

1. ``flow_mode`` — which mode (if any) is active.
2. ``task_self_merges`` — does *this* task self-merge into the baseline branch
   in-task? This drives the dispatch-prompt self-merge block
   (``scheduler/prompts.py``) and the checkpoint-rerun instruction.

The functions are pure (dict / object in, value out) with no I/O and no
scheduler imports, so any layer can call them without circular-import risk.

Mode is independent from *whether* the run was triggered by a timed schedule
(``FlowRun.is_scheduled``). That flag now means literally "timed trigger" and
only influences self-merge for the **normal** (no-mode) case, where a scheduled
run self-merges every task but a manual run defers merges to review/complaint.
The complaint-phase decision lives in ``scheduler/finalize.py`` (complaint runs
for every manual run, never for a scheduled run).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # avoid runtime import cycle (models imports nothing from here)
    from app.models import FlowAgent, FlowTask


# Spec-variable keys (mirrored on the frontend in ``lib/flowRuntime.ts``).
FLOW_EASY_MODE_KEY = "csflow.easy_mode"
FLOW_DEV_MODE_KEY = "csflow.dev_mode"

FlowMode = Literal["normal", "easy", "dev"]


def _is_true(value: object) -> bool:
    return str(value if value is not None else "").strip().lower() == "true"


def flow_mode(variables: Mapping[str, object] | None) -> FlowMode:
    """Resolve the active Flow mode from the spec's ``variables`` dict.

    Developer mode wins over easy mode if both flags are somehow set (the UI
    enforces mutual exclusion, but the backend stays deterministic regardless).
    """
    vars_ = variables or {}
    if _is_true(vars_.get(FLOW_DEV_MODE_KEY)):
        return "dev"
    if _is_true(vars_.get(FLOW_EASY_MODE_KEY)):
        return "easy"
    return "normal"


def task_self_merges(
    *,
    mode: FlowMode,
    run_is_scheduled: bool,
    task: FlowTask,
    agent: FlowAgent,
) -> bool:
    """Return True when *task* must self-merge its worktree branch in-task.

    * **dev** — OpenClaw is always forced to self-merge; every other agent
      honours ``task.dev_auto_merge`` (default True). A no-merge task never
      reaches the baseline branch; its worktree is discarded by terminal team
      cleanup at run end.
    * **easy** — every task self-merges.
    * **normal** — only scheduled (unattended) runs self-merge in-task; manual
      runs defer merges to the review / complaint phases.
    """
    # Imported lazily / via TYPE_CHECKING; compare by attribute to avoid a hard
    # import of the AgentKind enum here.
    is_openclaw = getattr(agent.kind, "value", agent.kind) == "openclaw"
    if mode == "dev":
        if is_openclaw:
            return True
        return bool(getattr(task, "dev_auto_merge", True))
    if mode == "easy":
        return True
    return bool(run_is_scheduled)


def merge_reference_enabled(*, mode: FlowMode) -> bool:
    """Whether to inject the generic merge + repo-lock reference into a dispatch.

    **dev / easy modes only** — every task (incl. the leader) gets it, because the
    generic reference is the developer-mode collaboration primitive (a task
    description may direct cross-worktree merges / PRs).

    **Normal mode never gets it**, including scheduled-normal auto-merge tasks:
    those only ever merge *their own* branch, which the task's existing self-merge
    instruction already covers — they do not need the generic cross-worktree
    how-to, and omitting it keeps normal-mode prompts lean.
    """
    return mode in ("dev", "easy")


__all__ = [
    "FLOW_DEV_MODE_KEY",
    "FLOW_EASY_MODE_KEY",
    "FlowMode",
    "flow_mode",
    "merge_reference_enabled",
    "task_self_merges",
]
