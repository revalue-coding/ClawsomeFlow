"""Tests for :mod:`app.bootstrap`."""

from __future__ import annotations

from app import bootstrap, paths


def test_ensure_data_layout_creates_all_dirs() -> None:
    home = bootstrap.ensure_data_layout()
    assert home.is_dir()
    for sub in (".flows", ".runs", "agents", ".system", ".skills-source", ".logs"):
        assert (home / sub).is_dir(), f"{sub} not created"


def test_summary_initial_state() -> None:
    bootstrap.ensure_data_layout()
    snap = bootstrap.bootstrap_summary()
    assert snap.config_present is False  # config only created on load_config
    assert snap.db_present is False
    assert snap.flows_count == 0
    assert snap.runs_count == 0
    assert snap.agents_count == 0


def test_summary_reflects_files_added() -> None:
    bootstrap.ensure_data_layout()
    (paths.flows_dir() / "f1.json").write_text("{}")
    (paths.flows_dir() / "f2.json").write_text("{}")
    (paths.runs_dir() / "r1").mkdir()
    snap = bootstrap.bootstrap_summary()
    assert snap.flows_count == 2
    assert snap.runs_count == 1


def test_idempotent() -> None:
    bootstrap.ensure_data_layout()
    bootstrap.ensure_data_layout()
    bootstrap.ensure_data_layout()
    # No exception, all dirs still exist.
    assert paths.flows_dir().is_dir()
