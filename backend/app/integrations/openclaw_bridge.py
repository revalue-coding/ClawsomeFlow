"""HTTP client for the OpenClaw Gateway (port 18789, Bearer token auth).

OpenClaw exposes an OpenAI-compatible HTTP surface on its gateway port:

* ``GET  /v1/models`` — list agent targets (``openclaw/<id>``).
* ``POST /v1/chat/completions`` — sync or SSE-streaming chat (model = agent id).
* ``POST /v1/responses`` — Responses API equivalent.

ClawsomeFlow deploy/upgrade enables
``gateway.http.endpoints.chatCompletions.enabled=true`` so this bridge can be
used out-of-the-box. If the endpoint is still disabled at runtime, the bridge
gracefully reports unavailable and the platform falls back to subprocess
``openclaw agent`` invocations (handled by the dispatcher in Phase 5).

This client is a small typed wrapper:

* :meth:`OpenclawBridge.health` — TCP/HTTP probe (returns ``GatewayHealth``).
* :meth:`OpenclawBridge.list_models` — JSON-decoded list (or empty + reason).
* :meth:`OpenclawBridge.chat_completion` — non-streaming chat call.
* :meth:`OpenclawBridge.chat_completion_stream` — SSE async iterator.

The bridge reads ``gateway.auth.token`` directly from
``~/.openclaw/openclaw.json`` so no separate config plumbing is required.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from app.config import Config, load_config
from app.integrations.openclaw_json import (
    OpenclawJsonError,
    load_openclaw_json,
)
from app.logging_setup import get_logger

logger = get_logger("openclaw_bridge")


class OpenclawBridgeError(Exception):
    """Base exception for bridge failures."""


class OpenclawGatewayUnavailable(OpenclawBridgeError):
    """Gateway is not reachable (connection refused / DNS / timeout)."""


class OpenclawAuthError(OpenclawBridgeError):
    """Gateway rejected our token (HTTP 401/403)."""


class OpenclawHttpEndpointDisabled(OpenclawBridgeError):
    """The OpenAI-compat HTTP endpoint is not enabled on this gateway (404)."""


@dataclass(frozen=True)
class GatewayHealth:
    """Result of a probe — separated so callers can decide how to react."""

    reachable: bool
    auth_ok: bool
    chat_completions_enabled: bool
    detail: str = ""


# ──────────────────────────────────────────────────────────────────────
# Bridge
# ──────────────────────────────────────────────────────────────────────


class OpenclawBridge:
    """Thin async wrapper around the OpenClaw HTTP gateway.

    The same instance can be reused across requests (httpx.AsyncClient is
    pool-friendly). Always use as an async context manager OR call
    :meth:`aclose` when done::

        async with OpenclawBridge.from_config() as bridge:
            health = await bridge.health()
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_config(cls, config: Config | None = None) -> "OpenclawBridge":
        """Build a bridge from ClawsomeFlow + OpenClaw config.

        Reads ``gateway.auth.token`` from ``~/.openclaw/openclaw.json``.
        Raises :class:`OpenclawJsonError` if the file is missing.
        """
        cfg = config or load_config()
        try:
            data = load_openclaw_json(cfg)
        except OpenclawJsonError:
            raise
        gw = data.get("gateway", {})
        token = (gw.get("auth") or {}).get("token", "")
        if not token:
            raise OpenclawJsonError(
                "gateway.auth.token is missing from openclaw.json"
            )
        port = gw.get("port", 18789)
        bind = gw.get("bind", "loopback")
        host = "127.0.0.1" if bind == "loopback" else "127.0.0.1"
        base = f"http://{host}:{port}"
        # Allow user override (e.g. tunneled) via Config.openclaw_gateway_url.
        if cfg.openclaw_gateway_url and cfg.openclaw_gateway_url != "http://127.0.0.1:18789":
            base = cfg.openclaw_gateway_url
        return cls(base, token)

    async def __aenter__(self) -> "OpenclawBridge":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # ── internals ────────────────────────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._client

    def _classify(self, exc: Exception) -> OpenclawBridgeError:
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in (401, 403):
                return OpenclawAuthError(f"gateway rejected token: HTTP {code}")
            if code == 404:
                return OpenclawHttpEndpointDisabled(
                    "HTTP endpoint not found (chat completions likely disabled)"
                )
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return OpenclawGatewayUnavailable(str(exc))
        if isinstance(exc, httpx.HTTPError):
            return OpenclawBridgeError(str(exc))
        return OpenclawBridgeError(repr(exc))

    # ── public surface ───────────────────────────────────────────────

    async def health(self) -> GatewayHealth:
        """Probe the gateway. Never raises; returns a structured result."""
        client = self._ensure_client()
        try:
            r = await client.get(f"{self.base_url}/v1/models", timeout=5.0)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            logger.info("gateway_probe", reachable=False, error=str(exc))
            return GatewayHealth(reachable=False, auth_ok=False, chat_completions_enabled=False, detail=str(exc))

        reachable = True
        if r.status_code in (401, 403):
            return GatewayHealth(reachable=True, auth_ok=False, chat_completions_enabled=False, detail=f"HTTP {r.status_code}")
        if r.status_code == 404:
            return GatewayHealth(reachable=True, auth_ok=True, chat_completions_enabled=False, detail="chat completions disabled")
        if r.status_code >= 500:
            return GatewayHealth(reachable=True, auth_ok=True, chat_completions_enabled=False, detail=f"HTTP {r.status_code}")
        if r.status_code == 200:
            return GatewayHealth(reachable=True, auth_ok=True, chat_completions_enabled=True)
        return GatewayHealth(reachable=reachable, auth_ok=True, chat_completions_enabled=False, detail=f"HTTP {r.status_code}")

    async def list_models(self) -> list[dict[str, Any]]:
        """Return ``GET /v1/models`` payload as ``data`` list, or raise."""
        client = self._ensure_client()
        try:
            r = await client.get(f"{self.base_url}/v1/models")
            r.raise_for_status()
            payload = r.json()
            return payload.get("data", [])
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc

    async def chat_completion(
        self,
        *,
        agent_id: str,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
        model_override: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Non-streaming OpenAI-compatible chat completion.

        ``agent_id`` is mapped to ``model: openclaw/<agent_id>``.
        Use ``model_override`` to set the backend provider/model via header.
        """
        client = self._ensure_client()
        body: dict[str, Any] = {
            "model": f"openclaw/{agent_id}",
            "messages": messages,
        }
        headers: dict[str, str] = {}
        if session_key is not None:
            headers["x-openclaw-session-key"] = session_key
        if model_override is not None:
            headers["x-openclaw-model"] = model_override
        try:
            r = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=headers or None,
                timeout=timeout if timeout is not None else self._timeout,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc

    async def chat_completion_stream(
        self,
        *,
        agent_id: str,
        messages: list[dict[str, Any]],
        session_key: str | None = None,
        model_override: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """SSE-streamed chat completion. Yields parsed JSON deltas.

        The async generator terminates after the gateway emits ``data: [DONE]``.
        """
        client = self._ensure_client()
        body: dict[str, Any] = {
            "model": f"openclaw/{agent_id}",
            "messages": messages,
            "stream": True,
        }
        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if session_key is not None:
            headers["x-openclaw-session-key"] = session_key
        if model_override is not None:
            headers["x-openclaw-model"] = model_override

        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        logger.warning("stream_invalid_json", line=payload[:200])
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc


__all__ = [
    "GatewayHealth",
    "OpenclawAuthError",
    "OpenclawBridge",
    "OpenclawBridgeError",
    "OpenclawGatewayUnavailable",
    "OpenclawHttpEndpointDisabled",
]
