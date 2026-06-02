"""ClawTeam MCP client wrapper.

Public API:
* :class:`ClawTeamMcpClient` — async wrapper around the official ``mcp`` SDK
  speaking stdio with the ``clawteam-mcp`` server.
* :func:`get_mcp_client` — lazy singleton (one stdio session per process).
* :func:`close_mcp_client` — used during app shutdown / tests.
* :class:`McpToolError` — raised when ``isError`` is set or JSON parse fails.

Design notes (kept intentionally simple for MVP, extensible per agreed plan):

* The wrapper exposes one *typed* method per ClawTeam MCP tool we actually
  use today (compile stage + scheduler hot path). Other tools can be added
  by appending a thin method that calls :meth:`_call_json`.
* Results are returned as ``dict[str, Any]`` (or ``list[dict]``) using the
  raw camelCase field names the server emits. We deliberately do *not* yet
  introduce per-tool Pydantic DTOs — those land alongside the modules that
  consume them, when their consumers actually need typing. This avoids
  upfront over-modelling and keeps schema drift low.
* All calls are serialised through a single ``asyncio.Lock`` because MCP
  stdio sessions are not thread-safe across concurrent in-flight requests.
  Phase 5+ may swap this for a small connection pool if profiling shows it.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app import logging_setup
from app.config import Config, load_config
from app.user_context import get_request_user


class McpToolError(Exception):
    """Raised when a ClawTeam MCP tool reports ``isError=True`` or returns no parseable content."""

    def __init__(self, tool: str, message: str, *, raw: str | None = None):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message
        self.raw = raw


class ClawTeamMcpClient:
    """Async wrapper around the ClawTeam MCP server.

    Usage::

        client = await ClawTeamMcpClient.start()
        team = await client.team_create("csflow-runABC", "leader", "id-xyz")
        ...
        await client.close()
    """

    def __init__(self, *, acting_user: str | None = None, config: Config | None = None) -> None:
        self._cfg = config or load_config()
        self._acting_user = acting_user or get_request_user() or self._cfg.default_user
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._call_lock = asyncio.Lock()
        self._log = logging_setup.get_logger("clawteam_mcp")

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    @classmethod
    async def start(
        cls, *, acting_user: str | None = None, config: Config | None = None,
    ) -> "ClawTeamMcpClient":
        """Spawn the MCP server subprocess and complete the handshake."""
        c = cls(acting_user=acting_user, config=config)
        await c._connect()
        return c

    async def _connect(self) -> None:
        params = StdioServerParameters(command="clawteam-mcp", env=self._env())

        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            self._log.info("mcp_session_started")
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            self._session = None
            raise

    async def close(self) -> None:
        """Close the stdio session (idempotent)."""
        if self._stack is None:
            return
        try:
            await self._stack.aclose()
        finally:
            self._stack = None
            self._session = None
            self._log.info("mcp_session_closed")

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CLAWTEAM_USER"] = self._acting_user
        if self._cfg.clawteam_data_dir:
            env["CLAWTEAM_DATA_DIR"] = self._cfg.clawteam_data_dir
        return env

    # ──────────────────────────────────────────────────────────────────
    # Low-level call helpers
    # ──────────────────────────────────────────────────────────────────

    async def _call_one(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Invoke *tool*, expect a single JSON object (or empty)."""
        contents = await self._call_raw(tool, args)
        if not contents:
            return None
        if len(contents) > 1:
            raise McpToolError(
                tool,
                f"expected single JSON object but received {len(contents)} content items",
            )
        return _parse_json(tool, contents[0])

    async def _call_many(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """Invoke *tool*, expect a list of JSON objects (one per ``TextContent``).

        ClawTeam MCP returns one ``TextContent`` per item for list-style tools
        (task_list, mailbox_receive, team_list, etc.). A single object can also
        be wrapped in a one-element list (e.g. some tools return ``{"...":...}``
        as the only content).
        """
        contents = await self._call_raw(tool, args)
        return [_parse_json(tool, c) for c in contents]

    async def _call_raw(self, tool: str, args: dict[str, Any]) -> list[Any]:
        """Low-level: call the tool, raise on error, return ``content`` list."""
        if self._session is None:
            raise McpToolError(tool, "session not started")
        async with self._call_lock:
            self._log.debug("mcp_call", tool=tool, args=_sanitise(args))
            result = await self._session.call_tool(tool, args)
        if result.isError:
            raw = _first_text(result.content) or ""
            raise McpToolError(tool, raw or "tool reported isError", raw=raw)
        return list(result.content or [])

    # ──────────────────────────────────────────────────────────────────
    # Team operations
    # ──────────────────────────────────────────────────────────────────

    async def team_create(
        self,
        team_name: str,
        leader_name: str,
        leader_id: str,
        *,
        description: str = "",
        user: str = "",
        leader_agent_type: str = "leader",
    ) -> dict[str, Any]:
        """Create a team (idempotent at the application layer; raises if it already exists)."""
        result = await self._call_one("team_create", {
            "team_name": team_name,
            "leader_name": leader_name,
            "leader_id": leader_id,
            "description": description,
            "user": user,
            "leader_agent_type": leader_agent_type,
        })
        if result is None:
            raise McpToolError("team_create", "no result")
        return result

    async def team_get(self, team_name: str) -> dict[str, Any] | None:
        try:
            return await self._call_one("team_get", {"team_name": team_name})
        except McpToolError:
            return None

    async def team_list(self) -> list[dict[str, Any]]:
        return await self._call_many("team_list", {})

    async def team_member_add(
        self,
        team_name: str,
        member_name: str,
        agent_id: str,
        *,
        agent_type: str = "general-purpose",
        user: str = "",
    ) -> dict[str, Any]:
        result = await self._call_one("team_member_add", {
            "team_name": team_name,
            "member_name": member_name,
            "agent_id": agent_id,
            "agent_type": agent_type,
            "user": user,
        })
        if result is None:
            raise McpToolError("team_member_add", "no result")
        return result

    # ──────────────────────────────────────────────────────────────────
    # Task operations
    # ──────────────────────────────────────────────────────────────────

    async def task_create(
        self,
        team_name: str,
        subject: str,
        *,
        description: str = "",
        owner: str = "",
        priority: str | None = None,
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "team_name": team_name,
            "subject": subject,
            "description": description,
            "owner": owner,
        }
        if priority:
            args["priority"] = priority
        if blocks is not None:
            args["blocks"] = blocks
        if blocked_by is not None:
            args["blocked_by"] = blocked_by
        if metadata is not None:
            args["metadata"] = metadata
        result = await self._call_one("task_create", args)
        if result is None:
            raise McpToolError("task_create", "no result")
        return result

    async def task_list(
        self,
        team_name: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        priority: str | None = None,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"team_name": team_name}
        if status:
            args["status"] = status
        if owner:
            args["owner"] = owner
        if priority:
            args["priority"] = priority
        return await self._call_many("task_list", args)

    async def task_get(self, team_name: str, task_id: str) -> dict[str, Any] | None:
        try:
            return await self._call_one("task_get", {
                "team_name": team_name, "task_id": task_id,
            })
        except McpToolError:
            return None

    async def task_update(
        self,
        team_name: str,
        task_id: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        caller: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "team_name": team_name,
            "task_id": task_id,
            "caller": caller,
            "force": force,
        }
        for k, v in (
            ("status", status),
            ("owner", owner),
            ("subject", subject),
            ("description", description),
            ("priority", priority),
            ("add_blocks", add_blocks),
            ("add_blocked_by", add_blocked_by),
            ("metadata", metadata),
        ):
            if v is not None:
                args[k] = v
        result = await self._call_one("task_update", args)
        if result is None:
            raise McpToolError("task_update", "no result")
        return result

    # ──────────────────────────────────────────────────────────────────
    # Mailbox operations
    # ──────────────────────────────────────────────────────────────────

    async def mailbox_send(
        self,
        team_name: str,
        *,
        from_agent: str,
        to: str,
        content: str,
    ) -> dict[str, Any] | None:
        return await self._call_one("mailbox_send", {
            "team_name": team_name,
            "from_agent": from_agent,
            "to": to,
            "content": content,
        })

    async def mailbox_receive(
        self, team_name: str, agent_name: str, *, limit: int = 10,
    ) -> list[dict[str, Any]]:
        return await self._mailbox_rows_via_cli(
            team_name=team_name,
            agent_name=agent_name,
            consume=True,
            limit=limit,
        )

    async def mailbox_peek(self, team_name: str, agent_name: str) -> list[dict[str, Any]]:
        return await self._mailbox_rows_via_cli(
            team_name=team_name,
            agent_name=agent_name,
            consume=False,
        )

    async def _mailbox_rows_via_cli(
        self,
        *,
        team_name: str,
        agent_name: str,
        consume: bool,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        subcmd = "receive" if consume else "peek"
        argv = [
            "clawteam",
            "--json",
            "inbox",
            subcmd,
            team_name,
            "--agent",
            agent_name,
        ]
        if consume:
            argv += ["--limit", str(limit)]
        exit_code, stdout, stderr = await _run_cli(argv, env=self._env())
        if exit_code != 0:
            self._log.warning(
                "mailbox_cli_read_failed",
                mode=subcmd,
                team=team_name,
                agent=agent_name,
                exit_code=exit_code,
                stderr=(stderr or "")[:1000],
            )
            return []
        rows = _extract_mailbox_rows(_try_parse_json(stdout))
        if rows:
            self._log.info(
                "mailbox_cli_read",
                mode=subcmd,
                team=team_name,
                agent=agent_name,
                row_count=len(rows),
            )
        return rows

    # ──────────────────────────────────────────────────────────────────
    # Workspace introspection (used by leader prompts + UI diff)
    # ──────────────────────────────────────────────────────────────────

    async def workspace_agent_diff(
        self,
        team_name: str,
        agent_name: str,
        *,
        repo: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "team_name": team_name,
            "agent_name": agent_name,
        }
        if repo:
            payload["repo"] = repo
        return await self._call_one("workspace_agent_diff", payload)

    async def workspace_agent_summary(self, team_name: str, agent_name: str) -> dict[str, Any] | None:
        return await self._call_one("workspace_agent_summary", {
            "team_name": team_name, "agent_name": agent_name,
        })


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _first_text(content: list[Any] | None) -> str | None:
    """Return the ``.text`` of the first ``TextContent`` in *content*, if any."""
    if not content:
        return None
    item = content[0]
    return getattr(item, "text", None)


def _parse_json(tool: str, item: Any) -> dict[str, Any]:
    """Extract ``.text`` from a ``TextContent`` and parse as JSON object."""
    text = getattr(item, "text", None)
    if text is None:
        raise McpToolError(tool, f"unexpected content item type: {type(item).__name__}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpToolError(tool, f"invalid JSON: {exc}", raw=text) from exc
    if not isinstance(parsed, dict):
        raise McpToolError(tool, f"expected JSON object, got {type(parsed).__name__}", raw=text)
    return parsed


def _try_parse_json(stdout: str) -> dict[str, Any] | list[Any] | None:
    if not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _extract_mailbox_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize mailbox payloads across MCP/CLI shape differences.

    We commonly see one of these forms:
    - list[message-row]
    - {"messages": list[message-row]}
    - {"result": list[message-row]}
    - list[{"messages": [...]}/{"result": [...]}]
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        out: list[dict[str, Any]] = []
        for item in payload:
            out.extend(_extract_mailbox_rows(item))
        return out
    if isinstance(payload, dict):
        if _is_mailbox_message(payload):
            return [payload]
        for key in ("messages", "result"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return _extract_mailbox_rows(nested)
        return []
    return []


def _is_mailbox_message(row: dict[str, Any]) -> bool:
    has_text = isinstance(row.get("content"), str) or isinstance(row.get("body"), str)
    has_identity = any(
        k in row for k in ("from_agent", "from", "to", "task_id", "taskId", "last_task", "lastTask")
    )
    return has_text and has_identity


async def _run_cli(argv: list[str], *, env: dict[str, str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_b.decode(errors="replace"),
        stderr_b.decode(errors="replace"),
    )


_SENSITIVE_KEYS = {"password", "token", "api_key", "secret"}


def _sanitise(args: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive values from log payloads."""
    return {k: ("<redacted>" if k.lower() in _SENSITIVE_KEYS else v) for k, v in args.items()}


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_singleton_by_user: dict[str, ClawTeamMcpClient] = {}
_init_lock = asyncio.Lock()


async def get_mcp_client(*, user: str | None = None) -> ClawTeamMcpClient:
    """Return the process-wide user-scoped :class:`ClawTeamMcpClient`."""
    cfg = load_config()
    effective_user = user or get_request_user() or cfg.default_user
    cached = _singleton_by_user.get(effective_user)
    if cached is not None:
        return cached
    async with _init_lock:
        cached = _singleton_by_user.get(effective_user)
        if cached is not None:
            return cached
        client = await ClawTeamMcpClient.start(acting_user=effective_user, config=cfg)
        _singleton_by_user[effective_user] = client
        return client


async def close_mcp_client(*, user: str | None = None) -> None:
    """Close one cached client or all clients when *user* is None."""
    if user is not None:
        client = _singleton_by_user.pop(user, None)
        if client is not None:
            await client.close()
        return
    clients = list(_singleton_by_user.values())
    _singleton_by_user.clear()
    for client in clients:
        await client.close()


__all__ = [
    "ClawTeamMcpClient",
    "McpToolError",
    "get_mcp_client",
    "close_mcp_client",
]
