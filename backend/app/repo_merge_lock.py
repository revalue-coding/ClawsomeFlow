"""Cross-process main-repo merge lock (pairs with ``clawteam_main_repo``).

ClawsomeFlow's in-process :class:`LockManager` serialises merge/spawn within one
``csflow`` process. Agents run in separate processes and must use the **same**
on-disk ``flock`` file so baseline ``git checkout``/``git merge`` on a shared
``repo_root`` do not race each other.

Public helpers:
* :func:`main_repo_lock_path` — stable lock file path for a main repo.
* :func:`main_repo_lock_explanation` — prompt text for agent self-merge steps.
* :func:`merge_lock_reference` — generic (non-mandatory) merge + repo-lock how-to
  injected into dispatch prompts that may merge (developer-mode collaboration).
* :func:`build_generic_locked_merge_command` — runtime-generic locked merge
  one-liner; agent fills REPO/SRC/DST and the lock is computed for whichever repo
  is targeted (used by :func:`merge_lock_reference`).
* :func:`build_flocked_baseline_merge_command` — cross-platform locked merge
  one-liner for a *known* repo/branches (``flock`` Linux, ``mkdir`` macOS;
  used by the mandatory self-merge step).
* :func:`main_repo_file_lock` — sync context manager (used from async via wrapper).
* :func:`async_main_repo_file_lock` — async context manager for scheduler paths.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import shlex
import sys
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, Iterator

from app import logging_setup
from app.paths import clawsomeflow_home_path, openclaw_agent_tools_dir


def _expand_repo(path: str) -> str:
    if not path:
        return path
    return str(Path(path).expanduser())

if sys.platform == "win32":  # pragma: no cover
    import msvcrt
else:
    import fcntl

_LOCKS_SUBDIR = ".locks/clawteam_main_repo"

# Bound the cross-process repo lock so a stuck/crashed-mid-merge holder can never
# block a peer forever. 8h covers the longest plausible task runtime; past it we
# treat the wait as a genuine failure (raise / non-zero exit) rather than silently
# proceeding unlocked. Used by both the Python file lock and the agent shell
# self-merge commands so every path that touches a shared repo agrees on the cap.
LOCK_WAIT_TIMEOUT_SECONDS = 8 * 3600
_LOCK_POLL_INTERVAL_SECONDS = 0.5

# The agent-facing locked merge is a FIXED, version-controlled, unit-tested script
# (deployed unconditionally to the global agent-tools dir). Dispatch prompts only
# ask the agent to *invoke* it with plain argv — repo/src/dst/message — instead of
# transcribing a complex inline shell command. This minimises the LLM-relay error
# surface (the locking apparatus is no longer in what the agent must reproduce) and
# is uniformly cross-platform (the script flocks the SAME ``<hash>.lock`` the
# scheduler uses). See ``clawsomeflow-agent-tools/scripts/git/csflow-locked-merge.py``.
_MERGE_SCRIPT_RELPATH = ("scripts", "git", "csflow-locked-merge.py")

# Injected into agent dispatch prompts — one line, no fetch/lock internals.
_BASELINE_GIT_TOOL_ONLY_LINE = (
    "Do not manually `git pull`, `git checkout`, or `git merge` on the baseline repo — "
    "run ONLY the locked-merge command (it branch-fetches remotes before taking the lock "
    "and fast-forwards the target branch best-effort)."
)


def merge_script_path() -> Path:
    """Absolute path to the deployed ``csflow-locked-merge.py`` agent tool."""
    return openclaw_agent_tools_dir().joinpath(*_MERGE_SCRIPT_RELPATH)


def _locked_merge_command(*, repo: str, src: str, dst: str, message: str) -> str:
    """Build the ``python3 <script> <repo> <src> <dst> <message>`` invocation.

    Each argument is a single pre-quoted shell token, so the only thing the agent
    must reproduce verbatim is a path plus four flat args (one quoted message).
    """
    tool = shlex.quote(str(merge_script_path()))
    return f"python3 {tool} {repo} {src} {dst} {message}"


def main_repo_lock_path(repo: str) -> Path:
    """Return the ``flock`` lock file for *repo* (expanded, not symlink-resolved)."""
    expanded = _expand_repo(repo)
    digest = hashlib.sha256(expanded.encode()).hexdigest()[:16]
    return clawsomeflow_home_path() / _LOCKS_SUBDIR / f"{digest}.lock"


def main_repo_lock_explanation(repo_root: str) -> str:
    """Deprecated verbose lock text — kept for tests; prefer :func:`self_merge_instruction`."""
    lock = main_repo_lock_path(repo_root)
    repo = _expand_repo(repo_root)
    return (
        f"Use `flock -x {lock}` only while running checkout/merge on `{repo}`."
    )


def self_merge_instruction(
    *,
    repo_root: str,
    base_branch: str,
    feature_branch: str,
    merge_message: str,
) -> str:
    """Concise self-merge steps for agent prompts."""
    repo = _expand_repo(repo_root)
    cmd = build_flocked_baseline_merge_command(
        repo_root=repo_root,
        base_branch=base_branch,
        feature_branch=feature_branch,
        merge_message=merge_message,
    )
    return (
        f"Merge `{feature_branch}` → `{base_branch}` at `{repo}`. "
        "Making the merge succeed is your first priority. "
        f"**MUST** run locked — never merge without the lock: `{cmd}`. "
        f"{_BASELINE_GIT_TOOL_ONLY_LINE} "
        "On conflict (exit 30): resolve conflicts yourself, `git add -A && git commit`, "
        "then re-run the SAME command to finalize."
    )


def build_generic_locked_merge_command() -> str:
    """Runtime-generic locked merge: the agent fills ``REPO``/``SRC``/``DST`` and
    the deployed ``csflow-locked-merge`` tool computes the correct per-repo lock
    **at run time from REPO**, so the right lock is always held no matter which
    repo the developer's task targets.

    The tool's lock-file path matches :func:`main_repo_lock_path` exactly (sha256
    of the expanded repo path, first 16 hex chars under ``CSFLOW_HOME``), so it
    resolves to the SAME lock file the in-process scheduler ``flock``s — mutual
    exclusion holds across processes. ``REPO`` must be an absolute path.
    """
    return _locked_merge_command(
        repo="<abs-repo>", src="<source-branch>", dst="<dest-branch>",
        message='"csflow: merge <source-branch> into <dest-branch>"',
    )


def merge_lock_reference() -> str:
    """Concise, generic merge + repo-lock how-to injected into dispatch prompts in
    dev/easy modes only (see ``flow_modes.merge_reference_enabled``).

    Reference material, never a mandate: the obligation to merge lives only in the
    auto-merge task's completion checklist. It gives a **generic method** (fill
    ``REPO``/``SRC``/``DST``; the tool auto-locks whichever repo is targeted), so a
    task can direct an agent to merge any upstream agent's worktree branch into any
    developer-specified repo/branch — or open a PR.
    """
    cmd = build_generic_locked_merge_command()
    return (
        "Any checkout/merge (or other write to git metadata) on a shared repo **MUST** hold "
        "that repo's lock, or concurrent agents corrupt git metadata — the tool below takes it "
        "for you. **Reference only — merge or open a PR only if this is an auto-merge task or "
        "the task tells you to; if it IS required, making the merge succeed is your first "
        "priority.**\n"
        "- Merge `SRC` branch into `DST` branch of repo `REPO` — fill the three vars (`REPO` = "
        "absolute path) and **MUST** run it (it takes the correct lock for `REPO`):\n"
        f"  `{cmd}`\n"
        "- Take `REPO`/`SRC`/`DST` from the upstream/worker info above (each lists its repo, "
        "branch and base branch) per this task's instructions; `SRC` must be reachable from `REPO`.\n"
        f"- {_BASELINE_GIT_TOOL_ONLY_LINE}\n"
        "- On conflict (exit 30): resolve, `git add -A && git commit`, then re-run the same command.\n"
        "- PR instead: `git push` the branch, then open the PR via your platform CLI/UI.\n"
        "- A repo path is a merge target only — never copy deliverables there; keep them in your worktree."
    )


def build_flocked_baseline_merge_command(
    *,
    repo_root: str,
    base_branch: str,
    feature_branch: str,
    merge_message: str,
) -> str:
    """Build the agent-facing locked-merge invocation for a *known* repo/branches.

    Returns ``python3 <tool> <repo> <feature_branch> <base_branch> <message>`` —
    the agent just runs it. The actual locking (flock the SAME ``<hash>.lock`` the
    scheduler uses, 8h bounded wait, MERGE_HEAD guard, conflict reporting) lives in
    the fixed, unit-tested ``csflow-locked-merge.py`` tool, NOT in this string, so
    there is no complex inline shell for the LLM to mangle while relaying it — only
    a path plus four plain argv tokens. The tool is deployed unconditionally to the
    global agent-tools dir at init/upgrade, so every agent kind can reach it.
    """
    repo = _expand_repo(repo_root)
    return _locked_merge_command(
        repo=shlex.quote(repo),
        src=shlex.quote(feature_branch),
        dst=shlex.quote(base_branch),
        message=shlex.quote(merge_message),
    )


class FileLockAbortedError(Exception):
    """The caller's task was cancelled while the lock thread was still polling."""


def _acquire_file_lock(
    fh,
    *,
    timeout: float,
    poll: float,
    lock_path: Path,
    abort_check: Callable[[], bool] | None = None,
) -> None:
    """Block until the exclusive lock is held, or raise ``TimeoutError`` at *timeout*.

    Polls a non-blocking lock instead of a bare blocking ``flock``/``msvcrt`` so the
    wait is bounded — a stuck or crashed-mid-merge holder cannot block a spawn/merge
    forever. The cap is logged so a real deadlock is visible in the JSON logs.

    ``abort_check`` (optional) is polled between attempts so an async caller that
    was cancelled can stop the worker thread promptly instead of letting it poll
    for up to ``timeout`` (8h) in the background.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if sys.platform == "win32":  # pragma: no cover
                pos = fh.tell()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                fh.seek(pos)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            # EACCES/EAGAIN (POSIX) or EDEADLK/EACCES (msvcrt) → lock is held; retry.
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            if abort_check is not None and abort_check():
                raise FileLockAbortedError(
                    f"aborted while waiting for repo file lock {lock_path}"
                ) from exc
            if time.monotonic() >= deadline:
                logging_setup.lock_timeout(
                    key=f"file:{lock_path}", waited_ms=timeout * 1000
                )
                raise TimeoutError(
                    f"timed out after {timeout:.0f}s acquiring repo file lock {lock_path}"
                ) from exc
            time.sleep(poll)


@contextmanager
def main_repo_file_lock(
    repo: str,
    *,
    timeout: float = LOCK_WAIT_TIMEOUT_SECONDS,
    _abort_check: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Exclusive advisory lock on :func:`main_repo_lock_path`, bounded by *timeout*."""
    lock_path = main_repo_lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with lock_path.open("a+") as fh:
        _acquire_file_lock(
            fh,
            timeout=timeout,
            poll=_LOCK_POLL_INTERVAL_SECONDS,
            lock_path=lock_path,
            abort_check=_abort_check,
        )
        logging_setup.file_lock_acquired(
            path=str(lock_path), wait_ms=(time.monotonic() - started) * 1000
        )
        try:
            yield
        finally:
            if sys.platform == "win32":  # pragma: no cover
                pos = fh.tell()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                fh.seek(pos)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def async_main_repo_file_lock(
    repo: str, *, timeout: float = LOCK_WAIT_TIMEOUT_SECONDS
) -> AsyncIterator[None]:
    """Async wrapper around :func:`main_repo_file_lock` (thread offload).

    Cancellation-safe: if the awaiting task is cancelled while the worker
    thread is still polling for the ``flock``, the thread is told to abort
    (next poll iteration, ≤0.5s) instead of polling up to 8h in the
    background — and if the thread happened to win the lock in the same
    instant, it is released immediately so the fd/flock can never leak.
    """
    guard = threading.Lock()
    state = {"cancelled": False, "acquired": False}
    cm = main_repo_file_lock(
        repo, timeout=timeout, _abort_check=lambda: state["cancelled"],
    )

    def _enter() -> None:
        cm.__enter__()
        with guard:
            if state["cancelled"]:
                must_release = True
            else:
                state["acquired"] = True
                must_release = False
        if must_release:
            # Cancelled in the same instant the lock was won — undo now.
            cm.__exit__(None, None, None)
            raise FileLockAbortedError(f"cancelled while locking {repo}")

    try:
        await asyncio.to_thread(_enter)
    except asyncio.CancelledError:
        with guard:
            state["cancelled"] = True
            acquired_before_cancel = state["acquired"]
            state["acquired"] = False
        if acquired_before_cancel:
            # The enter thread finished successfully but our await was
            # cancelled before resuming — release from a helper thread
            # (flock ops are cheap; daemon so shutdown is never blocked).
            threading.Thread(
                target=cm.__exit__, args=(None, None, None), daemon=True,
            ).start()
        raise
    try:
        yield
    finally:
        await asyncio.to_thread(cm.__exit__, None, None, None)


__all__ = [
    "FileLockAbortedError",
    "async_main_repo_file_lock",
    "build_flocked_baseline_merge_command",
    "build_generic_locked_merge_command",
    "main_repo_file_lock",
    "main_repo_lock_explanation",
    "main_repo_lock_path",
    "merge_lock_reference",
    "merge_script_path",
    "self_merge_instruction",
]
