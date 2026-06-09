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

OpenClaw leader requests still use the ``csflow-task-decomposer`` skill
contract. Non-OpenClaw leader requests receive a self-contained prompt
that returns one JSON object directly to stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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


# ──────────────────────────────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────────────────────────────


_OPENCLAW_PROMPT = """\
## ClawsomeFlow Task Decomposition Request

You are a Flow designer. Your job is to decompose one high-level Flow goal
into a scheduler-ready DAG. This session is triggered by ClawsomeFlow backend.
Your installed `csflow-task-decomposer` skill defines how to **POST results
back to ClawsomeFlow via curl**.

## Runtime variables for this request

- request_id: {request_id}
- user: {user}
- you (leader): {leader_agent_id}  (the `isLeaderSummary` task must be owned by you)
- ClawsomeFlow API base: {api_base}
- short-lived callback token: {token} (valid for 30 minutes)
- required output language for task `subject` + `description`: {result_language}

## User's Flow goal (keep verbatim context)

{goal}

## Available OpenClaw agents you may assign as task owners

These are the OpenClaw agents the user currently owns. **Prefer assigning
worker tasks to these existing agents** rather than inventing new ones.

{available_agents_yaml}

## Existing agents in editor (already drafted by the user; treat as hints)

{existing_agents_yaml}

## Existing tasks in editor (reference only; you may improve/reorder/replace)

{existing_tasks_yaml}

## What you must do

1. Analyze the goal and the available agents.
2. Produce one complete valid Flow proposal (agents + tasks JSON), with:
   - exactly one `isLeaderSummary: true` task, owned by `{leader_agent_id}`
   - leader owns no other task
   - owner kinds limited to `openclaw`, `claude`, `codex`, `cursor`, or `hermes`
   - unique ids and acyclic DAG
   - all task `subject` and `description` in `{result_language}`
3. **Owner assignment policy**:
   - First try to reuse one of the *Available OpenClaw agents* above.
   - If no existing agent fits a particular task, set its `ownerAgentId`
     to an **empty string** (`""`) so the user picks an owner manually
     in the editor. Do NOT invent a new OpenClaw agent the user does
     not have.
   - You may still propose non-OpenClaw workers (`claude` / `codex` /
     `cursor` / `hermes`) with placeholder `repo` / `targetBranch`
     when the work is clearly better suited to a local TUI runtime.
4. POST the result to `/api/internal/task-decompose/commit` as instructed by the skill.
5. Reply in one short sentence: generated N tasks for user review.
"""


_NON_OPENCLAW_PROMPT = """\
## ClawsomeFlow Task Decomposition Request

You are a Flow designer. This request is dispatched by ClawsomeFlow to a
non-OpenClaw leader runtime, so **do not rely on any OpenClaw-specific skill
hooks**.

## Runtime variables for this request

- request_id: {request_id}
- user: {user}
- leader_agent_id: {leader_agent_id}
- leader_kind: {leader_kind}
- leader_repo: {leader_repo}
- leader_target_branch: {leader_target_branch}
- ClawsomeFlow API base: {api_base}
- short-lived callback token: {token} (valid for 30 minutes)
- required output language for task `subject` + `description`: {result_language}

## User's Flow goal

{goal}

## Available OpenClaw agents

These are OpenClaw agents the user currently owns. Prefer reusing them for
worker tasks instead of inventing new OpenClaw ids.

{available_agents_yaml}

## Existing agents in editor (hint)

{existing_agents_yaml}

## Existing tasks in editor (hint)

{existing_tasks_yaml}

## Required output schema

Produce one JSON object with:

- `agents`: each item has `id`, `kind`, optional `repo`, optional
  `targetBranch`, `isLeader`.
- `tasks`: each item has `id`, `ownerAgentId`, `subject`, `description`,
  `dependsOn`, `isLeaderSummary`, optional `timeoutSeconds`.

Invariants:
1. Exactly one task has `isLeaderSummary=true`, owned by `{leader_agent_id}`.
2. Leader owns no non-summary tasks.
3. IDs are unique and dependencies form an acyclic DAG.
4. Use only `openclaw`, `claude`, `codex`, `cursor`, `hermes` as owner kinds.
5. All `subject` and `description` text must follow `{result_language}`.

Owner assignment policy:
- First reuse available OpenClaw agents.
- If no suitable owner exists for a worker task, set `ownerAgentId` to `""`
  and let the user decide in editor.
- You may still propose non-OpenClaw workers (`claude`/`codex`/`cursor`/`hermes`)
  with placeholder repo/branch.

## Output contract (must follow)

1. Do **not** execute shell commands, do **not** write files, and do **not**
   call curl. Permissions may block these actions.
2. Respond with exactly one JSON object and nothing else.
3. Use this exact shape:
   {{
     "agents": [/* your agents */],
     "tasks": [/* your tasks */]
   }}
4. Do not wrap the JSON with markdown fences.
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
    available_agents: list[dict[str, Any]],
    existing_agents: list[dict[str, Any]],
    existing_tasks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    language = {
        "zh": "Chinese",
        "en": "English",
    }.get((result_language or "").lower(), "same language as the user's goal")
    if leader_target.kind == AgentKind.openclaw:
        body = _OPENCLAW_PROMPT.format(
            request_id=request_id,
            user=user,
            leader_agent_id=leader_target.id,
            api_base=api_base,
            token=token,
            result_language=language,
            goal=goal.strip(),
            available_agents_yaml=_yaml_lines(available_agents),
            existing_agents_yaml=_yaml_lines(existing_agents),
            existing_tasks_yaml=_yaml_lines(existing_tasks),
        )
    else:
        body = _NON_OPENCLAW_PROMPT.format(
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
            available_agents_yaml=_yaml_lines(available_agents),
            existing_agents_yaml=_yaml_lines(existing_agents),
            existing_tasks_yaml=_yaml_lines(existing_tasks),
        )
    # The skill matches on the top-of-message prefix, so we put it as the
    # *user* message (the system role is hidden behind the gateway in some
    # OpenClaw configs and may not be searched for the trigger string).
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
        # Bind to the managed Hermes profile (== leader agent id). REQUIRED so
        # the decomposition runs under the agent's own identity/memory.
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
    return _LeaderTarget(
        id=leader_agent_id,
        kind=resolved_kind,
        repo=resolved_repo,
        target_branch=resolved_target,
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
    if not os.path.isdir(leader_target.repo):
        raise RuntimeError(f"leader repo does not exist: {leader_target.repo}")
    argv = _non_openclaw_dispatch_argv(
        kind=leader_target.kind,
        message=message,
        profile=leader_target.id,
    )
    timeout_sec = _non_openclaw_cli_timeout_seconds()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=leader_target.repo,
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

    # Hand the leader the user's full OpenClaw agent inventory so it can
    # pick existing owners instead of inventing new ones. Mirror the
    # shape used elsewhere (id / kind / isLeader) so the skill's owner
    # picker can dedupe against ``existing_agents``.
    inventory_rows = storage.openclaw_list(owner_user=user)
    available_agents: list[dict[str, Any]] = []
    for row in inventory_rows:
        available_agents.append(
            {
                "id": row.id,
                "name": row.name,
                "kind": "openclaw",
                "isLeader": row.id == leader_agent_id,
            }
        )

    coro = _dispatch_to_leader(
        request_id=request_id,
        goal=goal,
        leader_target=leader_target,
        user=user,
        api_base=api_base,
        token=token,
        result_language=result_language,
        available_agents=available_agents,
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
    available_agents: list[dict[str, Any]],
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
        available_agents=available_agents,
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
