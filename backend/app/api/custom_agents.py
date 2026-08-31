"""Public custom-agent registry API (我的团队 → 自定义Agent).

A slim sibling of the Hermes agent API: CRUD over the :class:`CustomAgent`
registry plus a deliberately simple chat dialog (one-shot headless subprocess
per turn, SSE with the same event shape as Hermes so the frontend loop is
shared).

Endpoints (prefix ``/api``):

* ``GET    /custom-agents``                    — list (own) + availability probe
* ``POST   /custom-agents``                    — register
* ``GET    /custom-agents/{id}``               — get
* ``PATCH  /custom-agents/{id}``               — update
* ``DELETE /custom-agents/{id}``               — delete (returns referencing Flows)
* ``POST   /custom-agents/{id}/chat``          — direct chat (SSE)
* ``GET    /custom-agents/{id}/chat/status``   — live turn state (reconnect)
* ``POST   /custom-agents/{id}/chat/stop``     — stop the in-flight turn
* ``GET    /custom-agents/{id}/chat-history``  — persisted UI transcript
* ``POST   /custom-agents/{id}/reset``         — new session (keep history + divider)
"""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api.errors import ApiError
from app.logging_setup import get_logger
from app.models import CustomAgent, iso_utc
from app.scheduler.naming import custom_agent_user_chat_session_id
from app.services import custom_agent_chat as chat_svc
from app.services import custom_agents as svc
from app.services import openclaw_chat_history as chat_history
from app.storage import StorageBackend, get_storage

router = APIRouter(prefix="/custom-agents", tags=["custom-agents"])
logger = get_logger("api.custom_agents")

# Strong refs to detached completion tasks (see hermes_agents._spawn_detached).
_DETACHED_TASKS: set[asyncio.Task] = set()


def _spawn_detached(coro) -> asyncio.Task:
    task = asyncio.ensure_future(coro)
    _DETACHED_TASKS.add(task)
    task.add_done_callback(_DETACHED_TASKS.discard)
    return task


def _storage_dep() -> StorageBackend:
    return get_storage()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]


# ──────────────────────────────────────────────────────────────────────
# Schemas
# ──────────────────────────────────────────────────────────────────────


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class CustomAgentSummary(_CamelModel):
    id: str
    name: str
    description: str
    # Command fields are round-tripped as plain command-line strings (shlex
    # quoting) — that's what the user typed and what the edit form re-shows.
    spawn_command: str
    resume_command: str = ""
    headless_command: str = ""
    headless_resume_command: str = ""
    ready_pattern: str = ""
    chat_workdir: str = ""
    # Parsed argv forms of spawn/resume — the FlowEditor writes these into the
    # Flow spec as the ``command``/``resumeCommand`` save-time snapshot (dual
    # write: ref + snapshot), so the frontend never re-implements shlex.
    spawn_argv: list[str] = []
    resume_argv: list[str] = []
    # Whether a headless command is configured (i.e. the chat dialog works).
    # NOT a binary probe — a registered agent is always offered; whether its
    # command runs is the user's own responsibility.
    chat_available: bool = False
    created_by_user: str
    created_at: str


class CustomAgentListResponse(_CamelModel):
    items: list[CustomAgentSummary]


class CreatePayload(_CamelModel):
    name: str
    description: str = ""
    spawn_command: str
    resume_command: str = ""
    headless_command: str = ""
    headless_resume_command: str = ""
    ready_pattern: str = ""
    chat_workdir: str = ""


class UpdatePayload(_CamelModel):
    name: str | None = None
    description: str | None = None
    spawn_command: str | None = None
    resume_command: str | None = None
    headless_command: str | None = None
    headless_resume_command: str | None = None
    ready_pattern: str | None = None
    chat_workdir: str | None = None


class DeleteResponse(_CamelModel):
    deleted: bool
    # Flows still referencing this agent — the runs of these Flows will fail
    # validation until the user re-points them (no cascade by design).
    referenced_flows: list[dict[str, str]] = []


class ChatPayload(_CamelModel):
    message: str
    workdir: str = ""


class ChatMessage(_CamelModel):
    role: str
    content: str
    ts: int | None = None
    id: int | None = None
    kind: str | None = None


class ChatHistoryResponse(_CamelModel):
    messages: list[ChatMessage]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _map_service_error(exc: svc.CustomAgentError) -> ApiError:
    return ApiError(exc.code, str(exc), status_code=exc.status_code)


def _joined(argv: list[str]) -> str:
    return shlex.join(argv) if argv else ""


def _to_summary(a: CustomAgent) -> CustomAgentSummary:
    return CustomAgentSummary(
        id=a.id,
        name=a.name,
        description=a.description,
        spawn_command=_joined(a.spawn_command),
        resume_command=_joined(a.resume_command),
        headless_command=_joined(a.headless_command),
        headless_resume_command=_joined(a.headless_resume_command),
        ready_pattern=a.ready_pattern,
        chat_workdir=a.chat_workdir,
        spawn_argv=list(a.spawn_command),
        resume_argv=list(a.resume_command),
        chat_available=bool(a.headless_command),
        created_by_user=a.created_by_user,
        created_at=iso_utc(a.created_at),
    )


def _get_owned(agent_id: str, user: str, storage: StorageBackend) -> CustomAgent:
    row = storage.custom_agent_get(agent_id)
    if row is None:
        raise ApiError(
            "CUSTOM_AGENT_NOT_FOUND",
            f"custom agent {agent_id!r} not found",
            status_code=404,
        )
    if row.created_by_user and row.created_by_user != user:
        raise ApiError("FORBIDDEN", "not your agent", status_code=403)
    return row


def _session_key(user: str, agent_id: str) -> str:
    return custom_agent_user_chat_session_id(user, agent_id)


def _validate_chat_workdir(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    path = FsPath(value).expanduser()
    if not path.is_dir():
        raise ApiError(
            "INVALID_PAYLOAD",
            f"chat workdir {value!r} is not an existing directory",
            status_code=400,
        )
    return value


def _history_resume_flag(rows: list[chat_history.ChatHistoryMessage]) -> bool:
    """Resume (use the headless *continue* template) when the current session —
    i.e. after the last divider — already contains an assistant reply."""
    for row in reversed(rows):
        if row.get("kind") == chat_history.SESSION_DIVIDER_KIND:
            return False
        if row.get("role") == "assistant":
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────


@router.get("", response_model=CustomAgentListResponse)
async def list_agents(user: UserDep, storage: StorageDep) -> CustomAgentListResponse:
    rows = storage.custom_agent_list(owner_user=user)
    return CustomAgentListResponse(items=[_to_summary(r) for r in rows])


@router.post("", response_model=CustomAgentSummary, status_code=201)
async def create_agent(
    payload: Annotated[CreatePayload, Body()], user: UserDep, storage: StorageDep,
) -> CustomAgentSummary:
    try:
        name = svc.validate_name(payload.name)
        description = svc.validate_description(payload.description)
        spawn = svc.parse_command(
            payload.spawn_command, field="spawn_command", required=True
        )
        resume = svc.parse_command(payload.resume_command, field="resume_command")
        headless = svc.parse_command(
            payload.headless_command, field="headless_command"
        )
        headless_resume = svc.parse_command(
            payload.headless_resume_command, field="headless_resume_command"
        )
        ready_pattern = svc.validate_ready_pattern(payload.ready_pattern)
    except svc.CustomAgentError as exc:
        raise _map_service_error(exc) from exc
    if headless_resume and not headless:
        raise ApiError(
            "INVALID_PAYLOAD",
            "headless_resume_command requires headless_command",
            status_code=400,
        )
    chat_workdir = _validate_chat_workdir(payload.chat_workdir)

    agent_id = svc.slugify_name(name)
    if storage.custom_agent_get(agent_id) is not None:
        raise ApiError(
            "CUSTOM_AGENT_DUPLICATE",
            f"a custom agent with id {agent_id!r} already exists — pick another name",
            status_code=409,
        )
    lowered = name.lower()
    if any(r.name.strip().lower() == lowered for r in storage.custom_agent_list()):
        raise ApiError(
            "CUSTOM_AGENT_DUPLICATE",
            f"a custom agent named {name!r} already exists",
            status_code=409,
        )

    row = CustomAgent(
        id=agent_id,
        name=name,
        description=description,
        spawn_command=spawn,
        resume_command=resume,
        headless_command=headless,
        headless_resume_command=headless_resume,
        ready_pattern=ready_pattern,
        chat_workdir=chat_workdir,
        created_by_user=user,
    )
    saved = storage.custom_agent_create(row)
    logger.info("custom_agent_created", agent_id=saved.id, user=user)
    return _to_summary(saved)


@router.get("/{agent_id}", response_model=CustomAgentSummary)
async def get_agent(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> CustomAgentSummary:
    return _to_summary(_get_owned(agent_id, user, storage))


@router.patch("/{agent_id}", response_model=CustomAgentSummary)
async def update_agent(
    agent_id: Annotated[str, Path()],
    payload: Annotated[UpdatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> CustomAgentSummary:
    row = _get_owned(agent_id, user, storage)
    try:
        if payload.name is not None:
            row.name = svc.validate_name(payload.name)
        if payload.description is not None:
            row.description = svc.validate_description(payload.description)
        if payload.spawn_command is not None:
            row.spawn_command = svc.parse_command(
                payload.spawn_command, field="spawn_command", required=True
            )
        if payload.resume_command is not None:
            row.resume_command = svc.parse_command(
                payload.resume_command, field="resume_command"
            )
        if payload.headless_command is not None:
            row.headless_command = svc.parse_command(
                payload.headless_command, field="headless_command"
            )
        if payload.headless_resume_command is not None:
            row.headless_resume_command = svc.parse_command(
                payload.headless_resume_command, field="headless_resume_command"
            )
        if payload.ready_pattern is not None:
            row.ready_pattern = svc.validate_ready_pattern(payload.ready_pattern)
    except svc.CustomAgentError as exc:
        raise _map_service_error(exc) from exc
    if payload.chat_workdir is not None:
        row.chat_workdir = _validate_chat_workdir(payload.chat_workdir)
    if row.headless_resume_command and not row.headless_command:
        raise ApiError(
            "INVALID_PAYLOAD",
            "headless_resume_command requires headless_command",
            status_code=400,
        )
    saved = storage.custom_agent_update(row)
    logger.info("custom_agent_updated", agent_id=saved.id, user=user)
    return _to_summary(saved)


@router.delete("/{agent_id}", response_model=DeleteResponse)
async def delete_agent(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> DeleteResponse:
    _get_owned(agent_id, user, storage)
    referenced = svc.flows_referencing(agent_id, storage=storage)
    # Kill any in-flight chat + drop the transcript (mirror Hermes delete).
    session_key = _session_key(user, agent_id)
    chat_svc.kill_chat(session_key)
    try:
        await chat_history.clear_messages(session_key)
    except Exception:
        logger.warning("custom_agent_chat_history_cleanup_failed", agent_id=agent_id)
    deleted = storage.custom_agent_delete(agent_id)
    logger.info(
        "custom_agent_deleted",
        agent_id=agent_id,
        user=user,
        referenced_flow_count=len(referenced),
    )
    return DeleteResponse(deleted=deleted, referenced_flows=referenced)


# ──────────────────────────────────────────────────────────────────────
# Direct chat (SSE — same event shape as the Hermes chat endpoint)
# ──────────────────────────────────────────────────────────────────────


@router.get(
    "/{agent_id}/chat-history",
    response_model=ChatHistoryResponse,
    response_model_exclude_none=True,
)
async def chat_history_view(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> ChatHistoryResponse:
    _get_owned(agent_id, user, storage)
    rows = await chat_history.list_messages(_session_key(user, agent_id))
    return ChatHistoryResponse(messages=[ChatMessage(**m) for m in rows])


async def _finalize_chat_history(job: chat_svc.ChatJob, conversation_key: str) -> None:
    """Persist the final answer when the job completes, even if the SSE client
    disconnected (same pattern as Hermes)."""
    while job.snapshot()["status"] == "running":
        await asyncio.sleep(0.5)
    snap = job.snapshot()
    if snap["status"] == "done" and snap["final"]:
        await chat_history.append_message(
            conversation_key, role="assistant", content=snap["final"],
        )
        logger.info(
            "custom_agent_chat_history_appended",
            agent_id=job.agent_id,
            final_len=len(snap["final"]),
        )


@router.post("/{agent_id}/chat")
async def chat_with_agent(
    agent_id: Annotated[str, Path()],
    payload: Annotated[ChatPayload, Body()],
    user: UserDep,
    storage: StorageDep,
):
    row = _get_owned(agent_id, user, storage)
    message = (payload.message or "").strip()
    if not message:
        raise ApiError("INVALID_PAYLOAD", "message is required", status_code=400)

    conversation_key = _session_key(user, agent_id)
    # Drop a trailing user row that never got a reply (failed previous turn).
    await chat_history.drop_trailing_unanswered_user(conversation_key)
    existing = await chat_history.list_messages(conversation_key)
    resume = _history_resume_flag(existing)
    await chat_history.append_message(conversation_key, role="user", content=message)

    try:
        workdir = chat_svc.resolve_chat_workdir(row, payload.workdir)
        job = chat_svc.start_chat(
            row,
            message=message,
            workdir=workdir,
            resume=resume,
            session_key=conversation_key,
        )
    except svc.CustomAgentError as exc:
        raise _map_service_error(exc) from exc

    _spawn_detached(_finalize_chat_history(job, conversation_key))

    async def _stream():
        while True:
            snap = job.snapshot()
            yield f"data: {json.dumps({'progress': snap['progress']})}\n\n"
            if snap["status"] != "running":
                if snap["status"] == "done":
                    if snap["final"]:
                        yield f"data: {json.dumps({'delta': snap['final']})}\n\n"
                else:
                    yield f"data: {json.dumps({'error': snap['error'] or 'chat failed'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/{agent_id}/chat/status")
async def chat_status(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> dict:
    _get_owned(agent_id, user, storage)
    job = chat_svc.get_job(_session_key(user, agent_id))
    if job is None:
        return {
            "status": "idle", "steps": [], "progress": None,
            "final": "", "error": "", "startedAtMono": None,
        }
    return job.snapshot()


@router.post("/{agent_id}/chat/stop", status_code=204)
async def stop_chat(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    chat_svc.kill_chat(_session_key(user, agent_id))


@router.post("/{agent_id}/reset", status_code=204)
async def reset_chat(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    key = _session_key(user, agent_id)
    chat_svc.kill_chat(key)
    await chat_history.append_divider(key)


__all__ = ["router"]
