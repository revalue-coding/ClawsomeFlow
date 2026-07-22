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
                                                    (``?agentId=`` → profile-scoped)
* ``GET    /hermes/agents/claimable``             — existing unmanaged profiles
* ``POST   /hermes/agents/claim``                 — register an existing profile
* ``*      /hermes/agents/{id}/settings/...``     — soul / model / secrets / skills / cron
* ``POST   /hermes/agents/{id}/chat``             — direct chat (SSE; step progress + final)
* ``GET    /hermes/agents/{id}/chat/status``      — live turn state (reconnect after refresh)
* ``GET    /hermes/agents/{id}/chat-history``     — UI chat history cache
* ``POST   /hermes/agents/{id}/reset``            — clear cached history

Teams are the shared :class:`OpenclawTeam` grouping (see ``/api/openclaw/agents/teams``).
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path as FsPath
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api.errors import ApiError, map_hermes_agent_error
from app.logging_setup import get_logger
from app.models import HermesAgent, iso_utc
from app.operations import get_op_registry
from app.scheduler.naming import hermes_user_chat_session_id
from app.services import chat_attachments as attachment_svc
from app.services import hermes_agents as svc
from app.services import hermes_chat as chat_svc
from app.services import hermes_chat_sessions as chat_sessions
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


class HermesAgentCreateResponse(HermesAgentDetail):
    # Non-fatal: the agent WAS created, but its self-definition bootstrap
    # (``hermes -z``) did not complete (e.g. no inference provider configured).
    # Empty string ⇒ bootstrap completed normally. The WebUI surfaces this so a
    # half-defined agent (empty SOUL.md) isn't reported as fully ready.
    bootstrap_warning: str = ""


class HermesAgentListResponse(_CamelModel):
    items: list[HermesAgentSummary]


class HermesRuntimeStatusResponse(_CamelModel):
    running: bool
    reason: str


class HermesDashboardOpenResponse(_CamelModel):
    url: str


class HermesGatewayStartResponse(_CamelModel):
    message: str


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
    # "default" (root/active profile) or an existing profile id.
    model_inherit_from: str = "default"
    # Optional "clone config from another agent": "" = no clone, "default" =
    # active/root profile, otherwise an existing profile id. ``clone_all`` does a
    # full clone (memories/sessions/skills/state) vs. a light config-only clone.
    clone_from: str = ""
    clone_all: bool = False


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


class GatewayView(_CamelModel):
    cwd: str = ""


class ModelImportPayload(_CamelModel):
    inherit_from: str = "default"


class McpServerView(_CamelModel):
    name: str
    transport: str = "http_sse"
    url: str = ""
    command: str = ""
    args: list[str] = []
    enabled: bool = True
    env_keys: list[str] = []


class McpServerUpsertPayload(_CamelModel):
    name: str
    transport: str = "http_sse"
    url: str = ""
    command: str = ""
    args: list[str] = []
    # Omitted/None → preserve existing env (edit path); "" → clear; text → replace.
    environment: str | None = None


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


class SkillUpdatePayload(_CamelModel):
    description: str = ""
    content: str = ""


class CronJobView(_CamelModel):
    id: str
    name: str = ""
    schedule: str = ""
    enabled: bool = True
    prompt: str = ""
    deliver: str = ""
    workdir: str = ""
    next_run: str = ""
    last_run: str = ""
    detail: str = ""
    raw: str = ""


class DeliveryTargetView(_CamelModel):
    value: str
    label: str


class CronListResponse(_CamelModel):
    available: bool
    items: list[CronJobView]
    delivery_targets: list[DeliveryTargetView] = []


class CreateCronPayload(_CamelModel):
    schedule: str
    prompt: str = ""
    name: str = ""
    workdir: str = ""
    deliver: str = "local"


class EditCronPayload(_CamelModel):
    # All optional — only fields actually present are forwarded to
    # ``hermes cron edit`` so an edit never clobbers untouched fields.
    schedule: str | None = None
    prompt: str | None = None
    name: str | None = None
    deliver: str | None = None
    workdir: str | None = None


class ChatPayload(_CamelModel):
    message: str
    workdir: str
    attachments: list[ChatAttachment] = Field(default_factory=list)


class ChatAttachment(_CamelModel):
    id: str
    name: str
    mime_type: str = ""
    size_bytes: int = 0
    absolute_path: str
    relative_path: str
    route: Literal["path_injection", "native"] = "path_injection"


class ChatMessage(_CamelModel):
    role: str
    content: str
    attachments: list[ChatAttachment] | None = None
    # Epoch ms the message was recorded server-side (chat-history responses only).
    ts: int | None = None
    # Stable server id (chat-history only); the UI keys render + dedup off it.
    id: int | None = None
    # "session_divider" for the persistent reset marker; normal messages omit it.
    kind: str | None = None


class ChatHistoryResponse(_CamelModel):
    messages: list[ChatMessage]


class ChatAttachmentUploadResponse(_CamelModel):
    attachment: ChatAttachment
    limits: dict[str, int]


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
    # Mapping single-sourced in app.api.errors (also backs the global
    # HermesAgentError exception handler registered there).
    return map_hermes_agent_error(exc)


def _get_owned(agent_id: str, user: str, storage: StorageBackend) -> HermesAgent:
    try:
        row = svc.get_agent(agent_id, storage=storage)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    if row.created_by_user and row.created_by_user != user:
        raise ApiError("FORBIDDEN", "not your agent", status_code=403)
    return row


def _session_key(user: str, agent_id: str) -> str:
    """Runtime key for the Hermes turn registry + session binding."""
    return hermes_user_chat_session_id(user, agent_id)


def _conversation_key(user: str, agent_id: str) -> str:
    """Key for persisted chat history (UI transcript).

    Equal to :func:`_session_key` today (Hermes' key is already revision-free),
    kept separate for parity with OpenClaw and so history/runtime can diverge
    without touching call sites.
    """
    return hermes_user_chat_session_id(user, agent_id)


def _resolve_workdir_path(workdir: str) -> FsPath:
    try:
        path = svc._existing_directory(workdir, field_name="workdir")
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return FsPath(path).expanduser().resolve(strict=False)


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


async def _store_hermes_chat_upload(
    *,
    request: Request,
    workdir: FsPath,
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
            base_dir=workdir,
            raw_filename=filename,
            mime_type=request.headers.get("content-type", ""),
            content=body,
        )
    except ValueError as exc:
        message = str(exc)
        code = "INVALID_ATTACHMENT"
        status = 400
        if "size limit" in message:
            code = "ATTACHMENT_TOO_LARGE"
            status = 413
        raise ApiError(code, message, status_code=status) from exc


def _resolve_hermes_payload_attachments(
    *,
    workdir: FsPath,
    attachments: list[ChatAttachment],
) -> list[attachment_svc.StoredAttachment]:
    resolved: list[attachment_svc.StoredAttachment] = []
    for item in attachments:
        try:
            resolved.append(
                attachment_svc.resolve_existing_attachment(
                    base_dir=workdir,
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


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────


@router.get("", response_model=HermesAgentListResponse)
def list_agents(
    user: UserDep,
    storage: StorageDep,
    mode: Annotated[str, Query()] = svc.RECONCILE_FULL,
) -> HermesAgentListResponse:
    # ``mode=fast`` scans ``~/.hermes/profiles/`` for instant first paint;
    # ``full`` (default) runs ``hermes profile list`` for authoritative reconcile.
    reconcile = svc.RECONCILE_FAST if mode == svc.RECONCILE_FAST else svc.RECONCILE_FULL
    items = svc.list_agents(user=user, storage=storage, reconcile=reconcile)
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
def open_dashboard(
    user: UserDep,
    storage: StorageDep,
    agent_id: Annotated[str | None, Query(alias="agentId")] = None,
) -> HermesDashboardOpenResponse:
    # With an agent id, open a dashboard scoped to that agent's Hermes profile so
    # the user lands on *that agent's* sessions (the root home has none of them).
    profile: str | None = None
    if agent_id:
        _get_owned(agent_id, user, storage)
        profile = agent_id
    try:
        url = dash_svc.ensure_hermes_dashboard_url(profile=profile)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return HermesDashboardOpenResponse(url=url)


@router.post("/{agent_id}/gateway/start", response_model=HermesGatewayStartResponse)
def start_gateway(
    agent_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> HermesGatewayStartResponse:
    _get_owned(agent_id, user, storage)
    try:
        message = svc.start_gateway(agent_id)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return HermesGatewayStartResponse(message=message)


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


@router.post("", response_model=HermesAgentCreateResponse, status_code=201)
async def create_agent(
    payload: Annotated[CreatePayload, Body()], user: UserDep, storage: StorageDep,
) -> HermesAgentCreateResponse:
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
        # Pass through verbatim ("" means "do not inherit"); the default value of
        # ``model_inherit_from`` is "default", so omitting it keeps legacy behaviour.
        model_inherit_from=payload.model_inherit_from,
        clone_from=payload.clone_from,
        clone_all=payload.clone_all,
    )
    op_id = f"hermes_create:{agent_id}"
    reg = get_op_registry()
    reg.start(op_id=op_id, user=user, kind="hermes_create")
    loop = asyncio.get_running_loop()
    # Populated in place by the service so we can report a non-fatal warning when
    # the self-definition bootstrap failed but the agent was still created.
    outcome = svc.BootstrapOutcome()

    async def _commit():
        # Records the op's terminal state on the event loop. Detached from the
        # request (shield below) so a client disconnect mid-create still marks
        # the op succeeded/failed — otherwise the recovery UI is stuck "running".
        try:
            row = await loop.run_in_executor(
                _CHAT_EXECUTOR,
                lambda: svc.commit_agent(
                    cmd, user=user, storage=storage, outcome=outcome
                ),
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
        # Surface a bootstrap failure in the op result too, so the recovery UI
        # (which reads the op registry after a disconnect) can show it as well.
        result = {"agentId": row.id}
        if outcome.ran and not outcome.ok:
            result["bootstrapWarning"] = outcome.error or "self-definition incomplete"
        reg.succeed(op_id, result=result)
        return row

    try:
        row = await asyncio.shield(_spawn_detached(_commit()))
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    team_names = _team_name_map(storage=storage, user=user)
    detail = _to_detail(row, team_name=team_names.get(row.team_id, ""))
    bootstrap_warning = (
        (outcome.error or "self-definition incomplete")
        if (outcome.ran and not outcome.ok)
        else ""
    )
    if bootstrap_warning:
        logger.warning(
            "hermes_agent_created_bootstrap_incomplete",
            agent_id=row.id,
            error=bootstrap_warning,
        )
    return HermesAgentCreateResponse(
        **detail.model_dump(by_alias=False),
        bootstrap_warning=bootstrap_warning,
    )


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
    # Drop the persisted chat transcript + session binding so a deleted agent
    # leaves no orphan history behind (best-effort).
    try:
        chat_sessions.clear_session_id(_session_key(user, agent_id))
        await chat_history.clear_messages(_conversation_key(user, agent_id))
    except Exception:  # pragma: no cover - cleanup is best-effort
        logger.warning("hermes_chat_history_cleanup_failed", agent_id=agent_id)


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


@router.get("/{agent_id}/settings/gateway", response_model=GatewayView)
def get_gateway_settings(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep
) -> GatewayView:
    _get_owned(agent_id, user, storage)
    g = svc.read_gateway_cwd(agent_id)
    return GatewayView(cwd=g["cwd"])


@router.put("/{agent_id}/settings/gateway", response_model=GatewayView)
def put_gateway_settings(
    agent_id: Annotated[str, Path()],
    payload: Annotated[GatewayView, Body()],
    user: UserDep,
    storage: StorageDep,
) -> GatewayView:
    _get_owned(agent_id, user, storage)
    try:
        g = svc.write_gateway_cwd(agent_id, cwd=payload.cwd)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return GatewayView(cwd=g["cwd"])


@router.post("/{agent_id}/settings/model/import", response_model=ModelView)
def import_model(
    agent_id: Annotated[str, Path()],
    payload: Annotated[ModelImportPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> ModelView:
    _get_owned(agent_id, user, storage)
    try:
        m = svc.import_model_from_profile(
            agent_id, source_profile=payload.inherit_from or "default"
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return ModelView(default=m["default"], provider=m["provider"], base_url=m["base_url"])


@router.get("/{agent_id}/settings/mcp", response_model=list[McpServerView])
def get_mcp_servers(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep
) -> list[McpServerView]:
    _get_owned(agent_id, user, storage)
    try:
        rows = svc.list_mcp_servers(agent_id)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return [McpServerView(**row) for row in rows]


@router.put("/{agent_id}/settings/mcp", response_model=McpServerView)
def put_mcp_server(
    agent_id: Annotated[str, Path()],
    payload: Annotated[McpServerUpsertPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> McpServerView:
    _get_owned(agent_id, user, storage)
    try:
        row = svc.upsert_mcp_server(
            agent_id,
            name=payload.name,
            transport=payload.transport,
            url=payload.url,
            command=payload.command,
            args=payload.args,
            environment=payload.environment,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc
    return McpServerView(**row)


@router.delete("/{agent_id}/settings/mcp/{name}", status_code=204)
def del_mcp_server(
    agent_id: Annotated[str, Path()],
    name: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    try:
        svc.delete_mcp_server(agent_id, name)
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


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
    meta = svc._parse_skill_front_matter(content)
    return SkillView(name=meta.get("name") or name, description=meta.get("description", ""), content=content)


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


@router.put("/{agent_id}/settings/skills/{name}", response_model=SkillView)
def update_skill(
    agent_id: Annotated[str, Path()],
    name: Annotated[str, Path()],
    payload: Annotated[SkillUpdatePayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> SkillView:
    _get_owned(agent_id, user, storage)
    try:
        out = svc.update_skill(
            agent_id, name=name, description=payload.description, content=payload.content,
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
        return CronListResponse(available=False, items=[], delivery_targets=[])
    return CronListResponse(
        available=True,
        items=[CronJobView(**j) for j in svc.list_cron(agent_id)],
        delivery_targets=[
            DeliveryTargetView(**t) for t in svc.list_cron_delivery_targets(agent_id)
        ],
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
            name=payload.name, workdir=payload.workdir, deliver=payload.deliver,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc


@router.put("/{agent_id}/settings/cron/{job_id}", status_code=204)
def edit_cron(
    agent_id: Annotated[str, Path()],
    job_id: Annotated[str, Path()],
    payload: Annotated[EditCronPayload, Body()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    try:
        svc.edit_cron(
            agent_id, job_id,
            schedule=payload.schedule, prompt=payload.prompt,
            name=payload.name, deliver=payload.deliver, workdir=payload.workdir,
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


@router.get(
    "/{agent_id}/chat-history",
    response_model=ChatHistoryResponse,
    response_model_exclude_none=True,
)
async def chat_history_view(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> ChatHistoryResponse:
    _get_owned(agent_id, user, storage)
    rows = await chat_history.list_messages(_conversation_key(user, agent_id))
    return ChatHistoryResponse(messages=[ChatMessage(**m) for m in rows])


@router.post("/{agent_id}/chat/attachments", response_model=ChatAttachmentUploadResponse)
async def upload_chat_attachment(
    agent_id: Annotated[str, Path()],
    filename: Annotated[str, Query(min_length=1, max_length=255)],
    workdir: Annotated[str, Query(min_length=1)],
    request: Request,
    user: UserDep,
    storage: StorageDep,
) -> ChatAttachmentUploadResponse:
    _get_owned(agent_id, user, storage)
    workdir_path = _resolve_workdir_path(workdir)
    stored = await _store_hermes_chat_upload(
        request=request,
        workdir=workdir_path,
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


async def _finalize_chat_history(
    job: chat_svc.ChatJob, session_key: str, conversation_key: str,
) -> None:
    """Persist the final answer to chat history when the job completes, even if
    the SSE client disconnected. Skips killed/superseded jobs (status != done),
    so a reset never leaves a ghost reply behind."""
    while job.snapshot()["status"] == "running":
        await asyncio.sleep(0.5)
    snap = job.snapshot()
    if snap["status"] == "done":
        if job.hermes_session_id:
            chat_sessions.set_session_id(session_key, job.hermes_session_id)
        if snap["final"]:
            await chat_history.append_message(
                conversation_key, role="assistant", content=snap["final"],
            )
            logger.info(
                "hermes_chat_history_appended",
                session_key=session_key,
                agent_id=job.agent_id,
                final_len=len(snap["final"]),
                tool_only=snap["final"] == chat_svc._NO_TEXT_REPLY_MARKER,
                hermes_session_id=job.hermes_session_id,
            )
        elif job.hermes_session_id:
            logger.info(
                "hermes_chat_session_bound_without_reply",
                session_key=session_key,
                agent_id=job.agent_id,
                hermes_session_id=job.hermes_session_id,
            )
    else:
        logger.info(
            "hermes_chat_history_skipped",
            session_key=session_key,
            agent_id=job.agent_id,
            status=snap["status"],
            final_len=len(snap.get("final") or ""),
            error_preview=(snap.get("error") or "")[:240] or None,
        )


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
    if not workdir:
        raise ApiError("INVALID_PAYLOAD", "workdir is required", status_code=400)
    workdir_path = _resolve_workdir_path(workdir)
    resolved_attachments = _resolve_hermes_payload_attachments(
        workdir=workdir_path,
        attachments=payload.attachments,
    )
    if not message and not resolved_attachments:
        raise ApiError("INVALID_PAYLOAD", "message is required", status_code=400)

    runtime_message = message
    if resolved_attachments:
        prompt_head = message or "Please inspect the uploaded files."
        injected = attachment_svc.build_path_injection_message(
            user_message=prompt_head,
            attachments=resolved_attachments,
        )
        runtime_message = injected

    session_key = _session_key(user, agent_id)  # runtime turn registry + binding
    conversation_key = _conversation_key(user, agent_id)  # persisted transcript
    # Previous turn may have persisted the user row then failed before an
    # assistant reply — drop that orphan before resume/history decisions so a
    # failed-only transcript cannot force Hermes ``-c`` / resume.
    await chat_history.drop_trailing_unanswered_user(conversation_key)
    # Resume ONLY when we have a persisted Hermes session id. Reset now KEEPS the
    # transcript (it appends a session_divider instead of clearing), so history
    # length can no longer be used to decide resume — a reset drops the saved id,
    # which is the sole signal that starts a fresh Hermes session next turn.
    saved_hermes_session_id = chat_sessions.get_session_id(session_key)
    resume = saved_hermes_session_id is not None
    await chat_history.append_message(
        conversation_key,
        role="user",
        content=message,
        attachments=_attachments_for_history(
            resolved_attachments,
            route="path_injection",
        ),
    )

    try:
        job = chat_svc.start_chat(
            agent_id,
            message=runtime_message,
            workdir=workdir,
            resume=resume,
            session_key=session_key,
            resume_session_id=saved_hermes_session_id,
        )
    except svc.HermesAgentError as exc:
        raise _map_service_error(exc) from exc

    # Record the final answer independently of the SSE client's lifetime so a
    # tab switch / disconnect still lands the reply in history (the status poll
    # and next page load recover it).
    _spawn_detached(_finalize_chat_history(job, session_key, conversation_key))

    async def _stream():
        while True:
            snap = job.snapshot()
            yield f"data: {json.dumps({'progress': snap['progress']})}\n\n"
            if snap["status"] != "running":
                if snap["status"] == "done":
                    final = snap["final"]
                    if final and final != chat_svc._NO_TEXT_REPLY_MARKER:
                        yield f"data: {json.dumps({'delta': final})}\n\n"
                else:
                    yield f"data: {json.dumps({'error': snap['error'] or 'chat failed'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        # Defeat response buffering (e.g. a reverse proxy) so SSE events reach the
        # browser as they are produced instead of all-at-once at turn end.
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
    """Live turn state for reconnect (tab switch / refresh). ``status`` is
    ``idle`` when no turn is tracked; otherwise the running/done/error job
    snapshot (steps + progress + final)."""
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
    """Stop the in-flight turn WITHOUT clearing history (the user's "stop
    generating" action). The killed job goes to ``error: cancelled`` so the SSE
    stream ends cleanly; the question stays in history for a regenerate/retry."""
    _get_owned(agent_id, user, storage)
    chat_svc.kill_chat(_session_key(user, agent_id))


@router.post("/{agent_id}/reset", status_code=204)
async def reset_chat(
    agent_id: Annotated[str, Path()], user: UserDep, storage: StorageDep,
) -> None:
    _get_owned(agent_id, user, storage)
    session_key = _session_key(user, agent_id)
    conversation_key = _conversation_key(user, agent_id)
    # Kill any in-flight turn FIRST so a "reset" can't leave a runaway hermes
    # process that later appends a ghost reply into the transcript.
    chat_svc.kill_chat(session_key)
    # Forget our session binding so the next WebUI turn creates a fresh Hermes
    # session (resume is now keyed solely off the saved id). KEEP the persisted
    # transcript and mark the boundary with a session_divider row.
    chat_sessions.clear_session_id(session_key)
    await chat_history.append_divider(conversation_key)


__all__ = ["router"]
