"""Public management API for env-home managed agents (Claude Code / Codex / Cursor).

One generic router keyed by ``kind`` (the frontend mounts a page per kind).
Identity/skills/MCP live in a relocatable config home injected at spawn via a
ClawTeam runtime profile; the working directory stays per-task. Teams reuse the
shared OpenClaw team store.
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
from app.models import ManagedAgent, iso_utc
from app.services import managed_agents as svc
from app.services import openclaw_agents as oc_svc
from app.services import openclaw_chat_history as chat_history
from app.storage import StorageBackend, get_storage

router = APIRouter(prefix="/managed/agents", tags=["managed"])
logger = get_logger("api.managed_agents")
_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="managed-cli")


def _storage_dep() -> StorageBackend:
    return get_storage()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class ManagedAgentSummary(_CamelModel):
    id: str
    kind: str
    name: str
    description: str
    team_id: str
    team_name: str
    config_home: str
    created_by_user: str
    created_at: str


class ManagedAgentDetail(ManagedAgentSummary):
    nl_prompt: str = ""
    clawteam_profile: str = ""


class ManagedListResponse(_CamelModel):
    items: list[ManagedAgentSummary]


class RuntimeStatusResponse(_CamelModel):
    running: bool
    reason: str


class CreatePayload(_CamelModel):
    kind: str
    id: str = ""
    name: str = ""
    responsibility: str = ""
    team_id: str = ""


class UpdatePayload(_CamelModel):
    name: str | None = None
    description: str | None = None
    team_id: str | None = None


class RoleDocView(_CamelModel):
    content: str


class McpServerView(_CamelModel):
    name: str
    detail: str = ""


class AddMcpPayload(_CamelModel):
    name: str
    command: list[str]


class SkillView(_CamelModel):
    name: str
    description: str = ""
    path: str = ""
    content: str | None = None


class SkillCreatePayload(_CamelModel):
    name: str
    description: str = ""
    content: str = ""


class ChatPayload(_CamelModel):
    message: str
    workdir: str


class ChatMessage(_CamelModel):
    role: str
    content: str


class ChatHistoryResponse(_CamelModel):
    messages: list[ChatMessage]


def _team_name_map(*, storage: StorageBackend, user: str | None) -> dict[str, str]:
    return {t.id: t.name for t in oc_svc.list_teams(user=user, storage=storage)}


def _summary(a: ManagedAgent, *, team_name: str) -> ManagedAgentSummary:
    return ManagedAgentSummary(
        id=a.id, kind=a.kind, name=a.name, description=a.description,
        team_id=a.team_id, team_name=team_name, config_home=a.config_home,
        created_by_user=a.created_by_user, created_at=iso_utc(a.created_at),
    )


def _detail(a: ManagedAgent, *, team_name: str) -> ManagedAgentDetail:
    return ManagedAgentDetail(
        id=a.id, kind=a.kind, name=a.name, description=a.description,
        team_id=a.team_id, team_name=team_name, config_home=a.config_home,
        created_by_user=a.created_by_user, created_at=iso_utc(a.created_at),
        nl_prompt=a.nl_prompt, clawteam_profile=a.clawteam_profile,
    )


def _map_err(exc: svc.ManagedAgentError) -> ApiError:
    mapping = {
        svc.KindUnsupported: ("INVALID_PAYLOAD", 400),
        svc.AgentIdInvalid: ("INVALID_PAYLOAD", 400),
        svc.AgentAlreadyExists: ("AGENT_ALREADY_EXISTS", 409),
        svc.AgentNotFound: ("AGENT_NOT_FOUND", 404),
        svc.AgentInUse: ("AGENT_IN_USE", 409),
        svc.CliUnavailable: ("CLI_UNAVAILABLE", 503),
        svc.CliFailed: ("CLI_FAILED", 502),
    }
    code, status = mapping.get(type(exc), ("MANAGED_ERROR", 500))
    return ApiError(code, str(exc), status_code=status, details=exc.details)


def _owned(agent_id: str, user: str, storage: StorageBackend) -> ManagedAgent:
    try:
        row = svc.get_agent(agent_id, storage=storage)
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc
    if row.created_by_user and row.created_by_user != user:
        raise ApiError("FORBIDDEN", "not your agent", status_code=403)
    return row


def _session_key(user: str, agent_id: str) -> str:
    return f"managed-user-chat-{user}-{agent_id}"


# ── CRUD ──────────────────────────────────────────────────────────────


@router.get("", response_model=ManagedListResponse)
def list_agents(
    user: UserDep, storage: StorageDep,
    kind: Annotated[str | None, Query()] = None,
) -> ManagedListResponse:
    items = svc.list_agents(user=user, kind=kind, storage=storage)
    names = _team_name_map(storage=storage, user=user)
    return ManagedListResponse(items=[_summary(a, team_name=names.get(a.team_id, "")) for a in items])


@router.get("/runtime/status", response_model=RuntimeStatusResponse)
def runtime_status(
    user: UserDep, kind: Annotated[str, Query()],
) -> RuntimeStatusResponse:
    del user
    try:
        running, reason = svc.probe_runtime_running(kind)
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc
    return RuntimeStatusResponse(running=running, reason=reason)


@router.post("", response_model=ManagedAgentDetail, status_code=201)
async def create_agent(
    payload: Annotated[CreatePayload, Body()], user: UserDep, storage: StorageDep,
) -> ManagedAgentDetail:
    aid = (payload.id or payload.name or "").strip().lower()
    if not payload.id:
        aid = "".join(ch for ch in aid if ch.isalnum() or ch == "-").strip("-")
    display_name = (payload.name or aid).strip() or aid
    cmd = svc.CommitInput(
        id=aid, kind=payload.kind, name=display_name,
        description=payload.responsibility, nl_prompt=payload.responsibility,
        team_id=payload.team_id,
    )
    loop = asyncio.get_running_loop()
    try:
        row = await loop.run_in_executor(
            _EXEC, lambda: svc.commit_agent(cmd, user=user, storage=storage),
        )
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc
    names = _team_name_map(storage=storage, user=user)
    return _detail(row, team_name=names.get(row.team_id, ""))


@router.post("/{agent_id}/cancel-create", status_code=202)
async def cancel_create(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> dict[str, bool]:
    """Cancel/roll back an in-flight or just-finished create for *agent_id*."""
    del user
    loop = asyncio.get_running_loop()
    try:
        rolled = await loop.run_in_executor(
            _EXEC, lambda: svc.cancel_create_agent(agent_id, storage=storage),
        )
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc
    return {"rolledBack": rolled}


@router.get("/{agent_id}", response_model=ManagedAgentDetail)
def get_agent(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> ManagedAgentDetail:
    row = _owned(agent_id, user, storage)
    names = _team_name_map(storage=storage, user=user)
    return _detail(row, team_name=names.get(row.team_id, ""))


@router.patch("/{agent_id}", response_model=ManagedAgentDetail)
def patch_agent(
    agent_id: Annotated[str, Path()], payload: Annotated[UpdatePayload, Body()],
    user: UserDep, storage: StorageDep,
) -> ManagedAgentDetail:
    _owned(agent_id, user, storage)
    try:
        row = svc.update_agent(
            agent_id,
            svc.UpdateInput(name=payload.name, description=payload.description, team_id=payload.team_id),
            storage=storage,
        )
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc
    names = _team_name_map(storage=storage, user=user)
    return _detail(row, team_name=names.get(row.team_id, ""))


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> None:
    _owned(agent_id, user, storage)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_EXEC, lambda: svc.delete_agent(agent_id, storage=storage))
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc


# ── settings ──────────────────────────────────────────────────────────


@router.get("/{agent_id}/settings/role", response_model=RoleDocView)
def get_role(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> RoleDocView:
    _owned(agent_id, user, storage)
    return RoleDocView(content=svc.read_role_doc(agent_id, storage=storage))


@router.put("/{agent_id}/settings/role", response_model=RoleDocView)
def put_role(
    agent_id: Annotated[str, Path()], payload: Annotated[RoleDocView, Body()],
    user: UserDep, storage: StorageDep,
) -> RoleDocView:
    _owned(agent_id, user, storage)
    return RoleDocView(content=svc.write_role_doc(agent_id, payload.content, storage=storage))


@router.get("/{agent_id}/settings/mcp", response_model=list[McpServerView])
def get_mcp(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> list[McpServerView]:
    _owned(agent_id, user, storage)
    try:
        return [McpServerView(**s) for s in svc.list_mcp(agent_id, storage=storage)]
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc


@router.post("/{agent_id}/settings/mcp", status_code=201)
def post_mcp(
    agent_id: Annotated[str, Path()], payload: Annotated[AddMcpPayload, Body()],
    user: UserDep, storage: StorageDep,
) -> None:
    _owned(agent_id, user, storage)
    try:
        svc.add_mcp(agent_id, name=payload.name, command=payload.command, storage=storage)
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc


@router.delete("/{agent_id}/settings/mcp/{name}", status_code=204)
def del_mcp(
    agent_id: Annotated[str, Path()], name: Annotated[str, Path()],
    user: UserDep, storage: StorageDep,
) -> None:
    _owned(agent_id, user, storage)
    try:
        svc.remove_mcp(agent_id, name, storage=storage)
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc


@router.get("/{agent_id}/settings/skills", response_model=list[SkillView])
def get_skills(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> list[SkillView]:
    _owned(agent_id, user, storage)
    return [SkillView(**s) for s in svc.list_skills(agent_id, storage=storage)]


@router.get("/{agent_id}/settings/skills/{name}", response_model=SkillView)
def get_skill(
    agent_id: Annotated[str, Path()], name: Annotated[str, Path()],
    user: UserDep, storage: StorageDep,
) -> SkillView:
    _owned(agent_id, user, storage)
    try:
        return SkillView(name=name, content=svc.read_skill(agent_id, name, storage=storage))
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc


@router.post("/{agent_id}/settings/skills", response_model=SkillView, status_code=201)
def create_skill(
    agent_id: Annotated[str, Path()],
    payload: Annotated[SkillCreatePayload, Body()],
    user: UserDep, storage: StorageDep,
) -> SkillView:
    _owned(agent_id, user, storage)
    try:
        out = svc.write_skill(
            agent_id, name=payload.name, description=payload.description,
            content=payload.content, storage=storage,
        )
    except svc.ManagedAgentError as exc:
        raise _map_err(exc) from exc
    return SkillView(name=out["name"], description=out.get("description", ""), path=out.get("path", ""))


# ── chat ──────────────────────────────────────────────────────────────


@router.get("/{agent_id}/chat-history", response_model=ChatHistoryResponse)
async def chat_history_view(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> ChatHistoryResponse:
    _owned(agent_id, user, storage)
    rows = await chat_history.list_messages(_session_key(user, agent_id))
    return ChatHistoryResponse(messages=[ChatMessage(**m) for m in rows])


@router.post("/{agent_id}/chat")
async def chat_with_agent(
    agent_id: Annotated[str, Path()], payload: Annotated[ChatPayload, Body()],
    user: UserDep, storage: StorageDep,
):
    _owned(agent_id, user, storage)
    message = (payload.message or "").strip()
    workdir = (payload.workdir or "").strip()
    if not message:
        raise ApiError("INVALID_PAYLOAD", "message is required", status_code=400)
    if not workdir:
        raise ApiError("INVALID_PAYLOAD", "workdir is required", status_code=400)
    session_key = _session_key(user, agent_id)
    await chat_history.append_message(session_key, role="user", content=message)

    async def _stream():
        loop = asyncio.get_running_loop()
        try:
            answer = await loop.run_in_executor(
                _EXEC, lambda: svc.chat_once(agent_id, message=message, workdir=workdir, storage=storage),
            )
        except svc.ManagedAgentError as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            yield "data: [DONE]\n\n"
            return
        await chat_history.append_message(session_key, role="assistant", content=answer)
        yield f"data: {json.dumps({'delta': answer})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.post("/{agent_id}/reset", status_code=204)
async def reset_chat(agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep) -> None:
    _owned(agent_id, user, storage)
    await chat_history.clear_messages(_session_key(user, agent_id))


__all__ = ["router"]
