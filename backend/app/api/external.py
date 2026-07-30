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
import html as html_mod
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, File, Form, Header, Path, Query, UploadFile, status
from fastapi.responses import HTMLResponse
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
# Human reply form (shareable link: any channel that can carry a URL)
# ──────────────────────────────────────────────────────────────────────
#
# ``GET  /reply/{run}/{task}?t=<ticket>`` — self-contained, mobile-friendly
# HTML form (NOT the SPA: the SPA is same-origin only, while this prefix is
# the one surface remote clients may reach). The ticket in the link is the
# same one-time dispatch credential the legacy receipt uses; a re-dispatch
# invalidates it, so a link identifies exactly one attempt of one task.
# ``POST /reply/{run}/{task}`` — multipart submit (status, summary, files).
# Attachments are stored under the run's own state dir and funnel through
# the ONE completion choke point together with the summary.

_REPLY_TEXTS = {
    "zh": {
        "title": "ClawsomeFlow 人工任务回执",
        "task": "任务",
        "assignee": "指派",
        "requirement": "输出要求",
        "result": "执行结果",
        "success": "成功",
        "failed": "失败 / 无法完成",
        "summary": "结果摘要",
        "summary_ph": "说明结论、关键数据；失败时写明原因。",
        "attachments": "附件（可选，最多 {max_count} 个，单个 ≤ {max_mb}MB）",
        "submit": "提交回执",
        "done_title": "回执已提交",
        "done_body": "结果已记录，任务流程将继续。可以关闭本页面。",
        "already_title": "回执已存在",
        "already_body": "这次派发的结果此前已提交过，无需重复操作。",
        "stale_title": "链接已失效",
        "stale_body": "该任务已被重新派发或已完成，此链接对应的派发已不再有效。"
                      "请使用最新的回执链接，或联系任务发起人。",
        "invalid_title": "链接无效",
        "invalid_body": "回执链接不完整或签名无效，请核对后重试。",
        "run_over_title": "执行流已结束",
        "run_over_body": "该执行流已结束，无法再提交回执。",
        "error_title": "提交失败",
    },
    "en": {
        "title": "ClawsomeFlow Human Task Receipt",
        "task": "Task",
        "assignee": "Assignee",
        "requirement": "Output requirement",
        "result": "Result",
        "success": "Success",
        "failed": "Failed / cannot complete",
        "summary": "Summary",
        "summary_ph": "State the conclusion and key data; on failure, the reason.",
        "attachments": "Attachments (optional, up to {max_count} files, ≤ {max_mb}MB each)",
        "submit": "Submit receipt",
        "done_title": "Receipt submitted",
        "done_body": "The result has been recorded and the flow will continue. "
                     "You can close this page.",
        "already_title": "Already recorded",
        "already_body": "A result for this dispatch was already submitted; "
                        "nothing else to do.",
        "stale_title": "Link no longer valid",
        "stale_body": "This task was re-dispatched or already finished, so this "
                      "link's dispatch is no longer valid. Use the newest reply "
                      "link or contact the flow owner.",
        "invalid_title": "Invalid link",
        "invalid_body": "The reply link is incomplete or its signature is "
                        "invalid. Please check and retry.",
        "run_over_title": "Run finished",
        "run_over_body": "This run already finished; receipts can no longer be "
                         "submitted.",
        "error_title": "Submission failed",
    },
}

_REPLY_PAGE_CSS = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
    "'PingFang SC','Microsoft YaHei',sans-serif;background:#f6f7f9;color:#1a202c;"
    "margin:0;padding:16px}main{max-width:640px;margin:0 auto;background:#fff;"
    "border:1px solid #e2e8f0;border-radius:12px;padding:20px}h1{font-size:18px;"
    "margin:0 0 12px}h2{font-size:15px;margin:16px 0 6px}p{line-height:1.6}"
    "pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;"
    "border:1px solid #e2e8f0;border-radius:8px;padding:10px;font-size:13px}"
    "label{display:block;font-weight:600;margin:14px 0 6px}textarea{width:100%;"
    "box-sizing:border-box;min-height:110px;border:1px solid #cbd5e1;"
    "border-radius:8px;padding:8px;font-size:14px}input[type=file]{width:100%}"
    ".radio{display:flex;gap:18px;font-weight:400}.radio label{display:flex;"
    "align-items:center;gap:6px;font-weight:400;margin:0}button{margin-top:18px;"
    "width:100%;padding:12px;border:0;border-radius:8px;background:#4f46e5;"
    "color:#fff;font-size:15px;font-weight:600;cursor:pointer}"
    ".muted{color:#64748b;font-size:13px}"
)


def _reply_lang(subject: str = "", description: str = "") -> str:
    from app.services.run_notify import resolve_notify_language

    return resolve_notify_language(
        {"taskSubject": subject, "content": description},
    )


def _reply_page(title: str, body_html: str, *, status_code: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html_mod.escape(title)}</title>"
        f"<style>{_REPLY_PAGE_CSS}</style></head>"
        f"<body><main><h1>{html_mod.escape(title)}</h1>{body_html}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status_code)


def _reply_message_page(
    lang: str, title_key: str, body_key: str, *, status_code: int = 200,
) -> HTMLResponse:
    texts = _REPLY_TEXTS[lang]
    return _reply_page(
        texts[title_key],
        f"<p>{html_mod.escape(texts[body_key])}</p>",
        status_code=status_code,
    )


def _load_reply_context(
    run_id: str, task_id: str, token: str,
) -> tuple[HTMLResponse | None, Any, Any, str, str]:
    """Shared GET/POST validation. Returns (error_page, run, dispatch_ev, nonce, lang)."""
    from app.services.external_tasks import latest_dispatch_event

    lang = _reply_lang()
    token = (token or "").strip()
    if not token:
        return _reply_message_page(lang, "invalid_title", "invalid_body",
                                   status_code=401), None, None, "", lang
    try:
        nonce = verify_ticket(token, run_id=run_id, task_id=task_id)
    except ExternalTaskError:
        return _reply_message_page(lang, "invalid_title", "invalid_body",
                                   status_code=401), None, None, "", lang
    storage = get_storage()
    run = storage.run_get(run_id)
    if run is None:
        return _reply_message_page(lang, "invalid_title", "invalid_body",
                                   status_code=404), None, None, "", lang
    if run.status in TERMINAL_RUN_STATUSES:
        return _reply_message_page(lang, "run_over_title", "run_over_body",
                                   status_code=409), None, None, "", lang
    dispatch_ev = latest_dispatch_event(storage, run_id=run_id, task_id=task_id)
    if dispatch_ev is None:
        return _reply_message_page(lang, "stale_title", "stale_body",
                                   status_code=409), None, None, "", lang
    current_nonce = str((dispatch_ev.payload or {}).get("nonce") or "")
    if not current_nonce or nonce != current_nonce:
        return _reply_message_page(lang, "stale_title", "stale_body",
                                   status_code=409), None, None, "", lang
    payload = dispatch_ev.payload or {}
    lang = _reply_lang(
        str(payload.get("subject") or ""), str(payload.get("description") or ""),
    )
    return None, run, dispatch_ev, nonce, lang


@router.get("/reply/{run_id}/{task_id}", response_class=HTMLResponse)
async def reply_form(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    t: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render the human reply form for one dispatch attempt (ticket in ``t``)."""
    from app.services.external_attachments import (
        MAX_ATTACHMENT_BYTES,
        MAX_ATTACHMENT_COUNT,
    )
    from app.services.external_tasks import find_completion_event

    error, run, dispatch_ev, nonce, lang = _load_reply_context(
        run_id, task_id, t or "",
    )
    if error is not None:
        return error
    if find_completion_event(
        get_storage(), run_id=run_id, task_id=task_id, nonce=nonce,
    ) is not None:
        return _reply_message_page(lang, "already_title", "already_body")

    texts = _REPLY_TEXTS[lang]
    payload = dispatch_ev.payload or {}
    subject = str(payload.get("subject") or "").strip()
    description = str(payload.get("description") or "").strip()
    requirement = str(payload.get("outputRequirement") or "").strip()
    assignee = str(payload.get("assignee") or "").strip()

    esc = html_mod.escape
    info_parts: list[str] = [
        f"<p><strong>{esc(texts['task'])}</strong>: "
        f"{esc(task_id)}{' · ' + esc(subject) if subject else ''}</p>"
    ]
    if assignee:
        info_parts.append(
            f"<p class='muted'>{esc(texts['assignee'])}: {esc(assignee)}</p>"
        )
    if description:
        info_parts.append(f"<pre>{esc(description)}</pre>")
    if requirement:
        info_parts.append(
            f"<h2>{esc(texts['requirement'])}</h2><pre>{esc(requirement)}</pre>"
        )
    attach_label = texts["attachments"].format(
        max_count=MAX_ATTACHMENT_COUNT,
        max_mb=MAX_ATTACHMENT_BYTES // (1024 * 1024),
    )
    form_html = (
        "".join(info_parts)
        + f"<form method='post' enctype='multipart/form-data' "
          f"action='/api/external/reply/{esc(run_id)}/{esc(task_id)}'>"
        + f"<input type='hidden' name='t' value='{esc(t or '')}'>"
        + f"<label>{esc(texts['result'])}</label>"
        + "<div class='radio'>"
        + f"<label><input type='radio' name='status' value='success' checked>"
          f"{esc(texts['success'])}</label>"
        + f"<label><input type='radio' name='status' value='failed'>"
          f"{esc(texts['failed'])}</label>"
        + "</div>"
        + f"<label>{esc(texts['summary'])}</label>"
        + f"<textarea name='summary' placeholder='{esc(texts['summary_ph'])}'>"
          "</textarea>"
        + f"<label>{esc(attach_label)}</label>"
        + "<input type='file' name='attachments' multiple>"
        + f"<button type='submit'>{esc(texts['submit'])}</button>"
        + "</form>"
    )
    return _reply_page(texts["title"], form_html)


@router.post("/reply/{run_id}/{task_id}", response_class=HTMLResponse)
async def reply_submit(
    run_id: Annotated[str, Path()],
    task_id: Annotated[str, Path()],
    t: Annotated[str, Form()] = "",
    status_value: Annotated[str, Form(alias="status")] = "success",
    summary: Annotated[str, Form()] = "",
    attachments: Annotated[list[UploadFile] | None, File()] = None,
) -> HTMLResponse:
    """Accept the human's receipt (multipart): status + summary + attachments."""
    from app.services.external_attachments import (
        AttachmentError,
        save_attachments,
    )
    from app.services.external_tasks import find_completion_event

    error, run, _dispatch_ev, nonce, lang = _load_reply_context(run_id, task_id, t)
    if error is not None:
        return error
    storage = get_storage()
    if find_completion_event(
        storage, run_id=run_id, task_id=task_id, nonce=nonce,
    ) is not None:
        return _reply_message_page(lang, "already_title", "already_body")
    texts = _REPLY_TEXTS[lang]

    ok = (status_value or "").strip().lower() != "failed"
    incoming = [
        (f.filename, f.file)
        for f in (attachments or [])
        # Browsers submit one empty part for an untouched <input type=file>.
        if f is not None and (f.filename or "").strip()
    ]
    try:
        saved = save_attachments(
            run_id=run_id, task_id=task_id, nonce=nonce, files=incoming,
        )
    except AttachmentError as exc:
        return _reply_page(
            texts["error_title"],
            f"<p>{html_mod.escape(exc.message)}</p>",
            status_code=400,
        )
    try:
        await complete_external_task(
            storage=storage,
            run=run,
            task_id=task_id,
            nonce=nonce,
            ok=ok,
            summary=summary or "",
            source="external_reply_form",
            attachments=saved,
        )
    except ExternalTaskError as exc:
        if exc.code in ("EXTERNAL_TICKET_STALE",):
            return _reply_message_page(lang, "stale_title", "stale_body",
                                       status_code=409)
        return _reply_page(
            texts["error_title"],
            f"<p>{html_mod.escape(exc.message)}</p>",
            status_code=exc.status_code,
        )
    return _reply_message_page(lang, "done_title", "done_body")


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
