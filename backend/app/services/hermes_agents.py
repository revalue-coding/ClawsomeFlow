"""Hermes agent management service.

A *managed Hermes agent* maps 1:1 to a **Hermes profile** (``hermes -p <id>``)
whose state lives under ``~/.hermes/profiles/{id}/`` (SOUL.md, memories/,
skills/, cron/, config.yaml, .env). Hermes owns that home; we drive it purely
through the ``hermes`` CLI (and read/write a few well-known profile files for
settings the CLI does not expose). The canonical id is shared:

    HermesAgent.id == hermes profile name == FlowAgent.id

Mirrors the shape of :mod:`app.services.openclaw_agents` but is far thinner —
Hermes manages its own workspace, so there is no common-source deployment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import Config, load_config
from app.logging_setup import get_logger
from app.models import Flow, HermesAgent
from app.services import subprocess_registry as _subproc_registry
from app.services.chat_retry import (
    CHAT_CONNECTION_RETRY_ATTEMPTS,
    CHAT_CONNECTION_RETRY_DELAYS_SEC,
    is_transient_connection_error,
)
from app.storage import StorageBackend, get_storage

logger = get_logger("services.hermes_agents")

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

HERMES_HOME_ENV = "HERMES_HOME"
_DEFAULT_HERMES_HOME = "~/.hermes"
# Hermes profile id rules: lowercase letters/digits/underscore/hyphen;
# first character must be lowercase letter or digit; max 64 chars.
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_AGENT_ID_MAX_LEN = 64
_RESERVED_PROFILE_NAMES = frozenset({"default"})

_CLI_TIMEOUT_SEC = 60.0
_BOOTSTRAP_TIMEOUT_SEC = 600.0
_CHAT_TIMEOUT_SEC = 1800.0

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SKILL_FRONT_MATTER_RE = re.compile(
    r"^---\s*\n(?P<header>.*?)\n---\s*(?:\n(?P<body>.*))?$", re.DOTALL,
)
_SOUL_FILENAME = "SOUL.md"
_SKILLS_DIRNAME = "skills"
_SKILL_ENTRY_FILENAME = "SKILL.md"
_SECRET_MASK = "••••••••"
_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MCP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "completed_with_conflicts", "complaint_failed", "failed", "aborted"}
)


# ──────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────


class HermesAgentError(Exception):
    """Base error for Hermes agent operations."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class HermesUnavailable(HermesAgentError):
    """The ``hermes`` CLI is not installed/usable."""


class AgentIdInvalid(HermesAgentError):
    """Requested agent id / profile name is invalid."""


class AgentAlreadyExists(HermesAgentError):
    """An agent (or profile) with this id already exists."""


class AgentNotFound(HermesAgentError):
    """No managed agent with this id."""


class AgentInUse(HermesAgentError):
    """Agent is referenced by an existing Flow and cannot be removed."""


class ProfileOpFailed(HermesAgentError):
    """A ``hermes`` CLI operation failed."""


class AgentCreateCancelled(HermesAgentError):
    """Creation was cancelled by the user before it finished."""


# ──────────────────────────────────────────────────────────────────────
# Paths / CLI plumbing
# ──────────────────────────────────────────────────────────────────────


def hermes_home() -> Path:
    raw = os.environ.get(HERMES_HOME_ENV) or _DEFAULT_HERMES_HOME
    return Path(raw).expanduser()


def hermes_profile_root(agent_id: str) -> Path:
    """``~/.hermes/profiles/{id}`` — the single source of truth for the path."""
    return hermes_home() / "profiles" / agent_id


def hermes_executable() -> str | None:
    return shutil.which("hermes")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _run_hermes(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = _CLI_TIMEOUT_SEC,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run ``hermes <args>`` and return (rc, stdout, stderr). Never raises on
    non-zero exit — callers decide. Raises :class:`HermesUnavailable` only when
    the binary is missing."""
    exe = hermes_executable()
    if exe is None:
        raise HermesUnavailable("`hermes` CLI not found on PATH")
    try:
        proc = subprocess.run(  # noqa: S603 — args are constructed, not shell
            [exe, *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            input=stdin,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ProfileOpFailed(
            f"hermes {' '.join(args[:2])} timed out after {timeout}s"
        ) from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _hermes_profile(agent_id: str, args: list[str], **kw: Any) -> tuple[int, str, str]:
    """Run a profile-scoped command: ``hermes -p <id> <args>``."""
    return _run_hermes(["-p", agent_id, *args], **kw)


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────


def _validate_agent_id(agent_id: str) -> str:
    aid = (agent_id or "").strip()
    if not _AGENT_ID_RE.fullmatch(aid):
        raise AgentIdInvalid(
            "Hermes agent id (profile name) must use lowercase letters/digits/"
            "underscore/hyphen, start with a letter or digit "
            "([a-z0-9][a-z0-9_-]*)"
        )
    if len(aid) > _AGENT_ID_MAX_LEN:
        raise AgentIdInvalid(
            f"Hermes agent id length must be <= {_AGENT_ID_MAX_LEN}"
        )
    if aid in _RESERVED_PROFILE_NAMES:
        raise AgentIdInvalid(f"'{aid}' is reserved and cannot be used")
    return aid


def _existing_directory(path: str, *, field_name: str = "path") -> Path:
    """Validate that *path* points to an existing directory."""
    raw = (path or "").strip()
    candidate = Path(raw).expanduser()
    if not candidate.exists():
        raise AgentIdInvalid(f"{field_name} path does not exist: {path}")
    if not candidate.is_dir():
        raise AgentIdInvalid(f"{field_name} path is not a directory: {path}")
    return candidate.resolve(strict=False)


# ──────────────────────────────────────────────────────────────────────
# Runtime availability
# ──────────────────────────────────────────────────────────────────────


PROBE_FAST = "fast"
PROBE_FULL = "full"
# List reconcile modes — ``fast`` scans ``~/.hermes/profiles/`` (microseconds);
# ``full`` runs ``hermes profile list`` (authoritative but slow on Linux when
# ``~/.local/bin`` holds large CLI binaries that Hermes' alias reverse-lookup reads).
RECONCILE_FAST = "fast"
RECONCILE_FULL = "full"
# Generous timeout for the FULL probe: `hermes --version` runs a synchronous
# update-check (git) that can take several seconds on the first (cold-cache)
# call, so the 5s default would false-negative a perfectly usable binary. The
# result is cached by hermes for 6h, so later probes are instant.
_FULL_PROBE_TIMEOUT_SEC = 30.0


def probe_runtime_running(
    *, config: Config | None = None, level: str = PROBE_FULL
) -> tuple[bool, str]:
    """Hermes has no long-lived daemon in our usage; "running" == CLI usable.

    Two levels so the UI can show fast and verify in the background:

    * ``fast`` — presence on PATH only (``shutil.which``); microseconds, no
      subprocess. Lets the WebUI render immediately.
    * ``full`` — actually execute ``hermes --version`` (generous timeout) to
      confirm the binary really runs, catching a genuinely broken install. A
      slow update-check never false-negatives here thanks to the long timeout.
    """
    exe = hermes_executable()
    if exe is None:
        return False, "hermes CLI not available"
    if level == PROBE_FAST:
        return True, exe
    for ver_args in (["--version"], ["version"]):
        try:
            rc, out, _err = _run_hermes(ver_args, timeout=_FULL_PROBE_TIMEOUT_SEC)
        except HermesAgentError:
            continue
        if rc == 0:
            first = next(
                (ln for ln in _strip_ansi(out).splitlines() if ln.strip()), ""
            )
            return True, first or "hermes available"
    # Present on PATH but no version command ran cleanly → broken install.
    return False, "hermes CLI present but failed to run (`hermes --version`)"


# ──────────────────────────────────────────────────────────────────────
# Profile listing (CLI table parsing)
# ──────────────────────────────────────────────────────────────────────


def _parse_profile_list(out: str) -> list[str]:
    names: list[str] = []
    for raw in out.splitlines():
        line = _strip_ansi(raw).strip()
        if not line:
            continue
        if line.startswith("Profile") and "Model" in line:
            continue  # header
        if set(line) <= set("─-—│| "):
            continue  # separator
        first = line.split()[0].lstrip("◆◇*").strip()
        if not first or first in _RESERVED_PROFILE_NAMES:
            continue
        if first not in names:
            names.append(first)
    return names


def list_profile_names_checked() -> tuple[bool, list[str]]:
    """``(query_ok, names)`` from ``hermes profile list``.

    ``query_ok=False`` means hermes could not be asked (CLI missing or the
    command failed) — callers MUST NOT treat the empty list as "no profiles
    exist", or a transient failure would wrongly prune every managed row."""
    try:
        rc, out, _err = _run_hermes(["profile", "list"])
    except HermesAgentError:
        return False, []
    if rc != 0:
        return False, []
    return True, _parse_profile_list(out)


def list_profile_names() -> list[str]:
    """Parse ``hermes profile list`` → profile names (default excluded)."""
    return list_profile_names_checked()[1]


def list_profile_names_from_fs() -> list[str]:
    """Named profile ids under ``~/.hermes/profiles/`` via directory scan.

    Matches the named-profile subset of ``hermes profile list`` without
    invoking the CLI — the hot path for the WebUI's first paint.
    """
    profiles_root = hermes_home() / "profiles"
    if not profiles_root.is_dir():
        return []
    names: list[str] = []
    try:
        entries = sorted(profiles_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name
        if name in _RESERVED_PROFILE_NAMES:
            continue
        if not _AGENT_ID_RE.fullmatch(name):
            continue
        names.append(name)
    return names


def read_profile_description(agent_id: str) -> str:
    rc, out, _err = _run_hermes(["profile", "describe", agent_id])
    if rc != 0:
        return ""
    return _strip_ansi(out).strip()


# ──────────────────────────────────────────────────────────────────────
# Mapping
# ──────────────────────────────────────────────────────────────────────


def to_summary_dict(row: HermesAgent, *, team_name: str = "") -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "team_id": row.team_id,
        "team_name": team_name,
        "profile_root": row.profile_root,
        "created_by_user": row.created_by_user,
        "created_at": row.created_at,
    }


# ──────────────────────────────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommitInput:
    id: str
    name: str
    description: str = ""
    nl_prompt: str = ""
    team_id: str = ""
    skip_bootstrap: bool = False
    # "default" → active/root profile; "" → do not inherit; otherwise the
    # profile id to copy initial model config from.
    model_inherit_from: str = "default"
    # Optional clone of an existing profile at create time (mirrors Hermes'
    # ``hermes profile create --clone[-all] [--clone-from SOURCE]``):
    #   clone_from == ""        → no clone
    #   clone_from == "default" → clone the active/root profile (`--clone`)
    #   clone_from == "<id>"    → clone that profile (`--clone --clone-from <id>`)
    # clone_all switches the light config clone (`--clone`) to a full state clone
    # (`--clone-all`: memories, sessions, skills, state).
    clone_from: str = ""
    clone_all: bool = False


@dataclass
class BootstrapOutcome:
    """Result of the (best-effort) self-definition bootstrap run during create.

    The bootstrap ``hermes -z`` turn is intentionally non-fatal — a failed
    self-definition never fails the create — but silently reporting such an
    agent as fully ready is misleading (empty SOUL.md, no identity). Callers may
    pass an instance to :func:`commit_agent`; it is populated so the create
    response can surface a **non-fatal warning** ("agent created, but its
    self-definition did not complete; configure a model/credential and retry").

    * ``ok`` — False when the bootstrap turn did not run to completion.
    * ``error`` — short diagnostic (e.g. "No inference provider configured").
    * ``ran`` — False when bootstrap was skipped entirely (``skip_bootstrap``).
    """

    ok: bool = True
    error: str = ""
    ran: bool = True


# Inference config files a fresh profile needs to be usable. A brand-new
# `hermes profile create` leaves these absent, so the profile has NO model /
# API keys / login credential — every `hermes -p <id> …` call (bootstrap, chat,
# task dispatch) then dies with "No inference provider configured" and SOUL.md
# is never written. We seed them from a source profile (default: the user's
# active/root profile) so managed agents inherit a usable model + keys.
# ``auth.json`` is REQUIRED here: when the operator authenticates via
# ``hermes model`` / OAuth, the live credential lives in the profile-local
# ``auth.json`` (NOT in ``.env`` as an API key), and Hermes reads ONLY the
# profile's own ``auth.json`` (no inheritance from the root) — so omitting it
# leaves a keyless profile whose very first ``hermes -z`` self-definition turn
# fails. We copy config.yaml + .env + auth.json only — never SOUL.md or
# memories, to avoid leaking private identity/memory.
_INFERENCE_CONFIG_FILES = ("config.yaml", ".env", "auth.json")
# Credential files copied above that must stay private (chmod 0600 on copy).
_INFERENCE_SECRET_FILES = frozenset({".env", "auth.json"})


def _active_profile_root() -> Path:
    """The profile `hermes` uses when invoked without ``-p`` — the source we
    copy inference config from. Honours ``~/.hermes/active_profile`` (set by
    ``hermes profile use``), falling back to the root profile ``~/.hermes``."""
    root = hermes_home()
    try:
        active = (root / "active_profile").read_text().strip()
    except OSError:
        active = ""
    if active and active != "default":
        candidate = root / "profiles" / active
        if candidate.is_dir():
            return candidate
    return root


def _resolve_inference_seed_source(*, source_profile: str | None, dest_agent_id: str) -> Path:
    source_id = (source_profile or "").strip()
    if not source_id or source_id == "default":
        return _active_profile_root()
    source_id = _validate_agent_id(source_id)
    if source_id == dest_agent_id:
        return hermes_profile_root(dest_agent_id)
    source = hermes_profile_root(source_id)
    if source.is_dir():
        return source
    raise AgentIdInvalid(f"model inherit source profile {source_id!r} not found")


def _seed_profile_inference_config(
    agent_id: str, *, source_profile: str | None = None
) -> None:
    """Copy model + API-key config into a freshly created profile (idempotent;
    never clobbers existing files). Best-effort: failures are logged, not
    raised — the caller surfaces a clearer error if bootstrap then fails."""
    source = _resolve_inference_seed_source(
        source_profile=source_profile, dest_agent_id=agent_id
    )
    dest = hermes_profile_root(agent_id)
    if source.resolve() == dest.resolve():
        return
    for name in _INFERENCE_CONFIG_FILES:
        src = source / name
        dst = dest / name
        if not src.is_file() or dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            if name in _INFERENCE_SECRET_FILES:  # keys/credential — keep private
                dst.chmod(0o600)
        except OSError as exc:
            logger.warning(
                "hermes_seed_config_failed", agent_id=agent_id, file=name, error=str(exc)
            )


def backfill_hermes_inference_config(
    *, storage: StorageBackend | None = None, config: Config | None = None
) -> int:
    """Ensure every managed Hermes profile has inference config (config.yaml/.env).

    Hermes profiles read ONLY their own config — there is no global/root config
    they can reference live — so a profile created before the seed existed (or
    before the operator configured a provider) is stranded with "No inference
    provider configured". This backfills them from the operator's active/root
    profile. Idempotent and ABSENT-ONLY (``_seed_profile_inference_config`` never
    overwrites an existing file), so it never clobbers a user's per-agent edits.
    Returns the number of profiles that gained config. Best-effort.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    seeded = 0
    for row in storage.hermes_list():
        dest = hermes_profile_root(row.id) / "config.yaml"
        had = dest.is_file()
        try:
            _seed_profile_inference_config(row.id)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("hermes_backfill_failed", agent_id=row.id, error=str(exc))
            continue
        if not had and dest.is_file():
            seeded += 1
    return seeded


# ──────────────────────────────────────────────────────────────────────
# Create cancellation (the bootstrap `hermes -z` can run up to 10 min; the
# UI must be able to abort it). We track the live bootstrap subprocess per
# agent id so a concurrent cancel request can kill it, and a flag so the
# create thread rolls back instead of persisting a half-built agent.
# ──────────────────────────────────────────────────────────────────────

_CREATE_LOCK = threading.Lock()
_BOOTSTRAP_PROCS: dict[str, subprocess.Popen] = {}
_CANCELLED_CREATES: set[str] = set()
# Ids whose create is currently between `hermes profile create` (profile lands
# on disk) and the final `storage.hermes_create` (row committed). The list /
# reconcile path must NOT adopt these on-disk-but-not-yet-rowed profiles, or the
# adopt inserts a row the create then collides with → false "already exists".
# Guarded by _CREATE_LOCK.
_CREATES_IN_FLIGHT: set[str] = set()
# One lock per agent id: serializes creates of the SAME id so a duplicate /
# concurrent POST can't race past the existence check (TOCTOU) and corrupt the
# winner. Distinct ids use distinct locks, so unrelated agents still create in
# parallel. Guarded by _CREATE_LOCK for get-or-create.
_CREATE_ID_LOCKS: dict[str, threading.Lock] = {}


def _create_id_lock(aid: str) -> threading.Lock:
    with _CREATE_LOCK:
        lock = _CREATE_ID_LOCKS.get(aid)
        if lock is None:
            lock = threading.Lock()
            _CREATE_ID_LOCKS[aid] = lock
        return lock


def _is_create_cancelled(aid: str) -> bool:
    with _CREATE_LOCK:
        return aid in _CANCELLED_CREATES


def is_create_in_flight(aid: str) -> bool:
    """True while a create for *aid* is between profile-create and row-commit.

    Used by the operation-status recovery layer (``GET /api/operations``) to
    report ``running`` when the registry entry was missed/evicted. Hermes holds
    this across the WHOLE create (incl. bootstrap), so it is accurate here.
    """
    with _CREATE_LOCK:
        return aid in _CREATES_IN_FLIGHT


def _run_bootstrap(
    aid: str,
    args: list[str],
    *,
    cwd: str,
    timeout: float,
    outcome: BootstrapOutcome | None = None,
) -> int:
    """Run the bootstrap ``hermes`` call as a killable subprocess registered
    under *aid* so :func:`cancel_create_agent` can terminate it mid-flight.
    Returns the exit code (non-zero if killed/cancelled). When *outcome* is
    given, its ``ok``/``error`` are set on a non-zero exit so the caller can
    surface a non-fatal "self-definition incomplete" warning."""
    exe = hermes_executable()
    if exe is None:
        raise HermesUnavailable("`hermes` CLI not found on PATH")
    proc = subprocess.Popen(  # noqa: S603 — args are constructed, not shell
        [exe, *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
        # Own process group so a cancel / shutdown can killpg the whole tree
        # (the bootstrap CLI may spawn children that would otherwise linger).
        start_new_session=True,
    )
    with _CREATE_LOCK:
        _BOOTSTRAP_PROCS[aid] = proc
    _subproc_registry.register(proc)
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _subproc_registry.kill_group(proc)
        proc.communicate()
        raise ProfileOpFailed(
            f"hermes bootstrap for {aid!r} timed out after {timeout}s"
        ) from None
    finally:
        _subproc_registry.unregister(proc)
        with _CREATE_LOCK:
            if _BOOTSTRAP_PROCS.get(aid) is proc:
                _BOOTSTRAP_PROCS.pop(aid, None)
    if proc.returncode != 0:
        detail = _strip_ansi(err).strip()[:500]
        if detail:
            logger.warning("hermes_bootstrap_failed", agent_id=aid, error=detail)
        if outcome is not None:
            outcome.ok = False
            outcome.error = detail or f"bootstrap exited with code {proc.returncode}"
    return proc.returncode


def _rollback_create(aid: str, *, storage: StorageBackend | None = None) -> None:
    """Best-effort removal of everything a (cancelled/failed) create produced:
    the DB row (if persisted) and the Hermes profile."""
    if storage is not None:
        try:
            if storage.hermes_get(aid) is not None:
                storage.hermes_delete(aid)
        except Exception as exc:  # noqa: BLE001 — cleanup must not raise
            logger.warning("hermes_rollback_row_failed", agent_id=aid, error=str(exc))
    try:
        _run_hermes(["profile", "delete", aid, "-y"])
    except HermesAgentError as exc:
        logger.warning("hermes_rollback_profile_failed", agent_id=aid, error=str(exc))


def cancel_create_agent(
    agent_id: str,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> bool:
    """Request cancellation of an in-flight create and roll back its artifacts.

    Idempotent and safe to call whether the create is mid-bootstrap, already
    finished, or never started. Returns True if a live bootstrap was killed."""
    aid = _validate_agent_id(agent_id)
    storage = storage or get_storage(config or load_config())
    with _CREATE_LOCK:
        _CANCELLED_CREATES.add(aid)
        proc = _BOOTSTRAP_PROCS.get(aid)
    killed = False
    if proc is not None:
        # Kill the whole group so any children the bootstrap spawned die too.
        _subproc_registry.kill_group(proc)
        killed = True
    _rollback_create(aid, storage=storage)
    return killed


def _bootstrap_prompt(name: str, responsibility: str) -> str:
    return (
        f"You are a newly created Hermes agent named '{name}'. "
        f"Your responsibility: {responsibility or name}.\n\n"
        "Establish your identity now: write a clear, professional SOUL.md that "
        "captures your role, scope of expertise, working style, and the kinds of "
        "tasks you are best at. Use your memory and skills tools as appropriate to "
        "set yourself up. Keep SOUL.md concise, well-structured, and accurate."
    )


def commit_agent(
    cmd: CommitInput,
    *,
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
    outcome: BootstrapOutcome | None = None,
) -> HermesAgent:
    """Create a Hermes profile + bootstrap self-definition + persist the row.

    Creates of the same id are serialized by a per-id lock: a duplicate /
    concurrent request (e.g. a double-submit) would otherwise both pass the
    existence check below and race to ``hermes_create``; the loser's
    IntegrityError rollback would then delete the WINNER's freshly-built
    profile. We acquire the lock non-blocking so the loser fails fast with
    AgentAlreadyExists instead of tying up a worker for the whole bootstrap.

    Pass *outcome* to learn whether the (non-fatal) self-definition bootstrap
    actually completed — it is populated in place so the caller can surface a
    warning without the create itself failing.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    aid = _validate_agent_id(cmd.id)

    lock = _create_id_lock(aid)
    if not lock.acquire(blocking=False):
        raise AgentAlreadyExists(f"a create for {aid!r} is already in progress")
    try:
        return _commit_agent_locked(
            cmd, aid, user=user, storage=storage, outcome=outcome
        )
    finally:
        lock.release()


def _commit_agent_locked(
    cmd: CommitInput,
    aid: str,
    *,
    user: str,
    storage: StorageBackend,
    outcome: BootstrapOutcome | None = None,
) -> HermesAgent:
    if storage.hermes_get(aid) is not None:
        raise AgentAlreadyExists(f"hermes agent {aid!r} already exists")
    if aid in list_profile_names():
        raise AgentAlreadyExists(
            f"a Hermes profile named {aid!r} already exists; claim it instead",
        )

    # Fresh attempt: clear any stale cancel flag from a previous create of the
    # same id (the user may retry an id whose earlier create they cancelled), and
    # publish this id as in-flight. The in-flight marker stops the list/reconcile
    # path (list_agents → _reconcile_managed_profiles) from *adopting* the
    # profile we are about to write to disk before our own row is committed: a
    # poll landing between `hermes profile create` below and the final
    # `storage.hermes_create` would otherwise insert a (nameless) DB row, and our
    # own create would then collide with it and raise a false "already exists".
    with _CREATE_LOCK:
        _CANCELLED_CREATES.discard(aid)
        _CREATES_IN_FLIGHT.add(aid)
    try:

        def _abort_if_cancelled() -> None:
            if _is_create_cancelled(aid):
                _rollback_create(aid, storage=storage)
                with _CREATE_LOCK:
                    _CANCELLED_CREATES.discard(aid)
                raise AgentCreateCancelled(f"creation of {aid!r} cancelled by user")

        description = (cmd.description or "").strip()
        clone_from = (cmd.clone_from or "").strip()
        model_inherit = (cmd.model_inherit_from or "").strip()

        # Step 1 — create the profile, optionally cloning an existing one. The
        # clone happens AT creation (Hermes copies config/.env[/full state] from
        # the source), so a subsequent model inheritance can override on top.
        create_args = ["profile", "create", aid]
        if clone_from:
            create_args.append("--clone-all" if cmd.clone_all else "--clone")
            if clone_from != "default":
                # "default" clones the active profile (no --clone-from); any other
                # value must be a valid profile id passed as the clone source.
                create_args += ["--clone-from", _validate_agent_id(clone_from)]
        if description:
            create_args += ["--description", description]
        rc, out, err = _run_hermes(create_args)
        if rc != 0:
            raise ProfileOpFailed(
                f"`hermes profile create {aid}` failed: "
                f"{(_strip_ansi(err) or _strip_ansi(out)).strip()}"
            )

        profile_root = str(hermes_profile_root(aid))

        # Step 2 — inference config. The new profile must be runnable (bootstrap +
        # later chat/task dispatch all go through `hermes -p <id>`). Order:
        #   * clone + model inherit → clone already populated config; run a model
        #     inheritance ON TOP (override just the model + import its keys),
        #     preserving the cloned skills/mcp/etc.
        #   * model inherit only (fresh profile) → copy config.yaml + .env wholesale.
        #   * neither → seed from the active/default profile so it still runs.
        if model_inherit:
            if clone_from:
                try:
                    import_model_from_profile(aid, source_profile=model_inherit)
                except HermesAgentError as exc:
                    logger.warning(
                        "hermes_model_inherit_after_clone_failed",
                        agent_id=aid,
                        error=str(exc),
                    )
            else:
                _seed_profile_inference_config(aid, source_profile=model_inherit)
        elif not clone_from:
            _seed_profile_inference_config(aid)

        _abort_if_cancelled()  # cancelled during profile create → roll back now

        # Bootstrap self-definition (best-effort: keep the agent even if it fails,
        # but honour a cancel that lands while the up-to-10-min `hermes -z` runs).
        # A failure here does NOT fail the create; we record it in *outcome* so
        # the caller can surface a non-fatal "self-definition incomplete" warning
        # instead of silently reporting a half-built agent as fully ready.
        if cmd.skip_bootstrap:
            if outcome is not None:
                outcome.ran = False
        else:
            try:
                rc = _run_bootstrap(
                    aid,
                    ["-p", aid, "--yolo", "-z", _bootstrap_prompt(cmd.name, description)],
                    cwd=profile_root,
                    timeout=_BOOTSTRAP_TIMEOUT_SEC,
                    outcome=outcome,
                )
                if rc != 0 and outcome is not None and outcome.ok:
                    # Non-zero exit that produced no stderr detail for _run_bootstrap.
                    outcome.ok = False
                    outcome.error = outcome.error or (
                        f"bootstrap exited with code {rc}"
                    )
            except HermesAgentError as exc:
                logger.warning("hermes_bootstrap_error", agent_id=aid, error=str(exc))
                if outcome is not None:
                    outcome.ok = False
                    outcome.error = str(exc)

        _abort_if_cancelled()  # cancelled during bootstrap → roll back, don't persist

        row = HermesAgent(
            id=aid,
            name=cmd.name.strip() or aid,
            description=description,
            team_id=cmd.team_id or "",
            profile_root=profile_root,
            created_by_user=user,
            nl_prompt=cmd.nl_prompt or description,
        )
        try:
            return storage.hermes_create(row)
        except Exception:
            # If a row already exists for this id (we lost a create race / hit a
            # duplicate request — e.g. a UNIQUE-constraint IntegrityError), the
            # profile belongs to the WINNER: never delete it, and surface a clean
            # AgentAlreadyExists instead of a raw 500. Only roll back a profile we
            # genuinely orphaned (no row exists).
            existing = None
            try:
                existing = storage.hermes_get(aid)
            except Exception:  # noqa: BLE001 — best-effort probe
                existing = None
            if existing is not None:
                raise AgentAlreadyExists(f"hermes agent {aid!r} already exists")
            try:
                _run_hermes(["profile", "delete", aid, "-y"])
            except HermesAgentError:
                pass
            raise
    finally:
        with _CREATE_LOCK:
            _CREATES_IN_FLIGHT.discard(aid)


def claim_profile(
    *,
    profile_name: str,
    name: str = "",
    description: str = "",
    team_id: str = "",
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
    known_profiles: set[str] | None = None,
    probe_description: bool = True,
) -> HermesAgent:
    """Register an EXISTING Hermes profile into management (DB row only).

    ``known_profiles`` / ``probe_description`` exist for the bulk reconcile path
    (:func:`_reconcile_managed_profiles`): pass the already-fetched profile-name
    set to skip a redundant ``hermes profile list`` subprocess, and set
    ``probe_description=False`` to skip the per-profile ``hermes profile
    describe`` subprocess. Both default to the safe standalone behaviour.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    aid = _validate_agent_id(profile_name)
    if storage.hermes_get(aid) is not None:
        raise AgentAlreadyExists(f"hermes agent {aid!r} is already managed")
    available = known_profiles if known_profiles is not None else set(list_profile_names())
    if aid not in available:
        raise AgentNotFound(f"no Hermes profile named {aid!r} to claim")
    desc = description.strip()
    if not desc and probe_description:
        desc = read_profile_description(aid)
    row = HermesAgent(
        id=aid,
        name=(name.strip() or aid),
        description=desc,
        team_id=team_id or "",
        profile_root=str(hermes_profile_root(aid)),
        created_by_user=user,
        nl_prompt="",
    )
    return storage.hermes_create(row)


def list_claimable_profiles(
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> list[dict[str, str]]:
    """Hermes profiles that exist on disk but are not yet managed."""
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    managed = {r.id for r in storage.hermes_list()}
    out: list[dict[str, str]] = []
    for name in list_profile_names():
        if name in managed:
            continue
        out.append({"id": name, "description": read_profile_description(name)})
    return out


def get_agent(
    agent_id: str,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> HermesAgent:
    storage = storage or get_storage(config or load_config())
    aid = _validate_agent_id(agent_id)
    row = storage.hermes_get(aid)
    if row is None:
        raise AgentNotFound(f"hermes agent {aid!r} not found")
    return row


def _reconcile_managed_profiles_with(
    *,
    user: str,
    storage: StorageBackend,
    on_disk: list[str],
) -> None:
    """Apply adopt/prune given a concrete on-disk profile-name list."""
    on_disk_set = set(on_disk)
    # Never adopt a profile whose create is still in flight: its row will be
    # committed by the create itself, and adopting here would make that create
    # collide with our row → false "already exists" (the create races bug).
    with _CREATE_LOCK:
        in_flight = set(_CREATES_IN_FLIGHT)
    managed = {r.id: r for r in storage.hermes_list(owner_user=user)}
    for name in on_disk:  # adopt newcomers
        if name not in managed and name not in in_flight:
            try:
                # Reuse the names we already fetched and skip the per-profile
                # `hermes profile describe` so adoption adds no extra subprocess
                # on this hot list path (was O(N) `hermes` invocations).
                claim_profile(
                    profile_name=name,
                    user=user,
                    storage=storage,
                    known_profiles=on_disk_set,
                    probe_description=False,
                )
            except HermesAgentError:
                continue
    for aid in managed:  # prune ghosts whose profile no longer exists
        if aid not in on_disk_set:
            try:
                storage.hermes_delete(aid)
                logger.info("hermes_agent_pruned_orphan", agent_id=aid)
            except Exception as exc:  # noqa: BLE001 — cleanup must not abort list
                logger.warning("hermes_prune_failed", agent_id=aid, error=str(exc))


def _reconcile_managed_profiles(
    *,
    user: str,
    storage: StorageBackend,
) -> None:
    """Make the managed-agent DB rows mirror the Hermes profiles that actually
    exist — Hermes is the source of truth:

    * **adopt** any on-disk profile not yet in the DB (no manual "claim"); and
    * **prune** any managed row whose profile is gone (deleted via the CLI, by
      the agent itself, or externally) so the platform never shows a ghost.

    The DB row only carries ClawsomeFlow-specific metadata (team, owner, display
    name, nl_prompt) layered on top of the profile — it is not a second source
    of truth. Best-effort and idempotent; individual failures never abort the
    listing. Crucially, reconciliation is **skipped entirely** when Hermes can't
    be queried, so a transient CLI failure never prunes valid rows.
    """
    query_ok, on_disk = list_profile_names_checked()
    if not query_ok:
        return  # hermes unavailable — trust the DB as-is, do not mutate
    _reconcile_managed_profiles_with(user=user, storage=storage, on_disk=on_disk)


def _reconcile_managed_profiles_fast(
    *,
    user: str,
    storage: StorageBackend,
) -> None:
    """Filesystem-only reconcile for the WebUI fast list path."""
    _reconcile_managed_profiles_with(
        user=user,
        storage=storage,
        on_disk=list_profile_names_from_fs(),
    )


def list_agents(
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
    adopt: bool = True,
    reconcile: str = RECONCILE_FULL,
) -> list[HermesAgent]:
    storage = storage or get_storage(config or load_config())
    # Reconcile against live Hermes profiles so the list always reflects what
    # actually exists. Only when we have a concrete owner user.
    if adopt and user:
        if reconcile == RECONCILE_FAST:
            _reconcile_managed_profiles_fast(user=user, storage=storage)
        elif reconcile == RECONCILE_FULL:
            _reconcile_managed_profiles(user=user, storage=storage)
    return storage.hermes_list(owner_user=user)


@dataclass
class UpdateInput:
    name: str | None = None
    description: str | None = None
    team_id: str | None = None


def update_agent(
    agent_id: str,
    patch: UpdateInput,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> HermesAgent:
    storage = storage or get_storage(config or load_config())
    row = get_agent(agent_id, storage=storage)
    if patch.name is not None:
        row.name = patch.name.strip() or row.name
    if patch.description is not None:
        row.description = patch.description.strip()
        # keep the kanban-routing description in sync (best-effort)
        try:
            _run_hermes(["profile", "describe", row.id, "--text", row.description])
        except HermesAgentError:
            pass
    if patch.team_id is not None:
        row.team_id = patch.team_id
    return storage.hermes_update(row)


def delete_agent(
    agent_id: str,
    *,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> None:
    """Permanently remove a managed Hermes agent (profile + DB row).

    Permanent by design — Hermes' ``profile delete`` stops the gateway, removes
    the alias/service and deletes all profile data.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    aid = _validate_agent_id(agent_id)
    row = storage.hermes_get(aid)
    if row is None:
        raise AgentNotFound(f"hermes agent {aid!r} not found")

    blocked = _collect_blocking_flow_names(storage=storage, user=row.created_by_user, agent_id=aid)
    if blocked["flow_names"]:
        raise AgentInUse(
            f"agent {aid!r} is used by existing Flows and cannot be removed",
            details=blocked,
        )

    rc, out, err = _run_hermes(["profile", "delete", aid, "-y"])
    if rc != 0:
        msg = (_strip_ansi(err) or _strip_ansi(out)).strip()
        # If the profile is already gone, proceed to drop the row.
        if "not found" not in msg.lower() and "does not exist" not in msg.lower():
            raise ProfileOpFailed(f"`hermes profile delete {aid}` failed: {msg}")

    storage.hermes_delete(aid)
    logger.info("hermes_agent_deleted", agent_id=aid)


# ──────────────────────────────────────────────────────────────────────
# Flow-usage guard
# ──────────────────────────────────────────────────────────────────────


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


def _flow_contains_hermes_agent(flow: Flow, agent_id: str) -> bool:
    # Flow.spec is the raw JSON dict (not a parsed FlowSpec).
    raw_agents = flow.spec.get("agents", []) if isinstance(flow.spec, dict) else []
    if not isinstance(raw_agents, list):
        return False
    for raw in raw_agents:
        if isinstance(raw, dict) and raw.get("kind") == "hermes" and raw.get("id") == agent_id:
            return True
    return False


def _collect_blocking_flow_names(
    *, storage: StorageBackend, user: str, agent_id: str,
) -> dict[str, list[str]]:
    names: set[str] = set()
    for flow in _iter_user_flows(storage=storage, user=user):
        if _flow_contains_hermes_agent(flow, agent_id):
            names.add(flow.name or flow.id)
    return {"flow_names": sorted(names)}


def is_managed(agent_id: str, *, storage: StorageBackend) -> bool:
    """True if ``agent_id`` is a managed Hermes agent (for Flow validation)."""
    try:
        aid = _validate_agent_id(agent_id)
    except AgentIdInvalid:
        return False
    return storage.hermes_get(aid) is not None


# ──────────────────────────────────────────────────────────────────────
# Gateway
# ──────────────────────────────────────────────────────────────────────

# ``gateway install`` prompts twice on Linux (start now + start on login/boot).
_GATEWAY_INSTALL_STDIN = "y\ny\n"


def start_gateway(agent_id: str) -> str:
    """Install/start Hermes gateway for one managed profile.

    Runs the same operator-facing commands as documented in the UI:

    * ``hermes -p <id> gateway install`` (non-interactive: auto-accept prompts)
    * ``hermes -p <id> gateway start``
    """
    aid = _validate_agent_id(agent_id)

    install_args = ["gateway", "install"]
    rc, out, err = _hermes_profile(aid, install_args, stdin=_GATEWAY_INSTALL_STDIN)
    if rc != 0:
        msg = (_strip_ansi(err) or _strip_ansi(out)).strip()
        raise ProfileOpFailed(
            f"`hermes -p {aid} gateway install` failed: {msg}",
            details={"command": f"hermes -p {aid} gateway install"},
        )

    start_args = ["gateway", "start"]
    rc, out, err = _hermes_profile(aid, start_args)
    if rc != 0:
        msg = (_strip_ansi(err) or _strip_ansi(out)).strip()
        raise ProfileOpFailed(
            f"`hermes -p {aid} gateway start` failed: {msg}",
            details={"command": f"hermes -p {aid} gateway start"},
        )

    message = (_strip_ansi(out) or _strip_ansi(err)).strip()
    return message or "gateway started"


def restart_gateway(agent_id: str) -> None:
    """Best-effort ``hermes -p <id> gateway restart`` (errors are ignored)."""
    aid = _validate_agent_id(agent_id)
    _hermes_profile(aid, ["gateway", "restart"])


# ──────────────────────────────────────────────────────────────────────
# Settings — SOUL.md (identity)
# ──────────────────────────────────────────────────────────────────────


def _soul_path(agent_id: str) -> Path:
    return hermes_profile_root(agent_id) / _SOUL_FILENAME


def read_soul(agent_id: str) -> str:
    p = _soul_path(_validate_agent_id(agent_id))
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def write_soul(agent_id: str, content: str) -> str:
    aid = _validate_agent_id(agent_id)
    root = hermes_profile_root(aid)
    if not root.exists():
        raise AgentNotFound(f"profile root for {aid!r} not found")
    p = root / _SOUL_FILENAME
    p.write_text(content, encoding="utf-8")
    return content


# ──────────────────────────────────────────────────────────────────────
# Settings — model & secrets
# ──────────────────────────────────────────────────────────────────────


def _config_path(agent_id: str) -> Path:
    rc, out, _err = _hermes_profile(agent_id, ["config", "path"])
    if rc == 0 and out.strip():
        return Path(_strip_ansi(out).strip())
    return hermes_profile_root(agent_id) / "config.yaml"


def _env_path(agent_id: str) -> Path:
    rc, out, _err = _hermes_profile(agent_id, ["config", "env-path"])
    if rc == 0 and out.strip():
        return Path(_strip_ansi(out).strip())
    return hermes_profile_root(agent_id) / ".env"


def read_model(agent_id: str) -> dict[str, str]:
    aid = _validate_agent_id(agent_id)
    cfg_path = _config_path(aid)
    model: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            model = data.get("model") or {}
        except yaml.YAMLError:
            model = {}
    return {
        "default": str(model.get("default") or ""),
        "provider": str(model.get("provider") or ""),
        "base_url": str(model.get("base_url") or ""),
    }


def read_gateway_cwd(agent_id: str) -> dict[str, str]:
    """Read ``terminal.cwd`` from the profile's config.yaml."""
    aid = _validate_agent_id(agent_id)
    cfg_path = _config_path(aid)
    cwd = ""
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            terminal = data.get("terminal") or {}
            if isinstance(terminal, dict):
                cwd = str(terminal.get("cwd") or "")
        except yaml.YAMLError:
            cwd = ""
    return {"cwd": cwd}


def write_gateway_cwd(agent_id: str, *, cwd: str) -> dict[str, str]:
    """Set ``terminal.cwd`` via the Hermes CLI, then restart the gateway."""
    aid = _validate_agent_id(agent_id)
    abs_path = str(_existing_directory(cwd, field_name="cwd"))
    rc, out, err = _hermes_profile(aid, ["config", "set", "terminal.cwd", abs_path])
    if rc != 0:
        raise ProfileOpFailed(
            f"`hermes config set terminal.cwd` failed: "
            f"{(_strip_ansi(err) or _strip_ansi(out)).strip()}"
        )
    restart_gateway(aid)
    return read_gateway_cwd(aid)


def write_model(
    agent_id: str,
    *,
    default: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
) -> dict[str, str]:
    aid = _validate_agent_id(agent_id)
    pairs = [
        ("model.default", default),
        ("model.provider", provider),
        ("model.base_url", base_url),
    ]
    for key, value in pairs:
        if value is None:
            continue
        rc, out, err = _hermes_profile(aid, ["config", "set", key, value])
        if rc != 0:
            raise ProfileOpFailed(
                f"`hermes config set {key}` failed: "
                f"{(_strip_ansi(err) or _strip_ansi(out)).strip()}"
            )
    return read_model(aid)


def import_model_from_profile(
    agent_id: str, *, source_profile: str = "default"
) -> dict[str, str]:
    """Import model config from default/another profile into an existing agent.

    Mirrors create-time inheritance intent while preserving unrelated settings in
    the destination profile's config.yaml (for example mcp_servers).
    """
    aid = _validate_agent_id(agent_id)
    source_root = _resolve_inference_seed_source(
        source_profile=source_profile, dest_agent_id=aid
    )
    source_cfg_path = source_root / "config.yaml"
    source_model: dict[str, Any] = {}
    if source_cfg_path.exists():
        try:
            raw = yaml.safe_load(source_cfg_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict) and isinstance(raw.get("model"), dict):
                source_model = dict(raw["model"])
        except yaml.YAMLError:
            source_model = {}

    dest_cfg = _read_profile_config_dict(aid)
    if source_model:
        dest_cfg["model"] = source_model
    else:
        dest_cfg.pop("model", None)
    _write_profile_config_dict(aid, dest_cfg)

    # Import source env keys so model/provider switches remain usable without
    # requiring manual re-entry of secrets in the settings UI.
    for key, value in _read_env_pairs(source_root / ".env").items():
        set_secret(aid, key, value)
    return read_model(aid)


def _read_profile_config_dict(agent_id: str) -> dict[str, Any]:
    aid = _validate_agent_id(agent_id)
    cfg_path = _config_path(aid)
    if not cfg_path.exists():
        return {}
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_profile_config_dict(agent_id: str, cfg: dict[str, Any]) -> None:
    aid = _validate_agent_id(agent_id)
    cfg_path = _config_path(aid)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False) or "{}\n",
        encoding="utf-8",
    )


def _validate_mcp_server_name(name: str) -> str:
    out = (name or "").strip()
    if not _MCP_SERVER_NAME_RE.fullmatch(out):
        raise AgentIdInvalid(
            "MCP server name must start with a letter/digit and contain only "
            "letters, digits, '.', '_' or '-'"
        )
    return out


def _parse_mcp_env_lines(environment: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in (environment or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            raise AgentIdInvalid(f"invalid MCP environment line: {line!r} (expected KEY=VALUE)")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _MCP_ENV_NAME_RE.fullmatch(key):
            raise AgentIdInvalid(f"invalid MCP environment key: {key!r}")
        env[key] = value.strip()
    return env


def list_mcp_servers(agent_id: str) -> list[dict[str, Any]]:
    aid = _validate_agent_id(agent_id)
    cfg = _read_profile_config_dict(aid)
    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(servers):
        entry = servers.get(name)
        if not isinstance(entry, dict):
            continue
        endpoint = str(entry.get("url") or "").strip()
        command = str(entry.get("command") or "").strip()
        raw_args = entry.get("args")
        args = [str(v) for v in raw_args] if isinstance(raw_args, list) else []
        if not endpoint and not command:
            continue
        raw_env = entry.get("env")
        env_keys = sorted(str(k) for k in raw_env.keys()) if isinstance(raw_env, dict) else []
        raw_transport = str(entry.get("transport") or "").strip().lower()
        if command and not endpoint:
            transport = "local"
        else:
            transport = "sse" if raw_transport == "sse" else "http_sse"
        out.append(
            {
                "name": str(name),
                "transport": transport,
                "url": endpoint,
                "command": command,
                "args": args,
                "enabled": bool(entry.get("enabled", True) is not False),
                "env_keys": env_keys,
            }
        )
    return out


def upsert_mcp_server(
    agent_id: str,
    *,
    name: str,
    transport: str,
    url: str,
    command: str = "",
    args: list[str] | None = None,
    environment: str | None = "",
) -> dict[str, Any]:
    """Create or update an MCP server entry.

    ``environment`` semantics:

    - a string (including ``""``) → **replace** the env block with the parsed
      pairs (empty string clears it). This is the create path.
    - ``None`` → **preserve** whatever env the existing entry already has. The
      edit form passes ``None`` when the user leaves the env field blank, so an
      edit that only changes the URL never wipes existing (masked) secrets.
    """
    aid = _validate_agent_id(agent_id)
    server_name = _validate_mcp_server_name(name)
    mode = (transport or "http_sse").strip().lower()
    if mode == "stdio":
        mode = "local"
    if mode not in {"http_sse", "streamable_http", "sse", "local"}:
        raise AgentIdInvalid(
            "unsupported MCP transport; expected 'http_sse', 'sse' or 'local'"
        )
    endpoint = (url or "").strip()
    local_command = (command or "").strip()
    local_args = [str(v).strip() for v in (args or []) if str(v).strip()]
    if mode == "local" and not local_command:
        raise AgentIdInvalid("MCP local server command is required")
    if mode != "local" and not endpoint:
        raise AgentIdInvalid("MCP server URL is required")

    cfg = _read_profile_config_dict(aid)
    mcp_servers = cfg.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    entry = mcp_servers.get(server_name)
    if not isinstance(entry, dict):
        entry = {}
    if mode == "local":
        entry["command"] = local_command
        if local_args:
            entry["args"] = local_args
        else:
            entry.pop("args", None)
        entry.pop("url", None)
        entry.pop("transport", None)
    else:
        entry["url"] = endpoint
        # Enforce URL-based flow (official add modal's HTTP/SSE path).
        entry.pop("command", None)
        entry.pop("args", None)
        if mode == "sse":
            entry["transport"] = "sse"
        else:
            entry.pop("transport", None)
    if environment is not None:
        env = _parse_mcp_env_lines(environment)
        if env:
            entry["env"] = env
        else:
            entry.pop("env", None)
    # else: leave entry["env"] untouched (preserve on edit).
    if "enabled" not in entry:
        entry["enabled"] = True
    mcp_servers[server_name] = entry
    cfg["mcp_servers"] = mcp_servers
    _write_profile_config_dict(aid, cfg)
    for item in list_mcp_servers(aid):
        if item["name"] == server_name:
            return item
    raise ProfileOpFailed(f"failed to persist MCP server {server_name!r}")


def delete_mcp_server(agent_id: str, name: str) -> None:
    aid = _validate_agent_id(agent_id)
    server_name = _validate_mcp_server_name(name)
    cfg = _read_profile_config_dict(aid)
    mcp_servers = cfg.get("mcp_servers")
    if not isinstance(mcp_servers, dict) or server_name not in mcp_servers:
        raise AgentNotFound(f"mcp server {server_name!r} not found")
    del mcp_servers[server_name]
    if mcp_servers:
        cfg["mcp_servers"] = mcp_servers
    else:
        cfg.pop("mcp_servers", None)
    _write_profile_config_dict(aid, cfg)


_DOTENV_LINE_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<val>.*)$")


def _read_env_pairs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _DOTENV_LINE_RE.match(raw)
        if not m:
            continue
        out[m.group("key")] = m.group("val").strip().strip('"').strip("'")
    return out


def list_secrets(agent_id: str) -> list[dict[str, Any]]:
    """Return env keys present in the profile .env (values masked)."""
    aid = _validate_agent_id(agent_id)
    p = _env_path(aid)
    out: list[dict[str, Any]] = []
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _DOTENV_LINE_RE.match(raw)
        if not m:
            continue
        val = m.group("val").strip().strip('"').strip("'")
        out.append({"key": m.group("key"), "preview": _SECRET_MASK if val else "", "is_set": bool(val)})
    return out


def set_secret(agent_id: str, key: str, value: str) -> None:
    aid = _validate_agent_id(agent_id)
    if not _DOTENV_LINE_RE.match(f"{key}=x"):
        raise AgentIdInvalid(f"invalid secret key: {key!r}")
    p = _env_path(aid)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []
    replaced = False
    new_line = f"{key}={value}"
    for i, raw in enumerate(lines):
        m = _DOTENV_LINE_RE.match(raw)
        if m and m.group("key") == key:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def delete_secret(agent_id: str, key: str) -> None:
    aid = _validate_agent_id(agent_id)
    p = _env_path(aid)
    if not p.exists():
        return
    kept = [
        raw for raw in p.read_text(encoding="utf-8", errors="replace").splitlines()
        if not (_DOTENV_LINE_RE.match(raw) and _DOTENV_LINE_RE.match(raw).group("key") == key)  # type: ignore[union-attr]
    ]
    p.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# Settings — skills (read from profile skills/ dir)
# ──────────────────────────────────────────────────────────────────────


def _parse_skill_front_matter(text: str) -> dict[str, str]:
    m = _SKILL_FRONT_MATTER_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group("header")) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "name": str(data.get("name") or ""),
        "description": str(data.get("description") or ""),
    }


def list_skills(agent_id: str) -> list[dict[str, str]]:
    aid = _validate_agent_id(agent_id)
    skills_dir = hermes_profile_root(aid) / _SKILLS_DIRNAME
    out: list[dict[str, str]] = []
    if not skills_dir.is_dir():
        return out
    for entry in sorted(skills_dir.iterdir()):
        skill_md = entry / _SKILL_ENTRY_FILENAME
        if not skill_md.is_file():
            continue
        meta = _parse_skill_front_matter(skill_md.read_text(encoding="utf-8", errors="replace"))
        out.append({
            "name": meta.get("name") or entry.name,
            "description": meta.get("description", ""),
            "path": str(entry),
        })
    return out


def read_skill(agent_id: str, name: str) -> str:
    aid = _validate_agent_id(agent_id)
    skill_md = hermes_profile_root(aid) / _SKILLS_DIRNAME / name / _SKILL_ENTRY_FILENAME
    if not skill_md.is_file():
        raise AgentNotFound(f"skill {name!r} not found")
    return skill_md.read_text(encoding="utf-8", errors="replace")


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _build_skill_md(name: str, description: str, body: str) -> str:
    desc = (description or "").strip().replace("\\", "\\\\").replace('"', '\\"')
    return f'---\nname: {name}\ndescription: "{desc}"\n---\n\n{(body or "").strip()}\n'


def write_skill(agent_id: str, *, name: str, description: str = "", content: str) -> dict[str, str]:
    """Create a new user-defined skill (``skills/{name}/SKILL.md``)."""
    aid = _validate_agent_id(agent_id)
    skill_name = (name or "").strip()
    if not _SKILL_NAME_RE.match(skill_name):
        raise AgentIdInvalid(
            "skill name must be non-empty and contain only letters, digits, '-' or '_'"
        )
    body = (content or "").strip()
    if not body:
        raise AgentIdInvalid("skill content is required")
    skill_dir = hermes_profile_root(aid) / _SKILLS_DIRNAME / skill_name
    if skill_dir.exists():
        raise AgentAlreadyExists(f"skill {skill_name!r} already exists")
    skill_dir.mkdir(parents=True, exist_ok=False)
    (skill_dir / _SKILL_ENTRY_FILENAME).write_text(
        _build_skill_md(skill_name, description, body), encoding="utf-8"
    )
    return {"name": skill_name, "description": (description or "").strip(), "path": str(skill_dir)}


def update_skill(agent_id: str, *, name: str, description: str = "", content: str) -> dict[str, str]:
    """Overwrite an existing user-defined skill's ``SKILL.md``.

    Mirrors :func:`write_skill` but requires the skill to already exist (the
    create path rejects existing dirs; this is the edit path)."""
    aid = _validate_agent_id(agent_id)
    skill_name = (name or "").strip()
    if not _SKILL_NAME_RE.match(skill_name):
        raise AgentIdInvalid(
            "skill name must be non-empty and contain only letters, digits, '-' or '_'"
        )
    body = (content or "").strip()
    if not body:
        raise AgentIdInvalid("skill content is required")
    skill_dir = hermes_profile_root(aid) / _SKILLS_DIRNAME / skill_name
    if not skill_dir.is_dir():
        raise AgentNotFound(f"skill {skill_name!r} not found")
    (skill_dir / _SKILL_ENTRY_FILENAME).write_text(
        _build_skill_md(skill_name, description, body), encoding="utf-8"
    )
    return {"name": skill_name, "description": (description or "").strip(), "path": str(skill_dir)}


def delete_skill(agent_id: str, name: str) -> None:
    aid = _validate_agent_id(agent_id)
    # Prefer the CLI (hub-installed skills); fall back to removing the dir.
    rc, _out, _err = _hermes_profile(aid, ["skills", "uninstall", name])
    if rc == 0:
        return
    skill_dir = hermes_profile_root(aid) / _SKILLS_DIRNAME / name
    root = hermes_profile_root(aid).resolve(strict=False)
    target = skill_dir.resolve(strict=False)
    if not str(target).startswith(str(root)) or not skill_dir.is_dir():
        raise AgentNotFound(f"skill {name!r} not found")
    shutil.rmtree(skill_dir)


# ──────────────────────────────────────────────────────────────────────
# Settings — cron (best-effort CLI parsing)
# ──────────────────────────────────────────────────────────────────────


def cron_available() -> bool:
    return hermes_executable() is not None


_STATUS_PLATFORM_SLUGS: dict[str, str] = {
    "telegram": "telegram",
    "discord": "discord",
    "whatsapp": "whatsapp",
    "signal": "signal",
    "slack": "slack",
    "email": "email",
    "sms": "sms",
    "dingtalk": "dingtalk",
    "feishu": "feishu",
    "wecom": "wecom",
    "wecom callback": "wecom_callback",
    "weixin": "weixin",
    "bluebubbles": "bluebubbles",
    "qqbot": "qqbot",
    "yuanbao": "yuanbao",
}

_MESSAGING_STATUS_RE = re.compile(
    r"^\s{2}([A-Za-z][A-Za-z0-9 /]+?)\s{2,}✓\s+configured"
    r"(?:\s+\(home:\s*([^)]+)\))?",
)


def _status_platform_slug(display: str) -> str:
    key = display.strip().lower()
    return _STATUS_PLATFORM_SLUGS.get(key, key.replace(" ", "_"))


def _parse_status_delivery_homes(text: str) -> dict[str, str]:
    """Map configured messaging platform slug → home ``chat_id`` (``""`` if the
    platform is configured but the status line reports no home).

    Only platforms shown as ``✓ configured`` in ``hermes status --all`` are
    returned — an unconfigured platform can't deliver, so it must not become a
    dropdown option.
    """
    homes: dict[str, str] = {}
    in_section = False
    for raw in text.splitlines():
        line = _strip_ansi(raw)
        if "Messaging Platforms" in line:
            in_section = True
            continue
        if in_section:
            if line.startswith("◆"):
                break
            m = _MESSAGING_STATUS_RE.match(line)
            if not m:
                continue
            slug = _status_platform_slug(m.group(1))
            homes.setdefault(slug, (m.group(2) or "").strip())
    return homes


def _parse_send_list_channels(text: str) -> dict[str, dict[str, str]]:
    """Map platform slug → ``{chat_id: display_name}`` from
    ``hermes send --list --json``.

    Hermes returns each channel as an **object** (``{"id","name","type",...}``),
    not a bare string; older/other builds may still emit plain strings, so we
    accept both. The ``id`` is the ``chat_id`` half of a ``platform:chat_id``
    delivery target; the ``name`` is only used to build a friendly label.
    """
    channels: dict[str, dict[str, str]] = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return channels
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        return channels
    for platform, chans in platforms.items():
        slug = str(platform).strip()
        if not slug or not isinstance(chans, list) or not chans:
            continue
        by_id: dict[str, str] = {}
        for ch in chans:
            if isinstance(ch, dict):
                cid = str(ch.get("id") or ch.get("chat_id") or "").strip()
                name = str(ch.get("name") or "").strip()
            else:
                cid = str(ch).strip()
                name = ""
            if not cid:
                continue
            by_id.setdefault(cid, name)
        if by_id:
            channels[slug] = by_id
    return channels


def list_cron_delivery_targets(agent_id: str) -> list[dict[str, str]]:
    """Return cron ``--deliver`` options for the profile.

    Always includes ``local``. Delivery targets are derived from two Hermes
    sources:

    - ``hermes -p <id> status --all`` — which platforms are ``✓ configured``
      and their home ``chat_id``.
    - ``hermes -p <id> send --list --json`` — discovered channels per platform.

    A bare ``<platform>`` target means "deliver to the platform's home chat";
    ``<platform>:<chat_id>`` targets a specific chat. Because a bare platform
    and its home ``chat_id`` deliver to the *same* place, we emit only the bare
    form for the home channel and reserve ``<platform>:<chat_id>`` for chats
    that differ from home — so a single home-only channel yields exactly one
    option instead of three near-duplicates.
    """
    aid = _validate_agent_id(agent_id)
    targets: list[dict[str, str]] = [{"value": "local", "label": "local"}]
    seen: set[str] = {"local"}

    def _add(value: str, label: str) -> None:
        if value and value not in seen:
            seen.add(value)
            targets.append({"value": value, "label": label})

    rc, out, _err = _hermes_profile(aid, ["send", "--list", "--json"])
    channels = _parse_send_list_channels(out) if rc == 0 else {}

    rc, out, _err = _hermes_profile(aid, ["status", "--all"])
    homes = _parse_status_delivery_homes(out) if rc == 0 else {}

    for platform in sorted(set(homes) | set(channels)):
        home = homes.get(platform, "")
        # Bare platform target (home chat) — only when Hermes reports the
        # platform configured; an unconfigured platform can't deliver.
        if platform in homes:
            _add(platform, platform)
        for cid, name in channels.get(platform, {}).items():
            if home and cid == home:
                continue  # same destination as the bare platform target
            label = f"{platform} ({name})" if name else f"{platform} ({cid})"
            _add(f"{platform}:{cid}", label)

    return targets


@dataclass
class CronJob:
    id: str = ""
    name: str = ""
    schedule: str = ""
    enabled: bool = True
    prompt: str = ""
    deliver: str = ""
    workdir: str = ""
    next_run: str = ""
    last_run: str = ""
    detail: str = ""
    raw: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _cron_jobs_path(agent_id: str) -> Path:
    """``~/.hermes/profiles/{id}/cron/jobs.json`` — Hermes' own per-profile cron
    store. A well-known profile file we read for fields the CLI does not expose
    (prompt, workdir, deliver, next/last run)."""
    return hermes_profile_root(agent_id) / "cron" / "jobs.json"


def _coerce_schedule_str(value: Any) -> str:
    """Hermes stores schedule as ``{"kind","expr","display"}`` (or a bare
    string in older formats). Return the most human-friendly form."""
    if isinstance(value, dict):
        return str(value.get("display") or value.get("expr") or "").strip()
    return str(value or "").strip()


def list_cron(agent_id: str) -> list[dict[str, Any]]:
    """List scheduled jobs by reading Hermes' per-profile ``cron/jobs.json``.

    The ``hermes cron list`` CLI prints a multi-line *block* per job (not a
    table) and omits the prompt entirely, so we read the structured store
    Hermes itself maintains — same approach as MCP servers / skills, which read
    well-known profile files rather than scraping CLI output. Returns one entry
    per job (the previous line-based parser produced one bogus entry per output
    line). Best-effort: a missing/unparseable file yields ``[]``.
    """
    aid = _validate_agent_id(agent_id)
    path = _cron_jobs_path(aid)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    raw_jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(raw_jobs, list):
        return []
    jobs: list[dict[str, Any]] = []
    for entry in raw_jobs:
        if not isinstance(entry, dict):
            continue
        job_id = str(entry.get("id") or "").strip()
        if not job_id:
            continue
        schedule = _coerce_schedule_str(
            entry.get("schedule_display") or entry.get("schedule")
        )
        name = str(entry.get("name") or "").strip()
        deliver = str(entry.get("deliver") or "").strip()
        workdir = str(entry.get("workdir") or "").strip()
        next_run = str(entry.get("next_run_at") or "").strip()
        last_run_at = str(entry.get("last_run_at") or "").strip()
        last_status = str(entry.get("last_status") or "").strip()
        last_run = f"{last_run_at} {last_status}".strip() if last_run_at else ""
        enabled = bool(entry.get("enabled", True)) and not entry.get("paused_at")
        detail_bits = [b for b in (schedule, deliver) if b]
        jobs.append({
            "id": job_id,
            "name": name or job_id,
            "schedule": schedule,
            "enabled": enabled,
            "prompt": str(entry.get("prompt") or ""),
            "deliver": deliver,
            "workdir": workdir,
            "next_run": next_run,
            "last_run": last_run,
            "detail": "  ·  ".join(detail_bits),
            "raw": schedule,
        })
    return jobs


def cron_action(agent_id: str, job_id: str, action: str) -> None:
    aid = _validate_agent_id(agent_id)
    verb = {"pause": "pause", "resume": "resume", "remove": "remove"}.get(action)
    if verb is None:
        raise AgentIdInvalid(f"unsupported cron action: {action!r}")
    rc, out, err = _hermes_profile(aid, ["cron", verb, job_id])
    if rc != 0:
        raise ProfileOpFailed(
            f"`hermes cron {verb} {job_id}` failed: "
            f"{(_strip_ansi(err) or _strip_ansi(out)).strip()}"
        )


def edit_cron(
    agent_id: str,
    job_id: str,
    *,
    schedule: str | None = None,
    prompt: str | None = None,
    name: str | None = None,
    deliver: str | None = None,
    workdir: str | None = None,
) -> None:
    """Edit an existing job via ``hermes -p <id> cron edit <job_id> --…``.

    Only the fields the caller passes (non-``None``) are forwarded, so an edit
    that touches just the schedule never clobbers the prompt/workdir. ``workdir``
    is the one field Hermes lets you *clear* with an empty string, so we forward
    it even when empty; the others are skipped when blank.
    """
    aid = _validate_agent_id(agent_id)
    jid = (job_id or "").strip()
    if not jid:
        raise AgentIdInvalid("cron job id is required")
    args = ["cron", "edit", jid]
    if schedule:
        args += ["--schedule", schedule]
    if prompt:
        args += ["--prompt", prompt]
    if name:
        args += ["--name", name]
    if deliver:
        args += ["--deliver", deliver]
    if workdir is not None:
        wd = workdir.strip()
        if wd:
            args += ["--workdir", str(_existing_directory(wd, field_name="workdir"))]
        else:
            args += ["--workdir", ""]  # explicit clear
    if len(args) == 3:
        return  # nothing to change
    rc, out, err = _hermes_profile(aid, args)
    if rc != 0:
        raise ProfileOpFailed(
            f"`hermes cron edit {jid}` failed: "
            f"{(_strip_ansi(err) or _strip_ansi(out)).strip()}"
        )


def create_cron(
    agent_id: str,
    *,
    schedule: str,
    prompt: str,
    name: str = "",
    workdir: str = "",
    deliver: str = "local",
) -> None:
    aid = _validate_agent_id(agent_id)
    args = ["cron", "create", schedule]
    if prompt:
        args.append(prompt)
    if name:
        args += ["--name", name]
    deliver_val = (deliver or "local").strip() or "local"
    if deliver_val != "local":
        args += ["--deliver", deliver_val]
    if workdir:
        args += ["--workdir", str(_existing_directory(workdir, field_name="workdir"))]
    args += ["--profile", aid]
    rc, out, err = _hermes_profile(aid, args)
    if rc != 0:
        raise ProfileOpFailed(
            f"`hermes cron create` failed: "
            f"{(_strip_ansi(err) or _strip_ansi(out)).strip()}"
        )


# ──────────────────────────────────────────────────────────────────────
# Direct chat dispatch (one-shot, profile-scoped, cwd = chosen workdir)
# ──────────────────────────────────────────────────────────────────────


def chat_once(agent_id: str, *, message: str, workdir: str, resume: bool = False) -> str:
    """Run a single profile-scoped turn in ``workdir`` and return clean text.

    Always enters the agent's role via ``hermes -p <id>``. The first turn of a
    conversation runs ``--yolo -z <message>`` (creates the session); subsequent
    turns add ``-c`` (``--continue`` with no name) to resume the most-recent
    session in this profile, so consecutive turns stay in one logical session.
    Each profile backs exactly one agent and only direct chat uses it, so
    "most recent" reliably identifies this conversation. ``-z`` prints only the
    final answer on stdout.

    When ``resume=True`` but the CLI session cannot be continued (e.g. after a
    restart), we silently fall back to a fresh turn without ``-c`` so the user
    can keep chatting; UI history remains the source of truth for the thread.

    Transient DNS/connection failures are retried up to
    :data:`CHAT_CONNECTION_RETRY_ATTEMPTS` times before surfacing an error.
    """
    aid = _validate_agent_id(agent_id)
    wd = _existing_directory(workdir, field_name="workdir")

    def _run(*, with_resume: bool) -> tuple[int, str, str]:
        args = ["--yolo", *(["-c"] if with_resume else []), "-z", message]
        return _hermes_profile(aid, args, cwd=wd, timeout=_CHAT_TIMEOUT_SEC)

    use_resume = resume
    last_detail = ""
    for attempt in range(CHAT_CONNECTION_RETRY_ATTEMPTS):
        rc, out, err = _run(with_resume=use_resume)
        if rc == 0:
            return out.strip()
        if use_resume:
            resume_err = (_strip_ansi(err) or _strip_ansi(out)).strip()[:500]
            logger.warning(
                "hermes_chat_resume_failed_fallback_fresh",
                agent_id=aid,
                error=resume_err,
            )
            use_resume = False
            rc, out, err = _run(with_resume=False)
            if rc == 0:
                return out.strip()
        last_detail = (_strip_ansi(err) or _strip_ansi(out)).strip()
        if (
            attempt + 1 < CHAT_CONNECTION_RETRY_ATTEMPTS
            and is_transient_connection_error(last_detail)
        ):
            delay = CHAT_CONNECTION_RETRY_DELAYS_SEC[
                min(attempt, len(CHAT_CONNECTION_RETRY_DELAYS_SEC) - 1)
            ]
            logger.warning(
                "hermes_chat_connection_retry",
                agent_id=aid,
                attempt=attempt + 1,
                delay_sec=delay,
                error_preview=last_detail[:240],
            )
            time.sleep(delay)
            use_resume = resume
            continue
        break
    raise ProfileOpFailed(f"hermes chat failed: {last_detail[:1000]}")


__all__ = [
    "CommitInput",
    "BootstrapOutcome",
    "UpdateInput",
    "HermesAgentError",
    "HermesUnavailable",
    "AgentIdInvalid",
    "AgentAlreadyExists",
    "AgentNotFound",
    "AgentInUse",
    "ProfileOpFailed",
    "AgentCreateCancelled",
    "cancel_create_agent",
    "is_create_in_flight",
    "PROBE_FAST",
    "PROBE_FULL",
    "RECONCILE_FAST",
    "RECONCILE_FULL",
    "hermes_home",
    "hermes_profile_root",
    "hermes_executable",
    "probe_runtime_running",
    "list_profile_names",
    "list_profile_names_checked",
    "list_profile_names_from_fs",
    "commit_agent",
    "claim_profile",
    "list_claimable_profiles",
    "get_agent",
    "list_agents",
    "update_agent",
    "delete_agent",
    "is_managed",
    "start_gateway",
    "restart_gateway",
    "read_gateway_cwd",
    "write_gateway_cwd",
    "read_soul",
    "write_soul",
    "read_model",
    "write_model",
    "import_model_from_profile",
    "list_mcp_servers",
    "upsert_mcp_server",
    "delete_mcp_server",
    "list_secrets",
    "set_secret",
    "delete_secret",
    "list_skills",
    "read_skill",
    "write_skill",
    "update_skill",
    "delete_skill",
    "cron_available",
    "list_cron_delivery_targets",
    "list_cron",
    "cron_action",
    "create_cron",
    "edit_cron",
    "chat_once",
    "backfill_hermes_inference_config",
]
