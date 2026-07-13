"""Tests for app.scheduler.run_metadata.run_is_unattended."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.scheduler.run_metadata import UNATTENDED_KEY, run_is_unattended


@dataclass
class _Run:
    is_scheduled: bool = False
    inputs: dict[str, Any] = field(default_factory=dict)


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
