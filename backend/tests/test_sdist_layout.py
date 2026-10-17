"""Sdist member paths must already be canonical.

PyPI rejects an archive member when ``posixpath.normpath`` would change it.
``../`` destinations in the sdist force-include table become members like
``clawsomeflow-0.2.7/../API.md`` and the upload returns 400.
"""

from __future__ import annotations

from pathlib import Path

import tomllib

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_sdist_force_include_destinations_stay_inside_archive() -> None:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    mapping = data["tool"]["hatch"]["build"]["targets"]["sdist"]["force-include"]
    assert mapping, "sdist force-include must keep shipping the shared files"
    for source, dest in mapping.items():
        parts = Path(dest).parts
        assert ".." not in parts, f"{source!r} -> {dest!r} escapes the sdist root"
        assert not dest.startswith("/"), dest
        assert not dest.startswith("\\"), dest
