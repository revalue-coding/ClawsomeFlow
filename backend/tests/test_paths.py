"""Tests for :mod:`app.paths`."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import paths


class TestValidateIdentifier:
    def test_accepts_valid(self) -> None:
        for v in ["foo", "foo-bar_baz.qux", "abc123", "a"]:
            assert paths.validate_identifier(v) == v

    @pytest.mark.parametrize(
        "value",
        ["", ".", "..", "foo/bar", "../etc", "foo bar", "中文"],
    )
    def test_rejects_invalid(self, value: str) -> None:
        with pytest.raises(ValueError):
            paths.validate_identifier(value)

    def test_allow_empty_flag(self) -> None:
        assert paths.validate_identifier("", allow_empty=True) == ""


class TestEnsureWithinRoot:
    def test_safe_join(self, tmp_path: Path) -> None:
        result = paths.ensure_within_root(tmp_path, "child", "leaf")
        assert result == tmp_path / "child" / "leaf"

    def test_rejects_escape(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="escapes"):
            paths.ensure_within_root(tmp_path, "..", "etc")


class TestHomeAndAccessors:
    def test_home_uses_env_var(self, tmp_path: Path) -> None:
        # The conftest fixture has already set CSFLOW_HOME to <tmp>/csflow_home.
        home = paths.clawsomeflow_home()
        assert home.is_dir()
        assert "csflow_home" in str(home)

    def test_subdirs_are_idempotent(self) -> None:
        # Calling each accessor twice should not raise / should return the same path.
        for fn in (
            paths.flows_dir,
            paths.runs_dir,
            paths.agents_dir,
            paths.system_dir,
            paths.skills_source_dir,
            paths.logs_dir,
        ):
            p1 = fn()
            p2 = fn()
            assert p1 == p2 and p1.is_dir()

    def test_run_dir_validates_id(self) -> None:
        with pytest.raises(ValueError):
            paths.run_dir("../escape")

    def test_agent_dir_validates_id(self) -> None:
        with pytest.raises(ValueError):
            paths.agent_dir("foo/bar")
