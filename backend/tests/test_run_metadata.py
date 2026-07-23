"""Tests for app.scheduler.run_metadata.run_is_unattended."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.scheduler.run_metadata import (
    CHECKPOINT_STATE_KEY,
    REVERTED_MERGE_AGENT_IDS_KEY,
    UNATTENDED_KEY,
    clear_checkpoint_state,
    coalesce_reverted_merge_markers,
    read_checkpoint_state,
    run_is_unattended,
    write_checkpoint_state,
)


@dataclass
class _Run:
    id: str = "run-x"
    is_scheduled: bool = False
    inputs: dict[str, Any] = field(default_factory=dict)


class _FakeStorage:
    def __init__(self, db_run: _Run | None) -> None:
        self._db_run = db_run

    def run_get(self, run_id: str) -> _Run | None:
        if self._db_run is None or self._db_run.id != run_id:
            return None
        return self._db_run


def test_unattended_false_by_default() -> None:
    assert run_is_unattended(_Run()) is False
    assert run_is_unattended(_Run(inputs={"goal": "x"})) is False


def test_unattended_true_for_scheduled() -> None:
    assert run_is_unattended(_Run(is_scheduled=True)) is True


def test_unattended_true_for_marker() -> None:
    assert run_is_unattended(_Run(inputs={UNATTENDED_KEY: "true"})) is True
    assert run_is_unattended(_Run(inputs={UNATTENDED_KEY: "TRUE"})) is True


def test_unattended_marker_only_true_string() -> None:
    assert run_is_unattended(_Run(inputs={UNATTENDED_KEY: "false"})) is False
    assert run_is_unattended(_Run(inputs={UNATTENDED_KEY: ""})) is False


def test_unattended_marker_key_is_internal_prefixed() -> None:
    # Must ride under the _csflow_ prefix so _public_run_inputs strips it.
    assert UNATTENDED_KEY.startswith("_csflow_")


def test_unattended_tolerates_missing_attrs() -> None:
    class _Bare:
        pass

    assert run_is_unattended(_Bare()) is False


def test_coalesce_reverted_merge_markers_unions_db_into_stale_run() -> None:
    stale = _Run(id="run-1", inputs={"goal": "x"})
    db = _Run(
        id="run-1",
        inputs={REVERTED_MERGE_AGENT_IDS_KEY: ["alice", "bob"]},
    )
    coalesce_reverted_merge_markers(stale, _FakeStorage(db))
    assert stale.inputs[REVERTED_MERGE_AGENT_IDS_KEY] == ["alice", "bob"]
    assert stale.inputs["goal"] == "x"


def test_coalesce_reverted_merge_markers_unions_both_sides() -> None:
    stale = _Run(
        id="run-1",
        inputs={REVERTED_MERGE_AGENT_IDS_KEY: ["alice"]},
    )
    db = _Run(
        id="run-1",
        inputs={REVERTED_MERGE_AGENT_IDS_KEY: ["bob"]},
    )
    coalesce_reverted_merge_markers(stale, _FakeStorage(db))
    assert stale.inputs[REVERTED_MERGE_AGENT_IDS_KEY] == ["alice", "bob"]


def test_coalesce_reverted_merge_markers_noop_when_absent() -> None:
    stale = _Run(id="run-1", inputs={"goal": "x"})
    coalesce_reverted_merge_markers(stale, _FakeStorage(_Run(id="run-1")))
    assert stale.inputs == {"goal": "x"}


def test_checkpoint_state_roundtrip() -> None:
    run = _Run(inputs={"goal": "x"})
    write_checkpoint_state(
        run, passed={"t1", "t2"}, summaries={"t1": "approved", "t2": None},
    )
    assert CHECKPOINT_STATE_KEY in run.inputs
    assert run.inputs["goal"] == "x"  # existing inputs preserved
    passed, summaries = read_checkpoint_state(run)
    assert passed == {"t1", "t2"}
    assert summaries == {"t1": "approved", "t2": None}
    clear_checkpoint_state(run)
    assert CHECKPOINT_STATE_KEY not in run.inputs
    assert read_checkpoint_state(run) == (set(), {})


def test_checkpoint_state_absent_safe_default() -> None:
    # Old runs without the marker → empty, never raises (upgrade-safe default).
    assert read_checkpoint_state(_Run()) == (set(), {})
    assert read_checkpoint_state(_Run(inputs={"goal": "x"})) == (set(), {})


def test_checkpoint_state_marker_key_is_internal_prefixed() -> None:
    # Must ride under the _csflow_ prefix so _public_run_inputs strips it.
    assert CHECKPOINT_STATE_KEY.startswith("_csflow_")


def test_checkpoint_state_write_noop_when_empty() -> None:
    # Nothing to persist → don't pollute inputs with an empty marker.
    run = _Run(inputs={"goal": "x"})
    write_checkpoint_state(run, passed=set(), summaries={})
    assert CHECKPOINT_STATE_KEY not in run.inputs
