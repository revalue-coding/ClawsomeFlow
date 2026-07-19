"""External execution node support (AgentKind.external).

An external node is a Flow agent whose tasks are executed OUTSIDE the local
agent stack: a human (WebUI todo card), an arbitrary developer system
(webhook), or a remote ClawsomeFlow instance (delegate). External nodes never
spawn a local process and own no worktree/branch; they participate in the DAG
purely through ClawTeam task state. The completion contract mirrors what a
regular worker does itself:

1. ``mailbox_send(from_agent=<node>, to=<leader>, "task <id> done: <summary>")``
   so downstream dispatch prompts pick the result up via the existing
   strict ``(from_agent, task_id)`` upstream-output matching, then
2. ``task_update(<clawteam id>, status=completed, force=True)`` so ClawTeam
   unblocks the dependents and the controller mirrors the state next tick.

────────────────────────────────────────────────────────────────────────
Stable wire protocol (``schemaVersion: 1``) — treat as a public contract.
Add optional fields freely; never rename/remove required keys without a
new schemaVersion. Integrators should ignore unknown fields.
────────────────────────────────────────────────────────────────────────

**A. Webhook dispatch** — ClawsomeFlow → your endpoint (POST JSON)::

    {
      "schemaVersion": 1,
      "event": "external_task_dispatch",
      "runId", "taskId", "agentId", "channel": "webhook",
      "subject", "description", "outputRequirement",
      "upstreamOutputs": [{"taskId","subject","fromAgent","summary"}],
      "callbackUrl", "callbackToken",
      "callback": {
        "method": "POST", "url": <callbackUrl>,
        "auth": "Authorization: Bearer <callbackToken>",
        "bodyExample": {"status": "success|failed", "summary": "..."}
      }
    }

**B. Webhook / external receipt** — your system → ClawsomeFlow::

    POST /api/external/tasks/{runId}/{taskId}/complete
    Authorization: Bearer <callbackToken>
    {"status": "success"|"failed", "summary": "<text>"}

**C. Remote ClawsomeFlow delegate** — origin → peer::

    POST {peer}/api/external/delegate
    Authorization: Bearer <pair-secret>
    {
      "flowId", "runtimePrompt"?, "inputs"?,
      "callbackUrl", "callbackToken",
      "sourceRunId"?, "sourceTaskId"?
    }
    → 202 {"id": <remoteRunId>, "status", "teamName"}

**D. Delegate callback** — peer → origin (on remote run terminal)::

    POST <callbackUrl>   # usually origin's /api/external/tasks/.../complete
    Authorization: Bearer <callbackToken>
    {"status": "success"|"failed", "summary": "<leader report>"}

**Ticket scheme** (one-time signed receipt credential, stateless verify):

    ticket = "{nonce}.{HMAC-SHA256(internal_token_secret,
                                   'csflow-external:{run_id}:{task_id}:{nonce}')}"

The currently-valid nonce is whatever the latest ``external_task_dispatched``
RunEvent for that (run, task) carries — a retry re-dispatches with a fresh
nonce, invalidating older tickets. The ticket only authorises submitting THIS
task's result; it is deliberately independent from the global ``api_token``.

**Delegate callback** (this instance ran a Flow on behalf of a remote one):
``POST /api/external/delegate`` stamps ``run.inputs[EXTERNAL_CALLBACK_KEY]``;
when the run turns terminal the storage ``run_update`` hook (same single choke
point as run_notify — do not scatter) calls :func:`prepare_delegate_callback`
inside the commit and fires :func:`send_delegate_callback` on a daemon thread.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from app.config import Config, load_config
from app.logging_setup import get_logger
from app.models import (
    TERMINAL_RUN_STATUSES,
    ExternalChannel,
    FlowAgent,
    RunStatus,
    iso_utc,
)
from app.scheduler.run_metadata import (
    EXTERNAL_CALLBACK_KEY,
    EXTERNAL_CALLBACK_SENT_KEY,
)
from app.services.run_report import extract_leader_report

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.models import FlowRun, RunEvent
    from app.storage import StorageBackend

logger = get_logger("external_tasks")

# RunEvent types (UI + verification both read these — on-disk contract).
EXTERNAL_TASK_DISPATCHED_EVENT = "external_task_dispatched"
EXTERNAL_TASK_COMPLETED_EVENT = "external_task_completed"
EXTERNAL_DELEGATE_ACCEPTED_EVENT = "external_delegate_accepted"

_TICKET_CONTEXT = "csflow-external"
#: Public wire-protocol version stamped on every outbound external package.
#: Bump only when making a breaking change; keep additive changes on the same
#: version (unknown fields must be ignored by receivers).
EXTERNAL_SCHEMA_VERSION = 1
_EVENT_SCAN_LIMIT = 5000
_OUTBOUND_TIMEOUT_SEC = 15.0
_CALLBACK_ATTEMPTS = 3
_CALLBACK_RETRY_DELAY_SEC = 5.0

#: Run terminal statuses that map to a "success" delegate callback.
_DELEGATE_SUCCESS_STATUSES = frozenset({
    RunStatus.completed,
    RunStatus.completed_with_conflicts,
})


class ExternalTaskError(Exception):
    """Business error surfaced by the /api/external endpoints."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


# ──────────────────────────────────────────────────────────────────────
# Ticket mint / verify (stateless HMAC, secret = internal_token_secret)
# ──────────────────────────────────────────────────────────────────────


def _secret(config: Config | None = None) -> bytes:
    cfg = config or load_config()
    secret = getattr(cfg, "internal_token_secret", None)
    if not secret:
        # Same fallback contract as app.integrations.internal_token._secret.
        secret = f"csflow:{cfg.default_user}:fallback-secret"
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def _sign(run_id: str, task_id: str, nonce: str, config: Config | None = None) -> str:
    msg = f"{_TICKET_CONTEXT}:{run_id}:{task_id}:{nonce}".encode()
    return hmac.new(_secret(config), msg, sha256).hexdigest()


def mint_ticket(
    run_id: str, task_id: str, nonce: str, *, config: Config | None = None,
) -> str:
    """One-time receipt credential for (run, task, dispatch attempt)."""
    return f"{nonce}.{_sign(run_id, task_id, nonce, config)}"


def verify_ticket(
    token: str, *, run_id: str, task_id: str, config: Config | None = None,
) -> str:
    """Verify *token* and return its nonce. Raises :class:`ExternalTaskError`."""
    raw = (token or "").strip()
    if "." not in raw:
        raise ExternalTaskError(
            "EXTERNAL_TICKET_INVALID", "malformed ticket", status_code=401,
        )
    nonce, sig = raw.rsplit(".", 1)
    expected = _sign(run_id, task_id, nonce, config)
    if not nonce or not hmac.compare_digest(expected, sig):
        raise ExternalTaskError(
            "EXTERNAL_TICKET_INVALID", "bad ticket signature", status_code=401,
        )
    return nonce


# ──────────────────────────────────────────────────────────────────────
# Event bookkeeping helpers
# ──────────────────────────────────────────────────────────────────────


def _scan_events(storage: StorageBackend, run_id: str) -> list[RunEvent]:
    return storage.event_list(run_id=run_id, limit=_EVENT_SCAN_LIMIT)


def latest_dispatch_event(
    storage: StorageBackend, *, run_id: str, task_id: str,
) -> RunEvent | None:
    """Latest ``external_task_dispatched`` event for (run, task), if any."""
    for ev in reversed(_scan_events(storage, run_id)):
        if ev.type == EXTERNAL_TASK_DISPATCHED_EVENT and ev.task_id == task_id:
            return ev
    return None


def find_completion_event(
    storage: StorageBackend, *, run_id: str, task_id: str, nonce: str,
) -> RunEvent | None:
    """Completion event already recorded for this dispatch attempt (idempotency)."""
    for ev in reversed(_scan_events(storage, run_id)):
        if (
            ev.type == EXTERNAL_TASK_COMPLETED_EVENT
            and ev.task_id == task_id
            and (ev.payload or {}).get("nonce") == nonce
        ):
            return ev
    return None


# ──────────────────────────────────────────────────────────────────────
# Dispatch (called by the scheduler's ExternalNodeSession)
# ──────────────────────────────────────────────────────────────────────


def _callback_url(run_id: str, task_id: str, *, config: Config | None = None) -> str:
    cfg = config or load_config()
    path = f"/api/external/tasks/{run_id}/{task_id}/complete"
    base = (getattr(cfg, "external_callback_base_url", None) or "").strip()
    return f"{base.rstrip('/')}{path}" if base else path


async def dispatch_external_task(
    *,
    storage: StorageBackend,
    run_id: str,
    team_name: str,
    agent: FlowAgent,
    task_id: str,
    message: str,
    package: dict[str, Any],
) -> None:
    """Issue the receipt ticket, persist the dispatch event, notify the channel.

    Raising here fails the dispatch — the controller leaves the task pending
    and retries next tick (with a FRESH nonce, invalidating this ticket).
    """
    ext = agent.external
    if ext is None:  # defensive — the model validator guarantees this
        raise RuntimeError(f"agent {agent.id!r} has no external config")
    cfg = load_config()
    nonce = secrets.token_urlsafe(16)
    ticket = mint_ticket(run_id, task_id, nonce, config=cfg)
    callback_url = _callback_url(run_id, task_id, config=cfg)

    outbound_package: dict[str, Any] = {
        "schemaVersion": EXTERNAL_SCHEMA_VERSION,
        "event": "external_task_dispatch",
        "runId": run_id,
        "taskId": task_id,
        "agentId": agent.id,
        "channel": ext.channel.value,
        "callbackUrl": callback_url,
        "callbackToken": ticket,
        # Self-describing completion contract so an integrated system needs no
        # out-of-band documentation: POST this body back when the work is done.
        # (callbackUrl/callbackToken above are kept as flat convenience fields.)
        "callback": {
            "method": "POST",
            "url": callback_url,
            "auth": "Authorization: Bearer <callbackToken>  (or body field 'token')",
            "bodyExample": {
                "status": "success | failed",
                "summary": "<completion summary — links/refs to deliverables; "
                           "on failure: the blocking reason>",
            },
        },
        **package,
    }
    # Webhook partners run on a different host — remind them not to chase
    # foreign absolute paths or echo local paths back in the summary.
    if ext.channel == ExternalChannel.webhook:
        from app.scheduler.prompts import WEBHOOK_REMOTE_NOTES

        outbound_package["notes"] = WEBHOOK_REMOTE_NOTES

    # The dispatch event is BOTH the UI's todo-card source and the ticket
    # validity record (its nonce is the only currently-valid one), so it must
    # persist before any outbound side effect.
    from app.events import publish_run_event

    row = publish_run_event(
        storage,
        run_id=run_id,
        event_type=EXTERNAL_TASK_DISPATCHED_EVENT,
        agent_id=agent.id,
        task_id=task_id,
        payload={
            "channel": ext.channel.value,
            "nonce": nonce,
            "assignee": ext.assignee,
            "message": message,
            **package,
        },
    )
    if row is None:
        raise RuntimeError(
            "failed to persist external dispatch event (ticket would be unverifiable)"
        )

    if ext.channel == ExternalChannel.human:
        _notify_flow_channels_async(
            storage, run_id=run_id, package=outbound_package, message=message,
        )
        return
    if ext.channel == ExternalChannel.webhook:
        await _post_outbound(str(ext.endpoint_url), outbound_package)
        return
    if ext.channel == ExternalChannel.remote_csflow:
        remote_run_id = await _post_delegate(
            ext=ext, cfg=cfg, message=message, outbound_package=outbound_package,
        )
        publish_run_event(
            storage,
            run_id=run_id,
            event_type=EXTERNAL_DELEGATE_ACCEPTED_EVENT,
            agent_id=agent.id,
            task_id=task_id,
            payload={"remoteRunId": remote_run_id, "baseUrl": ext.base_url},
        )
        return
    raise RuntimeError(f"unsupported external channel: {ext.channel!r}")


async def _post_outbound(url: str, body: dict[str, Any]) -> None:
    import httpx

    async with httpx.AsyncClient(timeout=_OUTBOUND_TIMEOUT_SEC) as client:
        resp = await client.post(url, json=body)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(
            f"external endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
        )


async def _post_delegate(
    *,
    ext: Any,
    cfg: Config,
    message: str,
    outbound_package: dict[str, Any],
) -> str | None:
    """Delegate the task to a remote ClawsomeFlow; returns the remote run id."""
    import httpx

    ref = (ext.pair_token_ref or "").strip()
    secret = (getattr(cfg, "external_remote_targets", None) or {}).get(ref)
    if not secret:
        raise RuntimeError(
            f"pair_token_ref {ref!r} not found in config.external_remote_targets"
        )
    url = f"{str(ext.base_url).rstrip('/')}/api/external/delegate"
    body = {
        "flowId": ext.flow_id,
        # The rendered task text drives the remote Flow as its runtime prompt.
        "runtimePrompt": message,
        "callbackUrl": outbound_package["callbackUrl"],
        "callbackToken": outbound_package["callbackToken"],
        "sourceRunId": outbound_package["runId"],
        "sourceTaskId": outbound_package["taskId"],
    }
    # Static param-field values for the remote Flow (its run-input fields),
    # configured on the external node. The remote delegate endpoint stores
    # them as the run's inputs — exactly like a local "参数字段" form fill.
    if getattr(ext, "inputs", None):
        body["inputs"] = dict(ext.inputs)
    async with httpx.AsyncClient(timeout=_OUTBOUND_TIMEOUT_SEC) as client:
        resp = await client.post(
            url, json=body, headers={"Authorization": f"Bearer {secret}"},
        )
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(
            f"remote delegate returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    try:
        return str(resp.json().get("id") or "") or None
    except Exception:
        return None


#: Cap the task-briefing block in the human-dispatch notification. Same order
#: of magnitude as run_notify._CONTENT_MAX_CHARS; chat platforms truncate
#: further per their own limits.
_NOTIFY_CONTENT_MAX_CHARS = 3000


def build_external_dispatch_notification(
    run: FlowRun,
    *,
    package: dict[str, Any],
    message: str,
    flow_name: str | None = None,
) -> dict[str, Any]:
    """Webhook payload announcing "an external task was dispatched".

    This is NOT a run-terminal notification — the ``run_external_task`` event
    tells the recipient (typically the assignee's chat) that a task now waits
    on an external executor. It carries the task identity (subject/channel/
    assignee) plus the full task sheet (description, upstream inputs, output
    requirement, result-submission how-to) as ``content``, and where to act
    (``runUrl`` — the local Run page for the human channel).
    """
    cfg = load_config()
    base = (getattr(cfg, "external_callback_base_url", None) or "").strip()
    if not base:
        base = f"http://127.0.0.1:{getattr(cfg, 'csflow_port', 17017)}"
    status = run.status.value if hasattr(run.status, "value") else str(run.status)
    return {
        "event": "run_external_task",
        "runId": run.id,
        "flowId": run.flow_id,
        "flowName": flow_name,
        "teamName": run.team_name,
        "status": status,
        "taskId": package.get("taskId") or "",
        "taskSubject": package.get("subject") or "",
        "channel": package.get("channel") or "",
        "assignee": package.get("assignee") or "",
        "runUrl": f"{base.rstrip('/')}/runs/{run.id}",
        # Full task sheet: flow goal, upstream inputs, task description
        # (incl. output requirement), result-submission instructions.
        "content": (message or "").strip()[:_NOTIFY_CONTENT_MAX_CHARS],
    }


def _notify_flow_channels_async(
    storage: StorageBackend,
    *,
    run_id: str,
    package: dict[str, Any],
    message: str,
) -> None:
    """Best-effort: push the human todo card to the Flow's notify webhooks.

    Reuses the per-Flow ``csflow.notify_webhooks`` channels so a Feishu/
    Telegram/... bot pings the human that a task is waiting. Never raises,
    never blocks the scheduler (daemon thread)."""
    try:
        from app.services.run_notify import flow_channels_for_run, post_webhook

        run = storage.run_get(run_id)
        if run is None:
            return
        channels = flow_channels_for_run(run)
        if not channels:
            return
        flow_name: str | None = None
        try:
            flow = storage.flow_get(run.flow_id)
            if flow is not None:
                flow_name = flow.name
        except Exception:  # pragma: no cover — enrichment only
            pass
        payload = build_external_dispatch_notification(
            run, package=package, message=message, flow_name=flow_name,
        )

        def _send() -> None:
            for ch in channels:
                url = str(ch.get("url") or "").strip()
                if not url:
                    continue
                ok, detail = post_webhook(url, payload, fmt=ch.get("format"))
                (logger.info if ok else logger.warning)(
                    "external_task_notify_sent" if ok else "external_task_notify_failed",
                    run_id=run_id, detail=detail,
                )

        threading.Thread(
            target=_send, name="csflow-external-notify", daemon=True,
        ).start()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("external_task_notify_skipped", error=str(exc))


# ──────────────────────────────────────────────────────────────────────
# Completion (receipt) — shared by /api/external and the WebUI human path
# ──────────────────────────────────────────────────────────────────────


async def complete_external_task(
    *,
    storage: StorageBackend,
    run: FlowRun,
    task_id: str,
    nonce: str,
    ok: bool,
    summary: str,
    source: str,
) -> dict[str, Any]:
    """Record an external task result and push it into ClawTeam.

    Success path mirrors what a regular worker does itself (inbox send with
    the strict ``task <id> done:`` prefix, then ``task_update completed``).
    Failure path sends the legacy ``FAILED:<task_id>:<reason>`` leader-inbox
    signal so the existing failure detector applies the agent's ``on_failure``
    policy (retry re-dispatches with a fresh nonce). Idempotent per nonce.
    """
    dispatch_ev = latest_dispatch_event(storage, run_id=run.id, task_id=task_id)
    if dispatch_ev is None:
        raise ExternalTaskError(
            "EXTERNAL_TASK_NOT_DISPATCHED",
            f"task {task_id!r} has no outstanding external dispatch",
            status_code=409,
        )
    payload = dispatch_ev.payload or {}
    current_nonce = str(payload.get("nonce") or "")
    if not current_nonce or nonce != current_nonce:
        raise ExternalTaskError(
            "EXTERNAL_TICKET_STALE",
            "ticket does not match the latest dispatch attempt "
            "(the task was re-dispatched; use the newest ticket)",
            status_code=409,
        )
    already = find_completion_event(
        storage, run_id=run.id, task_id=task_id, nonce=nonce,
    )
    if already is not None:
        return {"status": "already_recorded", "taskId": task_id}

    agent_id = dispatch_ev.agent_id or str(payload.get("agentId") or "")
    leader_id = str(payload.get("leaderAgentId") or "")
    ct_task_id = str(payload.get("clawteamTaskId") or "") or task_id
    summary_text = (summary or "").strip()

    from app.integrations.clawteam_mcp import get_mcp_client

    mcp = await get_mcp_client(user=run.user)
    if ok:
        if not summary_text:
            summary_text = "(external task completed without a summary)"
        if leader_id and agent_id != leader_id:
            # Same message shape a worker sends itself — downstream prompts
            # match it strictly by (from_agent, task_id).
            await mcp.mailbox_send(
                team_name=run.team_name,
                from_agent=agent_id,
                to=leader_id,
                content=f"task {task_id} done: {summary_text}",
            )
        await mcp.task_update(
            team_name=run.team_name,
            task_id=ct_task_id,
            status="completed",
            caller=agent_id or "csflow-external",
            force=True,
        )
    else:
        if not summary_text:
            summary_text = "external executor reported failure without a reason"
        if leader_id:
            # Legacy failure signal — picked up by failure.detect_failures
            # (leader_inbox_failed) and routed through on_failure policy.
            await mcp.mailbox_send(
                team_name=run.team_name,
                from_agent=agent_id,
                to=leader_id,
                content=f"FAILED: {task_id}: {summary_text}",
            )

    from app.events import publish_run_event

    publish_run_event(
        storage,
        run_id=run.id,
        event_type=EXTERNAL_TASK_COMPLETED_EVENT,
        agent_id=agent_id,
        task_id=task_id,
        payload={
            "nonce": nonce,
            "ok": ok,
            "summary": summary_text[:4000],
            "source": source,
        },
    )
    return {"status": "recorded", "taskId": task_id, "ok": ok}


# ──────────────────────────────────────────────────────────────────────
# Delegate callback (this instance executed a Flow FOR a remote instance)
# ──────────────────────────────────────────────────────────────────────


def prepare_delegate_callback(run: FlowRun) -> dict[str, Any] | None:
    """Called inside the storage ``run_update`` commit (single choke point).

    When *run* carries the delegate marker and just reached a terminal
    status, stamp the sent-dedupe marker into ``run.inputs`` (new dict, same
    commit) and return the prepared callback. Never raises.
    """
    try:
        if run.status not in TERMINAL_RUN_STATUSES:
            return None
        inputs = dict(run.inputs or {})
        raw = inputs.get(EXTERNAL_CALLBACK_KEY)
        if not raw or EXTERNAL_CALLBACK_SENT_KEY in inputs:
            return None
        info = json.loads(raw) if isinstance(raw, str) else dict(raw)
        url = str(info.get("url") or "").strip()
        token = str(info.get("token") or "").strip()
        if not url or not token:
            return None
        inputs[EXTERNAL_CALLBACK_SENT_KEY] = iso_utc(datetime.now(timezone.utc))
        run.inputs = inputs
        status = run.status.value if hasattr(run.status, "value") else str(run.status)
        return {
            "url": url,
            "token": token,
            "run_id": run.id,
            "ok": run.status in _DELEGATE_SUCCESS_STATUSES,
            "run_status": status,
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("delegate_callback_prepare_failed", error=str(exc))
        return None


def send_delegate_callback(prepared: dict[str, Any]) -> threading.Thread:
    """POST the delegated run's result back to the origin instance.

    Runs on a daemon thread with a few retries; the origin's completion
    endpoint is idempotent per ticket so duplicates are harmless."""

    def _send() -> None:
        summary = ""
        try:
            from app.storage import get_storage

            events = get_storage().event_list(
                run_id=prepared["run_id"], limit=500,
            )
            summary = extract_leader_report(events) or ""
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("delegate_callback_report_failed", error=str(exc))
        if not summary:
            summary = f"delegated run finished with status {prepared['run_status']}"
        body = {
            "schemaVersion": EXTERNAL_SCHEMA_VERSION,
            "token": prepared["token"],
            "status": "success" if prepared["ok"] else "failed",
            "summary": summary,
            "delegatedRunId": prepared["run_id"],
            "delegatedRunStatus": prepared["run_status"],
        }
        import httpx

        for attempt in range(1, _CALLBACK_ATTEMPTS + 1):
            try:
                resp = httpx.post(
                    prepared["url"], json=body, timeout=_OUTBOUND_TIMEOUT_SEC,
                )
                if 200 <= resp.status_code < 300:
                    logger.info(
                        "delegate_callback_sent",
                        run_id=prepared["run_id"], attempt=attempt,
                    )
                    return
                detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:
                detail = str(exc)
            logger.warning(
                "delegate_callback_attempt_failed",
                run_id=prepared["run_id"], attempt=attempt, detail=detail,
            )
            if attempt < _CALLBACK_ATTEMPTS:
                time.sleep(_CALLBACK_RETRY_DELAY_SEC)

    thread = threading.Thread(
        target=_send, name="csflow-delegate-callback", daemon=True,
    )
    thread.start()
    return thread


__all__ = [
    "EXTERNAL_DELEGATE_ACCEPTED_EVENT",
    "EXTERNAL_TASK_COMPLETED_EVENT",
    "EXTERNAL_TASK_DISPATCHED_EVENT",
    "ExternalTaskError",
    "complete_external_task",
    "dispatch_external_task",
    "find_completion_event",
    "latest_dispatch_event",
    "mint_ticket",
    "prepare_delegate_callback",
    "send_delegate_callback",
    "verify_ticket",
]
