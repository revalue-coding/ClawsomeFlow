"""WebSocket route — live Run event stream (per API.md "WebSocket").

Per-Run channel: ``/ws/{run_id}``. Once accepted, the server pushes one
JSON frame per :class:`RunEvent` (matching the shape of
``GET /api/runs/{id}/events``'s items, plus an optional ``dropped: True``
flag if the in-process broker had to discard the queue head — clients
should then call the events endpoint with ``sinceId`` to backfill).

Heartbeats:
* Server doesn't actively ping; clients send ``{"type":"ping"}`` every
  30s and we reply ``{"type":"pong"}`` (matches plan §11.5 / API.md).
* Inactivity is detected client-side via the missing pong; on
  disconnection clients reconnect + replay events via the REST endpoint.

Authorisation:
* Local mode resolves the user from ``$CSFLOW_USER`` env / config default
  (same as :mod:`app.api._auth`); we still enforce ``run.user == user``.
* Server mode (Phase 9) will accept a ``token`` query string carrying the
  OAuth bearer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Path,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from starlette.websockets import WebSocketState

from app.api._auth import resolve_current_user
from app.api.errors import ApiError
from app.events import get_event_broadcaster
from app.logging_setup import get_logger
from app.models import iso_utc
from app.operations import get_op_registry, op_channel
from app.storage import StorageBackend, get_storage

logger = get_logger("api.ws")

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────


def _storage_dep() -> StorageBackend:
    return get_storage()


@router.websocket("/ws/{run_id}")
async def run_event_stream(
    websocket: WebSocket,
    run_id: Annotated[str, Path()],
    storage: Annotated[StorageBackend, Depends(_storage_dep)],
    since_id: Annotated[int | None, Query(alias="sinceId", ge=0)] = None,
) -> None:
    try:
        user = resolve_current_user(websocket)
    except ApiError:
        await websocket.close(code=4401, reason="unauthenticated")
        return
    run = storage.run_get(run_id)
    if run is None:
        await websocket.close(code=4404, reason="run not found")
        return
    if run.user != user:
        await websocket.close(code=4403, reason="forbidden")
        return

    await websocket.accept()
    bus = get_event_broadcaster()
    logger.info("ws_run_subscribed", run_id=run_id, user=user)

    # If client supplied since_id, backfill missed events first. The DB read
    # is offloaded so a large backfill can't stall the event loop (and every
    # other WS/HTTP request) on synchronous SQLite I/O.
    backfill_count = 0
    if since_id is not None:
        events = await asyncio.to_thread(
            storage.event_list, run_id=run_id, since_id=since_id, limit=500,
        )
        for ev in events:
            await websocket.send_json({
                "id": ev.id, "ts": iso_utc(ev.ts), "type": ev.type,
                "agentId": ev.agent_id, "taskId": ev.task_id,
                "payload": ev.payload or {},
            })
            backfill_count += 1
    if backfill_count:
        logger.debug("ws_backfilled", run_id=run_id, count=backfill_count)

    async with bus.subscribe(run_id) as queue:
        # Two concurrent flows: server → client (events) and client → server
        # (ping). Use asyncio.create_task + cancel on disconnect.
        send_task = asyncio.create_task(
            _drain_to_socket(websocket, queue), name=f"ws-send-{run_id}",
        )
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            logger.info("ws_disconnected", run_id=run_id, user=user)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "ws_unexpected_error", run_id=run_id, error=str(exc),
            )
        finally:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):
                pass
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()


@router.websocket("/ws/op/{op_id}")
async def op_event_stream(
    websocket: WebSocket,
    op_id: Annotated[str, Path()],
) -> None:
    """Live status stream for a long-running operation (create/import/load).

    Mirrors :func:`run_event_stream` but for the operation registry: on connect
    we send the current registry snapshot (if any) so a late subscriber gets
    state immediately, then stream live transitions published on the
    ``op:{op_id}`` channel. Live-only — no backfill/replay (the frontend queries
    ``GET /api/operations/{op_id}`` on mount for terminal recovery).

    Ownership cannot be enforced pre-accept (the op may not exist yet — a client
    can connect before ``start()`` runs), so we gate only via the snapshot's
    owner check. In local single-user mode this matches the cancel-create
    endpoint's stance. TODO(server-mode): include ``user`` in published frames
    and filter per-frame for multi-tenant isolation.
    """
    try:
        user = resolve_current_user(websocket)
    except ApiError:
        await websocket.close(code=4401, reason="unauthenticated")
        return

    await websocket.accept()
    bus = get_event_broadcaster()
    reg = get_op_registry()
    logger.info("ws_op_subscribed", op_id=op_id, user=user)

    # Subscribe BEFORE snapshotting so a transition landing in the gap isn't
    # lost (the client may then see a duplicate frame — idempotent on the
    # frontend, which keys on terminal state).
    async with bus.subscribe(op_channel(op_id)) as queue:
        snap = reg.get(op_id, user=user)
        if snap is not None:
            await websocket.send_json({"type": "op_status", **snap.to_frame()})

        send_task = asyncio.create_task(
            _drain_to_socket(websocket, queue), name=f"ws-op-send-{op_id}",
        )
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            logger.info("ws_op_disconnected", op_id=op_id, user=user)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("ws_op_unexpected_error", op_id=op_id, error=str(exc))
        finally:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):
                pass
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


async def _drain_to_socket(
    websocket: WebSocket, queue: asyncio.Queue,
) -> None:
    """Forward every queued event to the WebSocket. Cancellable."""
    while True:
        event = await queue.get()
        try:
            await websocket.send_json(event)
        except Exception:
            return  # caller will close


__all__ = ["router"]
