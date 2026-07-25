"""External execution collaboration surface (``/api/external/*``).

The ONLY inbound face a remote executor ever talks to:

* ``POST /api/external/delegate`` — accept a Flow delegation from a remote
  ClawsomeFlow. Auth = a pairing credential from
  ``Config.external_pair_tokens`` (generated via ``csflow external
  pair-token``). Triggers the referenced local Flow **unattended**.
* ``GET /api/external/delegated-runs/{run_id}`` — how the delegating origin
  learns the result: it polls us. Same pairing credential, and only for a run
  that credential itself delegated. This is what lets an origin behind NAT
  delegate at all — we never need to reach it.
* ``POST /api/external/tasks/{run_id}/{task_id}/complete`` — **legacy** (v1)
  result push for an external-node task, kept for integrations already wired
  against it. Auth = the one-time signed task token from the dispatch package.
  Nothing in the v2 protocol depends on it: a webhook executor answers the
  dispatch request or gets polled (see ``services/external_tasks``).

A pre-v2 origin that still sends ``callbackUrl``/``callbackToken`` to
``/delegate`` is served exactly as before (the storage ``run_update`` hook
pushes the report when the delegated run turns terminal).

Network rule (enforced by :class:`app.api._api_guard.ApiTokenGuardMiddleware`):
this prefix is the ONLY surface remote source IPs may reach (peer-symmetric
model — every instance enforces the same law). Open by default
(``Config.external_api_expose`` = True — the surface is credential-gated);
``csflow external expose off`` re-locks it to loopback-only. The global
api_token / same-origin rules deliberately do NOT apply here — these
endpoints carry their own, narrower credentials.
"""

from __future__ import annotations

import hmac
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Path, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.api.errors import ApiError
from app.config import load_config
from app.logging_setup import get_logger
from app.models import (
    TERMINAL_RUN_STATUSES,
    FlowRun,
    FlowSpec,
    RunStatus,
)
from app.scheduler.run_metadata import (
    DELEGATE_ORIGIN_KEY,
    EXTERNAL_CALLBACK_KEY,
    UNATTENDED_KEY,
    read_delegate_origin,
)
from app.services.external_tasks import (
    DELEGATE_SUCCESS_STATUSES,
    ExternalTaskError,
    complete_external_task,
    verify_ticket,
)
from app.storage import get_storage

router = APIRouter(prefix="/external", tags=["external"])
logger = get_logger("api.external")


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


# ──────────────────────────────────────────────────────────────────────
# Task completion (receipt)
# ──────────────────────────────────────────────────────────────────────


class ExternalCompletePayload(_CamelModel):
    status: Literal["success", "failed"]
    summary: str = ""
    token: str | None = None  # alternative to the Authorization header


class ExternalCompleteResponse(_CamelModel):
    status: str
    task_id: str


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


@router.post(
    "/tasks/{run_id}/{task_id}/complete",
    response_model=ExternalCompleteResponse,
)
async def complete_task(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    payload: Annotated[ExternalCompletePayload, Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> ExternalCompleteResponse:
    """Submit an external task result using the dispatch ticket."""
    token = _bearer(authorization) or (payload.token or "").strip()
    if not token:
        raise ApiError(
            "EXTERNAL_TICKET_MISSING",
            "provide the dispatch ticket via 'Authorization: Bearer <ticket>' "
            "or the 'token' body field",
            status_code=401,
        )
    try:
        nonce = verify_ticket(token, run_id=run_id, task_id=task_id)
    except ExternalTaskError as exc:
        raise ApiError(exc.code, exc.message, status_code=exc.status_code) from exc

    storage = get_storage()
    run = storage.run_get(run_id)
    if run is None:
        raise ApiError("NOT_FOUND", f"run {run_id!r} not found", status_code=404)
    if run.status in TERMINAL_RUN_STATUSES:
        raise ApiError(
            "EXTERNAL_RUN_NOT_ACTIVE",
            f"run is already terminal (status={run.status.value})",
            status_code=409,
        )
    try:
        result = await complete_external_task(
            storage=storage,
            run=run,
            task_id=task_id,
            nonce=nonce,
            ok=(payload.status == "success"),
            summary=payload.summary,
            source="external_api",
        )
    except ExternalTaskError as exc:
        raise ApiError(exc.code, exc.message, status_code=exc.status_code) from exc
    return ExternalCompleteResponse(status=result["status"], task_id=task_id)


# ──────────────────────────────────────────────────────────────────────
# Flow delegation (remote ClawsomeFlow → this instance)
# ──────────────────────────────────────────────────────────────────────


class DelegatePayload(_CamelModel):
    flow_id: str
    inputs: dict[str, Any] | None = None
    runtime_prompt: str | None = None
    # v1 origins pushed their address here so we could POST the result back.
    # v2 origins poll ``GET /delegated-runs/{id}`` instead and send neither —
    # optional purely so an un-upgraded origin keeps working.
    callback_url: str | None = None
    callback_token: str | None = None
    source_run_id: str | None = None
    source_task_id: str | None = None


class DelegateResponse(_CamelModel):
    id: str
    status: str
    team_name: str


def _check_pair_token(authorization: str | None) -> str:
    """Return the matching pairing-credential NAME or raise 401."""
    presented = _bearer(authorization)
    if not presented:
        raise ApiError(
            "EXTERNAL_PAIR_TOKEN_MISSING",
            "provide the pairing credential via 'Authorization: Bearer <secret>'",
            status_code=401,
        )
    tokens: dict[str, str] = getattr(load_config(), "external_pair_tokens", None) or {}
    for name, secret in tokens.items():
        if secret and hmac.compare_digest(presented, secret):
            return name
    raise ApiError(
        "EXTERNAL_PAIR_TOKEN_INVALID",
        "pairing credential not recognised",
        status_code=401,
    )


@router.post(
    "/delegate",
    response_model=DelegateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def delegate_flow(
    payload: Annotated[DelegatePayload, Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> DelegateResponse:
    """Run a local Flow on behalf of a remote ClawsomeFlow instance.

    The run executes **unattended** (no review / complaint / checkpoint
    phases — same contract as MCP-triggered runs). The origin learns the result
    by polling :func:`delegated_run_status`; we record which pairing credential
    delegated the run so only that origin can read it.

    Legacy: when the origin supplied *callback_url* + *callback_token* we also
    push the leader work report on terminal (storage ``run_update`` hook + a
    daemon thread; see ``services/external_tasks.prepare_delegate_callback``).
    """
    pair_name = _check_pair_token(authorization)

    storage = get_storage()
    flow = storage.flow_get(payload.flow_id)
    if flow is None:
        raise ApiError(
            "NOT_FOUND", f"flow {payload.flow_id!r} not found", status_code=404,
        )

    # Delegated runs are unattended like MCP triggers, but the origin brief
    # is attached ONLY to the Flow description (once). Injecting it into every
    # task description nested full external sheets and bloated peer webhooks.
    from app.api.runs import (
        _normalize_runtime_prompt,
        _prepend_runtime_prompt,
        _runtime_prompt_from_inputs,
    )
    from app.models import _new_id
    from app.scheduler.engine import get_scheduler
    from app.scheduler.naming import team_name_for_run

    run_id = _new_id("run")
    run_inputs: dict[str, Any] = dict(payload.inputs or {})
    run_inputs[UNATTENDED_KEY] = "true"
    # Ownership record for the polling endpoint (and plain observability).
    run_inputs[DELEGATE_ORIGIN_KEY] = json.dumps({
        "pairTokenName": pair_name,
        "sourceRunId": payload.source_run_id,
        "sourceTaskId": payload.source_task_id,
    })
    if payload.callback_url and payload.callback_token:
        run_inputs[EXTERNAL_CALLBACK_KEY] = json.dumps({
            "url": payload.callback_url,
            "token": payload.callback_token,
            "sourceRunId": payload.source_run_id,
            "sourceTaskId": payload.source_task_id,
        })
    run = FlowRun(
        id=run_id,
        flow_id=flow.id,
        flow_version=flow.version,
        team_name=team_name_for_run(run_id),
        status=RunStatus.pending,
        inputs=run_inputs,
        user=flow.owner_user,
        is_scheduled=False,
    )
    saved = storage.run_create(run)

    runtime_prompt = _normalize_runtime_prompt(payload.runtime_prompt)
    if runtime_prompt is None:
        runtime_prompt = _runtime_prompt_from_inputs(payload.inputs or {})
    spec = FlowSpec.model_validate(flow.spec)
    flow_description = (
        _prepend_runtime_prompt(flow.description, runtime_prompt)
        if runtime_prompt else flow.description
    )
    sched = get_scheduler()
    sched.start_run(
        run=saved, spec=spec, flow=flow,
        flow_description=flow_description,
        storage=storage,
    )
    logger.info(
        "external_delegation_accepted",
        run_id=saved.id, flow_id=flow.id, pair_name=pair_name,
        source_run_id=payload.source_run_id, source_task_id=payload.source_task_id,
    )
    return DelegateResponse(
        id=saved.id,
        status=saved.status.value if hasattr(saved.status, "value") else str(saved.status),
        team_name=saved.team_name,
    )


class DelegatedRunStatusResponse(_CamelModel):
    run_id: str
    #: Protocol vocabulary shared with webhook polling: ``running`` until the
    #: run is terminal, then ``success`` / ``failed``.
    status: str
    run_status: str
    summary: str = ""


@router.get(
    "/delegated-runs/{run_id}",
    response_model=DelegatedRunStatusResponse,
)
async def delegated_run_status(
    run_id: Annotated[str, Path()],
    authorization: Annotated[str | None, Header()] = None,
) -> DelegatedRunStatusResponse:
    """Report a delegated run's progress to the origin that delegated it.

    The origin polls this instead of us pushing a callback, so delegation works
    regardless of whether the origin is addressable. Scoped by the pairing
    credential: a credential may only read runs it delegated itself.
    """
    pair_name = _check_pair_token(authorization)

    storage = get_storage()
    run = storage.run_get(run_id)
    origin = read_delegate_origin(run) if run is not None else None
    if run is None or origin is None:
        # Also covers "delegated by a different pairing credential" — do not
        # distinguish, or this becomes a run-id probe.
        raise ApiError(
            "NOT_FOUND", f"delegated run {run_id!r} not found", status_code=404,
        )
    if str(origin.get("pairTokenName") or "") != pair_name:
        raise ApiError(
            "NOT_FOUND", f"delegated run {run_id!r} not found", status_code=404,
        )

    run_status = run.status.value if hasattr(run.status, "value") else str(run.status)
    if run.status not in TERMINAL_RUN_STATUSES:
        return DelegatedRunStatusResponse(
            run_id=run.id, status="running", run_status=run_status,
        )
    from app.services.run_report import extract_leader_report

    ok = run.status in DELEGATE_SUCCESS_STATUSES
    return DelegatedRunStatusResponse(
        run_id=run.id,
        status="success" if ok else "failed",
        run_status=run_status,
        summary=extract_leader_report(
            storage.event_list(run_id=run.id, since_id=None, limit=500),
        ) or "",
    )


__all__ = ["router"]
