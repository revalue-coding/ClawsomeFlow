"""Cross-process main-repo merge lock (pairs with ``clawteam_main_repo``).

ClawsomeFlow's in-process :class:`LockManager` serialises merge/spawn within one
``csflow`` process. Agents run in separate processes and must use the **same**
on-disk ``flock`` file so baseline ``git checkout``/``git merge`` on a shared
``repo_root`` do not race each other.

Public helpers:
* :func:`main_repo_lock_path` — stable lock file path for a main repo.
* :func:`main_repo_lock_explanation` — prompt text for agent self-merge steps.
* :func:`build_flocked_baseline_merge_command` — ``flock -x … bash -c '…'`` one-liner.
* :func:`main_repo_file_lock` — sync context manager (used from async via wrapper).
* :func:`async_main_repo_file_lock` — async context manager for scheduler paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import sys
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator

from app.paths import clawsomeflow_home_path


def _expand_repo(path: str) -> str:
    if not path:
        return path
    return str(Path(path).expanduser())

if sys.platform == "win32":  # pragma: no cover
    import msvcrt
else:
    import fcntl

_LOCKS_SUBDIR = ".locks/clawteam_main_repo"


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
        f"Run locked: `{cmd}`. "
        "If merge fails: resolve conflicts yourself (no lock), commit, re-run the locked command."
    )


def build_flocked_baseline_merge_command(
    *,
    repo_root: str,
    base_branch: str,
    feature_branch: str,
    merge_message: str,
) -> str:
    """Shell one-liner: acquire repo lock, checkout base, merge feature branch."""
    repo = _expand_repo(repo_root)
    lock = main_repo_lock_path(repo)
    inner = (
        f"cd {shlex.quote(repo)} && "
        f"git checkout {shlex.quote(base_branch)} && "
        f"(git pull --ff-only || true) && "
        f"git merge --no-ff {shlex.quote(feature_branch)} "
        f"-m {shlex.quote(merge_message)}"
    )
    return f"flock -x {shlex.quote(str(lock))} bash -c {shlex.quote(inner)}"


@contextmanager
def main_repo_file_lock(repo: str) -> Iterator[None]:
    """Exclusive advisory lock on :func:`main_repo_lock_path`."""
    lock_path = main_repo_lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        if sys.platform == "win32":  # pragma: no cover
            pos = fh.tell()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            fh.seek(pos)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
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
async def async_main_repo_file_lock(repo: str) -> AsyncIterator[None]:
    """Async wrapper around :func:`main_repo_file_lock` (thread offload)."""
    cm = main_repo_file_lock(repo)
    await asyncio.to_thread(cm.__enter__)
    try:
        yield
    finally:
        await asyncio.to_thread(cm.__exit__, None, None, None)


__all__ = [
    "async_main_repo_file_lock",
    "build_flocked_baseline_merge_command",
    "main_repo_file_lock",
    "main_repo_lock_explanation",
    "main_repo_lock_path",
    "self_merge_instruction",
]
