"""Tests for app.scheduler.run_metadata.run_is_unattended."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.scheduler.run_metadata import (
    REVERTED_MERGE_AGENT_IDS_KEY,
    UNATTENDED_KEY,
    coalesce_reverted_merge_markers,
    run_is_unattended,
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
