"""Public Hermes agent management API.

Mirrors the OpenClaw agent API surface (minus Import&Optimize / Agent Store /
Restore — Hermes deletion is permanent via the ``hermes`` CLI). A managed
Hermes agent maps 1:1 to a Hermes **profile** (``hermes -p <id>``); every
sub-task / chat turn binds the executor via ``-p <id>``.

Endpoints (prefix ``/api``):

* ``POST   /hermes/agents``                       — create (sync, profile create + bootstrap)
* ``GET    /hermes/agents``                       — list (own)
* ``GET    /hermes/agents/{id}``                  — get
* ``PATCH  /hermes/agents/{id}``                  — update metadata
* ``DELETE /hermes/agents/{id}``                  — delete (permanent)
* ``GET    /hermes/agents/runtime/status``        — is the Hermes CLI usable
* ``POST   /hermes/agents/dashboard/open``        — ensure dashboard running; return URL
* ``GET    /hermes/agents/claimable``             — existing unmanaged profiles
* ``POST   /hermes/agents/claim``                 — register an existing profile
* ``*      /hermes/agents/{id}/settings/...``     — soul / model / secrets / skills / cron
* ``POST   /hermes/agents/{id}/chat``             — direct chat (SSE; message + workdir)
* ``GET    /hermes/agents/{id}/chat-history``     — UI chat history cache
* ``POST   /hermes/agents/{id}/reset``            — clear cached history

Teams are the shared :class:`OpenclawTeam` grouping (see ``/api/openclaw/agents/teams``).
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api.errors import ApiError
from app.logging_setup import get_logger
from app.models import HermesAgent, iso_utc
from app.operations import get_op_registry
from app.scheduler.naming import hermes_user_chat_session_id
from app.services import hermes_agents as svc
from app.services import hermes_dashboard as dash_svc
from app.services import openclaw_agents as oc_svc
from app.services import openclaw_chat_history as chat_history
from app.storage import StorageBackend, get_storage

router = APIRouter(prefix="/hermes/agents", tags=["hermes"])
logger = get_logger("api.hermes_agents")

_CHAT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hermes-chat")

# Strong refs to detached completion tasks so an orphaned (post-disconnect) task
# isn't garbage-collected before it records its result. See create_agent / chat.
_DETACHED_TASKS: set[asyncio.Task] = set()


def _spawn_detached(coro) -> asyncio.Task:
    """Run *coro* as a task whose lifecycle is independent of the request.

    Callers ``await asyncio.shield(task)`` so the happy path still propagates the
    result/exception, while a client disconnect (which cancels the request
    coroutine) leaves the task running to completion. This matters because the
    executor thread keeps running regardless, but the completion side effects (op
    registry transition, chat-history append) live on the event loop and would
    otherwise be skipped when the awaiting coroutine is cancelled.
    """
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


class HermesAgentSummary(_CamelModel):
    id: str
    name: str
    description: str
    team_id: str
    team_name: str
    profile_root: str
    created_by_user: str
    created_at: str


class HermesAgentDetail(HermesAgentSummary):
    nl_prompt: str = ""


class HermesAgentListResponse(_CamelModel):
    items: list[HermesAgentSummary]


class HermesRuntimeStatusResponse(_CamelModel):
    running: bool
    reason: str


class HermesDashboardOpenResponse(_CamelModel):
    url: str


class HermesClaimableAgent(_CamelModel):
    id: str
    description: str = ""


class HermesClaimableListResponse(_CamelModel):
    items: list[HermesClaimableAgent]
    total: int


class CreatePayload(_CamelModel):
    # ``id`` IS the Hermes profile id and is the primary, required field in the
    # UI ("Agent name (Profile id)"). ``name`` is optional and defaults to the
    # id; legacy callers may still send only ``name`` (id derived from it).
    id: str = ""
    name: str = ""
    responsibility: str = ""  # → description / nl_prompt
    team_id: str = ""


class UpdatePayload(_CamelModel):
    name: str | None = None
    description: str | None = None
    team_id: str | None = None


class ClaimPayload(_CamelModel):
    id: str
    name: str = ""
    team_id: str = ""


class SoulView(_CamelModel):
    content: str


class ModelView(_CamelModel):
    default: str = ""
    provider: str = ""
    base_url: str = ""


class SecretView(_CamelModel):
    key: str
    preview: str = ""
    is_set: bool = False


class SetSecretPayload(_CamelModel):
    key: str
    value: str


class SkillView(_CamelModel):
    name: str
    description: str = ""
    path: str = ""
    content: str | None = None


class SkillCreatePayload(_CamelModel):
    name: str
    description: str = ""
    content: str = ""


class CronJobView(_CamelModel):
    id: str
    name: str = ""
    schedule: str = ""
    enabled: bool = True
    detail: str = ""
    raw: str = ""


class CronListResponse(_CamelModel):
    available: bool
    items: list[CronJobView]


class CreateCronPayload(_CamelModel):
    schedule: str
    prompt: str = ""
    name: str = ""
    workdir: str = ""


class ChatPayload(_CamelModel):
    message: str
    workdir: str


class ChatMessage(_CamelModel):
    role: str
    content: str


class ChatHistoryResponse(_CamelModel):
    messages: list[ChatMessage]


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _team_name_map(*, storage: StorageBackend, user: str | None) -> dict[str, str]:
    teams = oc_svc.list_teams(user=user, storage=storage)
    return {t.id: t.name for t in teams}


def _to_summary(a: HermesAgent, *, team_name: str) -> HermesAgentSummary:
    return HermesAgentSummary(
        id=a.id, name=a.name, description=a.description,
        team_id=a.team_id, team_name=team_name, profile_root=a.profile_root,
        created_by_user=a.created_by_user, created_at=iso_utc(a.created_at),
    )


def _to_detail(a: HermesAgent, *, team_name: str) -> HermesAgentDetail:
    return HermesAgentDetail(
        id=a.id, name=a.name, description=a.description,
        team_id=a.team_id, team_name=team_name, profile_root=a.profile_root,
        created_by_user=a.created_by_user, created_at=iso_utc(a.created_at),
        nl_prompt=a.nl_prompt,
    )


def _map_service_error(exc: svc.HermesAgentError) -> ApiError:
    mapping: dict[type, tuple[str, int]] = {
        svc.AgentIdInvalid: ("INVALID_PAYLOAD", 400),
        svc.AgentAlreadyExists: ("AGENT_ALREADY_EXISTS", 409),
        svc.AgentNotFound: ("AGENT_NOT_FOUND", 404),
        svc.AgentInUse: ("AGENT_IN_USE", 409),
        svc.HermesUnavailable: ("HERMES_UNAVAILABLE", 503),
        svc.ProfileOpFailed: ("HERMES_CLI_FAILED", 502),
        svc.AgentCreateCancelled: ("AGENT_CREATE_CANCELLED", 409),
    }
    code, status = mapping.get(type(exc), ("HERMES_ERROR", 500))
    return ApiError(code, str(exc), status_code=status, details=exc.details)


def _get_owned(agent_id: str, user: str, storage: StorageBackend) -> HermesAgent:
    try:
        row = svc.get_agent(agent_id, storage=storage)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    if row.created_by_user and row.created_by_user != user:
        raise ApiError("FORBIDDEN", "not your agent", status_code=403)
    return row


def _session_key(user: str, agent_id: str) -> str:
    return hermes_user_chat_session_id(user, agent_id)


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────


@router.get("", response_model=HermesAgentListResponse)
def list_agents(user: UserDep, storage: StorageDep) -> HermesAgentListResponse:
    items = svc.list_agents(user=user, storage=storage)
    team_names = _team_name_map(storage=storage, user=user)
    return HermesAgentListResponse(
        items=[_to_summary(a, team_name=team_names.get(a.team_id, "")) for a in items],
    )


@router.get("/runtime/status", response_model=HermesRuntimeStatusResponse)
def runtime_status(
    user: UserDep,
    mode: Annotated[str, Query()] = svc.PROBE_FULL,
) -> HermesRuntimeStatusResponse:
    del user
    # ``mode=fast`` is presence-only (instant) so the WebUI can render right
    # away; the default ``full`` actually executes the CLI to confirm it runs.
    level = svc.PROBE_FAST if mode == svc.PROBE_FAST else svc.PROBE_FULL
    running, reason = svc.probe_runtime_running(level=level)
    return HermesRuntimeStatusResponse(running=running, reason=reason)


@router.post("/dashboard/open", response_model=HermesDashboardOpenResponse)
def open_dashboard(user: UserDep) -> HermesDashboardOpenResponse:
    del user
    try:
        url = dash_svc.ensure_hermes_dashboard_url()
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return HermesDashboardOpenResponse(url=url)


@router.get("/claimable", response_model=HermesClaimableListResponse)
def list_claimable(user: UserDep, storage: StorageDep) -> HermesClaimableListResponse:
    del user
    try:
        items = svc.list_claimable_profiles(storage=storage)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    views = [HermesClaimableAgent(id=i["id"], description=i.get("description", "")) for i in items]
    return HermesClaimableListResponse(items=views, total=len(views))


@router.post("/claim", response_model=HermesAgentDetail, status_code=201)
def claim_agent(
    payload: Annotated[ClaimPayload, Body()], user: UserDep, storage: StorageDep,
) -> HermesAgentDetail:
    try:
        row = svc.claim_profile(
            profile_name=payload.id, name=payload.name, team_id=payload.team_id,
            user=user, storage=storage,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    team_names = _team_name_map(storage=storage, user=user)
    return _to_detail(row, team_name=team_names.get(row.team_id, ""))


@router.post("", response_model=HermesAgentDetail, status_code=201)
async def create_agent(
    payload: Annotated[CreatePayload, Body()], user: UserDep, storage: StorageDep,
) -> HermesAgentDetail:
    display_name = (payload.name or "").strip()
    if not display_name:
        raise ApiError("INVALID_PAYLOAD", "name is required", status_code=400)
    agent_id = (payload.id or payload.name or "").strip().lower()
    # Best-effort derive an id from the name if not provided: keep ASCII [a-z0-9]
    # only. `str.isalnum()` is True for CJK/Unicode letters too, so it must be
    # paired with `isascii()` or a Chinese name would yield an invalid id that
    # then fails `_validate_agent_id`.
    if not payload.id:
        agent_id = "".join(ch for ch in agent_id if ch.isascii() and ch.isalnum())
    cmd = svc.CommitInput(
        id=agent_id,
        name=display_name,
        description=payload.responsibility,
        nl_prompt=payload.responsibility,
        team_id=payload.team_id,
    )
    op_id = f"hermes_create:{agent_id}"
    reg = get_op_registry()
    reg.start(op_id=op_id, user=user, kind="hermes_create")
    loop = asyncio.get_running_loop()

    async def _commit():
        # Records the op's terminal state on the event loop. Detached from the
        # request (shield below) so a client disconnect mid-create still marks
        # the op succeeded/failed — otherwise the recovery UI is stuck "running".
        try:
            row = await loop.run_in_executor(
                _CHAT_EXECUTOR,
                lambda: svc.commit_agent(cmd, user=user, storage=storage),
            )
        except svc.HermesAgentError as exc:
            # AgentCreateCancelled is a HermesAgentError subclass, so cancel flows here.
            detail = (
                "cancelled"
                if isinstance(exc, svc.AgentCreateCancelled)
                else f"{type(exc).__name__}: {exc}"
            )
            reg.fail(op_id, detail=detail)
            raise
        reg.succeed(op_id, result={"agentId": row.id})
        return row

    try:
        row = await asyncio.shield(_spawn_detached(_commit()))
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    team_names = _team_name_map(storage=storage, user=user)
    return _to_detail(row, team_name=team_names.get(row.team_id, ""))


@router.post("/{agent_id}/cancel-create", status_code=202)
async def cancel_create(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> dict[str, bool]:
    """Cancel an in-flight create (kills the bootstrap + rolls back artifacts).

    No ownership check: the row may not exist yet mid-create, and the id is
    user-chosen, so anyone who knows it could only ever *undo* a creation."""
    del user
    loop = asyncio.get_running_loop()
    try:
        killed = await loop.run_in_executor(
            _CHAT_EXECUTOR,
            lambda: svc.cancel_create_agent(agent_id, storage=storage),
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return {"killed": killed}


@router.get("/{agent_id}", response_model=HermesAgentDetail)
def get_agent(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> HermesAgentDetail:
    row = _get_owned(agent_id, user, storage)
    team_names = _team_name_map(storage=storage, user=user)
    return _to_detail(row, team_name=team_names.get(row.team_id, ""))


@router.patch("/{agent_id}", response_model=HermesAgentDetail)
def patch_agent(
    agent_id: Annotated[str, Path()],
    payload: Annotated[UpdatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> HermesAgentDetail:
    _get_owned(agent_id, user, storage)
    try:
        row = svc.update_agent(
            agent_id,
            svc.UpdateInput(name=payload.name, description=payload.description, team_id=payload.team_id),
            storage=storage,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    team_names = _team_name_map(storage=storage, user=user)
    return _to_detail(row, team_name=team_names.get(row.team_id, ""))


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            _CHAT_EXECUTOR, lambda: svc.delete_agent(agent_id, storage=storage),
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


# ──────────────────────────────────────────────────────────────────────
# Settings — SOUL.md
# ──────────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/settings/soul", response_model=SoulView)
def get_soul(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> SoulView:
    _get_owned(agent_id, user, storage)
    return SoulView(content=svc.read_soul(agent_id))


@router.put("/{agent_id}/settings/soul", response_model=SoulView)
def put_soul(
    agent_id: Annotated[str, Path()],
    payload: Annotated[SoulView, Body()],
    user: UserDep,
    storage: StorageDep,
) -> SoulView:
    _get_owned(agent_id, user, storage)
    try:
        return SoulView(content=svc.write_soul(agent_id, payload.content))
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


# ──────────────────────────────────────────────────────────────────────
# Settings — model & secrets
# ──────────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/settings/model", response_model=ModelView)
def get_model(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> ModelView:
    _get_owned(agent_id, user, storage)
    m = svc.read_model(agent_id)
    return ModelView(default=m["default"], provider=m["provider"], base_url=m["base_url"])


@router.put("/{agent_id}/settings/model", response_model=ModelView)
def put_model(
    agent_id: Annotated[str, Path()],
    payload: Annotated[ModelView, Body()],
    user: UserDep,
    storage: StorageDep,
) -> ModelView:
    _get_owned(agent_id, user, storage)
    try:
        m = svc.write_model(
            agent_id, default=payload.default, provider=payload.provider, base_url=payload.base_url,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return ModelView(default=m["default"], provider=m["provider"], base_url=m["base_url"])


@router.get("/{agent_id}/settings/secrets", response_model=list[SecretView])
def get_secrets(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> list[SecretView]:
    _get_owned(agent_id, user, storage)
    return [SecretView(**s) for s in svc.list_secrets(agent_id)]


@router.put("/{agent_id}/settings/secrets", status_code=204)
def put_secret(
    agent_id: Annotated[str, Path()],
    payload: Annotated[SetSecretPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    try:
        svc.set_secret(agent_id, payload.key, payload.value)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


@router.delete("/{agent_id}/settings/secrets/{key}", status_code=204)
def del_secret(
    agent_id: Annotated[str, Path()],
    key: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    svc.delete_secret(agent_id, key)


# ──────────────────────────────────────────────────────────────────────
# Settings — skills
# ──────────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/settings/skills", response_model=list[SkillView])
def get_skills(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> list[SkillView]:
    _get_owned(agent_id, user, storage)
    return [SkillView(**s) for s in svc.list_skills(agent_id)]


@router.get("/{agent_id}/settings/skills/{name}", response_model=SkillView)
def get_skill(
    agent_id: Annotated[str, Path()],
    name: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> SkillView:
    _get_owned(agent_id, user, storage)
    try:
        content = svc.read_skill(agent_id, name)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return SkillView(name=name, content=content)


@router.post("/{agent_id}/settings/skills", response_model=SkillView, status_code=201)
def create_skill(
    agent_id: Annotated[str, Path()],
    payload: Annotated[SkillCreatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> SkillView:
    _get_owned(agent_id, user, storage)
    try:
        out = svc.write_skill(
            agent_id, name=payload.name, description=payload.description, content=payload.content,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return SkillView(name=out["name"], description=out.get("description", ""), path=out.get("path", ""))


@router.delete("/{agent_id}/settings/skills/{name}", status_code=204)
def del_skill(
    agent_id: Annotated[str, Path()],
    name: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    try:
        svc.delete_skill(agent_id, name)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


# ──────────────────────────────────────────────────────────────────────
# Settings — cron
# ──────────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/settings/cron", response_model=CronListResponse)
def get_cron(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> CronListResponse:
    _get_owned(agent_id, user, storage)
    if not svc.cron_available():
        return CronListResponse(available=False, items=[])
    return CronListResponse(
        available=True,
        items=[CronJobView(**j) for j in svc.list_cron(agent_id)],
    )


@router.post("/{agent_id}/settings/cron", status_code=201)
def create_cron(
    agent_id: Annotated[str, Path()],
    payload: Annotated[CreateCronPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    try:
        svc.create_cron(
            agent_id, schedule=payload.schedule, prompt=payload.prompt,
            name=payload.name, workdir=payload.workdir,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


@router.post("/{agent_id}/settings/cron/{job_id}/{action}", status_code=204)
def cron_action(
    agent_id: Annotated[str, Path()],
    job_id: Annotated[str, Path()],
    action: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    if action not in {"pause", "resume", "remove"}:
        raise ApiError("INVALID_PAYLOAD", f"unsupported cron action: {action}", status_code=400)
    try:
        svc.cron_action(agent_id, job_id, action)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


# ──────────────────────────────────────────────────────────────────────
# Direct chat (SSE)
# ──────────────────────────────────────────────────────────────────────


@router.get("/{agent_id}/chat-history", response_model=ChatHistoryResponse)
async def chat_history_view(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> ChatHistoryResponse:
    _get_owned(agent_id, user, storage)
    rows = await chat_history.list_messages(_session_key(user, agent_id))
    return ChatHistoryResponse(messages=[ChatMessage(**m) for m in rows])


@router.post("/{agent_id}/chat")
async def chat_with_agent(
    agent_id: Annotated[str, Path()],
    payload: Annotated[ChatPayload, Body()],
    user: UserDep,
    storage: StorageDep,
):
    _get_owned(agent_id, user, storage)
    message = (payload.message or "").strip()
    workdir = (payload.workdir or "").strip()
    if not message:
        raise ApiError("INVALID_PAYLOAD", "message is required", status_code=400)
    if not workdir:
        raise ApiError("INVALID_PAYLOAD", "workdir is required", status_code=400)

    session_key = _session_key(user, agent_id)
    # Resume the existing session whenever this conversation already has turns
    # (history is persisted, so this survives a backend restart). ``/reset``
    # clears history → next turn starts a fresh session.
    resume = len(await chat_history.list_messages(session_key)) > 0
    await chat_history.append_message(session_key, role="user", content=message)

    async def _stream():
        loop = asyncio.get_running_loop()

        async def _answer() -> str:
            # Detached from the SSE request (shield below): if the client
            # disconnects mid-generation, this task still finishes and persists
            # the assistant turn to chat_history, so the next page load recovers
            # the real reply instead of a stale empty bubble.
            text = await loop.run_in_executor(
                _CHAT_EXECUTOR,
                lambda: svc.chat_once(agent_id, message=message, workdir=workdir, resume=resume),
            )
            await chat_history.append_message(session_key, role="assistant", content=text)
            return text

        try:
            answer = await asyncio.shield(_spawn_detached(_answer()))
        except svc.HermesAgentError as exc:
            err = {"error": str(exc)}
            yield f"data: {json.dumps(err)}\n\n"
            yield "data: [DONE]\n\n"
            return
        # Emit the answer as a single delta (hermes -z returns the final text).
        yield f"data: {json.dumps({'delta': answer})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{agent_id}/reset", status_code=204)
async def reset_chat(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    await chat_history.clear_messages(_session_key(user, agent_id))


__all__ = ["router"]
