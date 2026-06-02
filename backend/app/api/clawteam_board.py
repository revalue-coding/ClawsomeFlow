"""Same-origin proxy for ClawTeam Board.

Why this exists:
- In SSH/web deployments, users often expose only the ClawsomeFlow port.
- The board daemon listens on a second local port and is otherwise unreachable.
- The board SPA uses absolute ``/api/*`` calls; when served under a sub-path
  we must rewrite those calls to an explicit proxy prefix.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from app.api.errors import ApiError
from app.config import load_config

router = APIRouter(tags=["clawteam-board"])

_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}
_API_PATH_RE = re.compile(r"(['\"`])/api/")


def _request_headers(req: Request) -> dict[str, str]:
    return {
        k: v
        for k, v in req.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
    }


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _rewrite_board_html(html: str, *, api_prefix: str) -> str:
    # Board SPA hardcodes '/api/*'; route it through our prefixed proxy.
    normalized = api_prefix.rstrip("/") + "/"
    return _API_PATH_RE.sub(lambda m: f"{m.group(1)}{normalized}", html)


async def _stream_bytes(resp: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    finally:
        await resp.aclose()


async def _proxy(
    *,
    request: Request,
    upstream_path: str,
    rewrite_html: bool,
    api_prefix: str = "/clawteam-board-api",
) -> Response:
    cfg = load_config()
    base = f"http://127.0.0.1:{cfg.clawteam_board_port}"
    qs = request.url.query
    url = f"{base}{upstream_path}" + (f"?{qs}" if qs else "")
    body = await request.body()
    headers = _request_headers(request)
    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
            upstream_req = client.build_request(
                request.method,
                url,
                headers=headers,
                content=body if body else None,
            )
            resp = await client.send(upstream_req, stream=True)
            content_type = resp.headers.get("content-type", "")
            if rewrite_html and "text/html" in content_type.lower():
                raw = await resp.aread()
                await resp.aclose()
                text = raw.decode(resp.encoding or "utf-8", errors="replace")
                patched = _rewrite_board_html(text, api_prefix=api_prefix).encode("utf-8")
                return Response(
                    content=patched,
                    status_code=resp.status_code,
                    headers=_response_headers(resp.headers),
                    media_type="text/html; charset=utf-8",
                )
            return StreamingResponse(
                _stream_bytes(resp),
                status_code=resp.status_code,
                headers=_response_headers(resp.headers),
            )
    except httpx.HTTPError as exc:
        raise ApiError(
            "BOARD_PROXY_UNAVAILABLE",
            f"clawteam board proxy request failed: {exc}",
            status_code=503,
        ) from exc


async def _proxy_ws(*, websocket: WebSocket, upstream_path: str) -> None:
    cfg = load_config()
    query = websocket.url.query
    upstream_url = (
        f"ws://127.0.0.1:{cfg.clawteam_board_port}{upstream_path}"
        + (f"?{query}" if query else "")
    )
    await websocket.accept()
    try:
        async with ws_connect(upstream_url, open_timeout=10) as upstream:
            async def _client_to_upstream() -> None:
                while True:
                    try:
                        msg = await websocket.receive()
                    except WebSocketDisconnect:
                        return
                    if msg.get("type") == "websocket.disconnect":
                        return
                    text = msg.get("text")
                    data = msg.get("bytes")
                    if text is not None:
                        await upstream.send(text)
                    elif data is not None:
                        await upstream.send(data)

            async def _upstream_to_client() -> None:
                async for msg in upstream:
                    if isinstance(msg, bytes):
                        await websocket.send_bytes(msg)
                    else:
                        await websocket.send_text(msg)

            t1 = asyncio.create_task(_client_to_upstream())
            t2 = asyncio.create_task(_upstream_to_client())
            done, pending = await asyncio.wait(
                {t1, t2}, return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionClosed)):
                    raise exc
    except Exception:
        # Mirror HTTP proxy behavior: close client socket on upstream failures.
        if websocket.client_state.name.lower() != "disconnected":
            await websocket.close(code=1011, reason="clawteam board ws unavailable")


@router.api_route("/clawteam-board", methods=_ALL_METHODS)
@router.api_route("/clawteam-board/{path:path}", methods=_ALL_METHODS)
async def proxy_board_page(request: Request, path: str = "") -> Response:
    upstream_path = "/" if not path else f"/{path}"
    return await _proxy(
        request=request,
        upstream_path=upstream_path,
        rewrite_html=True,
        api_prefix="/clawteam-board-api",
    )


@router.api_route("/clawteam-board-api/{path:path}", methods=_ALL_METHODS)
async def proxy_board_api(request: Request, path: str = "") -> Response:
    upstream_path = "/api" if not path else f"/api/{path}"
    return await _proxy(request=request, upstream_path=upstream_path, rewrite_html=False)


@router.api_route("/clawteam-board-proxy", methods=_ALL_METHODS)
@router.api_route("/clawteam-board-proxy/{path:path}", methods=_ALL_METHODS)
async def proxy_board_page_ssh(request: Request, path: str = "") -> Response:
    upstream_path = "/" if not path else f"/{path}"
    return await _proxy(
        request=request,
        upstream_path=upstream_path,
        rewrite_html=True,
        api_prefix="/clawteam-board-proxy-api",
    )


@router.api_route("/clawteam-board-proxy-api/{path:path}", methods=_ALL_METHODS)
async def proxy_board_api_ssh(request: Request, path: str = "") -> Response:
    upstream_path = "/api" if not path else f"/api/{path}"
    return await _proxy(request=request, upstream_path=upstream_path, rewrite_html=False)


@router.websocket("/clawteam-board-api/ws")
@router.websocket("/clawteam-board-api/ws/{path:path}")
async def proxy_board_ws(websocket: WebSocket, path: str = "") -> None:
    upstream_path = "/api/ws" if not path else f"/api/ws/{path}"
    await _proxy_ws(websocket=websocket, upstream_path=upstream_path)


@router.websocket("/clawteam-board-proxy-api/ws")
@router.websocket("/clawteam-board-proxy-api/ws/{path:path}")
async def proxy_board_ws_ssh(websocket: WebSocket, path: str = "") -> None:
    upstream_path = "/api/ws" if not path else f"/api/ws/{path}"
    await _proxy_ws(websocket=websocket, upstream_path=upstream_path)

