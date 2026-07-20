"""ClawTeam CLI subprocess wrapper.

Public API:
* :class:`ClawTeamCli` — wraps every ClawTeam CLI invocation we need
  (``spawn`` / ``runtime inject`` / ``workspace`` / ``team`` / ``lifecycle``).
* :func:`get_clawteam_cli` — lazy singleton.
* :class:`CliInvocationError` — non-zero exit + captured stderr.

Why this lives in one place (DEV.md §4 / §5):
* The four anti-loop defences (no ``--task``, no ``--skill clawteam``, force
  ``--no-keepalive``, dispatch context block in every message) MUST be
  enforced for every spawn / dispatch path. By making :meth:`ClawTeamCli.spawn`
  the *only* way the rest of the codebase calls ``clawteam spawn``, the
  defences cannot be accidentally bypassed.
* Per-team / per-repo locks are also pinned here (see DEV.md §8) so callers
  don't have to remember which lock applies to which call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from app import logging_setup
from app.concurrency import LockManager, get_lock_manager
from app.config import Config, load_config
from app.integrations.git_repo import delete_clawteam_agent_branch
from app.repo_merge_lock import async_main_repo_file_lock
from app.user_context import get_request_user


# Skills banned from ever being passed to ``clawteam spawn`` (DEV.md §4 / §5).
# ``clawteam`` skill teaches workers to self-poll which would bypass the
# scheduler. Future names are added here when an equally-bad skill ships.
BANNED_SKILLS: frozenset[str] = frozenset({"clawteam"})

# The in-process ``clawteam_main_repo`` lock is held across branch prep +
# ``git worktree add`` (spawn) or checkout + merge (workspace_merge). Both can
# legitimately wait behind a long-running peer on the SAME main repo, so the
# wait is bounded generously (12h) rather than at the 30s default — a 30s
# timeout would spuriously fail a spawn/merge that was merely queued, not stuck.
# Pairs with the 8h cross-process file lock in ``repo_merge_lock``.
CLAWTEAM_MAIN_REPO_LOCK_TIMEOUT_SECONDS = 12 * 3600

# Ceiling for every *control-plane* CLI subprocess this module spawns
# (``clawteam spawn/inject/list/...``, git branch prep, tmux window ops).
# IMPORTANT: these commands NEVER execute agent tasks — tasks run
# asynchronously inside tmux / agent processes and are judged only by the
# scheduler's task timeout (``max(timeout_seconds, 14400)``). This guard
# exists solely so a hung CLI/git process cannot hold the repo/spawn locks
# forever (which would queue peers behind it for up to the 12h lock wait).
# Generous by design; override via ``CSFLOW_CLAWTEAM_CLI_TIMEOUT_SEC``.
DEFAULT_CLI_TIMEOUT_SECONDS = 15 * 60.0

# GNU-timeout style sentinel exit code returned by ``_run``/``_run_in_cwd``
# when the guard fires; call sites treat it like any other non-zero exit.
CLI_TIMEOUT_EXIT_CODE = 124


class CliInvocationError(Exception):
    """Raised when a ``clawteam`` CLI invocation exits with a non-zero status."""

    def __init__(self, *, argv: list[str], exit_code: int, stderr: str, stdout: str = ""):
        detail = (stderr or "").strip()
        if not detail:
            detail = (stdout or "").strip()
        super().__init__(
            f"`{' '.join(shlex.quote(a) for a in argv)}` exited {exit_code}: {detail[:500]}"
        )
        self.argv = argv
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout


class AntiLoopViolation(Exception):
    """Raised when a caller tries to bypass one of the four anti-loop defences.

    This is a *programmer error* — we never want to see this in production logs.
    Treated as a hard crash (no graceful fallback) so it surfaces in tests.
    """


# ──────────────────────────────────────────────────────────────────────
# Public dataclass for spawn results
# ──────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SpawnResult:
    """Outcome of a ``clawteam spawn`` call."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    json_payload: dict | None = None  # parsed when --json supplied + JSON valid


@dataclass(slots=True)
class WorkspaceCleanupAttempt:
    """One low-level cleanup command execution attempt."""

    argv: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class WorkspaceCleanupResult:
    """Detailed result for ``workspace cleanup`` (possibly multiple attempts)."""

    success: bool
    attempts: list[WorkspaceCleanupAttempt] = field(default_factory=list)


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cap *text* to ``max_bytes`` UTF-8 bytes; return ``(text, truncated)``.

    Used to bound diff patches surfaced over the API — a multi-MB patch would
    balloon the JSON response and the browser render. Cuts on a byte boundary
    (decoding with ``errors="ignore"`` drops any split multibyte tail).
    """
    if max_bytes <= 0:
        return text, False
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _is_empty_git_revert_output(text: str) -> bool:
    """True when ``git revert`` failed because the inverse was already applied.

    Re-reverting a merge that was already undone yields exit code 1 with
    ``nothing to commit, working tree clean`` (often after an ``Auto-merging``
    line). That is *not* a conflict with later commits — treat as success /
    already-reverted so the UI does not show a false "撤销合入失败".
    """
    lowered = (text or "").lower()
    return (
        "nothing to commit" in lowered
        and "working tree clean" in lowered
    )


def _match_agent_merge_shas(log_out: str, branch: str) -> list[str]:
    """From ``git log --format=%H%x1f%s`` output, return the commit shas (in
    **chronological** order, oldest first) whose subject references *branch* as a
    whole token.

    Every csflow merge subject embeds the run-unique branch name
    ``clawteam/{team}/{agent}`` — "[csflow] merge <branch> for …",
    "csflow: scheduled merge <branch>", "csflow: merge <branch> after run". The
    ``(?<!\\S)…(?!\\S)`` boundary keeps ``…/worker`` from matching ``…/worker2``.
    ``git log`` prints newest-first, so we reverse to chronological.
    """
    token = re.compile(rf"(?<!\S){re.escape(branch)}(?!\S)")
    matched: list[str] = []
    for line in reversed(log_out.splitlines()):
        if "\x1f" not in line:
            continue
        sha, _, subject = line.partition("\x1f")
        sha = sha.strip()
        if sha and token.search(subject):
            matched.append(sha)
    return matched


def _expand_repo(path: str) -> str:
    """Expand a leading ``~`` in a filesystem path.

    Subprocesses are launched without a shell (``create_subprocess_exec``), so a
    ``~`` is NOT expanded automatically — passing a raw ``~/foo`` as a ``cwd`` or
    as ``clawteam --repo`` fails with ``FileNotFoundError`` (notably on macOS,
    where agent repos like ``~/342test`` are common). We only ``expanduser`` here,
    never ``resolve``: canonicalizing symlinks (e.g. macOS ``/tmp``→``/private/tmp``)
    would break worktree-path matching elsewhere. Empty input is returned as-is.
    """
    if not path:
        return path
    return str(Path(path).expanduser())


@dataclass(slots=True)
class _SpawnArgs:
    """Internal: the immutable, post-defence-check argv builder."""

    backend: str  # "tmux" | "subprocess"
    command: list[str]  # e.g. ["claude"] or ["bash"] or ["claude", "--continue"]
    team: str
    agent_name: str
    repo: str
    workspace: bool = True       # ALWAYS True for fresh spawn (creates worktree)
    skip_permissions: bool = True
    profile: str | None = None
    agent_type: str = "general-purpose"
    skills: list[str] = field(default_factory=list)
    extra_flags: list[str] = field(default_factory=list)
    # The 3 hard gates we ENFORCE:
    #   - no --task
    #   - no --skill clawteam (validated against BANNED_SKILLS)
    #   - --no-keepalive

    def __post_init__(self) -> None:
        # Normalize the repo path once so BOTH the ``--repo`` argv (to_argv) and
        # the ``cwd=args.repo`` git branch-check (see _ensure_repo_on_target_branch)
        # receive an expanded absolute path. See _expand_repo.
        self.repo = _expand_repo(self.repo)

    def to_argv(self) -> list[str]:
        # clawteam v0.3.0's `spawn` typer command parses any agent-side flags
        # (e.g. `claude --continue`, `hermes -c`, `codex resume --last`) as
        # its own unknown options and fails with "No such option". We place
        # clawteam's own options FIRST and put the agent command after `--`
        # so typer treats every token after the separator as the COMMAND
        # positional list.
        argv: list[str] = ["clawteam", "spawn", self.backend]
        argv += ["--team", self.team, "--agent-name", self.agent_name]
        argv += ["--agent-type", self.agent_type]
        argv += ["--repo", self.repo]
        argv += ["--workspace" if self.workspace else "--no-workspace"]
        argv += ["--no-keepalive"]  # gate ③
        argv += ["--skip-permissions" if self.skip_permissions else "--no-skip-permissions"]
        if self.profile:
            argv += ["--profile", self.profile]
        for s in self.skills:
            argv += ["--skill", s]
        argv += self.extra_flags
        argv += ["--", *self.command]
        return argv


# ──────────────────────────────────────────────────────────────────────
# Wrapper
# ──────────────────────────────────────────────────────────────────────


class ClawTeamCli:
    """Async wrapper around the ``clawteam`` CLI binary.

    Every method that mutates ClawTeam state is responsible for taking the
    correct :class:`LockManager` lock so that concurrent ``RunController``s
    (or multiple users) don't race each other.
    """

    def __init__(self, *, config: Config | None = None, locks: LockManager | None = None):
        self._cfg = config or load_config()
        self._locks = locks or get_lock_manager(self._cfg)
        self._log = logging_setup.get_logger("clawteam_cli")

    # ──────────────────────────────────────────────────────────────────
    # Spawn — fresh / resume
    # ──────────────────────────────────────────────────────────────────

    async def spawn_fresh(
        self,
        *,
        team: str,
        agent_name: str,
        repo: str,
        target_branch: str | None = None,
        backend: str = "tmux",
        command: Sequence[str] = ("claude",),
        agent_type: str = "general-purpose",
        profile: str | None = None,
        skills: Sequence[str] = (),
        workspace: bool = True,
        skip_permissions: bool = True,
    ) -> SpawnResult:
        """Fresh ``clawteam spawn`` (creates worktree). Enforces anti-loop defences.

        Defenses ① ② ③ (from the four anti-loop defenses) are hard-enforced here;
        ④ (dispatch context block) is
        the responsibility of the caller composing the ``runtime_inject``
        ``summary`` argument (template enforced by :mod:`app.scheduler.prompts`).

        ``skip_permissions`` controls whether ClawTeam injects its per-CLI
        permission-bypass flag (claude → ``--dangerously-skip-permissions``,
        gemini/kimi/qwen/opencode → ``--yolo``, …). Callers that carry the exact
        flag themselves (e.g. gemini needs ``--approval-mode yolo`` which CONFLICTS
        with ClawTeam's ``--yolo``; opencode rejects ``--yolo`` outright) pass
        ``False`` to take full control via ``command``.
        """
        return await self._spawn(_SpawnArgs(
            backend=backend, command=list(command), team=team,
            agent_name=agent_name, repo=repo, workspace=workspace,
            profile=profile, agent_type=agent_type, skills=list(skills),
            skip_permissions=skip_permissions,
        ), main_repo_for_lock=repo, target_branch=target_branch)

    async def spawn_resume(
        self,
        *,
        team: str,
        agent_name: str,
        existing_worktree: str,
        backend: str = "tmux",
        resume_command: Sequence[str],
        agent_type: str = "general-purpose",
        profile: str | None = None,
        skills: Sequence[str] = (),
        skip_permissions: bool = True,
    ) -> SpawnResult:
        """Resume re-spawn after a crash.

        * ``resume_command`` is the agent-native resume invocation (e.g.
          ``["claude", "--continue"]`` / ``["codex", "resume", "--last"]``).
        * MUST pass ``--no-workspace --repo <existing_worktree>`` so ClawTeam
          re-uses the existing worktree instead of recreating it (which would
          drop all work done so far — see plan §5.1 / DEV.md §4).
        """
        # Resume uses --no-workspace, so no main_repo git mutation happens.
        # The "main_repo" lock key is therefore *informational* (keyed on the
        # worktree path); the team_spawn lock is the one that actually
        # prevents concurrency races (tmux session/window creation).
        return await self._spawn(_SpawnArgs(
            backend=backend, command=list(resume_command), team=team,
            agent_name=agent_name, repo=existing_worktree, workspace=False,
            profile=profile, agent_type=agent_type, skills=list(skills),
            skip_permissions=skip_permissions,
            # Recovery path: replace stale runtime records if ClawTeam still
            # thinks this agent is running.
            extra_flags=["--replace"],
        ), main_repo_for_lock=existing_worktree)

    async def _spawn(
        self,
        args: _SpawnArgs,
        *,
        main_repo_for_lock: str,
        target_branch: str | None = None,
    ) -> SpawnResult:
        """Common spawn path: enforce defences, take locks, run, parse, log."""
        _enforce_anti_loop(args)
        # Keep the per-main-repo lock key aligned with the expanded repo that
        # _SpawnArgs already normalized, so concurrent runs sharing a repo
        # serialize correctly regardless of ``~`` usage.
        main_repo_for_lock = _expand_repo(main_repo_for_lock)
        argv = args.to_argv()
        env = self._env()

        # Logging gate ① ② ③ all visible in the JSON event — used by
        # ``csflow logs verify-anti-loop`` to prove the invariant.
        logging_setup.spawn_cmd_built(
            cmd_argv=argv,
            workspace=args.workspace,
            repo=args.repo,
            keepalive=False,
            has_task=False,
            has_skill=bool(args.skills),
        )

        # Three locks, acquired in a globally consistent order
        # (``clawteam_main_repo`` → repo file lock → ``team_spawn``) so no
        # spawn/merge can deadlock against another:
        #   * ``clawteam_main_repo`` (in-process) serialises spawn vs merge vs
        #     spawn WITHIN this csflow process.
        #   * the cross-process repo **file lock** (same ``<hash>.lock`` an agent
        #     self-merge ``flock``s) is the ONLY thing that excludes git metadata
        #     work in *other* processes. Branch prep (checkout) + ``git worktree
        #     add`` mutate the shared working tree / ``.git`` exactly like a merge,
        #     so a worktree-creating spawn MUST hold it too — otherwise a
        #     scheduler spawn could race an agent self-merge on the same repo.
        #     Only taken when ``workspace`` (resume with ``--no-workspace`` neither
        #     checks out nor adds a worktree, so it touches no shared git state).
        #   * ``team_spawn`` only guards tmux session/window creation races, so
        #     it is held for the minimum window: just the spawn invocation.
        repo_file_lock = (
            async_main_repo_file_lock(args.repo) if args.workspace else nullcontext()
        )
        async with self._locks.lock(
            f"clawteam_main_repo:{main_repo_for_lock}",
            timeout=CLAWTEAM_MAIN_REPO_LOCK_TIMEOUT_SECONDS,
        ):
            async with repo_file_lock:
                # Prep the main repo for EVERY worktree-creating spawn (workspace=True),
                # whether or not a target branch is requested. ``_ensure_repo_on_target_branch``
                # auto-commits any pending changes ("csflow auto commit") so the new
                # worktree branches off a committed state, and — when a target branch is
                # given — guarantees the switch succeeds before the worktree is built.
                if args.workspace:
                    current_branch, switched = await _ensure_repo_on_target_branch(
                        repo=args.repo,
                        target_branch=target_branch,
                        env=env,
                    )
                    if switched:
                        self._log.info(
                            "spawn_repo_branch_switched",
                            repo=args.repo,
                            from_branch=current_branch,
                            to_branch=target_branch,
                            team=args.team,
                            agent_name=args.agent_name,
                        )
                async with self._locks.lock(f"team_spawn:{args.team}"):
                    exit_code, stdout, stderr = await _run(argv, env=env)

        logging_setup.spawn_cmd_executed(
            cmd_argv=argv, exit_code=exit_code, stderr=stderr, stdout=stdout,
        )
        if exit_code != 0:
            raise CliInvocationError(
                argv=argv, exit_code=exit_code, stderr=stderr, stdout=stdout,
            )
        return SpawnResult(
            argv=argv, exit_code=exit_code, stdout=stdout, stderr=stderr,
            json_payload=_try_parse_json(stdout),
        )

    # ──────────────────────────────────────────────────────────────────
    # Runtime inject — dispatch a message to a live tmux session
    # ──────────────────────────────────────────────────────────────────

    async def runtime_inject(
        self,
        *,
        team: str,
        agent: str,
        summary: str,
        source: str = "csflow-scheduler",
        channel: str = "task-dispatch",
        priority: str = "medium",
    ) -> None:
        """Wrap ``clawteam runtime inject`` (paste-buffer to live tmux pane)."""
        argv = [
            "clawteam", "runtime", "inject", team, agent,
            "--source", source, "--channel", channel, "--priority", priority,
            "--summary", summary,
        ]
        exit_code, stdout, stderr = await _run(argv, env=self._env())
        logging_setup.runtime_inject(
            target=f"{team}:{agent}",
            summary_len=len(summary),
            success=(exit_code == 0),
            exit_code=exit_code,
            error_msg=stderr.strip() if exit_code != 0 else None,
        )
        if exit_code != 0:
            raise CliInvocationError(
                argv=argv, exit_code=exit_code, stderr=stderr, stdout=stdout,
            )

    # ──────────────────────────────────────────────────────────────────
    # Team lifecycle
    # ──────────────────────────────────────────────────────────────────

    async def team_spawn_team(
        self,
        *,
        team: str,
        agent_name: str,
        agent_type: str = "leader",
        description: str = "",
    ) -> dict | None:
        """``clawteam team spawn-team`` — registers leader metadata, NO process.

        Returns the parsed JSON result (since we always pass ``--json``).
        """
        argv = [
            "clawteam", "--json", "team", "spawn-team", team,
            "-d", description, "-n", agent_name, "--agent-type", agent_type,
        ]
        exit_code, stdout, stderr = await _run(argv, env=self._env())
        if exit_code != 0:
            raise CliInvocationError(
                argv=argv, exit_code=exit_code, stderr=stderr, stdout=stdout,
            )
        return _try_parse_json(stdout)

    async def team_cleanup(self, *, team: str, force: bool = True) -> None:
        argv = ["clawteam", "team", "cleanup", team]
        if force:
            argv.append("--force")
        exit_code, _stdout, stderr = await _run(argv, env=self._env())
        if exit_code != 0:
            raise CliInvocationError(
                argv=argv, exit_code=exit_code, stderr=stderr,
            )

    # ──────────────────────────────────────────────────────────────────
    # Workspace operations
    # ──────────────────────────────────────────────────────────────────

    async def workspace_list(self, *, team: str, repo: str | None = None) -> list[dict]:
        """Return the parsed ``WorkspaceInfo`` rows for *team*.

        Returns an empty list when the team has no workspaces. The wire
        format is ``{"workspaces": [WorkspaceInfo, ...]}`` (see
        ``clawteam.cli.commands.workspace_list`` + ``WorkspaceInfo`` —
        fields: ``agent_name``, ``agent_id``, ``team_name``, ``branch_name``,
        ``worktree_path``, ``repo_root``, ``base_branch``, ``created_at``).
        """
        argv = ["clawteam", "--json", "workspace", "list", team]
        if repo:
            argv += ["--repo", _expand_repo(repo)]
        exit_code, stdout, stderr = await _run(argv, env=self._env())
        if exit_code != 0:
            # `clawteam` prints "Not in a git repo" + exits 1 when neither
            # --repo nor an inferable cwd repo is provided. Surface a clear
            # empty list for the worktree/lookup helper rather than raising.
            if "Not in a git repo" in (stderr or ""):
                return []
            raise CliInvocationError(
                argv=argv, exit_code=exit_code, stderr=stderr, stdout=stdout,
            )
        parsed = _try_parse_json(stdout)
        if isinstance(parsed, dict):
            return list(parsed.get("workspaces", []))
        if isinstance(parsed, list):  # tolerate older clawteam versions
            return parsed
        return []

    async def workspace_merge(
        self,
        *,
        team: str,
        agent: str,
        repo: str | None = None,
        target: str | None = None,
        cleanup: bool = True,
    ) -> tuple[bool, str]:
        """Merge one workspace branch into target branch.

        ClawsomeFlow performs the git merge itself (checkout target +
        ``git merge --no-ff``), while ClawTeam remains the source of truth
        for workspace metadata and optional cleanup.
        Returns ``(success, output)``; callers decide how to classify failures.
        """
        repo_hint = _expand_repo((repo or "").strip()) or None
        rows = await self.workspace_list(team=team, repo=repo_hint)
        row = next(
            (
                item
                for item in rows
                if str(item.get("agent_name") or item.get("agent_id") or "").strip() == agent
            ),
            None,
        )
        if row is None:
            msg = (
                f"no workspace found for team={team!r}, agent={agent!r}"
                + (f", repo={repo_hint!r}" if repo_hint else "")
            )
            logging_setup.workspace_merge(
                agent_id=agent,
                team=team,
                success=False,
                stderr=msg,
            )
            return False, msg

        repo_root = repo_hint or str(row.get("repo_root") or "").strip()
        branch_name = str(row.get("branch_name") or "").strip()
        target_branch = (target or str(row.get("base_branch") or "")).strip()
        if not repo_root:
            msg = (
                f"workspace metadata missing repo_root for team={team!r}, agent={agent!r}; "
                "please pass repo explicitly"
            )
            logging_setup.workspace_merge(
                agent_id=agent,
                team=team,
                success=False,
                stderr=msg,
            )
            return False, msg
        if not branch_name:
            msg = (
                f"workspace metadata missing branch_name for team={team!r}, agent={agent!r}"
            )
            logging_setup.workspace_merge(
                agent_id=agent,
                team=team,
                success=False,
                stderr=msg,
            )
            return False, msg
        if not target_branch:
            msg = (
                f"target branch is empty for team={team!r}, agent={agent!r}; "
                "pass target explicitly"
            )
            logging_setup.workspace_merge(
                agent_id=agent,
                team=team,
                success=False,
                stderr=msg,
            )
            return False, msg

        repo_root = _expand_repo(repo_root)
        env = self._env()
        # Branch-scoped network fetch only — does not touch HEAD/worktree; safe outside lock.
        await _best_effort_git_fetch(
            repo=repo_root, branch=target_branch, env=env,
        )
        async with self._locks.lock(
            f"clawteam_main_repo:{repo_root}",
            timeout=CLAWTEAM_MAIN_REPO_LOCK_TIMEOUT_SECONDS,
        ):
            async with async_main_repo_file_lock(repo_root):
                merge_head_code, _, _ = await _run_in_cwd(
                    ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                    cwd=repo_root,
                    env=env,
                )
                if merge_head_code == 0:
                    msg = (
                        f"baseline repo {repo_root!r} has an in-progress merge "
                        "(MERGE_HEAD exists); resolve or abort it before merging"
                    )
                    logging_setup.workspace_merge(
                        agent_id=agent,
                        team=team,
                        success=False,
                        stderr=msg,
                    )
                    return False, msg

                checkout_argv = ["git", "checkout", target_branch]
                checkout_code, checkout_out, checkout_err = await _run_in_cwd(
                    checkout_argv,
                    cwd=repo_root,
                    env=env,
                )
                if checkout_code != 0:
                    combined = (
                        (checkout_out or "")
                        + (checkout_err or "")
                        or f"git checkout failed: {' '.join(checkout_argv)}"
                    )
                    logging_setup.workspace_merge(
                        agent_id=agent,
                        team=team,
                        success=False,
                        stderr=combined,
                    )
                    return False, combined

                head_code, head_out, head_err = await _run_in_cwd(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=repo_root,
                    env=env,
                )
                current_branch = (head_out or "").strip()
                if head_code != 0 or current_branch != target_branch:
                    combined = (
                        f"after checkout expected branch {target_branch!r}, "
                        f"got {current_branch!r}"
                    )
                    if head_err:
                        combined += f"\n{head_err}"
                    logging_setup.workspace_merge(
                        agent_id=agent,
                        team=team,
                        success=False,
                        stderr=combined,
                    )
                    return False, combined

                await _fast_forward_origin_branch_if_available(
                    repo=repo_root,
                    branch=target_branch,
                    env=env,
                )

                merge_argv = [
                    "git",
                    "merge",
                    "--no-ff",
                    branch_name,
                    "-m",
                    f"[csflow] merge {branch_name} for {team}/{agent}",
                ]
                merge_code, merge_out, merge_err = await _run_in_cwd(
                    merge_argv,
                    cwd=repo_root,
                    env=env,
                )
                if merge_code != 0:
                    abort_argv = ["git", "merge", "--abort"]
                    abort_code, abort_out, abort_err = await _run_in_cwd(
                        abort_argv,
                        cwd=repo_root,
                        env=env,
                    )
                    combined = (merge_out or "") + (merge_err or "")
                    if abort_code != 0:
                        combined += (
                            "\n\n[csflow] git merge --abort failed:\n"
                            + (abort_out or "")
                            + (abort_err or "")
                        )
                    logging_setup.workspace_merge(
                        agent_id=agent,
                        team=team,
                        success=False,
                        stderr=combined,
                    )
                    return False, combined

                combined = (merge_out or "") + (merge_err or "")
            if cleanup:
                cleaned = await self.workspace_cleanup(
                    team=team,
                    agent=agent,
                    repo=repo_root,
                )
                if not cleaned:
                    # Preserve ClawTeam's historical behavior: merge is successful
                    # even when cleanup fails; callers can trigger retry/diagnostics.
                    self._log.warning(
                        "workspace_merge_cleanup_failed",
                        team=team,
                        agent=agent,
                        repo=repo_root,
                    )
            logging_setup.workspace_merge(
                agent_id=agent,
                team=team,
                success=True,
            )
            return True, combined

    async def workspace_has_uncommitted_changes(
        self,
        *,
        worktree_path: str,
    ) -> tuple[bool, list[str]]:
        """Return whether *worktree_path* has unstaged/staged/untracked changes."""
        argv = ["git", "status", "--porcelain"]
        exit_code, stdout, stderr = await _run_in_cwd(
            argv,
            cwd=worktree_path,
            env=self._env(),
        )
        if exit_code != 0:
            raise CliInvocationError(
                argv=argv,
                exit_code=exit_code,
                stderr=stderr,
                stdout=stdout,
            )
        lines = [line.rstrip() for line in stdout.splitlines() if line.strip()]
        return bool(lines), lines

    async def workspace_agent_patch(
        self,
        *,
        team: str,
        agent: str,
        repo: str | None = None,
        max_bytes: int = 400_000,
    ) -> dict[str, Any] | None:
        """Return the full unified diff for an agent's worktree branch.

        Resolves the workspace row (``repo_root`` / ``branch_name`` /
        ``base_branch`` / ``worktree_path``), then computes two patches:

        * ``patch`` — ``git diff --no-color {base}...{branch}`` in the main
          repo: the *committed* content that :meth:`workspace_merge` would
          bring into the target branch (three-dot = diff vs the merge base,
          identical semantics to ClawTeam's ``agent_diff`` stats).
        * ``uncommitted_patch`` — ``git diff --no-color HEAD`` inside the
          worktree: working-tree changes that are NOT yet committed and would
          therefore NOT be merged (best-effort; empty when the worktree is
          gone or clean).

        Also reports the divergence between the two tips (``git rev-list
        --left-right --count {base}...{branch}``): ``branch_ahead`` = commits
        the worktree adds on top of the fork point (all of which the merge
        brings in), ``base_ahead`` = commits the target branch gained since the
        worktree forked (invisible in the three-dot ``patch`` — a >0 value warns
        the base moved on and the merge may need to reconcile them). The count
        uses the LOCAL base ref (no network fetch — this is a read-only path);
        the real merge fast-forwards the base to ``origin`` first.

        Each patch is truncated to ``max_bytes`` (UTF-8) with a companion
        ``*_truncated`` flag so a huge diff can't blow up the response. Returns
        ``None`` when the workspace can't be resolved (caller maps to 404).
        """
        repo_hint = _expand_repo((repo or "").strip()) or None
        rows = await self.workspace_list(team=team, repo=repo_hint)
        row = next(
            (
                item
                for item in rows
                if str(item.get("agent_name") or item.get("agent_id") or "").strip() == agent
            ),
            None,
        )
        if row is None:
            return None

        repo_root = _expand_repo(repo_hint or str(row.get("repo_root") or "").strip())
        branch_name = str(row.get("branch_name") or "").strip()
        base_branch = str(row.get("base_branch") or "").strip() or "main"
        worktree_path = _expand_repo(str(row.get("worktree_path") or "").strip())
        if not repo_root or not branch_name:
            return None

        env = self._env()
        patch = ""
        patch_truncated = False
        diff_argv = ["git", "diff", "--no-color", f"{base_branch}...{branch_name}"]
        code, out, _ = await _run_in_cwd(diff_argv, cwd=repo_root, env=env)
        if code == 0:
            patch, patch_truncated = _truncate_utf8(out, max_bytes)

        uncommitted_patch = ""
        uncommitted_truncated = False
        if worktree_path and Path(worktree_path).exists():
            u_code, u_out, _ = await _run_in_cwd(
                ["git", "diff", "--no-color", "HEAD"],
                cwd=worktree_path,
                env=env,
            )
            if u_code == 0:
                uncommitted_patch, uncommitted_truncated = _truncate_utf8(
                    u_out, max_bytes
                )

        # Divergence: left = base ahead (commits target gained since the fork,
        # invisible in the three-dot patch), right = branch ahead (worktree's
        # own commits, all merged in). Best-effort; 0/0 on any git error.
        base_ahead = 0
        branch_ahead = 0
        rl_code, rl_out, _ = await _run_in_cwd(
            ["git", "rev-list", "--left-right", "--count", f"{base_branch}...{branch_name}"],
            cwd=repo_root,
            env=env,
        )
        if rl_code == 0:
            parts = rl_out.split()
            if len(parts) == 2:
                try:
                    base_ahead = int(parts[0])
                    branch_ahead = int(parts[1])
                except ValueError:
                    base_ahead = branch_ahead = 0

        return {
            "repo_root": repo_root,
            "worktree_path": worktree_path,
            "branch": branch_name,
            "base_branch": base_branch,
            "patch": patch,
            "patch_truncated": patch_truncated,
            "uncommitted_patch": uncommitted_patch,
            "uncommitted_truncated": uncommitted_truncated,
            "base_ahead": base_ahead,
            "branch_ahead": branch_ahead,
        }

    async def run_merged_agent_patch(
        self,
        *,
        team: str,
        agent: str,
        repo: str | None,
        include_patch: bool = True,
        max_bytes: int = 400_000,
    ) -> dict[str, Any] | None:
        """Reconstruct what *this run's* ``agent`` branch actually merged into a
        baseline branch, read from the baseline repo's **history** (not a worktree).

        Powers the post-run "Run diff" module: unlike :meth:`workspace_agent_patch`
        (which needs a live worktree and shows *pending* changes), this survives
        worktree cleanup because it reads committed merge history in the main repo.

        **Isolating this run's commits on a shared baseline.** A baseline branch is
        routinely written by many runs concurrently, plus arbitrary later commits.
        We key detection off the *run-unique* branch name
        ``clawteam/{team}/{agent}`` (``team`` = ``csflow-{run_id_short}`` is unique
        per run). Every csflow merge — the scheduler's :meth:`workspace_merge` and
        the agent self-merge tool (``csflow-locked-merge.py``) — is a
        ``git merge --no-ff <branch> -m "…<branch>…"``, so the branch name is
        embedded in the merge-commit subject. We select only merge commits whose
        subject references *this exact* branch (token-boundary match, so
        ``…/worker`` never matches ``…/worker2``); other runs' merges (different
        team) and unrelated later commits never match. For each matched merge ``M``
        the introduced content is ``git diff M^1 M`` (first parent = the baseline
        tip immediately before this merge, already including whatever else had
        landed), which isolates only this agent's contribution regardless of
        concurrent/subsequent baseline activity.

        **No checkout, no lock.** Every git call here is read-only and addresses
        commits by SHA (``log --all --merges``, ``diff <sha>^1 <sha>``,
        ``rev-list --count``) — none touch HEAD or the working tree, so the branch
        is never switched and the repo merge lock is unnecessary (same read-only
        posture as :meth:`workspace_agent_patch`).

        Returns ``None`` when the repo can't be resolved / isn't a git repo.
        Otherwise a dict; the caller keys "show this agent?" off **``files_changed``**
        (not ``merge_count``): a merge commit can exist yet bring **zero net file
        changes** to the baseline (e.g. the agent's commits duplicated content
        already on the base, or the merge was empty) — that is NOT an effective
        change and such agents must be dropped from the display. ``merge_count=0``
        means no matching merge at all (dismissed / failed+aborted / never merged).
        ``patch`` is omitted (empty) when ``include_patch`` is False (list view).
        """
        repo_root = _expand_repo((repo or "").strip())
        if not repo_root or not (Path(repo_root) / ".git").exists():
            return None
        branch = f"clawteam/{team}/{agent}"
        env = self._env()

        # Enumerate merge commits across all refs, then keep only those whose
        # subject references this run's exact branch (see _match_agent_merge_shas).
        log_code, log_out, _ = await _run_in_cwd(
            ["git", "log", "--all", "--merges", "--format=%H%x1f%s"],
            cwd=repo_root,
            env=env,
        )
        matched = _match_agent_merge_shas(log_out, branch) if log_code == 0 else []

        commit_count = 0
        files: set[str] = set()
        insertions = 0
        deletions = 0
        patch_parts: list[str] = []
        for sha in matched:
            # Commits this agent uniquely contributed via this merge:
            # reachable from the second parent (branch tip) but not the first
            # (baseline before merge). Best-effort; 0 on any git error.
            rc, rc_out, _ = await _run_in_cwd(
                ["git", "rev-list", "--count", f"{sha}^1..{sha}^2"],
                cwd=repo_root, env=env,
            )
            if rc == 0:
                try:
                    commit_count += int(rc_out.strip() or "0")
                except ValueError:
                    pass
            # numstat drives file/line aggregates without the full patch text.
            ns_code, ns_out, _ = await _run_in_cwd(
                ["git", "diff", "--no-color", "--numstat", f"{sha}^1", sha],
                cwd=repo_root, env=env,
            )
            if ns_code == 0:
                for row in ns_out.splitlines():
                    cols = row.split("\t")
                    if len(cols) != 3:
                        continue
                    add_s, del_s, path = cols
                    files.add(path)
                    if add_s.isdigit():
                        insertions += int(add_s)
                    if del_s.isdigit():
                        deletions += int(del_s)
            if include_patch:
                d_code, d_out, _ = await _run_in_cwd(
                    ["git", "diff", "--no-color", f"{sha}^1", sha],
                    cwd=repo_root, env=env,
                )
                if d_code == 0 and d_out.strip():
                    if len(matched) > 1:
                        # Multiple merges (e.g. an initial merge plus a
                        # complaint-phase re-merge) — label each fragment.
                        patch_parts.append(
                            f"===== merged commit {sha[:12]} =====\n{d_out}"
                        )
                    else:
                        patch_parts.append(d_out)

        patch = "\n\n".join(patch_parts)
        patch, patch_truncated = _truncate_utf8(patch, max_bytes)
        return {
            "repo_root": repo_root,
            "branch": branch,
            "merge_count": len(matched),
            "commit_count": commit_count,
            "files_changed": len(files),
            "insertions": insertions,
            "deletions": deletions,
            "patch": patch,
            "patch_truncated": patch_truncated,
        }

    async def revert_agent_merges(
        self,
        *,
        team: str,
        agent: str,
        repo: str | None,
        target_branch: str = "main",
    ) -> dict[str, Any]:
        """Revert (undo) this run's ``agent`` merges on ``target_branch`` via
        ``git revert -m 1`` — the "撤销合入" action, available any time after the
        run completes.

        **Non-destructive & no-force.** A revert *adds* new commits that invert the
        merges' changes; it never rewrites history or edits files by any other
        means. Reverts newest→oldest so a later merge is undone before an earlier
        one. If git can't do it cleanly (conflicts with commits that landed on top,
        a dirty tree, a merge/revert already in progress, checkout failure, …) the
        whole operation is **rolled back to the exact pre-revert tip** and we return
        ``ok=False`` with the git reason — no partial state, no forced writes.

        **Locking.** Unlike the read-only Run-diff path this WRITES the baseline
        (checkout + revert commits), so it takes the same in-process +
        cross-process repo locks as :meth:`workspace_merge`. Purely local — never
        fetches or pushes.

        Only merges that brought *effective* file changes are reverted (empty
        merges are skipped). Returns a dict: ``ok``, ``target_branch``,
        ``merge_shas`` (reverted, chronological), ``revert_head`` (new tip on
        success), ``nothing_to_revert``, ``message``.
        """
        result: dict[str, Any] = {
            "ok": False, "target_branch": target_branch, "merge_shas": [],
            "revert_head": "", "nothing_to_revert": False, "message": "",
        }
        repo_root = _expand_repo((repo or "").strip())
        if not repo_root or not (Path(repo_root) / ".git").exists():
            result["message"] = f"repo not found or not a git repo: {repo_root!r}"
            return result
        target = (target_branch or "").strip() or "main"
        result["target_branch"] = target
        branch = f"clawteam/{team}/{agent}"
        env = self._env()

        async with self._locks.lock(
            f"clawteam_main_repo:{repo_root}",
            timeout=CLAWTEAM_MAIN_REPO_LOCK_TIMEOUT_SECONDS,
        ):
            async with async_main_repo_file_lock(repo_root):
                # Merges of this agent's branch that live on the target branch.
                log_code, log_out, log_err = await _run_in_cwd(
                    ["git", "log", target, "--merges", "--format=%H%x1f%s"],
                    cwd=repo_root, env=env,
                )
                if log_code != 0:
                    result["message"] = (
                        f"cannot read history of {target!r}: {(log_err or '').strip()[:300]}"
                    )
                    return result
                matched = _match_agent_merge_shas(log_out, branch)
                # Keep only merges that brought real file changes (skip empty).
                effective: list[str] = []
                for sha in matched:
                    q_code, _, _ = await _run_in_cwd(
                        ["git", "diff", "--quiet", f"{sha}^1", sha],
                        cwd=repo_root, env=env,
                    )
                    if q_code == 1:  # 1 = differences present; 0 = none
                        effective.append(sha)
                if not effective:
                    # Idempotent: merges already empty / gone → success so the
                    # API can mark the agent reverted and hide it from Run-diff.
                    result["ok"] = True
                    result["nothing_to_revert"] = True
                    result["message"] = (
                        f"no effective merge of {branch!r} found on {target!r} to revert"
                    )
                    return result

                # Refuse to touch a repo that is mid-merge/revert/cherry-pick.
                for head_ref in ("MERGE_HEAD", "REVERT_HEAD", "CHERRY_PICK_HEAD"):
                    hc, _, _ = await _run_in_cwd(
                        ["git", "rev-parse", "-q", "--verify", head_ref],
                        cwd=repo_root, env=env,
                    )
                    if hc == 0:
                        result["message"] = (
                            f"{repo_root!r} has an in-progress {head_ref}; "
                            "resolve or abort it before reverting"
                        )
                        return result

                co_code, co_out, co_err = await _run_in_cwd(
                    ["git", "checkout", target], cwd=repo_root, env=env,
                )
                if co_code != 0:
                    result["message"] = (
                        f"git checkout {target} failed: "
                        f"{((co_err or '') + (co_out or '')).strip()[:300]}"
                    )
                    return result
                hb_code, hb_out, _ = await _run_in_cwd(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, env=env,
                )
                if hb_code != 0 or (hb_out or "").strip() != target:
                    result["message"] = (
                        f"expected to be on {target!r} after checkout, "
                        f"got {(hb_out or '').strip()!r}"
                    )
                    return result
                # Never modify files by force: require a clean tree.
                st_code, st_out, _ = await _run_in_cwd(
                    ["git", "status", "--porcelain"], cwd=repo_root, env=env,
                )
                if st_code != 0 or st_out.strip():
                    result["message"] = (
                        f"baseline {target!r} has uncommitted changes; "
                        "commit or clean them first (not touching files)"
                    )
                    return result

                orig_code, orig_out, _ = await _run_in_cwd(
                    ["git", "rev-parse", "HEAD"], cwd=repo_root, env=env,
                )
                orig_tip = (orig_out or "").strip()

                # Revert newest→oldest (mainline parent = -m 1). One command; git
                # stops at the first conflict, which we roll back cleanly below.
                newest_first = list(reversed(effective))
                rv_code, rv_out, rv_err = await _run_in_cwd(
                    ["git", "revert", "-m", "1", "--no-edit", *newest_first],
                    cwd=repo_root, env=env,
                )
                if rv_code != 0:
                    combined = ((rv_out or "") + (rv_err or "")).strip()
                    # Abort any in-progress revert, then restore to the exact
                    # pre-revert tip so no partial reverts remain.
                    await _run_in_cwd(
                        ["git", "revert", "--abort"], cwd=repo_root, env=env,
                    )
                    if orig_tip:
                        await _run_in_cwd(
                            ["git", "reset", "--hard", orig_tip], cwd=repo_root, env=env,
                        )
                    if _is_empty_git_revert_output(combined):
                        # Merges were already undone (prior 撤销合入 or manual
                        # revert). Tip unchanged; report success + mark done.
                        nh_code, nh_out, _ = await _run_in_cwd(
                            ["git", "rev-parse", "HEAD"], cwd=repo_root, env=env,
                        )
                        result["ok"] = True
                        result["nothing_to_revert"] = True
                        result["merge_shas"] = effective
                        result["revert_head"] = (
                            (nh_out or "").strip() if nh_code == 0 else orig_tip
                        )
                        result["message"] = (
                            "already reverted (empty git revert; no changes needed)"
                        )
                        return result
                    result["message"] = (
                        "git revert failed (likely a conflict with commits that "
                        "landed later); rolled back, no changes made:\n"
                        f"{combined[:600]}"
                    )
                    return result

                nh_code, nh_out, _ = await _run_in_cwd(
                    ["git", "rev-parse", "HEAD"], cwd=repo_root, env=env,
                )
                result["ok"] = True
                result["merge_shas"] = effective
                result["revert_head"] = (nh_out or "").strip() if nh_code == 0 else ""
                result["message"] = (rv_out or "").strip()[:600]
                return result

    async def workspace_cleanup(
        self, *, team: str, agent: str, repo: str | None = None,
    ) -> bool:
        result = await self.workspace_cleanup_with_diagnostics(
            team=team,
            agent=agent,
            repo=repo,
        )
        return result.success

    async def workspace_cleanup_with_diagnostics(
        self, *, team: str, agent: str, repo: str | None = None,
    ) -> WorkspaceCleanupResult:
        branch_name: str | None = None
        repo_root = _expand_repo((repo or "").strip())
        try:
            rows = await self.workspace_list(team=team, repo=repo_root or None)
            row = next(
                (
                    item
                    for item in rows
                    if str(item.get("agent_name") or item.get("agent_id") or "").strip() == agent
                ),
                None,
            )
            if row:
                branch_name = str(row.get("branch_name") or "").strip() or None
                if not repo_root:
                    repo_root = str(row.get("repo_root") or "").strip()
        except Exception as exc:
            self._log.debug(
                "workspace_cleanup_prefetch_failed",
                team=team,
                agent=agent,
                error=str(exc),
            )

        # ClawTeam >= 0.2 switched to `workspace cleanup <team> --agent <name>`.
        # Keep a positional fallback for older installations.
        attempts: list[WorkspaceCleanupAttempt] = []
        argv = ["clawteam", "workspace", "cleanup", team, "--agent", agent]
        if repo_root:
            argv += ["--repo", repo_root]
        exit_code, stdout, stderr = await _run(argv, env=self._env())
        attempts.append(WorkspaceCleanupAttempt(
            argv=argv,
            exit_code=exit_code,
            stdout=stdout[:2000],
            stderr=stderr[:2000],
        ))
        if exit_code == 0:
            result = WorkspaceCleanupResult(success=True, attempts=attempts)
        elif "--agent" in stderr and "No such option" in stderr:
            legacy_argv = ["clawteam", "workspace", "cleanup", team, agent]
            if repo_root:
                legacy_argv += ["--repo", repo_root]
            legacy_exit, legacy_stdout, legacy_stderr = await _run(
                legacy_argv, env=self._env(),
            )
            attempts.append(WorkspaceCleanupAttempt(
                argv=legacy_argv,
                exit_code=legacy_exit,
                stdout=legacy_stdout[:2000],
                stderr=legacy_stderr[:2000],
            ))
            result = WorkspaceCleanupResult(
                success=(legacy_exit == 0),
                attempts=attempts,
            )
        else:
            result = WorkspaceCleanupResult(success=False, attempts=attempts)

        if result.success and repo_root:
            deleted = await asyncio.to_thread(
                delete_clawteam_agent_branch,
                repo_root,
                team=team,
                agent=agent,
                branch_name=branch_name,
            )
            if not deleted:
                self._log.warning(
                    "workspace_cleanup_branch_delete_failed",
                    team=team,
                    agent=agent,
                    repo=repo_root,
                    branch=branch_name,
                )
        return result

    # ──────────────────────────────────────────────────────────────────
    # Profile (read-only wrappers — MVP API.md §Profiles)
    # ──────────────────────────────────────────────────────────────────

    async def profile_list(self) -> dict[str, dict]:
        """``clawteam --json profile list`` → ``{name: profile_dict}``.

        Returns ``{}`` when no profile is configured (CLI emits ``{}`` then).
        """
        argv = ["clawteam", "--json", "profile", "list"]
        rc, stdout, stderr = await _run(argv, env=self._env())
        if rc != 0:
            raise CliInvocationError(argv=argv, exit_code=rc, stderr=stderr, stdout=stdout)
        parsed = _try_parse_json(stdout)
        return parsed if isinstance(parsed, dict) else {}

    async def profile_show(self, name: str) -> dict:
        """``clawteam --json profile show <name>``.

        Raises :class:`CliInvocationError` if the profile doesn't exist
        (CLI exits 1 with ``Profile '<x>' not found``).
        """
        argv = ["clawteam", "--json", "profile", "show", name]
        rc, stdout, stderr = await _run(argv, env=self._env())
        if rc != 0:
            raise CliInvocationError(argv=argv, exit_code=rc, stderr=stderr, stdout=stdout)
        parsed = _try_parse_json(stdout)
        return parsed if isinstance(parsed, dict) else {}

    async def profile_set(
        self,
        name: str,
        *,
        agent: str | None = None,
        description: str | None = None,
        command: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        base_url_env: str | None = None,
        api_key_env: str | None = None,
        api_key_target_env: str | None = None,
        envs: list[str] | None = None,
        env_maps: list[str] | None = None,
        args: list[str] | None = None,
    ) -> dict:
        """``clawteam profile set <name> ...`` — create or update a profile.

        Each kwarg maps 1-to-1 onto the CLI flag of the same name (with
        snake_case→kebab-case). ``envs`` / ``env_maps`` / ``args`` are
        passed as repeated options.

        Returns the updated profile dict from ``clawteam profile show``
        (so the API layer can echo it back to the FE without a second
        round-trip).
        """
        argv: list[str] = ["clawteam", "profile", "set", name]
        if agent is not None:
            argv += ["--agent", agent]
        if description is not None:
            argv += ["--description", description]
        if command is not None:
            argv += ["--command", command]
        if model is not None:
            argv += ["--model", model]
        if base_url is not None:
            argv += ["--base-url", base_url]
        if base_url_env is not None:
            argv += ["--base-url-env", base_url_env]
        if api_key_env is not None:
            argv += ["--api-key-env", api_key_env]
        if api_key_target_env is not None:
            argv += ["--api-key-target-env", api_key_target_env]
        for kv in envs or []:
            argv += ["--env", kv]
        for kv in env_maps or []:
            argv += ["--env-map", kv]
        for a in args or []:
            argv += ["--arg", a]
        rc, stdout, stderr = await _run(argv, env=self._env())
        if rc != 0:
            raise CliInvocationError(
                argv=argv, exit_code=rc, stderr=stderr, stdout=stdout,
            )
        # Re-read the profile so the FE renders the canonical post-set state.
        return await self.profile_show(name)

    async def profile_remove(self, name: str) -> None:
        """``clawteam profile remove <name>`` — drop a profile by name."""
        argv = ["clawteam", "profile", "remove", name]
        rc, _stdout, stderr = await _run(argv, env=self._env())
        if rc != 0:
            raise CliInvocationError(
                argv=argv, exit_code=rc, stderr=stderr,
            )

    async def profile_test(
        self, name: str, *, prompt: str | None = None, cwd: str | None = None,
    ) -> tuple[bool, str]:
        """Run ``clawteam profile test <name>``; returns (ok, combined_output).

        The CLI returns 0 + the agent's reply on success, non-zero on
        connection / model errors. We intentionally don't enforce a JSON
        format — different profile backends produce wildly different smoke
        outputs and the user just needs to see whether it ran.
        """
        argv = ["clawteam", "profile", "test", name]
        if prompt is not None:
            argv += ["--prompt", prompt]
        if cwd is not None:
            argv += ["--cwd", cwd]
        rc, stdout, stderr = await _run(argv, env=self._env())
        return rc == 0, (stdout or "") + (stderr or "")

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle (graceful shutdown of a worker)
    # ──────────────────────────────────────────────────────────────────

    async def lifecycle_request_shutdown(
        self,
        *,
        team: str,
        to_agent: str,
        from_agent: str = "csflow-scheduler",
        reason: str = "",
    ) -> None:
        argv = [
            "clawteam",
            "lifecycle",
            "request-shutdown",
            team,
            from_agent,
            to_agent,
        ]
        if reason:
            argv += ["--reason", reason]
        exit_code, _stdout, stderr = await _run(argv, env=self._env())
        if exit_code != 0:
            raise CliInvocationError(
                argv=argv, exit_code=exit_code, stderr=stderr,
            )

    async def tmux_kill_agent_windows(self, *, team: str, agent: str) -> int:
        """Best-effort hard stop for lingering tmux windows of one agent."""
        session_name = f"clawteam-{team}"
        list_argv = [
            "tmux",
            "list-windows",
            "-t",
            session_name,
            "-F",
            "#{window_index}:#{window_name}",
        ]
        exit_code, stdout, _stderr = await _run(list_argv, env=self._env())
        if exit_code != 0:
            return 0

        killed = 0
        for line in stdout.splitlines():
            raw = line.strip()
            if not raw:
                continue
            idx_s, _, name = raw.partition(":")
            if name != agent:
                continue
            try:
                idx = int(idx_s)
            except ValueError:
                continue
            kill_argv = ["tmux", "kill-window", "-t", f"{session_name}:{idx}"]
            k_rc, _k_out, _k_err = await _run(kill_argv, env=self._env())
            if k_rc == 0:
                killed += 1

        if killed == 0:
            fallback_argv = ["tmux", "kill-window", "-t", f"{session_name}:{agent}"]
            f_rc, _f_out, _f_err = await _run(fallback_argv, env=self._env())
            if f_rc == 0:
                killed = 1

        return killed

    # ──────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CLAWTEAM_USER"] = get_request_user() or self._cfg.default_user
        if self._cfg.clawteam_data_dir:
            env["CLAWTEAM_DATA_DIR"] = self._cfg.clawteam_data_dir
        return env


# ──────────────────────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────────────────────


def _enforce_anti_loop(args: _SpawnArgs) -> None:
    """Hard-fail if any of the 4 defences have been bypassed."""
    # Gate ① no --task — built into argv generation; assert here as belt-and-braces.
    if "--task" in args.extra_flags:
        raise AntiLoopViolation("--task is forbidden (would trigger build_agent_prompt loop)")
    # Gate ② no --skill clawteam (or any future banned skill).
    for s in args.skills:
        if s in BANNED_SKILLS:
            raise AntiLoopViolation(
                f"skill {s!r} is in BANNED_SKILLS (would inject self-polling protocol)"
            )
    # Gate ③ enforced by to_argv() unconditionally; nothing to check here.


def _cli_timeout_seconds() -> float:
    """Resolve the control-plane subprocess ceiling (env-overridable)."""
    raw = os.environ.get("CSFLOW_CLAWTEAM_CLI_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_CLI_TIMEOUT_SECONDS


def _resolve_argv(argv: list[str]) -> list[str]:
    """Pin ``clawteam`` to the same binary doctor/startup probes resolve.

    Prevents PATH drift between the dep-check binary and the one the
    scheduler actually invokes (e.g. systemd vs login-shell PATH). All other
    argv[0] values (git / tmux) go through normal PATH lookup unchanged.
    """
    if argv and argv[0] == "clawteam":
        from app.runtime_bins import resolve_binary
        pinned = resolve_binary("clawteam")
        if pinned:
            return [pinned, *argv[1:]]
    return argv


def _kill_proc_group(proc: asyncio.subprocess.Process) -> None:
    """Best-effort SIGKILL of the subprocess' own process group."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _exec_capture(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: str | None = None,
    timeout_sec: float | None = None,
) -> tuple[int, str, str]:
    """Spawn *argv*, capture output, and never leave a child behind.

    * **Timeout guard** (default :data:`DEFAULT_CLI_TIMEOUT_SECONDS`): a hung
      control-plane command is killed (whole process group) and reported as
      exit code :data:`CLI_TIMEOUT_EXIT_CODE`. This never applies to agent
      task execution — see the constant's docstring.
    * **Cancellation-safe**: if the awaiting task is cancelled (run abort /
      service drain), the subprocess group is killed before the cancellation
      propagates, so no orphan ``git``/``clawteam`` processes survive.
    """
    effective_timeout = timeout_sec if timeout_sec is not None else _cli_timeout_seconds()
    proc = await asyncio.create_subprocess_exec(
        *_resolve_argv(argv),
        env=env,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=effective_timeout,
        )
    except asyncio.TimeoutError:
        _kill_proc_group(proc)
        with contextlib.suppress(Exception):
            await proc.communicate()
        detail = (
            f"command timed out after {effective_timeout:.0f}s "
            "(control-plane CLI guard; see CSFLOW_CLAWTEAM_CLI_TIMEOUT_SEC)"
        )
        return CLI_TIMEOUT_EXIT_CODE, "", detail
    except asyncio.CancelledError:
        _kill_proc_group(proc)
        with contextlib.suppress(Exception):
            await proc.communicate()
        raise
    return (
        proc.returncode or 0,
        stdout_b.decode(errors="replace"),
        stderr_b.decode(errors="replace"),
    )


async def _run(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout_sec: float | None = None,
) -> tuple[int, str, str]:
    """Spawn *argv* and capture stdout / stderr / exit code."""
    return await _exec_capture(argv, env=env, timeout_sec=timeout_sec)


async def _run_in_cwd(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_sec: float | None = None,
) -> tuple[int, str, str]:
    # Defensive: expand ``~`` so a tilde cwd never reaches the (shell-less)
    # subprocess as a literal path. Covers git ops in _ensure_repo_on_target_branch
    # plus workspace merge / uncommitted-check cwds. See _expand_repo.
    return await _exec_capture(
        argv, env=env, cwd=_expand_repo(cwd), timeout_sec=timeout_sec,
    )


_AUTO_COMMIT_MESSAGE = "csflow auto commit"


async def _best_effort_git_fetch(
    *, repo: str, branch: str, env: dict[str, str],
) -> None:
    """Fetch ``origin/<branch>`` before the merge lock (network-only; no HEAD change)."""
    await _run_in_cwd(["git", "fetch", "origin", branch], cwd=repo, env=env)


async def _fast_forward_origin_branch_if_available(
    *,
    repo: str,
    branch: str,
    env: dict[str, str],
) -> None:
    """Fast-forward *branch* to ``origin/<branch>`` inside the merge lock.

    Single ``git merge --ff-only`` call (no prior ref probe). Failures are ignored —
    best-effort baseline sync, same spirit as the former in-lock ``git pull``.
    """
    await _run_in_cwd(
        ["git", "merge", "--ff-only", f"origin/{branch}"],
        cwd=repo,
        env=env,
    )


async def _auto_commit_if_dirty(
    *,
    repo: str,
    env: dict[str, str],
) -> bool:
    """Stage + commit any pending changes in ``repo`` (``csflow auto commit``).

    Returns ``True`` if a commit was created, ``False`` if the tree was already
    clean. Raises :class:`CliInvocationError` if any git step fails — the caller
    is about to create a worktree off this repo, so a half-known state must fail
    loudly rather than silently lose work.

    ``--no-verify`` is intentional: this is a scheduler-side bookkeeping commit
    whose only job is to capture the working tree before a ``git worktree add``;
    a repo-local pre-commit hook must not be able to block the spawn.
    """
    status_argv = ["git", "status", "--porcelain"]
    status_code, status_out, status_err = await _run_in_cwd(status_argv, cwd=repo, env=env)
    if status_code != 0:
        raise CliInvocationError(
            argv=status_argv, exit_code=status_code, stderr=status_err, stdout=status_out,
        )
    if not status_out.strip():
        return False  # clean tree — nothing to commit

    add_argv = ["git", "add", "-A"]
    add_code, add_out, add_err = await _run_in_cwd(add_argv, cwd=repo, env=env)
    if add_code != 0:
        raise CliInvocationError(
            argv=add_argv, exit_code=add_code, stderr=add_err, stdout=add_out,
        )

    # After ``git add -A`` the index may still be empty (e.g. only ignored files
    # changed). ``git diff --cached --quiet`` → rc 0 == nothing staged, rc 1 ==
    # staged changes present; any other rc is a real error.
    staged_argv = ["git", "diff", "--cached", "--quiet"]
    staged_code, staged_out, staged_err = await _run_in_cwd(staged_argv, cwd=repo, env=env)
    if staged_code == 0:
        return False  # nothing actually staged — skip empty commit
    if staged_code != 1:
        raise CliInvocationError(
            argv=staged_argv, exit_code=staged_code, stderr=staged_err, stdout=staged_out,
        )

    commit_argv = ["git", "commit", "--no-verify", "-m", _AUTO_COMMIT_MESSAGE]
    commit_code, commit_out, commit_err = await _run_in_cwd(commit_argv, cwd=repo, env=env)
    if commit_code != 0:
        raise CliInvocationError(
            argv=commit_argv, exit_code=commit_code, stderr=commit_err, stdout=commit_out,
        )
    return True


async def _ensure_repo_on_target_branch(
    *,
    repo: str,
    target_branch: str | None,
    env: dict[str, str],
) -> tuple[str, bool]:
    """Prepare ``repo`` for a ``git worktree add``.

    Before a worktree is created we (1) auto-commit any pending changes so the
    new worktree branches off a committed state that includes the latest work,
    and (2) — when ``target_branch`` is given — guarantee the main repo is on
    that branch. Returns ``(previous_branch, switched)``.

    All scheduler-side commits use the message ``csflow auto commit``.
    """
    target = (target_branch or "").strip()

    # Current branch (best-effort): symbolic-ref fails on a detached HEAD — that
    # is fine, we still commit and (if a target is given) check it out below.
    current_argv = ["git", "symbolic-ref", "--quiet", "--short", "HEAD"]
    code, out, _err = await _run_in_cwd(current_argv, cwd=repo, env=env)
    current = out.strip() if code == 0 else ""

    # 1. Commit pending work first: a dirty tree would otherwise block the
    #    checkout, and the worktree must capture the latest state.
    await _auto_commit_if_dirty(repo=repo, env=env)

    # 2. No switch needed when there is no target branch (e.g. OpenClaw, which
    #    relies on session-id not branches) or we are already on it.
    if not target or current == target:
        return current, False

    exists_argv = ["git", "show-ref", "--verify", f"refs/heads/{target}"]
    exists_code, exists_out, exists_err = await _run_in_cwd(exists_argv, cwd=repo, env=env)
    if exists_code != 0:
        list_argv = ["git", "branch", "--format=%(refname:short)"]
        _lc, branches_out, _le = await _run_in_cwd(list_argv, cwd=repo, env=env)
        known = ", ".join([line.strip() for line in branches_out.splitlines() if line.strip()])
        detail = f"target branch {target!r} not found"
        if known:
            detail = f"{detail}; known branches: {known}"
        raise CliInvocationError(
            argv=exists_argv,
            exit_code=exists_code,
            stderr=detail,
            stdout=exists_out,
        )

    # Tree is clean now (step 1), so the checkout is safe.
    checkout_argv = ["git", "checkout", target]
    checkout_code, checkout_out, checkout_err = await _run_in_cwd(checkout_argv, cwd=repo, env=env)
    if checkout_code != 0:
        raise CliInvocationError(
            argv=checkout_argv,
            exit_code=checkout_code,
            stderr=checkout_err,
            stdout=checkout_out,
        )

    # Defensive: commit anything that surfaced on the target branch (normally a
    # no-op after a clean checkout).
    await _auto_commit_if_dirty(repo=repo, env=env)
    return current, True


def _try_parse_json(stdout: str) -> dict | list | None:
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_singleton: ClawTeamCli | None = None


def get_clawteam_cli() -> ClawTeamCli:
    """Return the process-wide :class:`ClawTeamCli`."""
    global _singleton
    if _singleton is not None:
        return _singleton
    _singleton = ClawTeamCli()
    return _singleton


def reset_clawteam_cli() -> None:
    """Drop the cached singleton (used by tests)."""
    global _singleton
    _singleton = None


__all__ = [
    "AntiLoopViolation",
    "BANNED_SKILLS",
    "CLI_TIMEOUT_EXIT_CODE",
    "ClawTeamCli",
    "CliInvocationError",
    "DEFAULT_CLI_TIMEOUT_SECONDS",
    "SpawnResult",
    "WorkspaceCleanupAttempt",
    "WorkspaceCleanupResult",
    "get_clawteam_cli",
    "reset_clawteam_cli",
]
