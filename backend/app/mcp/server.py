"""ClawsomeFlow MCP server — tool definitions.

Every tool is a thin wrapper over the local backend HTTP API (loopback +
api_token, reusing ``app.cli.ops._http`` connection settings). Tool docstrings
are the contract the calling agent reads, so keep them accurate and concise:
one-line purpose, each parameter's meaning/type, and the return shape.

The server does **not** prescribe a workflow or force the agent to poll — it
only describes what each tool does. Typical usage is the agent's choice:
discover a Flow (``list_flows`` / ``describe_flow``), start it (``run_flow``),
and later read status/result (``get_run_status`` / ``get_run_result``).
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from pydantic import Field

from app.cli.ops._http import _base, _headers
from app.cli.ops.runs import _extract_param_fields
from app.flow_modes import flow_mode

# The MCP server name the agent sees. Kept stable so registrations stay valid.
SERVER_NAME = "clawsomeflow"

_INSTRUCTIONS = (
    "ClawsomeFlow orchestration tools. Use these to discover DAG-based Flows, "
    "trigger a Flow run, and (only when asked) read a run's status and the "
    "leader's final work report. Runs triggered via run_flow are unattended: "
    "they skip human review/approval and drive straight to a terminal status.\n\n"
    "IMPORTANT — do not wait for results unless the user asks: run_flow returns "
    "immediately with a run id and the run continues on its own, possibly for a "
    "long time. After dispatching, report the run id to the user and stop. Do "
    "NOT poll or block waiting for the run to finish. Call get_run_status / "
    "get_run_result ONLY when the user explicitly asks about the outcome."
)

# HTTP timeout for local API calls (seconds). Generous for list/describe; all
# tools return quickly because triggering is non-blocking.
_HTTP_TIMEOUT = 30.0


class _ApiError(RuntimeError):
    """Raised when the local backend returns a non-2xx response."""


def _request(method: str, path: str, *, json: Any | None = None, params: dict | None = None) -> Any:
    """Call the local backend API; return parsed JSON (dict/list) or {}.

    Raises :class:`_ApiError` with a human-readable message on HTTP error so the
    tool can surface it to the agent instead of crashing the server.
    """
    url = f"{_base()}{path}"
    with httpx.Client(timeout=_HTTP_TIMEOUT, headers=_headers()) as client:
        resp = client.request(method, url, json=json, params=params or None)
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = f"{body.get('error', '')}: {body.get('message', '')}".strip(": ")
        except Exception:
            detail = resp.text[:300]
        raise _ApiError(f"HTTP {resp.status_code} calling {method} {path}: {detail}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def _summary_view(flow: dict) -> dict:
    """Compact view from a Flow list item (GET /api/flows — no full spec, but
    carries easyMode/devMode + paramFields)."""
    if flow.get("devMode"):
        mode = "dev"
    elif flow.get("easyMode"):
        mode = "easy"
    else:
        mode = "normal"
    return {
        "id": flow.get("id"),
        "name": flow.get("name"),
        "description": flow.get("description") or "",
        "mode": mode,
        "param_fields": flow.get("paramFields") or [],
    }


def _detail_view(flow: dict) -> dict:
    """Full view from a Flow detail (GET /api/flows/{id} — carries spec)."""
    spec = flow.get("spec") or {}
    variables = spec.get("variables") or {}
    return {
        "id": flow.get("id"),
        "name": flow.get("name"),
        "description": flow.get("description") or "",
        "mode": flow_mode(variables),  # "normal" | "easy" | "dev"
        "param_fields": _extract_param_fields(spec),
    }


def build_server() -> Any:
    """Construct and return the FastMCP server instance (tools registered)."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name=SERVER_NAME, instructions=_INSTRUCTIONS)

    @mcp.tool()
    def list_flows() -> list[dict]:
        """List the Flows you can run, with the input fields each one expects.

        Takes no arguments.

        Returns a list of objects, one per Flow:
          - id (str): the Flow id to pass to run_flow / describe_flow.
          - name (str): human-readable Flow name.
          - description (str): what the Flow does.
          - mode (str): execution mode, one of "normal" | "easy" | "dev".
          - param_fields (list[str]): names of the input fields this Flow
            expects; use these as keys in run_flow's `inputs` (empty = no inputs).
        """
        flows = _request("GET", "/api/flows")
        items = flows.get("items", flows) if isinstance(flows, dict) else flows
        return [_summary_view(f) for f in (items or [])]

    @mcp.tool()
    def describe_flow(
        flow_id: Annotated[str, Field(description="The Flow id (from list_flows).")],
    ) -> dict:
        """Get one Flow's details, including exactly which inputs it expects.

        Parameters:
          - flow_id (str, required): the Flow id.

        Returns an object:
          - id, name, description, mode (see list_flows)
          - param_fields (list[str]): required input field names for run_flow.
          - tasks (list): [{id, subject, owner_agent_id, is_leader_summary}] —
            an overview of the Flow's task DAG (leader summary marks the end).
          - agents (list): [{id, kind}] — the agents that execute the Flow.
        """
        flow = _request("GET", f"/api/flows/{flow_id}")
        view = _detail_view(flow)
        spec = flow.get("spec") or {}
        view["tasks"] = [
            {
                "id": t.get("id"),
                "subject": t.get("subject"),
                "owner_agent_id": t.get("ownerAgentId") or t.get("owner_agent_id"),
                "is_leader_summary": bool(
                    t.get("isLeaderSummary") or t.get("is_leader_summary")
                ),
            }
            for t in (spec.get("tasks") or [])
        ]
        view["agents"] = [
            {"id": a.get("id"), "kind": a.get("kind")}
            for a in (spec.get("agents") or [])
        ]
        return view

    @mcp.tool()
    def run_flow(
        flow_id: Annotated[str, Field(description="The Flow id to run (from list_flows).")],
        inputs: Annotated[
            dict[str, Any] | None,
            Field(
                description="Input field values for this run, keyed by the Flow's "
                "param_fields names (see describe_flow). Pass {} or omit if the "
                "Flow declares no inputs."
            ),
        ] = None,
    ) -> dict:
        """Start a run of a Flow. Returns immediately with the new run id.

        The run is triggered **unattended**: it skips the human merge-review,
        complaint and checkpoint phases and drives straight to a terminal status
        (the Flow's own execution mode — normal/easy/dev — is preserved). This
        call does NOT wait for the run to finish.

        Do not wait for the result: after this returns, report the run id to the
        user and stop. Do not poll get_run_status/get_run_result unless the user
        explicitly asks how the run turned out — the run may take a long time and
        finishes on its own.

        Parameters:
          - flow_id (str, required): the Flow id.
          - inputs (object, optional): {field_name: value} for the Flow's
            declared parameter fields; values are strings/numbers.

        Returns an object:
          - run_id (str): id of the created run (use it with get_run_status /
            get_run_result).
          - status (str): initial run status (usually "pending").
        """
        body = {"inputs": inputs or {}, "unattended": True}
        data = _request("POST", f"/api/flows/{flow_id}/runs", json=body)
        return {"run_id": data.get("id"), "status": data.get("status")}

    @mcp.tool()
    def get_run_status(
        run_id: Annotated[str, Field(description="The run id (from run_flow).")],
    ) -> dict:
        """Get a run's current status.

        Parameters:
          - run_id (str, required): the run id.

        Returns an object:
          - run_id (str)
          - status (str): e.g. pending / compiling / running / completed /
            completed_with_conflicts / failed / aborted.
          - terminal (bool): True once the run has finished (any terminal status).
          - success (bool): True if the run completed successfully.
        """
        data = _request("GET", f"/api/runs/{run_id}/result")
        return {
            "run_id": data.get("runId"),
            "status": data.get("status"),
            "terminal": bool(data.get("terminal")),
            "success": bool(data.get("success")),
        }

    @mcp.tool()
    def get_run_result(
        run_id: Annotated[str, Field(description="The run id (from run_flow).")],
    ) -> dict:
        """Get a run's status and the leader's final work report.

        Parameters:
          - run_id (str, required): the run id.

        Returns an object:
          - run_id (str)
          - status (str): current run status.
          - terminal (bool): True once the run has finished.
          - success (bool): True if the run completed successfully.
          - report (str | null): the leader's final work report text; null until
            the run reaches a terminal status.
          - reason (str | null): short failure reason for a non-successful
            terminal run, else null.
          - finished_at (str | null): ISO-8601 finish time, or null if unfinished.
        """
        data = _request("GET", f"/api/runs/{run_id}/result")
        return {
            "run_id": data.get("runId"),
            "status": data.get("status"),
            "terminal": bool(data.get("terminal")),
            "success": bool(data.get("success")),
            "report": data.get("report"),
            "reason": data.get("reason"),
            "finished_at": data.get("finishedAt"),
        }

    @mcp.tool()
    def list_runs(
        flow_id: Annotated[
            str | None,
            Field(description="Optional Flow id to filter runs to one Flow."),
        ] = None,
        limit: Annotated[
            int, Field(description="Max number of runs to return (1-100).", ge=1, le=100)
        ] = 20,
    ) -> list[dict]:
        """List recent runs, most recent first.

        Parameters:
          - flow_id (str, optional): restrict to runs of this Flow.
          - limit (int, optional, default 20): max runs to return.

        Returns a list of objects: {run_id, flow_id, status, started_at,
        finished_at}.
        """
        params: dict[str, Any] = {"limit": limit}
        if flow_id:
            params["flowId"] = flow_id
        data = _request("GET", "/api/runs", params=params)
        items = data.get("items", []) if isinstance(data, dict) else (data or [])
        return [
            {
                "run_id": r.get("id"),
                "flow_id": r.get("flowId"),
                "status": r.get("status"),
                "started_at": r.get("startedAt"),
                "finished_at": r.get("finishedAt"),
            }
            for r in items
        ]

    @mcp.tool()
    def abort_run(
        run_id: Annotated[str, Field(description="The run id to abort.")],
    ) -> dict:
        """Abort an in-progress run.

        Parameters:
          - run_id (str, required): the run id to cancel.

        Returns an object: {run_id, status} with the run's status after abort.
        """
        data = _request("POST", f"/api/runs/{run_id}/abort")
        return {"run_id": data.get("id", run_id), "status": data.get("status")}

    return mcp


def serve() -> None:
    """Run the MCP server over stdio (blocks until the client disconnects)."""
    build_server().run(transport="stdio")
