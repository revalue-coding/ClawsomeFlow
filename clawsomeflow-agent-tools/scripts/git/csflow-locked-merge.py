#!/usr/bin/env python3
"""csflow-locked-merge — merge a branch into a baseline branch under the csflow repo lock.

Usage:
    python3 csflow-locked-merge.py <repo> <src_branch> <dst_branch> <merge_message>

Why this exists: agents that must merge their worktree branch into a shared
baseline branch run ``git checkout`` + ``git merge`` on the MAIN repo. Those
mutate the repo's single working tree / HEAD / ``.git`` metadata, exactly like
the ClawsomeFlow scheduler's own spawn/merge. To stay mutually exclusive with
the scheduler (and with other agents), every such merge MUST hold the SAME
on-disk lock the scheduler uses:

    <CSFLOW_HOME>/.locks/clawteam_main_repo/<sha256(expanded_repo)[:16]>.lock

Performance / safety split (mirrors ``ClawTeamCli.workspace_merge``):
* **Before lock:** ``git fetch origin <dst_branch>`` — network only, no HEAD change.
* **Inside lock:** ``git checkout <dst>`` → ``git merge --ff-only origin/<dst>``
  (best-effort, one call) → ``git merge --no-ff <src>``.

This duplicates ``app.repo_merge_lock.main_repo_lock_path`` /
``app.paths.clawsomeflow_home_path`` by design: the lock-file path MUST stay
byte-for-byte identical to the Python side, or the locks would not be the same
file. Keep the two in sync.

Exit codes:
    0   success (merge landed, or already up to date)
    2   usage error
    10  lock acquisition timed out
    20  ``git checkout <dst>`` failed
    25  a merge is already in progress (MERGE_HEAD exists)
    30  merge conflict — resolve, commit, then re-run
    1   any other failure
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

LOCK_WAIT_TIMEOUT_SECONDS = 8 * 3600
_LOCK_POLL_INTERVAL_SECONDS = 0.5
_LOCKS_SUBDIR = ".locks/clawteam_main_repo"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LOCK_TIMEOUT = 10
EXIT_CHECKOUT_FAILED = 20
EXIT_MERGE_IN_PROGRESS = 25
EXIT_CONFLICT = 30
EXIT_OTHER = 1


def _emit(line: str) -> None:
    print(line, flush=True)


def _clawsomeflow_home() -> Path:
    """Mirror ``app.paths.clawsomeflow_home_path`` (CSFLOW_HOME or ~/.clawsomeflow)."""
    raw = os.environ.get("CSFLOW_HOME") or "~/.clawsomeflow"
    return Path(raw).expanduser()


def _lock_path(expanded_repo: str) -> Path:
    """Mirror ``app.repo_merge_lock.main_repo_lock_path`` exactly."""
    digest = hashlib.sha256(expanded_repo.encode()).hexdigest()[:16]
    return _clawsomeflow_home() / _LOCKS_SUBDIR / f"{digest}.lock"


def _git(repo: str, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _fetch_origin_branch(repo: str, branch: str) -> None:
    """Branch-scoped fetch before lock; does not change HEAD or the worktree."""
    _git(repo, "fetch", "origin", branch)


def _fast_forward_origin_branch(repo: str, branch: str) -> None:
    """Best-effort single-call fast-forward to ``origin/<branch>`` (inside lock)."""
    rc, _, _ = _git(repo, "merge", "--ff-only", f"origin/{branch}")
    _emit(
        "fast-forward: ok"
        if rc == 0
        else "fast-forward: skipped (no remote ref or not fast-forward)"
    )


def _do_merge(repo: str, src: str, dst: str, msg: str) -> int:
    # Refuse if the repo is mid-merge: a re-run after an unresolved conflict
    # would otherwise start a second merge on top. Guide the agent to finish.
    rc, _, _ = _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if rc == 0:
        _emit("csflow-locked-merge: result=merge_in_progress")
        _emit(
            f"next: a merge is already in progress in {repo} — resolve conflicts, "
            "`git add -A && git commit`, then re-run this command"
        )
        return EXIT_MERGE_IN_PROGRESS

    rc, so, se = _git(repo, "checkout", dst)
    if rc != 0:
        _emit("csflow-locked-merge: result=checkout_failed")
        _emit(f"checkout: failed -> {(se or so).strip()[:300]}")
        _emit(f"next: ensure branch '{dst}' exists in {repo} and the tree is clean")
        return EXIT_CHECKOUT_FAILED
    _emit(f"checkout: ok ({dst})")

    _fast_forward_origin_branch(repo, dst)

    rc, so, se = _git(repo, "merge", "--no-ff", src, "-m", msg)
    if rc == 0:
        rc2, sha, _ = _git(repo, "rev-parse", "HEAD")
        head = sha.strip()[:12] if rc2 == 0 else "?"
        _emit(f"merge: ok (head={head})")
        _emit("csflow-locked-merge: result=success")
        return EXIT_OK

    rcc, conf, _ = _git(repo, "diff", "--name-only", "--diff-filter=U")
    files = [f for f in conf.splitlines() if f.strip()] if rcc == 0 else []
    if files:
        shown = ", ".join(files[:50]) + (" …" if len(files) > 50 else "")
        _emit("csflow-locked-merge: result=conflict")
        _emit(f"conflict files: {shown}")
        _emit(
            f"next: in {repo} resolve the conflicts, `git add -A && git commit`, "
            "then re-run this command to finalize the merge"
        )
        return EXIT_CONFLICT

    _emit("csflow-locked-merge: result=merge_failed")
    _emit(f"merge: failed -> {(se or so).strip()[:300]}")
    _emit(f"next: inspect {repo}, fix the cause, then re-run this command")
    return EXIT_OTHER


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        _emit("csflow-locked-merge: result=usage_error")
        _emit(
            "usage: csflow-locked-merge.py <repo> <src_branch> <dst_branch> <merge_message>"
        )
        return EXIT_USAGE

    repo = str(Path(argv[1]).expanduser())
    src, dst, msg = argv[2], argv[3], argv[4]

    if not (Path(repo) / ".git").exists():
        _emit("csflow-locked-merge: result=bad_repo")
        _emit(f"next: '{repo}' is not a git repository (no .git)")
        return EXIT_OTHER

    lock_path = _lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _emit(f"csflow-locked-merge: repo={repo} merge={src} -> {dst}")

    _fetch_origin_branch(repo, dst)

    started = time.monotonic()
    deadline = started + LOCK_WAIT_TIMEOUT_SECONDS
    with lock_path.open("a+") as fh:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    _emit("csflow-locked-merge: result=lock_timeout")
                    _emit(
                        f"next: another process held the repo lock > "
                        f"{LOCK_WAIT_TIMEOUT_SECONDS}s; retry later"
                    )
                    return EXIT_LOCK_TIMEOUT
                time.sleep(_LOCK_POLL_INTERVAL_SECONDS)
        _emit(f"lock: acquired ({time.monotonic() - started:.1f}s)")
        try:
            return _do_merge(repo, src, dst, msg)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
