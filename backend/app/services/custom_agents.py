"""Custom agent registry (我的团队 → 自定义Agent).

A :class:`~app.models.CustomAgent` row registers an arbitrary agentic CLI that
satisfies the ClawsomeFlow/ClawTeam TUI contract. The scheduler drives it
through the **existing** ``AgentKind.custom`` + ``TmuxLiveSession`` path — this
module only owns the product layer:

* CRUD-level validation (name → id slug, shlex command parsing). Deliberately
  NOT a binary-availability probe — see the note above ``_spec_references_agent``.
* Flow-spec resolution: a Flow stores ``custom_agent_ref`` + a save-time
  ``command`` snapshot; :func:`resolve_spec_custom_agents` refreshes the argv
  from the registry at controller construction (registry preferred, snapshot
  fallback — never raises, so a deleted row can't break complaint/resume
  controllers; the strict gate is :func:`app.validators.flow.validate_flow_against_db`).
* Headless argv rendering for the chat dialog + the leader remote-param fill.
"""

from __future__ import annotations

import re
import shlex

from app.logging_setup import get_logger
from app.models import AgentKind, CustomAgent, FlowAgent, FlowSpec
from app.storage import StorageBackend

logger = get_logger("services.custom_agents")

MESSAGE_PLACEHOLDER = "{message}"

_MAX_NAME_LEN = 100
_MAX_DESCRIPTION_LEN = 2000
_MAX_READY_PATTERN_LEN = 300
_MAX_COMMAND_TOKENS = 64
# Kind values that would collide with platform enum names if used as an id.
_RESERVED_IDS = frozenset(k.value for k in AgentKind)


class CustomAgentError(Exception):
    """Service-level error carrying an API error code."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# ──────────────────────────────────────────────────────────────────────
# Field validation helpers
# ──────────────────────────────────────────────────────────────────────


def slugify_name(name: str) -> str:
    """Derive the registry id from the display name.

    Must survive as a :class:`FlowAgent.id` (charset ``[A-Za-z0-9_-]``) and as
    a ClawTeam agent_name. Non-ASCII names (e.g. Chinese) may slug to nothing —
    fall back to a stable hash suffix so any non-empty name yields an id.
    """
    text = (name or "").strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        import hashlib

        slug = "agent-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    if slug in _RESERVED_IDS:
        slug = f"{slug}-agent"
    return slug[:64]


def validate_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise CustomAgentError("INVALID_PAYLOAD", "name is required")
    if len(value) > _MAX_NAME_LEN:
        raise CustomAgentError(
            "INVALID_PAYLOAD", f"name too long (max {_MAX_NAME_LEN} chars)"
        )
    return value


def validate_description(description: str) -> str:
    value = (description or "").strip()
    if len(value) > _MAX_DESCRIPTION_LEN:
        raise CustomAgentError(
            "INVALID_PAYLOAD",
            f"description too long (max {_MAX_DESCRIPTION_LEN} chars)",
        )
    return value


def parse_command(
    text: str, *, field: str, required: bool = False
) -> list[str]:
    """Parse a user-typed command line into an argv list (shlex semantics)."""
    raw = (text or "").strip()
    if not raw:
        if required:
            raise CustomAgentError("INVALID_PAYLOAD", f"{field} is required")
        return []
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise CustomAgentError(
            "INVALID_PAYLOAD", f"{field}: cannot parse command line ({exc})"
        ) from exc
    if not argv:
        if required:
            raise CustomAgentError("INVALID_PAYLOAD", f"{field} is required")
        return []
    if len(argv) > _MAX_COMMAND_TOKENS:
        raise CustomAgentError(
            "INVALID_PAYLOAD",
            f"{field}: too many arguments (max {_MAX_COMMAND_TOKENS})",
        )
    if any(not tok.strip() for tok in argv):
        raise CustomAgentError(
            "INVALID_PAYLOAD", f"{field}: empty argument not allowed"
        )
    return argv


def validate_ready_pattern(pattern: str) -> str:
    value = (pattern or "").strip()
    if len(value) > _MAX_READY_PATTERN_LEN:
        raise CustomAgentError(
            "INVALID_PAYLOAD",
            f"ready_pattern too long (max {_MAX_READY_PATTERN_LEN} chars)",
        )
    return value


def compile_ready_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile the user pattern; invalid regex degrades to a literal match."""
    value = (pattern or "").strip()
    if not value:
        return None
    try:
        return re.compile(value)
    except re.error:
        return re.compile(re.escape(value))


# NOTE: there is deliberately NO binary-availability probe here. A registered
# custom agent is always offered (list page, FlowEditor owner dropdown, flow
# save, run trigger) — whether the command actually runs is the user's own
# responsibility. A command that can't start fails through the SAME channels as
# any other agent: a spawn failure becomes ``dispatch_failed`` → run paused, and
# a chat turn surfaces the subprocess error. Probing PATH here would only add a
# false gate (wrappers, shell functions, PATH differences between the service
# and the user's shell all make ``which`` a poor oracle).


# ──────────────────────────────────────────────────────────────────────
# Flow references / spec resolution
# ──────────────────────────────────────────────────────────────────────


def _spec_references_agent(spec: object, agent_id: str) -> bool:
    if not isinstance(spec, dict):
        return False
    for a in spec.get("agents") or []:
        if not isinstance(a, dict):
            continue
        ref = a.get("custom_agent_ref") or a.get("customAgentRef")
        if ref == agent_id:
            return True
    return False


def flows_referencing(
    agent_id: str, *, storage: StorageBackend
) -> list[dict[str, str]]:
    """Flows whose spec references this registry row (delete-time warning)."""
    out: list[dict[str, str]] = []
    offset = 0
    while True:
        flows, total = storage.flow_list(limit=200, offset=offset)
        for flow in flows:
            if _spec_references_agent(flow.spec, agent_id):
                out.append({"id": flow.id, "name": flow.name})
        offset += len(flows)
        if not flows or offset >= total:
            break
    return out


def resolve_spec_custom_agents(
    spec: FlowSpec, *, storage: StorageBackend
) -> dict[str, CustomAgent]:
    """Refresh every ``kind=custom`` agent's argv from the registry, in place.

    Registry preferred, save-time snapshot fallback. NEVER raises: complaint /
    resume controllers must stay constructible even after a registry row was
    deleted mid-run (the strict existence gate runs at flow save / run trigger
    via ``validate_flow_against_db``). Returns ``agent_id → registry row`` for
    the agents that resolved, so callers can reach chat/headless/ready fields.
    """
    rows: dict[str, CustomAgent] = {}
    for agent in spec.agents:
        if agent.kind != AgentKind.custom:
            continue
        ref = (agent.custom_agent_ref or "").strip()
        if not ref:
            continue
        try:
            row = storage.custom_agent_get(ref)
        except Exception as exc:  # pragma: no cover — defensive; storage is local sqlite
            logger.warning(
                "custom_agent_resolve_failed",
                agent_id=agent.id,
                custom_agent_ref=ref,
                error=str(exc),
            )
            continue
        if row is None:
            logger.warning(
                "custom_agent_ref_missing_using_snapshot",
                agent_id=agent.id,
                custom_agent_ref=ref,
            )
            continue
        if row.spawn_command:
            agent.command = list(row.spawn_command)
        agent.resume_command = list(row.resume_command) or None
        rows[agent.id] = row
    return rows


# ──────────────────────────────────────────────────────────────────────
# Headless argv rendering
# ──────────────────────────────────────────────────────────────────────


def render_headless_argv(template: list[str], message: str) -> list[str]:
    """Substitute ``{message}`` into *template*; append when absent."""
    out: list[str] = []
    substituted = False
    for tok in template:
        if MESSAGE_PLACEHOLDER in tok:
            out.append(tok.replace(MESSAGE_PLACEHOLDER, message))
            substituted = True
        else:
            out.append(tok)
    if not substituted:
        out.append(message)
    return out


def headless_argv_for_flow_agent(
    agent: FlowAgent, prompt: str, *, storage: StorageBackend
) -> list[str]:
    """One-shot argv for headless uses of a ``kind=custom`` Flow agent.

    Prefer the registry's headless template (purpose-built for one-shot
    invocations); fall back to the legacy convention of appending the prompt
    to the interactive command.
    """
    ref = (agent.custom_agent_ref or "").strip()
    if ref:
        try:
            row = storage.custom_agent_get(ref)
        except Exception:  # pragma: no cover — defensive
            row = None
        if row is not None and row.headless_command:
            return render_headless_argv(list(row.headless_command), prompt)
    return list(agent.command or []) + [prompt]


__all__ = [
    "MESSAGE_PLACEHOLDER",
    "CustomAgentError",
    "compile_ready_pattern",
    "flows_referencing",
    "headless_argv_for_flow_agent",
    "parse_command",
    "render_headless_argv",
    "resolve_spec_custom_agents",
    "slugify_name",
    "validate_description",
    "validate_name",
    "validate_ready_pattern",
]
