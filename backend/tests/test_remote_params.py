"""Unit tests for the remote-node parameter hand-off parsers (controller)."""

from __future__ import annotations

from app.scheduler.controller import (
    _extract_first_json_object,
    _extract_remote_params_block,
)
from app.scheduler.prompts import REMOTE_PARAMS_HEADER


def test_extract_first_json_object_plain() -> None:
    assert _extract_first_json_object('{"a": "1", "b": "2"}') == {"a": "1", "b": "2"}


def test_extract_first_json_object_with_prose_and_fences() -> None:
    text = 'Sure, here it is:\n```json\n{"x": "hello"}\n```\nthanks'
    assert _extract_first_json_object(text) == {"x": "hello"}


def test_extract_first_json_object_nested_and_braces_in_strings() -> None:
    text = 'noise {"outer": {"inner": "a}b"}} trailing'
    assert _extract_first_json_object(text) == {"outer": {"inner": "a}b"}}


def test_extract_first_json_object_none_when_absent() -> None:
    assert _extract_first_json_object("no json here") is None
    assert _extract_first_json_object("") is None


def test_extract_remote_params_block_matches_header_and_task_id() -> None:
    text = (
        f"{REMOTE_PARAMS_HEADER}: t-upstream\n"
        '{"需求描述": "抓取周报", "目标目录": ""}'
    )
    parsed = _extract_remote_params_block(text, "t-upstream")
    assert parsed == {"需求描述": "抓取周报", "目标目录": ""}


def test_extract_remote_params_block_prefers_per_downstream_header() -> None:
    text = (
        f"{REMOTE_PARAMS_HEADER}: t-up remote-a\n"
        '{"fa": "1"}\n'
        f"{REMOTE_PARAMS_HEADER}: t-up remote-b\n"
        '{"fb": "2"}'
    )
    assert _extract_remote_params_block(
        text, "t-up", downstream_task_id="remote-a",
    ) == {"fa": "1"}
    assert _extract_remote_params_block(
        text, "t-up", downstream_task_id="remote-b",
    ) == {"fb": "2"}


def test_extract_remote_params_block_legacy_fallback_for_downstream() -> None:
    """Old single-block form still works when asking for a specific downstream."""
    text = f'{REMOTE_PARAMS_HEADER}: t-up\n{{"shared": "v"}}'
    assert _extract_remote_params_block(
        text, "t-up", downstream_task_id="remote-a",
    ) == {"shared": "v"}


def test_extract_remote_params_block_ignores_other_task_id() -> None:
    text = f"{REMOTE_PARAMS_HEADER}: other\n{{\"a\": \"1\"}}"
    assert _extract_remote_params_block(text, "t-upstream") is None


def test_extract_remote_params_block_absent_returns_none() -> None:
    assert _extract_remote_params_block("task t1 done: did the thing", "t1") is None


def test_extract_remote_params_block_present_but_bad_json_returns_empty() -> None:
    text = f"{REMOTE_PARAMS_HEADER}: t1 (no json object follows)"
    assert _extract_remote_params_block(text, "t1") == {}


def test_extract_remote_params_block_null_values_become_empty_string() -> None:
    text = f'{REMOTE_PARAMS_HEADER}: t1\n{{"a": null, "b": "v"}}'
    assert _extract_remote_params_block(text, "t1") == {"a": "", "b": "v"}
