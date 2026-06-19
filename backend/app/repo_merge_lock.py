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


def build_generic_locked_merge_command() -> str:
    """Runtime-generic locked merge: the agent fills ``REPO``/``SRC``/``DST`` and
    the repo lock is computed **at run time from $REPO**, so the *correct per-repo*
    lock is always held — no matter which repo the developer's task targets. A
    build-time fixed lock would serialise the wrong repo ("拿错锁了").

    The hash matches :func:`main_repo_lock_path` exactly (sha256 of the expanded
    repo path, first 16 hex chars), so this resolves to the SAME lock file the
    in-process scheduler ``flock``s — mutual exclusion holds across processes.
    Requires ``REPO`` to be an absolute path (no ``~``) so the shell hash equals
    the Python one. Cross-platform: ``flock`` (Linux) / ``mkdir`` spinlock
    (macOS); ``sha256sum`` / ``shasum``.
    """
    ld = shlex.quote(str(clawsomeflow_home_path() / _LOCKS_SUBDIR))
    git = (
        'cd "$REPO" && git checkout "$DST" && (git pull --ff-only || true) && '
        'git merge --no-ff "$SRC" -m "csflow: merge $SRC into $DST"'
    )
    return (
        "REPO='<abs-repo>'; SRC='<source-branch>'; DST='<dest-branch>'; "
        f"LD={ld}; "
        'H=$(printf %s "$REPO" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-16); '
        'L="$LD/$H.lock"; mkdir -p "$LD"; '
        f'if command -v flock >/dev/null 2>&1; then ( flock -x 9 || exit 1; {git} ) 9>"$L"; '
        'else d="$L.d"; n=0; while ! mkdir "$d" 2>/dev/null; do sleep 0.2; n=$((n+1)); '
        '[ "$n" -ge 600 ] && break; done; '
        "trap 'rmdir \"$d\" 2>/dev/null || true' EXIT INT TERM; "
        f"{git}; fi"
    )


def merge_lock_reference() -> str:
    """Concise, generic merge + repo-lock how-to injected into dispatch prompts in
    dev/easy modes only (see ``flow_modes.merge_reference_enabled``).

    Reference material, never a mandate: the obligation to merge lives only in the
    auto-merge task's completion checklist. It gives a **generic method** (fill
    ``REPO``/``SRC``/``DST``; lock auto-computed for whichever repo is targeted),
    not a fixed lock, so a task can direct an agent to merge any upstream agent's
    worktree branch into any developer-specified repo/branch — or open a PR.
    """
    cmd = build_generic_locked_merge_command()
    return (
        "Any merge into a shared branch must hold that repo's lock, or concurrent agents "
        "corrupt git metadata. **Reference only — merge or open a PR only if this is an "
        "auto-merge task or the task tells you to.**\n"
        "- Merge `SRC` branch into `DST` branch of repo `REPO` — fill the three vars (`REPO` = "
        "absolute path), run verbatim; the correct lock is auto-computed for `REPO`:\n"
        f"  `{cmd}`\n"
        "- Take `REPO`/`SRC`/`DST` from the upstream/worker info above (each lists its repo, "
        "branch and base branch) per this task's instructions; `SRC` must be reachable from `REPO`.\n"
        "- Conflict: resolve, `git add -A && git commit`, then re-run.\n"
        "- PR: `git push` the branch, then open the PR via your platform CLI/UI.\n"
        "- A repo path is a merge target only — never copy deliverables there; keep them in your worktree."
    )


def build_flocked_baseline_merge_command(
    *,
    repo_root: str,
    base_branch: str,
    feature_branch: str,
    merge_message: str,
) -> str:
    """Shell one-liner: acquire repo lock, checkout base, merge feature branch.

    Cross-platform by design (Linux + macOS, unified): uses the ``flock`` binary
    when present (Linux), and falls back to an atomic ``mkdir`` spinlock on hosts
    that ship no ``flock`` (macOS). The git steps are identical on both, so the
    same instruction text is safe to hand any agent regardless of host OS — a
    bare ``flock -x`` would simply error out on macOS and leave the merge unrun.

    **Quoting is deliberately flat (single level).** A previous version wrapped
    the git steps in ``flock -x <lock> bash -c '<inner>'`` and then wrapped the
    whole script again in ``bash -c '<script>'`` — two nested ``shlex.quote``
    layers on top of the merge message, which produced an unreadable
    ``'"'"'"'"'"'"'"'"'`` quote pyramid. That string is byte-for-byte valid bash,
    but no agent (LLM) could relay it without mangling the quotes → "shell quote
    parse error" at merge time. We now hold the lock without a nested shell:
    ``( flock -x 9 || exit 1; <git steps> ) 9>"<lock>"`` on Linux (the fd is
    released when the subshell exits) and the mkdir spinlock runs the git steps
    directly. The merge message is therefore quoted exactly once. The result is a
    plain POSIX-sh compound command (no outer ``bash -c`` wrapper) that any agent
    shell can run verbatim.
    """
    repo = _expand_repo(repo_root)
    lock = main_repo_lock_path(repo)
    git_steps = (
        f"cd {shlex.quote(repo)} && "
        f"git checkout {shlex.quote(base_branch)} && "
        f"(git pull --ff-only || true) && "
        f"git merge --no-ff {shlex.quote(feature_branch)} "
        f"-m {shlex.quote(merge_message)}"
    )
    lock_q = shlex.quote(str(lock))
    lockdir_q = shlex.quote(str(lock) + ".d")
    parent_q = shlex.quote(str(lock.parent))
    # Linux: hold the lock on fd 9 across a subshell — no nested `bash -c`, so the
    # git steps (and the merge message) keep their single, original quoting level.
    flock_branch = f"( flock -x 9 || exit 1; {git_steps} ) 9>{lock_q}"
    # macOS/no-flock: atomic mkdir spinlock, then run the same git steps inline.
    mkdir_branch = (
        f"d={lockdir_q}; n=0; "
        f'while ! mkdir "$d" 2>/dev/null; do '
        f'sleep 0.2; n=$((n+1)); [ "$n" -ge 600 ] && break; done; '
        f"trap 'rmdir \"$d\" 2>/dev/null || true' EXIT INT TERM; "
        f"{git_steps}"
    )
    return (
        f"mkdir -p {parent_q}; "
        f"if command -v flock >/dev/null 2>&1; then {flock_branch}; "
        f"else {mkdir_branch}; fi"
    )


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
    "build_generic_locked_merge_command",
    "main_repo_file_lock",
    "main_repo_lock_explanation",
    "main_repo_lock_path",
    "merge_lock_reference",
    "self_merge_instruction",
]
