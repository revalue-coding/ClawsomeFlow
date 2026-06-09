"""Management service for env-home managed agents (Claude Code / Codex / Cursor).

Each managed agent owns a relocatable config home under
``~/.clawsomeflow/agents/{id}/{kind}-home`` and a ClawTeam runtime profile
(``csflow-{kind}-{id}``) that injects the home env var at spawn. Its skills/MCP
therefore follow the agent regardless of the per-task working directory.

CLI verified: ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` relocate config and are
honoured by ``<cli> mcp add/list`` (Codex requires the home dir to pre-exist).
Cursor (``CURSOR_CONFIG_DIR``) is wired the same way but is not end-to-end
verified here (binary absent).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Config, load_config
from app.logging_setup import get_logger
from app.models import Flow, ManagedAgent
from app.scheduler.managed_runtime import (
    HOME_ENV_VAR,
    KIND_CLI,
    MANAGED_KINDS,
    ensure_profile,
    managed_home,
    remove_profile,
)
from app.storage import StorageBackend, get_storage

logger = get_logger("services.managed_agents")

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_AGENT_ID_MIN_LEN = 2
_AGENT_ID_MAX_LEN = 48
_CLI_TIMEOUT_SEC = 60.0
_CHAT_TIMEOUT_SEC = 1800.0
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SKILL_FM_RE = re.compile(r"^---\s*\n(?P<h>.*?)\n---", re.DOTALL)


# ── errors ────────────────────────────────────────────────────────────


class ManagedAgentError(Exception):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class KindUnsupported(ManagedAgentError):
    ...


class AgentIdInvalid(ManagedAgentError):
    ...


class AgentAlreadyExists(ManagedAgentError):
    ...


class AgentNotFound(ManagedAgentError):
    ...


class AgentInUse(ManagedAgentError):
    ...


class CliUnavailable(ManagedAgentError):
    ...


class CliFailed(ManagedAgentError):
    ...


# ── helpers ───────────────────────────────────────────────────────────


def _strip(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _validate_kind(kind: str) -> str:
    if kind not in MANAGED_KINDS:
        raise KindUnsupported(f"unsupported managed kind: {kind!r}")
    return kind


def _validate_id(agent_id: str) -> str:
    aid = (agent_id or "").strip()
    if not _AGENT_ID_RE.fullmatch(aid) or not (_AGENT_ID_MIN_LEN <= len(aid) <= _AGENT_ID_MAX_LEN):
        raise AgentIdInvalid(
            "agent id must be lowercase [a-z0-9-], start alphanumeric, "
            f"length {_AGENT_ID_MIN_LEN}-{_AGENT_ID_MAX_LEN}"
        )
    return aid


def _cli_for(kind: str) -> str:
    return KIND_CLI.get(kind, kind)


def _env_for(kind: str, agent_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env[HOME_ENV_VAR[kind]] = str(managed_home(kind, agent_id))
    return env


def cli_available(kind: str) -> bool:
    return shutil.which(_cli_for(kind)) is not None


def _run_cli(
    kind: str, agent_id: str, args: list[str], *, cwd: str | Path | None = None,
    timeout: float = _CLI_TIMEOUT_SEC,
) -> tuple[int, str, str]:
    exe = shutil.which(_cli_for(kind))
    if exe is None:
        raise CliUnavailable(f"`{_cli_for(kind)}` CLI not found on PATH")
    managed_home(kind, agent_id).mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, *args], cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, timeout=timeout, env=_env_for(kind, agent_id),
        )
    except subprocess.TimeoutExpired as exc:
        raise CliFailed(f"{_cli_for(kind)} {' '.join(args[:2])} timed out") from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def probe_runtime_running(kind: str) -> tuple[bool, str]:
    _validate_kind(kind)
    if cli_available(kind):
        return True, f"{_cli_for(kind)} available"
    return False, f"`{_cli_for(kind)}` CLI not installed"


# ── role-doc seeding ──────────────────────────────────────────────────


def _seed_role_doc(kind: str, home: Path, name: str, responsibility: str) -> None:
    """Seed the user-level role/identity doc inside the config home.

    Claude reads ``<home>/CLAUDE.md`` as user memory; Codex/Cursor use AGENTS.md.
    """
    body = (
        f"# {name}\n\n"
        f"You are **{name}**, a managed agent in ClawsomeFlow.\n\n"
        f"## Responsibility\n{responsibility or name}\n"
    )
    fname = "CLAUDE.md" if kind == "claude" else "AGENTS.md"
    (home / fname).write_text(body, encoding="utf-8")
    (home / "skills").mkdir(parents=True, exist_ok=True)


# ── CRUD ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommitInput:
    id: str
    kind: str
    name: str
    description: str = ""
    nl_prompt: str = ""
    team_id: str = ""


def commit_agent(
    cmd: CommitInput, *, user: str,
    storage: StorageBackend | None = None, config: Config | None = None,
) -> ManagedAgent:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    kind = _validate_kind(cmd.kind)
    aid = _validate_id(cmd.id)
    if storage.managed_get(aid) is not None:
        raise AgentAlreadyExists(f"managed agent {aid!r} already exists")

    home = managed_home(kind, aid)
    home.mkdir(parents=True, exist_ok=True)
    _seed_role_doc(kind, home, cmd.name.strip() or aid, (cmd.description or "").strip())
    profile = ensure_profile(kind, aid)

    row = ManagedAgent(
        id=aid, kind=kind, name=cmd.name.strip() or aid,
        description=(cmd.description or "").strip(), team_id=cmd.team_id or "",
        config_home=str(home), clawteam_profile=profile,
        created_by_user=user, nl_prompt=cmd.nl_prompt or cmd.description or "",
    )
    try:
        return storage.managed_create(row)
    except Exception:
        remove_profile(kind, aid)
        shutil.rmtree(home, ignore_errors=True)
        raise


def get_agent(
    agent_id: str, *, storage: StorageBackend | None = None, config: Config | None = None,
) -> ManagedAgent:
    storage = storage or get_storage(config or load_config())
    row = storage.managed_get(_validate_id(agent_id))
    if row is None:
        raise AgentNotFound(f"managed agent {agent_id!r} not found")
    return row


def list_agents(
    *, user: str | None = None, kind: str | None = None,
    storage: StorageBackend | None = None, config: Config | None = None,
) -> list[ManagedAgent]:
    storage = storage or get_storage(config or load_config())
    return storage.managed_list(owner_user=user, kind=kind)


@dataclass
class UpdateInput:
    name: str | None = None
    description: str | None = None
    team_id: str | None = None


def update_agent(
    agent_id: str, patch: UpdateInput, *,
    storage: StorageBackend | None = None, config: Config | None = None,
) -> ManagedAgent:
    storage = storage or get_storage(config or load_config())
    row = get_agent(agent_id, storage=storage)
    if patch.name is not None:
        row.name = patch.name.strip() or row.name
    if patch.description is not None:
        row.description = patch.description.strip()
    if patch.team_id is not None:
        row.team_id = patch.team_id
    return storage.managed_update(row)


def delete_agent(
    agent_id: str, *, storage: StorageBackend | None = None, config: Config | None = None,
) -> None:
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    aid = _validate_id(agent_id)
    row = storage.managed_get(aid)
    if row is None:
        raise AgentNotFound(f"managed agent {aid!r} not found")
    blocked = _collect_blocking_flow_names(storage=storage, user=row.created_by_user, agent_id=aid)
    if blocked["flow_names"]:
        raise AgentInUse(
            f"agent {aid!r} is used by existing Flows and cannot be removed",
            details=blocked,
        )
    remove_profile(row.kind, aid)
    shutil.rmtree(Path(row.config_home), ignore_errors=True)
    storage.managed_delete(aid)
    logger.info("managed_agent_deleted", agent_id=aid, kind=row.kind)


def is_managed(agent_id: str, kind: str, *, storage: StorageBackend) -> bool:
    """True if ``agent_id`` is a managed agent of ``kind`` (for Flow validation)."""
    try:
        aid = _validate_id(agent_id)
    except AgentIdInvalid:
        return False
    row = storage.managed_get(aid)
    return row is not None and row.kind == kind


# ── flow-usage guard ──────────────────────────────────────────────────


def _iter_user_flows(*, storage: StorageBackend, user: str) -> list[Flow]:
    flows: list[Flow] = []
    offset = 0
    while True:
        page, total = storage.flow_list(owner_user=user, limit=200, offset=offset)
        flows.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break
    return flows


def _collect_blocking_flow_names(
    *, storage: StorageBackend, user: str, agent_id: str,
) -> dict[str, list[str]]:
    names: set[str] = set()
    for flow in _iter_user_flows(storage=storage, user=user):
        raw_agents = flow.spec.get("agents", []) if isinstance(flow.spec, dict) else []
        if isinstance(raw_agents, list) and any(
            isinstance(a, dict) and a.get("id") == agent_id
            and a.get("kind") in MANAGED_KINDS
            for a in raw_agents
        ):
            names.add(flow.name or flow.id)
    return {"flow_names": sorted(names)}


# ── settings: MCP ─────────────────────────────────────────────────────


def list_mcp(agent_id: str, *, storage: StorageBackend | None = None) -> list[dict[str, str]]:
    row = get_agent(agent_id, storage=storage)
    rc, out, _err = _run_cli(row.kind, row.id, ["mcp", "list"])
    if rc != 0:
        return []
    servers: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = _strip(raw).strip()
        if not line:
            continue
        low = line.lower()
        if "no mcp servers" in low or low.startswith("checking") or low.startswith("name "):
            continue
        # claude: "name: cmd - status"; codex: tabular "name cmd ..."
        if ":" in line and row.kind == "claude":
            name = line.split(":", 1)[0].strip()
        else:
            name = line.split()[0].strip()
        if name and name.lower() not in {"name", "-"}:
            servers.append({"name": name, "detail": line})
    return servers


def add_mcp(
    agent_id: str, *, name: str, command: list[str],
    storage: StorageBackend | None = None,
) -> None:
    row = get_agent(agent_id, storage=storage)
    if not name.strip() or not command:
        raise ManagedAgentError("mcp name and command are required")
    if row.kind == "claude":
        args = ["mcp", "add", "--scope", "user", name, "--", *command]
    else:  # codex / cursor share `mcp add <name> -- cmd`
        args = ["mcp", "add", name, "--", *command]
    rc, out, err = _run_cli(row.kind, row.id, args)
    if rc != 0:
        raise CliFailed(f"`{_cli_for(row.kind)} mcp add` failed: {(_strip(err) or _strip(out)).strip()[:400]}")


def remove_mcp(agent_id: str, name: str, *, storage: StorageBackend | None = None) -> None:
    row = get_agent(agent_id, storage=storage)
    rc, out, err = _run_cli(row.kind, row.id, ["mcp", "remove", name])
    if rc != 0:
        raise CliFailed(f"`mcp remove` failed: {(_strip(err) or _strip(out)).strip()[:300]}")


# ── settings: skills (filesystem under config home) ───────────────────


def list_skills(agent_id: str, *, storage: StorageBackend | None = None) -> list[dict[str, str]]:
    row = get_agent(agent_id, storage=storage)
    sdir = Path(row.config_home) / "skills"
    out: list[dict[str, str]] = []
    if not sdir.is_dir():
        return out
    for entry in sorted(sdir.iterdir()):
        md = entry / "SKILL.md"
        if not md.is_file():
            continue
        desc = ""
        m = _SKILL_FM_RE.match(md.read_text(encoding="utf-8", errors="replace"))
        if m:
            dm = re.search(r"description:\s*(.+)", m.group("h"))
            if dm:
                desc = dm.group(1).strip().strip('"').strip("'")
        out.append({"name": entry.name, "description": desc, "path": str(entry)})
    return out


def read_skill(agent_id: str, name: str, *, storage: StorageBackend | None = None) -> str:
    row = get_agent(agent_id, storage=storage)
    md = Path(row.config_home) / "skills" / name / "SKILL.md"
    if not md.is_file():
        raise AgentNotFound(f"skill {name!r} not found")
    return md.read_text(encoding="utf-8", errors="replace")


# ── role doc (identity) ───────────────────────────────────────────────


def _role_doc_path(row: ManagedAgent) -> Path:
    fname = "CLAUDE.md" if row.kind == "claude" else "AGENTS.md"
    return Path(row.config_home) / fname


def read_role_doc(agent_id: str, *, storage: StorageBackend | None = None) -> str:
    row = get_agent(agent_id, storage=storage)
    p = _role_doc_path(row)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def write_role_doc(agent_id: str, content: str, *, storage: StorageBackend | None = None) -> str:
    row = get_agent(agent_id, storage=storage)
    Path(row.config_home).mkdir(parents=True, exist_ok=True)
    _role_doc_path(row).write_text(content, encoding="utf-8")
    return content


# ── direct chat (headless, profile-home, cwd = chosen workdir) ─────────


def chat_once(agent_id: str, *, message: str, workdir: str, storage: StorageBackend | None = None) -> str:
    row = get_agent(agent_id, storage=storage)
    wd = Path(workdir).expanduser()
    if not wd.is_dir():
        raise ManagedAgentError(f"working directory does not exist: {workdir}")
    if row.kind == "claude":
        args = ["-p", "--permission-mode", "bypassPermissions", message]
    elif row.kind == "codex":
        args = ["exec", "--dangerously-bypass-approvals-and-sandbox", message]
    else:  # cursor
        args = ["-p", "--force", message]
    rc, out, err = _run_cli(row.kind, row.id, args, cwd=wd, timeout=_CHAT_TIMEOUT_SEC)
    if rc != 0:
        raise CliFailed(f"chat failed: {(_strip(err) or _strip(out)).strip()[:1000]}")
    return out.strip()


__all__ = [
    "CommitInput", "UpdateInput", "ManagedAgentError", "KindUnsupported",
    "AgentIdInvalid", "AgentAlreadyExists", "AgentNotFound", "AgentInUse",
    "CliUnavailable", "CliFailed",
    "cli_available", "probe_runtime_running",
    "commit_agent", "get_agent", "list_agents", "update_agent", "delete_agent",
    "is_managed",
    "list_mcp", "add_mcp", "remove_mcp",
    "list_skills", "read_skill", "read_role_doc", "write_role_doc",
    "chat_once",
]
