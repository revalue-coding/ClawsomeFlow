"""Tests for app.integrations.openclaw_bridge — HTTP gateway client.

Uses httpx.MockTransport so no real network is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.integrations import openclaw_bridge as br


def _bridge_with_transport(handler) -> br.OpenclawBridge:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        transport=transport,
        headers={"Authorization": "Bearer testtoken"},
    )
    return br.OpenclawBridge(
        base_url="http://127.0.0.1:18789",
        token="testtoken",
        client=client,
    )


@pytest.mark.asyncio
async def test_health_full_ok() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer testtoken"
        return httpx.Response(200, json={"data": [{"id": "openclaw/default"}]})

    async with _bridge_with_transport(handler) as bridge:
        h = await bridge.health()
        assert h.reachable and h.auth_ok and h.chat_completions_enabled


@pytest.mark.asyncio
async def test_health_auth_failure() -> None:
    handler = lambda req: httpx.Response(401, json={"error": "bad token"})  # noqa: E731
    async with _bridge_with_transport(handler) as bridge:
        h = await bridge.health()
        assert h.reachable and not h.auth_ok and not h.chat_completions_enabled


@pytest.mark.asyncio
async def test_health_chat_disabled() -> None:
    handler = lambda req: httpx.Response(404)  # noqa: E731
    async with _bridge_with_transport(handler) as bridge:
        h = await bridge.health()
        assert h.reachable and h.auth_ok and not h.chat_completions_enabled


@pytest.mark.asyncio
async def test_health_unreachable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _bridge_with_transport(handler) as bridge:
        h = await bridge.health()
        assert not h.reachable


@pytest.mark.asyncio
async def test_list_models() -> None:
    handler = lambda req: httpx.Response(  # noqa: E731
        200, json={"data": [{"id": "openclaw/default"}, {"id": "openclaw/foo"}]}
    )
    async with _bridge_with_transport(handler) as bridge:
        models = await bridge.list_models()
        assert {m["id"] for m in models} == {"openclaw/default", "openclaw/foo"}


@pytest.mark.asyncio
async def test_chat_completion_request_shape() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        captured["session_key"] = req.headers.get("x-openclaw-session-key")
        captured["model_override"] = req.headers.get("x-openclaw-model")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello"}}]},
        )

    async with _bridge_with_transport(handler) as bridge:
        out = await bridge.chat_completion(
            agent_id="my-agent",
            messages=[{"role": "user", "content": "hi"}],
            session_key="csflow-team-x",
            model_override="poe/GPT-5.4",
        )

    assert out["choices"][0]["message"]["content"] == "hello"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["model"] == "openclaw/my-agent"
    assert captured["session_key"] == "csflow-team-x"
    assert captured["model_override"] == "poe/GPT-5.4"


@pytest.mark.asyncio
async def test_chat_auth_error_classified() -> None:
    handler = lambda req: httpx.Response(403)  # noqa: E731
    async with _bridge_with_transport(handler) as bridge:
        with pytest.raises(br.OpenclawAuthError):
            await bridge.chat_completion(agent_id="x", messages=[])


@pytest.mark.asyncio
async def test_chat_endpoint_disabled_classified() -> None:
    handler = lambda req: httpx.Response(404)  # noqa: E731
    async with _bridge_with_transport(handler) as bridge:
        with pytest.raises(br.OpenclawHttpEndpointDisabled):
            await bridge.chat_completion(agent_id="x", messages=[])


@pytest.mark.asyncio
async def test_chat_completion_stream_yields_deltas() -> None:
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
        b'data: [DONE]\n\n'
    )

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["stream"] is True
        return httpx.Response(
            200,
            content=sse_body,
            headers={"content-type": "text/event-stream"},
        )

    async with _bridge_with_transport(handler) as bridge:
        deltas: list[dict] = []
        async for chunk in bridge.chat_completion_stream(
            agent_id="my-agent", messages=[{"role": "user", "content": "hi"}]
        ):
            deltas.append(chunk)

    assert len(deltas) == 2
    assert deltas[0]["choices"][0]["delta"]["content"] == "he"


@pytest.mark.asyncio
async def test_from_config_reads_token(tmp_path: Path) -> None:
    """OpenclawBridge.from_config() should pick up the gateway.auth.token."""
    from app.config import load_config, save_config

    oc_home = tmp_path / "openclaw_home"
    oc_home.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(update={"openclaw_home": str(oc_home)})
    save_config(cfg)
    (oc_home / "openclaw.json").write_text(json.dumps({
        "gateway": {"port": 18789, "bind": "loopback", "auth": {"token": "S3CR3T"}},
        "agents": {"list": []},
    }))

    bridge = br.OpenclawBridge.from_config()
    try:
        assert bridge._token == "S3CR3T"
        assert bridge.base_url == "http://127.0.0.1:18789"
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_from_config_missing_token_raises(tmp_path: Path) -> None:
    from app.config import load_config, save_config
    from app.integrations.openclaw_json import OpenclawJsonError

    oc_home = tmp_path / "openclaw_home"
    oc_home.mkdir()
    cfg = load_config()
    cfg = cfg.model_copy(update={"openclaw_home": str(oc_home)})
    save_config(cfg)
    (oc_home / "openclaw.json").write_text(json.dumps({
        "gateway": {"port": 18789, "auth": {}},  # no token
        "agents": {"list": []},
    }))

    with pytest.raises(OpenclawJsonError):
        br.OpenclawBridge.from_config()
