"""OpenClaw install + uninstall hook for ClawsomeFlow.

Current policy:

1. Seed bundled skills into ``~/.clawsomeflow/.skills-source/``.
2. Deploy common agent payload + global tool bundle into
   ``~/.clawsomeflow/.common-agent-source/`` and
   ``~/.clawsomeflow/.clawsomeflow-agent-tools/``.
3. Ensure gateway chat endpoint and runtime timeout defaults are configured.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app import paths
from app.config import Config, load_config, save_config
from app.integrations.internal_token import (
    ensure_api_token_initialised,
    ensure_secret_initialised,
)
from app.integrations.openclaw_agent_source import (
    deploy_agent_tools_bundle,
    deploy_common_agent_source,
)
from app.integrations.openclaw_json import (
    list_managed_agents,
    openclaw_json_path,
    remove_all_managed_agents,
    sanitize_managed_agent_entries,
    update_openclaw_json,
)
from app.integrations.openclaw_skills import seed_skills_source
from app.logging_setup import get_logger

logger = get_logger("openclaw_install")

_SCOPE_REPAIR_DISABLE_ENV = "CSFLOW_DISABLE_OPENCLAW_SCOPE_REPAIR"
_DEFAULT_AGENT_TIMEOUT_SECONDS = 1800
_DEFAULT_EXEC_TIMEOUT_SECONDS = 1800
_SCOPE_APPROVAL_HINTS = (
    "scope upgrade pending approval",
    "needs approval",
    "pairing required",
    "missing scope: operator.admin",
)
_DEFAULT_SCOPE_REPAIR_MAX_ATTEMPTS = 3
_DEFAULT_SCOPE_REPAIR_SLEEP_SECONDS = 1.0


def looks_like_pending_scope_approval(detail: str) -> bool:
    lowered = (detail or "").lower()
    return any(hint in lowered for hint in _SCOPE_APPROVAL_HINTS)


@dataclass(frozen=True)
class InstallResult:
    common_agent_source_deployed_to: Path
    agent_tools_deployed_to: Path
    skills_seeded_to: Path
    gateway_chat_endpoint_enabled: bool


@dataclass(frozen=True)
class UninstallResult:
    agents_removed: list[str]
    workspaces_removed: list[Path]
    purged_data_dir: bool


def _parse_json_from_mixed_output(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    first_obj = text.find("{")
    if first_obj < 0:
        return None
    try:
        parsed = json.loads(text[first_obj:])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _openclaw_cli_env(config: Config) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(openclaw_json_path(config))
    env.setdefault("OPENCLAW_STATE_DIR", str(config.openclaw_home_path))
    return env


def _pending_scope_repair_request_ids(payload: dict[str, Any]) -> list[str]:
    pending = payload.get("pending")
    if not isinstance(pending, list):
        return []
    strict: list[str] = []
    fallback: list[tuple[str, float]] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        request_id = item.get("requestId")
        if not isinstance(request_id, str) or not request_id.strip():
            continue
        requested_at = item.get("requestedAt")
        sort_key = 0.0
        if isinstance(requested_at, str) and requested_at.strip():
            normalized = requested_at.strip().replace("Z", "+00:00")
            try:
                sort_key = datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                sort_key = 0.0
        fallback.append((request_id, sort_key))
        if item.get("isRepair") is not True:
            continue
        if item.get("clientId") != "cli":
            continue
        strict.append(request_id)
    if strict:
        return list(dict.fromkeys(strict))
    if not fallback:
        return []
    fallback.sort(key=lambda x: x[1], reverse=True)
    return [fallback[0][0]]


def _repair_pending_scope_upgrades(
    *,
    config: Config,
    max_attempts: int = _DEFAULT_SCOPE_REPAIR_MAX_ATTEMPTS,
    sleep_seconds: float = _DEFAULT_SCOPE_REPAIR_SLEEP_SECONDS,
) -> list[str]:
    if os.getenv(_SCOPE_REPAIR_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.info("openclaw_scope_repair_skipped", reason=f"{_SCOPE_REPAIR_DISABLE_ENV}=true")
        return []

    executable = shutil.which("openclaw")
    if not executable:
        return []

    attempts = max(int(max_attempts), 1)
    approved: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            listed = subprocess.run(
                [executable, "devices", "list", "--json", "--timeout", "1500"],
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
                env=_openclaw_cli_env(config),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "openclaw_scope_repair_list_failed",
                attempt=attempt,
                total_attempts=attempts,
                error=str(exc),
            )
            if attempt < attempts and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            continue

        payload = _parse_json_from_mixed_output(listed.stdout)
        if payload is None:
            logger.warning(
                "openclaw_scope_repair_list_non_json",
                attempt=attempt,
                total_attempts=attempts,
            )
            if attempt < attempts and sleep_seconds > 0:
                time.sleep(sleep_seconds)
            continue
        request_ids = _pending_scope_repair_request_ids(payload)
        if not request_ids:
            logger.info(
                "openclaw_scope_repair_no_pending",
                attempt=attempt,
                total_attempts=attempts,
            )
            break
        logger.info(
            "openclaw_scope_repair_attempt",
            attempt=attempt,
            total_attempts=attempts,
            request_ids=request_ids,
        )
        approved_this_round: list[str] = []
        for request_id in request_ids:
            try:
                proc = subprocess.run(
                    [executable, "devices", "approve", request_id, "--json", "--timeout", "1500"],
                    capture_output=True,
                    text=True,
                    timeout=4.0,
                    check=False,
                    env=_openclaw_cli_env(config),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning(
                    "openclaw_scope_repair_approve_failed",
                    attempt=attempt,
                    request_id=request_id,
                    error=str(exc),
                )
                continue
            if proc.returncode == 0:
                approved_this_round.append(request_id)
                continue
            logger.warning(
                "openclaw_scope_repair_approve_nonzero",
                attempt=attempt,
                request_id=request_id,
                exit_code=proc.returncode,
                detail=(proc.stderr or proc.stdout or "").strip()[:240],
            )
        if approved_this_round:
            approved.extend(approved_this_round)
            logger.info(
                "openclaw_scope_repair_approved",
                attempt=attempt,
                approved_request_ids=approved_this_round,
                approved_count=len(approved_this_round),
            )
            break
        if attempt < attempts and sleep_seconds > 0:
            time.sleep(sleep_seconds)
    logger.info(
        "openclaw_scope_repair_complete",
        approved_request_ids=approved,
        approved_count=len(approved),
        attempts=attempts,
    )
    return approved


def repair_pending_scope_upgrades(
    *,
    config: Config | None = None,
    max_attempts: int = _DEFAULT_SCOPE_REPAIR_MAX_ATTEMPTS,
    sleep_seconds: float = _DEFAULT_SCOPE_REPAIR_SLEEP_SECONDS,
) -> list[str]:
    cfg = config or load_config()
    return _repair_pending_scope_upgrades(
        config=cfg,
        max_attempts=max_attempts,
        sleep_seconds=sleep_seconds,
    )


async def _ensure_runtime_timeout_defaults(*, config: Config) -> None:
    def _mut(data: dict[str, Any]) -> None:
        agents = data.get("agents")
        if agents is None:
            agents = {}
            data["agents"] = agents
        if not isinstance(agents, dict):
            raise RuntimeError("openclaw.json field 'agents' must be an object")

        defaults = agents.get("defaults")
        if defaults is None:
            defaults = {}
            agents["defaults"] = defaults
        if not isinstance(defaults, dict):
            raise RuntimeError("openclaw.json field 'agents.defaults' must be an object")
        defaults["timeoutSeconds"] = _DEFAULT_AGENT_TIMEOUT_SECONDS

        tools = data.get("tools")
        if tools is None:
            tools = {}
            data["tools"] = tools
        if not isinstance(tools, dict):
            raise RuntimeError("openclaw.json field 'tools' must be an object")

        exec_cfg = tools.get("exec")
        if exec_cfg is None:
            exec_cfg = {}
            tools["exec"] = exec_cfg
        if not isinstance(exec_cfg, dict):
            raise RuntimeError("openclaw.json field 'tools.exec' must be an object")
        exec_cfg["timeoutSec"] = _DEFAULT_EXEC_TIMEOUT_SECONDS

    await update_openclaw_json(
        _mut,
        config=config,
        operation="ensure_runtime_timeout_defaults",
    )


async def ensure_runtime_timeout_defaults(*, config: Config | None = None) -> None:
    cfg = config or load_config()
    await _ensure_runtime_timeout_defaults(config=cfg)


async def _ensure_gateway_chat_completions_enabled(*, config: Config) -> bool:
    def _require_secure_gateway_settings(data: dict[str, Any]) -> None:
        gateway = data.get("gateway")
        if not isinstance(gateway, dict):
            raise RuntimeError(
                "openclaw.json missing object field 'gateway'; refuse to enable chat endpoint"
            )
        bind = gateway.get("bind")
        if bind != "loopback":
            raise RuntimeError(
                "refuse to enable gateway chat endpoint: gateway.bind must be 'loopback'"
            )
        auth = gateway.get("auth")
        if not isinstance(auth, dict):
            raise RuntimeError(
                "openclaw.json missing object field 'gateway.auth'; refuse to enable chat endpoint"
            )
        mode = auth.get("mode")
        if not isinstance(mode, str) or not mode:
            raise RuntimeError(
                "refuse to enable gateway chat endpoint: gateway.auth.mode is required"
            )
        if mode == "none":
            raise RuntimeError(
                "refuse to enable gateway chat endpoint when gateway.auth.mode='none'"
            )
        if mode == "token":
            token = auth.get("token")
            if not isinstance(token, str) or not token.strip():
                raise RuntimeError(
                    "refuse to enable gateway chat endpoint: gateway.auth.token is empty"
                )

    def _mut(data: dict[str, Any]) -> None:
        _require_secure_gateway_settings(data)
        gateway = data["gateway"]

        http_cfg = gateway.get("http")
        if http_cfg is None:
            http_cfg = {}
            gateway["http"] = http_cfg
        if not isinstance(http_cfg, dict):
            raise RuntimeError("openclaw.json field 'gateway.http' must be an object")

        endpoints = http_cfg.get("endpoints")
        if endpoints is None:
            endpoints = {}
            http_cfg["endpoints"] = endpoints
        if not isinstance(endpoints, dict):
            raise RuntimeError("openclaw.json field 'gateway.http.endpoints' must be an object")

        chat = endpoints.get("chatCompletions")
        if chat is None:
            chat = {}
            endpoints["chatCompletions"] = chat
        if not isinstance(chat, dict):
            raise RuntimeError(
                "openclaw.json field 'gateway.http.endpoints.chatCompletions' must be an object"
            )

        images = chat.get("images")
        if images is None:
            images = {}
            chat["images"] = images
        if not isinstance(images, dict):
            raise RuntimeError(
                "openclaw.json field 'gateway.http.endpoints.chatCompletions.images' must be an object"
            )

        chat["enabled"] = True
        images["allowUrl"] = False

    await update_openclaw_json(
        _mut,
        config=config,
        operation="enable_gateway_chat_completions",
    )
    return True


async def install_into_openclaw(
    *,
    config: Config | None = None,
) -> InstallResult:
    cfg = config or load_config()

    cfg_with_secret = ensure_secret_initialised(cfg)
    cfg_with_secret = ensure_api_token_initialised(cfg_with_secret)
    if cfg_with_secret is not cfg:
        save_config(cfg_with_secret)
        cfg = cfg_with_secret

    seeded = seed_skills_source()
    common_source_deployed = deploy_common_agent_source()
    tools_deployed = deploy_agent_tools_bundle()
    gateway_chat_enabled = await _ensure_gateway_chat_completions_enabled(config=cfg)

    await sanitize_managed_agent_entries(config=cfg)
    await _ensure_runtime_timeout_defaults(config=cfg)
    repaired_scope_requests = _repair_pending_scope_upgrades(config=cfg)
    if repaired_scope_requests:
        logger.info(
            "openclaw_scope_repair_applied",
            request_ids=repaired_scope_requests,
            count=len(repaired_scope_requests),
        )

    logger.info(
        "openclaw_install_complete",
        common_agent_source_deployed_to=str(common_source_deployed),
        agent_tools_deployed_to=str(tools_deployed),
        skills_seeded_to=str(seeded),
        gateway_chat_endpoint_enabled=gateway_chat_enabled,
    )

    return InstallResult(
        common_agent_source_deployed_to=common_source_deployed,
        agent_tools_deployed_to=tools_deployed,
        skills_seeded_to=seeded,
        gateway_chat_endpoint_enabled=gateway_chat_enabled,
    )


async def uninstall_from_openclaw(
    *,
    purge_data_dir: bool = False,
    config: Config | None = None,
) -> UninstallResult:
    cfg = config or load_config()
    removed_agents = await remove_all_managed_agents(config=cfg)

    workspaces_removed: list[Path] = []

    purged = False
    if purge_data_dir:
        home = paths.clawsomeflow_home_path()
        if home.exists():
            shutil.rmtree(home)
            purged = True

    logger.info(
        "openclaw_uninstall_complete",
        agents_removed=removed_agents,
        workspaces_removed=[str(p) for p in workspaces_removed],
        purged=purged,
    )

    return UninstallResult(
        agents_removed=removed_agents,
        workspaces_removed=workspaces_removed,
        purged_data_dir=purged,
    )


def install_summary(config: Config | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    try:
        managed = list_managed_agents(cfg)
        managed_ids = [a.get("id") for a in managed]
    except Exception as exc:
        managed_ids = [f"<error: {exc}>"]

    return {
        "managed_agents_in_openclaw": managed_ids,
        "skills_source_dir": str(paths.skills_source_dir()),
        "common_agent_source_dir": str(paths.common_agent_source_dir()),
        "openclaw_agent_tools_dir": str(paths.openclaw_agent_tools_dir()),
    }


__all__ = [
    "InstallResult",
    "UninstallResult",
    "install_into_openclaw",
    "install_summary",
    "ensure_runtime_timeout_defaults",
    "looks_like_pending_scope_approval",
    "repair_pending_scope_upgrades",
    "uninstall_from_openclaw",
]
