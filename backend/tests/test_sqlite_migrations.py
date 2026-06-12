"""Idempotent additive-column migrations in ``SqliteStorage.init_schema``.

Covers the upgrade-parity invariant: an upgrade-only user whose DB predates a
new column must converge to the same schema as a fresh deploy, with old rows
loading under safe defaults.
"""

from __future__ import annotations

from pathlib import Path

from app.models import Flow, FlowRun, RunStatus
from app.storage.sqlite import SqliteStorage


def _legacy_flowrun_table(url: str) -> None:
    """Create a ``flowrun`` table WITHOUT the ``is_scheduled`` column and seed a
    row, mimicking a DB created before the scheduled-run flag existed."""
    store = SqliteStorage(url=url)
    with store._engine.begin() as conn:  # noqa: SLF001 - test reaches into engine
        conn.exec_driver_sql(
            "CREATE TABLE flowrun ("
            "id VARCHAR PRIMARY KEY, flow_id VARCHAR, flow_version INTEGER, "
            "team_name VARCHAR, status VARCHAR, inputs JSON, user VARCHAR, "
            "started_at DATETIME, finished_at DATETIME, pending_merges JSON)"
        )
        conn.exec_driver_sql(
            "INSERT INTO flowrun "
            "(id, flow_id, flow_version, team_name, status, inputs, user, started_at) "
            "VALUES ('run-legacy', 'flow-1', 1, 'csflow-legacy', 'completed', "
            "'{}', 'alice', '2026-01-01T00:00:00+00:00')"
        )
    store.close()


def test_flowrun_is_scheduled_column_added_on_init(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    _legacy_flowrun_table(url)

    store = SqliteStorage(url=url)
    store.init_schema()

    with store._engine.begin() as conn:  # noqa: SLF001
        cols = {str(r[1]) for r in conn.exec_driver_sql("PRAGMA table_info('flowrun')")}
    assert "is_scheduled" in cols

    # Legacy row reads back with the safe default (False).
    legacy = store.run_get("run-legacy")
    assert legacy is not None
    assert legacy.is_scheduled is False

    # Idempotent: a second init must not raise (column already present).
    store.init_schema()
    store.close()


def test_flowrun_is_scheduled_round_trip(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    store = SqliteStorage(url=url)
    store.init_schema()

    flow = store.flow_create(Flow(
        id="flow-1", name="t", description="", owner_user="alice", spec={},
    ))
    run = store.run_create(FlowRun(
        id="run-sched", flow_id=flow.id, flow_version=1,
        team_name="csflow-sched", status=RunStatus.pending,
        inputs={}, user="alice", is_scheduled=True,
    ))
    assert run.is_scheduled is True
    assert store.run_get("run-sched").is_scheduled is True
    store.close()
