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


def _backfill_is_temporary(cfg: Config) -> list[str] | None:
    """Repair: backfill ``FlowAgent.is_temporary`` on legacy Flow specs.

    Issue fixed: 0.1.13 added ``FlowAgent.is_temporary`` (default ``False``;
    see ``models.py``). Specs authored before 0.1.13 have no such key, so on load
    every non-OpenClaw agent becomes ``is_temporary=False`` = "managed/persistent".
    Consequences for an upgrade-only user:
      * Hermes — the editor's managed-agent picklist can't find the id (it isn't
        in the ``hermesagent`` table) → the leader dropdown renders blank; the
        runtime appends ``hermes -p <id>`` (``tmux_live.py``) → binds a
        non-existent profile.
      * claude/codex/cursor — these lost their persistent management platform in
        0.1.13, so a non-temporary value can bind a stale ``profile`` at spawn.

    This migration normalizes every agent in every stored Flow spec:
      * ``kind == "openclaw"``                       → untouched (never temporary)
      * registered Hermes (``hermes_get`` is not None) → ``is_temporary=False``
      * everything else non-OpenClaw                 → ``is_temporary=True``

    Target versions: specs written by ``<= 0.1.13b6`` (key absent or wrong).
    Removal baseline: safe to delete once the minimum upgrade baseline is raised
    past ``0.1.13b7``.

    Idempotent: only flows whose agents actually change value are re-saved; a
    second run is a no-op. Best-effort (``critical=False``): a per-flow failure is
    collected as a warning and never aborts the batch or the upgrade.
    """
    from app.storage import get_storage

    st = get_storage(cfg)

    # Collect ALL flows up front. flow_update bumps updated_at and flow_list
    # orders by updated_at desc, so mutating mid-pagination would reorder the
    # result window and risk revisiting/skipping rows.
    flows: list = []
    offset = 0
    page = 100
    while True:
        items, total = st.flow_list(owner_user=None, limit=page, offset=offset)
        flows.extend(items)
        offset += len(items)
        if not items or offset >= total:
            break

    warnings: list[str] = []
    repaired = 0
    for flow in flows:
        spec = flow.spec if isinstance(flow.spec, dict) else {}
        agents = spec.get("agents")
        if not isinstance(agents, list):
            continue
        changed = False
        for a in agents:
            if not isinstance(a, dict):
                continue
            kind = a.get("kind")
            if kind == "openclaw":
                continue
            aid = str(a.get("id") or "")
            registered_hermes = (
                kind == "hermes" and bool(aid) and st.hermes_get(aid) is not None
            )
            desired = not registered_hermes
            if a.get("is_temporary") != desired:
                a["is_temporary"] = desired
                changed = True
        if not changed:
            continue
        try:
            st.flow_update(flow, expected_version=flow.version)
            repaired += 1
        except Exception as exc:  # best-effort: one bad flow must not abort the rest
            warnings.append(f"could not repair flow {flow.id}: {exc}")
            logger.warning(
                "upgrade_is_temporary_repair_failed",
                flow_id=flow.id, error=str(exc),
            )

    logger.info("upgrade_is_temporary_backfill", flows=len(flows), repaired=repaired)
    return warnings or None


_DECOMPOSER_SKILL = "csflow-task-decomposer"


def _remove_task_decomposer_skill(cfg: Config) -> list[str] | None:
    """Repair: remove the deleted ``csflow-task-decomposer`` OpenClaw skill.

    Issue fixed: 0.1.15 deleted the ``csflow-task-decomposer`` skill — every
    decomposition instruction (including how to return the result) now lives in
    the dispatch prompt itself. The bundled mirror + per-workspace copies are
    normally pruned by the common-source sync (``_sync_exact_dir`` /
    ``_sync_common_workspace_skills``), but the per-workspace prune only fires
    for paths recorded in that workspace's common manifest, so a workspace
    created before the manifest existed would keep a stale
    ``skills/csflow-task-decomposer/``. This migration removes it
    unconditionally from every managed OpenClaw workspace AND the deployed
    common-agent-source mirror.

    Target versions: deployments that installed the skill at any point <= 0.1.14.
    Removal baseline: safe to delete once the minimum upgrade baseline is raised
    past ``0.1.15b1``.

    Idempotent: a missing directory is a no-op. Best-effort (``critical=False``):
    a per-target failure is collected as a warning and never aborts the upgrade.
    """
    import shutil as _shutil

    from app.storage import get_storage

    st = get_storage(cfg)
    warnings: list[str] = []
    removed = 0

    targets: list[Path] = [
        paths.common_agent_source_dir() / "skills" / _DECOMPOSER_SKILL,
    ]
    try:
        for agent in st.openclaw_list():
            ws = getattr(agent, "workspace_path", "") or ""
            base = Path(ws) if str(ws).strip() else (paths.agent_dir(agent.id) / "workspace")
            targets.append(base / "skills" / _DECOMPOSER_SKILL)
    except Exception as exc:  # best-effort: still prune the mirror below
        warnings.append(f"could not list managed agents: {exc}")
        logger.warning("upgrade_decomposer_skill_list_failed", error=str(exc))

    for target in targets:
        try:
            if target.is_dir():
                _shutil.rmtree(target)
                removed += 1
        except Exception as exc:  # one bad target must not abort the rest
            warnings.append(f"could not remove {target}: {exc}")
            logger.warning(
                "upgrade_decomposer_skill_remove_failed",
                path=str(target), error=str(exc),
            )

    logger.info(
        "upgrade_decomposer_skill_removed", removed=removed, candidates=len(targets),
    )
    return warnings or None


# Register migrations in chronological order. Newer entries come last.
# Each ``apply`` MUST be idempotent (re-runnable). A migration runs once ever,
# gated by the applied-migrations ledger (see module docstring).
MIGRATIONS: list[Migration] = [
    # 0.1.13b7: backfill FlowAgent.is_temporary on pre-0.1.13 specs (the key was
    # absent → loaded as managed, breaking the Hermes leader picklist + runtime
    # profile binding). Targets specs written by <= 0.1.13b6. Best-effort so a
    # single malformed flow never blocks the upgrade. See _backfill_is_temporary.
    Migration(
        version="0.1.13b7",
        description="backfill FlowAgent.is_temporary on legacy specs",
        apply=_backfill_is_temporary,
        critical=False,
    ),
    # 0.1.15b1: the csflow-task-decomposer OpenClaw skill was deleted (decompose
    # instructions are fully inline in the dispatch prompt now). Prune any stale
    # copy from managed agent workspaces + the deployed common-source mirror,
    # covering workspaces older than the common manifest. See
    # _remove_task_decomposer_skill.
    Migration(
        version="0.1.15b1",
        description="remove deleted csflow-task-decomposer skill from workspaces",
        apply=_remove_task_decomposer_skill,
        critical=False,
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

    # 0b. Managed Hermes profiles read only their own config (no global to
    # reference live), so backfill inference config into any profile that lacks
    # it from the operator's active/root profile. Idempotent + best-effort.
    try:
        from app.services.hermes_agents import backfill_hermes_inference_config

        seeded = backfill_hermes_inference_config(config=cfg)
        if seeded:
            logger.info("upgrade_hermes_inference_config_seeded", agents=seeded)
    except Exception as exc:  # pragma: no cover - defensive; never block upgrade
        report.repair_warnings.append(f"hermes inference config backfill: {exc}")
        logger.warning("upgrade_hermes_inference_backfill_failed", error=str(exc))

    # 0c. AI-decompose temporary-agent working directory. The AI decomposer
    # points every temporary agent it invents at ``~/csflow-ai-decompose``; that
    # dir must exist as a git repo with a commit before a run can build a worktree
    # from it. Create it idempotently on the upgrade path so upgrade-only users
    # converge with fresh deploys. Best-effort: never blocks the upgrade.
    try:
        from app.services.task_decompose import _ensure_ai_temp_agent_workdir

        _ensure_ai_temp_agent_workdir()
    except Exception as exc:  # pragma: no cover - defensive; never block upgrade
        report.repair_warnings.append(f"ai-decompose workdir init: {exc}")
        logger.warning("upgrade_ai_decompose_workdir_failed", error=str(exc))

    # 0d. opencode temporary agents: interactive auto-approval is config-only
    # (no CLI flag), so seed ``permission: allow`` into opencode's global config
    # when opencode is installed. Idempotent + non-destructive (never clobbers a
    # user-set ``permission``). On the upgrade path so upgrade-only users who use
    # opencode converge with fresh deploys.
    try:
        from app.integrations.opencode_config import ensure_opencode_permission_allow

        if ensure_opencode_permission_allow():
            logger.info("upgrade_opencode_permission_seeded")
    except Exception as exc:  # pragma: no cover - defensive; never block upgrade
        report.repair_warnings.append(f"opencode config init: {exc}")
        logger.warning("upgrade_opencode_config_failed", error=str(exc))

    # 0e. Qoder / CodeBuddy temporary agents gate startup on a per-folder trust
    # dialog that no flag skips; seed each CLI's global trust config (trustAll /
    # trustDirectories) so fresh ClawTeam worktrees don't hang on the prompt.
    # Idempotent + non-destructive + gated on the CLI being installed.
    try:
        from app.integrations.temp_agent_trust import (
            ensure_codebuddy_trust_all,
            ensure_qoder_trust_dirs,
        )

        if ensure_codebuddy_trust_all():
            logger.info("upgrade_codebuddy_trust_seeded")
        if ensure_qoder_trust_dirs():
            logger.info("upgrade_qoder_trust_seeded")
    except Exception as exc:  # pragma: no cover - defensive; never block upgrade
        report.repair_warnings.append(f"temp-agent trust config init: {exc}")
        logger.warning("upgrade_temp_agent_trust_failed", error=str(exc))

    # 0e2. ClawTeam spawn_ready_timeout (``~/.clawteam/config.json``). Stock 30s
    # makes ``_confirm_workspace_trust_if_prompted`` spin the full window when no
    # trust dialog is shown, holding repo locks unnecessarily. Lower to 2s on the
    # upgrade path so upgrade-only users converge (load_config reads the file, not
    # env). Best-effort: never blocks upgrade.
    try:
        from app.integrations.clawteam_spawn_config import ensure_spawn_ready_timeout

        if ensure_spawn_ready_timeout():
            logger.info("upgrade_clawteam_spawn_ready_timeout_set")
    except Exception as exc:  # pragma: no cover - defensive; never block upgrade
        report.repair_warnings.append(f"clawteam spawn_ready_timeout: {exc}")
        logger.warning("upgrade_clawteam_spawn_ready_timeout_failed", error=str(exc))

    # 0f. Global agent tools (``.clawsomeflow-agent-tools/``). These are NOT
    # OpenClaw-specific — every agent kind references them by absolute path (e.g.
    # the locked self-merge tool ``csflow-locked-merge.py``). Deploy here,
    # unconditionally and OpenClaw-independently, so upgrade-only users (incl.
    # those without OpenClaw, whose step-3 install is skipped) end up identical to
    # fresh deploys: the dir was previously created ONLY by the OpenClaw install,
    # so it was missing for TUI-only users. Idempotent (exact mirror).
    try:
        from app.integrations.openclaw_agent_source import deploy_agent_tools_bundle

        deploy_agent_tools_bundle()
        logger.info("upgrade_agent_tools_deployed")
    except Exception as exc:  # pragma: no cover - defensive; never block upgrade
        report.repair_warnings.append(f"agent tools deploy: {exc}")
        logger.warning("upgrade_agent_tools_deploy_failed", error=str(exc))

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
