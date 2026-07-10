"""Public OpenClaw agent management API.

Endpoints (per API.md):

* ``POST   /api/openclaw/agents``                          — create (sync)
* ``POST   /api/openclaw/agents/{agent_id}/cancel-create`` — cancel in-flight create
* ``GET    /api/openclaw/agents``                          — list (own)
* ``GET    /api/openclaw/agents/{agent_id}``               — get
* ``PATCH  /api/openclaw/agents/{agent_id}``               — update fields
* ``DELETE /api/openclaw/agents/{agent_id}``               — delete (purge=?)
* ``GET    /api/openclaw/agents/{agent_id}/chat-history``  — UI chat history cache
* ``POST   /api/openclaw/agents/{agent_id}/chat``          — direct chat (SSE)
* ``POST   /api/openclaw/agents/{agent_id}/reset``         — reset chat session

All create/update/delete operations are executed directly by backend services.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path as FsPath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app import paths as app_paths
from app.api._auth import current_user
from app.api.errors import ApiError
from app.config import load_config
from app.deployment import get_deployment_capabilities
from app.integrations import openclaw_json as oj
from app.integrations.openclaw_agent_source import (
    AGENTS_USER_CUSTOM_SECTION_END,
    AGENTS_USER_CUSTOM_SECTION_START,
)
from app.integrations.openclaw_cli import resolve_openclaw_executable
from app.integrations.openclaw_install import (
    looks_like_pending_scope_approval,
    repair_pending_scope_upgrades,
)
from app.integrations.openclaw_skills import (
    discover_user_agent_skills,
    seed_skills_source,
)
from app.logging_setup import get_logger
from app.models import OpenclawAgent, iso_utc
from app.operations import get_op_registry
from app.scheduler.naming import openclaw_user_chat_session_id
from app.services import chat_attachments as attachment_svc
from app.services import openclaw_agents as svc
from app.services import openclaw_chat as chat_progress
from app.services import openclaw_chat_history as chat_history
from app.services.chat_retry import (
    CHAT_CONNECTION_RETRY_ATTEMPTS,
    CHAT_CONNECTION_RETRY_DELAYS_SEC,
    is_transient_connection_error,
)
from app.services import subprocess_registry as _subproc_registry
from app.storage import StorageBackend, get_storage

router = APIRouter(prefix="/openclaw/agents", tags=["openclaw"])
logger = get_logger("api.openclaw_agents")
_NO_TEXT_REPLY_MARKER = "[[NO_TEXT_REPLY]]"
_DEFAULT_OPENCLAW_CLI_TIMEOUT_SEC = 1800.0
_MIN_OPENCLAW_CLI_TIMEOUT_SEC = 1800.0
_OPENCLAW_CLI_TIMEOUT_ENV = "CSFLOW_OPENCLAW_CLI_TIMEOUT_SECONDS"
_DEFAULT_OPENCLAW_AGENT_READY_TIMEOUT_SEC = 15.0
_MIN_OPENCLAW_AGENT_READY_TIMEOUT_SEC = 5.0
_MAX_OPENCLAW_AGENT_READY_TIMEOUT_SEC = 15.0
_OPENCLAW_AGENT_READY_TIMEOUT_ENV = "CSFLOW_OPENCLAW_AGENT_READY_TIMEOUT_SECONDS"
_OPENCLAW_AGENT_READY_POLL_INTERVAL_SEC = 1.0
_OPENCLAW_AGENT_READY_PROBE_TIMEOUT_SEC = 12.0
_OPENCLAW_AGENT_READY_REQUIRED_CONSECUTIVE_SUCCESSES = 2
_AGENT_CREATE_CANCEL_CLEANUP_WAIT_TIMEOUT_SEC = 30.0
_AGENT_CREATE_CANCEL_CLEANUP_POLL_SEC = 0.5
_AGENT_CREATE_CANCEL_CLEANUP_REQUIRED_CONSECUTIVE_ABSENT = 2
_CHAT_SESSION_REVISIONS: dict[tuple[str, str], int] = {}
_PENDING_AGENT_CREATE_CANCELLATIONS: dict[str, asyncio.Event] = {}
_REQUESTED_AGENT_CREATE_CANCELLATIONS: set[str] = set()
_REQUESTED_AGENT_CREATE_CANCELLATION_TTL_SEC = 120.0
# Batch ids whose external-import run the user asked to cancel. The import loop
# checks this between agents and stops (keeping already-imported agents). Cleared
# by the import handler's ``finally`` once the run ends.
_CANCELLED_IMPORT_BATCHES: set[str] = set()
_OPENCLAW_RUNTIME_OP_TIMEOUT_SEC = 30.0
_ENTROPY_CRON_NAME_PREFIX = "csflow-entropy-management-"
_SKILL_ENTRY_FILENAME = "SKILL.md"
_HOOK_MD_FILENAME = "HOOK.md"
_HOOK_HANDLER_FILENAME = "handler.ts"
_HOOKS_DIRNAME = "hooks"
_SKILLS_DIRNAME = "skills"
_AGENTS_CUSTOM_START_RE = re.compile(r"<!--\s*AGENTS_USER_CUSTOM_SECTION_START\s*-->")
_AGENTS_CUSTOM_END_RE = re.compile(r"<!--\s*AGENTS_USER_CUSTOM_SECTION_END\s*-->")
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SKILL_FRONT_MATTER_RE = re.compile(
    r"^---\s*\n(?P<header>.*?)\n---\s*(?:\n(?P<body>.*))?$",
    re.DOTALL,
)
_TIME_HH_MM_RE = re.compile(r"^(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)$")
_SYSTEM_TIMEZONE_CANDIDATES = (
    FsPath("/etc/timezone"),
    FsPath("/var/db/timezone/zoneinfo"),  # macOS
)
_SETTINGS_CACHE_TTL_SEC = 10.0
_SETTINGS_CACHE: dict[tuple[str, str], tuple[float, OpenclawAgentSettingsResponse]] = {}
# Settings/skill/cron handlers are sync ``def`` → FastAPI runs them in a
# threadpool, so the cache is touched by multiple threads concurrently. Guard
# every access (incl. the invalidate scan) to avoid lost writes and
# "dictionary changed size during iteration".
_SETTINGS_CACHE_LOCK = threading.Lock()
_IMPORT_OPTIMIZE_PROMPT = (
    "Follow the shared rules in AGENTS.md and improve each definition document based on your role. "
    "Keep content professional, concise, well-structured, and correct any unprofessional wording. "
    "`my-desktop/` is your work-material area and you have full edit authority there. "
    "Design and create only the necessary internal structure based on your responsibilities; "
    "remove unsuitable directories when needed, and add reusable templates for work folders when useful. "
    "Use this area to maintain core user interaction records, accumulated experience, and important summaries "
    "that should be tracked long-term. After defining management rules for this area, write the key rules "
    "into the AGENTS.md user custom section. Note: the current user custom section may contain older user-authored "
    "content; fix incorrect or redundant parts as needed. If existing managed work data is scattered in the workspace, "
    "consolidate it under `my-desktop/` when appropriate and update the management rules accordingly."
)
# Strong refs to detached create/bootstrap tasks so a client disconnect doesn't
# GC the task before it records the op's terminal state.
_DETACHED_CREATE_TASKS: set[asyncio.Task] = set()

# Per-agent handle on the live bootstrap task so cancel-create can await the
# *actual* task release (its ``finally`` clears the in-flight reservation)
# instead of blindly polling — see :func:`_await_create_release`.
_AGENT_CREATE_TASKS: dict[str, asyncio.Task] = {}


def _spawn_detached_create(coro) -> asyncio.Task:
    """Run *coro* as a task whose lifecycle is independent of the request.

    Callers ``await asyncio.shield(task)`` so the happy path still propagates the
    result/exception, while a client disconnect (which cancels the request
    coroutine) leaves the bootstrap running to completion.
    """
    task = asyncio.ensure_future(coro)
    _DETACHED_CREATE_TASKS.add(task)
    task.add_done_callback(_DETACHED_CREATE_TASKS.discard)
    return task


def _track_agent_create_task(agent_id: str, task: asyncio.Task) -> None:
    """Register the live bootstrap *task* for *agent_id* so cancel can await it."""
    _AGENT_CREATE_TASKS[agent_id] = task

    def _untrack(done: asyncio.Task, aid: str = agent_id) -> None:
        if _AGENT_CREATE_TASKS.get(aid) is done:
            _AGENT_CREATE_TASKS.pop(aid, None)

    task.add_done_callback(_untrack)


_CREATE_SELF_DEFINE_COMMIT_MESSAGE_PREFIX = "[csflow] bootstrap self-definition"
_NEW_AGENT_GATEWAY_RETRY_DELAYS_SEC: tuple[float, ...] = (0.4, 0.8, 1.6)


# ──────────────────────────────────────────────────────────────────────
# Common
# ──────────────────────────────────────────────────────────────────────


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


def _storage_dep() -> StorageBackend:
    return get_storage()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]


def _contains_whitespace(value: str) -> bool:
    return any(ch.isspace() for ch in value)


def _is_macos_runtime() -> bool:
    return sys.platform == "darwin"


def _should_wait_until_gateway_agent_ready() -> bool:
    # macOS file-watcher behavior can lag after atomic writes; Linux keeps
    # the existing fast path.
    return _is_macos_runtime()


def _register_agent_create_cancellation(agent_id: str) -> asyncio.Event:
    event = asyncio.Event()
    _PENDING_AGENT_CREATE_CANCELLATIONS[agent_id] = event
    if agent_id in _REQUESTED_AGENT_CREATE_CANCELLATIONS:
        event.set()
    return event


def _unregister_agent_create_cancellation(*, agent_id: str, event: asyncio.Event) -> None:
    current = _PENDING_AGENT_CREATE_CANCELLATIONS.get(agent_id)
    if current is event:
        _PENDING_AGENT_CREATE_CANCELLATIONS.pop(agent_id, None)


def _agent_create_cancellation_event(agent_id: str) -> asyncio.Event | None:
    return _PENDING_AGENT_CREATE_CANCELLATIONS.get(agent_id)


def _request_agent_create_cancellation(agent_id: str) -> bool:
    svc.request_create_cancellation(agent_id)
    _REQUESTED_AGENT_CREATE_CANCELLATIONS.add(agent_id)
    _schedule_clear_agent_create_cancellation_request(agent_id)
    event = _PENDING_AGENT_CREATE_CANCELLATIONS.get(agent_id)
    if event is None:
        return False
    event.set()
    return True


def _clear_agent_create_cancellation_request(agent_id: str) -> None:
    _REQUESTED_AGENT_CREATE_CANCELLATIONS.discard(agent_id)


def _schedule_clear_agent_create_cancellation_request(agent_id: str) -> None:
    async def _clear_later() -> None:
        await asyncio.sleep(_REQUESTED_AGENT_CREATE_CANCELLATION_TTL_SEC)
        if agent_id in _PENDING_AGENT_CREATE_CANCELLATIONS:
            return
        _REQUESTED_AGENT_CREATE_CANCELLATIONS.discard(agent_id)

    asyncio.create_task(_clear_later())


def _is_agent_create_cancelled(agent_id: str) -> bool:
    event = _agent_create_cancellation_event(agent_id)
    return bool(event is not None and event.is_set())


def _raise_if_agent_create_cancelled(*, agent_id: str) -> None:
    if not _is_agent_create_cancelled(agent_id):
        return
    raise ApiError(
        "AGENT_CREATE_CANCELLED",
        f'agent creation cancelled by user: "{agent_id}"',
        status_code=409,
    )


def _request_import_cancellation(batch_id: str) -> None:
    """Flag a batch import for cancellation; the loop stops at the next agent."""
    if batch_id:
        _CANCELLED_IMPORT_BATCHES.add(batch_id)


def _is_import_cancelled(batch_id: str) -> bool:
    return bool(batch_id) and batch_id in _CANCELLED_IMPORT_BATCHES


def _clear_import_cancellation(batch_id: str) -> None:
    _CANCELLED_IMPORT_BATCHES.discard(batch_id)


def _best_effort_purge_agent_dir(*, agent_id: str) -> str:
    agents_root = app_paths.agents_dir().expanduser().resolve(strict=False)
    agent_dir = app_paths.agent_dir(agent_id).expanduser().resolve(strict=False)
    try:
        agent_dir.relative_to(agents_root)
    except ValueError:
        return f"unsafe cleanup target refused: {agent_dir}"
    if not agent_dir.exists():
        return ""
    try:
        shutil.rmtree(agent_dir)
    except OSError as exc:
        return str(exc)
    return ""


async def _cleanup_failed_agent_create(*, agent_id: str, storage: StorageBackend) -> str:
    cleanup_errors: list[str] = []
    try:
        await svc.delete_agent(agent_id, mode="purge", storage=storage)
    except svc.AgentNotFound:
        pass
    except Exception as exc:
        cleanup_errors.append(str(exc))
    fs_error = _best_effort_purge_agent_dir(agent_id=agent_id)
    if fs_error:
        cleanup_errors.append(fs_error)
    return "; ".join(err for err in cleanup_errors if err)


def _agent_create_artifacts_still_present(
    *,
    agent_id: str,
    storage: StorageBackend,
) -> tuple[bool, str]:
    if storage.openclaw_get(agent_id) is not None:
        return True, "db_row_present"
    try:
        if oj.find_agent(agent_id) is not None:
            return True, "runtime_config_present"
    except Exception:
        return True, "runtime_config_scan_failed"
    agent_dir = app_paths.agent_dir(agent_id).expanduser().resolve(strict=False)
    if agent_dir.exists():
        return True, "agent_dir_present"
    return False, ""


async def _wait_until_agent_create_cleanup_visible(
    *,
    agent_id: str,
    storage: StorageBackend,
) -> None:
    deadline = time.monotonic() + _AGENT_CREATE_CANCEL_CLEANUP_WAIT_TIMEOUT_SEC
    consecutive_absent = 0
    last_presence = ""
    while True:
        present, detail = _agent_create_artifacts_still_present(
            agent_id=agent_id,
            storage=storage,
        )
        if present:
            consecutive_absent = 0
            last_presence = detail
        else:
            consecutive_absent += 1
            if consecutive_absent >= _AGENT_CREATE_CANCEL_CLEANUP_REQUIRED_CONSECUTIVE_ABSENT:
                return
        now = time.monotonic()
        if now >= deadline:
            raise ApiError(
                "AGENT_CANCEL_CLEANUP_TIMEOUT",
                (
                    f'cancel cleanup did not fully converge for "{agent_id}" within '
                    f'{int(_AGENT_CREATE_CANCEL_CLEANUP_WAIT_TIMEOUT_SEC)}s; '
                    f"last_presence={last_presence or 'unknown'}"
                ),
                status_code=504,
            )
        await asyncio.sleep(
            min(
                _AGENT_CREATE_CANCEL_CLEANUP_POLL_SEC,
                max(deadline - now, 0.0),
            )
        )


async def _await_create_release(*, agent_id: str) -> None:
    """Guarantee the in-flight create reservation is released after a cancel.

    By the time this runs the cancel has already (a) signalled the cancellation
    event — so any live bootstrap aborts to a cleanup no-op — and (b) purged
    every create artifact. We then converge the reservation:

    * If a live bootstrap task is tracked, ``await`` it (bounded) so its own
      ``finally`` releases the reservation *after* its workspace teardown
      finishes — the race-safe path that prevents a retry from re-scaffolding the
      same id while teardown is still running.
    * Otherwise (no live task — e.g. a registration-only agent created via
      ``commit_agent`` directly, or a genuine hang past the window) **force**
      the release so a retry is never permanently blocked by a stale reservation.

    This is the fix for the regression where a stuck reservation left re-creates
    of the same id rejected with "a create for X is already in progress".
    """
    task = _AGENT_CREATE_TASKS.get(agent_id)
    if task is not None and not task.done():
        try:
            # shield so wait_for's timeout cancels the wrapper, not the bootstrap.
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_AGENT_CREATE_CANCEL_CLEANUP_WAIT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            pass
        except Exception:
            # Bootstrap raised (e.g. AGENT_CREATE_CANCELLED); its finally already
            # ran. The reservation check below confirms.
            pass
    if svc.is_create_in_flight(agent_id):
        logger.warning(
            "openclaw_agent_cancel_force_release_in_flight",
            agent_id=agent_id,
            had_tracked_task=task is not None,
        )
        svc.finish_create_in_flight(agent_id)


def _settings_cache_get(*, user: str, agent_id: str) -> OpenclawAgentSettingsResponse | None:
    key = (user, agent_id)
    with _SETTINGS_CACHE_LOCK:
        item = _SETTINGS_CACHE.get(key)
        if item is None:
            return None
        ts, payload = item
        if time.time() - ts > _SETTINGS_CACHE_TTL_SEC:
            _SETTINGS_CACHE.pop(key, None)
            return None
        return payload.model_copy(deep=True)


def _settings_cache_put(
    *,
    user: str,
    agent_id: str,
    payload: OpenclawAgentSettingsResponse,
) -> None:
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE[(user, agent_id)] = (time.time(), payload.model_copy(deep=True))


def _settings_cache_invalidate(*, agent_id: str, user: str | None = None) -> None:
    with _SETTINGS_CACHE_LOCK:
        if user is not None:
            _SETTINGS_CACHE.pop((user, agent_id), None)
            return
        for key in [k for k in _SETTINGS_CACHE if k[1] == agent_id]:
            _SETTINGS_CACHE.pop(key, None)


def _trim_cli_output(text: str, *, limit: int = 1200) -> str:
    raw = (text or "").strip()
    if len(raw) <= limit:
        return raw
    return f"{raw[:limit]}..."


def _normalize_fs_item_name(value: str, *, field: str) -> str:
    name = (value or "").strip()
    if not name:
        raise ApiError(
            "INVALID_PAYLOAD",
            f"{field} is required",
            status_code=400,
        )
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ApiError(
            "INVALID_PAYLOAD",
            f"{field} contains invalid path characters",
            status_code=400,
        )
    return name


def _openclaw_runtime_env(*, config) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_STATE_DIR"] = str(config.openclaw_home_path)
    env["OPENCLAW_CONFIG_PATH"] = str(config.openclaw_home_path / "openclaw.json")
    return env


def _parse_json_object_from_mixed_output(text: str) -> dict[str, Any]:
    raw = _ANSI_ESCAPE_RE.sub("", (text or "")).replace("\ufeff", "").strip()
    if not raw:
        raise ApiError(
            "OPENCLAW_CLI_BAD_OUTPUT",
            "openclaw command returned empty output",
            status_code=502,
        )
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    start = raw.find("{")
    while start >= 0:
        try:
            parsed, _ = decoder.raw_decode(raw, start)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            start = raw.find("{", start + 1)
            continue
        start = raw.find("{", start + 1)
    raise ApiError(
        "OPENCLAW_CLI_BAD_OUTPUT",
        f"openclaw command returned non-JSON payload: {_trim_cli_output(raw)}",
        status_code=502,
    )


def _run_openclaw_cli(
    *,
    args: list[str],
    cwd: str | None = None,
    expect_json: bool = False,
    timeout_sec: float = _OPENCLAW_RUNTIME_OP_TIMEOUT_SEC,
) -> dict[str, Any] | str:
    executable = _resolve_openclaw_executable()
    if not executable:
        raise ApiError(
            "OPENCLAW_CLI_MISSING",
            "openclaw CLI is not available in PATH",
            status_code=503,
        )
    cfg = load_config()
    argv = [executable, *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
            env=_openclaw_runtime_env(config=cfg),
        )
    except subprocess.TimeoutExpired as exc:
        raise ApiError(
            "OPENCLAW_CLI_TIMEOUT",
            f"openclaw command timed out after {int(timeout_sec)}s: {' '.join(args[:3])}",
            status_code=504,
        ) from exc
    except OSError as exc:
        raise ApiError(
            "OPENCLAW_CLI_FAILED",
            f"failed to execute openclaw CLI: {exc}",
            status_code=502,
        ) from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    merged = "\n".join(part for part in (stdout, stderr) if part).strip()
    if proc.returncode != 0:
        raise ApiError(
            "OPENCLAW_CLI_FAILED",
            f"openclaw {' '.join(args[:4])} failed: {_trim_cli_output(merged)}",
            status_code=502,
        )
    if not expect_json:
        return merged
    if stdout:
        try:
            return _parse_json_object_from_mixed_output(stdout)
        except ApiError:
            # Some OpenClaw versions emit useful JSON on stdout but attach
            # extra diagnostics on stderr; retry against merged output.
            if merged and merged != stdout:
                return _parse_json_object_from_mixed_output(merged)
            raise
    return _parse_json_object_from_mixed_output(merged)


def _agent_workspace_dir(agent: OpenclawAgent) -> FsPath:
    workspace = FsPath(agent.workspace_path).expanduser().resolve(strict=False)
    if not workspace.exists() or not workspace.is_dir():
        raise ApiError(
            "WORKSPACE_NOT_FOUND",
            f"workspace not found: {workspace}",
            status_code=500,
        )
    return workspace


def _system_skill_names() -> set[str]:
    try:
        seeded = seed_skills_source()
        return set(discover_user_agent_skills(source_root=seeded))
    except Exception:
        try:
            return set(discover_user_agent_skills())
        except Exception:
            return set()


def _extract_agents_custom_section(raw_agents_md: str) -> str:
    start_m = _AGENTS_CUSTOM_START_RE.search(raw_agents_md)
    if start_m is None:
        return ""
    end_m = _AGENTS_CUSTOM_END_RE.search(raw_agents_md, start_m.end())
    if end_m is None:
        return ""
    return raw_agents_md[start_m.end():end_m.start()].strip()


def _replace_agents_custom_section(*, raw_agents_md: str, custom_section: str) -> str:
    start_m = _AGENTS_CUSTOM_START_RE.search(raw_agents_md)
    end_m = (
        _AGENTS_CUSTOM_END_RE.search(raw_agents_md, start_m.end())
        if start_m is not None
        else None
    )
    if start_m is None or end_m is None:
        raise ApiError(
            "AGENTS_CUSTOM_SECTION_MISSING",
            "AGENTS.md is missing AGENTS_USER_CUSTOM_SECTION markers",
            status_code=409,
        )
    body = custom_section.strip()
    replacement = (
        f"{AGENTS_USER_CUSTOM_SECTION_START}\n"
        f"{body}\n"
        f"{AGENTS_USER_CUSTOM_SECTION_END}"
    )
    return f"{raw_agents_md[:start_m.start()]}{replacement}{raw_agents_md[end_m.end():]}"


def _extract_hook_enabled_map() -> dict[str, bool]:
    data = oj.load_openclaw_json()
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return {}
    internal = hooks.get("internal", {})
    if not isinstance(internal, dict):
        return {}
    entries = internal.get("entries", {})
    if not isinstance(entries, dict):
        return {}
    out: dict[str, bool] = {}
    for name, raw in entries.items():
        if not isinstance(name, str):
            continue
        if isinstance(raw, dict):
            out[name] = bool(raw.get("enabled", True))
    return out


def _is_system_cron_job(job: dict[str, Any], *, agent_id: str) -> bool:
    name = str(job.get("name") or "")
    source = str(job.get("source") or "")
    if name == f"{_ENTROPY_CRON_NAME_PREFIX}{agent_id}":
        return True
    return source in {"system", "openclaw-bundled", "builtin", "built-in"}


async def _set_hook_enabled_fallback(*, hook_name: str, enabled: bool) -> None:
    def _mut(data: dict[str, Any]) -> None:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            hooks = {}
            data["hooks"] = hooks
        internal = hooks.setdefault("internal", {})
        if not isinstance(internal, dict):
            internal = {}
            hooks["internal"] = internal
        entries = internal.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            internal["entries"] = entries
        item = entries.get(hook_name)
        if not isinstance(item, dict):
            item = {}
        item["enabled"] = bool(enabled)
        entries[hook_name] = item

    await oj.update_openclaw_json(
        _mut,
        operation="update_hook_enabled",
        agent_id=hook_name,
    )


async def _remove_hook_entry_fallback(*, hook_name: str) -> None:
    def _mut(data: dict[str, Any]) -> None:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return
        internal = hooks.get("internal")
        if not isinstance(internal, dict):
            return
        entries = internal.get("entries")
        if not isinstance(entries, dict):
            return
        entries.pop(hook_name, None)

    await oj.update_openclaw_json(
        _mut,
        operation="remove_hook_entry",
        agent_id=hook_name,
    )


async def _set_hook_enabled(*, hook_name: str, enabled: bool, workspace: FsPath) -> None:
    cmd = "enable" if enabled else "disable"
    try:
        # Blocking CLI subprocess — keep it off the event loop so hook toggles
        # don't freeze other tabs.
        await asyncio.to_thread(
            _run_openclaw_cli,
            args=["hooks", cmd, hook_name],
            cwd=str(workspace),
            expect_json=False,
        )
    except ApiError:
        # Some custom hooks may not be discoverable immediately via CLI (e.g.
        # fresh files before gateway refresh). Persisting config directly keeps
        # the intended runtime setting consistent.
        await _set_hook_enabled_fallback(hook_name=hook_name, enabled=enabled)

# ──────────────────────────────────────────────────────────────────────
# Response models
# ──────────────────────────────────────────────────────────────────────


class OpenclawAgentSummary(_CamelModel):
    id: str
    name: str
    description: str
    team_id: str
    team_name: str
    workspace_path: str
    created_by_user: str
    created_at: str


class OpenclawAgentDetail(OpenclawAgentSummary):
    nl_prompt: str = ""
    openclaw_config_snapshot: dict[str, Any] = Field(default_factory=dict)


class OpenclawAgentListResponse(_CamelModel):
    items: list[OpenclawAgentSummary]


class OpenclawRuntimeStatusResponse(_CamelModel):
    running: bool
    reason: str
    gateway_url: str | None = None


class OpenclawRestorableAgentView(_CamelModel):
    id: str
    name: str
    description: str
    team_id: str
    team_name: str
    workspace_path: str
    created_by_user: str


class OpenclawRestorableListResponse(_CamelModel):
    items: list[OpenclawRestorableAgentView]
    total: int


class OpenclawTeamView(_CamelModel):
    id: str
    name: str
    created_by_user: str
    created_at: str


class OpenclawTeamListResponse(_CamelModel):
    items: list[OpenclawTeamView]
    total: int


class CreateTeamPayload(_CamelModel):
    name: str


class UpdateTeamPayload(_CamelModel):
    name: str


def _to_team_view(team: svc.OpenclawTeam) -> OpenclawTeamView:
    return OpenclawTeamView(
        id=team.id,
        name=team.name,
        created_by_user=team.created_by_user,
        created_at=iso_utc(team.created_at),
    )


def _team_name_map(
    *,
    storage: StorageBackend,
    user: str | None,
) -> dict[str, str]:
    teams = svc.list_teams(user=user, storage=storage)
    return {t.id: t.name for t in teams}


def _to_summary(a: OpenclawAgent, *, team_name: str) -> OpenclawAgentSummary:
    return OpenclawAgentSummary(
        id=a.id,
        name=a.name,
        description=a.description,
        team_id=a.team_id,
        team_name=team_name,
        workspace_path=a.workspace_path,
        created_by_user=a.created_by_user,
        created_at=iso_utc(a.created_at),
    )


def _to_detail(a: OpenclawAgent, *, team_name: str) -> OpenclawAgentDetail:
    return OpenclawAgentDetail(
        id=a.id,
        name=a.name,
        description=a.description,
        team_id=a.team_id,
        team_name=team_name,
        workspace_path=a.workspace_path,
        created_by_user=a.created_by_user,
        created_at=iso_utc(a.created_at),
        nl_prompt=a.nl_prompt,
        openclaw_config_snapshot=a.openclaw_config_snapshot,
    )


def _to_restorable_summary(
    item: svc.RestorableAgentCandidate,
    *,
    team_name: str,
) -> OpenclawRestorableAgentView:
    return OpenclawRestorableAgentView(
        id=item.id,
        name=item.name,
        description=item.description,
        team_id=item.team_id,
        team_name=team_name,
        workspace_path=item.workspace_path,
        created_by_user=item.created_by_user,
    )


class ExternalImportCandidateView(_CamelModel):
    id: str
    name: str
    description: str
    workspace_path: str


class ExternalImportCandidateListResponse(_CamelModel):
    items: list[ExternalImportCandidateView]
    total: int


class ImportExternalAgentsPayload(_CamelModel):
    agent_ids: list[str] = Field(default_factory=list)
    import_all: bool = False
    team_id: str | None = None
    # Optional client-generated batch id so the UI can recover the import popup
    # across a refresh / tab close+reopen (op_id ``openclaw_import_batch:{id}``).
    batch_id: str = ""


class ImportedExternalAgentView(_CamelModel):
    source_agent_id: str
    source_agent_name: str
    target_agent_id: str
    target_agent_name: str
    target_workspace_path: str
    target_team_id: str
    target_team_name: str
    optimization_scheduled: bool = True


class ImportExternalAgentFailure(_CamelModel):
    source_agent_id: str
    error_code: str
    message: str


class ImportExternalAgentsResponse(_CamelModel):
    requested_count: int
    imported: list[ImportedExternalAgentView]
    failed: list[ImportExternalAgentFailure]
    # True when the user cancelled mid-run: agents already imported are kept,
    # the remaining selected agents were skipped.
    cancelled: bool = False


class OpenclawAgentSkillView(_CamelModel):
    name: str
    description: str
    content: str
    path: str


class OpenclawAgentCronView(_CamelModel):
    id: str
    agent_id: str
    name: str
    enabled: bool
    schedule_expr: str
    schedule_tz: str
    message: str
    source: str
    system_builtin: bool
    can_edit: bool
    can_delete: bool


class OpenclawAgentHookView(_CamelModel):
    name: str
    description: str
    source: str
    events: list[str]
    enabled: bool
    eligible: bool | None = None
    requirements_satisfied: bool | None = None
    managed_by_plugin: bool | None = None
    system_builtin: bool
    can_edit: bool
    can_delete: bool
    hook_md: str | None = None
    handler_ts: str | None = None


class OpenclawAgentSettingsResponse(_CamelModel):
    agent_id: str
    skills: list[OpenclawAgentSkillView]
    cron_jobs: list[OpenclawAgentCronView]
    hooks: list[OpenclawAgentHookView]
    agents_user_custom_section: str


class OpenclawAgentSkillUpsertPayload(_CamelModel):
    name: str
    description: str = ""
    content: str | None = None
    skill_md: str | None = None


class OpenclawAgentSkillPatchPayload(_CamelModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    skill_md: str | None = None


class OpenclawAgentCronCreatePayload(_CamelModel):
    name: str
    schedule_mode: Literal["daily", "weekly", "monthly"] | None = None
    schedule_time: str | None = None
    schedule_weekday: int | None = None
    schedule_day_of_month: int | None = None
    cron_expr: str | None = None
    message: str
    enabled: bool = True


class OpenclawAgentCronPatchPayload(_CamelModel):
    name: str | None = None
    cron_expr: str | None = None
    schedule_mode: Literal["daily", "weekly", "monthly"] | None = None
    schedule_time: str | None = None
    schedule_weekday: int | None = None
    schedule_day_of_month: int | None = None
    message: str | None = None
    enabled: bool | None = None


class OpenclawAgentHookUpsertPayload(_CamelModel):
    name: str
    hook_md: str
    handler_ts: str
    enabled: bool = True


class OpenclawAgentHookPatchPayload(_CamelModel):
    name: str | None = None
    hook_md: str | None = None
    handler_ts: str | None = None
    enabled: bool | None = None


class OpenclawAgentCustomSectionPayload(_CamelModel):
    content: str


class OpenclawAgentCustomSectionView(_CamelModel):
    content: str


def _to_cron_view(job: dict[str, Any], *, agent_id: str) -> OpenclawAgentCronView:
    schedule = job.get("schedule")
    payload = job.get("payload")
    expr = ""
    tz = ""
    if isinstance(schedule, dict):
        expr = str(schedule.get("expr") or "")
        tz = str(schedule.get("tz") or "")
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("event") or "")
    source = str(job.get("source") or "")
    system_builtin = _is_system_cron_job(job, agent_id=agent_id)
    return OpenclawAgentCronView(
        id=str(job.get("id") or ""),
        agent_id=str(job.get("agentId") or ""),
        name=str(job.get("name") or ""),
        enabled=bool(job.get("enabled", True)),
        schedule_expr=expr,
        schedule_tz=tz,
        message=message,
        source=source,
        system_builtin=system_builtin,
        can_edit=not system_builtin,
        can_delete=not system_builtin,
    )


def _to_hook_view(
    *,
    raw: dict[str, Any],
    enabled_map: dict[str, bool],
    hook_md: str | None = None,
    handler_ts: str | None = None,
    source_override: str | None = None,
) -> OpenclawAgentHookView:
    name = str(raw.get("name") or "")
    source = source_override or str(raw.get("source") or "")
    system_builtin = source == "openclaw-bundled" or bool(raw.get("managedByPlugin"))
    if "enabled" in raw:
        enabled = bool(raw.get("enabled"))
    else:
        enabled = not bool(raw.get("disabled", False))
    if name in enabled_map:
        enabled = bool(enabled_map[name])
    raw_events = raw.get("events")
    events = [str(item) for item in raw_events] if isinstance(raw_events, list) else []
    return OpenclawAgentHookView(
        name=name,
        description=str(raw.get("description") or ""),
        source=source or "workspace-custom",
        events=events,
        enabled=enabled,
        eligible=(bool(raw.get("eligible")) if "eligible" in raw else None),
        requirements_satisfied=(
            bool(raw.get("requirementsSatisfied"))
            if "requirementsSatisfied" in raw
            else None
        ),
        managed_by_plugin=(
            bool(raw.get("managedByPlugin"))
            if "managedByPlugin" in raw
            else None
        ),
        system_builtin=system_builtin,
        can_edit=not system_builtin,
        can_delete=not system_builtin,
        hook_md=hook_md,
        handler_ts=handler_ts,
    )

def _to_external_import_candidate_view(
    candidate: svc.ExternalImportCandidate,
) -> ExternalImportCandidateView:
    return ExternalImportCandidateView(
        id=candidate.id,
        name=candidate.name,
        description=candidate.description,
        workspace_path=candidate.workspace_path,
    )


def _to_imported_external_agent_view(
    item: svc.ImportedExternalAgent,
) -> ImportedExternalAgentView:
    return ImportedExternalAgentView(
        source_agent_id=item.source_agent_id,
        source_agent_name=item.source_agent_name,
        target_agent_id=item.target_agent_id,
        target_agent_name=item.target_agent_name,
        target_workspace_path=item.target_workspace_path,
        target_team_id=item.target_team_id,
        target_team_name=item.target_team_name,
        optimization_scheduled=True,
    )


def _ensure_owner(agent: OpenclawAgent, user: str) -> None:
    if agent.created_by_user != user:
        raise ApiError(
            "FORBIDDEN",
            "agent belongs to a different user",
            status_code=403,
        )


def _strip_wrapped_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and (
        (text.startswith('"') and text.endswith('"'))
        or (text.startswith("'") and text.endswith("'"))
    ):
        return text[1:-1]
    return text


def _parse_skill_document(*, raw: str, fallback_name: str) -> tuple[str, str, str]:
    text = (raw or "").replace("\r\n", "\n")
    stripped = text.strip()
    if not stripped:
        return fallback_name, "", ""
    header_name = fallback_name
    header_description = ""
    body = stripped
    m = _SKILL_FRONT_MATTER_RE.match(stripped)
    if m is None:
        return header_name, header_description, body
    header = (m.group("header") or "").strip()
    body = (m.group("body") or "").strip()
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        k = key.strip().lower()
        v = _strip_wrapped_quotes(value)
        if k == "name" and v:
            header_name = v
        elif k == "description":
            header_description = v
    return header_name, header_description, body


def _quote_yaml_double(value: str) -> str:
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_skill_document(*, name: str, description: str, content: str) -> str:
    body = (content or "").strip()
    doc = (
        "---\n"
        f"name: {name}\n"
        f"description: {_quote_yaml_double((description or '').strip())}\n"
        "---\n\n"
        f"{body}\n"
    )
    return doc


def _list_user_defined_skills(*, workspace: FsPath) -> list[OpenclawAgentSkillView]:
    skills_root = workspace / _SKILLS_DIRNAME
    if not skills_root.exists() or not skills_root.is_dir():
        return []
    system = _system_skill_names()
    views: list[OpenclawAgentSkillView] = []
    for child in sorted(skills_root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if child.name in system:
            continue
        skill_md_path = child / _SKILL_ENTRY_FILENAME
        if not skill_md_path.exists() or not skill_md_path.is_file():
            continue
        raw = skill_md_path.read_text(encoding="utf-8")
        _, description, content = _parse_skill_document(
            raw=raw,
            fallback_name=child.name,
        )
        views.append(
            OpenclawAgentSkillView(
                name=child.name,
                description=description,
                content=content,
                path=str(skill_md_path),
            )
        )
    return views


def _parse_cron_jobs_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = payload.get("jobs")
    if isinstance(jobs, list):
        return [item for item in jobs if isinstance(item, dict)]
    return []


def _system_timezone_name() -> str:
    from_env = (os.environ.get("TZ") or "").strip()
    if from_env:
        return from_env
    try:
        localtime_target = os.path.realpath("/etc/localtime")
        marker = "/usr/share/zoneinfo/"
        if marker in localtime_target:
            zone = localtime_target.split(marker, 1)[1].strip()
            if zone:
                return zone
    except OSError:
        pass
    for path in _SYSTEM_TIMEZONE_CANDIDATES:
        try:
            if path.exists() and path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    local = datetime.now().astimezone().tzname() or ""
    if "/" in local:
        return local
    return "UTC"


def _parse_time_hh_mm(value: str) -> tuple[int, int]:
    m = _TIME_HH_MM_RE.match((value or "").strip())
    if m is None:
        raise ApiError(
            "INVALID_PAYLOAD",
            "schedule_time must be HH:MM (24h), for example 09:30",
            status_code=400,
        )
    return int(m.group("hour")), int(m.group("minute"))


def _cron_expr_from_schedule(
    *,
    mode: str,
    time_hh_mm: str,
    weekday: int | None,
    day_of_month: int | None,
) -> str:
    hour, minute = _parse_time_hh_mm(time_hh_mm)
    if mode == "daily":
        return f"{minute} {hour} * * *"
    if mode == "weekly":
        if weekday is None or weekday < 0 or weekday > 6:
            raise ApiError(
                "INVALID_PAYLOAD",
                "schedule_weekday is required for weekly mode (0=Sun ... 6=Sat)",
                status_code=400,
            )
        return f"{minute} {hour} * * {weekday}"
    if mode == "monthly":
        if day_of_month is None or day_of_month < 1 or day_of_month > 31:
            raise ApiError(
                "INVALID_PAYLOAD",
                "schedule_day_of_month is required for monthly mode (1-31)",
                status_code=400,
            )
        return f"{minute} {hour} {day_of_month} * *"
    raise ApiError(
        "INVALID_PAYLOAD",
        "schedule_mode must be one of: daily, weekly, monthly",
        status_code=400,
    )


def _resolve_cron_create_payload(payload: OpenclawAgentCronCreatePayload) -> tuple[str, str]:
    explicit_expr = (payload.cron_expr or "").strip()
    if explicit_expr:
        return explicit_expr, _system_timezone_name()
    mode = (payload.schedule_mode or "weekly").strip()
    time_hh_mm = (payload.schedule_time or "03:00").strip()
    expr = _cron_expr_from_schedule(
        mode=mode,
        time_hh_mm=time_hh_mm,
        weekday=(payload.schedule_weekday if payload.schedule_weekday is not None else 1),
        day_of_month=(payload.schedule_day_of_month if payload.schedule_day_of_month is not None else 1),
    )
    return expr, _system_timezone_name()


def _resolve_cron_patch_expr(
    payload: OpenclawAgentCronPatchPayload,
) -> tuple[str, str] | None:
    explicit_expr = (payload.cron_expr or "").strip()
    schedule_fields_present = any(
        value is not None
        for value in (
            payload.schedule_mode,
            payload.schedule_time,
            payload.schedule_weekday,
            payload.schedule_day_of_month,
        )
    )
    if explicit_expr:
        return explicit_expr, _system_timezone_name()
    if not schedule_fields_present:
        return None
    mode = (payload.schedule_mode or "weekly").strip()
    time_hh_mm = (payload.schedule_time or "03:00").strip()
    expr = _cron_expr_from_schedule(
        mode=mode,
        time_hh_mm=time_hh_mm,
        weekday=(payload.schedule_weekday if payload.schedule_weekday is not None else 1),
        day_of_month=(payload.schedule_day_of_month if payload.schedule_day_of_month is not None else 1),
    )
    return expr, _system_timezone_name()


def _list_agent_cron_jobs(*, agent_id: str, workspace: FsPath) -> list[dict[str, Any]]:
    payload = _run_openclaw_cli(
        args=["cron", "list", "--agent", agent_id, "--all", "--json"],
        cwd=str(workspace),
        expect_json=True,
    )
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for item in _parse_cron_jobs_payload(payload):
        if str(item.get("agentId") or "") != agent_id:
            continue
        out.append(item)
    return out


def _find_agent_cron_job(
    *,
    agent_id: str,
    job_id: str,
    workspace: FsPath,
) -> dict[str, Any]:
    try:
        payload = _run_openclaw_cli(
            args=["cron", "get", job_id],
            cwd=str(workspace),
            expect_json=True,
        )
    except ApiError as exc:
        if "not found" in exc.message.lower():
            raise ApiError(
                "CRON_JOB_NOT_FOUND",
                f"cron job not found: {job_id}",
                status_code=404,
            ) from exc
        raise
    if not isinstance(payload, dict):
        raise ApiError(
            "OPENCLAW_CLI_BAD_OUTPUT",
            f"cron get returned invalid payload for {job_id}",
            status_code=502,
        )
    if str(payload.get("agentId") or "") != agent_id:
        raise ApiError(
            "CRON_JOB_NOT_FOUND",
            f"cron job not found for agent {agent_id}: {job_id}",
            status_code=404,
        )
    return payload


def _parse_hooks_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hooks = payload.get("hooks")
    if not isinstance(hooks, list):
        return []
    return [item for item in hooks if isinstance(item, dict)]


def _list_runtime_hooks(*, workspace: FsPath) -> list[dict[str, Any]]:
    payload = _run_openclaw_cli(
        args=["hooks", "list", "--json", "--verbose"],
        cwd=str(workspace),
        expect_json=True,
    )
    if not isinstance(payload, dict):
        return []
    return _parse_hooks_payload(payload)


def _read_agents_custom_section(*, workspace: FsPath) -> str:
    agents_md = workspace / "AGENTS.md"
    if not agents_md.exists() or not agents_md.is_file():
        return ""
    return _extract_agents_custom_section(agents_md.read_text(encoding="utf-8"))


def _collect_workspace_custom_hooks(*, workspace: FsPath) -> list[OpenclawAgentHookView]:
    hooks_root = workspace / _HOOKS_DIRNAME
    if not hooks_root.exists() or not hooks_root.is_dir():
        return []
    enabled_map = _extract_hook_enabled_map()
    views: list[OpenclawAgentHookView] = []
    for child in sorted(hooks_root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        hook_md_path = child / _HOOK_MD_FILENAME
        handler_path = child / _HOOK_HANDLER_FILENAME
        hook_md = hook_md_path.read_text(encoding="utf-8") if hook_md_path.exists() else ""
        handler_ts = handler_path.read_text(encoding="utf-8") if handler_path.exists() else ""
        raw = {
            "name": child.name,
            "description": "",
            "source": "workspace-custom",
            "events": [],
            "enabled": enabled_map.get(child.name, True),
            "eligible": True,
            "requirementsSatisfied": True,
            "managedByPlugin": False,
        }
        views.append(
            _to_hook_view(
                raw=raw,
                enabled_map=enabled_map,
                hook_md=hook_md,
                handler_ts=handler_ts,
                source_override="workspace-custom",
            )
        )
    return views


def _collect_settings_skills(
    *,
    workspace: FsPath,
) -> list[OpenclawAgentSkillView]:
    return _list_user_defined_skills(workspace=workspace)


def _collect_settings_cron_jobs(
    *,
    agent: OpenclawAgent,
    workspace: FsPath,
) -> list[OpenclawAgentCronView]:
    raw_cron_jobs: list[dict[str, Any]] = []
    try:
        raw_cron_jobs = _list_agent_cron_jobs(agent_id=agent.id, workspace=workspace)
    except ApiError as exc:
        if exc.code != "OPENCLAW_CLI_BAD_OUTPUT":
            raise
        logger.warning(
            "openclaw_settings_cron_bad_output_fallback",
            agent_id=agent.id,
            error=exc.message,
        )
    cron_jobs = [_to_cron_view(item, agent_id=agent.id) for item in raw_cron_jobs]
    cron_jobs.sort(key=lambda item: (item.system_builtin, item.name.lower(), item.id))
    return cron_jobs


def _collect_settings_hooks(
    *,
    agent: OpenclawAgent,
    workspace: FsPath,
) -> list[OpenclawAgentHookView]:
    runtime_hooks: list[dict[str, Any]] = []
    try:
        runtime_hooks = _list_runtime_hooks(workspace=workspace)
    except ApiError as exc:
        if exc.code != "OPENCLAW_CLI_BAD_OUTPUT":
            raise
        logger.warning(
            "openclaw_settings_hooks_bad_output_fallback",
            agent_id=agent.id,
            error=exc.message,
        )

    enabled_map = _extract_hook_enabled_map()
    hooks_by_name: dict[str, OpenclawAgentHookView] = {}
    for raw in runtime_hooks:
        name = str(raw.get("name") or "")
        if not name:
            continue
        hooks_by_name[name] = _to_hook_view(raw=raw, enabled_map=enabled_map)
    for custom in _collect_workspace_custom_hooks(workspace=workspace):
        hooks_by_name[custom.name] = custom
    return sorted(hooks_by_name.values(), key=lambda item: item.name.lower())


def _collect_settings_custom_section(*, workspace: FsPath) -> str:
    return _read_agents_custom_section(workspace=workspace)


def _collect_agent_settings(
    *,
    agent: OpenclawAgent,
) -> OpenclawAgentSettingsResponse:
    workspace = _agent_workspace_dir(agent)
    skills = _collect_settings_skills(workspace=workspace)

    # Keep cron/hooks in parallel for legacy all-in-one snapshot callers.
    cron_jobs: list[OpenclawAgentCronView] = []
    hooks: list[OpenclawAgentHookView] = []
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="settings-runtime") as pool:
        cron_future = pool.submit(_collect_settings_cron_jobs, agent=agent, workspace=workspace)
        hooks_future = pool.submit(_collect_settings_hooks, agent=agent, workspace=workspace)
        cron_jobs = cron_future.result()
        hooks = hooks_future.result()

    return OpenclawAgentSettingsResponse(
        agent_id=agent.id,
        skills=skills,
        cron_jobs=cron_jobs,
        hooks=hooks,
        agents_user_custom_section=_collect_settings_custom_section(workspace=workspace),
    )


def _find_hook_view(
    *,
    settings: OpenclawAgentSettingsResponse,
    hook_name: str,
) -> OpenclawAgentHookView | None:
    for item in settings.hooks:
        if item.name == hook_name:
            return item
    return None


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────


@router.get("/teams", response_model=OpenclawTeamListResponse)
def list_teams(
    user: UserDep,
    storage: StorageDep,
) -> OpenclawTeamListResponse:
    items = svc.list_teams(user=user, storage=storage)
    views = [_to_team_view(item) for item in items]
    return OpenclawTeamListResponse(items=views, total=len(views))


@router.post("/teams", response_model=OpenclawTeamView, status_code=201)
def create_team(
    payload: Annotated[CreateTeamPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawTeamView:
    item = svc.create_team(payload.name, user=user, storage=storage)
    return _to_team_view(item)


@router.patch("/teams/{team_id}", response_model=OpenclawTeamView)
def update_team(
    team_id: Annotated[str, Path()],
    payload: Annotated[UpdateTeamPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawTeamView:
    item = svc.update_team(team_id, payload.name, user=user, storage=storage)
    return _to_team_view(item)


class CreatePayload(_CamelModel):
    id: str
    name: str
    description: str = ""
    team_id: str | None = None
    model: str | None = None
    identity_emoji: str | None = None
    identity_theme: str | None = None
    nl_prompt: str = ""
    extra_skills: list[str] = Field(default_factory=list)


def _build_create_self_define_prompt(
    *,
    agent_id: str,
    agent_name: str,
    user_requirement: str,
) -> str:
    requirement = (user_requirement or "").strip() or "No additional responsibility details were provided."
    return (
        f"You are `{agent_name}` (id: `{agent_id}`). Your workspace has already been initialized "
        "by the system and registered to OpenClaw runtime.\n\n"
        "Please directly improve and correct your definition documents in the current workspace. "
        "Do not create duplicate templates and do not modify the shared system-rules section.\n\n"
        "Following OpenClaw best practices, prioritize improving these files:\n"
        "- `AGENTS.md`: operating rules and memory strategy (only `AGENTS_USER_CUSTOM_SECTION` can be edited)\n"
        "- `SOUL.md`: personality style, values, and behavioral boundaries\n"
        "- `USER.md`: owner profile, communication preferences, and delivery preferences\n"
        "- `IDENTITY.md`: your name, role positioning, temperament, and boundaries\n"
        "- `TOOLS.md`: local tool conventions, path rules, and usage boundaries\n"
        "- `HEARTBEAT.md`: short periodic checklist, keep it concise\n"
        "- `MEMORY.md`: long-term high-value memory (avoid verbose logs)\n\n"
        "Enrich these documents professionally based on your role definition. Keep them clear, "
        "structured, concise, and correct any unprofessional wording. `my-desktop/` is your work-material "
        "area with full edit authority. Design and create its internal structure based on responsibilities, "
        "keep management logic clear and concise, create only necessary directories, remove unsuitable ones, "
        "and add templates for work folders when useful. For your business domain, this area should dynamically "
        "track core content from interactions with the owner and key experience/insights accumulated during work. "
        "After designing management rules for this directory, write the core rules into AGENTS.md user custom section.\n\n"
        f"User-provided responsibility requirements (must be incorporated):\n{requirement}\n\n"
        "After completion, reply with:\n"
        "1) Which files you modified.\n"
        "2) Key changes for each file.\n"
        "3) Final summary of your responsibility boundaries."
    )


def _commit_bootstrap_workspace(*, workspace_path: str, agent_id: str) -> str:
    workspace = FsPath(workspace_path).expanduser().resolve(strict=False)
    if not workspace.exists() or not workspace.is_dir():
        raise ApiError(
            "WORKSPACE_NOT_FOUND",
            f"workspace not found: {workspace}",
            status_code=500,
        )

    def _run(argv: list[str]) -> str:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
            raise ApiError(
                "WORKSPACE_GIT_FAILED",
                f"failed to run {' '.join(argv[:2])}: {detail[:800]}",
                status_code=500,
            )
        return (proc.stdout or "").strip()

    _run(["git", "add", "-A"])
    _run(
        [
            "git",
            "commit",
            "--allow-empty",
            "-m",
            f"{_CREATE_SELF_DEFINE_COMMIT_MESSAGE_PREFIX} for {agent_id}",
        ]
    )
    sha = _run(["git", "rev-parse", "HEAD"])
    return sha


@router.post("", response_model=OpenclawAgentDetail, status_code=201)
async def create_agent(
    payload: Annotated[CreatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentDetail:
    if _contains_whitespace(payload.id):
        raise ApiError(
            "INVALID_PAYLOAD",
            "agent id must not contain whitespace",
            status_code=400,
        )
    if not payload.name.strip():
        raise ApiError(
            "INVALID_PAYLOAD",
            "agent name is required",
            status_code=400,
        )
    if _resolve_openclaw_executable() is None:
        raise ApiError(
            "OPENCLAW_CLI_MISSING",
            "openclaw CLI is not available in PATH",
            status_code=503,
        )

    agent_id = payload.id.strip()
    if agent_id in _REQUESTED_AGENT_CREATE_CANCELLATIONS or svc.is_create_cancelled(agent_id):
        raise ApiError(
            "AGENT_CREATE_CANCELLED",
            f'agent creation for "{agent_id}" was cancelled',
            status_code=409,
            details={"agent_id": agent_id},
        )

    cmd = svc.CommitInput(
        id=payload.id,
        name=payload.name,
        description=payload.description,
        identity=svc.AgentIdentity(
            emoji=payload.identity_emoji,
            theme=payload.identity_theme,
        ),
        model=payload.model,
        nl_prompt=payload.nl_prompt,
        extra_skills=tuple(payload.extra_skills),
    )
    try:
        created = await svc.commit_agent(
            cmd,
            user=user,
            team_id=payload.team_id,
            storage=storage,
        )
    except svc.AgentCreateCancelled as exc:
        raise ApiError(
            "AGENT_CREATE_CANCELLED",
            str(exc),
            status_code=409,
            details={"agent_id": payload.id.strip()},
        ) from exc

    # Op-status tracking starts once registration succeeds (the pre-registration
    # window is short and covered by the GET in-flight layer); op_id uses the
    # canonical created.id. The DB row already exists here, but bootstrap (below)
    # is the long part the frontend recovers across refresh/close.
    op_id = f"openclaw_create:{created.id}"
    reg = get_op_registry()
    reg.start(op_id=op_id, user=user, kind="openclaw_create")
    cancellation_event = _register_agent_create_cancellation(created.id)
    bootstrap_prompt = _build_create_self_define_prompt(
        agent_id=created.id,
        agent_name=created.name,
        user_requirement=payload.description or payload.nl_prompt,
    )
    session_key = f"{openclaw_user_chat_session_id(user, created.id)}-bootstrap-{int(time.time())}"

    async def _bootstrap_and_finish() -> OpenclawAgentDetail:
        try:
            if _should_wait_until_gateway_agent_ready():
                await _wait_until_gateway_agent_ready(agent_id=created.id)
            _raise_if_agent_create_cancelled(agent_id=created.id)
            bootstrap_reply = await _chat_completion_via_cli(
                agent_id=created.id,
                session_key=session_key,
                message=bootstrap_prompt,
                model_override=payload.model,
                timeout_sec=_chat_cli_timeout_seconds(),
            )
            _raise_if_agent_create_cancelled(agent_id=created.id)
            bootstrap_text = _normalize_assistant_text(_extract_chunk_text(bootstrap_reply))
            commit_sha = await asyncio.to_thread(
                _commit_bootstrap_workspace,
                workspace_path=created.workspace_path,
                agent_id=created.id,
            )
        except Exception as exc:
            cleanup_error = await _cleanup_failed_agent_create(
                agent_id=created.id,
                storage=storage,
            )
            if cleanup_error:
                logger.warning(
                    "openclaw_agent_create_bootstrap_cleanup_failed",
                    agent_id=created.id,
                    error=cleanup_error,
                )
            if isinstance(exc, ApiError) and exc.code == "AGENT_CREATE_CANCELLED":
                reg.fail(op_id, detail="cancelled", result={"agentId": created.id})
                raise ApiError(
                    "AGENT_CREATE_CANCELLED",
                    f'agent creation cancelled for "{created.id}"',
                    status_code=409,
                    details={
                        "agent_id": created.id,
                        "workspace": created.workspace_path,
                        **({"cleanup_error": cleanup_error} if cleanup_error else {}),
                    },
                ) from exc
            message = str(exc)
            if isinstance(exc, ApiError):
                message = f"{exc.code}: {exc.message}"
            reg.fail(op_id, detail=message)
            raise ApiError(
                "AGENT_BOOTSTRAP_FAILED",
                f"agent bootstrap failed after registration: {message}",
                status_code=502,
                details={
                    "agent_id": created.id,
                    "workspace": created.workspace_path,
                    **({"cleanup_error": cleanup_error} if cleanup_error else {}),
                },
            ) from exc
        finally:
            _unregister_agent_create_cancellation(
                agent_id=created.id,
                event=cancellation_event,
            )
            _clear_agent_create_cancellation_request(created.id)
            svc.finish_create_in_flight(created.id)
            svc.clear_create_cancellation(created.id)

        logger.info(
            "openclaw_agent_create_bootstrap_completed",
            agent_id=created.id,
            session_key=session_key,
            bootstrap_reply_excerpt=bootstrap_text[:280],
            workspace_commit=commit_sha,
        )
        reg.succeed(op_id, result={"agentId": created.id})
        team_name = _team_name_map(storage=storage, user=user).get(created.team_id, "")
        return _to_detail(created, team_name=team_name)

    # Detached + shield: a tab switch / client abort cancels the request
    # coroutine but the bootstrap keeps running and records the op terminal
    # state — otherwise recovery falsely succeeds via entity-existence fallback
    # while the self-definition turn never ran. Track the task by id so a later
    # cancel-create can await its actual release (see _await_create_release).
    bootstrap_task = _spawn_detached_create(_bootstrap_and_finish())
    _track_agent_create_task(created.id, bootstrap_task)
    return await asyncio.shield(bootstrap_task)


@router.post("/{agent_id}/cancel-create", status_code=202)
async def cancel_create_agent(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    target = (agent_id or "").strip()
    if not target:
        raise ApiError(
            "INVALID_PAYLOAD",
            "agent id is required",
            status_code=400,
        )
    if _contains_whitespace(target):
        raise ApiError(
            "INVALID_PAYLOAD",
            "agent id must not contain whitespace",
            status_code=400,
        )
    try:
        row = svc.get_agent(target, storage=storage)
    except svc.AgentNotFound:
        row = None
    if row is not None:
        _ensure_owner(row, user)
    cancel_requested = _request_agent_create_cancellation(target)
    cleanup_error = await _cleanup_failed_agent_create(
        agent_id=target,
        storage=storage,
    )
    if cleanup_error:
        logger.warning(
            "openclaw_agent_cancel_create_cleanup_failed",
            agent_id=target,
            error=cleanup_error,
        )
    await _wait_until_agent_create_cleanup_visible(
        agent_id=target,
        storage=storage,
    )
    await _await_create_release(agent_id=target)
    op_id = f"openclaw_create:{target}"
    reg = get_op_registry()
    op = reg.get(op_id, user=user)
    if op is not None and op.state == "running":
        reg.fail(op_id, detail="cancelled", result={"agentId": target})
    _clear_agent_create_cancellation_request(target)
    svc.clear_create_cancellation(target)
    logger.info(
        "openclaw_agent_cancel_create_requested",
        agent_id=target,
        cancel_requested=cancel_requested,
        cleanup_error=bool(cleanup_error),
    )


@router.get("", response_model=OpenclawAgentListResponse)
def list_agents(
    user: UserDep,
    storage: StorageDep,
    all_users: Annotated[bool, Query(alias="allUsers")] = False,
) -> OpenclawAgentListResponse:
    cfg = load_config()
    caps = get_deployment_capabilities(cfg)
    if all_users and not caps.allow_all_users_query:
        raise ApiError(
            "FORBIDDEN",
            "allUsers=true is disabled in server mode until RBAC is enabled",
            status_code=403,
        )
    items = svc.list_agents(
        user=None if all_users else user,
        storage=storage,
    )
    team_names = _team_name_map(
        storage=storage,
        user=None if all_users else user,
    )
    return OpenclawAgentListResponse(
        items=[
            _to_summary(a, team_name=team_names.get(a.team_id, ""))
            for a in items
        ],
    )


@router.get("/runtime/status", response_model=OpenclawRuntimeStatusResponse)
def openclaw_runtime_status(
    user: UserDep,
    mode: Annotated[Literal["fast", "strict"], Query()] = "fast",
) -> OpenclawRuntimeStatusResponse:
    del user  # reserved for future RBAC scope checks
    cfg = load_config()
    if mode == "strict":
        running, reason = svc.probe_runtime_running_strict(config=cfg)
    else:
        running, reason = svc.probe_runtime_running(config=cfg)
    return OpenclawRuntimeStatusResponse(
        running=running,
        reason=reason,
        gateway_url=svc.resolve_runtime_gateway_url(config=cfg),
    )


@router.get("/import/candidates", response_model=ExternalImportCandidateListResponse)
def list_import_candidates(
    user: UserDep,
    storage: StorageDep,
) -> ExternalImportCandidateListResponse:
    del user  # local mode single-user; kept for signature symmetry
    items = svc.list_external_import_candidates(storage=storage)
    views = [_to_external_import_candidate_view(item) for item in items]
    return ExternalImportCandidateListResponse(items=views, total=len(views))


@router.post("/import", response_model=ImportExternalAgentsResponse)
async def import_external_agents(
    payload: Annotated[ImportExternalAgentsPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> ImportExternalAgentsResponse:
    candidates = svc.list_external_import_candidates(storage=storage)
    candidate_ids = {item.id for item in candidates}
    if payload.import_all:
        requested_ids = [item.id for item in candidates]
    else:
        requested_ids = list(dict.fromkeys((x or "").strip() for x in payload.agent_ids if (x or "").strip()))
    if not requested_ids:
        raise ApiError(
            "INVALID_PAYLOAD",
            "no external agents selected for import",
            status_code=400,
        )
    unknown = sorted(set(requested_ids) - candidate_ids)
    if unknown:
        raise ApiError(
            "EXTERNAL_AGENT_NOT_FOUND",
            f"some selected agents are not import candidates: {', '.join(unknown)}",
            status_code=404,
            details={"missing_ids": unknown},
        )

    imported: list[ImportedExternalAgentView] = []
    failed: list[ImportExternalAgentFailure] = []
    cancelled = False
    reg = get_op_registry()
    batch_op = f"openclaw_import_batch:{payload.batch_id}" if payload.batch_id else None
    if batch_op:
        reg.start(op_id=batch_op, user=user, kind="openclaw_import_batch")
    for source_agent_id in requested_ids:
        # Cancel is checked between agents (chosen semantics: keep what's already
        # imported, skip the rest). The agent being processed when cancel lands
        # finishes so it is never left half-imported.
        if _is_import_cancelled(payload.batch_id):
            cancelled = True
            break
        op_id = f"openclaw_import:{source_agent_id}"
        reg.start(op_id=op_id, user=user, kind="openclaw_import")
        imported_item: svc.ImportedExternalAgent | None = None
        try:
            imported_item = await svc.import_external_agent(
                source_agent_id,
                user=user,
                team_id=payload.team_id,
                storage=storage,
            )
            await _run_import_optimization_chat(
                user,
                imported_item.target_agent_id,
                imported_item.target_workspace_path,
            )
        except svc.OpenclawAgentError as exc:
            reg.fail(op_id, detail=f"{exc.code}: {exc.message}")
            failed.append(
                ImportExternalAgentFailure(
                    source_agent_id=source_agent_id,
                    error_code=exc.code,
                    message=exc.message,
                ),
            )
            continue
        except Exception as exc:
            cleanup_error = ""
            if imported_item is not None:
                try:
                    await svc.delete_agent(
                        imported_item.target_agent_id,
                        mode="purge",
                        storage=storage,
                    )
                except Exception as cleanup_exc:
                    cleanup_error = str(cleanup_exc)
                    logger.warning(
                        "import_optimization_cleanup_failed",
                        source_agent_id=source_agent_id,
                        target_agent_id=imported_item.target_agent_id,
                        error=str(cleanup_exc),
                    )
            if isinstance(exc, ApiError):
                code = exc.code
                message = exc.message
            else:
                code = "IMPORT_OPTIMIZATION_FAILED"
                message = str(exc) or "import optimization failed"
            if cleanup_error:
                message = f"{message} (cleanup_error={cleanup_error})"
            reg.fail(op_id, detail=f"{code}: {message}")
            failed.append(
                ImportExternalAgentFailure(
                    source_agent_id=source_agent_id,
                    error_code=code,
                    message=message,
                ),
            )
            continue
        reg.succeed(op_id, result={"targetAgentId": imported_item.target_agent_id})
        imported.append(_to_imported_external_agent_view(imported_item))
    # Also honour a cancel that landed while the LAST agent was processing (loop
    # ended normally without hitting the top-of-loop check).
    if _is_import_cancelled(payload.batch_id):
        cancelled = True
    # Free the flag for this (always-unique) batch id; a leaked flag would never
    # match a future import (fresh UUID each run) but we clean up anyway.
    _clear_import_cancellation(payload.batch_id)
    if batch_op:
        if cancelled:
            # Cancelled mid-run: mark the batch op cancelled (mirrors the create
            # cancel terminal state the frontend verifies against).
            reg.fail(
                batch_op,
                detail="cancelled",
                result={"importedCount": len(imported), "failedCount": len(failed)},
            )
        else:
            # The batch "succeeds" as a whole (per-item failures are in `failed`);
            # the UI shows success iff failed == 0, matching the in-page path.
            reg.succeed(
                batch_op,
                result={"importedCount": len(imported), "failedCount": len(failed)},
            )
    return ImportExternalAgentsResponse(
        requested_count=len(requested_ids),
        imported=imported,
        failed=failed,
        cancelled=cancelled,
    )


@router.post("/import/{batch_id}/cancel", status_code=202)
async def cancel_import(
    batch_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    """Cancel an in-flight external-import batch.

    Chosen semantics: agents already imported are KEPT; the import loop stops
    before the next agent. The batch op is marked ``failed`` / ``cancelled`` so
    the frontend's cancel verify converges immediately even while the agent
    currently being processed finishes (it is never left half-imported)."""
    del storage
    target = (batch_id or "").strip()
    if not target:
        raise ApiError("INVALID_PAYLOAD", "batch id is required", status_code=400)
    _request_import_cancellation(target)
    op_id = f"openclaw_import_batch:{target}"
    reg = get_op_registry()
    op = reg.get(op_id, user=user)
    if op is not None and op.state == "running":
        reg.fail(op_id, detail="cancelled", result=op.result)
    logger.info("openclaw_import_cancel_requested", batch_id=target)


@router.get("/{agent_id}", response_model=OpenclawAgentDetail)
def get_agent(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentDetail:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    team_name = _team_name_map(storage=storage, user=user).get(row.team_id, "")
    return _to_detail(row, team_name=team_name)


class UpdatePayload(_CamelModel):
    name: str | None = None
    description: str | None = None
    team_id: str | None = None
    model: str | None = None
    identity_emoji: str | None = None
    identity_theme: str | None = None


@router.patch("/{agent_id}", response_model=OpenclawAgentDetail)
async def patch_agent(
    agent_id: Annotated[str, Path()],
    payload: Annotated[UpdatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentDetail:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    if payload.name is not None and not payload.name.strip():
        raise ApiError(
            "INVALID_PAYLOAD",
            "agent name is required",
            status_code=400,
        )
    team_id_patch: str | None = None
    if "team_id" in payload.model_fields_set:
        team_id_patch = (payload.team_id or "").strip()
    identity = None
    if payload.identity_emoji is not None or payload.identity_theme is not None:
        identity = svc.AgentIdentity(
            emoji=payload.identity_emoji,
            theme=payload.identity_theme,
        )
    patch = svc.UpdateInput(
        name=payload.name,
        description=payload.description,
        team_id=team_id_patch,
        model=payload.model,
        identity=identity,
    )
    updated = await svc.update_agent(agent_id, patch, storage=storage)
    team_name = _team_name_map(storage=storage, user=user).get(updated.team_id, "")
    return _to_detail(updated, team_name=team_name)


@router.get("/restore/candidates", response_model=OpenclawRestorableListResponse)
def list_restore_candidates(
    user: UserDep,
    storage: StorageDep,
) -> OpenclawRestorableListResponse:
    team_map = _team_name_map(storage=storage, user=user)
    items = svc.list_restorable_agents(user=user, storage=storage)
    views = [
        _to_restorable_summary(item, team_name=team_map.get(item.team_id, ""))
        for item in items
    ]
    return OpenclawRestorableListResponse(items=views, total=len(views))


@router.post("/restore/{agent_id}", response_model=OpenclawAgentDetail)
async def restore_agent(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentDetail:
    restored = await svc.restore_agent_registration(agent_id, user=user, storage=storage)
    team_name = _team_name_map(storage=storage, user=user).get(restored.team_id, "")
    return _to_detail(restored, team_name=team_name)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
    mode: Annotated[
        str | None,
        Query(description="unregister | purge"),
    ] = None,
    purge: Annotated[bool, Query(description="deprecated; use mode=purge")] = False,
) -> None:
    if mode is None:
        mode = "purge" if purge else "unregister"
    try:
        row = svc.get_agent(agent_id, storage=storage)
    except svc.AgentNotFound:
        if mode != "purge" or not svc.user_may_purge_workspace_orphan(
            agent_id,
            user=user,
            storage=storage,
        ):
            raise
    else:
        _ensure_owner(row, user)
    await svc.delete_agent(agent_id, mode=mode, storage=storage)


# ──────────────────────────────────────────────────────────────────────
# Runtime settings (skills / cron / hooks / AGENTS custom section)
# ──────────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/settings", response_model=OpenclawAgentSettingsResponse)
def get_agent_settings(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentSettingsResponse:
    cached = _settings_cache_get(user=user, agent_id=agent_id)
    if cached is not None:
        return cached
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    payload = _collect_agent_settings(agent=row)
    _settings_cache_put(user=user, agent_id=agent_id, payload=payload)
    return payload


@router.get("/{agent_id}/settings/skills", response_model=list[OpenclawAgentSkillView])
def get_agent_settings_skills(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> list[OpenclawAgentSkillView]:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    return _collect_settings_skills(workspace=workspace)


@router.get("/{agent_id}/settings/cron", response_model=list[OpenclawAgentCronView])
def get_agent_settings_cron(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> list[OpenclawAgentCronView]:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    return _collect_settings_cron_jobs(agent=row, workspace=workspace)


@router.get("/{agent_id}/settings/hooks", response_model=list[OpenclawAgentHookView])
def get_agent_settings_hooks(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> list[OpenclawAgentHookView]:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    return _collect_settings_hooks(agent=row, workspace=workspace)


@router.get(
    "/{agent_id}/settings/agents-custom-section",
    response_model=OpenclawAgentCustomSectionView,
)
def get_agent_custom_section(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentCustomSectionView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    return OpenclawAgentCustomSectionView(
        content=_collect_settings_custom_section(workspace=workspace),
    )


@router.post(
    "/{agent_id}/settings/skills",
    response_model=OpenclawAgentSkillView,
    status_code=201,
)
def create_agent_skill(
    agent_id: Annotated[str, Path()],
    payload: Annotated[OpenclawAgentSkillUpsertPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentSkillView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    name = _normalize_fs_item_name(payload.name, field="skill name")
    if name in _system_skill_names():
        raise ApiError(
            "SKILL_RESERVED",
            f"skill {name!r} is system-managed and cannot be modified",
            status_code=409,
        )
    content = payload.content
    description = (payload.description or "").strip()
    if content is None:
        # Backward compatibility for old clients sending ``skillMd`` only.
        legacy_md = (payload.skill_md or "").strip()
        if legacy_md:
            _, parsed_description, parsed_content = _parse_skill_document(
                raw=legacy_md,
                fallback_name=name,
            )
            description = description or parsed_description
            content = parsed_content
    content = (content or "").strip()
    if not content:
        raise ApiError(
            "INVALID_PAYLOAD",
            "content is required",
            status_code=400,
        )
    skills_root = workspace / _SKILLS_DIRNAME
    skill_dir = skills_root / name
    if skill_dir.exists():
        raise ApiError(
            "SKILL_EXISTS",
            f"skill already exists: {name}",
            status_code=409,
        )
    skill_dir.mkdir(parents=True, exist_ok=False)
    skill_md_path = skill_dir / _SKILL_ENTRY_FILENAME
    skill_doc = _build_skill_document(
        name=name,
        description=description,
        content=content,
    )
    skill_md_path.write_text(skill_doc, encoding="utf-8")
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return OpenclawAgentSkillView(
        name=name,
        description=description,
        content=content,
        path=str(skill_md_path),
    )


@router.patch(
    "/{agent_id}/settings/skills/{skill_name}",
    response_model=OpenclawAgentSkillView,
)
def patch_agent_skill(
    agent_id: Annotated[str, Path()],
    skill_name: Annotated[str, Path()],
    payload: Annotated[OpenclawAgentSkillPatchPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentSkillView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    old_name = _normalize_fs_item_name(skill_name, field="skill name")
    new_name = _normalize_fs_item_name(payload.name, field="skill name") if payload.name else old_name
    system = _system_skill_names()
    if old_name in system or new_name in system:
        raise ApiError(
            "SKILL_RESERVED",
            "system-managed skill cannot be edited",
            status_code=409,
        )
    old_dir = workspace / _SKILLS_DIRNAME / old_name
    if not old_dir.exists() or not old_dir.is_dir():
        raise ApiError(
            "SKILL_NOT_FOUND",
            f"skill not found: {old_name}",
            status_code=404,
        )
    target_dir = old_dir
    if new_name != old_name:
        target_dir = workspace / _SKILLS_DIRNAME / new_name
        if target_dir.exists():
            raise ApiError(
                "SKILL_EXISTS",
                f"skill already exists: {new_name}",
                status_code=409,
            )
        old_dir.rename(target_dir)
    skill_md_path = target_dir / _SKILL_ENTRY_FILENAME
    current = skill_md_path.read_text(encoding="utf-8") if skill_md_path.exists() else ""
    _, current_description, current_content = _parse_skill_document(
        raw=current,
        fallback_name=old_name,
    )
    next_description = (
        payload.description.strip()
        if payload.description is not None
        else current_description
    )
    next_content = payload.content
    if next_content is None and payload.skill_md is not None:
        _, parsed_description, parsed_content = _parse_skill_document(
            raw=payload.skill_md,
            fallback_name=new_name,
        )
        if payload.description is None:
            next_description = parsed_description
        next_content = parsed_content
    if next_content is None:
        next_content = current_content
    next_content = next_content.strip()
    if not next_content:
        raise ApiError(
            "INVALID_PAYLOAD",
            "content cannot be empty",
            status_code=400,
        )
    skill_doc = _build_skill_document(
        name=new_name,
        description=next_description,
        content=next_content,
    )
    skill_md_path.write_text(skill_doc, encoding="utf-8")
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return OpenclawAgentSkillView(
        name=new_name,
        description=next_description,
        content=next_content,
        path=str(skill_md_path),
    )


@router.delete("/{agent_id}/settings/skills/{skill_name}", status_code=204)
def delete_agent_skill(
    agent_id: Annotated[str, Path()],
    skill_name: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    name = _normalize_fs_item_name(skill_name, field="skill name")
    if name in _system_skill_names():
        raise ApiError(
            "SKILL_RESERVED",
            "system-managed skill cannot be deleted",
            status_code=409,
        )
    skill_dir = workspace / _SKILLS_DIRNAME / name
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise ApiError(
            "SKILL_NOT_FOUND",
            f"skill not found: {name}",
            status_code=404,
        )
    shutil.rmtree(skill_dir)
    _settings_cache_invalidate(agent_id=agent_id, user=user)


@router.post(
    "/{agent_id}/settings/cron",
    response_model=OpenclawAgentCronView,
    status_code=201,
)
def create_agent_cron_job(
    agent_id: Annotated[str, Path()],
    payload: Annotated[OpenclawAgentCronCreatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentCronView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    name = payload.name.strip()
    cron_expr, tz = _resolve_cron_create_payload(payload)
    message = payload.message.strip()
    if not name or not cron_expr or not message:
        raise ApiError(
            "INVALID_PAYLOAD",
            "name, schedule and message are required",
            status_code=400,
        )
    existing = next(
        (
            item
            for item in _list_agent_cron_jobs(agent_id=row.id, workspace=workspace)
            if str(item.get("name") or "").strip() == name
        ),
        None,
    )
    if isinstance(existing, dict):
        raise ApiError(
            "CRON_JOB_EXISTS",
            f"cron job already exists: {name}",
            status_code=409,
            details={"name": name, "jobId": str(existing.get("id") or "")},
        )
    args = [
        "cron",
        "add",
        "--name",
        name,
        "--cron",
        cron_expr,
        "--tz",
        tz,
        "--session",
        "isolated",
        "--agent",
        row.id,
        "--message",
        message,
        "--json",
    ]
    if not payload.enabled:
        args.append("--disabled")
    out = _run_openclaw_cli(args=args, cwd=str(workspace), expect_json=True)
    if not isinstance(out, dict):
        raise ApiError(
            "OPENCLAW_CLI_BAD_OUTPUT",
            "cron add returned invalid payload",
            status_code=502,
        )
    created = out.get("job")
    if not isinstance(created, dict):
        created = out
    job_id = str(created.get("id") or "")
    if not job_id:
        # Fallback when CLI omits id in the add response.
        matches = [
            item
            for item in _list_agent_cron_jobs(agent_id=row.id, workspace=workspace)
            if str(item.get("name") or "") == name
        ]
        if matches:
            matches.sort(key=lambda item: int(item.get("createdAtMs") or 0), reverse=True)
            job_id = str(matches[0].get("id") or "")
    if not job_id:
        raise ApiError(
            "OPENCLAW_CLI_BAD_OUTPUT",
            "cron add response missing job id",
            status_code=502,
        )
    job = _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return _to_cron_view(job, agent_id=row.id)


@router.patch(
    "/{agent_id}/settings/cron/{job_id}",
    response_model=OpenclawAgentCronView,
)
def patch_agent_cron_job(
    agent_id: Annotated[str, Path()],
    job_id: Annotated[str, Path()],
    payload: Annotated[OpenclawAgentCronPatchPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentCronView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    job = _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    system_builtin = _is_system_cron_job(job, agent_id=row.id)
    resolved_schedule = _resolve_cron_patch_expr(payload)
    non_toggle_change = any(
        value is not None
        for value in (
            payload.name,
            payload.cron_expr,
            payload.schedule_mode,
            payload.schedule_time,
            payload.schedule_weekday,
            payload.schedule_day_of_month,
            payload.message,
        )
    )
    if system_builtin and non_toggle_change:
        raise ApiError(
            "CRON_JOB_READONLY",
            "system built-in cron can only be enabled/disabled",
            status_code=409,
        )
    args = ["cron", "edit", job_id]
    if payload.name is not None:
        args.extend(["--name", payload.name.strip()])
    if resolved_schedule is not None:
        cron_expr, tz = resolved_schedule
        args.extend(["--cron", cron_expr, "--tz", tz])
    if payload.message is not None:
        args.extend(["--message", payload.message.strip()])
    if payload.enabled is True:
        args.append("--enable")
    elif payload.enabled is False:
        args.append("--disable")
    if len(args) > 3:
        _run_openclaw_cli(args=args, cwd=str(workspace), expect_json=False)
    updated = _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return _to_cron_view(updated, agent_id=row.id)


@router.delete("/{agent_id}/settings/cron/{job_id}", status_code=204)
def delete_agent_cron_job(
    agent_id: Annotated[str, Path()],
    job_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    job = _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    if _is_system_cron_job(job, agent_id=row.id):
        raise ApiError(
            "CRON_JOB_READONLY",
            "system built-in cron cannot be deleted",
            status_code=409,
        )
    _run_openclaw_cli(
        args=["cron", "rm", job_id, "--json"],
        cwd=str(workspace),
        expect_json=False,
    )
    _settings_cache_invalidate(agent_id=agent_id, user=user)


@router.post(
    "/{agent_id}/settings/cron/{job_id}/enable",
    response_model=OpenclawAgentCronView,
)
def enable_agent_cron_job(
    agent_id: Annotated[str, Path()],
    job_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentCronView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    _run_openclaw_cli(args=["cron", "enable", job_id], cwd=str(workspace), expect_json=False)
    updated = _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return _to_cron_view(updated, agent_id=row.id)


@router.post(
    "/{agent_id}/settings/cron/{job_id}/disable",
    response_model=OpenclawAgentCronView,
)
def disable_agent_cron_job(
    agent_id: Annotated[str, Path()],
    job_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentCronView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    _run_openclaw_cli(args=["cron", "disable", job_id], cwd=str(workspace), expect_json=False)
    updated = _find_agent_cron_job(agent_id=row.id, job_id=job_id, workspace=workspace)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return _to_cron_view(updated, agent_id=row.id)


@router.post(
    "/{agent_id}/settings/hooks",
    response_model=OpenclawAgentHookView,
    status_code=201,
)
async def create_agent_hook(
    agent_id: Annotated[str, Path()],
    payload: Annotated[OpenclawAgentHookUpsertPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentHookView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    name = _normalize_fs_item_name(payload.name, field="hook name")
    existing = _find_hook_view(settings=_collect_agent_settings(agent=row), hook_name=name)
    if existing is not None and existing.system_builtin:
        raise ApiError(
            "HOOK_READONLY",
            f"hook {name!r} is system built-in and cannot be overwritten",
            status_code=409,
        )
    hooks_root = workspace / _HOOKS_DIRNAME
    hook_dir = hooks_root / name
    if hook_dir.exists():
        raise ApiError("HOOK_EXISTS", f"hook already exists: {name}", status_code=409)
    hook_md = payload.hook_md.strip()
    handler_ts = payload.handler_ts.strip()
    if not hook_md or not handler_ts:
        raise ApiError(
            "INVALID_PAYLOAD",
            "hook_md and handler_ts are required",
            status_code=400,
        )
    hook_dir.mkdir(parents=True, exist_ok=False)
    (hook_dir / _HOOK_MD_FILENAME).write_text(f"{hook_md}\n", encoding="utf-8")
    (hook_dir / _HOOK_HANDLER_FILENAME).write_text(f"{handler_ts}\n", encoding="utf-8")
    await _set_hook_enabled(hook_name=name, enabled=payload.enabled, workspace=workspace)
    refreshed = _collect_agent_settings(agent=row)
    view = _find_hook_view(settings=refreshed, hook_name=name)
    if view is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found after create: {name}", status_code=500)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return view


@router.patch(
    "/{agent_id}/settings/hooks/{hook_name}",
    response_model=OpenclawAgentHookView,
)
async def patch_agent_hook(
    agent_id: Annotated[str, Path()],
    hook_name: Annotated[str, Path()],
    payload: Annotated[OpenclawAgentHookPatchPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentHookView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    old_name = _normalize_fs_item_name(hook_name, field="hook name")
    settings = _collect_agent_settings(agent=row)
    current = _find_hook_view(settings=settings, hook_name=old_name)
    if current is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found: {old_name}", status_code=404)
    if current.system_builtin:
        raise ApiError(
            "HOOK_READONLY",
            "system built-in hook cannot be edited",
            status_code=409,
        )
    new_name = _normalize_fs_item_name(payload.name, field="hook name") if payload.name else old_name
    if new_name != old_name:
        conflict = _find_hook_view(settings=settings, hook_name=new_name)
        if conflict is not None:
            raise ApiError(
                "HOOK_EXISTS",
                f"hook already exists: {new_name}",
                status_code=409,
            )
    hooks_root = workspace / _HOOKS_DIRNAME
    old_dir = hooks_root / old_name
    if not old_dir.exists() or not old_dir.is_dir():
        raise ApiError("HOOK_NOT_FOUND", f"hook directory missing: {old_name}", status_code=404)
    target_dir = old_dir
    if new_name != old_name:
        target_dir = hooks_root / new_name
        old_dir.rename(target_dir)
        await _remove_hook_entry_fallback(hook_name=old_name)
    hook_md_path = target_dir / _HOOK_MD_FILENAME
    handler_path = target_dir / _HOOK_HANDLER_FILENAME
    if payload.hook_md is not None:
        md_text = payload.hook_md.strip()
        if not md_text:
            raise ApiError("INVALID_PAYLOAD", "hook_md cannot be empty", status_code=400)
        hook_md_path.write_text(f"{md_text}\n", encoding="utf-8")
    if payload.handler_ts is not None:
        ts_text = payload.handler_ts.strip()
        if not ts_text:
            raise ApiError("INVALID_PAYLOAD", "handler_ts cannot be empty", status_code=400)
        handler_path.write_text(f"{ts_text}\n", encoding="utf-8")
    if payload.enabled is not None:
        await _set_hook_enabled(hook_name=new_name, enabled=payload.enabled, workspace=workspace)
    refreshed = _collect_agent_settings(agent=row)
    view = _find_hook_view(settings=refreshed, hook_name=new_name)
    if view is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found after patch: {new_name}", status_code=500)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return view


@router.delete("/{agent_id}/settings/hooks/{hook_name}", status_code=204)
async def delete_agent_hook(
    agent_id: Annotated[str, Path()],
    hook_name: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    name = _normalize_fs_item_name(hook_name, field="hook name")
    settings = _collect_agent_settings(agent=row)
    current = _find_hook_view(settings=settings, hook_name=name)
    if current is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found: {name}", status_code=404)
    if current.system_builtin:
        raise ApiError(
            "HOOK_READONLY",
            "system built-in hook cannot be deleted",
            status_code=409,
        )
    hook_dir = workspace / _HOOKS_DIRNAME / name
    if hook_dir.exists() and hook_dir.is_dir():
        shutil.rmtree(hook_dir)
    await _remove_hook_entry_fallback(hook_name=name)
    _settings_cache_invalidate(agent_id=agent_id, user=user)


@router.post(
    "/{agent_id}/settings/hooks/{hook_name}/enable",
    response_model=OpenclawAgentHookView,
)
async def enable_agent_hook(
    agent_id: Annotated[str, Path()],
    hook_name: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentHookView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    name = _normalize_fs_item_name(hook_name, field="hook name")
    settings = _collect_agent_settings(agent=row)
    if _find_hook_view(settings=settings, hook_name=name) is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found: {name}", status_code=404)
    await _set_hook_enabled(hook_name=name, enabled=True, workspace=workspace)
    refreshed = _collect_agent_settings(agent=row)
    out = _find_hook_view(settings=refreshed, hook_name=name)
    if out is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found: {name}", status_code=404)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return out


@router.post(
    "/{agent_id}/settings/hooks/{hook_name}/disable",
    response_model=OpenclawAgentHookView,
)
async def disable_agent_hook(
    agent_id: Annotated[str, Path()],
    hook_name: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentHookView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    name = _normalize_fs_item_name(hook_name, field="hook name")
    settings = _collect_agent_settings(agent=row)
    if _find_hook_view(settings=settings, hook_name=name) is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found: {name}", status_code=404)
    await _set_hook_enabled(hook_name=name, enabled=False, workspace=workspace)
    refreshed = _collect_agent_settings(agent=row)
    out = _find_hook_view(settings=refreshed, hook_name=name)
    if out is None:
        raise ApiError("HOOK_NOT_FOUND", f"hook not found: {name}", status_code=404)
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return out


@router.put(
    "/{agent_id}/settings/agents-custom-section",
    response_model=OpenclawAgentCustomSectionView,
)
def update_agent_custom_section(
    agent_id: Annotated[str, Path()],
    payload: Annotated[OpenclawAgentCustomSectionPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> OpenclawAgentCustomSectionView:
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    workspace = _agent_workspace_dir(row)
    agents_md_path = workspace / "AGENTS.md"
    if not agents_md_path.exists() or not agents_md_path.is_file():
        raise ApiError(
            "AGENTS_FILE_NOT_FOUND",
            f"AGENTS.md not found: {agents_md_path}",
            status_code=404,
        )
    raw = agents_md_path.read_text(encoding="utf-8")
    merged = _replace_agents_custom_section(
        raw_agents_md=raw,
        custom_section=payload.content,
    )
    agents_md_path.write_text(merged, encoding="utf-8")
    _settings_cache_invalidate(agent_id=agent_id, user=user)
    return OpenclawAgentCustomSectionView(
        content=_extract_agents_custom_section(merged),
    )


# ──────────────────────────────────────────────────────────────────────
# Direct chat (SSE)
# ──────────────────────────────────────────────────────────────────────


class ChatMessage(_CamelModel):
    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str
    attachments: list[ChatAttachment] | None = None
    # Epoch ms the message was recorded server-side (chat-history responses only).
    ts: int | None = None


class ChatAttachment(_CamelModel):
    id: str
    name: str
    mime_type: str = ""
    size_bytes: int = 0
    absolute_path: str
    relative_path: str
    route: Literal["path_injection", "native"] = "path_injection"


class ChatPayload(_CamelModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    attachments: list[ChatAttachment] = Field(default_factory=list)
    stream: bool = True
    model_override: str | None = None


class ChatHistoryResponse(_CamelModel):
    messages: list[ChatMessage]


class ChatAttachmentUploadResponse(_CamelModel):
    attachment: ChatAttachment
    limits: dict[str, int]


def _session_key(user: str, agent_id: str) -> str:
    """Per-(user, agent) chat session, isolated from Flow dispatch sessions."""
    revision = _CHAT_SESSION_REVISIONS.get((user, agent_id), 0)
    suffix = "" if revision <= 0 else f"-r{revision}"
    return f"{openclaw_user_chat_session_id(user, agent_id)}{suffix}"


def _bump_session_revision(user: str, agent_id: str) -> str:
    slot = (user, agent_id)
    _CHAT_SESSION_REVISIONS[slot] = _CHAT_SESSION_REVISIONS.get(slot, 0) + 1
    return _session_key(user, agent_id)


async def _run_import_optimization_chat(
    user: str,
    agent_id: str,
    workspace_path: str,
) -> None:
    """Run one optimization turn and persist workspace updates for imported agent."""
    completion = await _chat_completion_via_cli(
        agent_id=agent_id,
        session_key=_session_key(user, agent_id),
        message=_IMPORT_OPTIMIZE_PROMPT,
        model_override=None,
        timeout_sec=_chat_cli_timeout_seconds(),
    )
    optimize_text = _normalize_assistant_text(_extract_chunk_text(completion))
    commit_sha = await asyncio.to_thread(
        _commit_bootstrap_workspace,
        workspace_path=workspace_path,
        agent_id=agent_id,
    )
    logger.info(
        "import_optimization_chat_completed",
        agent_id=agent_id,
        user=user,
        workspace_commit=commit_sha,
        optimization_reply_excerpt=optimize_text[:280],
    )


def _ensure_chat_target_access(agent_id: str, user: str, storage: StorageBackend) -> OpenclawAgent:
    """Ensure the target agent exists and belongs to the current user."""
    row = svc.get_agent(agent_id, storage=storage)
    _ensure_owner(row, user)
    return row


def _to_chat_attachment(
    item: attachment_svc.StoredAttachment,
    *,
    route: Literal["path_injection", "native"],
) -> ChatAttachment:
    return ChatAttachment(
        id=item.id,
        name=item.name,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        absolute_path=item.absolute_path,
        relative_path=item.relative_path,
        route=route,
    )


async def _store_openclaw_chat_upload(
    *,
    request: Request,
    workspace: FsPath,
    filename: str,
) -> attachment_svc.StoredAttachment:
    content_length = request.headers.get("content-length", "").strip()
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = -1
        if declared_size > attachment_svc.MAX_ATTACHMENT_SIZE_BYTES:
            raise ApiError(
                "ATTACHMENT_TOO_LARGE",
                "uploaded file exceeds size limit",
                status_code=413,
                details={"maxBytes": attachment_svc.MAX_ATTACHMENT_SIZE_BYTES},
            )
    body = await request.body()
    try:
        return attachment_svc.store_upload_bytes(
            base_dir=workspace,
            raw_filename=filename,
            mime_type=request.headers.get("content-type", ""),
            content=body,
        )
    except ValueError as exc:
        msg = str(exc)
        code = "INVALID_ATTACHMENT"
        status = 400
        if "size limit" in msg:
            code = "ATTACHMENT_TOO_LARGE"
            status = 413
        raise ApiError(code, msg, status_code=status) from exc


def _resolve_openclaw_payload_attachments(
    *,
    workspace: FsPath,
    attachments: list[ChatAttachment],
) -> list[attachment_svc.StoredAttachment]:
    resolved: list[attachment_svc.StoredAttachment] = []
    for item in attachments:
        try:
            resolved.append(
                attachment_svc.resolve_existing_attachment(
                    base_dir=workspace,
                    absolute_path=item.absolute_path,
                    name=item.name,
                    mime_type=item.mime_type,
                )
            )
        except ValueError as exc:
            raise ApiError("INVALID_ATTACHMENT", str(exc), status_code=400) from exc
    try:
        attachment_svc.validate_batch_limits(resolved)
    except ValueError as exc:
        message = str(exc)
        code = "INVALID_ATTACHMENT"
        status = 400
        if "count exceeds limit" in message:
            code = "ATTACHMENT_COUNT_EXCEEDED"
            status = 400
        elif "total size exceeds limit" in message:
            code = "ATTACHMENT_TOTAL_SIZE_EXCEEDED"
            status = 413
        raise ApiError(
            code,
            message,
            status_code=status,
            details={
                "maxCount": attachment_svc.MAX_ATTACHMENT_COUNT,
                "maxBytesPerFile": attachment_svc.MAX_ATTACHMENT_SIZE_BYTES,
                "maxTotalBytes": attachment_svc.MAX_ATTACHMENT_TOTAL_BYTES,
            },
        ) from exc
    return resolved


def _attachments_for_history(
    items: list[attachment_svc.StoredAttachment],
    *,
    route: Literal["path_injection", "native"],
) -> list[chat_history.ChatAttachmentMeta]:
    return [
        {
            "id": item.id,
            "name": item.name,
            "mime_type": item.mime_type,
            "size_bytes": item.size_bytes,
            "absolute_path": item.absolute_path,
            "relative_path": item.relative_path,
            "route": route,
        }
        for item in items
    ]


def _pick_latest_user_message(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Pick only the latest non-empty user turn for OpenClaw runtime."""
    for m in reversed(messages):
        role = str(m.get("role") or "")
        if role != "user":
            continue
        content = str(m.get("content") or "").strip()
        if content:
            return {"role": "user", "content": content}
    raise ApiError(
        "INVALID_PAYLOAD",
        "messages must include at least one non-empty user message",
        status_code=400,
    )


def _extract_chunk_text(chunk: dict[str, Any]) -> str:
    """Extract text from one OpenAI-compatible chunk/response."""
    choice = chunk.get("choices", [{}])[0]
    delta = choice.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]
    message = choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    return ""


def _normalize_assistant_text(text: str) -> str:
    if text.strip():
        return text
    return _NO_TEXT_REPLY_MARKER


def _extract_cli_assistant_text(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    raw_payloads = result.get("payloads")
    texts: list[str] = []
    if isinstance(raw_payloads, list):
        for item in raw_payloads:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
    if texts:
        return "\n".join(texts)
    for field in ("finalAssistantVisibleText", "finalAssistantRawText"):
        value = result.get(field)
        if isinstance(value, str):
            return value
    return ""


def _to_openai_chat_completion(
    *,
    agent_id: str,
    session_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    text = _extract_cli_assistant_text(payload)
    completion: dict[str, Any] = {
        "id": str(payload.get("runId") or f"chatcmpl-cli-{int(time.time())}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"openclaw/{agent_id}",
        "session_key": session_key,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    result = payload.get("result")
    if isinstance(result, dict):
        usage = ((result.get("meta") or {}).get("agentMeta") or {}).get("usage")
        if isinstance(usage, dict):
            prompt_tokens = usage.get("input")
            completion_tokens = usage.get("output")
            total_tokens = usage.get("total")
            if all(isinstance(v, int) for v in (prompt_tokens, completion_tokens, total_tokens)):
                completion["usage"] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }
    return completion


def _chat_cli_timeout_seconds() -> float:
    raw = os.getenv(_OPENCLAW_CLI_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_OPENCLAW_CLI_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_OPENCLAW_CLI_TIMEOUT_SEC
    return max(value, _MIN_OPENCLAW_CLI_TIMEOUT_SEC)


def _resolve_openclaw_executable() -> str | None:
    return resolve_openclaw_executable()


def _agent_ready_timeout_seconds() -> float:
    raw = os.getenv(_OPENCLAW_AGENT_READY_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_OPENCLAW_AGENT_READY_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_OPENCLAW_AGENT_READY_TIMEOUT_SEC
    return min(
        max(value, _MIN_OPENCLAW_AGENT_READY_TIMEOUT_SEC),
        _MAX_OPENCLAW_AGENT_READY_TIMEOUT_SEC,
    )


def _probe_gateway_agent_ready_once(
    *,
    agent_id: str,
    probe_timeout_sec: float | None = None,
) -> tuple[bool, str]:
    effective_timeout_sec = max(
        probe_timeout_sec or _OPENCLAW_AGENT_READY_PROBE_TIMEOUT_SEC,
        1.0,
    )
    timeout_ms = str(max(int(effective_timeout_sec * 1000), 1000))
    try:
        payload = _run_openclaw_cli(
            args=[
                "gateway",
                "call",
                "agents.list",
                "--json",
                "--params",
                "{}",
                "--timeout",
                timeout_ms,
            ],
            expect_json=True,
            timeout_sec=effective_timeout_sec,
        )
    except ApiError as exc:
        if looks_like_pending_scope_approval(exc.message):
            try:
                repair_pending_scope_upgrades()
            except Exception:
                pass
        return False, f"{exc.code}: {exc.message}"

    if not isinstance(payload, dict):
        return False, "gateway agents.list returned invalid payload"

    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list):
        return False, "gateway agents.list missing agents field"

    recognized = any(
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("id") == agent_id
        for item in raw_agents
    )
    if not recognized:
        return False, f'agent "{agent_id}" not present in gateway agents.list'

    # Agent-scoped command must also stop reporting unknown-id.
    try:
        _run_openclaw_cli(
            args=[
                "cron",
                "list",
                "--agent",
                agent_id,
                "--json",
                "--timeout",
                timeout_ms,
            ],
            expect_json=True,
            timeout_sec=effective_timeout_sec,
        )
    except ApiError as exc:
        lowered = exc.message.lower()
        if looks_like_pending_scope_approval(exc.message):
            try:
                repair_pending_scope_upgrades()
            except Exception:
                pass
        if "unknown agent id" in lowered:
            return False, f'agent "{agent_id}" still unknown for agent-scoped gateway calls'
        return False, f"{exc.code}: {exc.message}"

    return True, ""


async def _wait_until_gateway_agent_ready(*, agent_id: str) -> None:
    timeout_sec = _agent_ready_timeout_seconds()
    deadline = time.monotonic() + timeout_sec
    attempt = 0
    consecutive_success = 0
    last_detail = ""
    _raise_if_agent_create_cancelled(agent_id=agent_id)
    while True:
        attempt += 1
        remaining_before_probe = max(deadline - time.monotonic(), 0.0)
        probe_timeout_sec = max(
            min(_OPENCLAW_AGENT_READY_PROBE_TIMEOUT_SEC, remaining_before_probe),
            1.0,
        )
        ready, detail = await asyncio.to_thread(
            _probe_gateway_agent_ready_once,
            agent_id=agent_id,
            probe_timeout_sec=probe_timeout_sec,
        )
        _raise_if_agent_create_cancelled(agent_id=agent_id)
        if ready:
            consecutive_success += 1
            if consecutive_success >= _OPENCLAW_AGENT_READY_REQUIRED_CONSECUTIVE_SUCCESSES:
                logger.info(
                    "openclaw_agent_gateway_ready_confirmed",
                    agent_id=agent_id,
                    attempts=attempt,
                    timeout_sec=timeout_sec,
                    required_consecutive_successes=_OPENCLAW_AGENT_READY_REQUIRED_CONSECUTIVE_SUCCESSES,
                )
                return
        else:
            consecutive_success = 0
            last_detail = detail

        now = time.monotonic()
        if now >= deadline:
            raise ApiError(
                "OPENCLAW_GATEWAY_AGENT_NOT_READY",
                (
                    f'gateway did not confirm agent "{agent_id}" within {int(timeout_sec)}s; '
                    f'last_probe={last_detail or "unknown"}'
                ),
                status_code=504,
            )
        sleep_sec = min(_OPENCLAW_AGENT_READY_POLL_INTERVAL_SEC, max(deadline - now, 0.0))
        if sleep_sec <= 0:
            continue
        cancel_event = _agent_create_cancellation_event(agent_id)
        if cancel_event is None:
            await asyncio.sleep(sleep_sec)
        else:
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=sleep_sec)
            except asyncio.TimeoutError:
                pass
        _raise_if_agent_create_cancelled(agent_id=agent_id)


def _looks_like_unknown_agent_id(detail: str, *, agent_id: str) -> bool:
    lowered = (detail or "").lower()
    if "unknown agent id" not in lowered:
        return False
    target = (agent_id or "").strip().lower()
    if not target:
        return True
    return (
        f'"{target}"' in lowered
        or f"'{target}'" in lowered
        or f" {target}" in lowered
    )


async def _chat_completion_via_cli(
    *,
    agent_id: str,
    session_key: str,
    message: str,
    model_override: str | None,
    timeout_sec: float = _DEFAULT_OPENCLAW_CLI_TIMEOUT_SEC,
) -> dict[str, Any]:
    executable = _resolve_openclaw_executable()
    if not executable:
        raise ApiError(
            "OPENCLAW_CLI_MISSING",
            "openclaw CLI is not available in PATH",
            status_code=503,
        )
    argv = [
        executable,
        "agent",
        "--agent",
        agent_id,
        "--session-id",
        session_key,
        "--message",
        message,
        "--timeout",
        str(int(timeout_sec)),
        "--json",
    ]
    if model_override:
        argv.extend(["--model", model_override])
    should_retry_unknown_agent = ("-bootstrap-" in session_key) or (message == _IMPORT_OPTIMIZE_PROMPT)
    unknown_agent_retry_index = 0
    scope_repair_attempted = False
    connection_retry_index = 0
    while True:
        _raise_if_agent_create_cancelled(agent_id=agent_id)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group so a cancel / shutdown can killpg the whole tree
            # (the openclaw CLI spawns children that keep writing artifacts;
            # a bare proc.kill() left them alive → cancel-create took ~20s).
            start_new_session=True,
        )
        # Record the in-flight turn process so a reset / new send can kill it
        # (no-op unless a chat-turn progress entry is registered for this key).
        chat_progress.set_agent_proc(session_key, proc)
        cancel_event = _agent_create_cancellation_event(agent_id)
        if cancel_event is None:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            except asyncio.TimeoutError as exc:
                _subproc_registry.kill_group(proc)
                await proc.communicate()
                raise ApiError(
                    "OPENCLAW_CLI_TIMEOUT",
                    f"openclaw agent invocation exceeded {int(timeout_sec)} seconds",
                    status_code=504,
                ) from exc
        else:
            # This is a create/bootstrap turn (cancellation armed) — register so
            # a graceful shutdown sweep can killpg it if still running.
            _subproc_registry.register(proc)
            communicate_task = asyncio.create_task(proc.communicate())
            cancel_task = asyncio.create_task(cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {communicate_task, cancel_task},
                    timeout=timeout_sec,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if communicate_task in done:
                    stdout, stderr = communicate_task.result()
                elif cancel_task in done and cancel_task.result():
                    _subproc_registry.kill_group(proc)
                    try:
                        await communicate_task
                    except Exception:
                        pass
                    raise ApiError(
                        "AGENT_CREATE_CANCELLED",
                        f'agent creation cancelled while bootstrapping "{agent_id}"',
                        status_code=409,
                    )
                else:
                    _subproc_registry.kill_group(proc)
                    try:
                        await communicate_task
                    except Exception:
                        pass
                    raise ApiError(
                        "OPENCLAW_CLI_TIMEOUT",
                        f"openclaw agent invocation exceeded {int(timeout_sec)} seconds",
                        status_code=504,
                    )
            finally:
                cancel_task.cancel()
                _subproc_registry.unregister(proc)
        _raise_if_agent_create_cancelled(agent_id=agent_id)

        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            detail = err_text or out_text or f"exit code {proc.returncode}"
            if (not scope_repair_attempted) and looks_like_pending_scope_approval(detail):
                scope_repair_attempted = True
                logger.warning(
                    "openclaw_cli_scope_pending_detected",
                    agent_id=agent_id,
                    session_key=session_key,
                    detail=detail[:240],
                )
                try:
                    # Helper does blocking subprocess + time.sleep retries —
                    # keep it off the event loop.
                    repaired = await asyncio.to_thread(repair_pending_scope_upgrades)
                    logger.info(
                        "openclaw_cli_scope_repair_result",
                        agent_id=agent_id,
                        session_key=session_key,
                        repaired_request_ids=repaired,
                        repaired_count=len(repaired),
                    )
                except Exception as exc:
                    # best-effort fallback; if repair fails we still return the
                    # original OpenClaw error details on the final attempt.
                    logger.warning(
                        "openclaw_cli_scope_repair_failed",
                        agent_id=agent_id,
                        session_key=session_key,
                        error=str(exc),
                    )
                continue
            if (
                should_retry_unknown_agent
                and unknown_agent_retry_index < len(_NEW_AGENT_GATEWAY_RETRY_DELAYS_SEC)
                and _looks_like_unknown_agent_id(detail, agent_id=agent_id)
            ):
                delay = _NEW_AGENT_GATEWAY_RETRY_DELAYS_SEC[unknown_agent_retry_index]
                unknown_agent_retry_index += 1
                logger.warning(
                    "openclaw_cli_unknown_agent_retrying",
                    agent_id=agent_id,
                    session_key=session_key,
                    retry_index=unknown_agent_retry_index,
                    retry_delay_sec=delay,
                    detail=detail[:240],
                )
                await asyncio.sleep(delay)
                continue
            if (
                connection_retry_index + 1 < CHAT_CONNECTION_RETRY_ATTEMPTS
                and is_transient_connection_error(detail)
            ):
                delay = CHAT_CONNECTION_RETRY_DELAYS_SEC[
                    min(connection_retry_index, len(CHAT_CONNECTION_RETRY_DELAYS_SEC) - 1)
                ]
                connection_retry_index += 1
                logger.warning(
                    "openclaw_cli_connection_retry",
                    agent_id=agent_id,
                    session_key=session_key,
                    retry_index=connection_retry_index,
                    retry_delay_sec=delay,
                    detail=detail[:240],
                )
                await asyncio.sleep(delay)
                continue
            raise ApiError(
                "OPENCLAW_CLI_FAILED",
                f"openclaw agent invocation failed: {detail[:800]}",
                status_code=502,
            )
        try:
            payload = json.loads(out_text)
        except json.JSONDecodeError as exc:
            raise ApiError(
                "OPENCLAW_CLI_BAD_OUTPUT",
                "openclaw agent returned non-JSON output",
                status_code=502,
            ) from exc
        return _to_openai_chat_completion(
            agent_id=agent_id,
            session_key=session_key,
            payload=payload,
        )


# Strong refs to detached completion tasks so a client disconnect doesn't GC the
# task before it records the reply into history + the turn registry.
_DETACHED_CHAT_TASKS: set[asyncio.Task] = set()


def _spawn_detached_chat(coro) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _DETACHED_CHAT_TASKS.add(task)
    task.add_done_callback(_DETACHED_CHAT_TASKS.discard)
    return task


async def _chat_via_cli(
    *,
    agent_id: str,
    payload: ChatPayload,
    session_key: str,
    turn: dict[str, str],
):
    # Register a progress turn + start the trajectory follower BEFORE running the
    # agent, so step-level progress streams live (the agent CLI itself only
    # returns the final answer).
    chat_progress.start_progress(agent_id, session_key)

    async def _run_completion_once() -> dict[str, Any]:
        return await _chat_completion_via_cli(
            agent_id=agent_id,
            session_key=session_key,
            message=turn["content"],
            model_override=payload.model_override,
            timeout_sec=_chat_cli_timeout_seconds(),
        )

    if not payload.stream:
        try:
            response = await _run_completion_once()
        except Exception:
            chat_progress.finish_progress(session_key, status="error")
            raise
        assistant_text = _normalize_assistant_text(_extract_chunk_text(response))
        await chat_history.append_message(session_key, role="assistant", content=assistant_text)
        chat_progress.finish_progress(session_key, status="done", final=assistant_text)
        return response

    ct = chat_progress.get_turn(session_key)

    async def _run_completion() -> None:
        # Detached from the SSE request: a client disconnect still lands the
        # reply in history + the turn registry (the status poll recovers it).
        try:
            response = await _run_completion_once()
            text = _normalize_assistant_text(_extract_chunk_text(response))
            await chat_history.append_message(session_key, role="assistant", content=text)
            chat_progress.finish_progress(session_key, status="done", final=text)
        except ApiError as exc:
            chat_progress.finish_progress(session_key, status="error", error=exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            chat_progress.finish_progress(session_key, status="error", error=str(exc))

    _spawn_detached_chat(_run_completion())

    async def _event_stream():
        while True:
            snap = ct.snapshot() if ct is not None else {"status": "done", "final": "", "error": ""}
            if snap.get("progress"):
                yield f"data: {json.dumps({'progress': snap['progress']})}\n\n"
            if snap["status"] != "running":
                if snap["status"] == "done":
                    text = snap["final"]
                    if text and text != _NO_TEXT_REPLY_MARKER:
                        chunk = {
                            "choices": [
                                {"index": 0, "delta": {"content": text}, "finish_reason": None}
                            ]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                else:
                    yield f"data: {json.dumps({'error': snap['error'] or 'chat failed'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        # Defeat response buffering (e.g. a reverse proxy) so SSE events reach the
        # browser as they are produced instead of all-at-once at turn end.
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get(
    "/{agent_id}/chat-history",
    response_model=ChatHistoryResponse,
    response_model_exclude_none=True,
)
async def chat_history_view(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> ChatHistoryResponse:
    _ensure_chat_target_access(agent_id, user, storage)
    rows = await chat_history.list_messages(_session_key(user, agent_id))
    return ChatHistoryResponse(messages=[ChatMessage(**m) for m in rows])


@router.get("/{agent_id}/chat/status")
async def chat_status(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> dict:
    """Live turn state for reconnect (tab switch / refresh). ``idle`` when no
    turn is tracked; otherwise the running/done/error snapshot (steps + final)."""
    _ensure_chat_target_access(agent_id, user, storage)
    turn = chat_progress.get_turn(_session_key(user, agent_id))
    if turn is None:
        return {
            "status": "idle", "steps": [], "progress": None,
            "final": "", "error": "", "startedAtMono": None,
        }
    return turn.snapshot()


@router.post("/{agent_id}/chat/attachments", response_model=ChatAttachmentUploadResponse)
async def upload_chat_attachment(
    agent_id: Annotated[str, Path()],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    request: Request,
    user: UserDep,
    storage: StorageDep,
) -> ChatAttachmentUploadResponse:
    row = _ensure_chat_target_access(agent_id, user, storage)
    workspace = _agent_workspace_dir(row)
    stored = await _store_openclaw_chat_upload(
        request=request,
        workspace=workspace,
        filename=filename,
    )
    return ChatAttachmentUploadResponse(
        attachment=_to_chat_attachment(stored, route="path_injection"),
        limits={
            "maxCount": attachment_svc.MAX_ATTACHMENT_COUNT,
            "maxBytesPerFile": attachment_svc.MAX_ATTACHMENT_SIZE_BYTES,
            "maxTotalBytes": attachment_svc.MAX_ATTACHMENT_TOTAL_BYTES,
        },
    )


@router.post("/{agent_id}/chat")
async def chat_with_agent(
    agent_id: Annotated[str, Path()],
    payload: Annotated[ChatPayload, Body()],
    user: UserDep,
    storage: StorageDep,
):
    # Verify the agent exists (and is ours) BEFORE we open a bridge.
    row = _ensure_chat_target_access(agent_id, user, storage)
    # NOTE: the workspace is intentionally NOT auto-committed before a chat turn.
    # Committing chat-driven workspace changes is the agent's own responsibility.
    session_key = _session_key(user, agent_id)
    incoming = [m.model_dump(mode="python") for m in payload.messages]
    try:
        turn = _pick_latest_user_message(incoming)
    except ApiError:
        if payload.attachments:
            turn = {"role": "user", "content": ""}
        else:
            raise
    original_user_text = turn["content"]

    workspace = _agent_workspace_dir(row)
    resolved_attachments = _resolve_openclaw_payload_attachments(
        workspace=workspace,
        attachments=payload.attachments,
    )
    if resolved_attachments:
        prompt_head = original_user_text.strip() or "Please inspect the uploaded files."
        injected_message = attachment_svc.build_path_injection_message(
            user_message=prompt_head,
            attachments=resolved_attachments,
        )
        turn = {"role": "user", "content": injected_message}

    # Previous turn may have persisted the user row then failed before an
    # assistant reply — drop that orphan so it cannot linger in UI history.
    await chat_history.drop_trailing_unanswered_user(session_key)
    await chat_history.append_message(
        session_key,
        role=turn["role"],
        content=original_user_text,
        attachments=_attachments_for_history(
            resolved_attachments,
            route="path_injection",
        ),
    )
    return await _chat_via_cli(
        agent_id=agent_id,
        payload=payload,
        session_key=session_key,
        turn=turn,
    )


@router.post("/{agent_id}/chat/stop", status_code=204)
async def stop_chat_turn(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    """Stop the in-flight turn (agent + follower) WITHOUT clearing history — the
    user's "stop generating" action. The question stays for regenerate/retry."""
    _ensure_chat_target_access(agent_id, user, storage)
    chat_progress.kill_turn(_session_key(user, agent_id))


@router.post("/{agent_id}/reset", status_code=204)
async def reset_chat_session(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    """Reset the per-(user, agent) session by sending exactly ``/reset``."""
    _ensure_chat_target_access(agent_id, user, storage)
    session_key = _session_key(user, agent_id)
    # Stop any in-flight turn (agent + trajectory follower) before rotating the
    # session, so a reset can't leave a runaway turn writing to the old session.
    chat_progress.kill_turn(session_key)
    should_fallback_rotate = False
    try:
        await _chat_completion_via_cli(
            agent_id=agent_id,
            session_key=session_key,
            message="/reset",
            model_override=None,
            timeout_sec=_chat_cli_timeout_seconds(),
        )
    except ApiError as exc:
        lowered = exc.message.lower()
        if exc.code == "OPENCLAW_CLI_FAILED" and "missing scope: operator.admin" in lowered:
            should_fallback_rotate = True
            logger.warning(
                "chat_reset_scope_fallback_rotate_session",
                agent_id=agent_id,
                user=user,
                session_key=session_key,
            )
        else:
            raise
    await chat_history.clear_messages(session_key)
    if should_fallback_rotate:
        rotated = _bump_session_revision(user, agent_id)
        await chat_history.clear_messages(rotated)
