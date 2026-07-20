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
* :data:`FAILED_AUTO_MERGE_AGENT_IDS_KEY` — easy / developer manual runs: agent
  ids that were supposed to self-merge in-task but did not land on the baseline
  while the worktree still has mergeable content. Worktrees survive terminal
  cleanup for the Run detail "failed auto-merge" module (view diff / merge /
  discard). Written at finalize when entering ``awaiting_user_complaint``.
* :data:`UNATTENDED_KEY` — set at trigger time (value ``"true"``) to mark a run
  as **unattended**: no human in the loop, so the scheduler skips the merge
  review, complaint and human-checkpoint phases and drives straight to a
  terminal status (exactly like a timed-schedule run). Used by MCP-triggered
  runs and ``csflow runs start --unattended``. This is orthogonal to
  ``FlowRun.is_scheduled`` (which stays reserved for *timed* triggers set only
  by ``services/run_schedules.py``); behavioural decisions consult the union of
  the two via :func:`run_is_unattended`, so a run's execution *mode*
  (normal / easy / dev) is untouched — an unattended dev run still runs as dev.
* :data:`EXTERNAL_CALLBACK_KEY` — set by ``POST /api/external/delegate`` on a
  run this instance executes on behalf of a remote ClawsomeFlow. Value is a
  JSON string ``{"url": ..., "token": ...}``; when the run reaches a terminal
  status the storage ``run_update`` hook POSTs the leader report back to that
  URL (see ``app.services.external_tasks.prepare_delegate_callback``).
* :data:`EXTERNAL_CALLBACK_SENT_KEY` — ISO timestamp in-flight/success dedupe
  marker for the delegate callback (stamped when prepare queues the POST;
  cleared on exhausted failure so a later ``run_update`` / upgrade can retry).

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
FAILED_AUTO_MERGE_AGENT_IDS_KEY = "_csflow_failed_auto_merge_agent_ids"
UNATTENDED_KEY = "_csflow_unattended"
EXTERNAL_CALLBACK_KEY = "_csflow_external_callback"
EXTERNAL_CALLBACK_SENT_KEY = "_csflow_external_callback_sent_at"


def coalesce_reverted_merge_markers(run: Any, storage: Any) -> None:
    """Union DB ``REVERTED_MERGE_AGENT_IDS_KEY`` into *run.inputs* in place.

    The live :class:`RunController` may hold a stale ``run.inputs`` while the
    API writes 撤销合入 markers (common on abort: user reverts, then finalize
    persists the in-memory blob and would otherwise wipe the marker). Call
    before any ``run_update`` that might overwrite inputs from a long-lived
    controller object. Duck-typed to stay import-cycle-free.
    """
    run_id = getattr(run, "id", None)
    if not run_id or storage is None or not hasattr(storage, "run_get"):
        return
    try:
        db_run = storage.run_get(run_id)
    except Exception:
        return
    if db_run is None:
        return
    local = dict(getattr(run, "inputs", None) or {})
    db_inputs = getattr(db_run, "inputs", None) or {}

    def _as_set(raw: Any) -> set[str]:
        if not isinstance(raw, list):
            return set()
        return {str(a).strip() for a in raw if str(a or "").strip()}

    merged = _as_set(local.get(REVERTED_MERGE_AGENT_IDS_KEY)) | _as_set(
        db_inputs.get(REVERTED_MERGE_AGENT_IDS_KEY),
    )
    if not merged:
        return
    local[REVERTED_MERGE_AGENT_IDS_KEY] = sorted(merged)
    run.inputs = local


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


def read_failed_auto_merge_agent_ids(run: Any) -> set[str]:
    """Agent ids in the failed in-task auto-merge module (easy/dev manual runs)."""
    raw = (getattr(run, "inputs", None) or {}).get(FAILED_AUTO_MERGE_AGENT_IDS_KEY)
    if not isinstance(raw, list):
        return set()
    return {str(a).strip() for a in raw if str(a or "").strip()}


__all__ = [
    "DEV_PENDING_PR_AGENT_IDS_KEY",
    "FAILED_AUTO_MERGE_AGENT_IDS_KEY",
    "EXTERNAL_CALLBACK_KEY",
    "EXTERNAL_CALLBACK_SENT_KEY",
    "POST_COMPLAINT_STATUS_KEY",
    "POST_REVIEW_TERMINAL_STATUS_KEY",
    "PRESERVE_WORKTREE_AGENT_IDS_KEY",
    "REVERTED_MERGE_AGENT_IDS_KEY",
    "UNATTENDED_KEY",
    "coalesce_reverted_merge_markers",
    "read_failed_auto_merge_agent_ids",
    "run_is_unattended",
]
