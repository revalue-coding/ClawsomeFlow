"""Flow consistency validation.

These checks live outside :mod:`app.models` so they can be exercised
independently (no DB roundtrip needed for the pure-spec subset) and so the
API layer can map them to user-facing error codes (see API.md "Errors").

Two entry points:
* :func:`validate_flow_spec` — pure: agent ids unique, leader unique, DAG
  acyclic, task references valid, leader_summary present.
* :func:`validate_flow_against_db` — adds storage-aware checks: OpenClaw
  agent ids exist; non-OpenClaw repo paths are valid git repositories.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.models import AgentKind, FlowSpec

if TYPE_CHECKING:  # avoid circular import; storage layer pulls models
    from app.storage import StorageBackend


# Error codes mirror API.md "Errors" table; keep in sync if extended.
ERROR_INVALID_DAG = "INVALID_DAG"
ERROR_INVALID_AGENT_REF = "INVALID_AGENT_REF"
ERROR_INVALID_LEADER = "INVALID_LEADER"
ERROR_MISSING_LEADER_SUMMARY = "MISSING_LEADER_SUMMARY"
ERROR_OPENCLAW_AGENT_NOT_FOUND = "OPENCLAW_AGENT_NOT_FOUND"
ERROR_HERMES_AGENT_NOT_FOUND = "HERMES_AGENT_NOT_FOUND"
ERROR_MISSING_AGENT_REPO = "MISSING_AGENT_REPO"
ERROR_INVALID_REPO = "INVALID_REPO"
ERROR_DUPLICATE_AGENT_ID = "DUPLICATE_AGENT_ID"
ERROR_DUPLICATE_TASK_ID = "DUPLICATE_TASK_ID"
ERROR_TASK_OWNS_NOTHING = "TASK_OWNS_NOTHING"
ERROR_LEADER_OWNS_WORKER_TASK = "LEADER_OWNS_WORKER_TASK"
ERROR_SUMMARY_NO_DEPENDENCY = "SUMMARY_NO_DEPENDENCY"


@dataclass(slots=True)
class FlowValidationError(Exception):
    """Raised when a Flow violates a business invariant.

    Attributes:
        code: Stable string identifier (matches API.md error codes).
        message: Human-readable description.
        details: Optional structured context (e.g. cycle nodes, missing ids).
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


# ──────────────────────────────────────────────────────────────────────
# Pure validation (no I/O)
# ──────────────────────────────────────────────────────────────────────


def validate_flow_spec(spec: FlowSpec) -> None:
    """Validate a :class:`FlowSpec` against pure structural invariants.

    Raises :class:`FlowValidationError` on the first failure (fail-fast so the
    user sees the most fundamental issue first).
    """
    _check_unique_agent_ids(spec)
    _check_unique_task_ids(spec)
    _check_leader(spec)
    _check_task_references(spec)
    _check_leader_summary(spec)
    _check_dag_acyclic(spec)


def _check_unique_agent_ids(spec: FlowSpec) -> None:
    # A FlowAgent.id must be globally unique within a Flow, regardless of
    # platform: every downstream key (the controller's worker-session map, the
    # ``clawteam-<team>:<id>`` tmux window, the ClawTeam member name, ``hermes -p
    # <id>``) is keyed by id ALONE — kind never participates. So two genuinely
    # distinct agents that merely share a name across platforms (e.g. an OpenClaw
    # ``alice`` and a Hermes ``alice``) still collide and must be rejected. We
    # track the first-seen kind so the cross-platform case gets a message that
    # actually tells the user *why* (the raw "appears more than once" reads as a
    # bug when the two pickers clearly showed two different agents).
    seen: dict[str, AgentKind] = {}
    for a in spec.agents:
        prev_kind = seen.get(a.id)
        if prev_kind is not None:
            if prev_kind != a.kind:
                raise FlowValidationError(
                    ERROR_DUPLICATE_AGENT_ID,
                    (
                        f"agent id {a.id!r} is used by two different platforms "
                        f"({prev_kind.value} and {a.kind.value}); a Flow agent id "
                        f"must be globally unique across platforms — rename one. / "
                        f"Agent id {a.id!r} 被两个不同平台（{prev_kind.value} 与 "
                        f"{a.kind.value}）同时使用；同一 Flow 内 Agent id 必须跨平台全局"
                        f"唯一，请将其中一个改名。"
                    ),
                    {"agent_id": a.id, "kinds": [prev_kind.value, a.kind.value]},
                )
            raise FlowValidationError(
                ERROR_DUPLICATE_AGENT_ID,
                (
                    f"agent id {a.id!r} appears more than once. / "
                    f"Agent id {a.id!r} 重复出现。"
                ),
                {"agent_id": a.id},
            )
        seen[a.id] = a.kind


def _check_unique_task_ids(spec: FlowSpec) -> None:
    seen: set[str] = set()
    for t in spec.tasks:
        if t.id in seen:
            raise FlowValidationError(
                ERROR_DUPLICATE_TASK_ID,
                f"task id {t.id!r} appears more than once",
                {"task_id": t.id},
            )
        seen.add(t.id)


def _check_leader(spec: FlowSpec) -> None:
    leaders = [a for a in spec.agents if a.is_leader]
    if not leaders:
        raise FlowValidationError(
            ERROR_INVALID_LEADER,
            "Flow must have exactly one agent with is_leader=true (got 0)",
            {"leader_count": 0},
        )
    if len(leaders) > 1:
        raise FlowValidationError(
            ERROR_INVALID_LEADER,
            f"Flow must have exactly one leader (got {len(leaders)})",
            {"leader_count": len(leaders), "leader_ids": [a.id for a in leaders]},
        )


def _check_task_references(spec: FlowSpec) -> None:
    agent_ids = {a.id for a in spec.agents}
    task_ids = {t.id for t in spec.tasks}
    for t in spec.tasks:
        if t.owner_agent_id not in agent_ids:
            raise FlowValidationError(
                ERROR_INVALID_AGENT_REF,
                f"task {t.id!r} owner_agent_id={t.owner_agent_id!r} not found",
                {"task_id": t.id, "owner_agent_id": t.owner_agent_id},
            )
        for dep in t.depends_on:
            if dep not in task_ids:
                raise FlowValidationError(
                    ERROR_INVALID_AGENT_REF,
                    f"task {t.id!r} depends_on={dep!r} which doesn't exist",
                    {"task_id": t.id, "missing_dependency": dep},
                )


def _check_leader_summary(spec: FlowSpec) -> None:
    """Validate leader-summary contract + leader-only-does-summary constraint.

    Three rules:
    1. ≥1 ``is_leader_summary=true`` task exists and is owned by the leader.
    2. **The leader owns ONLY leader-summary tasks** — no other (worker) task
       may have the leader as owner. This keeps semantics clean (workers
       inbox-send the leader; if the leader were also a worker we'd have
       self-send-self) and matches plan §8.6 which spawns the leader only at
       the end (so the leader cannot be running an earlier worker task).
    3. **Each summary task has ≥1 dependency.** The summary's job is to review
       and report on upstream worker outputs, so it must point at the tasks it
       reviews; a dependency-less summary would have nothing to summarise. (The
       scheduler additionally waits for *all* tasks before dispatching the
       summary, but the configured deps still select which outputs feed it.)
    """
    leader = next(a for a in spec.agents if a.is_leader)
    summary_tasks = [t for t in spec.tasks if t.is_leader_summary]
    if not summary_tasks:
        raise FlowValidationError(
            ERROR_MISSING_LEADER_SUMMARY,
            "Flow must have at least one task with is_leader_summary=true",
            {},
        )
    for t in summary_tasks:
        if t.owner_agent_id != leader.id:
            raise FlowValidationError(
                ERROR_INVALID_AGENT_REF,
                f"leader_summary task {t.id!r} must be owned by the leader "
                f"{leader.id!r} (got owner={t.owner_agent_id!r})",
                {"task_id": t.id, "expected_owner": leader.id, "actual_owner": t.owner_agent_id},
            )
    # Rule 3: a summary must depend on at least one upstream task to review.
    for t in summary_tasks:
        if not [d for d in t.depends_on if str(d).strip()]:
            raise FlowValidationError(
                ERROR_SUMMARY_NO_DEPENDENCY,
                f"leader_summary task {t.id!r} must have at least one dependency "
                "(the upstream task(s) it reviews and reports on)",
                {"task_id": t.id},
            )
    # Rule 2: leader cannot also be a worker.
    leader_worker_tasks = [
        t for t in spec.tasks
        if t.owner_agent_id == leader.id and not t.is_leader_summary
    ]
    if leader_worker_tasks:
        raise FlowValidationError(
            ERROR_LEADER_OWNS_WORKER_TASK,
            f"leader {leader.id!r} cannot own non-summary task(s) "
            f"{[t.id for t in leader_worker_tasks]!r} — leaders only own "
            "is_leader_summary tasks",
            {
                "leader_id": leader.id,
                "task_ids": [t.id for t in leader_worker_tasks],
            },
        )


def _check_dag_acyclic(spec: FlowSpec) -> None:
    """Detect cycles via 3-colour DFS; report the first cycle found."""
    graph: dict[str, list[str]] = {t.id: list(t.depends_on) for t in spec.tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[str, int] = {tid: WHITE for tid in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if colour[nxt] == GRAY:
                cycle = stack[stack.index(nxt):] + [nxt]
                return cycle
            if colour[nxt] == WHITE:
                cycle = visit(nxt)
                if cycle is not None:
                    return cycle
        colour[node] = BLACK
        stack.pop()
        return None

    for tid in graph:
        if colour[tid] == WHITE:
            cycle = visit(tid)
            if cycle is not None:
                raise FlowValidationError(
                    ERROR_INVALID_DAG,
                    f"task dependencies contain a cycle: {' -> '.join(cycle)}",
                    {"cycle": cycle},
                )


# ──────────────────────────────────────────────────────────────────────
# DB / filesystem-aware validation
# ──────────────────────────────────────────────────────────────────────


def validate_flow_against_db(spec: FlowSpec, storage: "StorageBackend") -> None:
    """Validate :class:`FlowSpec` plus storage-aware references.

    1. Run pure :func:`validate_flow_spec` first.
    2. Each non-OpenClaw agent must have ``repo`` set and pointing at a real
       git repo with at least one commit (required by ``git worktree add``).
    3. Each ``kind=openclaw`` agent's ``id`` must exist in the OpenclawAgent table.
    4. Each ``kind=hermes`` agent's ``id`` must exist in the HermesAgent table
       (Hermes agents must be created from the management module, never ad-hoc
       in the Flow editor) AND still requires a working-directory ``repo``.
    """
    validate_flow_spec(spec)
    # Keep DB index in sync with managed openclaw.json entries before checking refs.
    from app.services.openclaw_agents import reindex_registered_agents
    reindex_registered_agents(storage=storage)
    for a in spec.agents:
        if a.kind == AgentKind.openclaw:
            existing = storage.openclaw_get(a.id)
            if existing is None:
                raise FlowValidationError(
                    ERROR_OPENCLAW_AGENT_NOT_FOUND,
                    f"agent {a.id!r}: kind=openclaw, but no OpenclawAgent with "
                    f"that id is registered in ClawsomeFlow",
                    {"agent_id": a.id},
                )
        else:
            # Temporary (ad-hoc) agents are not registered in any managed store,
            # so skip the existence lookups for them — they still need a valid
            # working-directory ``repo`` (checked below). OpenClaw cannot be
            # temporary (rejected at the model level). Claude/Codex/Cursor have no
            # persistent management platform, so they are only ever ad-hoc (no
            # existence check) — just like Cursor.
            if (
                a.kind == AgentKind.hermes
                and not a.is_temporary
                and storage.hermes_get(a.id) is None
            ):
                raise FlowValidationError(
                    ERROR_HERMES_AGENT_NOT_FOUND,
                    f"agent {a.id!r}: kind=hermes, but no HermesAgent with that "
                    "id is registered in ClawsomeFlow. Create the Hermes agent "
                    "in the management module first.",
                    {"agent_id": a.id},
                )
            if not a.repo:
                raise FlowValidationError(
                    ERROR_MISSING_AGENT_REPO,
                    f"agent {a.id!r} (kind={a.kind.value}): 'repo' is required for "
                    "non-OpenClaw agents",
                    {"agent_id": a.id, "kind": a.kind.value},
                )
            repo_path = Path(a.repo).expanduser()
            if not repo_path.exists():
                raise FlowValidationError(
                    ERROR_INVALID_REPO,
                    f"agent {a.id!r}: repo path {a.repo!r} does not exist",
                    {
                        "agent_id": a.id,
                        "repo": a.repo,
                        "reason": "path_not_found",
                        "path_exists": False,
                        "is_git_repo": False,
                        "has_initial_commit": False,
                    },
                )
            if not repo_path.is_dir():
                raise FlowValidationError(
                    ERROR_INVALID_REPO,
                    f"agent {a.id!r}: repo path {a.repo!r} is not a directory",
                    {
                        "agent_id": a.id,
                        "repo": a.repo,
                        "reason": "not_directory",
                        "path_exists": True,
                        "is_git_repo": False,
                        "has_initial_commit": False,
                    },
                )
            if not (repo_path / ".git").exists():
                raise FlowValidationError(
                    ERROR_INVALID_REPO,
                    f"agent {a.id!r}: repo {a.repo!r} does not appear to be a git "
                    "repository (no .git directory found)",
                    {
                        "agent_id": a.id,
                        "repo": a.repo,
                        "reason": "not_git_repo",
                        "path_exists": True,
                        "is_git_repo": False,
                        "has_initial_commit": False,
                    },
                )
            if not _repo_has_initial_commit(repo_path):
                raise FlowValidationError(
                    ERROR_INVALID_REPO,
                    f"agent {a.id!r}: repo {a.repo!r} has no initial commit yet; "
                    "create a first commit before spawning worktrees",
                    {
                        "agent_id": a.id,
                        "repo": a.repo,
                        "reason": "no_initial_commit",
                        "path_exists": True,
                        "is_git_repo": True,
                        "has_initial_commit": False,
                    },
                )


def _repo_has_initial_commit(repo_path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True
