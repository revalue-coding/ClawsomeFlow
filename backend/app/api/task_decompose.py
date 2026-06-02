"""Task decomposition API (public + internal halves).

Public surface (mounted under ``/api``):
* ``POST /flows/decompose``                    — kick off
* ``GET  /flows/decompose/{request_id}``       — poll status
* ``POST /flows/decompose/{request_id}/cancel`` — cancel current request

Internal surface (mounted under ``/api/internal``, loopback + token):
* ``POST /task-decompose/commit``              — skill posts result back
* ``POST /task-decompose/fail``                — skill reports failure

Pipeline mirrors the NL-create-agent flow (Phase 4) one-for-one — same
``internal_token`` + same ``InternalCallerDep`` + same status-poll UX.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api._auth_internal import InternalCallerDep
from app.api.errors import ApiError
from app.config import load_config
from app.models import TaskDecomposeRequest, iso_utc
from app.services import task_decompose as svc
from app.services.task_decompose_validation import (
    ProposalValidationError,
    validate_decompose_proposal,
)
from app.storage import StorageBackend, get_storage

# ──────────────────────────────────────────────────────────────────────


def _storage_dep() -> StorageBackend:
    return get_storage()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


# ──────────────────────────────────────────────────────────────────────
# Public router (mounted in app.api.__init__ at /api)
# ──────────────────────────────────────────────────────────────────────


public_router = APIRouter(tags=["decompose"])


class DecomposeStartPayload(_CamelModel):
    goal: str = Field(..., description="Natural-language Flow goal.")
    leader_agent_id: str = Field(
        ..., description="Leader agent id for decomposition dispatch.",
    )
    leader_kind: str | None = Field(
        default=None,
        description=(
            "Leader kind. Required for non-OpenClaw leaders "
            "(e.g. claude/codex/cursor/hermes)."
        ),
    )
    leader_repo: str | None = Field(
        default=None,
        description="Leader repo path when leader kind is non-OpenClaw.",
    )
    leader_target_branch: str | None = Field(
        default=None,
        description="Leader target branch when leader kind is non-OpenClaw.",
    )
    existing_agents: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Hints — agents already in the editor.",
    )
    existing_tasks: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Hints — tasks already in the editor.",
    )
    result_language: Literal["zh", "en"] | None = Field(
        default=None,
        description=(
            "Preferred language for generated task subject/description. "
            "If omitted, leader may follow the goal language."
        ),
    )


class DecomposeStartResponse(_CamelModel):
    request_id: str
    status: str
    token_ttl_seconds: int
    status_url: str


class DecomposeStatusResponse(_CamelModel):
    request_id: str
    status: str
    goal: str
    leader_agent_id: str
    existing_agents: list[dict[str, Any]] = Field(default_factory=list)
    existing_tasks: list[dict[str, Any]] = Field(default_factory=list)
    result_agents: list[dict[str, Any]] | None = None
    result_tasks: list[dict[str, Any]] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    expires_at: str


def _to_status_view(r: TaskDecomposeRequest) -> DecomposeStatusResponse:
    return DecomposeStatusResponse(
        request_id=r.request_id,
        status=r.status.value if hasattr(r.status, "value") else str(r.status),
        goal=r.goal,
        leader_agent_id=r.leader_agent_id,
        existing_agents=r.existing_agents or [],
        existing_tasks=r.existing_tasks or [],
        result_agents=r.result_agents,
        result_tasks=r.result_tasks,
        error_code=r.error_code,
        error_message=r.error_message,
        created_at=iso_utc(r.created_at),
        updated_at=iso_utc(r.updated_at),
        expires_at=iso_utc(r.expires_at),
    )


@public_router.post(
    "/flows/decompose",
    response_model=DecomposeStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_decompose(
    payload: Annotated[DecomposeStartPayload, Body()],
    user: UserDep,
    storage: StorageDep,
    request: Request,
) -> DecomposeStartResponse:
    """Kick off an AI decompose request.

    Returns a request_id for the front-end to poll.
    """
    # OpenClaw callback curl runs on the server box, so it must hit *this
    # server's* loopback — not whatever host the browser dialled (which over
    # an SSH tunnel is the user's laptop and connection-refused for leader
    # processes). Pin local.
    cfg = load_config()
    api_base = f"http://127.0.0.1:{cfg.csflow_port}"
    try:
        result = await svc.start_decompose_request(
            goal=payload.goal,
            leader_agent_id=payload.leader_agent_id,
            leader_kind=payload.leader_kind,
            leader_repo=payload.leader_repo,
            leader_target_branch=payload.leader_target_branch,
            user=user,
            api_base=api_base,
            existing_agents=payload.existing_agents,
            existing_tasks=payload.existing_tasks,
            result_language=payload.result_language,
            storage=storage,
        )
    except svc.LeaderAgentNotFound as exc:
        raise ApiError(exc.code, exc.message, status_code=exc.status_code,
                       details=exc.details) from exc
    except svc.TaskDecomposeError as exc:
        raise ApiError(exc.code, exc.message, status_code=exc.status_code,
                       details=exc.details) from exc
    except ValueError as exc:
        raise ApiError("INVALID_PAYLOAD", str(exc), status_code=400) from exc

    return DecomposeStartResponse(
        request_id=result.request_id,
        status=result.status.value,
        token_ttl_seconds=result.token_ttl_seconds,
        status_url=f"/api/flows/decompose/{result.request_id}",
    )


@public_router.get(
    "/flows/decompose/{request_id}",
    response_model=DecomposeStatusResponse,
)
def get_decompose_status(
    request_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> DecomposeStatusResponse:
    svc.reap_expired_requests(storage=storage)
    row = svc.get_request(request_id, storage=storage)
    if row is None:
        raise ApiError("NOT_FOUND",
                       f"decompose request {request_id!r} not found",
                       status_code=404)
    if row.user != user:
        raise ApiError("FORBIDDEN",
                       "request belongs to a different user",
                       status_code=403)
    return _to_status_view(row)


@public_router.post(
    "/flows/decompose/{request_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_decompose(
    request_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    svc.reap_expired_requests(storage=storage)
    row = svc.get_request(request_id, storage=storage)
    if row is None:
        raise ApiError(
            "NOT_FOUND",
            f"decompose request {request_id!r} not found",
            status_code=404,
        )
    if row.user != user:
        raise ApiError(
            "FORBIDDEN",
            "request belongs to a different user",
            status_code=403,
        )
    await svc.cancel_decompose_request(request_id, storage=storage)


# ──────────────────────────────────────────────────────────────────────
# Internal router (mounted in app.api.__init__ at /api/internal)
# ──────────────────────────────────────────────────────────────────────


internal_router = APIRouter(prefix="/internal/task-decompose", tags=["internal"])


class CommitPayload(_CamelModel):
    """Body posted by the csflow-task-decomposer skill."""

    request_id: str
    agents: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class CommitResponse(_CamelModel):
    request_id: str
    status: str
    accepted_tasks: int
    accepted_agents: int


class FailPayload(_CamelModel):
    request_id: str
    code: str = "DECOMPOSER_FAILED"
    message: str = ""


@internal_router.post(
    "/commit", response_model=CommitResponse, status_code=200,
)
async def commit_decompose(
    payload: Annotated[CommitPayload, Body()],
    caller: InternalCallerDep,
    storage: StorageDep,
) -> CommitResponse:
    if caller.purpose != "task_decompose":
        raise ApiError(
            "TOKEN_PURPOSE_MISMATCH",
            "this token is not authorised for task-decompose commits",
            status_code=401,
        )
    if caller.request_id != payload.request_id:
        raise ApiError(
            "TOKEN_REQUEST_MISMATCH",
            "token request_id does not match body request_id",
            status_code=401,
        )

    row = svc.get_request(payload.request_id, storage=storage)
    if row is None:
        raise ApiError(
            "NL_REQUEST_NOT_FOUND",
            f"request {payload.request_id!r} not found",
            status_code=404,
        )

    # Server-side validation of the proposed Flow. We deliberately use the
    # SAME Flow validators a normal Save would use, so the user can just
    # hit Save in the editor without a second round of validation errors.
    try:
        validate_decompose_proposal(
            payload.agents,
            payload.tasks,
            expected_leader=row.leader_agent_id,
        )
    except ProposalValidationError as exc:
        svc.mark_request_failed(
            payload.request_id, code=exc.code, message=exc.message,
            storage=storage,
        )
        raise ApiError(exc.code, exc.message, status_code=400,
                       details=exc.details) from exc

    svc.mark_request_succeeded(
        payload.request_id, agents=payload.agents, tasks=payload.tasks,
        storage=storage,
    )
    return CommitResponse(
        request_id=payload.request_id,
        status="succeeded",
        accepted_agents=len(payload.agents),
        accepted_tasks=len(payload.tasks),
    )


@internal_router.post("/fail", response_model=CommitResponse, status_code=200)
async def fail_decompose(
    payload: Annotated[FailPayload, Body()],
    caller: InternalCallerDep,
    storage: StorageDep,
) -> CommitResponse:
    if caller.purpose != "task_decompose":
        raise ApiError(
            "TOKEN_PURPOSE_MISMATCH",
            "this token is not authorised for task-decompose commits",
            status_code=401,
        )
    if caller.request_id != payload.request_id:
        raise ApiError("TOKEN_REQUEST_MISMATCH",
                       "token / body mismatch", status_code=401)
    svc.mark_request_failed(
        payload.request_id, code=payload.code, message=payload.message,
        storage=storage,
    )
    return CommitResponse(
        request_id=payload.request_id, status="failed",
        accepted_agents=0, accepted_tasks=0,
    )


