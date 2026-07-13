"""Single source of truth for scheduler-internal ``FlowRun.inputs`` keys.

These markers ride inside ``run.inputs`` (survives restarts + old-version
round-trips because it is plain JSON) and were historically re-declared in
``controller`` / ``finalize`` / ``engine`` / ``api.runs``. Any drift between
those copies silently breaks the review/complaint hand-off, so every module
must import the constants from here.

* :data:`POST_COMPLAINT_STATUS_KEY` — the terminal :class:`RunStatus` value a
  run should adopt once the user complaint phase finishes.
* :data:`POST_REVIEW_TERMINAL_STATUS_KEY` — the terminal status recorded when
  a run enters ``awaiting_user_review`` after an abort / task failure.
* :data:`PRESERVE_WORKTREE_AGENT_IDS_KEY` — agent ids whose worktrees must
  survive terminal team cleanup (merge-conflict follow-up).
* :data:`REVERTED_MERGE_AGENT_IDS_KEY` — agent ids whose run-diff merges the user
  reverted ("撤销合入"); excluded from the post-run Run-diff module.
* :data:`DEV_PENDING_PR_AGENT_IDS_KEY` — developer-mode runs only: non-OpenClaw
  agent ids that owned at least one no-merge (``devAutoMerge=false``) task.
  Their worktrees survive terminal cleanup so the user can inspect / one-click
  PR / discard them from the Run detail "PR" module. Written at finalize time,
  so it doubles as the "this run executed in developer mode" record (a Flow
  later switched to dev mode never grows this marker retroactively).
* :data:`UNATTENDED_KEY` — set at trigger time (value ``"true"``) to mark a run
  as **unattended**: no human in the loop, so the scheduler skips the merge
  review, complaint and human-checkpoint phases and drives straight to a
  terminal status (exactly like a timed-schedule run). Used by MCP-triggered
  runs and ``csflow runs start --unattended``. This is orthogonal to
  ``FlowRun.is_scheduled`` (which stays reserved for *timed* triggers set only
  by ``services/run_schedules.py``); behavioural decisions consult the union of
  the two via :func:`run_is_unattended`, so a run's execution *mode*
  (normal / easy / dev) is untouched — an unattended dev run still runs as dev.

NOTE: the string values are a persisted on-disk contract (existing user DBs
contain them) — never rename the values, only the Python symbols.
"""

from __future__ import annotations

from typing import Any

POST_COMPLAINT_STATUS_KEY = "_csflow_post_complaint_final_status"
POST_REVIEW_TERMINAL_STATUS_KEY = "_csflow_post_review_terminal_status"
PRESERVE_WORKTREE_AGENT_IDS_KEY = "_csflow_preserve_worktree_agent_ids"
REVERTED_MERGE_AGENT_IDS_KEY = "_csflow_reverted_merge_agent_ids"
DEV_PENDING_PR_AGENT_IDS_KEY = "_csflow_dev_pending_pr_agent_ids"
UNATTENDED_KEY = "_csflow_unattended"


def run_is_unattended(run: Any) -> bool:
    """Whether *run* executes without a human in the loop.

    True for both timed-schedule runs (``FlowRun.is_scheduled``) and runs
    explicitly flagged unattended at trigger time (the :data:`UNATTENDED_KEY`
    marker in ``run.inputs`` — MCP / ``--unattended``). Every *behavioural*
    is-this-run-unattended decision in the scheduler (finalize phase selection,
    in-task self-merge, human-checkpoint bypass) must go through this predicate
    rather than reading ``is_scheduled`` directly.

    Duck-typed on purpose (no ``FlowRun`` import) to stay import-cycle-free.
    """
    if bool(getattr(run, "is_scheduled", False)):
        return True
    inputs = getattr(run, "inputs", None) or {}
    return str(inputs.get(UNATTENDED_KEY, "")).strip().lower() == "true"


__all__ = [
    "DEV_PENDING_PR_AGENT_IDS_KEY",
    "POST_COMPLAINT_STATUS_KEY",
    "POST_REVIEW_TERMINAL_STATUS_KEY",
    "PRESERVE_WORKTREE_AGENT_IDS_KEY",
    "REVERTED_MERGE_AGENT_IDS_KEY",
    "UNATTENDED_KEY",
    "run_is_unattended",
]
