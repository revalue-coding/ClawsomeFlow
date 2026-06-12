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

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import Config, load_config
from app.logging_setup import get_logger
from app.models import Flow, HermesAgent
from app.services import subprocess_registry as _subproc_registry
from app.storage import StorageBackend, get_storage

logger = get_logger("services.hermes_agents")

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

HERMES_HOME_ENV = "HERMES_HOME"
_DEFAULT_HERMES_HOME = "~/.hermes"
_AGENT_ID_RE = re.compile(r"^[a-z0-9]+$")  # hermes requires lowercase alphanumeric
_AGENT_ID_MIN_LEN = 2
_AGENT_ID_MAX_LEN = 40
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
            "Hermes agent id (profile name) must be lowercase alphanumeric "
            "([a-z0-9])"
        )
    if not (_AGENT_ID_MIN_LEN <= len(aid) <= _AGENT_ID_MAX_LEN):
        raise AgentIdInvalid(
            f"Hermes agent id length must be {_AGENT_ID_MIN_LEN}-{_AGENT_ID_MAX_LEN}"
        )
    if aid in _RESERVED_PROFILE_NAMES:
        raise AgentIdInvalid(f"'{aid}' is reserved and cannot be used")
    return aid


# ──────────────────────────────────────────────────────────────────────
# Runtime availability
# ──────────────────────────────────────────────────────────────────────


PROBE_FAST = "fast"
PROBE_FULL = "full"
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


# Inference config files a fresh profile needs to be usable. A brand-new
# `hermes profile create` leaves these absent, so the profile has NO model /
# API keys — every `hermes -p <id> …` call (bootstrap, chat, task dispatch)
# then dies with "No inference provider configured" and SOUL.md is never
# written. We seed them from the user's active/root profile so managed agents
# inherit the host's model + keys (config.yaml + .env only — never SOUL.md or
# memories, to avoid leaking the operator's personal identity/memory).
_INFERENCE_CONFIG_FILES = ("config.yaml", ".env")


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


def _seed_profile_inference_config(agent_id: str) -> None:
    """Copy model + API-key config into a freshly created profile (idempotent;
    never clobbers existing files). Best-effort: failures are logged, not
    raised — the caller surfaces a clearer error if bootstrap then fails."""
    source = _active_profile_root()
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
            if name == ".env":  # keys file — keep it private
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


def _run_bootstrap(aid: str, args: list[str], *, cwd: str, timeout: float) -> int:
    """Run the bootstrap ``hermes`` call as a killable subprocess registered
    under *aid* so :func:`cancel_create_agent` can terminate it mid-flight.
    Returns the exit code (non-zero if killed/cancelled)."""
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
    if proc.returncode != 0 and err:
        logger.warning(
            "hermes_bootstrap_failed", agent_id=aid, error=_strip_ansi(err).strip()[:500]
        )
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
) -> HermesAgent:
    """Create a Hermes profile + bootstrap self-definition + persist the row.

    Creates of the same id are serialized by a per-id lock: a duplicate /
    concurrent request (e.g. a double-submit) would otherwise both pass the
    existence check below and race to ``hermes_create``; the loser's
    IntegrityError rollback would then delete the WINNER's freshly-built
    profile. We acquire the lock non-blocking so the loser fails fast with
    AgentAlreadyExists instead of tying up a worker for the whole bootstrap.
    """
    cfg = config or load_config()
    storage = storage or get_storage(cfg)
    aid = _validate_agent_id(cmd.id)

    lock = _create_id_lock(aid)
    if not lock.acquire(blocking=False):
        raise AgentAlreadyExists(f"a create for {aid!r} is already in progress")
    try:
        return _commit_agent_locked(cmd, aid, user=user, storage=storage)
    finally:
        lock.release()


def _commit_agent_locked(
    cmd: CommitInput, aid: str, *, user: str, storage: StorageBackend
) -> HermesAgent:
    if storage.hermes_get(aid) is not None:
        raise AgentAlreadyExists(f"hermes agent {aid!r} already exists")
    if aid in list_profile_names():
        raise AgentAlreadyExists(
            f"a Hermes profile named {aid!r} already exists; claim it instead",
        )

    # Fresh attempt: clear any stale cancel flag from a previous create of the
    # same id (the user may retry an id whose earlier create they cancelled).
    with _CREATE_LOCK:
        _CANCELLED_CREATES.discard(aid)

    def _abort_if_cancelled() -> None:
        if _is_create_cancelled(aid):
            _rollback_create(aid, storage=storage)
            with _CREATE_LOCK:
                _CANCELLED_CREATES.discard(aid)
            raise AgentCreateCancelled(f"creation of {aid!r} cancelled by user")

    description = (cmd.description or "").strip()
    create_args = ["profile", "create", aid]
    if description:
        create_args += ["--description", description]
    rc, out, err = _run_hermes(create_args)
    if rc != 0:
        raise ProfileOpFailed(
            f"`hermes profile create {aid}` failed: {(_strip_ansi(err) or _strip_ansi(out)).strip()}"
        )

    profile_root = str(hermes_profile_root(aid))

    # Seed model + API keys so the new profile can actually run (bootstrap +
    # later chat/task dispatch all go through `hermes -p <id>` and need them).
    _seed_profile_inference_config(aid)

    _abort_if_cancelled()  # cancelled during profile create → roll back now

    # Bootstrap self-definition (best-effort: keep the agent even if it fails,
    # but honour a cancel that lands while the up-to-10-min `hermes -z` runs).
    if not cmd.skip_bootstrap:
        try:
            _run_bootstrap(
                aid,
                ["-p", aid, "--yolo", "-z", _bootstrap_prompt(cmd.name, description)],
                cwd=profile_root,
                timeout=_BOOTSTRAP_TIMEOUT_SEC,
            )
        except HermesAgentError as exc:
            logger.warning("hermes_bootstrap_error", agent_id=aid, error=str(exc))

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
    on_disk_set = set(on_disk)
    managed = {r.id: r for r in storage.hermes_list(owner_user=user)}
    for name in on_disk:  # adopt newcomers
        if name not in managed:
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


def list_agents(
    *,
    user: str | None = None,
    storage: StorageBackend | None = None,
    config: Config | None = None,
    adopt: bool = True,
) -> list[HermesAgent]:
    storage = storage or get_storage(config or load_config())
    # Reconcile against live Hermes profiles so the list always reflects what
    # actually exists. Only when we have a concrete owner user.
    if adopt and user:
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


_DOTENV_LINE_RE = re.compile(r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<val>.*)$")


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


@dataclass
class CronJob:
    id: str = ""
    name: str = ""
    schedule: str = ""
    enabled: bool = True
    detail: str = ""
    raw: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def list_cron(agent_id: str) -> list[dict[str, Any]]:
    """Best-effort parse of ``hermes -p <id> cron list --all``.

    The CLI prints a human table; we extract a stable id/name + schedule per
    row and keep the raw line so the UI can still show unparsed detail.
    """
    aid = _validate_agent_id(agent_id)
    rc, out, _err = _hermes_profile(aid, ["cron", "list", "--all"])
    if rc != 0:
        return []
    jobs: list[dict[str, Any]] = []
    for raw in out.splitlines():
        line = _strip_ansi(raw).strip()
        if not line:
            continue
        low = line.lower()
        if "no scheduled jobs" in low or low.startswith("create one"):
            continue
        if low.startswith("id") and "schedule" in low:
            continue  # header
        if set(line) <= set("─-—│| "):
            continue
        cols = re.split(r"\s{2,}", line)
        first = cols[0].strip()
        jobs.append({
            "id": first,
            "name": cols[1].strip() if len(cols) > 1 else first,
            "schedule": cols[2].strip() if len(cols) > 2 else "",
            "enabled": "paused" not in low and "disabled" not in low,
            "detail": "  ".join(c.strip() for c in cols[1:]),
            "raw": line,
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


def create_cron(
    agent_id: str,
    *,
    schedule: str,
    prompt: str,
    name: str = "",
    workdir: str = "",
) -> None:
    aid = _validate_agent_id(agent_id)
    args = ["cron", "create", schedule]
    if prompt:
        args.append(prompt)
    if name:
        args += ["--name", name]
    if workdir:
        args += ["--workdir", workdir]
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
    """
    aid = _validate_agent_id(agent_id)
    wd = Path(workdir).expanduser()
    if not wd.is_dir():
        raise HermesAgentError(f"working directory does not exist: {workdir}")

    def _run(*, with_resume: bool) -> tuple[int, str, str]:
        args = ["--yolo", *(["-c"] if with_resume else []), "-z", message]
        return _hermes_profile(aid, args, cwd=wd, timeout=_CHAT_TIMEOUT_SEC)

    rc, out, err = _run(with_resume=resume)
    if rc == 0:
        return out.strip()
    if resume:
        resume_err = (_strip_ansi(err) or _strip_ansi(out)).strip()[:500]
        logger.warning(
            "hermes_chat_resume_failed_fallback_fresh",
            agent_id=aid,
            error=resume_err,
        )
        rc, out, err = _run(with_resume=False)
        if rc == 0:
            return out.strip()
    raise ProfileOpFailed(
        f"hermes chat failed: {(_strip_ansi(err) or _strip_ansi(out)).strip()[:1000]}"
    )


__all__ = [
    "CommitInput",
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
    "PROBE_FAST",
    "PROBE_FULL",
    "hermes_home",
    "hermes_profile_root",
    "hermes_executable",
    "probe_runtime_running",
    "list_profile_names",
    "list_profile_names_checked",
    "commit_agent",
    "claim_profile",
    "list_claimable_profiles",
    "get_agent",
    "list_agents",
    "update_agent",
    "delete_agent",
    "is_managed",
    "read_soul",
    "write_soul",
    "read_model",
    "write_model",
    "list_secrets",
    "set_secret",
    "delete_secret",
    "list_skills",
    "read_skill",
    "delete_skill",
    "cron_available",
    "list_cron",
    "cron_action",
    "create_cron",
    "chat_once",
    "backfill_hermes_inference_config",
]
