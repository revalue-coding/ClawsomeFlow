"""Business logic for ClawsomeFlow-managed OpenClaw agents.

Owns the **end-to-end lifecycle** of an OpenClaw agent governed by us:

* :func:`commit_agent` — atomically: validate → deploy workspace scaffold →
  git init → install user skills → register in ``openclaw.json`` (and in the
  managed-agent registry) → persist ``OpenclawAgent`` row.
* :func:`update_agent` — patch identity / name / model / description; DB keeps
  the full payload while ``openclaw.json`` only stores keys accepted by
  current OpenClaw schema.
* :func:`delete_agent` — refuse if any non-terminal Run uses this agent;
  otherwise remove from ``openclaw.json``, drop the skill, delete the DB
  row, and (optionally) wipe the workspace directory.
* :func:`get_agent` / :func:`list_agents` — read-through helpers.

All write paths are idempotent on the agent id and either succeed
completely or roll back on the filesystem (best-effort) so the system
never ends up with a half-registered agent.

Concurrency:

* The single ``openclaw_json`` lock serialises all openclaw.json mutations.
* The DB layer uses optimistic single-row writes (no version column on
  ``OpenclawAgent`` — last-writer-wins on rare concurrent updates is fine
  because identity edits are rare and reversible).
* ``run_count_active_for_openclaw_agent`` is checked **inside** the
  delete path; the small TOCTOU window vs. a brand-new Run starting at the
  same moment is intentionally tolerated (Run creation will then fail with
  ``OPENCLAW_AGENT_NOT_FOUND`` and the user can retry).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from app import paths
from app.config import Config, load_config
from app.integrations import openclaw_json as oj
from app.integrations.openclaw_agent_source import (
    AGENTS_USER_CUSTOM_SECTION_END,
    AGENTS_USER_CUSTOM_SECTION_START,
    deploy_common_agent_source,
    deploy_common_agent_workspace,
    load_common_rules_text,
)
from app.integrations.openclaw_cli import resolve_openclaw_executable
from app.integrations.openclaw_install import ensure_runtime_timeout_defaults
from app.integrations.openclaw_skills import (
    SkillInstaller,
    discover_user_agent_skills,
    seed_skills_source,
)
from app.logging_setup import get_logger
from app.models import Flow, FlowRun, OpenclawAgent, OpenclawTeam, RunStatus
from app.storage import StorageBackend, get_storage

logger = get_logger("svc.openclaw_agents")
_TEAM_ID_PREFIX = "csfow-group-"
_DELETE_MODE_UNREGISTER = "unregister"
_DELETE_MODE_PURGE = "purge"
_PAGE_SIZE = 200
_IMPORT_TARGET_ID_PREFIX = "csflow-"
_TERMINAL_RUN_STATUSES = {
    RunStatus.completed.value,
    RunStatus.completed_with_conflicts.value,
    RunStatus.complaint_failed.value,
    RunStatus.failed.value,
    RunStatus.aborted.value,
}
_ENTROPY_CRON_NAME_PREFIX = "csflow-entropy-management"
_COMMON_CRON_JOBS_SUBDIR = "cron-jobs"
_OPENCLAW_CRON_SYNC_TIMEOUT_SEC = 15.0
# Strict probe talks HTTP to the gateway's ``/health`` endpoint
# (subprocess-free). Strict is a **fallback safety net** behind the
# sub-second socket fast-path — it must never flip a healthy, running
# gateway to "not running" because of a tight ceiling. Typical roundtrip
# on localhost is single-digit milliseconds; the cap below is many orders
# of magnitude above that, kept only because ``urlopen`` requires a finite
# timeout. Retry exists so even a one-off TCP/HTTP hiccup is forgiven.
_OPENCLAW_RUNTIME_PROBE_TIMEOUT_SEC = 10.0
_OPENCLAW_RUNTIME_PROBE_RETRY_TIMEOUT_SEC = 15.0
_OPENCLAW_RUNTIME_PROBE_SOCKET_TIMEOUT_SEC = 0.12
_OPENCLAW_RUNTIME_HEALTH_PATH = "/health"
_DEFAULT_OPENCLAW_GATEWAY_URL = "http://127.0.0.1:18789"
_FALLBACK_ENTROPY_CRON_EXPR = "0 3 * * 1"
_FALLBACK_ENTROPY_CRON_TZ = "UTC"
_CRON_BACKUP_SNAPSHOT_KEY = "csflow_cron_backup_jobs"
_CRON_BACKUP_CAPTURED_AT_KEY = "csflow_cron_backup_captured_at"
_CRON_BACKUP_RESTORED_AT_KEY = "csflow_cron_backup_restored_at"
_AUTH_PROFILES_FILENAME = "auth-profiles.json"
_PORTABLE_AUTH_PROFILE_TYPES = frozenset({"api_key", "api-key", "token"})
_FALLBACK_ENTROPY_MESSAGE_TEMPLATE = (
    "Run the weekly \"entropy management\" inspection: clean outdated, incorrect, redundant, "
    "or duplicated content in the current workspace; maintain and update each `INDEX.md` under "
    "`my-desktop/` according to the current directory structure (create missing files); output a "
    "concise change summary and include absolute paths of key modified files. Current agent: {agent_id}."
)


# ──────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────


class OpenclawAgentError(Exception):
    """Base class so the API layer can map to ``ApiError``."""

    code: str = "OPENCLAW_AGENT_ERROR"
    status_code: int = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AgentIdInvalid(OpenclawAgentError):
    code = "INVALID_AGENT_ID"


class AgentAlreadyExists(OpenclawAgentError):
    code = "AGENT_EXISTS"
    status_code = 409


class AgentCreateCancelled(OpenclawAgentError):
    code = "AGENT_CREATE_CANCELLED"
    status_code = 409


class AgentNotFound(OpenclawAgentError):
    code = "OPENCLAW_AGENT_NOT_FOUND"
    status_code = 404


class AgentInUse(OpenclawAgentError):
    code = "AGENT_IN_USE"
    status_code = 409


class AgentUnmanaged(OpenclawAgentError):
    """Refuse to mutate an agent NOT created by ClawsomeFlow."""

    code = "AGENT_NOT_MANAGED_BY_CSFLOW"
    status_code = 409


class ExternalAgentNotFound(OpenclawAgentError):
    """Requested runtime agent is not eligible for import."""

    code = "EXTERNAL_AGENT_NOT_FOUND"
    status_code = 404


class ExternalWorkspaceInvalid(OpenclawAgentError):
    """Import source workspace is missing or invalid."""

    code = "EXTERNAL_AGENT_WORKSPACE_INVALID"
    status_code = 409


class TeamNotFound(OpenclawAgentError):
    """Specified team does not exist for current user."""

    code = "OPENCLAW_TEAM_NOT_FOUND"
    status_code = 404


# ──────────────────────────────────────────────────────────────────────
# Team helpers
# ──────────────────────────────────────────────────────────────────────


def _normalize_team_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise OpenclawAgentError("team name is required")
    return value


def _next_team_id(*, storage: StorageBackend) -> str:
    max_n = 0
    for item in storage.openclaw_team_list(owner_user=None):
        if not item.id.startswith(_TEAM_ID_PREFIX):
            continue
        suffix = item.id[len(_TEAM_ID_PREFIX):]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{_TEAM_ID_PREFIX}{max_n + 1:02d}"


def list_teams(
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> list[OpenclawTeam]:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    teams = storage.openclaw_team_list(owner_user=user)
    if not teams:
        return []
    # OpenclawTeam is a ClawsomeFlow grouping shared across agent platforms
    # (OpenClaw + Hermes), so a team is "active" if *any* platform's agent
    # references it. Counting only OpenClaw agents here would wrongly hide a
    # team that holds only Hermes agents — making a freshly-assigned Hermes
    # agent fall back to "ungrouped" and its new group never appear.
    active_team_ids = {
        row.team_id
        for row in storage.openclaw_list(owner_user=user)
        if isinstance(row.team_id, str) and row.team_id.strip()
    }
    active_team_ids |= {
        row.team_id
        for row in storage.hermes_list(owner_user=user)
        if isinstance(row.team_id, str) and row.team_id.strip()
    }
    return [team for team in teams if team.id in active_team_ids]


def create_team(
    name: str,
    *,
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> OpenclawTeam:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    normalized = _normalize_team_name(name)
    # Idempotent by (user, lower(name)) to avoid accidental duplicates.
    for item in storage.openclaw_team_list(owner_user=user):
        if item.name.strip().lower() == normalized.lower():
            return item
    team = OpenclawTeam(
        id=_next_team_id(storage=storage),
        name=normalized,
        created_by_user=user,
    )
    return storage.openclaw_team_create(team)


def update_team(
    team_id: str,
    name: str,
    *,
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> OpenclawTeam:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    row = get_team(team_id, user=user, storage=storage, config=cfg)
    row.name = _normalize_team_name(name)
    return storage.openclaw_team_update(row)


def get_team(
    team_id: str,
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> OpenclawTeam:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    row = storage.openclaw_team_get(team_id)
    if row is None:
        raise TeamNotFound(f"openclaw team {team_id!r} not found")
    if user is not None and row.created_by_user != user:
        raise TeamNotFound(f"openclaw team {team_id!r} not found")
    return row


def _resolve_team_id_for_user(
    *,
    user: str,
    team_id: str | None,
    storage: StorageBackend,
    config: Config,
) -> str:
    raw_team_id = (team_id or "").strip()
    if not raw_team_id:
        # Empty means "ungrouped".
        return ""
    team = get_team(raw_team_id, user=user, storage=storage, config=config)
    return team.id


# ──────────────────────────────────────────────────────────────────────
# Inputs
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgentIdentity:
    """Identity block stored under ``identity`` in openclaw.json."""

    emoji: str | None = None
    theme: str | None = None
    display_name: str | None = None  # mirrors `name` if absent

    def to_openclaw_dict(self, name: str) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.display_name or name}
        if self.emoji:
            out["emoji"] = self.emoji
        if self.theme:
            out["theme"] = self.theme
        return out


@dataclass(frozen=True)
class CommitInput:
    """Validated input for :func:`commit_agent`.

    Constructed by either the public NL pipeline or the internal API after
    parameter validation; ``id`` MUST already be a valid identifier.
    """

    id: str
    name: str
    description: str = ""
    identity: AgentIdentity = AgentIdentity()
    model: str | None = None
    nl_prompt: str = ""
    extra_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpdateInput:
    """Patch payload for :func:`update_agent`. ``None`` = field unchanged."""

    name: str | None = None
    description: str | None = None
    identity: AgentIdentity | None = None
    model: str | None = None
    team_id: str | None = None


@dataclass(frozen=True)
class ExternalImportCandidate:
    """One unmanaged runtime agent that can be imported into ClawsomeFlow."""

    id: str
    name: str
    description: str
    workspace_path: str


@dataclass(frozen=True)
class ImportedExternalAgent:
    """Result of importing one unmanaged runtime agent."""

    source_agent_id: str
    source_agent_name: str
    target_agent_id: str
    target_agent_name: str
    target_workspace_path: str
    target_team_id: str
    target_team_name: str


@dataclass(frozen=True)
class RestorableAgentCandidate:
    """Agent that exists under ``~/.clawsomeflow`` but is absent in runtime."""

    id: str
    name: str
    description: str
    team_id: str
    workspace_path: str
    created_by_user: str


@dataclass(frozen=True)
class _CommonCronJobDefinition:
    definition_id: str
    name_template: str
    cron_expr: str
    cron_tz: str
    session: str
    message_template: str


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _validate_agent_id(value: str) -> str:
    """Reuse the home-wide identifier rules so the id is filesystem-safe."""
    try:
        return paths.validate_identifier(value, kind="openclaw agent id")
    except ValueError as exc:
        raise AgentIdInvalid(str(exc)) from exc


def _agent_workspace(agent_id: str) -> Path:
    """``~/.clawsomeflow/agents/{id}/workspace/`` (auto-mkdir parent)."""
    base = paths.agent_dir(agent_id)
    base.mkdir(parents=True, exist_ok=True)
    return base / "workspace"


def _git_init_workspace(workspace: Path) -> None:
    """Initialise a fresh git repo at *workspace* with an empty initial commit.

    Idempotent: if ``.git`` already exists, do nothing. The empty commit gives
    ClawTeam something to base its worktrees on (no-commit repos can't be
    branched).
    """
    from app.integrations.git_repo import git_init_repo

    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / ".git").exists():
        return

    def _run(cmd: list[str]) -> None:
        subprocess.run(cmd, cwd=workspace, check=True, capture_output=True)

    git_init_repo(workspace)
    _run(["git", "config", "user.email", "csflow@local"])
    _run(["git", "config", "user.name", "ClawsomeFlow"])
    marker = workspace / ".csflow-keep"
    marker.write_text(
        "This directory is the main workspace of an OpenClaw agent managed by "
        "ClawsomeFlow. Worktree branches live under ~/.clawteam/workspaces/.\n"
    )
    _run(["git", "add", "-A"])
    _run(["git", "commit", "-m", "[csflow] initial commit"])


def _ensure_workspace_templates(workspace: Path, *, agent_id: str, agent_name: str) -> None:
    """Ensure the managed workspace follows OpenClaw file-map conventions."""
    for rel in (
        "memory",
        "my-desktop",
        "skills",
    ):
        (workspace / rel).mkdir(parents=True, exist_ok=True)

    templates = {
        "README.md": (
            f"# {agent_name} Workspace\n\n"
            "This is the OpenClaw agent workspace managed by ClawsomeFlow.\n\n"
            "Core files (OpenClaw workspace map):\n"
            "- `AGENTS.md` operating instructions and memory policy\n"
            "- `SOUL.md` persona and tone\n"
            "- `USER.md` user profile and preferences\n"
            "- `IDENTITY.md` name/role/vibe\n"
            "- `TOOLS.md` local tool conventions\n"
            "- `HEARTBEAT.md` tiny proactive checklist\n"
            "- `MEMORY.md` curated long-term memory\n"
            "- `memory/YYYY-MM-DD.md` daily logs\n"
            "\n"
            f"agent_id: `{agent_id}`\n"
        ),
        "IDENTITY.md": (
            "# IDENTITY.md\n\n"
            f"- id: `{agent_id}`\n"
            f"- name: `{agent_name}`\n"
            "- emoji: 🧭\n"
            "- role: Define your professional role and responsibility scope.\n"
            "- boundaries: Define what you should do and should refuse.\n"
            "- self-intro: One concise introduction style.\n"
        ),
        "SOUL.md": (
            "# SOUL.md\n\n"
            "- personality: Professional, concise, reliable.\n"
            "- mission: Clarify your long-term mission.\n"
            "- non-goals: Clarify what is explicitly out of scope.\n"
            "- principles: Define decision and prioritization principles.\n"
            "- tone: Define speaking tone and collaboration style.\n"
        ),
        "USER.md": (
            "# USER.md\n\n"
            "- user_profile: Who you serve.\n"
            "- communication_preferences: Language/style/verbosity preferences.\n"
            "- deliverable_preferences: Expected output format and quality bar.\n"
            "- constraints: Time/risk/privacy constraints from user.\n"
        ),
        "TOOLS.md": (
            "# TOOLS.md\n\n"
            "- Note: This file documents conventions only; it does NOT grant permissions.\n"
            "- workspace_skills: `./skills/`\n"
            "- global_tools: `~/.clawsomeflow/.clawsomeflow-agent-tools/`\n"
            "- path_conventions: Record important local path conventions.\n"
            "- risky_commands: List commands/actions requiring confirmation.\n"
        ),
        "MEMORY.md": (
            "# MEMORY.md\n\n"
            "Curated long-term memory (not raw transcript).\n\n"
            "## Durable Facts\n"
            "- Keep only high-value, stable facts.\n\n"
            "## Decisions\n"
            "- Record major decisions and rationale.\n\n"
            "## Lessons\n"
            "- Record reusable lessons and anti-patterns.\n"
        ),
        "HEARTBEAT.md": (
            "# HEARTBEAT.md\n\n"
            "Keep this file short. Only include periodic checks that truly need proactive runs.\n\n"
            "- Check overdue commitments and important follow-ups.\n"
            "- Check whether MEMORY.md needs consolidation from daily logs.\n"
            "- If nothing needs action, reply with `HEARTBEAT_OK`.\n"
        ),
        ".env": (
            "# ClawsomeFlow managed agent environment variables.\n"
            "# Add custom KEY=VALUE pairs below.\n"
        ),
        "my-desktop/README.md": (
            "# my-desktop\n\n"
            "Your long-term working area for user-facing materials.\n"
            "Design the internal structure according to your role.\n"
        ),
    }

    for rel, content in templates.items():
        p = workspace / rel
        if not p.exists():
            p.write_text(content, encoding="utf-8")


def _build_openclaw_entry(
    cmd: CommitInput,
    workspace: Path,
    *,
    agent_dir: Path | None = None,
) -> dict[str, Any]:
    """Translate a :class:`CommitInput` into an openclaw.json agent entry.

    Points ``workspace`` at our git-initialised directory.
    """
    entry: dict[str, Any] = {
        "id": cmd.id,
        "name": cmd.name,
        "workspace": str(workspace),
        "default": False,
        "identity": cmd.identity.to_openclaw_dict(cmd.name),
        "heartbeat": {
            "every": "12h",
            "isolatedSession": True,
            "includeSystemPromptSection": False,
        },
        "sandbox": {},
        "tools": {"profile": "coding"},
    }
    if agent_dir is not None:
        entry["agentDir"] = str(agent_dir)
    if cmd.model:
        entry["model"] = cmd.model
    return entry


def _resolve_openclaw_agent_dir(
    *,
    raw: str | None,
    fallback: Path,
    config: Config,
) -> Path:
    candidate = (raw or "").strip()
    if not candidate:
        return fallback
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        return (config.openclaw_home_path / path).resolve(strict=False)
    return path


def _resolve_default_source_agent_id(*, config: Config) -> str:
    """Resolve source agent id for portable auth seeding (OpenClaw-compatible)."""
    fallback = "main"
    try:
        payload = oj.load_openclaw_json(config)
    except oj.OpenclawJsonError:
        return fallback
    agents = payload.get("agents", {}).get("list", [])
    if not isinstance(agents, list):
        return fallback

    first_agent_id: str | None = None
    for item in agents:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        agent_id = raw_id.strip()
        if first_agent_id is None:
            first_agent_id = agent_id
        if item.get("default") is True:
            return agent_id
    return first_agent_id or fallback


def _runtime_agent_dir(*, agent_id: str, config: Config) -> Path:
    fallback = config.openclaw_home_path / "agents" / agent_id / "agent"
    try:
        entry = oj.find_agent(agent_id, config)
    except oj.OpenclawJsonError:
        return fallback
    if not isinstance(entry, dict):
        return fallback
    raw_agent_dir = entry.get("agentDir")
    return _resolve_openclaw_agent_dir(
        raw=raw_agent_dir if isinstance(raw_agent_dir, str) else None,
        fallback=fallback,
        config=config,
    )


def _load_auth_profiles_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "openclaw_auth_profiles_parse_failed",
            path=str(path),
            error=str(exc)[:240],
        )
        return {}
    if not isinstance(payload, dict):
        logger.warning(
            "openclaw_auth_profiles_invalid_shape",
            path=str(path),
        )
        return {}
    return payload


def _extract_profile_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("profiles")
    out: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for profile_id, profile in raw.items():
            if (
                isinstance(profile_id, str)
                and profile_id.strip()
                and isinstance(profile, dict)
            ):
                out[profile_id] = profile
        return out
    if isinstance(raw, list):
        for profile in raw:
            if not isinstance(profile, dict):
                continue
            profile_id = profile.get("id")
            if not isinstance(profile_id, str) or not profile_id.strip():
                continue
            profile_copy = dict(profile)
            profile_copy.pop("id", None)
            out[profile_id.strip()] = profile_copy
    return out


def _copy_to_agents_override(profile: dict[str, Any]) -> bool | None:
    raw = profile.get("copyToAgents")
    if isinstance(raw, bool):
        return raw
    return None


def _has_copyable_oauth_material(profile: dict[str, Any]) -> bool:
    profile_type = str(profile.get("type") or profile.get("kind") or "").strip().lower()
    if profile_type != "oauth":
        return False
    access = profile.get("access")
    refresh = profile.get("refresh")
    has_access = isinstance(access, str) and bool(access.strip())
    has_refresh = isinstance(refresh, str) and bool(refresh.strip())
    return has_access or has_refresh


def _is_auth_profile_portable_for_agent_copy(profile: dict[str, Any]) -> bool:
    override = _copy_to_agents_override(profile)
    if override is False:
        return False
    profile_type = str(profile.get("type") or profile.get("kind") or "").strip().lower()
    if profile_type == "oauth":
        return override is True and _has_copyable_oauth_material(profile)

    has_key = "key" in profile or "keyRef" in profile
    has_token = "token" in profile or "tokenRef" in profile
    if profile_type in _PORTABLE_AUTH_PROFILE_TYPES:
        if profile_type in {"api_key", "api-key"}:
            return has_key
        if profile_type == "token":
            return has_token
    if profile_type and profile_type not in _PORTABLE_AUTH_PROFILE_TYPES:
        return False
    return has_key or has_token


def _seed_portable_static_auth_profiles(
    *,
    agent_id: str,
    config: Config,
    source_agent_id: str | None = None,
) -> int:
    """Seed portable auth profiles from default source agent to one new agent."""
    source_agent_id = source_agent_id or _resolve_default_source_agent_id(config=config)
    if source_agent_id == agent_id:
        return 0
    source_path = (
        _runtime_agent_dir(agent_id=source_agent_id, config=config)
        / _AUTH_PROFILES_FILENAME
    )
    target_path = _runtime_agent_dir(agent_id=agent_id, config=config) / _AUTH_PROFILES_FILENAME
    source_abs = str(source_path.resolve(strict=False))
    target_abs = str(target_path.resolve(strict=False))
    if source_abs == target_abs:
        return 0
    if not source_path.exists() or target_path.exists():
        return 0

    source_payload = _load_auth_profiles_payload(source_path)
    source_profiles = _extract_profile_map(source_payload)
    if not source_profiles:
        return 0

    portable: dict[str, dict[str, Any]] = {}
    for profile_id, profile in source_profiles.items():
        if _is_auth_profile_portable_for_agent_copy(profile):
            portable[profile_id] = deepcopy(profile)
    if not portable:
        return 0

    target_payload: dict[str, Any] = {
        "version": 1,
        "profiles": portable,
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps(target_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(portable)


def _ensure_agent_sessions_dir(
    *,
    agent_id: str,
    config: Config,
) -> bool:
    sessions_dir = config.openclaw_home_path / "agents" / agent_id / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return True


def _agent_runtime_home(*, agent_id: str, config: Config) -> Path:
    """OpenClaw's per-agent runtime home ``~/.openclaw/agents/{id}/``.

    Holds the agent runtime dir (``agent/``, incl. seeded ``auth-profiles.json``)
    and ``sessions/`` (conversation history + session state). Created at
    :func:`commit_agent` time by :func:`_seed_portable_static_auth_profiles`
    and :func:`_ensure_agent_sessions_dir`. Lives in OpenClaw's global state
    tree keyed by agent id — like the cron store, removing the agent from
    ``openclaw.json`` does NOT cascade to it, so a purge must delete it
    explicitly or it leaks (stale sessions + credentials) forever.
    """
    return config.openclaw_home_path / "agents" / agent_id


def _purge_agent_runtime_home(*, agent_id: str, config: Config) -> bool:
    """Best-effort removal of the per-agent OpenClaw runtime home on purge.

    Only used on the permanent (``purge``) path — ``unregister`` keeps it so a
    later restore retains session history. Returns whether the dir existed.
    """
    home = _agent_runtime_home(agent_id=agent_id, config=config)
    existed = home.exists()
    _safe_rmtree(home)
    if existed:
        logger.info("openclaw_agent_runtime_home_purged", agent_id=agent_id, path=str(home))
    return existed


def _install_user_skills(workspace: Path, extra: tuple[str, ...]) -> list[str]:
    """Install dynamically discovered common skills plus extras."""
    source_root = seed_skills_source()  # idempotent
    # Discover from bundled common source-of-truth, then install from seeded
    # writable mirror so deleted source skills are not re-injected by stale cache.
    discovered = discover_user_agent_skills()
    skills = tuple(sorted(set(discovered).union(extra)))
    installer = SkillInstaller(workspace, source_root=source_root)
    records = installer.install(skills)
    return [r.name for r in records]


def _workspace_matches_managed_agent_home(agent_id: str, workspace: str) -> bool:
    """Return whether *workspace* matches ``~/.clawsomeflow/agents/{id}/workspace``."""
    try:
        expected = (paths.agent_dir(agent_id) / "workspace").resolve(strict=False)
    except ValueError:
        return False
    actual = Path(workspace).expanduser().resolve(strict=False)
    return actual == expected


def _is_duplicate_cron_error(detail: str) -> bool:
    text = (detail or "").lower()
    if not text:
        return False
    return (
        "already exists" in text
        or "duplicate" in text
        or "\u5df2\u5b58\u5728" in detail
        or "\u91cd\u590d" in detail
    )


def _openclaw_runtime_env(*, config: Config) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(oj.openclaw_json_path(config))
    env.setdefault("OPENCLAW_STATE_DIR", str(config.openclaw_home_path))
    return env


def _run_openclaw_cli(
    *,
    args: list[str],
    config: Config,
) -> subprocess.CompletedProcess[str] | None:
    executable = resolve_openclaw_executable()
    if not executable:
        logger.warning("openclaw_common_cron_sync_skipped_cli_missing")
        return None
    try:
        return subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_OPENCLAW_CRON_SYNC_TIMEOUT_SEC,
            env=_openclaw_runtime_env(config=config),
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "openclaw_common_cron_cli_timeout",
            args=" ".join(args[:4]),
            error=str(exc),
        )
    except OSError as exc:
        logger.warning(
            "openclaw_common_cron_cli_failed",
            args=" ".join(args[:4]),
            error=str(exc),
        )
    return None


def _cli_detail(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()


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
    start = text.find("{")
    while start >= 0:
        try:
            parsed = json.loads(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    return None


def _normalize_gateway_url(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    return text.rstrip("/")


def _gateway_url_from_openclaw_json(*, config: Config) -> str | None:
    try:
        payload = oj.load_openclaw_json(config)
    except oj.OpenclawJsonError:
        return None
    gateway = payload.get("gateway")
    if not isinstance(gateway, dict):
        return None
    port_raw = gateway.get("port", 18789)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return None
    if port <= 0 or port > 65535:
        return None
    # Keep loopback host resolution for local-mode probing.
    return f"http://127.0.0.1:{port}"


def _gateway_url_candidates(*, config: Config) -> list[str]:
    cfg_url = _normalize_gateway_url(config.openclaw_gateway_url)
    json_url = _normalize_gateway_url(_gateway_url_from_openclaw_json(config=config) or "")
    out: list[str] = []
    seen: set[str] = set()

    def _push(url: str | None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        out.append(url)

    if cfg_url and cfg_url != _DEFAULT_OPENCLAW_GATEWAY_URL:
        _push(cfg_url)
        _push(json_url)
    else:
        _push(json_url)
        _push(cfg_url)
    return out


def resolve_runtime_gateway_url(*, config: Config | None = None) -> str | None:
    cfg = config or load_config()
    default_ok, _default_reason = _probe_default_openclaw_gateway_health(
        timeout_sec=_OPENCLAW_RUNTIME_PROBE_SOCKET_TIMEOUT_SEC,
    )
    if default_ok:
        return _DEFAULT_OPENCLAW_GATEWAY_URL
    candidates = _gateway_url_candidates(config=cfg)
    if not candidates:
        return None
    return candidates[0]


def _run_openclaw_health_check(
    *,
    executable: str,
    config: Config,
    timeout_sec: float,
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [executable, "health", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
            env=_openclaw_runtime_env(config=config),
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError:
        return False, "invoke_failed"

    if proc.returncode != 0:
        return False, "health_failed"

    payload = _parse_json_object_from_mixed_output(proc.stdout or proc.stderr or "")
    if isinstance(payload, dict) and payload.get("ok") is True:
        return True, "ok"
    return False, "not_running"


def _probe_openclaw_gateway_socket(
    *,
    config: Config,
    timeout_sec: float,
) -> tuple[bool, str] | None:
    gateway_urls = _gateway_url_candidates(config=config)
    if not gateway_urls:
        return None
    for gateway_url in gateway_urls:
        parsed = urlparse(gateway_url)
        host = parsed.hostname
        port = parsed.port
        if not host or port is None:
            continue
        try:
            with socket.create_connection((host, port), timeout=timeout_sec):
                return True, "gateway_reachable"
        except OSError:
            continue
    return False, "gateway_unreachable"


def _probe_openclaw_gateway_health_url(
    *,
    gateway_url: str,
    timeout_sec: float,
) -> tuple[bool, str]:
    health_url = urljoin(
        gateway_url if gateway_url.endswith("/") else gateway_url + "/",
        _OPENCLAW_RUNTIME_HEALTH_PATH.lstrip("/"),
    )
    request = Request(health_url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_sec) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read(8192)
    except (URLError, TimeoutError) as exc:
        if isinstance(exc, TimeoutError):
            return False, "timeout"
        reason_cls = getattr(exc, "reason", None)
        if isinstance(reason_cls, TimeoutError):
            return False, "timeout"
        return False, "gateway_unreachable"
    except OSError:
        return False, "gateway_unreachable"
    if status != 200:
        return False, "health_failed"
    payload = _parse_json_object_from_mixed_output(body.decode("utf-8", errors="replace"))
    if isinstance(payload, dict) and payload.get("ok") is True:
        return True, "ok"
    return False, "not_running"


def _probe_default_openclaw_gateway_health(*, timeout_sec: float) -> tuple[bool, str]:
    return _probe_openclaw_gateway_health_url(
        gateway_url=_DEFAULT_OPENCLAW_GATEWAY_URL,
        timeout_sec=timeout_sec,
    )


def _probe_openclaw_gateway_health(
    *,
    config: Config,
    timeout_sec: float,
    gateway_urls: list[str] | None = None,
) -> tuple[bool, str] | None:
    """HTTP ``GET <gateway>/health`` — the canonical strict liveness check.

    Returns ``None`` when no usable gateway URL is configured so callers can
    fall back to the legacy CLI-based probe. A reachable gateway that does
    not respond ``{"ok": true}`` is reported as ``not_running``.
    """
    gateway_urls = gateway_urls if gateway_urls is not None else _gateway_url_candidates(config=config)
    if not gateway_urls:
        return None
    saw_timeout = False
    last_reason = "gateway_unreachable"
    for gateway_url in gateway_urls:
        ok, reason = _probe_openclaw_gateway_health_url(
            gateway_url=gateway_url,
            timeout_sec=timeout_sec,
        )
        if ok:
            return True, "ok"
        if reason == "timeout":
            saw_timeout = True
            last_reason = "timeout"
            continue
        last_reason = reason
    if saw_timeout:
        return False, "timeout"
    return False, last_reason


def _probe_runtime_running_strict_with_config(
    *,
    config: Config,
    timeout_sec: float,
) -> tuple[bool, str]:
    default_probe = _probe_default_openclaw_gateway_health(timeout_sec=timeout_sec)
    if default_probe[0]:
        return default_probe
    if default_probe[1] == "timeout":
        default_retry = _probe_default_openclaw_gateway_health(
            timeout_sec=max(timeout_sec, _OPENCLAW_RUNTIME_PROBE_RETRY_TIMEOUT_SEC),
        )
        if default_retry[0]:
            return default_retry

    candidate_urls = [
        url
        for url in _gateway_url_candidates(config=config)
        if _normalize_gateway_url(url) != _DEFAULT_OPENCLAW_GATEWAY_URL
    ]
    # Preferred path: HTTP ``GET /health`` against the configured gateway.
    # Replaces the previous ``subprocess.run(["openclaw","health","--json"])``
    # path — that one cold-started Node + the openclaw CLI on every probe,
    # taking ~2.5s and consistently timing out the strict check.
    http_result = _probe_openclaw_gateway_health(
        config=config,
        timeout_sec=timeout_sec,
        gateway_urls=candidate_urls,
    )
    if http_result is not None:
        ok, reason = http_result
        if ok:
            return True, reason
        if reason != "timeout":
            return False, reason
        retry_timeout = max(timeout_sec, _OPENCLAW_RUNTIME_PROBE_RETRY_TIMEOUT_SEC)
        retry = _probe_openclaw_gateway_health(
            config=config,
            timeout_sec=retry_timeout,
            gateway_urls=candidate_urls,
        )
        if retry is not None:
            return retry
        return False, reason

    # No gateway URL configured — fall back to the CLI health check.
    executable = resolve_openclaw_executable()
    if not executable:
        return False, "cli_missing"
    ok, reason = _run_openclaw_health_check(
        executable=executable,
        config=config,
        timeout_sec=timeout_sec,
    )
    if ok:
        return True, reason
    if reason != "timeout":
        return False, reason
    return _run_openclaw_health_check(
        executable=executable,
        config=config,
        timeout_sec=max(timeout_sec, _OPENCLAW_RUNTIME_PROBE_RETRY_TIMEOUT_SEC),
    )


def probe_runtime_running_strict(
    *,
    config: Config | None = None,
    timeout_sec: float = _OPENCLAW_RUNTIME_PROBE_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """Strict runtime verification via OpenClaw CLI health check."""
    cfg = config or load_config()
    return _probe_runtime_running_strict_with_config(config=cfg, timeout_sec=timeout_sec)


def probe_runtime_running(
    *,
    config: Config | None = None,
    timeout_sec: float = _OPENCLAW_RUNTIME_PROBE_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """Sub-second runtime liveness probe for OpenClaw service availability."""
    cfg = config or load_config()
    # Stage 1: explicit first-choice check for the default OpenClaw gateway port.
    # If the service on 18789 is healthy OpenClaw, unlock immediately.
    default_probe = _probe_default_openclaw_gateway_health(
        timeout_sec=min(timeout_sec, _OPENCLAW_RUNTIME_PROBE_SOCKET_TIMEOUT_SEC),
    )
    if default_probe[0]:
        return default_probe

    # Stage 2: fallback verification only when 18789 is unavailable.
    return _probe_runtime_running_strict_with_config(config=cfg, timeout_sec=timeout_sec)


def _list_agent_cron_jobs(*, agent_id: str, config: Config) -> list[dict[str, Any]]:
    proc = _run_openclaw_cli(
        args=["cron", "list", "--agent", agent_id, "--all", "--json"],
        config=config,
    )
    if proc is None:
        return []
    if proc.returncode != 0:
        logger.warning(
            "openclaw_common_cron_list_failed",
            agent_id=agent_id,
            detail=_cli_detail(proc)[:500],
        )
        return []
    payload = _parse_json_object_from_mixed_output(proc.stdout or proc.stderr or "")
    if not isinstance(payload, dict):
        logger.warning("openclaw_common_cron_list_bad_json", agent_id=agent_id)
        return []
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return []
    out: list[dict[str, Any]] = []
    for item in jobs:
        if not isinstance(item, dict):
            continue
        if str(item.get("agentId") or "") != agent_id:
            continue
        out.append(item)
    return out


def _list_agent_cron_jobs_strict(*, agent_id: str, config: Config) -> list[dict[str, Any]]:
    """Strict cron list for backup/restore paths; raises on invocation failure."""
    proc = _run_openclaw_cli(
        args=["cron", "list", "--agent", agent_id, "--all", "--json"],
        config=config,
    )
    if proc is None:
        raise OpenclawAgentError(
            "unable to query OpenClaw cron jobs: openclaw CLI unavailable",
        )
    if proc.returncode != 0:
        raise OpenclawAgentError(
            f"unable to query OpenClaw cron jobs: {_cli_detail(proc)[:500]}",
        )
    payload = _parse_json_object_from_mixed_output(proc.stdout or proc.stderr or "")
    if not isinstance(payload, dict):
        raise OpenclawAgentError(
            "unable to query OpenClaw cron jobs: invalid JSON payload",
        )
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return []
    out: list[dict[str, Any]] = []
    for item in jobs:
        if not isinstance(item, dict):
            continue
        if str(item.get("agentId") or "") != agent_id:
            continue
        out.append(item)
    return out


def _is_system_cron_job(job: dict[str, Any], *, agent_id: str) -> bool:
    name = str(job.get("name") or "").strip()
    source = str(job.get("source") or "").strip().lower()
    if name == f"{_ENTROPY_CRON_NAME_PREFIX}-{agent_id}":
        return True
    return source in {"system", "openclaw-bundled", "builtin", "built-in"}


def _normalize_custom_cron_backup_entries(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        cron_expr = str(item.get("cron_expr") or "").strip()
        cron_tz = str(item.get("cron_tz") or "").strip() or _FALLBACK_ENTROPY_CRON_TZ
        session = str(item.get("session") or "").strip() or "isolated"
        message = str(item.get("message") or "").strip()
        enabled = bool(item.get("enabled", True))
        if not name or not cron_expr or not message:
            continue
        out.append(
            {
                "name": name,
                "cron_expr": cron_expr,
                "cron_tz": cron_tz,
                "session": session,
                "message": message,
                "enabled": enabled,
            }
        )
    return out


def _extract_custom_cron_backup_entries(
    *,
    agent_id: str,
    jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in jobs:
        if _is_system_cron_job(item, agent_id=agent_id):
            continue
        name = str(item.get("name") or "").strip()
        schedule = item.get("schedule")
        payload = item.get("payload")
        cron_expr = ""
        cron_tz = _FALLBACK_ENTROPY_CRON_TZ
        if isinstance(schedule, dict):
            cron_expr = str(schedule.get("expr") or "").strip()
            cron_tz = str(schedule.get("tz") or "").strip() or _FALLBACK_ENTROPY_CRON_TZ
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("event") or "").strip()
        session = str(item.get("session") or "").strip() or "isolated"
        enabled = bool(item.get("enabled", True))
        if not name or not cron_expr or not message:
            continue
        out.append(
            {
                "name": name,
                "cron_expr": cron_expr,
                "cron_tz": cron_tz,
                "session": session,
                "message": message,
                "enabled": enabled,
            }
        )
    return out


def _update_snapshot_with_cron_backup(
    *,
    snapshot: dict[str, Any] | None,
    backup_entries: list[dict[str, Any]],
    mark_captured: bool = False,
    mark_restored: bool = False,
) -> dict[str, Any]:
    next_snapshot = dict(snapshot) if isinstance(snapshot, dict) else {}
    next_snapshot[_CRON_BACKUP_SNAPSHOT_KEY] = backup_entries
    now_iso = datetime.now(timezone.utc).isoformat()
    if mark_captured:
        next_snapshot[_CRON_BACKUP_CAPTURED_AT_KEY] = now_iso
    if mark_restored:
        next_snapshot[_CRON_BACKUP_RESTORED_AT_KEY] = now_iso
    return next_snapshot


def _capture_custom_cron_backup(
    *,
    agent_id: str,
    current_snapshot: dict[str, Any] | None,
    config: Config,
) -> tuple[list[dict[str, Any]], bool]:
    fallback_backup = _normalize_custom_cron_backup_entries(
        (current_snapshot or {}).get(_CRON_BACKUP_SNAPSHOT_KEY)
        if isinstance(current_snapshot, dict)
        else None
    )
    try:
        runtime_jobs = _list_agent_cron_jobs_strict(agent_id=agent_id, config=config)
        backup = _extract_custom_cron_backup_entries(agent_id=agent_id, jobs=runtime_jobs)
        return backup, True
    except OpenclawAgentError as exc:
        logger.warning(
            "openclaw_cron_backup_capture_failed",
            agent_id=agent_id,
            error=str(exc),
            fallback_entries=len(fallback_backup),
        )
        return fallback_backup, False


def _remove_all_agent_cron_jobs(*, agent_id: str, config: Config) -> tuple[int, int]:
    """Best-effort removal of EVERY runtime cron job owned by ``agent_id``.

    Both the system (entropy) job and workspace-custom jobs are removed so that
    an unregistered or purged agent leaves no scheduled jobs firing in
    OpenClaw's cron store. That store lives outside ``openclaw.json`` and is
    NOT touched by :func:`remove_managed_agent`, so without this the cron jobs
    would keep firing against an agent that is no longer in the runtime.

    Custom jobs are backed up into the DB snapshot beforehand (unregister) and
    replayed by :func:`restore_agent_registration`; the entropy job is
    re-scheduled on restore. Never raises — cron residue cleanup must not block
    agent removal. Returns ``(removed, failed)`` counts.
    """
    jobs = _list_agent_cron_jobs(agent_id=agent_id, config=config)
    removed = 0
    failed = 0
    for job in jobs:
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        proc = _run_openclaw_cli(args=["cron", "rm", job_id, "--json"], config=config)
        if proc is not None and proc.returncode == 0:
            removed += 1
            continue
        failed += 1
        logger.warning(
            "openclaw_agent_cron_remove_failed",
            agent_id=agent_id,
            job_id=job_id,
            detail=(
                _cli_detail(proc)[:500] if proc is not None else "openclaw CLI unavailable"
            ),
        )
    if removed or failed:
        logger.info(
            "openclaw_agent_cron_jobs_removed",
            agent_id=agent_id,
            removed=removed,
            failed=failed,
        )
    return removed, failed


def _upsert_custom_cron_job_from_backup_entry(
    *,
    agent_id: str,
    entry: dict[str, Any],
    config: Config,
) -> None:
    name = str(entry.get("name") or "").strip()
    cron_expr = str(entry.get("cron_expr") or "").strip()
    cron_tz = str(entry.get("cron_tz") or "").strip() or _FALLBACK_ENTROPY_CRON_TZ
    session = str(entry.get("session") or "").strip() or "isolated"
    message = str(entry.get("message") or "").strip()
    enabled = bool(entry.get("enabled", True))
    if not name or not cron_expr or not message:
        return

    def _edit_existing_cron_job(*, job_id: str) -> str | None:
        edit_args = [
            "cron",
            "edit",
            job_id,
            "--name",
            name,
            "--cron",
            cron_expr,
            "--tz",
            cron_tz,
            "--session",
            session,
            "--agent",
            agent_id,
            "--message",
            message,
            "--enable" if enabled else "--disable",
        ]
        edit_proc = _run_openclaw_cli(args=edit_args, config=config)
        if edit_proc is not None and edit_proc.returncode == 0:
            return None
        return _cli_detail(edit_proc) if edit_proc is not None else "cli invocation failed"

    # Guard against duplicated jobs: always check same-name existence first.
    existing_jobs = _list_agent_cron_jobs_strict(agent_id=agent_id, config=config)
    existing = next(
        (item for item in existing_jobs if str(item.get("name") or "").strip() == name),
        None,
    )
    existing_id = str(existing.get("id") or "").strip() if isinstance(existing, dict) else ""
    if existing_id:
        edit_detail = _edit_existing_cron_job(job_id=existing_id)
        if edit_detail is None:
            return
        raise OpenclawAgentError(
            f"failed to restore cron job {name!r} for agent {agent_id!r}: {edit_detail[:500]}"
        )

    add_args = [
        "cron",
        "add",
        "--name",
        name,
        "--cron",
        cron_expr,
        "--tz",
        cron_tz,
        "--session",
        session,
        "--agent",
        agent_id,
        "--message",
        message,
        "--json",
    ]
    if not enabled:
        add_args.append("--disabled")
    proc = _run_openclaw_cli(args=add_args, config=config)
    if proc is not None and proc.returncode == 0:
        return

    detail = _cli_detail(proc) if proc is not None else "cli invocation failed"
    if _is_duplicate_cron_error(detail):
        jobs = _list_agent_cron_jobs_strict(agent_id=agent_id, config=config)
        match = next(
            (item for item in jobs if str(item.get("name") or "").strip() == name),
            None,
        )
        match_id = str(match.get("id") or "").strip() if isinstance(match, dict) else ""
        if match_id:
            edit_detail = _edit_existing_cron_job(job_id=match_id)
            if edit_detail is None:
                return
            detail = edit_detail

    raise OpenclawAgentError(
        f"failed to restore cron job {name!r} for agent {agent_id!r}: {detail[:500]}"
    )


def _restore_custom_cron_jobs_from_backup(
    *,
    agent_id: str,
    snapshot: dict[str, Any] | None,
    config: Config,
) -> int:
    backup_entries = _normalize_custom_cron_backup_entries(
        (snapshot or {}).get(_CRON_BACKUP_SNAPSHOT_KEY)
        if isinstance(snapshot, dict)
        else None
    )
    restored = 0
    for entry in backup_entries:
        _upsert_custom_cron_job_from_backup_entry(
            agent_id=agent_id,
            entry=entry,
            config=config,
        )
        restored += 1
    return restored


def _render_agent_template(template: str, *, agent_id: str, field: str) -> str:
    try:
        rendered = template.format(agent_id=agent_id)
    except (KeyError, IndexError, ValueError):
        logger.warning(
            "openclaw_common_cron_template_invalid",
            field=field,
            template=template[:200],
        )
        rendered = template.replace("{agent_id}", agent_id)
    return rendered.strip()


def _fallback_entropy_cron_definition() -> _CommonCronJobDefinition:
    return _CommonCronJobDefinition(
        definition_id="entropy-management",
        name_template=f"{_ENTROPY_CRON_NAME_PREFIX}" + "-{agent_id}",
        cron_expr=_FALLBACK_ENTROPY_CRON_EXPR,
        cron_tz=_FALLBACK_ENTROPY_CRON_TZ,
        session="isolated",
        message_template=_FALLBACK_ENTROPY_MESSAGE_TEMPLATE,
    )


def _load_common_cron_job_definitions() -> tuple[_CommonCronJobDefinition, ...]:
    try:
        deploy_common_agent_source()
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning(
            "openclaw_common_cron_source_deploy_failed",
            error=str(exc),
        )

    root = paths.common_agent_source_dir() / _COMMON_CRON_JOBS_SUBDIR
    out: list[_CommonCronJobDefinition] = []
    if root.exists() and root.is_dir():
        for file in sorted(root.glob("*.json")):
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "openclaw_common_cron_definition_read_failed",
                    path=str(file),
                    error=str(exc),
                )
                continue
            if not isinstance(payload, dict):
                logger.warning(
                    "openclaw_common_cron_definition_invalid",
                    path=str(file),
                    reason="payload must be object",
                )
                continue
            schedule = payload.get("schedule")
            if not isinstance(schedule, dict):
                logger.warning(
                    "openclaw_common_cron_definition_invalid",
                    path=str(file),
                    reason="missing schedule object",
                )
                continue
            definition_id = str(payload.get("id") or file.stem).strip() or file.stem
            name_template = str(payload.get("nameTemplate") or "").strip()
            cron_expr = str(schedule.get("expr") or "").strip()
            cron_tz = str(schedule.get("tz") or "").strip() or _FALLBACK_ENTROPY_CRON_TZ
            session = str(payload.get("session") or "isolated").strip() or "isolated"
            message_template = str(payload.get("messageTemplate") or "").strip()
            if not name_template or not cron_expr or not message_template:
                logger.warning(
                    "openclaw_common_cron_definition_invalid",
                    path=str(file),
                    reason="nameTemplate/schedule.expr/messageTemplate are required",
                )
                continue
            out.append(
                _CommonCronJobDefinition(
                    definition_id=definition_id,
                    name_template=name_template,
                    cron_expr=cron_expr,
                    cron_tz=cron_tz,
                    session=session,
                    message_template=message_template,
                )
            )
    if out:
        return tuple(out)
    logger.warning("openclaw_common_cron_definition_missing_fallback")
    return (_fallback_entropy_cron_definition(),)


def _edit_agent_cron_job(
    *,
    agent_id: str,
    job_id: str,
    definition: _CommonCronJobDefinition,
    job_name: str,
    message: str,
    config: Config,
) -> bool:
    proc = _run_openclaw_cli(
        args=[
            "cron",
            "edit",
            job_id,
            "--name",
            job_name,
            "--cron",
            definition.cron_expr,
            "--tz",
            definition.cron_tz,
            "--session",
            definition.session,
            "--agent",
            agent_id,
            "--message",
            message,
        ],
        config=config,
    )
    if proc is not None and proc.returncode == 0:
        logger.info(
            "openclaw_common_cron_job_synced",
            agent_id=agent_id,
            definition_id=definition.definition_id,
            job_name=job_name,
            job_id=job_id,
            action="edit",
            cron=definition.cron_expr,
            tz=definition.cron_tz,
        )
        return True
    logger.warning(
        "openclaw_common_cron_job_sync_failed",
        agent_id=agent_id,
        definition_id=definition.definition_id,
        job_name=job_name,
        job_id=job_id,
        action="edit",
        detail=_cli_detail(proc)[:500] if proc is not None else "cli invocation failed",
    )
    return False


def _sync_common_cron_jobs_for_agent(
    *,
    agent_id: str,
    definitions: tuple[_CommonCronJobDefinition, ...] | None = None,
    config: Config | None = None,
) -> bool:
    cfg = config or load_config()
    active_definitions = definitions or _load_common_cron_job_definitions()
    if not active_definitions:
        return False

    try:
        existing_jobs = _list_agent_cron_jobs_strict(agent_id=agent_id, config=cfg)
    except OpenclawAgentError as exc:
        logger.warning(
            "openclaw_common_cron_list_failed_strict",
            agent_id=agent_id,
            detail=str(exc)[:500],
        )
        return False
    existing_by_name: dict[str, dict[str, Any]] = {}
    for job in existing_jobs:
        name = str(job.get("name") or "").strip()
        if not name or name in existing_by_name:
            continue
        existing_by_name[name] = job

    all_ok = True
    for definition in active_definitions:
        job_name = _render_agent_template(
            definition.name_template,
            agent_id=agent_id,
            field="nameTemplate",
        )
        message = _render_agent_template(
            definition.message_template,
            agent_id=agent_id,
            field="messageTemplate",
        )
        if not job_name or not message:
            logger.warning(
                "openclaw_common_cron_render_failed",
                agent_id=agent_id,
                definition_id=definition.definition_id,
            )
            all_ok = False
            continue
        existing = existing_by_name.get(job_name)
        existing_id = str(existing.get("id") or "").strip() if isinstance(existing, dict) else ""
        if existing_id:
            ok = _edit_agent_cron_job(
                agent_id=agent_id,
                job_id=existing_id,
                definition=definition,
                job_name=job_name,
                message=message,
                config=cfg,
            )
            all_ok = all_ok and ok
            continue

        proc = _run_openclaw_cli(
            args=[
                "cron",
                "add",
                "--name",
                job_name,
                "--cron",
                definition.cron_expr,
                "--tz",
                definition.cron_tz,
                "--session",
                definition.session,
                "--agent",
                agent_id,
                "--message",
                message,
                "--json",
            ],
            config=cfg,
        )
        if proc is not None and proc.returncode == 0:
            logger.info(
                "openclaw_common_cron_job_synced",
                agent_id=agent_id,
                definition_id=definition.definition_id,
                job_name=job_name,
                action="create",
                cron=definition.cron_expr,
                tz=definition.cron_tz,
            )
            continue

        detail = _cli_detail(proc) if proc is not None else "cli invocation failed"
        if _is_duplicate_cron_error(detail):
            try:
                refreshed = _list_agent_cron_jobs_strict(agent_id=agent_id, config=cfg)
            except OpenclawAgentError as exc:
                refreshed = []
                detail = str(exc)
            match = next(
                (
                    item for item in refreshed
                    if str(item.get("name") or "").strip() == job_name
                ),
                None,
            )
            match_id = str(match.get("id") or "").strip() if isinstance(match, dict) else ""
            if match_id:
                ok = _edit_agent_cron_job(
                    agent_id=agent_id,
                    job_id=match_id,
                    definition=definition,
                    job_name=job_name,
                    message=message,
                    config=cfg,
                )
                all_ok = all_ok and ok
                continue

        logger.warning(
            "openclaw_common_cron_job_sync_failed",
            agent_id=agent_id,
            definition_id=definition.definition_id,
            job_name=job_name,
            action="create",
            detail=detail[:500],
        )
        all_ok = False
    return all_ok


def _schedule_default_entropy_management_task(
    *,
    agent_id: str,
    config: Config | None = None,
) -> bool:
    return _sync_common_cron_jobs_for_agent(agent_id=agent_id, config=config)


def _flow_contains_openclaw_agent(flow: Flow, agent_id: str) -> bool:
    raw_agents = flow.spec.get("agents", [])
    if not isinstance(raw_agents, list):
        return False
    for raw_agent in raw_agents:
        if not isinstance(raw_agent, dict):
            continue
        if raw_agent.get("kind") == "openclaw" and raw_agent.get("id") == agent_id:
            return True
    return False


def _iter_user_flows(*, storage: StorageBackend, user: str) -> list[Flow]:
    out: list[Flow] = []
    offset = 0
    while True:
        items, total = storage.flow_list(owner_user=user, limit=_PAGE_SIZE, offset=offset)
        if not items:
            break
        out.extend(items)
        offset += len(items)
        if offset >= total:
            break
    return out


def _iter_user_runs(*, storage: StorageBackend, user: str) -> list[FlowRun]:
    out: list[FlowRun] = []
    offset = 0
    while True:
        items, total = storage.run_list(user=user, limit=_PAGE_SIZE, offset=offset)
        if not items:
            break
        out.extend(items)
        offset += len(items)
        if offset >= total:
            break
    return out


def _collect_blocking_flow_names(
    *,
    storage: StorageBackend,
    user: str,
    agent_id: str,
) -> dict[str, list[str] | int]:
    template_flow_names: set[str] = set()
    flow_map: dict[str, Flow] = {}
    for flow in _iter_user_flows(storage=storage, user=user):
        if not _flow_contains_openclaw_agent(flow, agent_id):
            continue
        flow_map[flow.id] = flow
        template_flow_names.add(flow.name or flow.id)

    active_flow_names: set[str] = set()
    for run in _iter_user_runs(storage=storage, user=user):
        if run.status in _TERMINAL_RUN_STATUSES:
            continue
        flow = flow_map.get(run.flow_id) or storage.flow_get(run.flow_id)
        if flow is None:
            continue
        if _flow_contains_openclaw_agent(flow, agent_id):
            active_flow_names.add(flow.name or flow.id)

    blocked_flow_names = sorted(template_flow_names.union(active_flow_names))
    return {
        "flow_names": blocked_flow_names,
        "template_flow_names": sorted(template_flow_names),
        "active_flow_names": sorted(active_flow_names),
        "active_runs": len(active_flow_names),
    }


def _import_target_agent_id(source_agent_id: str) -> str:
    """Deterministic import naming: ``csflow-{source_agent_id}``."""
    return _validate_agent_id(f"{_IMPORT_TARGET_ID_PREFIX}{source_agent_id}")


def _source_agent_id_from_imported_target_id(agent_id: str) -> str | None:
    """Reverse imported id mapping when the managed id uses ``csflow-`` prefix."""
    if not agent_id.startswith(_IMPORT_TARGET_ID_PREFIX):
        return None
    raw = agent_id[len(_IMPORT_TARGET_ID_PREFIX):].strip()
    if not raw:
        return None
    try:
        return _validate_agent_id(raw)
    except AgentIdInvalid:
        return None


def _load_external_candidate_map(
    *,
    storage: StorageBackend,
    config: Config,
) -> dict[str, ExternalImportCandidate]:
    """Read unmanaged runtime agents from openclaw.json."""
    try:
        payload = oj.load_openclaw_json(config)
    except Exception as exc:
        raise OpenclawAgentError(f"failed to read openclaw.json: {exc}") from exc
    raw_agents = payload.get("agents", {}).get("list", [])
    if not isinstance(raw_agents, list):
        return {}

    managed_ids = set(oj.list_managed_agent_ids())
    db_ids = {row.id for row in storage.openclaw_list(owner_user=None)}
    imported_source_ids = {
        source_id
        for managed_id in (managed_ids | db_ids)
        for source_id in [_source_agent_id_from_imported_target_id(managed_id)]
        if source_id is not None
    }
    excluded_ids = managed_ids | db_ids | imported_source_ids

    out: dict[str, ExternalImportCandidate] = {}
    for raw in raw_agents:
        if not isinstance(raw, dict):
            continue
        aid_raw = raw.get("id")
        if not isinstance(aid_raw, str):
            continue
        aid = aid_raw.strip()
        if not aid or aid in excluded_ids:
            continue
        ws = str(raw.get("workspace") or "").strip()
        out[aid] = ExternalImportCandidate(
            id=aid,
            name=str(raw.get("name") or aid),
            description=str(raw.get("description") or ""),
            workspace_path=ws,
        )
    return out


def _copy_workspace_overlay(*, source: Path, target: Path) -> None:
    """Copy source workspace into target, overwriting same-name paths."""
    if not source.exists() or not source.is_dir():
        raise ExternalWorkspaceInvalid(f"source workspace not found: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        dst = target / child.name
        if child.is_dir():
            shutil.copytree(child, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, dst)


def _render_imported_agents_md(*, imported_agents_md: str) -> str:
    """Build AGENTS.md with latest common rules + imported content as custom block."""
    try:
        common_rules = load_common_rules_text()
    except FileNotFoundError as exc:
        raise OpenclawAgentError(f"common rules file not found: {exc}") from exc
    except ValueError as exc:
        raise OpenclawAgentError(str(exc)) from exc
    imported = (imported_agents_md or "").strip()
    if imported:
        # Avoid nested marker collisions inside the custom section.
        imported = imported.replace(
            AGENTS_USER_CUSTOM_SECTION_START,
            "<!-- AGENTS_USER_CUSTOM_SECTION_START(imported) -->",
        ).replace(
            AGENTS_USER_CUSTOM_SECTION_END,
            "<!-- AGENTS_USER_CUSTOM_SECTION_END(imported) -->",
        )
        custom = (
            "## AGENTS_USER_CUSTOM_SECTION\n\n"
            "The content below comes from the pre-import agent definition (auto-preserved):\n\n"
            f"{imported}"
        )
    else:
        custom = (
            "## AGENTS_USER_CUSTOM_SECTION\n\n"
            "- Add user- or manager-guided personalized rules here.\n"
            "- Re-deploy and upgrade only update shared rules outside this section."
        )
    return (
        f"{common_rules}\n\n"
        f"{AGENTS_USER_CUSTOM_SECTION_START}\n"
        f"{custom}\n"
        f"{AGENTS_USER_CUSTOM_SECTION_END}\n"
    )


def _rewrite_imported_agents_md(*, workspace: Path, source_workspace: Path) -> None:
    """Re-emit AGENTS.md so common rules are fresh and imported defs stay in custom."""
    source_agents_md = source_workspace / "AGENTS.md"
    imported_text = ""
    if source_agents_md.exists() and source_agents_md.is_file():
        imported_text = source_agents_md.read_text(encoding="utf-8")
    (workspace / "AGENTS.md").write_text(
        _render_imported_agents_md(imported_agents_md=imported_text),
        encoding="utf-8",
    )


def reindex_registered_agents(
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> list[str]:
    """Backfill DB rows from ``openclaw.json`` entries when missing.

    Scope:
    - only entries whose workspace is exactly
      ``~/.clawsomeflow/agents/{id}/workspace``;
    - marks each backfilled id as managed in the managed-agent registry.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    try:
        data = oj.load_openclaw_json(cfg)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("openclaw_reindex_skipped", error=str(exc))
        return []

    raw_agents = data.get("agents", {}).get("list", [])
    if not isinstance(raw_agents, list):  # pragma: no cover - defensive
        return []

    inserted: list[str] = []
    for raw in raw_agents:
        if not isinstance(raw, dict):
            continue
        aid = raw.get("id")
        if not isinstance(aid, str) or not aid:
            continue
        if storage.openclaw_get(aid) is not None:
            continue
        workspace_raw = raw.get("workspace")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            continue
        if not _workspace_matches_managed_agent_home(aid, workspace_raw):
            logger.warning(
                "openclaw_reindex_workspace_mismatch",
                agent_id=aid,
                workspace=workspace_raw,
            )
            continue
        workspace = Path(workspace_raw).expanduser().resolve(strict=False)
        if not workspace.exists() or not workspace.is_dir():
            logger.warning(
                "openclaw_reindex_workspace_missing",
                agent_id=aid,
                workspace=workspace_raw,
            )
            continue

        row = OpenclawAgent(
            id=aid,
            name=str(raw.get("name") or aid),
            description=str(raw.get("description") or ""),
            team_id="",
            workspace_path=str(workspace),
            openclaw_config_snapshot=dict(raw),
            created_by_user=cfg.default_user,
            nl_prompt="",
        )
        created = False
        try:
            storage.openclaw_create(row)
            created = True
            oj.mark_agent_managed_sync(aid, config=cfg)
            inserted.append(aid)
        except Exception as exc:  # pragma: no cover - race / storage failure
            if created:
                try:
                    storage.openclaw_delete(aid)
                except Exception:  # pragma: no cover - best-effort rollback
                    logger.warning("openclaw_reindex_rollback_failed", agent_id=aid)
            logger.warning(
                "openclaw_reindex_insert_failed",
                agent_id=aid,
                error=str(exc),
            )

    if inserted:
        logger.info(
            "openclaw_reindex_done",
            inserted=inserted,
            count=len(inserted),
        )
    return inserted


# ──────────────────────────────────────────────────────────────────────
# Public service surface
# ──────────────────────────────────────────────────────────────────────


# Ids with an in-flight create (registration + bootstrap). A duplicate /
# concurrent create of the same id fails fast instead of racing the in-flight
# one — the two share one id-keyed workspace, and the loser's rollback
# (``_safe_rmtree``) would otherwise wipe it, taking the winner's agent down
# with it. The backend runs one event loop, so a check-then-add with no await
# in between is race-free.
_CREATE_IN_PROGRESS: set[str] = set()
_CANCELLED_CREATES: set[str] = set()
_CREATE_SELF_DEFINE_COMMIT_MESSAGE_PREFIX = "[csflow] bootstrap self-definition"


def is_create_in_flight(aid: str) -> bool:
    """True while a create for *aid* is registering side effects or bootstrapping.

    Used by the operation-status recovery layer (``GET /api/operations``) when
    the in-memory op registry entry was missed or evicted. Held from the start
    of ``commit_agent`` until the API handler finishes bootstrap (success,
    failure, or cancel). Read on the event loop only (no lock).
    """
    return aid in _CREATE_IN_PROGRESS


def finish_create_in_flight(aid: str) -> None:
    """Release the in-flight reservation after bootstrap terminates."""
    _CREATE_IN_PROGRESS.discard(aid)


def request_create_cancellation(aid: str) -> None:
    """Mark an in-flight create as cancelled (honoured at the next await point)."""
    _CANCELLED_CREATES.add(aid)


def clear_create_cancellation(aid: str) -> None:
    """Clear a stale cancel flag before a fresh create attempt for the same id."""
    _CANCELLED_CREATES.discard(aid)


def is_create_cancelled(aid: str) -> bool:
    return aid in _CANCELLED_CREATES


def is_bootstrap_complete(agent_id: str, *, storage: StorageBackend | None = None) -> bool:
    """True once the bootstrap self-definition git commit landed in the workspace."""
    store = storage or get_storage()
    row = store.openclaw_get(agent_id)
    if row is None:
        return False
    workspace = Path(row.workspace_path).expanduser().resolve(strict=False)
    if not workspace.is_dir():
        return False
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    return _CREATE_SELF_DEFINE_COMMIT_MESSAGE_PREFIX in (proc.stdout or "")


async def commit_agent(
    cmd: CommitInput,
    *,
    user: str,
    team_id: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> OpenclawAgent:
    """Atomically register a new OpenClaw agent.

    Steps (in order):

    1. Validate id; reject if any agent with that id already exists in
       openclaw.json (managed or not).
    2. Deploy common-agent rules + common skills.
    3. Create the agent's main workspace dir + ``git init`` it.
    4. Install common managed skills (and any extras) into the
       workspace's ``skills/`` subdir.
    5. Append the agent entry to openclaw.json under
       ``lock("openclaw_json")``.
    6. Persist the ``OpenclawAgent`` DB row.
    7. Best-effort seed portable auth profiles from default source agentDir.
    8. Best-effort prepare OpenClaw per-agent ``sessions/`` directory.

    On step 4 / 5 failure we attempt best-effort filesystem rollback so the
    next attempt with the same id can succeed.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    # NB: every blocking call below (DB scans, filesystem scaffolding, git
    # init, openclaw CLI cron sync) is dispatched via ``asyncio.to_thread`` so
    # the uvicorn event loop stays responsive. Running them inline froze the
    # whole backend for seconds (git ``subprocess.run`` ×5 + cron-sync CLI
    # calls at 15s each), making every other page hang while an agent was being
    # created.
    await asyncio.to_thread(reindex_registered_agents, storage=storage, config=cfg)
    # Self-heal managed entries from legacy invalid keys (e.g. description)
    # so subsequent openclaw CLI commands pass schema validation.
    await oj.sanitize_managed_agent_entries(config=cfg)
    # Keep runtime timeout defaults pinned even for agents created after deploy.
    await ensure_runtime_timeout_defaults(config=cfg)
    auth_seed_source_agent_id = _resolve_default_source_agent_id(config=cfg)

    aid = _validate_agent_id(cmd.id)
    if not cmd.name:
        raise OpenclawAgentError("name is required")

    # Fresh attempt: clear any stale cancel flag from a previous create of the
    # same id (the user may retry after cancelling).
    clear_create_cancellation(aid)

    # Reserve the id for the whole create (registration + bootstrap) so a
    # duplicate / concurrent request fails fast instead of racing into the
    # shared, id-keyed workspace below — whose files the loser's rollback
    # (``_safe_rmtree``) would otherwise wipe, taking the winner's agent with
    # it. Check-then-add is race-free on the single event loop (no await
    # between). The reservation is released by :func:`finish_create_in_flight`
    # once bootstrap terminates (API layer); failures during registration
    # release it in the ``except`` below.
    if aid in _CREATE_IN_PROGRESS:
        raise AgentAlreadyExists(f"a create for {aid!r} is already in progress")
    _CREATE_IN_PROGRESS.add(aid)
    try:
        return await _commit_agent_reserved(
            cmd,
            aid,
            user=user,
            team_id=team_id,
            storage=storage,
            config=cfg,
            auth_seed_source_agent_id=auth_seed_source_agent_id,
        )
    except Exception:
        finish_create_in_flight(aid)
        raise


async def _abort_commit_if_cancelled(
    aid: str,
    *,
    workspace: Path,
    config: Config,
    storage: StorageBackend,
    json_registered: bool,
    row_persisted: bool,
) -> None:
    if not is_create_cancelled(aid):
        return
    if row_persisted:
        try:
            await delete_agent(aid, mode="purge", storage=storage, config=config)
        except AgentNotFound:
            pass
    elif json_registered:
        try:
            await oj.remove_managed_agent(aid, config=config)
        except Exception:  # pragma: no cover - rollback best-effort
            logger.warning("rollback_openclaw_json_failed", agent_id=aid)
        _safe_rmtree(workspace.parent)
    else:
        _safe_rmtree(workspace.parent)
    clear_create_cancellation(aid)
    raise AgentCreateCancelled(f"creation of {aid!r} cancelled by user")


async def _commit_agent_reserved(
    cmd: CommitInput,
    aid: str,
    *,
    user: str,
    team_id: str | None,
    storage: StorageBackend,
    config: Config,
    auth_seed_source_agent_id: str | None,
) -> OpenclawAgent:
    """Body of :func:`commit_agent`, run while *aid* is reserved in
    ``_CREATE_IN_PROGRESS`` so duplicate creates can't race the side effects."""
    cfg = config
    runtime_agent_dir = _runtime_agent_dir(agent_id=aid, config=cfg)
    resolved_team_id = _resolve_team_id_for_user(
        user=user,
        team_id=team_id,
        storage=storage,
        config=cfg,
    )

    # Conflict check BEFORE any side effects.
    if storage.openclaw_get(aid) is not None:
        raise AgentAlreadyExists(f"openclaw agent {aid!r} already exists")
    existing = oj.find_agent(aid, cfg)
    if existing is not None:
        # Another agent of the same id exists. If it's managed by us but
        # missing in DB, that's a corrupted state — refuse and demand manual
        # cleanup; otherwise it belongs to the user (refuse always).
        raise AgentAlreadyExists(
            f"openclaw.json already contains an agent named {aid!r}",
            details={"managed": oj.has_managed_agent(aid, cfg)},
        )

    workspace = _agent_workspace(aid)
    json_registered = False
    row_persisted = False

    # Filesystem-side preparation (cheap; reversible). Offloaded to a worker
    # thread — git init shells out repeatedly and must not block the loop.
    try:
        await asyncio.to_thread(
            deploy_common_agent_workspace, workspace, overwrite_agents_md=True
        )
    except FileNotFoundError as exc:
        raise OpenclawAgentError(
            f"common-agent source is missing: {exc}",
        ) from exc
    await asyncio.to_thread(
        _ensure_workspace_templates, workspace, agent_id=aid, agent_name=cmd.name
    )
    await asyncio.to_thread(_git_init_workspace, workspace)
    await _abort_commit_if_cancelled(
        aid,
        workspace=workspace,
        config=cfg,
        storage=storage,
        json_registered=json_registered,
        row_persisted=row_persisted,
    )
    installed_skills = await asyncio.to_thread(
        _install_user_skills, workspace, cmd.extra_skills
    )

    entry = _build_openclaw_entry(
        cmd,
        workspace,
        agent_dir=runtime_agent_dir,
    )

    # Critical section: openclaw.json edit + DB insert.
    # We DON'T hold the json lock during DB write (DB is independent),
    # but if either step fails we roll back the other.
    #
    # INVARIANT — do NOT introduce an `await` between append_managed_agent()
    # below and openclaw_create() further down. reindex_registered_agents()
    # (run by a concurrent list_agents/get_agent) ADOPTS any openclaw.json entry
    # that has no DB row yet. The entry is visible the instant append returns; if
    # the event loop could yield before the row is committed, that reconcile
    # would insert a row our own openclaw_create then collides with — the exact
    # create-vs-reconcile race that bit Hermes (see hermes_agents._CREATES_IN_
    # FLIGHT). Keeping this stretch await-free makes it atomic on the single
    # loop. If a future change must await here, add an in-flight-adopt guard
    # mirroring the Hermes fix.
    try:
        await oj.append_managed_agent(entry, config=cfg)
        json_registered = True
    except oj.OpenclawJsonError as exc:
        # Roll back FS (best-effort) so retry works.
        _safe_rmtree(workspace.parent)
        raise AgentAlreadyExists(str(exc)) from exc

    try:
        agent = OpenclawAgent(
            id=aid,
            name=cmd.name,
            description=cmd.description,
            team_id=resolved_team_id,
            workspace_path=str(workspace),
            openclaw_config_snapshot=entry,
            created_by_user=user,
            nl_prompt=cmd.nl_prompt,
        )
        saved = storage.openclaw_create(agent)
        row_persisted = True
    except Exception as exc:
        # Roll back openclaw.json change so the id is free again.
        try:
            await oj.remove_managed_agent(aid, config=cfg)
        except Exception:  # pragma: no cover - rollback best-effort
            logger.warning("rollback_openclaw_json_failed", agent_id=aid)
        _safe_rmtree(workspace.parent)
        raise OpenclawAgentError(f"failed to persist OpenclawAgent row: {exc}") from exc

    seeded_auth_profiles = 0
    try:
        seeded_auth_profiles = await asyncio.to_thread(
            _seed_portable_static_auth_profiles,
            agent_id=aid,
            config=cfg,
            source_agent_id=auth_seed_source_agent_id,
        )
    except Exception as exc:  # pragma: no cover - non-blocking best-effort path
        logger.warning(
            "openclaw_agent_auth_seed_failed",
            agent_id=aid,
            error=str(exc)[:240],
        )

    await _abort_commit_if_cancelled(
        aid,
        workspace=workspace,
        config=cfg,
        storage=storage,
        json_registered=json_registered,
        row_persisted=row_persisted,
    )

    sessions_dir_ready = False
    try:
        sessions_dir_ready = await asyncio.to_thread(
            _ensure_agent_sessions_dir, agent_id=aid, config=cfg
        )
    except Exception as exc:  # pragma: no cover - non-blocking best-effort path
        logger.warning(
            "openclaw_agent_sessions_dir_prepare_failed",
            agent_id=aid,
            error=str(exc)[:240],
        )

    # Cron sync shells out to the openclaw CLI (list + add/edit, 15s timeout
    # each) — by far the worst event-loop offender; keep it on a worker thread.
    entropy_scheduled = await asyncio.to_thread(
        _schedule_default_entropy_management_task, agent_id=aid, config=cfg
    )
    logger.info(
        "openclaw_agent_committed",
        agent_id=aid,
        user=user,
        skills_installed=installed_skills,
        auth_profiles_seeded=seeded_auth_profiles,
        sessions_dir_ready=sessions_dir_ready,
        entropy_scheduled=entropy_scheduled,
    )
    return saved


async def update_agent(
    agent_id: str,
    patch: UpdateInput,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> OpenclawAgent:
    """Patch an agent's identity / display fields.

    Refuses non-managed agents. Description is persisted in DB only;
    ``openclaw.json`` is kept free of unsupported keys.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)
    aid = _validate_agent_id(agent_id)

    row = storage.openclaw_get(aid)
    if row is None:
        raise AgentNotFound(f"openclaw agent {aid!r} not found")
    existing = oj.find_agent(aid, cfg)
    if existing is None:
        raise AgentNotFound(
            f"openclaw agent {aid!r} not found in openclaw.json (corrupted state)"
        )
    if not oj.has_managed_agent(aid, cfg):
        raise AgentUnmanaged(f"agent {aid!r} is not managed by ClawsomeFlow")

    # Compose the new entry.
    new_name = patch.name if patch.name is not None else row.name
    new_desc = patch.description if patch.description is not None else row.description
    new_identity = patch.identity if patch.identity is not None else None
    new_team_id = row.team_id
    if patch.team_id is not None:
        requested_team_id = patch.team_id.strip()
        if requested_team_id:
            target_team = get_team(
                requested_team_id,
                user=row.created_by_user,
                storage=storage,
                config=cfg,
            )
            new_team_id = target_team.id
        else:
            # Explicit empty team id means "ungrouped".
            new_team_id = ""

    def _mut(data: dict[str, Any]) -> None:
        for a in data["agents"]["list"]:
            if a.get("id") != aid:
                continue
            # OpenClaw schema currently rejects this legacy key.
            a.pop("description", None)
            if patch.name is not None:
                a["name"] = patch.name
            if patch.model is not None:
                a["model"] = patch.model
            if new_identity is not None:
                a["identity"] = new_identity.to_openclaw_dict(new_name)
            return
        raise AgentNotFound(f"agent {aid!r} disappeared from openclaw.json")

    snapshot = await oj.update_openclaw_json(
        _mut, config=cfg, operation="update_managed_agent", agent_id=aid,
    )
    new_entry = next(a for a in snapshot["agents"]["list"] if a["id"] == aid)

    row.name = new_name
    row.description = new_desc
    row.team_id = new_team_id
    row.openclaw_config_snapshot = new_entry
    saved = storage.openclaw_update(row)
    logger.info("openclaw_agent_updated", agent_id=aid)
    return saved


async def delete_agent(
    agent_id: str,
    *,
    mode: str | None = None,
    purge_workspace: bool | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> None:
    """Remove or unregister a ClawsomeFlow-managed OpenClaw agent.

    Modes:
    - ``unregister``: remove runtime registration only (keep DB + workspace)
    - ``purge``: remove runtime registration and purge DB + workspace
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    await asyncio.to_thread(reindex_registered_agents, storage=storage, config=cfg)
    aid = _validate_agent_id(agent_id)
    if mode is None:
        mode = _DELETE_MODE_PURGE if bool(purge_workspace) else _DELETE_MODE_UNREGISTER
    mode = (mode or "").strip().lower()
    if mode not in {_DELETE_MODE_UNREGISTER, _DELETE_MODE_PURGE}:
        raise OpenclawAgentError(
            f"unsupported delete mode: {mode!r}",
            details={"supported_modes": [_DELETE_MODE_UNREGISTER, _DELETE_MODE_PURGE]},
        )
    if purge_workspace is not None and mode == _DELETE_MODE_UNREGISTER and purge_workspace:
        raise OpenclawAgentError(
            "delete mode conflict: purge_workspace=true requires mode='purge'",
        )

    row = storage.openclaw_get(aid)
    if row is None:
        if mode != _DELETE_MODE_PURGE:
            raise AgentNotFound(f"openclaw agent {aid!r} not found")
        workspace = (paths.agent_dir(aid) / "workspace").resolve(strict=False)
        if not workspace.exists() or not workspace.is_dir():
            logger.info(
                "openclaw_agent_delete_noop",
                agent_id=aid,
                mode=mode,
                reason="workspace_orphan_missing",
            )
            return
        blocked = _collect_blocking_flow_names(
            storage=storage,
            user=cfg.default_user,
            agent_id=aid,
        )
        flow_names = blocked.get("flow_names", [])
        if isinstance(flow_names, list) and flow_names:
            raise AgentInUse(
                f"agent {aid!r} is used by existing Flows and cannot be removed",
                details=blocked,
            )
        # Drop any residual cron jobs before removing the runtime entry — the
        # cron store is keyed by agent id independently of openclaw.json.
        orphan_cron_removed, orphan_cron_remove_failed = await asyncio.to_thread(
            _remove_all_agent_cron_jobs, agent_id=aid, config=cfg
        )
        if oj.has_managed_agent(aid, cfg):
            try:
                await oj.remove_managed_agent(aid, config=cfg)
            except oj.OpenclawJsonError as exc:
                raise AgentUnmanaged(str(exc)) from exc
        await asyncio.to_thread(_safe_rmtree, workspace.parent)
        await asyncio.to_thread(_purge_agent_runtime_home, agent_id=aid, config=cfg)
        logger.info(
            "openclaw_agent_deleted",
            agent_id=aid,
            mode=mode,
            purged=True,
            removed_from_json=False,
            workspace_orphan=True,
            cron_jobs_removed=orphan_cron_removed,
            cron_remove_failed=orphan_cron_remove_failed,
        )
        return

    blocked = _collect_blocking_flow_names(
        storage=storage,
        user=row.created_by_user,
        agent_id=aid,
    )
    flow_names = blocked.get("flow_names", [])
    if isinstance(flow_names, list) and flow_names:
        raise AgentInUse(
            f"agent {aid!r} is used by existing Flows and cannot be removed",
            details=blocked,
        )

    backup_entries_count = 0
    backup_captured = False
    if mode == _DELETE_MODE_UNREGISTER and oj.has_managed_agent(aid, cfg):
        # Cron backup shells out to the openclaw CLI — keep it off the loop.
        backup_entries, backup_captured = await asyncio.to_thread(
            _capture_custom_cron_backup,
            agent_id=aid,
            current_snapshot=(
                row.openclaw_config_snapshot
                if isinstance(row.openclaw_config_snapshot, dict)
                else None
            ),
            config=cfg,
        )
        row.openclaw_config_snapshot = _update_snapshot_with_cron_backup(
            snapshot=row.openclaw_config_snapshot
            if isinstance(row.openclaw_config_snapshot, dict)
            else None,
            backup_entries=backup_entries,
            mark_captured=True,
        )
        row = storage.openclaw_update(row)
        backup_entries_count = len(backup_entries)

    # Remove ALL runtime cron jobs (system entropy + custom) for both unregister
    # and purge, so neither leaves cron jobs firing in OpenClaw. Custom jobs were
    # just backed up (unregister) and are replayed on restore; the entropy job is
    # re-scheduled on restore. Done while the agent still exists in openclaw.json
    # (safest for the cron CLI). Best-effort — must not block removal.
    cron_removed, cron_remove_failed = await asyncio.to_thread(
        _remove_all_agent_cron_jobs, agent_id=aid, config=cfg
    )

    removed_from_json = False
    if oj.has_managed_agent(aid, cfg):
        # Managed runtime entries are removable. Already-unregistered agents are
        # intentionally allowed (for restore/purge lifecycle).
        try:
            removed_from_json = await oj.remove_managed_agent(aid, config=cfg)
        except oj.OpenclawJsonError as exc:
            raise AgentUnmanaged(str(exc)) from exc

    if mode == _DELETE_MODE_PURGE:
        storage.openclaw_delete(aid)
        await asyncio.to_thread(_safe_rmtree, Path(row.workspace_path).parent)
        # Also drop OpenClaw's own per-agent runtime home (sessions + seeded
        # auth-profiles) — it lives under ~/.openclaw and is NOT removed by
        # remove_managed_agent, so a purge without this leaks it forever.
        await asyncio.to_thread(_purge_agent_runtime_home, agent_id=aid, config=cfg)

    logger.info(
        "openclaw_agent_deleted",
        agent_id=aid,
        mode=mode,
        purged=(mode == _DELETE_MODE_PURGE),
        removed_from_json=removed_from_json,
        cron_backup_entries=backup_entries_count,
        cron_backup_captured=backup_captured,
        cron_jobs_removed=cron_removed,
        cron_remove_failed=cron_remove_failed,
    )


def user_may_purge_workspace_orphan(
    agent_id: str,
    *,
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> bool:
    """True when *user* may purge/idempotently re-purge a DB-less managed workspace."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    try:
        aid = _validate_agent_id(agent_id)
    except AgentIdInvalid:
        return False
    if storage.openclaw_get(aid) is not None:
        return False
    return user == cfg.default_user


def get_agent(
    agent_id: str,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> OpenclawAgent:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)
    aid = _validate_agent_id(agent_id)
    row = storage.openclaw_get(aid)
    if row is None:
        raise AgentNotFound(f"openclaw agent {aid!r} not found")
    return row


def list_agents(
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> list[OpenclawAgent]:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)
    rows = storage.openclaw_list(owner_user=user)
    if not rows:
        return []

    try:
        payload = oj.load_openclaw_json(cfg)
        runtime_agents = payload.get("agents", {}).get("list", [])
        runtime_ids = {
            item.get("id")
            for item in runtime_agents
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        managed_ids = set(oj.list_managed_agent_ids())
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.warning("openclaw_list_agents_runtime_scan_failed", error=str(exc))
        try:
            managed_ids = set(oj.list_managed_agent_ids())
        except Exception as managed_exc:  # pragma: no cover - defensive fallback
            logger.warning(
                "openclaw_list_agents_registry_scan_failed",
                error=str(managed_exc),
            )
            managed_ids = set()
        return [row for row in rows if row.id in managed_ids]

    visible_ids = runtime_ids.intersection(managed_ids)
    return [row for row in rows if row.id in visible_ids]


def list_restorable_agents(
    *,
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> list[RestorableAgentCandidate]:
    """List candidates that can be re-registered into OpenClaw runtime."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)

    runtime_ids: set[str] = set()
    try:
        payload = oj.load_openclaw_json(cfg)
        runtime_agents = payload.get("agents", {}).get("list", [])
        if isinstance(runtime_agents, list):
            for raw in runtime_agents:
                if isinstance(raw, dict):
                    aid = raw.get("id")
                    if isinstance(aid, str) and aid:
                        runtime_ids.add(aid)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("restore_candidates_runtime_scan_failed", error=str(exc))

    out: dict[str, RestorableAgentCandidate] = {}
    for row in storage.openclaw_list(owner_user=user):
        if row.id in runtime_ids:
            continue
        ws = Path(row.workspace_path).expanduser()
        if not ws.exists() or not ws.is_dir():
            continue
        out[row.id] = RestorableAgentCandidate(
            id=row.id,
            name=row.name,
            description=row.description,
            team_id=row.team_id,
            workspace_path=str(ws),
            created_by_user=row.created_by_user,
        )

    if user == cfg.default_user:
        agents_root = paths.agents_dir()
        if agents_root.exists():
            for child in agents_root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    aid = _validate_agent_id(child.name)
                except AgentIdInvalid:
                    continue
                if aid in out or aid in runtime_ids:
                    continue
                ws = child / "workspace"
                if not ws.exists() or not ws.is_dir():
                    continue
                row = storage.openclaw_get(aid)
                if row is not None and row.created_by_user != user:
                    continue
                out[aid] = RestorableAgentCandidate(
                    id=aid,
                    name=row.name if row is not None else aid,
                    description=row.description if row is not None else "",
                    team_id=row.team_id if row is not None else "",
                    workspace_path=str(ws),
                    created_by_user=user,
                )

    return sorted(out.values(), key=lambda item: (item.name.lower(), item.id))


async def restore_agent_registration(
    agent_id: str,
    *,
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> OpenclawAgent:
    """Re-register one previously-unregistered managed agent into runtime."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    await asyncio.to_thread(reindex_registered_agents, storage=storage, config=cfg)
    aid = _validate_agent_id(agent_id)

    existing_runtime = oj.find_agent(aid, cfg)
    if existing_runtime is not None:
        row = storage.openclaw_get(aid)
        if row is None:
            raise AgentAlreadyExists(
                f"agent {aid!r} already exists in runtime",
                details={"managed": oj.has_managed_agent(aid, cfg)},
            )
        if row.created_by_user != user:
            raise AgentUnmanaged("agent belongs to a different user")
        restored_cron_jobs = await asyncio.to_thread(
            _restore_custom_cron_jobs_from_backup,
            agent_id=aid,
            snapshot=row.openclaw_config_snapshot
            if isinstance(row.openclaw_config_snapshot, dict)
            else None,
            config=cfg,
        )
        if restored_cron_jobs:
            row.openclaw_config_snapshot = _update_snapshot_with_cron_backup(
                snapshot=row.openclaw_config_snapshot
                if isinstance(row.openclaw_config_snapshot, dict)
                else None,
                backup_entries=_normalize_custom_cron_backup_entries(
                    row.openclaw_config_snapshot.get(_CRON_BACKUP_SNAPSHOT_KEY)
                    if isinstance(row.openclaw_config_snapshot, dict)
                    else None
                ),
                mark_restored=True,
            )
            row = storage.openclaw_update(row)
            logger.info(
                "openclaw_agent_restored_existing_runtime_cron_synced",
                agent_id=aid,
                restored_cron_jobs=restored_cron_jobs,
            )
        return row

    row = storage.openclaw_get(aid)
    if row is not None and row.created_by_user != user:
        raise AgentUnmanaged("agent belongs to a different user")

    workspace = (
        Path(row.workspace_path).expanduser().resolve(strict=False)
        if row is not None
        else (paths.agent_dir(aid) / "workspace").resolve(strict=False)
    )
    if not workspace.exists() or not workspace.is_dir():
        raise AgentNotFound(
            f"workspace for agent {aid!r} not found: {workspace}",
        )

    if row is None:
        row = storage.openclaw_create(
            OpenclawAgent(
                id=aid,
                name=aid,
                description="",
                team_id="",
                workspace_path=str(workspace),
                openclaw_config_snapshot={},
                created_by_user=user,
                nl_prompt="",
            )
        )

    raw_snapshot = row.openclaw_config_snapshot if isinstance(row.openclaw_config_snapshot, dict) else {}
    raw_identity = raw_snapshot.get("identity")
    identity = AgentIdentity(
        emoji=raw_identity.get("emoji") if isinstance(raw_identity, dict) else None,
        theme=raw_identity.get("theme") if isinstance(raw_identity, dict) else None,
        display_name=raw_identity.get("name") if isinstance(raw_identity, dict) else None,
    )
    raw_model = raw_snapshot.get("model")
    model = raw_model if isinstance(raw_model, str) and raw_model.strip() else None
    entry = _build_openclaw_entry(
        CommitInput(
            id=aid,
            name=row.name or aid,
            description=row.description,
            identity=identity,
            model=model,
            nl_prompt=row.nl_prompt,
        ),
        workspace,
        agent_dir=_runtime_agent_dir(agent_id=aid, config=cfg),
    )
    try:
        await oj.append_managed_agent(entry, config=cfg)
    except oj.OpenclawJsonError as exc:
        msg = str(exc)
        if "already exists in openclaw.json" in msg:
            raise AgentAlreadyExists(
                f"agent {aid!r} already exists in runtime",
                details={"managed": oj.has_managed_agent(aid, cfg)},
            ) from exc
        raise OpenclawAgentError(msg) from exc
    row.openclaw_config_snapshot = entry
    saved = storage.openclaw_update(row)
    # Both calls shell out to the openclaw CLI — offload off the event loop.
    entropy_scheduled = await asyncio.to_thread(
        _schedule_default_entropy_management_task, agent_id=aid, config=cfg
    )
    restored_cron_jobs = await asyncio.to_thread(
        _restore_custom_cron_jobs_from_backup,
        agent_id=aid,
        snapshot=raw_snapshot,
        config=cfg,
    )
    if restored_cron_jobs:
        saved.openclaw_config_snapshot = _update_snapshot_with_cron_backup(
            snapshot=saved.openclaw_config_snapshot
            if isinstance(saved.openclaw_config_snapshot, dict)
            else None,
            backup_entries=_normalize_custom_cron_backup_entries(
                raw_snapshot.get(_CRON_BACKUP_SNAPSHOT_KEY)
            ),
            mark_restored=True,
        )
        saved = storage.openclaw_update(saved)
    logger.info(
        "openclaw_agent_restored",
        agent_id=aid,
        entropy_scheduled=entropy_scheduled,
        restored_cron_jobs=restored_cron_jobs,
    )
    return saved


async def restore_all_agent_registrations(
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Re-register every restorable managed agent into OpenClaw runtime."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    owner = user or cfg.default_user
    candidates = list_restorable_agents(user=owner, storage=storage, config=cfg)
    restored: list[str] = []
    failed: dict[str, str] = {}
    for item in candidates:
        try:
            await restore_agent_registration(
                item.id,
                user=owner,
                storage=storage,
                config=cfg,
            )
            restored.append(item.id)
        except Exception as exc:  # pragma: no cover - defensive logging path
            failed[item.id] = str(exc)
            logger.warning(
                "openclaw_agent_restore_bulk_item_failed",
                agent_id=item.id,
                error=str(exc),
            )
    logger.info(
        "openclaw_agent_restore_bulk_done",
        requested_count=len(candidates),
        restored_count=len(restored),
        failed_count=len(failed),
        restored=restored,
        failed=failed,
    )
    return restored, failed


def list_external_import_candidates(
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> list[ExternalImportCandidate]:
    """List runtime agents that are registered but not managed by ClawsomeFlow."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)
    candidates = _load_external_candidate_map(storage=storage, config=cfg)
    return sorted(
        candidates.values(),
        key=lambda item: (item.name.lower(), item.id),
    )


async def import_external_agent(
    source_agent_id: str,
    *,
    user: str,
    team_id: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> ImportedExternalAgent:
    """Import one unmanaged runtime agent into ClawsomeFlow governance."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    await asyncio.to_thread(reindex_registered_agents, storage=storage, config=cfg)
    source_id = _validate_agent_id(source_agent_id)
    candidate_map = _load_external_candidate_map(storage=storage, config=cfg)
    source = candidate_map.get(source_id)
    if source is None:
        raise ExternalAgentNotFound(
            f"external agent {source_id!r} not found in runtime candidates"
        )
    source_workspace = Path(source.workspace_path).expanduser().resolve(strict=False)
    if not source.workspace_path:
        raise ExternalWorkspaceInvalid(
            f"external agent {source_id!r} does not have a workspace path"
        )
    if not source_workspace.exists() or not source_workspace.is_dir():
        raise ExternalWorkspaceInvalid(
            f"external agent {source_id!r} workspace not found: {source_workspace}"
        )

    target_id = _import_target_agent_id(source_id)
    target_name = f"csflow-{source.name}"
    created = await commit_agent(
        CommitInput(
            id=target_id,
            name=target_name,
            description=source.description,
            nl_prompt=f"[imported from runtime agent {source_id}]",
        ),
        user=user,
        team_id=team_id,
        storage=storage,
        config=cfg,
    )
    target_workspace = Path(created.workspace_path).expanduser().resolve(strict=False)
    if target_workspace == source_workspace:
        raise ExternalWorkspaceInvalid(
            f"source and target workspace are identical: {target_workspace}"
        )

    def _overlay_workspace() -> None:
        # Recursive copytree + skill install + AGENTS.md rewrite — all blocking
        # filesystem work; run in a worker thread to keep the loop responsive.
        _copy_workspace_overlay(source=source_workspace, target=target_workspace)
        deploy_common_agent_workspace(target_workspace, overwrite_agents_md=False)
        _install_user_skills(target_workspace, ())
        _rewrite_imported_agents_md(
            workspace=target_workspace,
            source_workspace=source_workspace,
        )

    try:
        await asyncio.to_thread(_overlay_workspace)
    except Exception as exc:
        try:
            await delete_agent(
                target_id,
                purge_workspace=True,
                storage=storage,
                config=cfg,
            )
        except Exception:
            logger.warning(
                "openclaw_import_cleanup_failed",
                source_agent_id=source_id,
                target_agent_id=target_id,
            )
        raise OpenclawAgentError(
            f"failed to import external agent {source_id!r}: {exc}"
        ) from exc

    logger.info(
        "openclaw_external_agent_imported",
        source_agent_id=source_id,
        target_agent_id=target_id,
        source_workspace=str(source_workspace),
        target_workspace=str(target_workspace),
        user=user,
    )
    return ImportedExternalAgent(
        source_agent_id=source.id,
        source_agent_name=source.name,
        target_agent_id=created.id,
        target_agent_name=created.name,
        target_workspace_path=created.workspace_path,
        target_team_id=created.team_id,
        target_team_name=(
            get_team(
                created.team_id,
                user=user,
                storage=storage,
                config=cfg,
            ).name
            if created.team_id
            else ""
        ),
    )


def reinstall_skills(
    agent_id: str,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
    common_cron_definitions: tuple[_CommonCronJobDefinition, ...] | None = None,
) -> list[str]:
    """Re-deploy runtime materials and standard skills into one workspace.

    Includes:
    - managed-agent ``AGENTS.md`` common-rules refresh while preserving
      ``AGENTS_USER_CUSTOM_SECTION``;
    - common skills from ``~/.clawsomeflow/.common-agent-source/skills/``;
    - dynamically discovered skills from ``.skills-source``.
    - common built-in cron job definitions from
      ``~/.clawsomeflow/.common-agent-source/cron-jobs/``.

    Used by ``csflow agents reinstall-skills`` and by ``csflow upgrade-runtime`` to
    roll out new docs/skills/tool scripts into agents created before these
    materials existed.

    Returns the list of skill names installed.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)
    aid = _validate_agent_id(agent_id)
    row = storage.openclaw_get(aid)
    if row is None:
        raise AgentNotFound(f"openclaw agent {aid!r} not found")
    workspace = Path(row.workspace_path)
    if not workspace.exists():
        # Workspace was purged at some point; recreate so install can land.
        workspace.mkdir(parents=True, exist_ok=True)
    deploy_common_agent_workspace(workspace, overwrite_agents_md=False)
    installed = _install_user_skills(workspace, ())
    cron_synced = _sync_common_cron_jobs_for_agent(
        agent_id=aid,
        definitions=common_cron_definitions,
        config=cfg,
    )
    logger.info(
        "reinstall_skills_complete",
        agent_id=aid,
        skills_installed=installed,
        common_cron_synced=cron_synced,
    )
    return installed


def reinstall_skills_for_all(
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> dict[str, list[str]]:
    """Reinstall runtime materials for every managed OpenClaw agent.

    Returns a mapping ``agent_id → installed skill names``. Skips agents
    whose workspace_path doesn't exist (logged warning).
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)
    common_cron_definitions = _load_common_cron_job_definitions()
    out: dict[str, list[str]] = {}
    for a in storage.openclaw_list(owner_user=user):
        try:
            out[a.id] = reinstall_skills(
                a.id,
                storage=storage,
                config=cfg,
                common_cron_definitions=common_cron_definitions,
            )
        except Exception as exc:
            logger.warning(
                "reinstall_skills_failed", agent_id=a.id, error=str(exc),
            )
            out[a.id] = []
    return out


def sync_common_cron_jobs_for_all(
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> dict[str, bool]:
    """Ensure every managed agent has the latest built-in cron definitions."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    reindex_registered_agents(storage=storage, config=cfg)
    common_cron_definitions = _load_common_cron_job_definitions()
    out: dict[str, bool] = {}
    for row in storage.openclaw_list(owner_user=user):
        try:
            ok = _sync_common_cron_jobs_for_agent(
                agent_id=row.id,
                definitions=common_cron_definitions,
                config=cfg,
            )
        except Exception as exc:
            logger.warning(
                "sync_common_cron_jobs_for_all_failed",
                agent_id=row.id,
                error=str(exc),
            )
            ok = False
        out[row.id] = ok
    return out


# ──────────────────────────────────────────────────────────────────────
# Filesystem helpers
# ──────────────────────────────────────────────────────────────────────


def _safe_rmtree(path: Path) -> None:
    """``shutil.rmtree`` swallowing missing-path / permission errors."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:  # pragma: no cover - best-effort
        logger.warning("rmtree_failed", path=str(path), error=str(exc))


__all__ = [
    "AgentAlreadyExists",
    "AgentIdentity",
    "AgentIdInvalid",
    "AgentInUse",
    "AgentNotFound",
    "AgentUnmanaged",
    "ExternalAgentNotFound",
    "ExternalImportCandidate",
    "ExternalWorkspaceInvalid",
    "RestorableAgentCandidate",
    "TeamNotFound",
    "CommitInput",
    "ImportedExternalAgent",
    "OpenclawAgentError",
    "UpdateInput",
    "create_team",
    "AgentCreateCancelled",
    "clear_create_cancellation",
    "commit_agent",
    "finish_create_in_flight",
    "is_bootstrap_complete",
    "is_create_in_flight",
    "is_create_cancelled",
    "request_create_cancellation",
    "delete_agent",
    "get_team",
    "get_agent",
    "import_external_agent",
    "list_agents",
    "list_restorable_agents",
    "list_teams",
    "list_external_import_candidates",
    "restore_all_agent_registrations",
    "restore_agent_registration",
    "reindex_registered_agents",
    "resolve_runtime_gateway_url",
    "sync_common_cron_jobs_for_all",
    "update_agent",
    "update_team",
    "user_may_purge_workspace_orphan",
]
