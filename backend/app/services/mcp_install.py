"""Register the ClawsomeFlow MCP server into agent-platform configurations.

The server itself is a stdio process launched as ``csflow mcp serve``. This
module writes that server entry into each supported platform's MCP config so an
agent can call ClawsomeFlow's tools. Two shapes of target:

* **Hermes** — per-profile (each agent has its own ``config.yaml``). Requires an
  ``agent_id``; delegates to :mod:`app.services.hermes_agents`.
* **Everything else** — a single global config file per platform.

Auto-write platforms use a **non-destructive merge** (mirrors
``integrations/opencode_config.py``): an unparseable file is never clobbered, an
existing entry with the same name is left alone unless ``force=True``, and every
other key is preserved. Platforms whose MCP-config format ClawsomeFlow does not
manage fall back to :func:`print_config` (the user pastes the snippet).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.integrations.opencode_config import opencode_config_path
from app.logging_setup import get_logger
from app.mcp.server import SERVER_NAME

logger = get_logger("services.mcp_install")

# The stdio server invocation every platform registers.
SERVER_COMMAND = "csflow"
SERVER_ARGS = ["mcp", "serve"]

PlatformStyle = Literal["hermes", "json_mcp_servers", "opencode_json", "codex_toml", "manual"]


@dataclass(frozen=True)
class PlatformSpec:
    """How one platform stores its MCP-server config."""

    id: str
    style: PlatformStyle
    cli: str  # binary probed with shutil.which (for a friendly "not installed" hint)
    path: Callable[[], Path] | None = None  # global config file (None for hermes)
    note: str = ""


@dataclass(frozen=True)
class InstallResult:
    platform: str
    action: str  # written | unchanged | removed | manual | absent
    path: str | None
    message: str


def _claude_path() -> Path:
    return Path.home() / ".claude.json"


def _cursor_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _gemini_path() -> Path:
    return Path.home() / ".gemini" / "settings.json"


def _codex_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


# Registry. Order = display order for list-platforms.
_PLATFORMS: dict[str, PlatformSpec] = {
    "hermes": PlatformSpec("hermes", "hermes", "hermes", None,
                           "Per-agent profile via --agent <id>; omit --agent to use the default profile."),
    "openclaw": PlatformSpec("openclaw", "manual", "openclaw", None,
                             "OpenClaw has no global MCP-config file managed by ClawsomeFlow; "
                             "register the printed entry via OpenClaw's own configuration."),
    "claude": PlatformSpec("claude", "json_mcp_servers", "claude", _claude_path,
                           "Global ~/.claude.json mcpServers."),
    "cursor": PlatformSpec("cursor", "json_mcp_servers", "cursor", _cursor_path,
                           "Global ~/.cursor/mcp.json mcpServers."),
    "gemini": PlatformSpec("gemini", "json_mcp_servers", "gemini", _gemini_path,
                           "Global ~/.gemini/settings.json mcpServers."),
    "codex": PlatformSpec("codex", "codex_toml", "codex", _codex_path,
                          "Global ~/.codex/config.toml (mcp_servers table)."),
    "opencode": PlatformSpec("opencode", "opencode_json", "opencode", opencode_config_path,
                             "Global opencode.json mcp block."),
    "kimi": PlatformSpec("kimi", "manual", "kimi", None,
                         "MCP-config format not managed by ClawsomeFlow; use the printed entry."),
    "qwen": PlatformSpec("qwen", "manual", "qwen", None,
                         "MCP-config format not managed by ClawsomeFlow; use the printed entry."),
    "nanobot": PlatformSpec("nanobot", "manual", "nanobot", None,
                            "MCP-config format not managed by ClawsomeFlow; use the printed entry."),
}


def supported_platforms() -> list[str]:
    return list(_PLATFORMS)


def _spec(platform: str) -> PlatformSpec:
    spec = _PLATFORMS.get(platform.strip().lower())
    if spec is None:
        raise ValueError(
            f"unknown platform {platform!r}; supported: {', '.join(_PLATFORMS)}"
        )
    return spec


# ── config-shape renderers (also used by print_config) ──────────────────


def _json_mcp_servers_entry() -> dict:
    return {"command": SERVER_COMMAND, "args": list(SERVER_ARGS)}


def _opencode_entry() -> dict:
    return {"type": "local", "command": [SERVER_COMMAND, *SERVER_ARGS], "enabled": True}


def print_config(platform: str, *, name: str = SERVER_NAME) -> str:
    """Return a copy-pasteable config snippet for *platform* (no file writes)."""
    spec = _spec(platform)
    if spec.style == "hermes":
        return (
            f"# Hermes per-profile config.yaml (mcp_servers):\n"
            f"mcp_servers:\n  {name}:\n    command: {SERVER_COMMAND}\n"
            f"    args:\n      - {SERVER_ARGS[0]}\n      - {SERVER_ARGS[1]}\n    enabled: true\n"
        )
    if spec.style == "opencode_json":
        return json.dumps({"mcp": {name: _opencode_entry()}}, indent=2)
    if spec.style == "codex_toml":
        return _codex_block(name)
    # json_mcp_servers + manual → the widely-used mcpServers JSON shape.
    return json.dumps({"mcpServers": {name: _json_mcp_servers_entry()}}, indent=2)


def _codex_block(name: str) -> str:
    args = ", ".join(json.dumps(a) for a in SERVER_ARGS)
    return (
        f"[mcp_servers.{name}]\n"
        f"command = {json.dumps(SERVER_COMMAND)}\n"
        f"args = [{args}]\n"
    )


# ── install / uninstall ─────────────────────────────────────────────────


def install(
    platform: str,
    *,
    agent_id: str | None = None,
    name: str = SERVER_NAME,
    force: bool = False,
) -> InstallResult:
    """Register the ClawsomeFlow MCP server into *platform*'s config.

    ``force`` skips the "CLI installed?" gate and overwrites an existing entry
    of the same name. For Hermes, ``agent_id`` is required.
    """
    spec = _spec(platform)

    if spec.style == "hermes":
        return _install_hermes(agent_id=agent_id, name=name)

    if spec.style == "manual":
        return InstallResult(
            spec.id, "manual", None,
            f"{spec.id}: not auto-configurable — {spec.note}\n\n{print_config(spec.id, name=name)}",
        )

    if not force and shutil.which(spec.cli) is None:
        return InstallResult(
            spec.id, "absent", None,
            f"{spec.cli!r} not found on PATH; skipped. Re-run with --force to write anyway, "
            f"or use `csflow mcp print-config --platform {spec.id}`.",
        )

    assert spec.path is not None
    path = spec.path()
    if spec.style == "codex_toml":
        return _install_codex(path, name)
    if spec.style == "opencode_json":
        return _install_json(path, "mcp", _opencode_entry(), name, spec,
                             extra_root={"$schema": "https://opencode.ai/config.json"})
    return _install_json(path, "mcpServers", _json_mcp_servers_entry(), name, spec)


def uninstall(platform: str, *, agent_id: str | None = None, name: str = SERVER_NAME) -> InstallResult:
    """Remove the ClawsomeFlow MCP server entry from *platform*'s config."""
    spec = _spec(platform)

    if spec.style == "hermes":
        return _uninstall_hermes(agent_id=agent_id, name=name)
    if spec.style == "manual" or spec.path is None:
        return InstallResult(spec.id, "manual", None,
                             f"{spec.id}: remove the {name!r} entry manually.")

    path = spec.path()
    if not path.exists():
        return InstallResult(spec.id, "unchanged", str(path), "config file does not exist")
    if spec.style == "codex_toml":
        return _uninstall_codex(path, name)
    container = "mcp" if spec.style == "opencode_json" else "mcpServers"
    return _uninstall_json(path, container, name, spec)


# ── hermes ───────────────────────────────────────────────────────────────


def _install_hermes(*, agent_id: str | None, name: str) -> InstallResult:
    from app.services import hermes_agents

    if not agent_id:
        # No --agent → target the operator's default profile (the one `hermes`
        # uses without -p: active profile or root ~/.hermes).
        hermes_agents.upsert_default_profile_mcp_server(
            name=name, transport="stdio", url="",
            command=SERVER_COMMAND, args=list(SERVER_ARGS),
        )
        path = hermes_agents.default_profile_config_path()
        return InstallResult("hermes", "written", str(path),
                             f"registered {name!r} in Hermes default profile")

    hermes_agents.upsert_mcp_server(
        agent_id,
        name=name,
        transport="stdio",
        url="",
        command=SERVER_COMMAND,
        args=list(SERVER_ARGS),
    )
    return InstallResult("hermes", "written", None,
                         f"registered {name!r} in Hermes profile {agent_id!r}")


def _uninstall_hermes(*, agent_id: str | None, name: str) -> InstallResult:
    from app.services import hermes_agents

    if not agent_id:
        removed = hermes_agents.delete_default_profile_mcp_server(name)
        action = "removed" if removed else "unchanged"
        return InstallResult("hermes", action, str(hermes_agents.default_profile_config_path()),
                             f"{'removed' if removed else 'not present:'} {name!r} in Hermes default profile")

    try:
        hermes_agents.delete_mcp_server(agent_id, name)
    except Exception as exc:
        return InstallResult("hermes", "unchanged", None, f"nothing removed: {exc}")
    return InstallResult("hermes", "removed", None,
                         f"removed {name!r} from Hermes profile {agent_id!r}")


# ── generic JSON (mcpServers / opencode mcp) ─────────────────────────────


def _load_json_obj(path: Path) -> dict | None:
    """Return the parsed dict, {} if the file is absent, or None if unparseable."""
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("mcp_install_json_unreadable", path=str(path), error=str(exc))
        return None


def _install_json(
    path: Path,
    container_key: str,
    entry: dict,
    name: str,
    spec: PlatformSpec,
    *,
    extra_root: dict | None = None,
) -> InstallResult:
    data = _load_json_obj(path)
    if data is None:
        return InstallResult(spec.id, "unchanged", str(path),
                             "existing config is not valid JSON; left untouched — "
                             f"use `csflow mcp print-config --platform {spec.id}`")
    for k, v in (extra_root or {}).items():
        data.setdefault(k, v)
    container = data.get(container_key)
    if not isinstance(container, dict):
        container = {}
    if name in container:
        return InstallResult(spec.id, "unchanged", str(path),
                             f"{name!r} already present (use --force to overwrite)")
    container[name] = entry
    data[container_key] = container
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return InstallResult(spec.id, "written", str(path), f"registered {name!r}")


def _uninstall_json(path: Path, container_key: str, name: str, spec: PlatformSpec) -> InstallResult:
    data = _load_json_obj(path)
    if data is None:
        return InstallResult(spec.id, "unchanged", str(path), "config not valid JSON; left untouched")
    container = data.get(container_key)
    if not isinstance(container, dict) or name not in container:
        return InstallResult(spec.id, "unchanged", str(path), f"{name!r} not present")
    container.pop(name, None)
    if not container:
        data.pop(container_key, None)
    else:
        data[container_key] = container
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return InstallResult(spec.id, "removed", str(path), f"removed {name!r}")


# ── codex TOML (append-only, non-destructive) ────────────────────────────


def _install_codex(path: Path, name: str) -> InstallResult:
    header = f"[mcp_servers.{name}]"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if header in existing:
        return InstallResult("codex", "unchanged", str(path),
                             f"{name!r} already present (edit ~/.codex/config.toml to change)")
    block = _codex_block(name)
    sep = "" if existing.endswith("\n") or not existing else "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + sep + ("\n" if existing else "") + block, encoding="utf-8")
    return InstallResult("codex", "written", str(path), f"appended [mcp_servers.{name}]")


def _uninstall_codex(path: Path, name: str) -> InstallResult:
    header = f"[mcp_servers.{name}]"
    text = path.read_text(encoding="utf-8")
    if header not in text:
        return InstallResult("codex", "unchanged", str(path), f"{name!r} not present")
    # Drop the header line and the following non-blank, non-table lines.
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skipping = True
            continue
        if skipping:
            if stripped.startswith("[") or stripped == "":
                skipping = False
            else:
                continue
        out.append(line)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    return InstallResult("codex", "removed", str(path), f"removed [mcp_servers.{name}]")


__all__ = [
    "InstallResult",
    "PlatformSpec",
    "SERVER_ARGS",
    "SERVER_COMMAND",
    "install",
    "print_config",
    "supported_platforms",
    "uninstall",
]
