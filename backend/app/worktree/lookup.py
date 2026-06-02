"""Worktree path lookup for the dispatch path.

Every dispatch message must declare the worker's worktree absolute path so
the agent knows where to write (DEV.md §5.4 first layer of OpenClaw safety,
plus general TUI behaviour). ClawTeam already creates the worktrees inside
``~/.clawteam/workspaces/{team}/{agent}/``; this module is the single
helper that resolves *team + agent → (path, branch)* and caches the lookup
within a single Run to avoid spamming ``clawteam workspace list``.

Public API:

* :class:`WorktreeInfo` — typed view over a single workspace row.
* :class:`WorktreeLookup` — per-Run lookup with TTL cache.
* :func:`get_worktree_lookup` — module-level singleton wrapping the
  default :class:`ClawTeamCli` (handy for tests).

Strategy for the source of truth (in priority order):

1. **CLI** — ``clawteam --json workspace list {team} --repo {repo}``.
   Authoritative, but each call spawns a subprocess (~50ms), so we cache.
2. **Registry file** — fall back to reading
   ``~/.clawteam/workspaces/{team}/workspace-registry.json`` if the CLI
   call fails (e.g. transient subprocess error). This file is the same
   source ClawTeam itself reads, so it's safe.

The cache TTL is short (default 2s) — long enough that a tight scheduler
loop polling 5–10 task statuses doesn't re-shell the CLI each time, but
short enough that adding a new worktree (e.g. resume re-spawn) is reflected
near-immediately.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config, load_config
from app.integrations.clawteam_cli import ClawTeamCli, get_clawteam_cli
from app.logging_setup import get_logger

logger = get_logger("worktree.lookup")


@dataclass(frozen=True)
class WorktreeInfo:
    """Subset of ``WorkspaceInfo`` (ClawTeam) that the dispatcher needs."""

    agent_name: str
    branch_name: str
    worktree_path: str
    repo_root: str
    base_branch: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorktreeInfo":
        return cls(
            agent_name=raw["agent_name"],
            branch_name=raw["branch_name"],
            worktree_path=raw["worktree_path"],
            repo_root=raw["repo_root"],
            base_branch=raw.get("base_branch", "main"),
        )


# ──────────────────────────────────────────────────────────────────────
# Cache
# ──────────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    fetched_at: float
    workspaces: list[WorktreeInfo]


class WorktreeLookup:
    """Resolve worktree paths for a Run; caches per-team for ``ttl_seconds``."""

    def __init__(
        self,
        *,
        cli: ClawTeamCli | None = None,
        config: Config | None = None,
        ttl_seconds: float = 2.0,
    ) -> None:
        self._cli = cli or get_clawteam_cli()
        self._cfg = config or load_config()
        self._ttl = ttl_seconds
        self._cache: dict[tuple[str, str | None], _CacheEntry] = {}

    # ── primary surface ───────────────────────────────────────────────

    async def list_team(
        self, team: str, *, repo: str | None = None, force: bool = False,
    ) -> list[WorktreeInfo]:
        """Return all worktrees for *team* (cached)."""
        key = (team, repo)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and not force and (now - cached.fetched_at) < self._ttl:
            return cached.workspaces

        try:
            raw = await self._cli.workspace_list(team=team, repo=repo)
            workspaces = [WorktreeInfo.from_dict(w) for w in raw]
        except Exception as exc:
            # Fall back to the on-disk registry (best-effort).
            logger.warning(
                "worktree_lookup_cli_failed",
                team=team, repo=repo, error=str(exc),
            )
            workspaces = self._read_registry_fallback(team)

        self._cache[key] = _CacheEntry(fetched_at=now, workspaces=workspaces)
        return workspaces

    async def get(
        self,
        team: str,
        agent_name: str,
        *,
        repo: str | None = None,
        force: bool = False,
    ) -> WorktreeInfo | None:
        """Lookup one worktree; emits a debug log per call."""
        for w in await self.list_team(team, repo=repo, force=force):
            if w.agent_name == agent_name:
                logger.debug(
                    "worktree_path_lookup",
                    agent_id=agent_name, team=team,
                    worktree_path=w.worktree_path, branch=w.branch_name,
                )
                return w
        logger.debug(
            "worktree_path_lookup",
            agent_id=agent_name, team=team,
            worktree_path=None, branch=None,
        )
        return None

    def invalidate(self, team: str | None = None) -> None:
        """Drop cache entries (call after spawn / workspace_cleanup)."""
        if team is None:
            self._cache.clear()
            return
        for key in list(self._cache.keys()):
            if key[0] == team:
                del self._cache[key]

    # ── fallback ─────────────────────────────────────────────────────

    def _read_registry_fallback(self, team: str) -> list[WorktreeInfo]:
        """Read ``~/.clawteam/workspaces/{team}/workspace-registry.json`` directly.

        The registry has shape ``{"team_name", "repo_root", "workspaces": [...]}``
        per ``clawteam.workspace.models.WorkspaceRegistry``.
        """
        clawteam_data = (
            Path(self._cfg.clawteam_data_dir).expanduser()
            if self._cfg.clawteam_data_dir
            else Path.home() / ".clawteam"
        )
        path = clawteam_data / "workspaces" / team / "workspace-registry.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "worktree_registry_read_failed",
                path=str(path), error=str(exc),
            )
            return []
        return [WorktreeInfo.from_dict(w) for w in data.get("workspaces", [])]


# ──────────────────────────────────────────────────────────────────────
# Module singleton (mostly for ad-hoc callers; RunController owns its own
# instance to bind the cache to its run lifecycle).
# ──────────────────────────────────────────────────────────────────────


_singleton: WorktreeLookup | None = None


def get_worktree_lookup() -> WorktreeLookup:
    global _singleton
    if _singleton is None:
        _singleton = WorktreeLookup()
    return _singleton


def reset_worktree_lookup() -> None:
    global _singleton
    _singleton = None


__all__ = [
    "WorktreeInfo",
    "WorktreeLookup",
    "get_worktree_lookup",
    "reset_worktree_lookup",
]
