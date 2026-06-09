"""Re-install / upgrade orchestration for ``~/.clawsomeflow/``.

Maintenance rule for legacy-repair modules (MUST follow):
* Every repair block added to this file MUST carry an inline comment that
  clearly states: (1) which historical issue it fixes, (2) which old versions
  it targets, and (3) after which upgrade baseline the block can be removed.
* This rule exists so we can safely raise the minimum upgrade baseline later
  and delete obsolete repairs with confidence.

Goal: a `pip install -U clawsomeflow` followed by `csflow upgrade` (or
`csflow start`, which auto-detects) should bring an existing data dir
fully in sync with the new package version — schema + bundled runtime
materials — **without ever destroying user state**.

Design (two independent pieces of state — this is the key to stable↔beta safety):

* **Version marker** ``~/.clawsomeflow/.csflow-version`` — the human-facing
  "what version last blessed this dir". Versions are PEP 440-ordered so
  pre-releases sort *below* their final (``X.Y.Za1 < …b1 < …rc1 < X.Y.Z``; see
  :func:`_key`). The marker is a **HIGH-WATERMARK**: :func:`run_upgrade` only
  advances it (``_gt(target, marker)``), never moves it backward. So a downgrade
  (e.g. an explicit ``csflow upgrade`` after installing an older build) keeps the
  higher marker. ``needs_upgrade`` (what ``csflow start`` calls) only fires going
  forward; downgrade is a no-op + warning.

* **Applied-migrations ledger** ``~/.clawsomeflow/.csflow-migrations.json`` — the
  *authoritative, direction-safe* gate for which migrations run. A migration runs
  iff its id (``Migration.version``) is NOT in the ledger, so it executes
  **exactly once ever**, regardless of how the user hops between stable and beta
  builds (``0.1.12b1 → 0.1.12b2 → 0.1.12`` runs a 0.1.12-targeted migration once,
  not three times) or downgrades then re-upgrades. On first use the ledger is
  seeded from the existing marker (migrations ``≤ marker`` are treated as already
  applied); a fresh install seeds the full set via :func:`seed_fresh_migration_ledger`.
  ``Migration.version``/``applies_after`` remain useful for ordering + legacy
  seeding, but they are no longer the run gate.

* Migrations MUST be idempotent regardless (defence in depth), and registered in
  chronological order with **unique** ids (enforced by :func:`_assert_unique_migration_ids`).

* Non-migration upgrade steps (re-deploy source payloads and per-user agent
  runtime refresh: skills + common cron jobs) are idempotent — we
  run them every time, regardless of version delta. Cheap and self-healing.
* Upgrade/deploy NEVER auto-restores removed OpenClaw registrations; restore
  remains an explicit user action via the "Restore Agent" entry.
* "Non-first deployment" is determined by whether ``~/.clawsomeflow/``
  already exists, not by marker presence.

What :func:`run_upgrade` does, in order:
1. (Optional) Rebuild ``frontend/dist`` when running from an editable source tree.
2. Detect ``from_version`` from the marker (or ``None`` for legacy/unmarked
   installs).
3. Run any DB migrations whose range matches.
4. Re-seed bundled skills into ``~/.clawsomeflow/.skills-source/``.
5. Re-deploy OpenClaw common payload + tools (optional; non-fatal if runtime
   is absent).
6. Re-deploy per-user agent runtime materials (skills + built-in cron jobs only;
   no auto registration restore).
7. Write the marker to the new version.

If any step raises, the marker is left untouched, so the next attempt
will retry from the same starting point.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app import __version__, paths
from app.config import Config, load_config, save_config
from app.logging_setup import get_logger

logger = get_logger("upgrade")


# ──────────────────────────────────────────────────────────────────────
# Version marker
# ──────────────────────────────────────────────────────────────────────


_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$"
)


def read_marker() -> str | None:
    """Return the marker version string, or ``None`` if missing/unreadable."""
    path = paths.clawsomeflow_home_path() / ".csflow-version"
    if not path.exists():
        return None
    try:
        v = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not v or not _VERSION_RE.match(v):
        logger.warning("version_marker_invalid", value=v)
        return None
    return v


def write_marker(version: str) -> None:
    """Write the marker. Invalid values raise ``ValueError`` early."""
    if not _VERSION_RE.match(version):
        raise ValueError(f"Refusing to write invalid marker {version!r}")
    path = paths.version_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(version + "\n", encoding="utf-8")
    tmp.replace(path)


def _key(v: str) -> tuple[int, int, int, int, int]:
    """Parse ``X.Y.Z[bN|rcN]`` into a sortable tuple where the
    release line *follows* its pre-releases (PEP 440 ordering):

        1.2.3a1 < 1.2.3a2 < 1.2.3b1 < 1.2.3rc1 < 1.2.3
    """
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$", v)
    if not m:
        raise ValueError(f"Invalid version {v!r}")
    major, minor, patch = int(m[1]), int(m[2]), int(m[3])
    kind, num = m[4], m[5]
    if kind is None:
        # Final release outranks all pre-releases of same X.Y.Z.
        return (major, minor, patch, 3, 0)
    rank = {"a": 0, "b": 1, "rc": 2}[kind]
    return (major, minor, patch, rank, int(num))


def _gt(a: str | None, b: str | None) -> bool:
    """``a > b`` per :func:`_key`. ``None`` is treated as ``-∞``."""
    if a is None:
        return False
    if b is None:
        return True
    return _key(a) > _key(b)


# ──────────────────────────────────────────────────────────────────────
# Applied-migrations ledger — the authoritative, direction-safe gate
# ──────────────────────────────────────────────────────────────────────
#
# The single version marker tells us "which code last touched the dir", but it
# cannot answer "did migration X already run?" when the user hops between stable
# and beta builds (e.g. 0.1.12b1 → 0.1.12b2 → 0.1.12) — a `version > marker`
# gate would re-run a 0.1.12-targeted migration on every beta. The ledger records
# the exact set of applied migration ids, so each migration runs **exactly once**
# regardless of version direction (beta↔stable, downgrade-then-reupgrade).


def read_applied_migrations() -> set[str] | None:
    """Return the set of applied migration ids, or ``None`` if no ledger exists."""
    path = paths.migrations_ledger_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        applied = data.get("applied", [])
        return {str(x) for x in applied} if isinstance(applied, list) else set()
    except (OSError, ValueError):
        logger.warning("migrations_ledger_unreadable", path=str(path))
        return set()


def _write_applied_migrations(applied: set[str]) -> None:
    path = paths.migrations_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"applied": sorted(applied)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _seed_applied_from_marker(marker: str | None) -> set[str]:
    """First-time ledger seed: migrations whose ``version`` is at-or-below the
    existing marker were already applied under the legacy marker-only scheme
    (or are baked into a fresh install at ``marker``). ``marker is None``
    (legacy/unmarked) seeds empty → every migration will run."""
    if marker is None:
        return set()
    return {m.version for m in MIGRATIONS if not _gt(m.version, marker)}


def seed_fresh_migration_ledger() -> None:
    """Mark **all** registered migrations as applied — used on FRESH install,
    whose schema already reflects the current code (no historical data to
    migrate). Idempotent: overwrites the ledger with the full set."""
    _write_applied_migrations({m.version for m in MIGRATIONS})


def _assert_unique_migration_ids() -> None:
    seen: set[str] = set()
    for m in MIGRATIONS:
        if m.version in seen:
            raise ValueError(f"duplicate migration id {m.version!r} in MIGRATIONS")
        seen.add(m.version)


# ──────────────────────────────────────────────────────────────────────
# Migration registry
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Migration:
    """A single, ordered upgrade step.

    Two fields define when the migration applies:

    * ``version``       — the version this migration *brings the data dir to*.
                          The migration is skipped if the marker is already
                          at or past this version.
    * ``applies_after`` — *inclusive* lower bound on the marker. The
                          migration is skipped if marker < ``applies_after``
                          (i.e. earlier prerequisite migrations haven't run
                          yet, and we shouldn't skip ahead).
                          Default ``None`` = no lower bound.

    Special case: ``marker is None`` means "legacy install, no marker
    file exists yet". In that case the lower bound is ignored — every
    migration applies, in registry order, to bring the dir up to date.

    Migrations MUST be idempotent (re-running them on the same data dir
    should be a safe no-op). In particular, **a repair that finds the data
    structure already in its target state must treat that as SUCCESS, never an
    error**: check the target state before mutating AND, if the mutating op still
    raises, re-check — if it's now (or already was) fixed, swallow and move on;
    only surface genuine failures. (The ledger may be reset / re-seeded, and
    best-effort repairs are retried, so "already fixed" WILL happen.)

    ``critical`` (default ``True``): a critical migration that RAISES blocks the
    marker from advancing (so it retries next run) — use for schema/data
    transforms that must not be skipped. A **best-effort repair** sets
    ``critical=False``: if it raises, the failure is reported to the terminal but
    the upgrade still completes and the marker advances (the service stays
    usable). Either way, the rest of the pipeline (schema init, redeploy) always
    runs — a migration failure never aborts the whole upgrade.

    ``apply`` may return a ``list[str]`` of non-fatal "could not repair X"
    messages; they are surfaced to the user terminal as warnings without marking
    the upgrade failed. Returning ``None`` means "no warnings".
    """
    version: str
    description: str
    apply: Callable[[Config], list[str] | None]
    applies_after: str | None = None
    critical: bool = True


def _applies(m: Migration, marker: str | None) -> bool:
    # Already at or past this migration's target version → skip.
    if marker is not None and not _gt(m.version, marker):
        return False
    # Lower bound check (only when we know the marker).
    if marker is not None and m.applies_after is not None:
        if _gt(m.applies_after, marker):  # applies_after > marker → too early
            return False
    return True


def _provision_managed_agents_for_existing_flows(config: Config) -> None:
    """Back-compat: provision managed-agent records + runtime profiles for
    Hermes / Claude / Codex agents already referenced by existing Flow templates.

    Newer ClawsomeFlow requires these agents to be *managed* (created in the
    management module) — `validate_flow_against_db` rejects unmanaged ones and
    the scheduler binds them via a profile (Hermes ``-p <id>`` / Claude+Codex
    ``CLAUDE_CONFIG_DIR``/``CODEX_HOME`` through a ClawTeam runtime profile).
    Upgrade-only users whose Flows predate this would otherwise break, so we
    create the minimal artifacts here:

    * Hermes  → ``hermes profile create <id>`` + HermesAgent row.
    * Claude/Codex → config home + ClawTeam env profile + ManagedAgent row.

    Idempotent + best-effort: existing records are skipped, and any per-agent
    failure (e.g. an id that isn't a valid lowercase-alphanumeric Hermes profile
    name, or a missing CLI) is logged and skipped so the upgrade still advances.
    """
    from app.models import HermesAgent, ManagedAgent
    from app.scheduler import managed_runtime
    from app.services.hermes_agents import hermes_profile_root
    from app.storage import get_storage

    storage = get_storage(config)  # ensure HermesAgent/ManagedAgent tables exist

    # Page through every user's Flows.
    flows = []
    offset = 0
    while True:
        page, total = storage.flow_list(owner_user=None, limit=200, offset=offset)
        flows.extend(page)
        offset += len(page)
        if not page or offset >= total:
            break

    hermes_done = 0
    managed_done = 0
    failures: list[str] = []  # human-readable per-agent failures → terminal
    hermes_exe = shutil.which("hermes")
    for flow in flows:
        spec = flow.spec if isinstance(flow.spec, dict) else {}
        owner = flow.owner_user
        flow_label = flow.name or flow.id
        for agent in (spec.get("agents") or []):
            if not isinstance(agent, dict):
                continue
            kind = agent.get("kind")
            aid = str(agent.get("id") or "").strip()
            if not aid:
                continue
            try:
                if kind == "hermes":
                    if storage.hermes_get(aid) is not None:
                        continue
                    if hermes_exe is None:
                        failures.append(
                            f"Flow '{flow_label}': Hermes agent '{aid}' not provisioned "
                            "(hermes CLI not installed) — create it in the management module."
                        )
                        continue
                    proc = subprocess.run(  # noqa: S603
                        [hermes_exe, "profile", "create", aid],
                        capture_output=True, text=True, timeout=60,
                    )
                    out = (proc.stderr or "") + (proc.stdout or "")
                    if proc.returncode != 0 and "exist" not in out.lower():
                        failures.append(
                            f"Flow '{flow_label}': could not create Hermes profile '{aid}': "
                            f"{out.strip()[:160]}"
                        )
                        continue
                    try:
                        storage.hermes_create(HermesAgent(
                            id=aid, name=aid, profile_root=str(hermes_profile_root(aid)),
                            created_by_user=owner,
                        ))
                    except Exception:
                        # Already fixed (row appeared concurrently / out-of-band)
                        # → not an error; only re-raise if it's a real failure.
                        if storage.hermes_get(aid) is not None:
                            continue
                        raise
                    hermes_done += 1
                elif kind in ("claude", "codex"):
                    if storage.managed_get(aid) is not None:
                        continue
                    home = managed_runtime.managed_home(kind, aid)
                    home.mkdir(parents=True, exist_ok=True)
                    profile = managed_runtime.ensure_profile(kind, aid)
                    try:
                        storage.managed_create(ManagedAgent(
                            id=aid, kind=kind, name=aid, config_home=str(home),
                            clawteam_profile=profile, created_by_user=owner,
                        ))
                    except Exception:
                        if storage.managed_get(aid) is not None:
                            continue  # already fixed → success, not error
                        raise
                    managed_done += 1
                # cursor: not managed-enforced yet → skip.
            except Exception as exc:  # best-effort: never block the upgrade
                failures.append(
                    f"Flow '{flow_label}': could not provision {kind} agent '{aid}': "
                    f"{str(exc)[:160]}"
                )
                logger.warning(
                    "upgrade_provision_agent_failed",
                    agent_id=aid, kind=kind, error=str(exc)[:240],
                )
    if hermes_done or managed_done:
        logger.info(
            "upgrade_provisioned_managed_agents",
            hermes=hermes_done, managed=managed_done, failures=len(failures),
        )
    return failures


# Register migrations in chronological order. Newer entries come last.
# Each ``apply`` MUST be idempotent (re-runnable). A migration runs only when
# the user's version marker is below the migration ``version`` (minimal,
# baseline-aware compatibility).
MIGRATIONS: list[Migration] = [
    Migration(
        # MUST be strictly greater than the highest already-released version
        # (0.1.12) so existing users' ledger seed does NOT mark it already-applied
        # — otherwise the provision would be skipped for current installs. This
        # ships in the 0.1.13 line (first beta 0.1.13b1).
        version="0.1.13b1",
        description="provision managed-agent records/profiles for agents already in existing Flows",
        apply=_provision_managed_agents_for_existing_flows,
        critical=False,  # best-effort repair: failures are reported, never abort
    ),
]


# ──────────────────────────────────────────────────────────────────────
# Upgrade report
# ──────────────────────────────────────────────────────────────────────


@dataclass
class UpgradeReport:
    from_version: str | None
    to_version: str
    data_home_preexisting: bool = False
    frontend_build_status: str = "not-requested"
    frontend_build_detail: str = ""
    migrations_run: list[str] = field(default_factory=list)
    skills_reseeded: bool = False
    openclaw_status: str = ""
    user_agent_skill_results: dict[str, list[str]] = field(default_factory=dict)
    user_agent_cron_sync_results: dict[str, bool] = field(default_factory=dict)
    redeploy_performed: bool = False
    schema_ready: bool = False
    marker_written: bool = False
    errors: list[str] = field(default_factory=list)
    # Non-fatal repair failures (best-effort migrations + optional steps).
    # Surfaced to the user terminal but do NOT make the upgrade "not ok".
    repair_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def is_first_install(self) -> bool:
        return not self.data_home_preexisting

    @property
    def is_no_op(self) -> bool:
        """True if the marker already matched current version."""
        return self.from_version == self.to_version


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────


def run_upgrade(
    *,
    config: Config | None = None,
    target_version: str | None = None,
    include_openclaw: bool = True,
    include_user_agent_skill_refresh: bool = True,
    include_frontend_build: bool = False,
) -> UpgradeReport:
    """Drive the full upgrade pipeline. Always idempotent.

    Returns a populated :class:`UpgradeReport` even on partial failure.
    Caller decides whether to abort (check ``report.ok``).
    """
    data_home_preexisting = paths.clawsomeflow_home_exists()
    cfg = config or load_config()
    target = target_version or __version__
    marker = read_marker()
    report = UpgradeReport(
        from_version=marker,
        to_version=target,
        data_home_preexisting=data_home_preexisting,
    )

    logger.info(
        "upgrade_start",
        from_version=marker, to_version=target,
        is_first_install=report.is_first_install,
    )

    # 0a. Private secrets: ensure the config has both the internal HMAC secret
    # and the public-API ``api_token``. Done here, unconditionally and early, so
    # upgrade-only users (who may have a pre-token config.json) end up identical
    # to fresh deploys — the /api guard works for them too. Idempotent: a no-op
    # when both are already present. Secrets live only in the private
    # ~/.clawsomeflow/config.json (gitignored), never committed.
    try:
        from app.integrations.internal_token import (
            ensure_api_token_initialised,
            ensure_secret_initialised,
        )

        cfg_with_secrets = ensure_secret_initialised(cfg)
        cfg_with_secrets = ensure_api_token_initialised(cfg_with_secrets)
        if cfg_with_secrets is not cfg:
            save_config(cfg_with_secrets)
            cfg = cfg_with_secrets
            logger.info("upgrade_secrets_initialised")
    except Exception as exc:  # pragma: no cover - defensive; never block upgrade
        report.errors.append(f"secrets init: {exc}")
        logger.exception("upgrade_secrets_init_failed", error=str(exc))

    # 0. Editable-source frontend build (optional).
    if include_frontend_build:
        status, detail = _build_frontend_bundle_if_editable()
        report.frontend_build_status = status
        report.frontend_build_detail = detail
        logger.info(
            "upgrade_frontend_build_status",
            status=status,
            detail=detail,
        )
        if status == "failed":
            # Non-aborting: editable-source SPA rebuild failed, but we still run
            # the data-affecting steps below so the service stays usable.
            report.repair_warnings.append(f"frontend build: {detail}")
    else:
        report.frontend_build_status = "not-requested"

    # 1. Migrations — gated by the applied-ledger (direction-safe), NOT by a
    #    `version > marker` comparison. This is what makes stable↔beta switching
    #    correct: each migration runs exactly once, ever.
    _assert_unique_migration_ids()
    applied = read_applied_migrations()
    if applied is None:
        # First run with the ledger: seed from the legacy marker so migrations
        # already applied under the old scheme aren't re-run.
        applied = _seed_applied_from_marker(marker)
        _write_applied_migrations(applied)
    critical_migration_failed = False
    for m in MIGRATIONS:
        if m.version in applied:
            continue
        try:
            warnings = m.apply(cfg)
            applied.add(m.version)
            _write_applied_migrations(applied)  # record incrementally
            report.migrations_run.append(m.version)
            if warnings:
                report.repair_warnings.extend(str(w) for w in warnings)
            logger.info(
                "upgrade_migration_applied",
                version=m.version, description=m.description,
                warnings=len(warnings or []),
            )
        except Exception as exc:
            logger.exception(
                "upgrade_migration_failed",
                version=m.version, critical=m.critical, error=str(exc),
            )
            if m.critical:
                # Block the marker (retry next run) but DO NOT abort — keep going
                # so schema init etc. still run and the service stays usable.
                report.errors.append(f"migration {m.version}: {exc}")
                critical_migration_failed = True
            else:
                # Best-effort repair: report to terminal, give up on this one
                # (mark applied so it doesn't re-fail forever), and continue.
                report.repair_warnings.append(f"repair {m.version}: {exc}")
                applied.add(m.version)
                _write_applied_migrations(applied)

    # 2. Schema (idempotent — SQLModel.create_all + missing-column adds).
    try:
        from app.storage import get_storage
        get_storage(cfg)
        report.schema_ready = True
    except NotImplementedError as exc:
        report.errors.append(f"storage backend: {exc}")
    except Exception as exc:
        report.errors.append(f"schema init: {exc}")
        logger.exception("upgrade_schema_failed", error=str(exc))

    # 3. OpenClaw integration: re-seed skills + redeploy common payload.
    if include_openclaw:
        try:
            from app.integrations.openclaw_install import install_into_openclaw
            import asyncio
            asyncio.run(install_into_openclaw(config=cfg))
            report.skills_reseeded = True
            report.redeploy_performed = True
            report.openclaw_status = "ready"
        except FileNotFoundError as exc:
            # OpenClaw is optional for deploy/upgrade. Skip quietly with status.
            report.openclaw_status = "not-configured"
            logger.warning("upgrade_openclaw_skipped", error=str(exc))
        except Exception as exc:
            report.openclaw_status = "integration-failed"
            report.repair_warnings.append(f"openclaw integration: {exc}")
            logger.exception("upgrade_openclaw_failed", error=str(exc))
    else:
        report.openclaw_status = "skipped-by-flag"

    # 4. Re-deploy per-user agent runtime materials:
    #    - skills (so old agents pick up new common skills)
    #    - common built-in cron definitions
    #    Failures here are non-fatal.
    if include_user_agent_skill_refresh:
        try:
            from app.services.openclaw_agents import (
                reinstall_skills_for_all,
                sync_common_cron_jobs_for_all,
            )
        except Exception as exc:
            # Most likely cause: storage not yet ready (server mode w/ no PG).
            # Don't fail the whole upgrade — warn.
            report.repair_warnings.append(f"agent runtime refresh unavailable: {exc}")
            logger.warning("upgrade_user_runtime_refresh_skipped", error=str(exc))
        else:
            try:
                report.user_agent_skill_results = reinstall_skills_for_all(config=cfg)
            except Exception as exc:
                report.repair_warnings.append(f"agent skills refresh: {exc}")
                logger.warning("upgrade_user_skills_refresh_failed", error=str(exc))
            try:
                report.user_agent_cron_sync_results = sync_common_cron_jobs_for_all(config=cfg)
            except Exception as exc:
                report.repair_warnings.append(f"agent common-cron refresh: {exc}")
                logger.warning("upgrade_user_common_cron_refresh_failed", error=str(exc))

    # 5. Persist marker as a HIGH-WATERMARK — advance only, never move it
    #    backward. So a downgrade (e.g. beta 0.2.0 → stable 0.1.12, possible via
    #    an explicit `csflow upgrade`) keeps the higher marker; combined with the
    #    direction-safe migration ledger, a later re-upgrade won't re-run
    #    anything. We're lenient about #3/#4 errors so the marker still reflects
    #    "schema reached <target>".
    if not critical_migration_failed:
        if _gt(target, marker):
            try:
                write_marker(target)
                report.marker_written = True
            except Exception as exc:
                report.errors.append(f"write marker: {exc}")
                logger.exception("upgrade_marker_failed", error=str(exc))
        else:
            logger.info(
                "upgrade_marker_not_advanced",
                marker=marker, target=target,
                reason="target_not_greater_than_marker",
            )

    logger.info(
        "upgrade_complete",
        ok=report.ok,
        from_version=marker, to_version=target,
        migrations_run=report.migrations_run,
        openclaw=report.openclaw_status,
        marker_written=report.marker_written,
        errors=report.errors,
    )
    return report


# ──────────────────────────────────────────────────────────────────────
# Helper: should we auto-upgrade on `csflow start`?
# ──────────────────────────────────────────────────────────────────────


def needs_upgrade(target_version: str | None = None) -> tuple[bool, str | None]:
    """Decide whether ``csflow start`` should fire ``run_upgrade()``.

    Returns ``(needs_upgrade, current_marker)``. The marker is included
    so the caller can show "0.1.0 → 0.2.0" in its banner.

    Cases:
    * ``~/.clawsomeflow/`` does not exist → fresh first install; no upgrade.
    * Data dir exists but marker missing → legacy/unmarked install; upgrade.
    * Marker present but < target → upgrade.
    * Marker == target → no-op.
    * Marker > target → no-op + warn (downgrade is not auto-handled).
    """
    target = target_version or __version__
    if not paths.clawsomeflow_home_exists():
        return False, None
    marker = read_marker()
    if marker is None:
        return True, None
    if _gt(target, marker):
        return True, marker
    if _gt(marker, target):
        logger.warning(
            "upgrade_marker_newer_than_package",
            marker=marker, package=target,
        )
    return False, marker


def _discover_editable_frontend_dir() -> Path | None:
    """Return ``<repo>/frontend`` in editable source mode, else ``None``."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        frontend = ancestor / "frontend"
        if (frontend / "package.json").exists() and (frontend / "src").exists():
            return frontend
    return None


def _build_frontend_bundle_if_editable() -> tuple[str, str]:
    """Best-effort frontend build for source checkouts.

    Returns ``(status, detail)`` where status is one of:
      - ``rebuilt``
      - ``skipped-no-editable-frontend``
      - ``skipped-by-env``
      - ``failed``
    """
    if os.environ.get("CSFLOW_SKIP_FRONTEND_BUILD") in {"1", "true", "TRUE", "yes", "YES"}:
        return "skipped-by-env", "CSFLOW_SKIP_FRONTEND_BUILD is set"

    frontend = _discover_editable_frontend_dir()
    if frontend is None:
        return "skipped-no-editable-frontend", "no frontend source tree detected"

    npm = shutil.which("npm")
    if not npm:
        return "failed", "npm is required to build editable frontend but was not found in PATH"

    proc = subprocess.run(
        [npm, "--prefix", str(frontend), "run", "build"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        merged = (proc.stderr or proc.stdout or "").strip()
        detail = merged[-800:] if merged else f"exit code {proc.returncode}"
        return "failed", detail

    return "rebuilt", str(frontend / "dist")

__all__ = [
    "Migration",
    "MIGRATIONS",
    "UpgradeReport",
    "needs_upgrade",
    "read_marker",
    "run_upgrade",
    "write_marker",
]
