"""Tests for ``app.upgrade`` — the version marker + migration runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import paths, upgrade
from app.config import Config


# ── version marker round-trip ─────────────────────────────────────────


def test_marker_missing_when_dir_fresh(tmp_clawsomeflow_home: Path) -> None:
    assert upgrade.read_marker() is None


def test_write_then_read_marker(tmp_clawsomeflow_home: Path) -> None:
    upgrade.write_marker("1.2.3")
    assert upgrade.read_marker() == "1.2.3"
    assert paths.version_marker_path().read_text(encoding="utf-8") == "1.2.3\n"


def test_write_then_read_pre_release(tmp_clawsomeflow_home: Path) -> None:
    upgrade.write_marker("1.2.3b4")
    assert upgrade.read_marker() == "1.2.3b4"


def test_write_marker_rejects_garbage(tmp_clawsomeflow_home: Path) -> None:
    for bad in ["", "v1.2.3", "abc", "1.2", "1.2.3.4", "1.2.3-beta"]:
        with pytest.raises(ValueError):
            upgrade.write_marker(bad)


def test_read_marker_returns_none_for_corrupt_value(tmp_clawsomeflow_home: Path) -> None:
    paths.version_marker_path().write_text("not-a-version\n", encoding="utf-8")
    assert upgrade.read_marker() is None


# ── version comparison (PEP 440 ordering for our subset) ──────────────


@pytest.mark.parametrize("a,b", [
    ("1.2.4", "1.2.3"),
    ("1.3.0", "1.2.99"),
    ("2.0.0", "1.999.999"),
    ("1.2.3", "1.2.3b1"),     # final beats pre-release
    ("1.2.3", "1.2.3rc99"),
    ("1.2.3rc1", "1.2.3b99"),
    ("1.2.3b2", "1.2.3b1"),
    ("1.2.3a99", "1.2.2"),    # pre-release of newer ranks above old release
])
def test_gt_ordering(a: str, b: str) -> None:
    assert upgrade._gt(a, b)
    assert not upgrade._gt(b, a)
    assert not upgrade._gt(a, a)


def test_gt_none_handling() -> None:
    assert upgrade._gt("1.0.0", None) is True
    assert upgrade._gt(None, "1.0.0") is False
    assert upgrade._gt(None, None) is False


# ── needs_upgrade decision matrix ─────────────────────────────────────


def test_needs_upgrade_fresh_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ~/.clawsomeflow dir yet => treat as first install (no upgrade)."""
    home = tmp_path / "never-created-home"
    monkeypatch.setenv(paths.CSFLOW_HOME_ENV, str(home))
    needs, marker = upgrade.needs_upgrade(target_version="1.0.0")
    assert needs is False
    assert marker is None


def test_needs_upgrade_legacy_install(tmp_clawsomeflow_home: Path) -> None:
    """Data dir exists + no marker = legacy/unmarked install needs upgrade."""
    needs, marker = upgrade.needs_upgrade(target_version="1.0.0")
    assert needs is True
    assert marker is None


def test_needs_upgrade_stale_marker(tmp_clawsomeflow_home: Path) -> None:
    upgrade.write_marker("1.0.0")
    needs, marker = upgrade.needs_upgrade(target_version="1.1.0")
    assert needs is True
    assert marker == "1.0.0"


def test_needs_upgrade_current_marker(tmp_clawsomeflow_home: Path) -> None:
    upgrade.write_marker("1.0.0")
    needs, marker = upgrade.needs_upgrade(target_version="1.0.0")
    assert needs is False
    assert marker == "1.0.0"


def test_needs_upgrade_marker_newer_than_package(
    tmp_clawsomeflow_home: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    upgrade.write_marker("9.9.9")
    needs, marker = upgrade.needs_upgrade(target_version="1.0.0")
    assert needs is False
    assert marker == "9.9.9"


# ── Migration registry application semantics ──────────────────────────


def test_applies_skipped_when_marker_at_target() -> None:
    """Already migrated to this version → don't re-run."""
    m = upgrade.Migration("1.5.0", "test", apply=lambda c: None)
    assert upgrade._applies(m, "1.5.0") is False
    assert upgrade._applies(m, "1.6.0") is False
    assert upgrade._applies(m, "2.0.0") is False


def test_applies_when_marker_older_than_target() -> None:
    m = upgrade.Migration("1.5.0", "test", apply=lambda c: None)
    assert upgrade._applies(m, "1.4.0") is True
    assert upgrade._applies(m, "1.0.0") is True
    assert upgrade._applies(m, None) is True   # legacy → apply everything


def test_applies_lower_bound_inclusive() -> None:
    """``applies_after`` is INCLUSIVE: marker == applies_after → still applies."""
    m = upgrade.Migration(
        "2.0.0", "test", apply=lambda c: None,
        applies_after="1.5.0",
    )
    assert upgrade._applies(m, "1.5.0") is True   # exactly at lower bound
    assert upgrade._applies(m, "1.6.0") is True
    assert upgrade._applies(m, "1.4.9") is False  # below lower bound → defer
    assert upgrade._applies(m, None) is True       # legacy → bypass lower bound


def test_applies_combo_pre_release_ordering() -> None:
    """Pre-release marker behaves correctly across boundaries."""
    m = upgrade.Migration(
        "2.0.0", "test", apply=lambda c: None,
        applies_after="1.5.0",
    )
    assert upgrade._applies(m, "1.5.0b3") is False   # 1.5.0b3 < 1.5.0 → too early
    assert upgrade._applies(m, "2.0.0b1") is True    # past 1.5.0, before 2.0.0
    assert upgrade._applies(m, "2.0.0") is False     # already there


# ── run_upgrade end-to-end ────────────────────────────────────────────


def test_run_upgrade_unmarked_home_writes_marker(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    """First-time upgrade: no marker → marker == target after run."""
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)

    report = upgrade.run_upgrade(config=fake_config, target_version="1.2.3")
    assert report.ok is True
    assert report.from_version is None
    assert report.to_version == "1.2.3"
    assert report.marker_written is True
    assert upgrade.read_marker() == "1.2.3"
    assert report.is_first_install is False
    assert report.redeploy_performed is True


def test_run_upgrade_skips_old_migrations(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    """Migrations whose target is at-or-below the marker get skipped."""
    upgrade.write_marker("2.0.0")

    calls: list[str] = []
    monkeypatch.setattr(upgrade, "MIGRATIONS", [
        upgrade.Migration(
            "1.5.0", "ancient",
            apply=lambda c: calls.append("ancient"),
            applies_after="1.0.0",
        ),
        upgrade.Migration(
            "2.5.0", "current",
            apply=lambda c: calls.append("current"),
            applies_after="2.0.0",
        ),
    ])
    _disable_external_calls(monkeypatch)

    report = upgrade.run_upgrade(config=fake_config, target_version="2.5.0")
    assert report.ok is True
    assert report.migrations_run == ["2.5.0"]
    assert calls == ["current"]
    assert upgrade.read_marker() == "2.5.0"


def test_run_upgrade_migration_failure_keeps_marker(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    """If a migration raises, marker is NOT bumped — next run will retry."""
    upgrade.write_marker("1.0.0")

    def boom(c: Config) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(upgrade, "MIGRATIONS", [
        upgrade.Migration("2.0.0", "boom", apply=boom),
    ])
    _disable_external_calls(monkeypatch)

    report = upgrade.run_upgrade(config=fake_config, target_version="2.0.0")
    assert report.ok is False
    assert any("disk full" in e for e in report.errors)
    assert report.marker_written is False
    assert upgrade.read_marker() == "1.0.0"   # unchanged → safe to retry


def test_run_upgrade_idempotent_no_op(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    upgrade.write_marker("1.0.0")
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)

    report = upgrade.run_upgrade(config=fake_config, target_version="1.0.0")
    assert report.ok is True
    assert report.is_no_op is True
    assert report.migrations_run == []
    assert upgrade.read_marker() == "1.0.0"


def test_run_upgrade_can_skip_openclaw_and_user_skill_refresh(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)

    report = upgrade.run_upgrade(
        config=fake_config,
        target_version="1.0.0",
        include_openclaw=False,
        include_user_agent_skill_refresh=False,
    )

    assert report.ok is True
    assert report.openclaw_status == "skipped-by-flag"
    assert report.skills_reseeded is False
    assert report.user_agent_skill_results == {}
    assert report.user_agent_cron_sync_results == {}


def test_run_upgrade_detects_first_install_by_missing_home_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    fresh_home = tmp_path / "fresh-home-not-created-yet"
    monkeypatch.setenv(paths.CSFLOW_HOME_ENV, str(fresh_home))
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)

    report = upgrade.run_upgrade(config=fake_config, target_version="1.0.0")

    assert report.ok is True
    assert report.is_first_install is True


def test_run_upgrade_reinstalls_skills_without_auto_restoring_runtime_agents(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)
    calls: list[str] = []

    def _fake_reinstall(*_a, **_kw):
        calls.append("reinstall")
        return {"a1": ["skill-x"]}

    def _fake_sync_common_cron(*_a, **_kw):
        calls.append("sync-common-cron")
        return {"a1": True}

    monkeypatch.setattr(
        "app.services.openclaw_agents.reinstall_skills_for_all",
        _fake_reinstall,
    )
    monkeypatch.setattr(
        "app.services.openclaw_agents.sync_common_cron_jobs_for_all",
        _fake_sync_common_cron,
    )

    report = upgrade.run_upgrade(config=fake_config, target_version="1.0.0")
    assert report.ok is True
    assert calls == ["reinstall", "sync-common-cron"]
    assert report.user_agent_skill_results == {"a1": ["skill-x"]}
    assert report.user_agent_cron_sync_results == {"a1": True}


def test_run_upgrade_still_syncs_common_cron_when_skill_refresh_fails(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)
    calls: list[str] = []

    def _fake_reinstall(*_a, **_kw):
        calls.append("reinstall")
        raise RuntimeError("skills broken")

    def _fake_sync_common_cron(*_a, **_kw):
        calls.append("sync-common-cron")
        return {"a1": True}

    monkeypatch.setattr(
        "app.services.openclaw_agents.reinstall_skills_for_all",
        _fake_reinstall,
    )
    monkeypatch.setattr(
        "app.services.openclaw_agents.sync_common_cron_jobs_for_all",
        _fake_sync_common_cron,
    )

    report = upgrade.run_upgrade(config=fake_config, target_version="1.0.0")
    assert report.ok is True
    assert calls == ["reinstall", "sync-common-cron"]
    assert report.user_agent_skill_results == {}
    assert report.user_agent_cron_sync_results == {"a1": True}


def test_run_upgrade_treats_missing_openclaw_as_non_fatal(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)

    async def _missing_openclaw(*_a, **_kw):
        raise FileNotFoundError("openclaw not found")

    monkeypatch.setattr(
        "app.integrations.openclaw_install.install_into_openclaw",
        _missing_openclaw,
    )

    report = upgrade.run_upgrade(config=fake_config, target_version="1.0.0")
    assert report.ok is True
    assert report.openclaw_status == "not-configured"
    assert report.errors == []


# ── helpers / fixtures ────────────────────────────────────────────────


def _disable_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the heavy side-effects we don't want to exercise here:
    storage init, OpenClaw install, per-agent skill refresh."""
    # storage
    import app.storage as st
    monkeypatch.setattr(st, "get_storage", lambda *_a, **_kw: object())
    # openclaw install
    from types import SimpleNamespace
    fake_install = SimpleNamespace(gateway_chat_endpoint_enabled=True)

    async def _noop_install(*_a, **_kw):
        return fake_install

    monkeypatch.setattr(
        "app.integrations.openclaw_install.install_into_openclaw",
        _noop_install,
    )
    # user-agent skill refresh
    monkeypatch.setattr(
        "app.services.openclaw_agents.reinstall_skills_for_all",
        lambda *_a, **_kw: {},
    )
    monkeypatch.setattr(
        "app.services.openclaw_agents.sync_common_cron_jobs_for_all",
        lambda *_a, **_kw: {},
    )


@pytest.fixture
def tmp_clawsomeflow_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    home = tmp_path / "csflow-home"
    home.mkdir()
    monkeypatch.setenv(paths.CSFLOW_HOME_ENV, str(home))
    return home


@pytest.fixture
def fake_config(tmp_clawsomeflow_home: Path) -> Config:
    """Minimal valid Config — no on-disk side effects beyond what tests opt into."""
    cfg = Config()
    return cfg


def test_run_upgrade_generates_api_token_for_pretoken_config(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_config: Config,
) -> None:
    """Upgrade-only users (pre-token config) must end up with an api_token,
    independent of the OpenClaw step (here disabled + stubbed)."""
    from app.config import load_config

    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    _disable_external_calls(monkeypatch)
    assert fake_config.api_token is None

    report = upgrade.run_upgrade(
        config=fake_config,
        target_version="1.0.0",
        include_openclaw=False,            # prove independence from openclaw step
        include_user_agent_skill_refresh=False,
    )
    assert report.ok is True

    # Persisted to the private config.json and visible via load_config.
    token1 = load_config(force_reload=True).api_token
    assert token1 and isinstance(token1, str)
    # Also got the internal HMAC secret (same step).
    assert load_config(force_reload=True).internal_token_secret

    # Idempotent: a second upgrade (loading the now-persisted config) keeps it.
    upgrade.run_upgrade(
        target_version="1.0.0",
        include_openclaw=False,
        include_user_agent_skill_refresh=False,
    )
    assert load_config(force_reload=True).api_token == token1


def test_run_upgrade_creates_hermes_agent_table(
    tmp_clawsomeflow_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade-parity: the HermesAgent table is created on the upgrade path so an
    upgrade-only user reaches the same schema as a fresh deploy."""
    from app.models import HermesAgent
    from app.storage import get_storage

    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    upgrade.run_upgrade(
        target_version="1.0.0",
        include_openclaw=False,
        include_user_agent_skill_refresh=False,
    )
    storage = get_storage()
    # Tables are queryable (empty) and accept a row → schema was created.
    assert storage.hermes_list() == []
    storage.hermes_create(
        HermesAgent(id="probe", name="P", profile_root="x", created_by_user="alice")
    )
    assert storage.hermes_get("probe") is not None


# ── stable ↔ beta switching: ledger gate + high-watermark marker ──────


def _counting_migration(version: str, counter: dict) -> "upgrade.Migration":
    def _apply(_cfg):  # noqa: ANN001
        counter[version] = counter.get(version, 0) + 1
    return upgrade.Migration(version, f"count {version}", apply=_apply)


def _run(target: str, fake_config: Config):
    return upgrade.run_upgrade(
        target_version=target,
        include_openclaw=False,
        include_user_agent_skill_refresh=False,
    )


def test_seed_applied_from_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade, "MIGRATIONS", [
        upgrade.Migration("0.1.5", "a", apply=lambda c: None),
        upgrade.Migration("0.1.12", "b", apply=lambda c: None),
    ])
    assert upgrade._seed_applied_from_marker("0.1.11") == {"0.1.5"}
    assert upgrade._seed_applied_from_marker(None) == set()
    assert upgrade._seed_applied_from_marker("0.1.12") == {"0.1.5", "0.1.12"}


def test_duplicate_migration_ids_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade, "MIGRATIONS", [
        upgrade.Migration("0.1.12", "a", apply=lambda c: None),
        upgrade.Migration("0.1.12", "b", apply=lambda c: None),
    ])
    with pytest.raises(ValueError):
        upgrade._assert_unique_migration_ids()


def test_migration_runs_once_across_beta_to_stable(
    tmp_clawsomeflow_home: Path, monkeypatch: pytest.MonkeyPatch, fake_config: Config,
) -> None:
    """A 0.1.12-targeted migration must run exactly once across b1→b2→final."""
    counter: dict = {}
    monkeypatch.setattr(upgrade, "MIGRATIONS", [_counting_migration("0.1.12", counter)])
    upgrade.write_marker("0.1.11")  # user starts on prior stable
    for target in ["0.1.12b1", "0.1.12b2", "0.1.12"]:
        _run(target, fake_config)
    assert counter.get("0.1.12") == 1            # exactly once, not 3×
    assert upgrade.read_marker() == "0.1.12"     # advanced to final
    assert "0.1.12" in (upgrade.read_applied_migrations() or set())


def test_marker_is_high_watermark_no_downgrade(
    tmp_clawsomeflow_home: Path, monkeypatch: pytest.MonkeyPatch, fake_config: Config,
) -> None:
    monkeypatch.setattr(upgrade, "MIGRATIONS", [])
    upgrade.write_marker("0.2.0")
    report = _run("0.1.12", fake_config)   # downgrade target
    assert upgrade.read_marker() == "0.2.0"   # not moved backward
    assert report.marker_written is False


def test_fresh_seed_prevents_migration_rerun(
    tmp_clawsomeflow_home: Path, monkeypatch: pytest.MonkeyPatch, fake_config: Config,
) -> None:
    counter: dict = {}
    monkeypatch.setattr(upgrade, "MIGRATIONS", [_counting_migration("0.1.12", counter)])
    upgrade.write_marker("0.1.12")
    upgrade.seed_fresh_migration_ledger()   # fresh install marks all applied
    _run("0.1.13", fake_config)             # later upgrade
    assert counter.get("0.1.12", 0) == 0    # fresh-seeded migration never re-runs


# ── resilience: a failing repair never aborts the whole upgrade ────────


def test_best_effort_repair_failure_does_not_abort_or_fail(
    tmp_clawsomeflow_home: Path, monkeypatch: pytest.MonkeyPatch, fake_config: Config,
) -> None:
    """A non-critical (best-effort) migration that raises is reported as a
    warning, the upgrade stays ok, schema still initialises (service usable),
    and the marker still advances."""
    def boom_repair(_cfg):  # noqa: ANN001
        raise RuntimeError("clawteam offline")

    monkeypatch.setattr(upgrade, "MIGRATIONS", [
        upgrade.Migration("0.1.12", "best-effort", apply=boom_repair, critical=False),
    ])
    upgrade.write_marker("0.1.11")
    report = _run("0.1.12", fake_config)
    assert report.ok is True                              # NOT failed
    assert report.errors == []
    assert any("clawteam offline" in w for w in report.repair_warnings)
    assert report.schema_ready is True                   # service usable
    assert upgrade.read_marker() == "0.1.12"             # marker advanced
    # marked applied → won't re-fail forever
    assert "0.1.12" in (upgrade.read_applied_migrations() or set())


def test_repair_returns_warnings_surfaced(
    tmp_clawsomeflow_home: Path, monkeypatch: pytest.MonkeyPatch, fake_config: Config,
) -> None:
    """A best-effort migration may RETURN a list of non-fatal failure messages
    which are surfaced to the report (→ terminal)."""
    monkeypatch.setattr(upgrade, "MIGRATIONS", [
        upgrade.Migration(
            "0.1.12", "partial",
            apply=lambda c: ["could not provision agent 'x'"],
            critical=False,
        ),
    ])
    upgrade.write_marker("0.1.11")
    report = _run("0.1.12", fake_config)
    assert report.ok is True
    assert "could not provision agent 'x'" in report.repair_warnings


def test_critical_migration_failure_still_runs_schema_keeps_marker(
    tmp_clawsomeflow_home: Path, monkeypatch: pytest.MonkeyPatch, fake_config: Config,
) -> None:
    """A critical migration failure blocks the marker (retry) but does NOT abort:
    schema init still runs so the service can start."""
    def boom(_cfg):  # noqa: ANN001
        raise RuntimeError("bad schema step")

    monkeypatch.setattr(upgrade, "MIGRATIONS", [
        upgrade.Migration("0.1.12", "critical", apply=boom, critical=True),
    ])
    upgrade.write_marker("0.1.11")
    report = _run("0.1.12", fake_config)
    assert report.ok is False                        # surfaced as error
    assert report.schema_ready is True               # but service still usable
    assert report.marker_written is False
    assert upgrade.read_marker() == "0.1.11"         # retried next run
    assert "0.1.12" not in (upgrade.read_applied_migrations() or set())


