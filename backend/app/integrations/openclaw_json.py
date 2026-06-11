"""Safe editor for ``~/.openclaw/openclaw.json``.

Public API:
* :func:`load_openclaw_json` — read + parse the active config.
* :func:`update_openclaw_json` — convenience: read → mutate via callback →
  write, all under one lock acquisition.
* :func:`append_managed_agent` / :func:`remove_managed_agent` — high-level
  mutators used by the OpenClaw integration.
* :func:`sanitize_managed_agent_entries` — strip legacy invalid keys from
  managed entries.
* :func:`list_managed_agents` — resolve managed agents via the ClawsomeFlow
  registry file (not by injecting extra keys into openclaw.json).
* :class:`OpenclawJsonError` — load / parse / ownership failures.

Management tracking is stored in:
``~/.clawsomeflow/.system/openclaw-managed-agents.json``
so we never write unknown keys into OpenClaw's schema.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import logging_setup, paths
from app.concurrency import LockManager, get_lock_manager
from app.config import Config, load_config
from app.fileutil import atomic_write_json, file_locked


_MANAGED_REGISTRY_FILENAME = "openclaw-managed-agents.json"
_INVALID_MANAGED_AGENT_KEYS = frozenset({"description", "_managed_by", "timeoutSeconds"})
_GATEWAY_CONFIG_CALL_TIMEOUT_SEC = 8.0
_GATEWAY_CONFIG_CALL_TIMEOUT_MS = max(int(_GATEWAY_CONFIG_CALL_TIMEOUT_SEC * 1000), 1000)
_GATEWAY_CONFIG_GET_METHOD = "config.get"
_GATEWAY_CONFIG_SET_METHOD = "config.set"
_GATEWAY_CONFIG_TIMEOUT_ARG = str(_GATEWAY_CONFIG_CALL_TIMEOUT_MS)
_logger = logging_setup.get_logger("openclaw_json")


class OpenclawJsonError(Exception):
    """Raised when openclaw.json is missing or malformed."""


# ──────────────────────────────────────────────────────────────────────
# Path resolution
# ──────────────────────────────────────────────────────────────────────


def openclaw_json_path(config: Config | None = None) -> Path:
    """Resolve ``~/.openclaw/openclaw.json`` honouring :class:`Config`."""
    cfg = config or load_config()
    return cfg.openclaw_home_path / "openclaw.json"


def openclaw_json_backup_path(config: Config | None = None) -> Path:
    """Path of the ``openclaw.json.bak`` we maintain (mirrors OpenClaw's own .bak)."""
    cfg = config or load_config()
    return cfg.openclaw_home_path / "openclaw.json.bak"


def managed_registry_path() -> Path:
    """Path to the managed-agent registry under ``~/.clawsomeflow/.system/``."""
    return paths.system_dir() / _MANAGED_REGISTRY_FILENAME


# ──────────────────────────────────────────────────────────────────────
# Pure I/O (no locking — caller's job)
# ──────────────────────────────────────────────────────────────────────


def load_openclaw_json(config: Config | None = None) -> dict[str, Any]:
    """Read + parse openclaw.json (no lock; for read-only paths)."""
    path = openclaw_json_path(config)
    if not path.exists():
        raise OpenclawJsonError(f"openclaw.json not found at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenclawJsonError(f"openclaw.json is not valid JSON: {exc}") from exc


def _should_sync_gateway_config() -> bool:
    platform = sys.platform
    return platform == "darwin" or platform.startswith("linux")


def _openclaw_runtime_env(*, config: Config) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(openclaw_json_path(config))
    env.setdefault("OPENCLAW_STATE_DIR", str(config.openclaw_home_path))
    return env


def _parse_json_object_from_mixed_output(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start >= 0:
        try:
            parsed, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    return None


def _run_gateway_call(
    *,
    executable: str,
    method: str,
    params: dict[str, Any],
    config: Config,
) -> tuple[bool, dict[str, Any] | None, str]:
    params_raw = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
    argv = [
        executable,
        "gateway",
        "call",
        method,
        "--json",
        "--params",
        params_raw,
        "--timeout",
        _GATEWAY_CONFIG_TIMEOUT_ARG,
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GATEWAY_CONFIG_CALL_TIMEOUT_SEC,
            env=_openclaw_runtime_env(config=config),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, None, str(exc)
    merged = "\n".join(
        part for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if part
    ).strip()
    payload = _parse_json_object_from_mixed_output(merged)
    if proc.returncode != 0:
        return False, payload, merged or f"exit code {proc.returncode}"
    if payload is None:
        return False, None, merged or "gateway call returned non-JSON payload"
    return True, payload, ""


def _extract_gateway_base_hash(payload: dict[str, Any]) -> str | None:
    def _pick_hash(node: dict[str, Any]) -> str | None:
        for key in ("hash", "baseHash", "configHash"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    direct = _pick_hash(payload)
    if direct:
        return direct

    stack: list[dict[str, Any]] = [payload]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        hash_value = _pick_hash(node)
        if hash_value:
            return hash_value
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
    return None


def _gateway_set_payload_ok(payload: dict[str, Any]) -> bool:
    saw_positive = False
    stack: list[dict[str, Any]] = [payload]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        node_id = id(node)
        if node_id in seen:
            continue
        seen.add(node_id)
        for key in ("ok", "success"):
            value = node.get(key)
            if isinstance(value, bool):
                if value is False:
                    return False
                saw_positive = True
        error = node.get("error")
        if isinstance(error, str) and error.strip():
            return False
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
    return True if saw_positive else True


def _sync_gateway_config_unlocked(
    data: dict[str, Any],
    *,
    config: Config | None = None,
) -> bool:
    if not _should_sync_gateway_config():
        return False
    try:
        from app.integrations.openclaw_cli import resolve_openclaw_executable

        cfg = config or load_config()
        executable = resolve_openclaw_executable()
        if not executable:
            return False
        ok, get_payload, get_detail = _run_gateway_call(
            executable=executable,
            method=_GATEWAY_CONFIG_GET_METHOD,
            params={},
            config=cfg,
        )
        if not ok or not isinstance(get_payload, dict):
            _logger.warning(
                "openclaw_gateway_config_get_failed",
                detail=(get_detail or "unknown")[:500],
            )
            return False
        base_hash = _extract_gateway_base_hash(get_payload)
        if not base_hash:
            _logger.warning("openclaw_gateway_config_get_missing_base_hash")
            return False
        raw_json = json.dumps(data, ensure_ascii=False, indent=2)
        ok, set_payload, set_detail = _run_gateway_call(
            executable=executable,
            method=_GATEWAY_CONFIG_SET_METHOD,
            params={"raw": raw_json, "baseHash": base_hash},
            config=cfg,
        )
        if not ok or not isinstance(set_payload, dict):
            _logger.warning(
                "openclaw_gateway_config_set_failed",
                detail=(set_detail or "unknown")[:500],
            )
            return False
        if not _gateway_set_payload_ok(set_payload):
            _logger.warning(
                "openclaw_gateway_config_set_rejected",
                payload=json.dumps(set_payload, ensure_ascii=False)[:500],
            )
            return False
        _logger.info("openclaw_gateway_config_set_applied")
        return True
    except Exception as exc:
        _logger.warning("openclaw_gateway_config_sync_exception", error=str(exc)[:500])
        return False


def save_openclaw_json_unlocked(
    data: dict[str, Any], *, config: Config | None = None,
) -> None:
    """Persist config (gateway sync on macOS, plus atomic file write backup)."""
    cfg = config or load_config()
    _sync_gateway_config_unlocked(data, config=cfg)
    path = openclaw_json_path(cfg)
    backup = openclaw_json_backup_path(cfg)
    if path.exists():
        shutil.copy2(path, backup)
    atomic_write_json(path, data, indent=2)


# ──────────────────────────────────────────────────────────────────────
# Lock-aware helpers (recommended call points)
# ──────────────────────────────────────────────────────────────────────


_LOCK_KEY = "openclaw_json"


def _is_clawsomeflow_managed_workspace(agent: dict[str, Any]) -> bool:
    """Heuristic fallback: workspace points to ``~/.clawsomeflow/agents/<id>/workspace``."""
    aid = agent.get("id")
    workspace = agent.get("workspace")
    if not isinstance(aid, str) or not aid.strip():
        return False
    if not isinstance(workspace, str) or not workspace.strip():
        return False
    expected = (
        paths.clawsomeflow_home_path()
        / "agents"
        / aid
        / "workspace"
    ).expanduser().resolve(strict=False)
    actual = Path(workspace).expanduser().resolve(strict=False)
    return actual == expected


def _load_managed_agent_ids_unlocked() -> set[str]:
    path = managed_registry_path()
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenclawJsonError(f"managed-agent registry is invalid JSON: {exc}") from exc
    ids = payload.get("agent_ids", [])
    if not isinstance(ids, list):
        raise OpenclawJsonError("managed-agent registry field 'agent_ids' must be a list")
    out: set[str] = set()
    for raw in ids:
        if isinstance(raw, str) and raw:
            out.add(raw)
    return out


def _save_managed_agent_ids_unlocked(agent_ids: set[str]) -> None:
    atomic_write_json(
        managed_registry_path(),
        {"agent_ids": sorted(agent_ids)},
        indent=2,
    )


def _strip_invalid_managed_agent_keys(agent_entry: dict[str, Any]) -> list[str]:
    """Drop legacy keys that are rejected by current OpenClaw schema."""
    removed: list[str] = []
    for key in sorted(_INVALID_MANAGED_AGENT_KEYS):
        if key in agent_entry:
            agent_entry.pop(key, None)
            removed.append(key)
    return removed


def list_managed_agent_ids() -> list[str]:
    """Return managed agent ids from the ClawsomeFlow registry."""
    return sorted(_load_managed_agent_ids_unlocked())


async def update_openclaw_json(
    mutator: Callable[[dict[str, Any]], None | Awaitable[None]],
    *,
    config: Config | None = None,
    locks: LockManager | None = None,
    operation: str = "update",
    agent_id: str | None = None,
) -> dict[str, Any]:
    """Read → mutate (via callback) → write atomically under the global lock.

    The mutator may be sync or async. Returns the final on-disk dict.
    ``operation`` / ``agent_id`` are forwarded to the standard
    ``openclaw_json_modify`` log event (DEV.md §7).
    """
    cfg = config or load_config()
    lm = locks or get_lock_manager(cfg)
    async with lm.lock(_LOCK_KEY):
        data = load_openclaw_json(cfg)
        result = mutator(data)
        if result is not None:
            await result  # awaitable mutator
        await asyncio.to_thread(save_openclaw_json_unlocked, data, config=cfg)
        logging_setup.openclaw_json_modify(
            operation=operation,
            agent_id=agent_id,
            agent_count=len(data.get("agents", {}).get("list", [])),
        )
        return data


async def sanitize_managed_agent_entries(
    *,
    config: Config | None = None,
    locks: LockManager | None = None,
) -> dict[str, list[str]]:
    """Remove legacy invalid keys from managed entries in ``openclaw.json``."""
    cfg = config or load_config()
    lm = locks or get_lock_manager(cfg)
    async with lm.lock(_LOCK_KEY):
        with file_locked(managed_registry_path()):
            managed_ids = _load_managed_agent_ids_unlocked()
        data = load_openclaw_json(cfg)
        agents = data.get("agents", {}).get("list", [])
        if not isinstance(agents, list):
            return {}
        removed_by_agent: dict[str, list[str]] = {}
        for item in agents:
            if not isinstance(item, dict):
                continue
            aid = item.get("id")
            if not isinstance(aid, str) or aid not in managed_ids:
                continue
            removed = _strip_invalid_managed_agent_keys(item)
            if removed:
                removed_by_agent[aid] = removed
        if not removed_by_agent:
            return {}
        await asyncio.to_thread(save_openclaw_json_unlocked, data, config=cfg)
        logging_setup.openclaw_json_modify(
            operation="sanitize_managed_agent_entries",
            agent_id=None,
            agent_count=len(agents),
        )
        return removed_by_agent


# ──────────────────────────────────────────────────────────────────────
# Managed-agent helpers (for installer + uninstall)
# ──────────────────────────────────────────────────────────────────────


def list_managed_agents(config: Config | None = None) -> list[dict[str, Any]]:
    """Return agents tracked by ClawsomeFlow's managed-agent registry."""
    data = load_openclaw_json(config)
    managed_ids = set(_load_managed_agent_ids_unlocked())
    agents = data.get("agents", {}).get("list", [])
    return [a for a in agents if a.get("id") in managed_ids]


def has_managed_agent(agent_id: str, config: Config | None = None) -> bool:
    """Return True if *agent_id* is present in the managed-agent registry."""
    del config  # reserved for symmetric signature with other helpers
    return agent_id in _load_managed_agent_ids_unlocked()


def find_agent(agent_id: str, config: Config | None = None) -> dict[str, Any] | None:
    """Return the raw agent entry (regardless of management) or None."""
    data = load_openclaw_json(config)
    for a in data.get("agents", {}).get("list", []):
        if a.get("id") == agent_id:
            return a
    return None


# ──────────────────────────────────────────────────────────────────────
# High-level mutators (build on update_openclaw_json)
# ──────────────────────────────────────────────────────────────────────


async def append_managed_agent(
    agent_entry: dict[str, Any],
    *,
    config: Config | None = None,
    locks: LockManager | None = None,
) -> dict[str, Any]:
    """Append a new managed agent entry and register its id.

    Refuses to overwrite an existing agent (raises :class:`OpenclawJsonError`).
    """
    if "id" not in agent_entry:
        raise OpenclawJsonError("agent_entry must include an 'id' field")
    aid = agent_entry["id"]
    cfg = config or load_config()
    lm = locks or get_lock_manager(cfg)
    async with lm.lock(_LOCK_KEY):
        data = load_openclaw_json(cfg)
        agents = data.setdefault("agents", {}).setdefault("list", [])
        for existing in agents:
            if existing.get("id") == aid:
                raise OpenclawJsonError(f"agent {aid!r} already exists in openclaw.json")
        entry_copy = dict(agent_entry)
        _strip_invalid_managed_agent_keys(entry_copy)
        agents.append(entry_copy)
        await asyncio.to_thread(save_openclaw_json_unlocked, data, config=cfg)
        with file_locked(managed_registry_path()):
            managed_ids = _load_managed_agent_ids_unlocked()
            managed_ids.add(aid)
            _save_managed_agent_ids_unlocked(managed_ids)
        logging_setup.openclaw_json_modify(
            operation="append_managed_agent",
            agent_id=aid,
            agent_count=len(agents),
        )
        return data


async def remove_managed_agent(
    agent_id: str,
    *,
    config: Config | None = None,
    locks: LockManager | None = None,
) -> bool:
    """Remove a single managed agent. Returns True if it existed and was removed."""
    cfg = config or load_config()
    lm = locks or get_lock_manager(cfg)
    async with lm.lock(_LOCK_KEY):
        with file_locked(managed_registry_path()):
            managed_ids = _load_managed_agent_ids_unlocked()
            if agent_id not in managed_ids:
                raise OpenclawJsonError(
                    f"refusing to remove agent {agent_id!r}: not managed by ClawsomeFlow"
                )
        data = load_openclaw_json(cfg)
        agents = data.get("agents", {}).get("list", [])
        new_list: list[dict[str, Any]] = []
        removed = False
        for a in agents:
            if a.get("id") == agent_id:
                removed = True
                continue
            new_list.append(a)
        data.setdefault("agents", {})["list"] = new_list
        await asyncio.to_thread(save_openclaw_json_unlocked, data, config=cfg)
        with file_locked(managed_registry_path()):
            managed_ids = _load_managed_agent_ids_unlocked()
            managed_ids.discard(agent_id)
            _save_managed_agent_ids_unlocked(managed_ids)
        logging_setup.openclaw_json_modify(
            operation="remove_managed_agent",
            agent_id=agent_id,
            agent_count=len(new_list),
        )
        return removed


async def remove_all_managed_agents(
    *,
    config: Config | None = None,
    locks: LockManager | None = None,
) -> list[str]:
    """Drop every managed agent recorded by ClawsomeFlow. Returns removed ids."""
    cfg = config or load_config()
    lm = locks or get_lock_manager(cfg)
    async with lm.lock(_LOCK_KEY):
        with file_locked(managed_registry_path()):
            managed_ids = _load_managed_agent_ids_unlocked()
        data = load_openclaw_json(cfg)
        agents = data.get("agents", {}).get("list", [])
        kept: list[dict[str, Any]] = []
        removed: list[str] = []
        for a in agents:
            aid = a.get("id")
            if aid in managed_ids or _is_clawsomeflow_managed_workspace(a):
                if isinstance(aid, str):
                    removed.append(aid)
                continue
            kept.append(a)
        data.setdefault("agents", {})["list"] = kept
        await asyncio.to_thread(save_openclaw_json_unlocked, data, config=cfg)
        with file_locked(managed_registry_path()):
            _save_managed_agent_ids_unlocked(set())
        logging_setup.openclaw_json_modify(
            operation="remove_all_managed_agents",
            agent_id=None,
            agent_count=len(kept),
        )
        return removed


async def mark_agent_managed(
    agent_id: str,
    *,
    config: Config | None = None,
    locks: LockManager | None = None,
) -> None:
    """Mark an existing openclaw agent id as ClawsomeFlow-managed."""
    cfg = config or load_config()
    lm = locks or get_lock_manager(cfg)
    async with lm.lock(_LOCK_KEY):
        with file_locked(managed_registry_path()):
            managed_ids = _load_managed_agent_ids_unlocked()
            managed_ids.add(agent_id)
            _save_managed_agent_ids_unlocked(managed_ids)
        logging_setup.openclaw_json_modify(
            operation="mark_managed_agent",
            agent_id=agent_id,
            agent_count=len(load_openclaw_json(cfg).get("agents", {}).get("list", [])),
        )


def mark_agent_managed_sync(
    agent_id: str,
    *,
    config: Config | None = None,
) -> None:
    """Mark an existing openclaw agent id as ClawsomeFlow-managed (sync path).

    Used by sync request paths (e.g. reindex) that cannot await
    :func:`mark_agent_managed`.
    """
    if not isinstance(agent_id, str) or not agent_id:
        raise OpenclawJsonError("agent_id must be a non-empty string")
    cfg = config or load_config()
    path = managed_registry_path()
    with file_locked(path):
        managed_ids = _load_managed_agent_ids_unlocked()
        if agent_id in managed_ids:
            return
        managed_ids.add(agent_id)
        _save_managed_agent_ids_unlocked(managed_ids)
        logging_setup.openclaw_json_modify(
            operation="mark_managed_agent_sync",
            agent_id=agent_id,
            agent_count=len(load_openclaw_json(cfg).get("agents", {}).get("list", [])),
        )


__all__ = [
    "OpenclawJsonError",
    "openclaw_json_path",
    "openclaw_json_backup_path",
    "managed_registry_path",
    "load_openclaw_json",
    "save_openclaw_json_unlocked",
    "update_openclaw_json",
    "list_managed_agent_ids",
    "list_managed_agents",
    "has_managed_agent",
    "find_agent",
    "append_managed_agent",
    "remove_managed_agent",
    "remove_all_managed_agents",
    "sanitize_managed_agent_entries",
    "mark_agent_managed",
    "mark_agent_managed_sync",
]
