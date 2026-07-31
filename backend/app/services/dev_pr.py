"""Developer-mode PR pipeline helpers (push + create / discovery).

Shared by the scheduler's auto-PR hook (``dev_submit_pr`` tasks) and the Run
detail API (run-diff PR entries / pending-PR module). Everything here is
**best-effort**: a PR failure must never block a run, so helpers return
result tuples / empty lists instead of raising on subprocess errors.

PR creation is **git-native first** — no third-party tool required:

1. ``git push -u origin <branch>`` (plain git). Hosting platforms print a
   PR/MR link on push; an already-existing PR/MR URL is harvested from the
   push output.
2. ``git push -o merge_request.create -o merge_request.target=<base> …``
   (git push-options — creates a real MR on GitLab/Gitee; unsupported
   elsewhere, where the failure is simply tolerated).
3. Only as an optional extra, when the GitHub CLI (``gh``) happens to be
   installed, ``gh pr create`` is tried (covers GitHub auto-creation).
4. Last resort: the platform's pre-filled "create PR" page URL printed by
   the initial push (GitHub prints ``…/pull/new/<branch>``; GitLab prints
   ``…/merge_requests/new?…``). The entry then links to a one-click create
   page — the most git can do without platform tooling.

PR *discovery* (listing PRs opened out-of-band, e.g. by the agent itself)
has no git-native equivalent; it uses ``gh pr list`` ONLY when ``gh`` is
installed and silently degrades to "recorded PRs only" otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from typing import Any

PR_PUSH_TIMEOUT_SEC = 300.0
PR_CREATE_TIMEOUT_SEC = 120.0
PR_LIST_TIMEOUT_SEC = 60.0

#: An existing / created PR or MR (GitHub ``/pull/<n>``, GitLab/Gitee
#: ``/merge_requests/<n>``).
_PR_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+/(?:pull|merge_requests)/\d+")
#: A pre-filled "create PR/MR" page (GitHub ``/pull/new/<branch>``, GitLab
#: ``/merge_requests/new?…``).
_PR_CREATE_PAGE_RE = re.compile(
    r"https?://[^\s)\]\"'<>]+/(?:pull/new|merge_requests/new)[^\s)\]\"'<>]*",
)


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


def _gh_available() -> bool:
    """Whether the (optional) GitHub CLI is installed. Never required."""
    return shutil.which("gh") is not None


def _extract_pr_url(text: str) -> str:
    """First existing/created PR-or-MR URL in *text* (``""`` when absent)."""
    m = _PR_URL_RE.search(text or "")
    return m.group(0) if m else ""


def _extract_create_page_url(text: str) -> str:
    """First pre-filled create-PR/MR page URL in *text* (``""`` when absent)."""
    m = _PR_CREATE_PAGE_RE.search(text or "")
    return m.group(0) if m else ""


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
    """Push *branch* and open a PR/MR against *target_branch* (git-native first).

    Returns ``(ok, pr_url, failed_step, detail)``. ``failed_step`` is
    ``"push"`` or ``"pr_create"`` on failure (``detail`` carries the trimmed
    stderr/stdout); empty on success. ``pr_url`` is a real PR/MR URL when one
    could be created or found, otherwise the platform's pre-filled create-PR
    page URL (see module docstring, step 4).
    """
    # Step 1 — plain git push.
    rc, out, err = await run_pr_command(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree, timeout_sec=PR_PUSH_TIMEOUT_SEC,
    )
    if rc != 0:
        return False, "", "push", (err or out).strip()[:1000]
    push_text = f"{out}\n{err}"
    url = _extract_pr_url(push_text)
    if url:
        return True, url, "", ""

    # Step 2 — git-native MR creation via push options (GitLab/Gitee). On
    # platforms without push-option support this errors — tolerated.
    rc2, out2, err2 = await run_pr_command(
        ["git", "push",
         "-o", "merge_request.create",
         "-o", f"merge_request.target={target_branch}",
         "-o", f"merge_request.title={title}",
         "origin", branch],
        cwd=worktree, timeout_sec=PR_CREATE_TIMEOUT_SEC,
    )
    if rc2 == 0:
        url = _extract_pr_url(f"{out2}\n{err2}")
        if url:
            return True, url, "", ""

    # Step 3 — optional GitHub CLI fallback (only when installed).
    gh_detail = ""
    if _gh_available():
        rc3, out3, err3 = await run_pr_command(
            ["gh", "pr", "create", "--base", target_branch, "--head", branch,
             "--title", title, "--body", body],
            cwd=worktree, timeout_sec=PR_CREATE_TIMEOUT_SEC,
        )
        gh_text = f"{out3}\n{err3}"
        url = _extract_pr_url(gh_text)  # covers "already exists: <url>" too
        if url:
            return True, url, "", ""
        if rc3 != 0:
            gh_detail = (err3 or out3).strip()[:1000]

    # Step 4 — last resort: the pre-filled create-PR page from the push output.
    create_url = _extract_create_page_url(push_text) or _extract_create_page_url(
        f"{out2}\n{err2}",
    )
    if create_url:
        return True, create_url, "", ""
    return (
        False, "", "pr_create",
        gh_detail or "no PR URL obtained from push output (no gh, no platform link)",
    )


async def list_branch_prs(*, cwd: str, branch: str) -> list[dict[str, Any]]:
    """Discover PRs (any state, any author) opened from *branch*.

    Uses ``gh pr list --head <branch> --state all`` **only when ``gh`` is
    installed** — there is no git-native way to enumerate PRs. Returns ``[]``
    on any failure (no ``gh``, no remote, not a GitHub repo, timeout …) —
    discovery is a pure enrichment over the recorded PR list and must never
    break the run-diff endpoint.
    """
    if not branch or not _gh_available():
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
