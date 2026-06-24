"""Shared pytest fixtures.

Every test gets an isolated ``CSFLOW_HOME`` (tmp directory) so they never
touch the developer's real ``~/.clawsomeflow/``. Module-level singletons
(config cache / lock manager / logging setup) are reset per test so the
file-based logger re-targets the new tmp home.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Ensure tests always import the repository-local backend package (`backend/app`)
# instead of an older globally installed `app` package when pytest is launched
# from the monorepo root.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app import config as cfg_mod
from app import logging_setup, paths
from app.concurrency import reset_lock_manager
from app.storage import reset_storage


def _reset_all_singletons() -> None:
    cfg_mod.reset_config_cache()
    reset_lock_manager()
    reset_storage()
    # Phase 5+6: scheduler engine, worktree lookup; Phase 7: event broadcaster.
    from app.events import reset_event_broadcaster
    from app.scheduler.engine import reset_scheduler
    from app.services.run_schedules import reset_run_schedule_worker
    from app.worktree.lookup import reset_worktree_lookup
    reset_scheduler()
    reset_run_schedule_worker()
    reset_worktree_lookup()
    reset_event_broadcaster()
    # Create/cancel coordination state is held in module-global sets/dicts that
    # outlive a single test. Tests that call ``commit_agent`` directly leak the
    # agent id into ``_CREATE_IN_PROGRESS`` (the API layer, not the service,
    # releases it), so reusing the id in a later test would otherwise be rejected
    # with "a create for X is already in progress". Clear them between tests.
    from app.services import hermes_agents as _hermes_svc
    from app.services import openclaw_agents as _oc_svc
    _oc_svc._CREATE_IN_PROGRESS.clear()
    _oc_svc._CANCELLED_CREATES.clear()
    _hermes_svc._CREATES_IN_FLIGHT.clear()
    try:
        from app.api import openclaw_agents as _oc_api
        _oc_api._AGENT_CREATE_TASKS.clear()
        _oc_api._PENDING_AGENT_CREATE_CANCELLATIONS.clear()
        _oc_api._REQUESTED_AGENT_CREATE_CANCELLATIONS.clear()
    except Exception:
        pass
    logging_setup._configured = False
    # Drop existing logging handlers so the next configure_logging call
    # reattaches to the (new) tmp logs directory.
    logging.root.handlers.clear()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``CSFLOW_HOME`` at a per-test tmp directory + reset singletons.

    Also disables Phase 9's auto-spawn of ``clawteam board serve`` so tests
    don't fork subprocesses they don't await.
    """
    home = tmp_path / "csflow_home"
    monkeypatch.setenv(paths.CSFLOW_HOME_ENV, str(home))
    monkeypatch.setenv("CSFLOW_DISABLE_BOARD", "1")
    monkeypatch.setenv("CSFLOW_DISABLE_CLAWTEAM_STACK_CHECK", "1")
    monkeypatch.setenv("CSFLOW_DISABLE_RUN_SCHEDULE_WORKER", "1")
    # The startup orphan sweep flips any ACTIVE_DRIVING run to ``orphaned`` on
    # app start; disabled by default so it doesn't reconcile fixtures created
    # before a TestClient lifespan boots. Sweep tests opt back in explicitly.
    monkeypatch.setenv("CSFLOW_DISABLE_ORPHAN_SWEEP", "1")
    # Don't fork real `openclaw sessions tail/list` progress followers in tests.
    monkeypatch.setenv("CSFLOW_DISABLE_OPENCLAW_CHAT_FOLLOWER", "1")
    _reset_all_singletons()
    yield home
    _reset_all_singletons()
