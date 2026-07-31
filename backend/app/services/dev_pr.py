"""Developer-mode PR pipeline helpers (push + ``gh pr create`` / discovery).

Shared by the scheduler's auto-PR hook (``dev_submit_pr`` tasks) and the Run
detail API (run-diff PR entries / pending-PR module). Everything here is
**best-effort**: a PR failure must never block a run, so helpers return
result tuples / empty lists instead of raising on subprocess errors.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

PR_PUSH_TIMEOUT_SEC = 300.0
PR_CREATE_TIMEOUT_SEC = 120.0
PR_LIST_TIMEOUT_SEC = 60.0


async def run_pr_command(
    argv: list[str], *, cwd: str, timeout_sec: float,
) -> tuple[int, str, str]:
    """Run one PR-pipeline subprocess with a hard timeout + group kill."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        return 124, "", f"timed out after {int(timeout_sec)}s: {' '.join(argv)}"
    return (
        proc.returncode or 0,
        (stdout_b or b"").decode(errors="replace"),
        (stderr_b or b"").decode(errors="replace"),
    )


def pr_title_body(
    *, run_id: str, agent_id: str, branch: str, target_branch: str,
) -> tuple[str, str]:
    """Canonical PR title/body for ClawsomeFlow-opened PRs."""
    title = f"[ClawsomeFlow] {agent_id}: {branch} -> {target_branch}"
    body = (
        f"Automated PR opened by ClawsomeFlow developer mode.\n\n"
        f"- Run: {run_id}\n- Agent: {agent_id}\n- Branch: `{branch}` -> `{target_branch}`"
    )
    return title, body


async def push_and_create_pr(
    *,
    worktree: str,
    branch: str,
    target_branch: str,
    title: str,
    body: str,
) -> tuple[bool, str, str, str]:
    """``git push -u origin <branch>`` then ``gh pr create`` inside *worktree*.

    Returns ``(ok, pr_url, failed_step, detail)``. ``failed_step`` is
    ``"push"`` or ``"pr_create"`` on failure (``detail`` carries the trimmed
    stderr/stdout); empty on success.
    """
    rc, out, err = await run_pr_command(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree, timeout_sec=PR_PUSH_TIMEOUT_SEC,
    )
    if rc != 0:
        return False, "", "push", (err or out).strip()[:1000]
    rc, out, err = await run_pr_command(
        ["gh", "pr", "create", "--base", target_branch, "--head", branch,
         "--title", title, "--body", body],
        cwd=worktree, timeout_sec=PR_CREATE_TIMEOUT_SEC,
    )
    if rc != 0:
        return False, "", "pr_create", (err or out).strip()[:1000]
    pr_url = next(
        (ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith("http")),
        "",
    )
    return True, pr_url, "", ""


async def list_branch_prs(*, cwd: str, branch: str) -> list[dict[str, Any]]:
    """Discover PRs (any state, any author) opened from *branch*.

    Uses ``gh pr list --head <branch> --state all``. Returns ``[]`` on any
    failure (no ``gh``, no remote, not a GitHub repo, timeout …) — discovery
    is a pure enrichment over the recorded PR list and must never break the
    run-diff endpoint.
    """
    if not branch:
        return []
    rc, out, _err = await run_pr_command(
        ["gh", "pr", "list", "--head", branch, "--state", "all",
         "--json", "url,title,state,baseRefName", "--limit", "50"],
        cwd=cwd, timeout_sec=PR_LIST_TIMEOUT_SEC,
    )
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out_rows: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        out_rows.append({
            "url": url,
            "title": str(row.get("title") or ""),
            "state": str(row.get("state") or ""),
            "base": str(row.get("baseRefName") or ""),
        })
    return out_rows
