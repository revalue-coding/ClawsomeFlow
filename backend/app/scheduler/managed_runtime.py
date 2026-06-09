"""Deterministic runtime helpers for env-home managed agents (claude/codex/cursor).

These agents carry their identity/skills/MCP in a relocatable config home that
ClawTeam injects at spawn via a runtime profile's ``--env``. Both the management
service (at create time) and the scheduler (at spawn time) need the SAME
deterministic home path + profile name, so they live here with NO DB dependency.

Verified mechanism (DEV.md):
* ``clawteam profile set <name> --agent <cli> --env <VAR>=<home>`` stores the env.
* At spawn, ClawTeam writes the profile env into a sourced ``.env.sh`` inside the
  tmux pane, so ``<VAR>`` reaches the CLI regardless of the running tmux server.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app import paths as app_paths
from app.logging_setup import get_logger

logger = get_logger("scheduler.managed_runtime")

# kind → environment variable that relocates that CLI's config home.
HOME_ENV_VAR: dict[str, str] = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "cursor": "CURSOR_CONFIG_DIR",
}

# kind → the CLI binary name (cursor's CLI binary is ``agent``).
KIND_CLI: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "cursor": "agent",
}

MANAGED_KINDS = frozenset(HOME_ENV_VAR)


def is_managed_kind(kind: str) -> bool:
    return kind in MANAGED_KINDS


def managed_home(kind: str, agent_id: str) -> Path:
    """``~/.clawsomeflow/agents/{id}/{kind}-home`` — the relocated config home."""
    return app_paths.agent_dir(agent_id) / f"{kind}-home"


def managed_profile_name(kind: str, agent_id: str) -> str:
    return f"csflow-{kind}-{agent_id}"


def _clawteam() -> str | None:
    return shutil.which("clawteam")


def ensure_profile(kind: str, agent_id: str, *, timeout: float = 30.0) -> str:
    """Idempotently create/update the ClawTeam profile that injects the config
    home env var, creating the home dir first (Codex refuses a missing home).

    Returns the profile name. Best-effort: logs and returns the name even if the
    CLI call fails, so a spawn degrades to "no extra tools" rather than crashing.
    """
    home = managed_home(kind, agent_id)
    home.mkdir(parents=True, exist_ok=True)
    name = managed_profile_name(kind, agent_id)
    var = HOME_ENV_VAR.get(kind)
    cli = KIND_CLI.get(kind, kind)
    exe = _clawteam()
    if exe is None or var is None:
        return name
    try:
        proc = subprocess.run(  # noqa: S603 — constructed args
            [exe, "profile", "set", name, "--agent", cli, "--env", f"{var}={home}"],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning(
                "managed_profile_set_failed", kind=kind, agent_id=agent_id,
                error=(proc.stderr or proc.stdout or "").strip()[:300],
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("managed_profile_set_error", kind=kind, agent_id=agent_id, error=str(exc))
    return name


def remove_profile(kind: str, agent_id: str, *, timeout: float = 30.0) -> None:
    """Best-effort removal of the ClawTeam profile."""
    exe = _clawteam()
    if exe is None:
        return
    name = managed_profile_name(kind, agent_id)
    try:
        subprocess.run(  # noqa: S603
            [exe, "profile", "remove", name],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("managed_profile_remove_error", kind=kind, agent_id=agent_id, error=str(exc))


__all__ = [
    "HOME_ENV_VAR",
    "KIND_CLI",
    "MANAGED_KINDS",
    "is_managed_kind",
    "managed_home",
    "managed_profile_name",
    "ensure_profile",
    "remove_profile",
]
