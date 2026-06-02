"""OpenClaw post-task audit (plan §9.3 / DEV.md §9 layer 3).

Why we need it:

OpenClaw's ``openclaw.json`` workspace path **wins** over any subprocess
``cwd``; we mitigate that with two layers (plan §9.3):

1. Dispatch message hard-states the worktree path (handled by
   :mod:`app.scheduler.prompts`).
2. **This module** — *defensive audit after each OpenClaw task completes*:
   - if the agent's main repo (``OpenclawAgent.workspace_path``) has
     uncommitted changes, auto-commit them as a task-scoped checkpoint;
   - if the agent's worktree has *no* commit referencing this task, drop a
     synthetic checkpoint commit so the work isn't lost on cleanup.

Public API:

* :class:`AuditResult` — typed outcome.
* :func:`run_post_task_audit` — the audit entrypoint, called by
  :class:`RunController` whenever an OpenClaw owner's task transitions to
  ``completed``.

Notes:
* All git invocations go through ``asyncio.create_subprocess_exec`` so the
  audit doesn't block the controller loop.
* Failures are *swallowed* and reported via :class:`AuditResult` — the
  audit is best-effort defence, never a hard failure path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger, workspace_violation
from app.models import AgentKind, FlowAgent, FlowTask, RunEvent
from app.storage import StorageBackend

logger = get_logger("worktree.audit")


@dataclass
class AuditResult:
    """Returned by :func:`run_post_task_audit` so callers can log / surface details."""

    agent_id: str
    task_id: str
    main_dirty: bool = False
    main_dirty_files: str = ""
    main_stash_ref: str | None = None
    main_auto_commit_sha: str | None = None
    worktree_missing_commit: bool = False
    auto_checkpoint_sha: str | None = None
    error: str = ""
    skipped: str = ""

    def as_event_payload(self) -> dict[str, Any]:
        return {
            "main_dirty": self.main_dirty,
            "main_stash_ref": self.main_stash_ref,
            "main_auto_commit_sha": self.main_auto_commit_sha,
            "worktree_missing_commit": self.worktree_missing_commit,
            "auto_checkpoint_sha": self.auto_checkpoint_sha,
            "error": self.error or None,
        }


# ──────────────────────────────────────────────────────────────────────
# Public entrypoint
# ──────────────────────────────────────────────────────────────────────


async def run_post_task_audit(
    *,
    agent: FlowAgent,
    task: FlowTask,
    main_workspace: str,
    worktree_path: str | None,
    storage: StorageBackend | None = None,
    run_id: str | None = None,
) -> AuditResult:
    """Audit one OpenClaw task completion. Safe to call for any agent kind
    (returns immediately with ``skipped`` set when not applicable).
    """
    out = AuditResult(agent_id=agent.id, task_id=task.id)

    if agent.kind != AgentKind.openclaw:
        out.skipped = "not_openclaw"
        return out
    if not main_workspace:
        out.skipped = "no_main_workspace"
        return out

    main = Path(main_workspace).expanduser()
    if not (main / ".git").exists():
        out.skipped = "main_not_a_git_repo"
        return out

    # ── Check 1: main repo dirty? -----------------------------------
    rc, dirty, stderr = await _git(main, "status", "--porcelain")
    if rc != 0:
        out.error = f"git status failed: {stderr.strip()[:200]}"
        return out
    if dirty.strip():
        out.main_dirty = True
        out.main_dirty_files = dirty.strip()[:1000]
        # Policy: treat main-repo writes as a normal checkpoint path for now.
        # We auto-commit instead of auto-stash, so users can review linear
        # history directly on main.
        rc2, _so2, se2 = await _git(main, "add", "-A")
        if rc2 != 0:
            out.error = (out.error + f"; auto-add failed: {se2.strip()[:200]}").strip("; ")
            workspace_violation(
                agent_id=agent.id, task_id=task.id, dirty_files=out.main_dirty_files,
            )
        else:
            msg = f"[csflow] main-write checkpoint task {task.id}"
            rc3, _so3, se3 = await _git(
                main, "commit", "--allow-empty", "-m", msg,
            )
            if rc3 == 0 or "nothing to commit" in (se3 or "").lower():
                rc4, sha, se4 = await _git(main, "rev-parse", "HEAD")
                if rc4 == 0:
                    out.main_auto_commit_sha = sha.strip()
                else:
                    out.error = (
                        out.error + f"; rev-parse failed: {se4.strip()[:200]}"
                    ).strip("; ")
            else:
                out.error = (out.error + f"; auto-commit failed: {se3.strip()[:200]}").strip("; ")
                workspace_violation(
                    agent_id=agent.id, task_id=task.id, dirty_files=out.main_dirty_files,
                )

    # ── Check 2: worktree carries this task's commit? ---------------
    if worktree_path:
        wt = Path(worktree_path).expanduser()
        if (wt / ".git").exists() or wt.exists():
            # Look at the head commit message for "task <id>" prefix.
            rc3, last_msg, _ = await _git(wt, "log", "-1", "--format=%s")
            head_msg = last_msg.strip() if rc3 == 0 else ""
            if not _commit_msg_mentions_task(head_msg, task.id):
                # Maybe earlier commits do (task wrote multiple commits before
                # the head). Search the last 20 commits for any mention.
                rc4, log_text, _ = await _git(wt, "log", "-20", "--format=%H %s")
                found = False
                if rc4 == 0:
                    for line in log_text.splitlines():
                        _sha, _sp, msg = line.partition(" ")
                        if _commit_msg_mentions_task(msg, task.id):
                            found = True
                            break
                if not found:
                    out.worktree_missing_commit = True
                    # Auto checkpoint so the (uncommitted) working changes don't
                    # get dropped by clawteam workspace cleanup.
                    rc5, _so, se5 = await _git(wt, "add", "-A")
                    if rc5 != 0:
                        out.error = (
                            f"{out.error}; auto-add failed: {se5.strip()[:200]}"
                        ).strip("; ")
                    else:
                        msg = f"[csflow] auto-checkpoint task {task.id}"
                        rc6, _so2, se6 = await _git(
                            wt, "commit", "--allow-empty", "-m", msg,
                        )
                        if rc6 == 0:
                            rc7, sha, _ = await _git(wt, "rev-parse", "HEAD")
                            if rc7 == 0:
                                out.auto_checkpoint_sha = sha.strip()
                        else:
                            out.error = (
                                f"{out.error}; auto-commit failed: {se6.strip()[:200]}"
                            ).strip("; ")

    # Persist as a RunEvent if storage is provided + something happened.
    if storage is not None and run_id is not None and (
        out.main_dirty or out.worktree_missing_commit
    ):
        event_type = "missing_commit"
        if out.main_dirty:
            event_type = (
                "main_repo_autocommit"
                if out.main_auto_commit_sha
                else "workspace_violation"
            )
        try:
            storage.event_append(RunEvent(
                run_id=run_id,
                type=event_type,
                agent_id=agent.id, task_id=task.id,
                payload=out.as_event_payload(),
            ))
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("audit_event_persist_failed", error=str(exc))

    logger.info(
        "post_task_audit",
        agent_id=agent.id, task_id=task.id,
        main_dirty=out.main_dirty,
        main_auto_commit_sha=out.main_auto_commit_sha,
        worktree_missing_commit=out.worktree_missing_commit,
        skipped=out.skipped or None,
        error=out.error or None,
    )
    return out


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _commit_msg_mentions_task(msg: str, task_id: str) -> bool:
    """Lenient check: ``task <id>`` anywhere in the message body."""
    if not msg:
        return False
    m = msg.lower()
    return f"task {task_id}".lower() in m


async def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    """Run ``git <args>`` in *cwd*; return (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


__all__ = [
    "AuditResult",
    "run_post_task_audit",
]
