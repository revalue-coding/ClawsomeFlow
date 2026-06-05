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

Design:
* Single source of truth for the data-dir's "last-blessed version" is
  ``~/.clawsomeflow/.csflow-version`` (a one-line text file). Read /
  write via :func:`read_marker` / :func:`write_marker`.
* Migrations are a versioned, ordered registry (:data:`MIGRATIONS`).
  Each migration declares the version range it applies to and a
  callable that takes a :class:`Config`. We apply every migration
  whose ``applies_after`` < marker ≤ ``applies_through``, in order.
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
    should be a safe no-op).
    """
    version: str
    description: str
    apply: Callable[[Config], None]
    applies_after: str | None = None


def _applies(m: Migration, marker: str | None) -> bool:
    # Already at or past this migration's target version → skip.
    if marker is not None and not _gt(m.version, marker):
        return False
    # Lower bound check (only when we know the marker).
    if marker is not None and m.applies_after is not None:
        if _gt(m.applies_after, marker):  # applies_after > marker → too early
            return False
    return True


# Register migrations in chronological order. Newer entries come last.
# Currently empty — 0.1.0 is the first published version, so there's
# nothing to migrate from. Future schema changes append a Migration
# here whose ``apply`` is an idempotent (re-runnable) callable.
MIGRATIONS: list[Migration] = []


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
            report.errors.append(f"frontend build: {detail}")
            return report
    else:
        report.frontend_build_status = "not-requested"

    # 1. Migrations.
    for m in MIGRATIONS:
        if _applies(m, marker):
            try:
                m.apply(cfg)
                report.migrations_run.append(m.version)
                logger.info(
                    "upgrade_migration_applied",
                    version=m.version, description=m.description,
                )
            except Exception as exc:
                report.errors.append(f"migration {m.version}: {exc}")
                logger.exception(
                    "upgrade_migration_failed",
                    version=m.version, error=str(exc),
                )
                return report  # Don't bump marker on migration failure.

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
            logger.warning("upgrade_user_runtime_refresh_skipped", error=str(exc))
        else:
            try:
                report.user_agent_skill_results = reinstall_skills_for_all(config=cfg)
            except Exception as exc:
                logger.warning("upgrade_user_skills_refresh_failed", error=str(exc))
            try:
                report.user_agent_cron_sync_results = sync_common_cron_jobs_for_all(config=cfg)
            except Exception as exc:
                logger.warning("upgrade_user_common_cron_refresh_failed", error=str(exc))

    # 5. Persist marker — only if we got past migrations. We're lenient
    #    about #3/#4 errors so the marker reflects "schema is at <target>".
    if not any(e.startswith("migration ") for e in report.errors):
        try:
            write_marker(target)
            report.marker_written = True
        except Exception as exc:
            report.errors.append(f"write marker: {exc}")
            logger.exception("upgrade_marker_failed", error=str(exc))

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
