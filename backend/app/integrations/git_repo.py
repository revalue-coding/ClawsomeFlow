"""Git repo helpers — branch listing, init, and target-branch resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.models import DEFAULT_TARGET_BRANCH


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def list_local_branches(path: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        name = line.strip()
        if name:
            out.append(name)
    return out


def current_branch(path: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    value = (proc.stdout or "").strip()
    return value or None


def conventional_branch(branches: list[str]) -> str:
    """Pick ``main`` or ``master`` when present; otherwise empty (caller must fill)."""
    for candidate in (DEFAULT_TARGET_BRANCH, "master"):
        if candidate in branches:
            return candidate
    return ""


def branch_exists_in_repo(repo: str | Path, branch: str) -> bool:
    """Return whether *branch* is a local branch in *repo*."""
    name = (branch or "").strip()
    if not name:
        return False
    path = Path(str(repo or "")).expanduser()
    if not path.is_dir() or not is_git_repo(path):
        return False
    return name in list_local_branches(path)


def resolve_workspace_base_branch(repo: str | Path) -> str:
    """Resolve the base branch of a git repo (e.g. OpenClaw agent workspace).

    Prefers HEAD, then ``main``/``master``, then the first local branch.
    Falls back to ``DEFAULT_TARGET_BRANCH`` only when the path is not a git repo
    yet (fresh workspace before ``git init``).
    """
    path = Path(str(repo or "")).expanduser()
    if not path.is_dir() or not is_git_repo(path):
        return DEFAULT_TARGET_BRANCH
    branches = list_local_branches(path)
    if not branches:
        return DEFAULT_TARGET_BRANCH
    head = current_branch(path)
    if head and head in branches:
        return head
    for candidate in (DEFAULT_TARGET_BRANCH, "master"):
        if candidate in branches:
            return candidate
    return branches[0]


def resolve_target_branch(repo: str | Path, requested: str | None) -> str:
    """Return *requested* when it exists in *repo*; else ``main``/``master``; else ``""``."""
    req = (requested or "").strip()
    raw_repo = str(repo or "").strip()
    if not raw_repo:
        return req
    path = Path(raw_repo).expanduser()
    if not path.is_dir() or not is_git_repo(path):
        return req
    branches = list_local_branches(path)
    if req and req in branches:
        return req
    return conventional_branch(branches)


def git_init_repo(path: Path) -> None:
    """Initialize *path* as a git repo on ``DEFAULT_TARGET_BRANCH`` (``main``)."""
    try:
        try:
            subprocess.run(
                ["git", "init", "-q", "-b", DEFAULT_TARGET_BRANCH],
                cwd=str(path),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            if "unknown switch" in stderr.lower() or "unknown option" in stderr.lower():
                subprocess.run(
                    ["git", "init", "-q"],
                    cwd=str(path),
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                raise
    except FileNotFoundError as exc:
        raise RuntimeError("git command is unavailable") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()[:1000]
        raise RuntimeError(f"git init failed: {stderr}") from exc
