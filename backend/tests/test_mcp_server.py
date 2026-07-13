"""Tests for the ClawsomeFlow MCP server tools (app.mcp.server)."""

from __future__ import annotations

import asyncio
import json

import pytest

import app.mcp.server as srv


def _tool_json(result) -> dict:
    """Extract the structured JSON payload from a FastMCP call_tool result.

    This SDK's ``call_tool`` returns a ``(content_blocks, structured)`` tuple;
    the structured dict is the reliable full payload (the content blocks may
    render only the first element of a list return). A non-dict tool return is
    wrapped by FastMCP as ``{"result": <value>}``.
    """
    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], dict):
        return result[1]
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


def test_build_server_registers_expected_tools() -> None:
    mcp = srv.build_server()
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {
        "list_flows", "describe_flow", "run_flow",
        "get_run_status", "get_run_result", "list_runs", "abort_run",
    }


def test_every_tool_has_a_description() -> None:
    mcp = srv.build_server()
    for t in asyncio.run(mcp.list_tools()):
        assert t.description and t.description.strip(), t.name


def test_detail_view_extracts_mode_and_param_fields() -> None:
    flow = {
        "id": "f1", "name": "N", "description": "D",
        "spec": {"variables": {
            "csflow.dev_mode": "true",
            "csflow.runtime.param_fields": json.dumps(["topic", "url"]),
        }},
    }
    view = srv._detail_view(flow)
    assert view["id"] == "f1"
    assert view["mode"] == "dev"
    assert view["param_fields"] == ["topic", "url"]


def test_summary_view_mode_from_booleans() -> None:
    # GET /api/flows returns summaries with easyMode/devMode (no spec).
    assert srv._summary_view({"id": "f1", "easyMode": True})["mode"] == "easy"
    assert srv._summary_view({"id": "f1", "devMode": True})["mode"] == "dev"
    assert srv._summary_view({"id": "f1"})["mode"] == "normal"


def test_run_flow_sends_unattended_and_returns_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_request(method, path, *, json=None, params=None):
        calls.append({"method": method, "path": path, "json": json})
        return {"id": "run-9", "status": "pending", "teamName": "csflow-x"}

    monkeypatch.setattr(srv, "_request", fake_request)
    mcp = srv.build_server()
    out = _tool_json(asyncio.run(
        mcp.call_tool("run_flow", {"flow_id": "f1", "inputs": {"goal": "x"}})
    ))
    assert out == {"run_id": "run-9", "status": "pending"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/api/flows/f1/runs"
    assert calls[0]["json"]["unattended"] is True
    assert calls[0]["json"]["inputs"] == {"goal": "x"}


def test_get_run_result_maps_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srv, "_request", lambda *a, **k: {
        "runId": "run-9", "status": "completed", "terminal": True,
        "success": True, "report": "done", "reason": None, "finishedAt": "2026-07-13T00:00:00Z",
    })
    mcp = srv.build_server()
    out = _tool_json(asyncio.run(mcp.call_tool("get_run_result", {"run_id": "run-9"})))
    assert out["report"] == "done"
    assert out["success"] is True
    assert out["terminal"] is True


def test_list_flows_maps_items(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real GET /api/flows returns FlowSummary items (easyMode/devMode, no spec).
    monkeypatch.setattr(srv, "_request", lambda *a, **k: {"items": [
        {"id": "f1", "name": "A", "description": "", "easyMode": True},
    ]})
    mcp = srv.build_server()
    payload = _tool_json(asyncio.run(mcp.call_tool("list_flows", {})))
    # FastMCP wraps a non-dict return as {"result": <value>}.
    flows = payload["result"] if isinstance(payload, dict) and "result" in payload else payload
    assert isinstance(flows, list)
    assert flows[0]["id"] == "f1"
