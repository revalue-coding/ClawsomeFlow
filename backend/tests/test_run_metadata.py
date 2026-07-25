"""Tests for app.scheduler.run_metadata.run_is_unattended."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.scheduler.run_metadata import (
    CHECKPOINT_STATE_KEY,
    FAILURE_GUIDANCE_KEY,
    FAILURE_GUIDANCE_MAX_CHARS,
    PAUSE_REASON_DRAIN,
    PAUSE_REASON_FAILURE,
    PAUSE_REASON_USER,
    PAUSE_STATE_KEY,
    REVERTED_MERGE_AGENT_IDS_KEY,
    UNATTENDED_KEY,
    clear_checkpoint_state,
    clear_failure_guidance,
    coalesce_reverted_merge_markers,
    pause_reason_outranks,
    read_checkpoint_state,
    read_failure_guidance,
    run_is_unattended,
    write_checkpoint_state,
    write_failure_guidance,
    write_pause_state,
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


def test_failure_guidance_roundtrip_and_clear() -> None:
    run = _Run(inputs={"goal": "x"})
    assert write_failure_guidance(run, task_id="t1", text="  use the v2 API  ") is True
    assert run.inputs["goal"] == "x"  # existing inputs preserved
    assert read_failure_guidance(run) == ("t1", "use the v2 API")
    clear_failure_guidance(run)
    assert FAILURE_GUIDANCE_KEY not in run.inputs
    assert read_failure_guidance(run) is None
    clear_failure_guidance(run)  # idempotent


def test_failure_guidance_is_single_slot() -> None:
    # A second guidance replaces the first — the staging area never accumulates.
    run = _Run()
    write_failure_guidance(run, task_id="t1", text="first")
    write_failure_guidance(run, task_id="t2", text="second")
    assert read_failure_guidance(run) == ("t2", "second")


def test_failure_guidance_needs_task_and_text() -> None:
    run = _Run()
    assert write_failure_guidance(run, task_id="", text="hi") is False
    assert write_failure_guidance(run, task_id="t1", text="   ") is False
    assert run.inputs == {}


def test_failure_guidance_text_is_capped() -> None:
    run = _Run()
    write_failure_guidance(run, task_id="t1", text="x" * (FAILURE_GUIDANCE_MAX_CHARS + 50))
    staged = read_failure_guidance(run)
    assert staged is not None
    assert len(staged[1]) == FAILURE_GUIDANCE_MAX_CHARS


def test_failure_guidance_absent_safe_default() -> None:
    # Old runs without the marker → None, never raises (upgrade-safe default).
    assert read_failure_guidance(_Run()) is None
    assert read_failure_guidance(_Run(inputs={FAILURE_GUIDANCE_KEY: "junk"})) is None


def test_failure_guidance_marker_key_is_internal_prefixed() -> None:
    # Must ride under the _csflow_ prefix so _public_run_inputs strips it.
    assert FAILURE_GUIDANCE_KEY.startswith("_csflow_")


def test_pause_reason_outranks_drain_is_weakest() -> None:
    assert pause_reason_outranks(PAUSE_REASON_USER, PAUSE_REASON_DRAIN)
    assert pause_reason_outranks(PAUSE_REASON_FAILURE, PAUSE_REASON_DRAIN)
    assert pause_reason_outranks(PAUSE_REASON_FAILURE, PAUSE_REASON_USER)
    assert not pause_reason_outranks(PAUSE_REASON_DRAIN, PAUSE_REASON_USER)
    assert not pause_reason_outranks(PAUSE_REASON_USER, PAUSE_REASON_FAILURE)


def test_write_pause_state_refuses_to_downgrade_user_to_drain() -> None:
    run = _Run(inputs={"goal": "x"})
    write_pause_state(run, reason=PAUSE_REASON_USER, detail="user requested pause")
    write_pause_state(
        run, reason=PAUSE_REASON_DRAIN, detail="service stop / upgrade drain",
    )
    blob = run.inputs[PAUSE_STATE_KEY]
    assert blob["reason"] == PAUSE_REASON_USER
    assert blob["detail"] == "user requested pause"
