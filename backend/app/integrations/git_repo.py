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


CLAWTEAM_AGENT_BRANCH_PREFIX = "clawteam/"


def clawteam_agent_branch_name(team: str, agent: str) -> str:
    """Default ClawTeam per-agent worktree branch name."""
    return f"clawteam/{team}/{agent}"


def delete_local_branch(repo: Path, branch: str, *, force: bool = True) -> bool:
    """Delete a local branch in *repo*. Returns True when absent or deleted."""
    name = (branch or "").strip()
    if not name:
        return True
    path = Path(str(repo)).expanduser()
    if not path.is_dir() or not is_git_repo(path):
        return False
    if name not in list_local_branches(path):
        return True
    flag = "-D" if force else "-d"
    try:
        subprocess.run(
            ["git", "branch", flag, name],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return name not in list_local_branches(path)


def delete_clawteam_agent_branch(
    repo: str | Path,
    *,
    team: str,
    agent: str,
    branch_name: str | None = None,
) -> bool:
    """Remove one ClawTeam agent worktree branch ref after workspace cleanup."""
    branch = (branch_name or "").strip() or clawteam_agent_branch_name(team, agent)
    return delete_local_branch(Path(str(repo)), branch, force=True)


def delete_clawteam_team_branches(repo: Path, team: str) -> list[str]:
    """Delete all ``clawteam/{team}/…`` local branch refs in *repo*."""
    prefix = f"clawteam/{team}/"
    deleted: list[str] = []
    for branch in list_local_branches(repo):
        if branch.startswith(prefix) and delete_local_branch(repo, branch, force=True):
            deleted.append(branch)
    return deleted


def is_clawteam_agent_branch(name: str) -> bool:
    """True for ClawTeam per-agent worktree branches (``clawteam/{team}/{agent}``)."""
    return (name or "").strip().startswith(CLAWTEAM_AGENT_BRANCH_PREFIX)


def _baseline_branch_sort_key(name: str) -> tuple[int, str]:
    for idx, candidate in enumerate((DEFAULT_TARGET_BRANCH, "master", "develop")):
        if name == candidate:
            return (idx, name)
    return (99, name)


def list_flow_target_branches(path: Path) -> list[str]:
    """Branches suitable as Flow agent target/base picks in the UI.

    ClawTeam agent worktree branches linger as local refs after worktrees are
    removed; they must not appear in the baseline-branch picker.
    """
    all_branches = list_local_branches(path)
    filtered = [b for b in all_branches if not is_clawteam_agent_branch(b)]
    if filtered:
        return sorted(filtered, key=_baseline_branch_sort_key)
    head = current_branch(path)
    if head and not is_clawteam_agent_branch(head):
        return [head]
    conv = conventional_branch(all_branches)
    return [conv] if conv else []


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
