"""Best-effort webhook notification when a Run reaches a terminal state or
pauses at a manual checkpoint.

Opt-in via ``Config.notify_webhook_url`` (default ``None`` → the whole module
is a no-op, so tests and existing deployments are unaffected — same
zero-regression pattern as the ``api_token`` guard).

Wiring: the storage layer's ``run_update`` (the single choke point every
status flip goes through — finalize, complaint end, abort, review merge,
uncaught-exception fallback, …) calls :func:`prepare_terminal_notification`
and :func:`prepare_checkpoint_notification` *inside* its DB transaction. The
helpers decide whether to notify (terminal: dedupe marker stamped into
``run.inputs`` in the same commit; checkpoint: keyed on the status
*transition*, so re-entering a waiting state after leaving it notifies
again while repeated persists in the same state stay silent); after the
commit the storage layer fires :func:`send_run_notification` on a daemon
thread so no scheduler/API path ever blocks on the webhook.

Terminal statuses (``event: "run_terminal"``): completed /
completed_with_conflicts / complaint_failed / failed / aborted. ``orphaned``
is deliberately excluded — the startup orphan sweep may reconcile many stale
rows at once and a burst of "your run died" webhooks on every service restart
would be noise, not signal.

Manual-checkpoint statuses (``event: "run_checkpoint"``): the run paused and
needs user action — awaiting_user_checkpoint (mid-DAG manual checkpoint) /
awaiting_user_review (merge review) / awaiting_user_complaint (complaint
feedback window).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.models import TERMINAL_RUN_STATUSES, RunStatus, iso_utc

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.models import FlowRun

logger = get_logger("run_notify")

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


def prepare_terminal_notification(run: FlowRun) -> dict[str, Any] | None:
    """Decide whether *run* (about to be persisted) should fire the webhook.

    Returns ``{"url": ..., "payload": ...}`` when a notification is due and
    stamps :data:`NOTIFIED_MARKER_KEY` into ``run.inputs`` (a **new** dict so
    SQLAlchemy JSON change detection sees it); returns ``None`` otherwise.
    Never raises — a config/load failure just skips the notification.
    """
    try:
        if run.status not in _NOTIFY_STATUSES:
            return None
        inputs = dict(run.inputs or {})
        if NOTIFIED_MARKER_KEY in inputs:
            return None
        from app.config import load_config

        url = (load_config().notify_webhook_url or "").strip()
        if not url:
            return None
        inputs[NOTIFIED_MARKER_KEY] = iso_utc(datetime.now(timezone.utc))
        run.inputs = inputs
        return {"url": url, "payload": build_run_terminal_payload(run)}
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("run_notify_prepare_failed", error=str(exc))
        return None


def prepare_checkpoint_notification(
    run: FlowRun, *, old_status: RunStatus | None,
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
        from app.config import load_config

        url = (load_config().notify_webhook_url or "").strip()
        if not url:
            return None
        return {"url": url, "payload": _build_run_payload(run, event="run_checkpoint")}
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
    url: str, payload: dict[str, Any], *, timeout: float = _WEBHOOK_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """Synchronously POST *payload* to *url*. Returns ``(success, detail)``.

    Shared by the background sender and the "send test" API endpoint.
    """
    import httpx

    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except Exception as exc:
        return False, str(exc)
    if 200 <= resp.status_code < 300:
        return True, f"HTTP {resp.status_code}"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def send_run_notification(prepared: dict[str, Any]) -> threading.Thread:
    """Fire a prepared webhook (terminal or checkpoint) on a daemon thread.

    Returns the thread so tests can join it deterministically.
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
        ok, detail = post_webhook(prepared["url"], payload)
        event = payload.get("event") or "run_terminal"
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
    "NOTIFIED_MARKER_KEY",
    "build_run_terminal_payload",
    "post_webhook",
    "prepare_checkpoint_notification",
    "prepare_terminal_notification",
    "send_run_notification",
]
