"""AI-driven task-decomposition orchestrator.

End-user flow (per the "AI decompose task" feature):

    POST /api/flows/decompose
       1. resolve leader dispatch target (OpenClaw or non-OpenClaw)
       2. mint short-lived HMAC token (30 min, purpose=task_decompose)
       3. INSERT TaskDecomposeRequest(status=pending, expiresAt)
       4. dispatch decomposition prompt to leader:
          - OpenClaw leader: openclaw CLI + gateway bridge fallback
          - non-OpenClaw leader: direct one-shot CLI session (no team/tmux)
            that returns JSON on stdout (server parses + validates directly)
       5. OpenClaw leader POSTs result to /api/internal/task-decompose/commit
    GET  /api/flows/decompose/{request_id}
       polled until status ∈ {succeeded, failed, timed_out}; the
       front-end then renders ``result_tasks`` + ``result_agents``.

The dispatch prompt is fully self-contained for BOTH leader kinds (there is
no longer a ``csflow-task-decomposer`` OpenClaw skill — every instruction,
including how to return the result, lives in the prompt). The only difference
between the two prompts is the trailing "how to return your result" section:
an OpenClaw leader curl-POSTs the JSON back to ``/api/internal/task-decompose/
commit`` (its stdout is not read), while a non-OpenClaw leader prints one JSON
object to stdout which the server captures and parses directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.config import Config, load_config
from app.integrations import internal_token as it
from app.integrations.openclaw_cli import resolve_openclaw_executable
from app.integrations.openclaw_install import (
    looks_like_pending_scope_approval,
    repair_pending_scope_upgrades,
)
from app.integrations.openclaw_bridge import (
    OpenclawBridge,
    OpenclawBridgeError,
)
from app.logging_setup import get_logger
from app.models import (
    DEFAULT_TARGET_BRANCH,
    AgentKind,
    TaskDecomposeRequest,
    TaskDecomposeStatus,
)
from app.services.task_decompose_validation import (
    ProposalValidationError,
    validate_decompose_proposal,
)
from app.storage import StorageBackend, get_storage

logger = get_logger("svc.task_decompose")

_REQUEST_TTL_SECONDS = 1800  # 30 minutes
_PURPOSE = "task_decompose"
_DEFAULT_OPENCLAW_CLI_TIMEOUT_SEC = 1800.0
_MIN_OPENCLAW_CLI_TIMEOUT_SEC = 1800.0
_OPENCLAW_CLI_TIMEOUT_ENV = "CSFLOW_OPENCLAW_CLI_TIMEOUT_SECONDS"
# Keep non-OpenClaw one-shot sessions within request TTL by default.
_DEFAULT_NON_OPENCLAW_CLI_TIMEOUT_SEC = float(_REQUEST_TTL_SECONDS)
_MIN_NON_OPENCLAW_CLI_TIMEOUT_SEC = 1800.0
_NON_OPENCLAW_CLI_TIMEOUT_ENV = "CSFLOW_NON_OPENCLAW_CLI_TIMEOUT_SECONDS"
_DECOMPOSE_CANCEL_RESET_TIMEOUT_SEC = 30.0

_INFLIGHT_DISPATCH_TASKS: dict[str, asyncio.Task[None]] = {}
_START_REQUEST_LOCK = asyncio.Lock()

_NON_OPENCLAW_SUPPORTED_KINDS: frozenset[AgentKind] = frozenset({
    AgentKind.claude,
    AgentKind.codex,
    AgentKind.cursor,
    AgentKind.hermes,
})

# Default working directory for AI-assigned temporary (ad-hoc) agents. The AI
# decomposer points every temporary agent it invents at this base repo (a
# temporary agent's platform can never be OpenClaw). It is created + git-inited
# idempotently (see ``_ensure_ai_temp_agent_workdir``) so ClawTeam can base
# worktrees on it at run time.
_AI_TEMP_AGENT_WORKDIR = "~/csflow-ai-decompose"

# Candidate temporary-agent platforms (non-OpenClaw) and their CLI binary name,
# mirrored from ``cli/deps.py::_NON_OPENCLAW_AGENT_TOOLS``. We probe these with
# ``shutil.which`` so the prompt only advertises platforms actually installed on
# this host. Order is preserved in the rendered list.
_TEMP_AGENT_PLATFORM_BINARIES: tuple[tuple[AgentKind, str], ...] = (
    (AgentKind.claude, "claude"),
    (AgentKind.codex, "codex"),
    (AgentKind.cursor, "agent"),
    (AgentKind.hermes, "hermes"),
)


def _detect_temp_agent_platforms() -> list[str]:
    """Return the temporary-agent platforms whose CLI is installed on PATH."""
    available: list[str] = []
    for kind, binary in _TEMP_AGENT_PLATFORM_BINARIES:
        if shutil.which(binary):
            available.append(kind.value)
    return available


def _platform_lines(platforms: list[str]) -> str:
    if not platforms:
        return "  (none installed)"
    return "\n".join(f"  - {p}" for p in platforms)


def _ensure_ai_temp_agent_workdir() -> str:
    """Idempotently create ``~/csflow-ai-decompose`` as a git repo with a commit.

    Mirrors ``openclaw_agents._git_init_workspace``: a temporary agent spawns via
    ``clawteam spawn --workspace --repo <dir>`` which needs ``<dir>`` to be a git
    repo with at least one commit (no-commit repos can't be branched). Fully
    best-effort — any failure is logged and swallowed so it can never block a
    decomposition. Returns the unexpanded literal for the prompt.
    """
    path = os.path.expanduser(_AI_TEMP_AGENT_WORKDIR)
    try:
        os.makedirs(path, exist_ok=True)
        if not os.path.isdir(os.path.join(path, ".git")):
            def _run(cmd: list[str]) -> None:
                subprocess.run(cmd, cwd=path, check=True, capture_output=True)

            _run(["git", "init", "-b", "main"])
            _run(["git", "config", "user.email", "csflow@local"])
            _run(["git", "config", "user.name", "ClawsomeFlow"])
            marker = os.path.join(path, ".csflow-keep")
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(
                    "Default working directory for ClawsomeFlow AI-assigned "
                    "temporary agents. Worktree branches live under "
                    "~/.clawteam/workspaces/.\n"
                )
            _run(["git", "add", "-A"])
            _run(["git", "commit", "-m", "[csflow] ai-decompose workspace initial commit"])
    except Exception as exc:  # best-effort: never block decomposition
        logger.warning(
            "decompose_temp_workdir_init_failed",
            path=path,
            error=_error_text(exc),
        )
    return _AI_TEMP_AGENT_WORKDIR


# ──────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────


class TaskDecomposeError(Exception):
    """Service-layer error mapped to ``ApiError`` by the API exception handler."""

    code: str = "TASK_DECOMPOSE_ERROR"
    status_code: int = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class LeaderAgentNotFound(TaskDecomposeError):
    code = "OPENCLAW_AGENT_NOT_FOUND"
    status_code = 404


class LeaderAgentForbidden(TaskDecomposeError):
    code = "FORBIDDEN"
    status_code = 403


class LeaderContextMissing(TaskDecomposeError):
    code = "LEADER_CONTEXT_MISSING"
    status_code = 400


class LeaderKindUnsupported(TaskDecomposeError):
    code = "LEADER_KIND_UNSUPPORTED"
    status_code = 400


# ──────────────────────────────────────────────────────────────────────
# Result type returned to the API caller after kicking off
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DecomposeStartResult:
    request_id: str
    token_ttl_seconds: int
    status: TaskDecomposeStatus


@dataclass(frozen=True)
class _LeaderTarget:
    id: str
    kind: AgentKind
    repo: str | None = None
    target_branch: str | None = None
    # A temporary (ad-hoc) Hermes leader has NO managed profile — it must run
    # under Hermes' default profile (no ``-p <id>``), exactly like temporary
    # Claude/Codex agents. The id is only a ClawTeam marker, not a profile name.
    is_temporary: bool = False


# ──────────────────────────────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────────────────────────────


# Shared core sent to BOTH leader kinds. The only per-kind difference is the
# trailing "how to return your result" section (see _DELIVERY_* below).
_DECOMPOSE_CORE = """\
## ClawsomeFlow Task Decomposition Request

You are a Flow designer dispatched by ClawsomeFlow to decompose one high-level
Flow goal into a scheduler-ready DAG of tasks for the user to review.

## Runtime variables for this request

- request_id: {request_id}
- user: {user}
- leader_agent_id (you): {leader_agent_id}  (the `isLeaderSummary` task must be owned by you)
- leader_kind: {leader_kind}
- leader_repo: {leader_repo}
- leader_target_branch: {leader_target_branch}
- ClawsomeFlow API base: {api_base}
- short-lived callback token: {token} (valid for 30 minutes)
- required output language for task `subject` + `description`: {result_language}

## User's Flow goal (keep verbatim context)

{goal}

## Owner sources — every task owner is ONE of these two kinds

ClawsomeFlow assigns each task to either a **persistent agent** (a managed agent
that already exists on one of the user's persistent platforms) or a **temporary
agent** (an ad-hoc agent you define inline just for this Flow).

### 1) Persistent agents you may assign as task owners

These are the user's existing persistent agents (platforms: OpenClaw + Hermes).
**Always prefer assigning a worker task to one of these when one fits.** When you
reuse a persistent agent, its `agents[]` entry must set `kind` to that agent's
platform (`openclaw` or `hermes`) and `isTemporary: false`.
If a worker task is assigned to a non-OpenClaw persistent agent, set its
`repo` to `{temp_agent_workdir}` by default unless the goal clearly needs another path.

{persistent_agents_yaml}

### 2) Temporary-agent platforms available on this host

If — and only if — no persistent agent above is a clean fit for a worker task,
define a **temporary agent** to own it. A temporary agent's platform must be one
of the agent CLIs actually installed on this host (listed below). **OpenClaw can
NEVER be a temporary agent.**

{available_platforms_yaml}

For every temporary agent you define, its `agents[]` entry must set:
- `kind`: one of the platforms listed above (never `openclaw`)
- `isTemporary`: true
- `repo`: `{temp_agent_workdir}` (default working directory for AI-assigned
  temporary agents) unless the goal clearly requires another path
- `targetBranch`: `main` unless the goal says otherwise
- `id`: a fresh unique id not used by any persistent agent

## Existing agents in editor (already drafted by the user; treat as hints)

{existing_agents_yaml}

## Existing tasks in editor (reference only; you may improve/reorder/replace)

{existing_tasks_yaml}

## Required output schema

Produce one JSON object with two arrays:

- `agents`: each item has `id`, `kind`, optional `repo`, optional `targetBranch`,
  `isTemporary`, `isLeader`.
- `tasks`: each item has `id`, `ownerAgentId`, `subject` (<=80 chars),
  `description` (1-3 sentences), `dependsOn` (array of task ids), `isLeaderSummary`
  (boolean), optional `timeoutSeconds` (default 1800).

Invariants (the server rejects violations):
1. Exactly one task has `isLeaderSummary: true`, owned by `{leader_agent_id}`.
2. The leader owns no other (non-summary) task.
3. Task ids and agent ids are unique; `dependsOn` references resolve; the DAG is acyclic.
4. Owner kinds limited to `openclaw`, `claude`, `codex`, `cursor`, or `hermes`.
5. Every agent in `agents` is referenced by at least one task.
6. All task `subject` and `description` text is in {result_language}.

## Owner assignment policy (in priority order)

a. Reuse a *persistent agent* from section 1 **only when it genuinely fits** the
   task (set `isTemporary: false`). Never force an ill-suited persistent agent
   onto a task just to reuse one.
b. If no persistent agent fits, you MUST define a *temporary agent* per section 2
   (set `isTemporary: true`, `kind` != `openclaw`, `repo` = `{temp_agent_workdir}`).
   Do NOT leave a worker task's `ownerAgentId` empty.
c. Only if NO temporary platform is available above and no persistent agent fits,
   set `ownerAgentId` to an empty string `""` for the user to pick manually
   (last-resort fallback).
Never invent a new OpenClaw/Hermes persistent agent the user does not have.\
"""


# OpenClaw leader: its stdout is NOT read by the server — it must POST the
# result back to the loopback API. Literal ``{{``/``}}`` survive ``.format``.
_DELIVERY_OPENCLAW = """\
## How to return your result (REQUIRED — you must POST it back)

The ClawsomeFlow API is loopback-only. The `api_base` and `token` above are
literal values you substitute yourself (they are NOT shell environment vars).
Use the bash tool to write the JSON body to a temp file and curl it back, so
quotes inside `description` don't break:

    cat > /tmp/csflow-decompose-result.json <<'EOF'
    {{
      "request_id": "{request_id}",
      "agents": [ /* your agents */ ],
      "tasks":  [ /* your tasks  */ ]
    }}
    EOF

    curl -fsSL -X POST "{api_base}/api/internal/task-decompose/commit" \\
      -H "Authorization: Bearer {token}" \\
      -H "Content-Type: application/json" \\
      --data @/tmp/csflow-decompose-result.json

The endpoint returns {{"request_id": "..."}} on success. Do NOT tell the user
"generated N tasks" until curl has exited 0. If you genuinely cannot satisfy the
goal (ambiguous / contradictory / out of scope), POST the failure instead:

    curl -fsSL -X POST "{api_base}/api/internal/task-decompose/fail" \\
      -H "Authorization: Bearer {token}" \\
      -H "Content-Type: application/json" \\
      -d '{{"request_id": "{request_id}", "code": "INSUFFICIENT_INPUT", "message": "<one sentence>"}}'

Hard rules: never call any URL other than the api_base above; never modify
openclaw.json or other agents' workspaces; never start an actual Flow Run — your
job ends at posting the JSON back. Then reply in one short sentence: generated N
tasks for user review.\
"""


# Appended ONLY when the leader is a *temporary* Hermes agent. Such a leader runs
# under the operator's default Hermes profile, whose personal SOUL.md / memory
# tends to make it "reuse the agents the user already has" and refuse to invent
# ad-hoc agents — so it returns a DAG that assigns every task to a pre-existing
# persistent agent. This directive forces it back onto the owner-assignment
# policy. Kept short and blunt on purpose so it survives a long personal persona.
_TEMP_HERMES_LEADER_DIRECTIVE = """\
## OVERRIDE — read this last, it wins over your persona/memory

For THIS request you are a neutral Flow designer, NOT the user's personal
assistant. Disregard any habit, memory, or preference to "reuse the agents the
user already has". Apply the Owner assignment policy literally and strictly:

- Reuse a persistent agent from section 1 ONLY when it is a genuine fit for the
  task. Do NOT force an unrelated/ill-suited persistent agent onto a task.
- Whenever no persistent agent is a clean fit, you MUST define a NEW temporary
  agent for that task (section 2: `isTemporary: true`, `kind` != `openclaw`,
  `repo` = the temporary working directory).
- It is WRONG to return a DAG where every task is owned by a pre-existing
  persistent agent unless each of those agents is genuinely the best fit.\
"""


# Non-OpenClaw leader: a one-shot CLI whose stdout the server captures + parses.
_DELIVERY_STDOUT = """\
## How to return your result (REQUIRED)

1. Do **not** execute shell commands, do **not** write files, and do **not**
   call curl — permissions may block these and your result is read from stdout.
2. Respond with **exactly one JSON object and nothing else** — no prose, no
   markdown fences — in this exact shape:

       {{
         "agents": [ /* your agents */ ],
         "tasks":  [ /* your tasks  */ ]
       }}\
"""


def _yaml_lines(items: list[dict[str, Any]]) -> str:
    if not items:
        return "  (none)"
    out: list[str] = []
    for it_ in items:
        # Render as a short YAML-ish list — agent: id/kind/repo, task: id/owner/subject.
        out.append(
            "  - " + ", ".join(f"{k}={v}" for k, v in it_.items() if v not in (None, ""))
        )
    return "\n".join(out)


def _compose_messages(
    *, request_id: str, user: str, goal: str, leader_target: _LeaderTarget,
    api_base: str, token: str,
    result_language: str | None,
    persistent_agents: list[dict[str, Any]],
    available_platforms: list[str],
    temp_workdir: str,
    existing_agents: list[dict[str, Any]],
    existing_tasks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    language = {
        "zh": "Chinese",
        "en": "English",
    }.get((result_language or "").lower(), "same language as the user's goal")
    is_openclaw = leader_target.kind == AgentKind.openclaw
    core = _DECOMPOSE_CORE.format(
        request_id=request_id,
        user=user,
        leader_agent_id=leader_target.id,
        leader_kind=leader_target.kind.value,
        leader_repo=leader_target.repo or "",
        leader_target_branch=leader_target.target_branch or DEFAULT_TARGET_BRANCH,
        api_base=api_base,
        token=token,
        result_language=language,
        goal=goal.strip(),
        temp_agent_workdir=temp_workdir,
        persistent_agents_yaml=_yaml_lines(persistent_agents),
        available_platforms_yaml=_platform_lines(available_platforms),
        existing_agents_yaml=_yaml_lines(existing_agents),
        existing_tasks_yaml=_yaml_lines(existing_tasks),
    )
    # The ONLY per-leader-kind difference: how the result is returned. OpenClaw's
    # stdout is not read (it must curl the result back), while a non-OpenClaw
    # one-shot CLI returns one JSON object on stdout that the server parses.
    delivery_template = _DELIVERY_OPENCLAW if is_openclaw else _DELIVERY_STDOUT
    delivery = delivery_template.format(
        request_id=request_id, api_base=api_base, token=token,
    )
    body = f"{core}\n\n{delivery}"
    # A temporary Hermes leader executes under the operator's default profile,
    # whose personal SOUL/memory biases it toward reusing existing agents. Append
    # a blunt override (last, so it wins) that re-forces the owner-assignment
    # policy. Persistent Hermes (named profile) and other kinds don't need it.
    if leader_target.kind == AgentKind.hermes and leader_target.is_temporary:
        body = f"{body}\n\n{_TEMP_HERMES_LEADER_DIRECTIVE}"
    # The result is the prompt body as a single *user* message (the system role
    # is hidden behind the gateway in some OpenClaw configs).
    return [
        {"role": "user", "content": body},
    ]


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


BridgeFactory = Callable[[Config], OpenclawBridge]


def _default_bridge_factory(cfg: Config) -> OpenclawBridge:
    return OpenclawBridge.from_config(cfg)


def _chat_cli_timeout_seconds() -> float:
    raw = os.getenv(_OPENCLAW_CLI_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_OPENCLAW_CLI_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_OPENCLAW_CLI_TIMEOUT_SEC
    return max(value, _MIN_OPENCLAW_CLI_TIMEOUT_SEC)


def _non_openclaw_cli_timeout_seconds() -> float:
    raw = os.getenv(_NON_OPENCLAW_CLI_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_NON_OPENCLAW_CLI_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_NON_OPENCLAW_CLI_TIMEOUT_SEC
    return max(value, _MIN_NON_OPENCLAW_CLI_TIMEOUT_SEC)


def _resolve_openclaw_executable() -> str | None:
    return resolve_openclaw_executable()


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    return exc.__class__.__name__


def _normalize_goal_for_dedupe(goal: str) -> str:
    return goal.strip()


def _canonical_json_payload(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _coerce_non_openclaw_proposal(
    payload: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise RuntimeError("non-openclaw proposal must be a JSON object")
    agents_raw = payload.get("agents")
    tasks_raw = payload.get("tasks")
    if not isinstance(agents_raw, list):
        raise RuntimeError("non-openclaw proposal missing list field 'agents'")
    if not isinstance(tasks_raw, list):
        raise RuntimeError("non-openclaw proposal missing list field 'tasks'")
    agents: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for idx, item in enumerate(agents_raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"agents[{idx}] must be an object")
        agents.append(dict(item))
    for idx, item in enumerate(tasks_raw):
        if not isinstance(item, dict):
            raise RuntimeError(f"tasks[{idx}] must be an object")
        tasks.append(dict(item))
    return agents, tasks


def _extract_json_object_from_text(text: str) -> dict[str, Any]:
    raw = text.strip()
    if not raw:
        raise RuntimeError("non-openclaw leader returned empty output")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            parsed, _ = decoder.raw_decode(raw, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise RuntimeError("non-openclaw leader output did not contain a JSON object")


def _extract_non_openclaw_proposal(
    *,
    stdout_text: str,
    stderr_text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[tuple[str, str]] = []
    if stdout_text.strip():
        candidates.append(("stdout", stdout_text))
    if stderr_text.strip():
        candidates.append(("stderr", stderr_text))
    if stdout_text.strip() and stderr_text.strip():
        candidates.append(("combined", f"{stdout_text}\n{stderr_text}"))

    errors: list[str] = []
    for source, text in candidates:
        try:
            payload = _extract_json_object_from_text(text)
            return _coerce_non_openclaw_proposal(payload)
        except RuntimeError as exc:
            errors.append(f"{source}: {_error_text(exc)}")

    joined = "; ".join(errors) if errors else "no output"
    raise RuntimeError(
        "non-openclaw leader returned no parseable JSON proposal: "
        f"{joined[:800]}",
    )


def _find_reusable_request(
    *,
    storage: StorageBackend,
    user: str,
    leader_agent_id: str,
    goal: str,
    existing_agents: list[dict[str, Any]],
    existing_tasks: list[dict[str, Any]],
) -> TaskDecomposeRequest | None:
    now = datetime.now(timezone.utc)
    goal_key = _normalize_goal_for_dedupe(goal)
    agents_key = _canonical_json_payload(existing_agents)
    tasks_key = _canonical_json_payload(existing_tasks)
    rows, _ = storage.task_decompose_list(user=user, limit=50)
    for row in rows:
        if row.user != user:
            continue
        if row.status not in {
            TaskDecomposeStatus.pending,
            TaskDecomposeStatus.dispatched,
        }:
            continue
        if _ensure_aware(row.expires_at) <= now:
            continue
        if row.leader_agent_id != leader_agent_id:
            continue
        if _normalize_goal_for_dedupe(row.goal) != goal_key:
            continue
        if _canonical_json_payload(row.existing_agents or []) != agents_key:
            continue
        if _canonical_json_payload(row.existing_tasks or []) != tasks_key:
            continue
        return row
    return None


def _normalize_agent_kind(raw: str | AgentKind | None) -> AgentKind | None:
    if isinstance(raw, AgentKind):
        return raw
    text = str(raw or "").strip().lower()
    if not text:
        return None
    try:
        return AgentKind(text)
    except ValueError:
        return None


def _extract_leader_hint(
    *,
    leader_agent_id: str,
    existing_agents: list[dict[str, Any]],
) -> tuple[AgentKind | None, str | None, str | None]:
    matched: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    for item in existing_agents:
        if str(item.get("id") or "").strip() != leader_agent_id:
            continue
        if bool(item.get("isLeader") or item.get("is_leader")):
            matched = item
            break
        fallback = item
    picked = matched or fallback
    if picked is None:
        return None, None, None
    kind = _normalize_agent_kind(picked.get("kind"))
    repo = str(picked.get("repo") or "").strip() or None
    target_branch = str(
        picked.get("targetBranch") or picked.get("target_branch") or "",
    ).strip() or None
    return kind, repo, target_branch


def _non_openclaw_dispatch_argv(
    *, kind: AgentKind, message: str, profile: str | None = None,
) -> list[str]:
    if kind == AgentKind.claude:
        return [
            "claude",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "-p",
            message,
        ]
    if kind == AgentKind.codex:
        return [
            "codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "exec",
            message,
        ]
    if kind == AgentKind.cursor:
        return [
            "agent",
            "--force",
            "--approve-mcps",
            "--sandbox",
            "disabled",
            "-p",
            message,
        ]
    if kind == AgentKind.hermes:
        # Bind to a managed Hermes profile (== persistent agent id) so the
        # decomposition runs under that agent's own identity/memory. A temporary
        # Hermes leader has no profile → ``profile`` is None → default profile.
        argv = ["hermes", "--yolo"]
        if profile:
            argv += ["-p", profile]
        argv += ["-z", message]
        return argv
    raise RuntimeError(f"unsupported non-openclaw leader kind: {kind.value}")


async def _resolve_leader_target(
    *,
    leader_agent_id: str,
    user: str,
    storage: StorageBackend,
    config: Config,
    existing_agents: list[dict[str, Any]],
    leader_kind: str | AgentKind | None,
    leader_repo: str | None,
    leader_target_branch: str | None,
) -> _LeaderTarget:
    explicit_kind = _normalize_agent_kind(leader_kind)
    hint_kind, hint_repo, hint_target = _extract_leader_hint(
        leader_agent_id=leader_agent_id,
        existing_agents=existing_agents,
    )
    resolved_kind = explicit_kind or hint_kind
    resolved_repo = (leader_repo or "").strip() or hint_repo
    resolved_target = (
        (leader_target_branch or "").strip()
        or hint_target
        or DEFAULT_TARGET_BRANCH
    )

    # OpenClaw target: validate against managed registry ownership.
    if resolved_kind == AgentKind.openclaw or resolved_kind is None:
        from app.services.openclaw_agents import reindex_registered_agents

        reindex_registered_agents(storage=storage, config=config)
        leader = storage.openclaw_get(leader_agent_id)
        if leader is not None:
            if leader.created_by_user != user:
                raise LeaderAgentForbidden(
                    f"leader {leader_agent_id!r} belongs to a different user",
                    details={"leader_agent_id": leader_agent_id},
                )
            return _LeaderTarget(id=leader_agent_id, kind=AgentKind.openclaw)
        raise LeaderAgentNotFound(
            f"leader {leader_agent_id!r} is not a managed OpenClaw agent",
            details={"leader_agent_id": leader_agent_id},
        )

    if resolved_kind == AgentKind.openclaw:
        raise LeaderAgentNotFound(
            f"leader {leader_agent_id!r} is not a managed OpenClaw agent",
            details={"leader_agent_id": leader_agent_id},
        )
    if resolved_kind not in _NON_OPENCLAW_SUPPORTED_KINDS:
        raise LeaderKindUnsupported(
            f"leader kind {resolved_kind.value!r} is not supported for AI decompose dispatch",
            details={
                "leader_agent_id": leader_agent_id,
                "leader_kind": resolved_kind.value,
            },
        )
    if not resolved_repo:
        raise LeaderContextMissing(
            "leader_repo is required when leader_kind is non-OpenClaw",
            details={
                "leader_agent_id": leader_agent_id,
                "leader_kind": resolved_kind.value,
                "field": "leader_repo",
            },
        )
    # A Hermes leader is persistent only when it is a registered managed agent
    # (its profile exists on disk). Claude/Codex/Cursor have no persistent
    # platform, so they are always temporary. A temporary leader must NOT be
    # bound to a ``-p <id>`` profile — it runs under the default profile.
    is_temporary = not (
        resolved_kind == AgentKind.hermes
        and storage.hermes_get(leader_agent_id) is not None
    )
    return _LeaderTarget(
        id=leader_agent_id,
        kind=resolved_kind,
        repo=resolved_repo,
        target_branch=resolved_target,
        is_temporary=is_temporary,
    )


async def _dispatch_to_openclaw_leader_via_cli(
    *,
    cfg: Config,
    leader_agent_id: str,
    session_key: str,
    message: str,
) -> None:
    executable = _resolve_openclaw_executable()
    if not executable:
        raise RuntimeError("openclaw CLI is not available in PATH")

    timeout_sec = _chat_cli_timeout_seconds()
    argv = [
        executable,
        "agent",
        "--agent",
        leader_agent_id,
        "--session-id",
        session_key,
        "--message",
        message,
        "--timeout",
        str(int(timeout_sec)),
        "--json",
    ]

    for attempt in range(2):
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.CancelledError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            raise
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(
                f"openclaw agent invocation exceeded {int(timeout_sec)} seconds",
            ) from exc

        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            return

        detail = (err_text or out_text or f"exit code {proc.returncode}").strip()
        if attempt == 0 and looks_like_pending_scope_approval(detail):
            logger.warning(
                "decompose_dispatch_scope_pending_detected",
                request_session=session_key,
                leader=leader_agent_id,
                detail=detail[:240],
            )
            try:
                repaired = repair_pending_scope_upgrades(config=cfg)
                logger.info(
                    "decompose_dispatch_scope_repair_result",
                    request_session=session_key,
                    leader=leader_agent_id,
                    repaired_request_ids=repaired,
                    repaired_count=len(repaired),
                )
            except Exception as exc:
                logger.warning(
                    "decompose_dispatch_scope_repair_failed",
                    request_session=session_key,
                    leader=leader_agent_id,
                    error=str(exc),
                )
            continue
        raise RuntimeError(f"openclaw agent invocation failed: {detail[:1000]}")

    raise RuntimeError("openclaw agent invocation failed after automatic scope-repair retry")


async def _dispatch_to_non_openclaw_leader_via_cli(
    *,
    request_id: str,
    leader_target: _LeaderTarget,
    message: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if leader_target.repo is None:
        raise RuntimeError("non-openclaw leader dispatch requires repo")
    # Expand a leading ``~``: this subprocess runs without a shell, so a raw
    # ``~/foo`` cwd would FileNotFoundError (common on macOS agent repos).
    repo = os.path.expanduser(leader_target.repo)
    if not os.path.isdir(repo):
        raise RuntimeError(f"leader repo does not exist: {leader_target.repo}")
    argv = _non_openclaw_dispatch_argv(
        kind=leader_target.kind,
        message=message,
        # Temporary Hermes leaders have no managed profile → default profile.
        profile=None if leader_target.is_temporary else leader_target.id,
    )
    timeout_sec = _non_openclaw_cli_timeout_seconds()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{leader_target.kind.value} CLI is not available in PATH: {argv[0]}",
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"failed to launch {leader_target.kind.value} CLI: {_error_text(exc)}",
        ) from exc

    logger.info(
        "decompose_non_openclaw_session_started",
        request_id=request_id,
        leader=leader_target.id,
        leader_kind=leader_target.kind.value,
        cwd=leader_target.repo,
        command=argv[:2],
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.CancelledError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        logger.info(
            "decompose_non_openclaw_session_cancelled",
            request_id=request_id,
            leader=leader_target.id,
            leader_kind=leader_target.kind.value,
        )
        raise
    except asyncio.TimeoutError as exc:
        proc.kill()
        stdout, stderr = await proc.communicate()
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        detail = (err_text or out_text or "timeout").strip()
        raise RuntimeError(
            f"{leader_target.kind.value} agent invocation exceeded "
            f"{int(timeout_sec)} seconds: {detail[:300]}",
        ) from exc

    out_text = stdout.decode("utf-8", errors="replace").strip()
    err_text = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = (err_text or out_text or f"exit code {proc.returncode}").strip()
        raise RuntimeError(
            f"{leader_target.kind.value} agent invocation failed: {detail[:1000]}",
        )
    logger.info(
        "decompose_non_openclaw_session_completed",
        request_id=request_id,
        leader=leader_target.id,
        leader_kind=leader_target.kind.value,
        stdout_chars=len(out_text),
        stderr_chars=len(err_text),
    )
    return _extract_non_openclaw_proposal(
        stdout_text=out_text,
        stderr_text=err_text,
    )


async def start_decompose_request(
    *,
    goal: str,
    leader_agent_id: str,
    user: str,
    api_base: str,
    leader_kind: str | AgentKind | None = None,
    leader_repo: str | None = None,
    leader_target_branch: str | None = None,
    existing_agents: list[dict[str, Any]] | None = None,
    existing_tasks: list[dict[str, Any]] | None = None,
    result_language: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
    bridge_factory: BridgeFactory | None = None,
    background: bool = True,
) -> DecomposeStartResult:
    """Kick off an async decomposition. Returns the polling handle.

    OpenClaw leaders use the historical skill path; non-OpenClaw leaders
    run as one-shot CLI sessions that return JSON on stdout.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    # Resolve at call time so tests can monkey-patch the module-level
    # ``_default_bridge_factory`` after import.
    if bridge_factory is None:
        bridge_factory = _default_bridge_factory

    if not goal.strip():
        raise TaskDecomposeError("goal must not be empty", details={"field": "goal"})
    if not user:
        raise TaskDecomposeError("user must not be empty")
    if not leader_agent_id:
        raise TaskDecomposeError(
            "leader_agent_id is required",
            details={"field": "leader_agent_id"},
        )

    existing_agents_payload = list(existing_agents or [])
    existing_tasks_payload = list(existing_tasks or [])
    leader_target = await _resolve_leader_target(
        leader_agent_id=leader_agent_id,
        user=user,
        storage=storage,
        config=cfg,
        existing_agents=existing_agents_payload,
        leader_kind=leader_kind,
        leader_repo=leader_repo,
        leader_target_branch=leader_target_branch,
    )

    async with _START_REQUEST_LOCK:
        reusable = _find_reusable_request(
            storage=storage,
            user=user,
            leader_agent_id=leader_agent_id,
            goal=goal,
            existing_agents=existing_agents_payload,
            existing_tasks=existing_tasks_payload,
        )
        if reusable is not None:
            logger.info(
                "decompose_request_reused_existing",
                request_id=reusable.request_id,
                leader=leader_agent_id,
                user=user,
                status=(
                    reusable.status.value
                    if isinstance(reusable.status, TaskDecomposeStatus)
                    else str(reusable.status)
                ),
            )
            return DecomposeStartResult(
                request_id=reusable.request_id,
                token_ttl_seconds=_REQUEST_TTL_SECONDS,
                status=(
                    reusable.status
                    if isinstance(reusable.status, TaskDecomposeStatus)
                    else TaskDecomposeStatus(str(reusable.status))
                ),
            )

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=_REQUEST_TTL_SECONDS)
        req = TaskDecomposeRequest(
            user=user,
            goal=goal,
            leader_agent_id=leader_agent_id,
            existing_agents=existing_agents_payload,
            existing_tasks=existing_tasks_payload,
            status=TaskDecomposeStatus.pending,
            expires_at=expires_at,
        )
        saved = storage.task_decompose_create(req)
        request_id = saved.request_id

    token = it.mint_token(
        request_id=request_id,
        user=user,
        purpose=_PURPOSE,
        ttl_seconds=_REQUEST_TTL_SECONDS,
        config=cfg,
    )

    # Hand the leader the user's full PERSISTENT agent inventory (the two
    # persistent platforms: OpenClaw + Hermes) so it can reuse existing owners
    # instead of inventing new ones. Shape: id / name / kind / isLeader, so the
    # owner picker can dedupe against ``existing_agents``.
    persistent_agents: list[dict[str, Any]] = []
    for row in storage.openclaw_list(owner_user=user):
        persistent_agents.append(
            {
                "id": row.id,
                "name": row.name,
                "kind": "openclaw",
                "isLeader": row.id == leader_agent_id,
            }
        )
    try:
        from app.services.hermes_agents import list_agents as _hermes_list_agents

        for row in _hermes_list_agents(user=user, storage=storage, config=cfg):
            persistent_agents.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "kind": "hermes",
                    "isLeader": row.id == leader_agent_id,
                }
            )
    except Exception as exc:  # listing Hermes must never block decomposition
        logger.warning("decompose_hermes_inventory_failed", error=_error_text(exc))

    # Temporary-agent platforms actually installed on this host, plus the default
    # working directory those temporary agents will be pointed at.
    available_platforms = _detect_temp_agent_platforms()
    temp_workdir = _ensure_ai_temp_agent_workdir()

    coro = _dispatch_to_leader(
        request_id=request_id,
        goal=goal,
        leader_target=leader_target,
        user=user,
        api_base=api_base,
        token=token,
        result_language=result_language,
        persistent_agents=persistent_agents,
        available_platforms=available_platforms,
        temp_workdir=temp_workdir,
        existing_agents=existing_agents_payload,
        existing_tasks=existing_tasks_payload,
        cfg=cfg,
        storage=storage,
        bridge_factory=bridge_factory,
    )
    if background:
        _track_dispatch_task(request_id, asyncio.create_task(coro))
    else:
        await coro

    return DecomposeStartResult(
        request_id=request_id,
        token_ttl_seconds=_REQUEST_TTL_SECONDS,
        status=TaskDecomposeStatus.pending,
    )


def _track_dispatch_task(request_id: str, task: asyncio.Task[None]) -> None:
    _INFLIGHT_DISPATCH_TASKS[request_id] = task

    def _done(done_task: asyncio.Task[None]) -> None:
        current = _INFLIGHT_DISPATCH_TASKS.get(request_id)
        if current is done_task:
            _INFLIGHT_DISPATCH_TASKS.pop(request_id, None)
        try:
            done_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "decompose_dispatch_task_failed",
                request_id=request_id,
                error=_error_text(exc),
            )

    task.add_done_callback(_done)


async def _cancel_background_dispatch_task(request_id: str) -> bool:
    task = _INFLIGHT_DISPATCH_TASKS.pop(request_id, None)
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning(
            "decompose_dispatch_cancel_wait_failed",
            request_id=request_id,
            error=_error_text(exc),
        )
    return True


async def _reset_openclaw_decompose_session_best_effort(
    *,
    leader_agent_id: str,
    request_id: str,
) -> None:
    executable = _resolve_openclaw_executable()
    if not executable:
        return
    session_key = f"task-decompose-{request_id}"
    argv = [
        executable,
        "agent",
        "--agent",
        leader_agent_id,
        "--session-id",
        session_key,
        "--message",
        "/reset",
        "--timeout",
        str(int(_DECOMPOSE_CANCEL_RESET_TIMEOUT_SEC)),
        "--json",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        logger.warning(
            "decompose_cancel_reset_spawn_failed",
            request_id=request_id,
            leader=leader_agent_id,
            error=_error_text(exc),
        )
        return
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_DECOMPOSE_CANCEL_RESET_TIMEOUT_SEC + 3.0,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        logger.warning(
            "decompose_cancel_reset_timeout",
            request_id=request_id,
            leader=leader_agent_id,
        )
        return
    if proc.returncode != 0:
        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        logger.warning(
            "decompose_cancel_reset_failed",
            request_id=request_id,
            leader=leader_agent_id,
            detail=(err_text or out_text or f"exit={proc.returncode}")[:300],
        )
        return
    logger.info(
        "decompose_cancel_reset_done",
        request_id=request_id,
        leader=leader_agent_id,
    )


async def _dispatch_to_leader(
    *,
    request_id: str,
    goal: str,
    leader_target: _LeaderTarget,
    user: str,
    api_base: str,
    token: str,
    result_language: str | None,
    persistent_agents: list[dict[str, Any]],
    available_platforms: list[str],
    temp_workdir: str,
    existing_agents: list[dict[str, Any]],
    existing_tasks: list[dict[str, Any]],
    cfg: Config,
    storage: StorageBackend,
    bridge_factory: BridgeFactory,
) -> None:
    """Send the chat request to the leader. Updates request status."""
    messages = _compose_messages(
        request_id=request_id, user=user, goal=goal,
        leader_target=leader_target, api_base=api_base, token=token,
        result_language=result_language,
        persistent_agents=persistent_agents,
        available_platforms=available_platforms,
        temp_workdir=temp_workdir,
        existing_agents=existing_agents, existing_tasks=existing_tasks,
    )
    dispatch_message = str(messages[0].get("content", "")) if messages else ""
    if leader_target.kind != AgentKind.openclaw:
        try:
            _bump_status(storage, request_id, TaskDecomposeStatus.dispatched)
            agents, tasks = await _dispatch_to_non_openclaw_leader_via_cli(
                request_id=request_id,
                leader_target=leader_target,
                message=dispatch_message,
            )
            current = storage.task_decompose_get(request_id)
            if current is None:
                return
            if current.status in {
                TaskDecomposeStatus.succeeded,
                TaskDecomposeStatus.failed,
                TaskDecomposeStatus.timed_out,
            }:
                logger.info(
                    "decompose_dispatch_non_openclaw_terminal_seen",
                    request_id=request_id,
                    leader=leader_target.id,
                    leader_kind=leader_target.kind.value,
                    status=(
                        current.status.value
                        if isinstance(current.status, TaskDecomposeStatus)
                        else str(current.status)
                    ),
                )
                return
            validate_decompose_proposal(
                agents,
                tasks,
                expected_leader=leader_target.id,
            )
            mark_request_succeeded(
                request_id,
                agents=agents,
                tasks=tasks,
                storage=storage,
            )
            logger.info(
                "decompose_dispatch_complete",
                request_id=request_id,
                leader=leader_target.id,
                leader_kind=leader_target.kind.value,
                user=user,
                transport="direct_cli_json",
                accepted_agents=len(agents),
                accepted_tasks=len(tasks),
            )
        except ProposalValidationError as exc:
            _mark_failed(storage, request_id, exc.code, exc.message)
            logger.warning(
                "decompose_dispatch_non_openclaw_invalid_proposal",
                request_id=request_id,
                leader=leader_target.id,
                leader_kind=leader_target.kind.value,
                error=exc.message,
            )
        except Exception as exc:
            detail = _error_text(exc)
            _mark_failed(storage, request_id, "DISPATCH_ERROR", detail)
            logger.warning(
                "decompose_dispatch_non_openclaw_error",
                request_id=request_id,
                leader=leader_target.id,
                leader_kind=leader_target.kind.value,
                error=detail,
            )
        return

    # OpenClaw: keep historical CLI-first + bridge fallback behavior.
    session_key = f"task-decompose-{request_id}"
    cli_error: str | None = None
    try:
        await _dispatch_to_openclaw_leader_via_cli(
            cfg=cfg,
            leader_agent_id=leader_target.id,
            session_key=session_key,
            message=dispatch_message,
        )
        _bump_status(storage, request_id, TaskDecomposeStatus.dispatched)
        logger.info(
            "decompose_dispatch_complete",
            request_id=request_id,
            leader=leader_target.id,
            user=user,
            transport="cli",
        )
        return
    except Exception as exc:
        cli_error = _error_text(exc)
        if cli_error == "openclaw CLI is not available in PATH":
            cli_error = None
            logger.info(
                "decompose_dispatch_cli_skipped",
                request_id=request_id,
                leader=leader_target.id,
                reason="cli_not_found",
            )
        else:
            logger.warning(
                "decompose_dispatch_cli_error",
                request_id=request_id,
                leader=leader_target.id,
                error=cli_error,
            )

    bridge = bridge_factory(cfg)
    try:
        try:
            await bridge.chat_completion(
                agent_id=leader_target.id,
                messages=messages,
                session_key=session_key,
            )
        except OpenclawBridgeError as exc:
            bridge_error = _error_text(exc)
            if cli_error:
                _mark_failed(
                    storage,
                    request_id,
                    "DISPATCH_ERROR",
                    f"CLI dispatch failed: {cli_error}; bridge fallback failed: {bridge_error}",
                )
            else:
                _mark_failed(storage, request_id, "BRIDGE_ERROR", bridge_error)
            logger.warning(
                "decompose_dispatch_bridge_error",
                request_id=request_id, error=bridge_error,
            )
            return
        _bump_status(storage, request_id, TaskDecomposeStatus.dispatched)
        logger.info(
            "decompose_dispatch_complete",
            request_id=request_id,
            leader=leader_target.id,
            user=user,
            transport="bridge",
            cli_error=cli_error,
        )
    finally:
        await bridge.aclose()


# ──────────────────────────────────────────────────────────────────────
# Status mutators (called by internal commit endpoint + status endpoint)
# ──────────────────────────────────────────────────────────────────────


def _bump_status(
    storage: StorageBackend, request_id: str, new_status: TaskDecomposeStatus,
) -> None:
    row = storage.task_decompose_get(request_id)
    if row is None:
        return
    if row.status in {
        TaskDecomposeStatus.succeeded,
        TaskDecomposeStatus.failed,
        TaskDecomposeStatus.timed_out,
    }:
        return  # respect terminal verdict from the commit callback
    row.status = new_status
    storage.task_decompose_update(row)


def _mark_failed(
    storage: StorageBackend, request_id: str, code: str, message: str,
) -> None:
    row = storage.task_decompose_get(request_id)
    if row is None:
        return
    if row.status in {
        TaskDecomposeStatus.succeeded,
        TaskDecomposeStatus.failed,
    }:
        return
    row.status = TaskDecomposeStatus.failed
    row.error_code = code
    row.error_message = (message or code).strip()
    storage.task_decompose_update(row)


def mark_request_succeeded(
    request_id: str,
    *,
    agents: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    storage: StorageBackend | None = None,
) -> TaskDecomposeRequest:
    """Called by ``/api/internal/task-decompose/commit`` after validation."""
    storage = storage or get_storage()
    row = storage.task_decompose_get(request_id)
    if row is None:
        raise KeyError(request_id)
    row.status = TaskDecomposeStatus.succeeded
    row.result_agents = agents
    row.result_tasks = tasks
    row.error_code = None
    row.error_message = None
    return storage.task_decompose_update(row)


def mark_request_failed(
    request_id: str, *, code: str, message: str,
    storage: StorageBackend | None = None,
) -> TaskDecomposeRequest:
    storage = storage or get_storage()
    row = storage.task_decompose_get(request_id)
    if row is None:
        raise KeyError(request_id)
    row.status = TaskDecomposeStatus.failed
    row.error_code = code
    row.error_message = (message or code).strip()
    return storage.task_decompose_update(row)


async def cancel_decompose_request(
    request_id: str,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> TaskDecomposeRequest | None:
    """Cancel a pending/dispatched request and end this leader conversation."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    row = storage.task_decompose_get(request_id)
    if row is None:
        return None
    cancelled_dispatch = await _cancel_background_dispatch_task(request_id)
    # Always mark as failed-with-cancel so pollers stop immediately.
    _mark_failed(
        storage,
        request_id,
        "USER_CANCELLED",
        "request cancelled by user",
    )
    leader = storage.openclaw_get(row.leader_agent_id)
    if leader is not None:
        await _reset_openclaw_decompose_session_best_effort(
            leader_agent_id=row.leader_agent_id,
            request_id=request_id,
        )
    current = storage.task_decompose_get(request_id)
    logger.info(
        "decompose_cancelled_by_user",
        request_id=request_id,
        leader_agent_id=row.leader_agent_id,
        cancelled_dispatch=cancelled_dispatch,
        status=(current.status.value if current is not None else "missing"),
    )
    return current


def get_request(
    request_id: str, *, storage: StorageBackend | None = None,
) -> TaskDecomposeRequest | None:
    storage = storage or get_storage()
    return storage.task_decompose_get(request_id)


def reap_expired_requests(*, storage: StorageBackend | None = None) -> int:
    """Mark non-terminal requests past their expiry as ``timed_out``."""
    storage = storage or get_storage()
    now = datetime.now(timezone.utc)
    items, _ = storage.task_decompose_list(limit=200)
    n = 0
    for r in items:
        if r.status in {
            TaskDecomposeStatus.succeeded,
            TaskDecomposeStatus.failed,
            TaskDecomposeStatus.timed_out,
        }:
            continue
        if _ensure_aware(r.expires_at) < now:
            r.status = TaskDecomposeStatus.timed_out
            r.error_code = "REQUEST_TIMED_OUT"
            r.error_message = "leader did not call back within the TTL window"
            storage.task_decompose_update(r)
            dispatch_task = _INFLIGHT_DISPATCH_TASKS.pop(r.request_id, None)
            if dispatch_task is not None and not dispatch_task.done():
                dispatch_task.cancel()
            n += 1
    return n


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


__all__ = [
    "BridgeFactory",
    "cancel_decompose_request",
    "DecomposeStartResult",
    "LeaderAgentForbidden",
    "LeaderAgentNotFound",
    "LeaderContextMissing",
    "LeaderKindUnsupported",
    "TaskDecomposeError",
    "get_request",
    "mark_request_failed",
    "mark_request_succeeded",
    "reap_expired_requests",
    "start_decompose_request",
]
