"""Best-effort webhook notification when a Run reaches a terminal state or
pauses at a manual checkpoint.

**Per-Flow configuration.** Each Flow carries its own list of webhook channels
in ``spec.variables[FLOW_NOTIFY_WEBHOOKS_VAR]`` (a JSON string, same round-trip-
safe ``variables`` pattern as ``csflow.easy_mode`` / run-input fields). A Flow
with no channels is a full no-op — zero-regression opt-in. Scheduled runs read
the Flow's channels at notify time just like manual runs, so a timed trigger
honours whatever the Flow's owner configured.

Wiring: the storage layer's ``run_update`` (the single choke point every
status flip goes through — finalize, complaint end, abort, review merge,
uncaught-exception fallback, …) loads the Flow's channels once (via
:func:`flow_channels_for_run`) and calls :func:`prepare_terminal_notification`
and :func:`prepare_checkpoint_notification` *inside* its DB transaction. The
helpers decide whether to notify (terminal: dedupe marker stamped into
``run.inputs`` in the same commit; checkpoint: keyed on the status
*transition*, so re-entering a waiting state after leaving it notifies
again while repeated persists in the same state stay silent); after the
commit the storage layer fires :func:`send_run_notification` on a daemon
thread so no scheduler/API path ever blocks on the webhook. One prepared
notification fans out to **every** configured channel.

Terminal statuses (``event: "run_terminal"``): completed /
completed_with_conflicts / complaint_failed / failed / aborted. ``orphaned``
is deliberately excluded — the startup orphan sweep may reconcile many stale
rows at once and a burst of "your run died" webhooks on every service restart
would be noise, not signal.

Manual-checkpoint statuses (``event: "run_checkpoint"``): the run paused and
needs user action — awaiting_user_checkpoint (mid-DAG manual checkpoint) /
awaiting_user_review (merge review) / awaiting_user_complaint (complaint
feedback window).

Message formats: users can paste a chat-platform bot webhook URL directly —
no relay service needed. Each channel carries its own ``format`` (``None``/
"auto" = detect by URL host, unknown hosts fall back to the raw ``generic``
JSON). See :data:`WEBHOOK_FORMATS` / :func:`detect_webhook_format` /
:func:`build_webhook_request`.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from app.logging_setup import get_logger
from app.models import TERMINAL_RUN_STATUSES, RunStatus, iso_utc

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.models import FlowRun

logger = get_logger("run_notify")

#: ``spec.variables`` key holding the Flow's webhook channels (JSON array of
#: ``{"url": str, "format": str|null}``). Round-trip-safe like the other
#: ``csflow.*`` variables. Absent/empty → the Flow sends no notifications.
FLOW_NOTIFY_WEBHOOKS_VAR = "csflow.notify_webhooks"

#: ``run.inputs`` key stamping when (ISO-8601) the terminal webhook fired.
#: Doubles as the dedupe marker: a run is notified at most once.
NOTIFIED_MARKER_KEY = "csflow.terminal_webhook_notified_at"

_NOTIFY_STATUSES: frozenset[RunStatus] = TERMINAL_RUN_STATUSES - {RunStatus.orphaned}

#: Waiting-for-user states that fire the ``run_checkpoint`` webhook on entry.
CHECKPOINT_NOTIFY_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.awaiting_user_checkpoint,
    RunStatus.awaiting_user_review,
    RunStatus.awaiting_user_complaint,
})

_WEBHOOK_TIMEOUT_SEC = 10.0

#: Explicit outgoing-body formats. "generic" = the raw ClawsomeFlow JSON
#: (historical behavior, for self-built receivers). Everything else renders a
#: human-readable text message in the target platform's bot-webhook schema.
#: NOTE "slack" is also the right pick for Mattermost / Rocket.Chat (both are
#: Slack-webhook compatible) and "gotify" has no auto-detection (self-hosted).
WEBHOOK_FORMATS: tuple[str, ...] = (
    "generic",
    "feishu",      # 飞书 / Lark
    "dingtalk",    # 钉钉
    "wecom",       # 企业微信
    "slack",
    "discord",
    "teams",       # Microsoft Teams (Workflows / legacy connector)
    "googlechat",
    "telegram",
    "ntfy",
    "bark",
    "serverchan",  # Server酱 Turbo
    "gotify",
)

#: Discord caps message ``content`` at 2000 chars; keep margin for safety.
_DISCORD_MAX_CONTENT = 1900


def detect_webhook_format(url: str) -> str:
    """Map a webhook URL to a platform format by its (unambiguous) host.

    Only first-party bot-webhook domains are recognized; anything else —
    including self-hosted Mattermost/Gotify/ntfy instances — returns
    ``generic`` so custom receivers keep getting the raw JSON unchanged.
    """
    try:
        parts = urlsplit(url)
    except Exception:
        return "generic"
    host = (parts.hostname or "").lower()
    path = (parts.path or "").lower()
    if host in ("open.feishu.cn", "open.larksuite.com"):
        return "feishu"
    if host == "oapi.dingtalk.com":
        return "dingtalk"
    if host == "qyapi.weixin.qq.com":
        return "wecom"
    if host == "hooks.slack.com":
        return "slack"
    if (
        host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com")
        and "/api/webhooks/" in path
    ):
        return "discord"
    # Legacy O365 connectors (*.webhook.office.com) and their Power Automate
    # Workflows replacement (*.logic.azure.com) both accept the same
    # message+Adaptive Card wrapper we send for "teams".
    if host.endswith(".webhook.office.com") or host.endswith(".logic.azure.com"):
        return "teams"
    if host == "chat.googleapis.com":
        return "googlechat"
    if host == "api.telegram.org" and "/sendmessage" in path:
        return "telegram"
    if host == "ntfy.sh":
        return "ntfy"
    if host == "api.day.app":
        return "bark"
    if host == "sctapi.ftqq.com":
        return "serverchan"
    return "generic"


def resolve_webhook_format(url: str, configured: str | None) -> str:
    """Effective format for *url*: explicit config wins, else auto-detect."""
    fmt = (configured or "").strip().lower()
    if fmt in WEBHOOK_FORMATS:
        return fmt
    return detect_webhook_format(url)


# ── per-Flow channel storage (spec.variables) ───────────────────────


def parse_flow_channels(variables: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parse the Flow's webhook channels out of ``spec.variables``.

    Returns a list of ``{"url": str, "format": str|None}`` (``format`` None =
    auto-detect). Malformed / missing → ``[]`` (never raises). Duplicate URLs
    collapse to the first occurrence so a copy-paste slip can't double-send.
    """
    raw = (variables or {}).get(FLOW_NOTIFY_WEBHOOKS_VAR)
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        fmt = str(item.get("format") or "").strip().lower()
        out.append({"url": url, "format": fmt or None})
    return out


def serialize_flow_channels(channels: list[dict[str, Any]]) -> str:
    """Serialize *channels* to the compact JSON string stored in variables."""
    payload = [
        {"url": c["url"], "format": (c.get("format") or None)}
        for c in channels
        if str(c.get("url") or "").strip()
    ]
    return json.dumps(payload, ensure_ascii=False)


def flow_channels_for_run(run: FlowRun) -> list[dict[str, Any]]:
    """Load *run*'s Flow and return its configured webhook channels.

    Best-effort: any lookup/parse failure yields ``[]`` (no notification), so
    the webhook can never break ``run_update``. Reads the CURRENT Flow (not the
    run's start-time snapshot) so live edits to the config take effect.
    """
    try:
        from app.storage import get_storage

        flow = get_storage().flow_get(run.flow_id)
        if flow is None:
            return []
        return parse_flow_channels((flow.spec or {}).get("variables"))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("run_notify_channels_load_failed", error=str(exc))
        return []


def _headline(payload: dict[str, Any]) -> str:
    event = payload.get("event")
    status = str(payload.get("status") or "")
    if event == "run_checkpoint":
        return "⏸️ ClawsomeFlow run paused — action required"
    if event == "run_terminal_test":
        return "🔔 ClawsomeFlow webhook test"
    icon = {
        "completed": "✅",
        "completed_with_conflicts": "⚠️",
        "aborted": "⏹️",
    }.get(status, "❌")
    return f"{icon} ClawsomeFlow run finished"


def _short_title(payload: dict[str, Any]) -> str:
    """One-line title for platforms with a separate title field.

    Deliberately ASCII-only: ntfy carries it in an HTTP header, and header
    values must stay latin-1 safe.
    """
    event = payload.get("event")
    status = payload.get("status")
    if event == "run_checkpoint":
        return f"ClawsomeFlow: action required ({status})"
    if event == "run_terminal_test":
        return "ClawsomeFlow: webhook test"
    return f"ClawsomeFlow: run {status}"


#: Cap the enriched content block so a huge leader report can't bloat the
#: webhook body (the per-platform message is truncated further where needed).
_CONTENT_MAX_CHARS = 3000


def render_message_text(payload: dict[str, Any]) -> str:
    """Human-readable plain-text message shared by all chat formats."""
    lines = [_headline(payload)]

    def add(label: str, value: Any) -> None:
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    add("Flow", payload.get("flowName") or payload.get("flowId"))
    add("Run", payload.get("runId"))
    add("Team", payload.get("teamName"))
    add("Status", payload.get("status"))
    if payload.get("isScheduled"):
        lines.append("Trigger: scheduled")
    add("Started", payload.get("startedAt"))
    add("Finished", payload.get("finishedAt"))
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        label = (
            "Checkpoint output"
            if payload.get("event") == "run_checkpoint"
            else "Leader report"
        )
        lines.append("")
        lines.append(f"── {label} ──")
        lines.append(content.strip())
    return "\n".join(lines)


def build_webhook_request(
    url: str, payload: dict[str, Any], fmt: str,
) -> dict[str, Any]:
    """Render the outgoing HTTP request for *fmt*.

    Returns ``{"url", "json"}`` for JSON bodies or ``{"url", "content",
    "headers"}`` for raw-body platforms (ntfy). ``generic`` passes *payload*
    through untouched (the historical contract for custom receivers).
    """
    if fmt == "generic":
        return {"url": url, "json": payload}
    text = render_message_text(payload)
    title = _short_title(payload)
    if fmt == "feishu":
        return {"url": url, "json": {"msg_type": "text", "content": {"text": text}}}
    if fmt in ("dingtalk", "wecom"):
        return {"url": url, "json": {"msgtype": "text", "text": {"content": text}}}
    if fmt in ("slack", "googlechat"):
        return {"url": url, "json": {"text": text}}
    if fmt == "discord":
        return {"url": url, "json": {"content": text[:_DISCORD_MAX_CONTENT]}}
    if fmt == "teams":
        return {
            "url": url,
            "json": {
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                    },
                }],
            },
        }
    if fmt == "telegram":
        # Bot API needs a chat_id; users append it to the URL query
        # (…/bot<token>/sendMessage?chat_id=123). Mirror it into the JSON body
        # (query params also work server-side, but being explicit is safer).
        body: dict[str, Any] = {"text": text}
        try:
            chat_ids = parse_qs(urlsplit(url).query).get("chat_id") or []
            if chat_ids:
                body["chat_id"] = chat_ids[0]
        except Exception:
            pass
        return {"url": url, "json": body}
    if fmt == "ntfy":
        return {
            "url": url,
            "content": text.encode("utf-8"),
            "headers": {"Title": title},
        }
    if fmt == "bark":
        return {"url": url, "json": {"title": title, "body": text}}
    if fmt == "serverchan":
        return {"url": url, "json": {"title": title, "desp": text}}
    if fmt == "gotify":
        return {"url": url, "json": {"title": title, "message": text, "priority": 5}}
    return {"url": url, "json": payload}  # unknown value — behave like generic


def _platform_error(fmt: str, resp: Any) -> str | None:
    """Some platforms report failures inside an HTTP-200 JSON body (Feishu,
    DingTalk, WeCom, ServerChan). Surface those so the "send test" button
    gives a real diagnosis instead of a false success. Never raises."""
    try:
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        if fmt == "feishu":
            code = data.get("code", data.get("StatusCode"))
            if code not in (None, 0):
                return f"feishu code {code}: {data.get('msg') or ''}".strip()
        elif fmt in ("dingtalk", "wecom"):
            code = data.get("errcode")
            if code not in (None, 0):
                return f"{fmt} errcode {code}: {data.get('errmsg') or ''}".strip()
        elif fmt == "serverchan":
            code = data.get("code")
            if code not in (None, 0):
                return f"serverchan code {code}: {data.get('message') or ''}".strip()
        elif fmt == "telegram" and data.get("ok") is False:
            return f"telegram: {data.get('description') or 'not ok'}"
    except Exception:  # pragma: no cover — defensive
        return None
    return None


def prepare_terminal_notification(
    run: FlowRun, *, channels: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Decide whether *run* (about to be persisted) should fire the webhook.

    Returns ``{"channels": [...], "payload": ...}`` when a notification is due
    (stamping :data:`NOTIFIED_MARKER_KEY` into ``run.inputs`` as a **new** dict
    so SQLAlchemy JSON change detection sees it) or ``None`` otherwise. The
    single marker means "this run's terminal notification fired" for ALL
    channels at once. Never raises — a failure just skips the notification.
    """
    try:
        if run.status not in _NOTIFY_STATUSES or not channels:
            return None
        inputs = dict(run.inputs or {})
        if NOTIFIED_MARKER_KEY in inputs:
            return None
        inputs[NOTIFIED_MARKER_KEY] = iso_utc(datetime.now(timezone.utc))
        run.inputs = inputs
        return {
            "channels": channels,
            "payload": build_run_terminal_payload(run),
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("run_notify_prepare_failed", error=str(exc))
        return None


def prepare_checkpoint_notification(
    run: FlowRun, *, old_status: RunStatus | None, channels: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Decide whether *run* just ENTERED a waiting-for-user state.

    Called from the storage layer's ``run_update`` with the status currently
    persisted in the DB (*old_status*). Fires only on the transition into a
    :data:`CHECKPOINT_NOTIFY_STATUSES` state — repeated persists while waiting
    (pending-merge updates, checkpoint output refreshes, …) stay silent, and a
    run that resumes and pauses again notifies once per pause. No dedupe
    marker is needed (the transition itself is the dedupe). Never raises.
    """
    try:
        if run.status not in CHECKPOINT_NOTIFY_STATUSES or old_status == run.status:
            return None
        if not channels:
            return None
        return {
            "channels": channels,
            "payload": _build_run_payload(run, event="run_checkpoint"),
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("run_notify_prepare_checkpoint_failed", error=str(exc))
        return None


def build_run_terminal_payload(run: FlowRun) -> dict[str, Any]:
    """Build the terminal webhook JSON body (camelCase, matching the API)."""
    return _build_run_payload(run, event="run_terminal")


def _build_run_payload(run: FlowRun, *, event: str) -> dict[str, Any]:
    status = run.status.value if hasattr(run.status, "value") else str(run.status)
    return {
        "event": event,
        "runId": run.id,
        "flowId": run.flow_id,
        "flowName": None,  # filled lazily by the sender thread (best-effort)
        "teamName": run.team_name,
        "status": status,
        "isScheduled": bool(run.is_scheduled),
        "startedAt": iso_utc(run.started_at) if run.started_at else None,
        "finishedAt": iso_utc(run.finished_at) if run.finished_at else None,
    }


def post_webhook(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = _WEBHOOK_TIMEOUT_SEC,
    fmt: str | None = None,
) -> tuple[bool, str]:
    """Synchronously POST *payload* to *url*. Returns ``(success, detail)``.

    Shared by the background sender and the "send test" API endpoint. *fmt*
    is the configured format (``None`` = auto-detect by URL host); the payload
    is rendered into the platform's schema before sending.
    """
    import httpx

    resolved = resolve_webhook_format(url, fmt)
    req = build_webhook_request(url, payload, resolved)
    try:
        if "content" in req:
            resp = httpx.post(
                req["url"], content=req["content"],
                headers=req.get("headers"), timeout=timeout,
            )
        else:
            resp = httpx.post(req["url"], json=req["json"], timeout=timeout)
    except Exception as exc:
        return False, str(exc)
    if 200 <= resp.status_code < 300:
        platform_err = _platform_error(resolved, resp)
        if platform_err:
            return False, f"HTTP {resp.status_code}, {platform_err}"
        return True, f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def _extract_leader_report(events: list[Any]) -> str | None:
    """Leader's final report text from the ``run_terminal_execution_log``
    event's ``worker_report_history`` (mirrors the WebUI's extractLeaderReply).

    Scans newest event first; within it, the latest report whose summary
    starts with the ``leader final reply:`` marker wins, else the last
    non-empty summary. Best-effort — returns None on any shape mismatch.
    """
    needle = "leader final reply:"
    for ev in reversed(events):
        if getattr(ev, "type", None) != "run_terminal_execution_log":
            continue
        history = (getattr(ev, "payload", None) or {}).get("worker_report_history")
        if not isinstance(history, list):
            continue
        fallback: str | None = None
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            raw = str(item.get("summary") or "").strip()
            if not raw:
                continue
            if raw.lower().startswith(needle):
                stripped = raw[len(needle):].strip()
                return stripped or raw
            if fallback is None:
                fallback = raw
        if fallback:
            return fallback
    return None


def _extract_checkpoint_output(events: list[Any]) -> str | None:
    """Pending checkpoint output(s) from the latest checkpoint event's items
    (``task_checkpoint_waiting`` / ``task_checkpoint_updated``). Renders one
    ``<subject>: <summary>`` block per item. Best-effort — None if absent."""
    wanted = {"task_checkpoint_waiting", "task_checkpoint_updated"}
    for ev in reversed(events):
        if getattr(ev, "type", None) not in wanted:
            continue
        items = (getattr(ev, "payload", None) or {}).get("items")
        if not isinstance(items, list) or not items:
            continue
        blocks: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            if not summary:
                continue
            subject = str(item.get("subject") or item.get("task_id") or "").strip()
            blocks.append(f"{subject}: {summary}" if subject else summary)
        if blocks:
            return "\n\n".join(blocks)
    return None


def enrich_run_content(payload: dict[str, Any]) -> None:
    """Best-effort: attach the leader report / checkpoint output to *payload*
    in-place as ``content``. Runs on the sender thread (never the DB
    transaction / scheduler loop) and never raises — a lookup failure just
    leaves ``content`` unset.
    """
    run_id = payload.get("runId")
    if not run_id or payload.get("event") == "run_terminal_test":
        return
    try:
        from app.storage import get_storage

        events = get_storage().event_list(run_id=str(run_id), limit=500)
        if payload.get("event") == "run_checkpoint" and (
            payload.get("status") == RunStatus.awaiting_user_checkpoint.value
        ):
            content = _extract_checkpoint_output(events)
        else:
            # terminal, or review/complaint checkpoint → leader's final report.
            content = _extract_leader_report(events)
        if content:
            payload["content"] = content[:_CONTENT_MAX_CHARS]
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("run_notify_content_enrich_failed", error=str(exc))


def send_run_notification(prepared: dict[str, Any]) -> threading.Thread:
    """Fire a prepared webhook (terminal or checkpoint) on a daemon thread.

    The payload is enriched ONCE (flow name + leader report / checkpoint
    output), then POSTed to every configured channel in turn using each
    channel's own format. Returns the thread so tests can join it.
    """

    def _send() -> None:
        payload = dict(prepared["payload"])
        if payload.get("flowName") is None and payload.get("flowId"):
            # Best-effort flow-name enrichment; a lookup failure keeps null.
            try:
                from app.storage import get_storage

                flow = get_storage().flow_get(str(payload["flowId"]))
                if flow is not None:
                    payload["flowName"] = flow.name
            except Exception:
                pass
        # Best-effort content enrichment (leader report / checkpoint output).
        enrich_run_content(payload)
        event = payload.get("event") or "run_terminal"
        for channel in prepared.get("channels") or []:
            url = str(channel.get("url") or "").strip()
            if not url:
                continue
            ok, detail = post_webhook(url, payload, fmt=channel.get("format"))
            log = logger.info if ok else logger.warning
            log(
                f"{event}_webhook_sent" if ok else f"{event}_webhook_failed",
                run_id=payload.get("runId"),
                status=payload.get("status"),
                detail=detail,
            )

    thread = threading.Thread(target=_send, name="csflow-run-notify", daemon=True)
    thread.start()
    return thread


__all__ = [
    "CHECKPOINT_NOTIFY_STATUSES",
    "FLOW_NOTIFY_WEBHOOKS_VAR",
    "NOTIFIED_MARKER_KEY",
    "WEBHOOK_FORMATS",
    "build_run_terminal_payload",
    "build_webhook_request",
    "detect_webhook_format",
    "enrich_run_content",
    "flow_channels_for_run",
    "parse_flow_channels",
    "post_webhook",
    "prepare_checkpoint_notification",
    "prepare_terminal_notification",
    "render_message_text",
    "resolve_webhook_format",
    "serialize_flow_channels",
    "send_run_notification",
]
