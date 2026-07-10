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

NOTE: the string values are a persisted on-disk contract (existing user DBs
contain them) — never rename the values, only the Python symbols.
"""

from __future__ import annotations

POST_COMPLAINT_STATUS_KEY = "_csflow_post_complaint_final_status"
POST_REVIEW_TERMINAL_STATUS_KEY = "_csflow_post_review_terminal_status"
PRESERVE_WORKTREE_AGENT_IDS_KEY = "_csflow_preserve_worktree_agent_ids"
REVERTED_MERGE_AGENT_IDS_KEY = "_csflow_reverted_merge_agent_ids"

__all__ = [
    "POST_COMPLAINT_STATUS_KEY",
    "POST_REVIEW_TERMINAL_STATUS_KEY",
    "PRESERVE_WORKTREE_AGENT_IDS_KEY",
    "REVERTED_MERGE_AGENT_IDS_KEY",
]
