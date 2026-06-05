"""Tests for ``scripts/_bump_version.py`` (the release-flow version bumper).

Imports the script as a module so we can unit-test the pure
``Version`` class + check the CLI's I/O wrapper writes correctly to
test fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "_bump_version.py"

# ``_bump_version.py`` is private release tooling (gitignored). Skip this whole
# module wherever the script is absent — CI runners and contributor clones — so
# collection never crashes. It still runs locally for maintainers who have it.
if not SCRIPT.exists():
    pytest.skip(
        "scripts/_bump_version.py not present (private release tooling)",
        allow_module_level=True,
    )


def _load_module():
    spec = importlib.util.spec_from_file_location("bump_version", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bump_version"] = mod
    spec.loader.exec_module(mod)
    return mod


bump = _load_module()
Version = bump.Version


# ── Version parsing ───────────────────────────────────────────────────


@pytest.mark.parametrize("s,expected", [
    ("1.2.3", (1, 2, 3, None, None)),
    ("0.1.0", (0, 1, 0, None, None)),
    ("1.2.3b1", (1, 2, 3, "b", 1)),
    ("1.2.3rc4", (1, 2, 3, "rc", 4)),
    ("10.20.30", (10, 20, 30, None, None)),
])
def test_parse_valid(s, expected) -> None:
    v = Version.parse(s)
    assert (v.major, v.minor, v.patch, v.pre_kind, v.pre_num) == expected


@pytest.mark.parametrize("bad", ["1", "1.2", "1.2.3.4", "abc", "v1.2.3", ""])
def test_parse_invalid(bad) -> None:
    with pytest.raises(ValueError):
        Version.parse(bad)


def test_str_roundtrip() -> None:
    for s in ("0.1.0", "1.2.3b1", "1.2.3rc10"):
        assert str(Version.parse(s)) == s


# ── Bump semantics ────────────────────────────────────────────────────


@pytest.mark.parametrize("start,part,channel,expected", [
    # Plain release bumps from a release.
    ("0.1.0", "patch", "release", "0.1.1"),
    ("0.1.0", "minor", "release", "0.2.0"),
    ("0.1.0", "major", "release", "1.0.0"),
    # Beta bumps from a release.
    ("0.1.0", "minor", "beta", "0.2.0b1"),
    ("0.1.0", "patch", "rc", "0.1.1rc1"),
    # Same-channel bump on an already-prereleased version increments pre_num.
    ("0.2.0b1", "minor", "beta", "0.2.0b2"),
    ("0.2.0b3", "patch", "beta", "0.2.0b4"),  # patch on b → still bumps pre_num
    ("0.2.0rc2", "minor", "rc", "0.2.0rc3"),
    # Going to release from a prerelease finalises THIS X.Y.Z.
    ("0.2.0b3", "minor", "release", "0.2.0"),
    ("0.2.0rc1", "patch", "release", "0.2.0"),
    # Switching channel mid-prerelease starts the new kind at 1.
    ("0.2.0b3", "minor", "rc", "0.2.0rc1"),
])
def test_bump(start: str, part: str, channel: str, expected: str) -> None:
    v = Version.parse(start)
    out = v.bump(part, channel=channel)
    assert str(out) == expected


def test_bump_validates_part_and_channel() -> None:
    v = Version.parse("0.1.0")
    with pytest.raises(ValueError):
        v.bump("hotfix", channel="release")
    with pytest.raises(ValueError):
        v.bump("patch", channel="ga")


def test_is_prerelease() -> None:
    assert not Version.parse("1.0.0").is_prerelease
    assert Version.parse("1.0.0b1").is_prerelease
    assert Version.parse("1.0.0rc4").is_prerelease


# ── I/O against repo fixtures ─────────────────────────────────────────


def test_real_repo_files_consistent() -> None:
    """Sanity: ssot / pyproject / package.json all agree."""
    ok, snap = bump.check_consistent()
    assert ok, f"version drift: {snap}"


def test_write_everywhere_round_trip(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """``write_everywhere`` mutates the live tree — point the helper at
    a temp copy so tests stay hermetic. We rebind the module-level
    ``SSOT`` / ``PYPROJECT`` / ``FRONTEND_PKG`` globals."""
    src = REPO
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "backend" / "app").mkdir(parents=True)
    (mirror / "backend" / "app" / "__init__.py").write_text(
        '__version__ = "0.0.1"\n', encoding="utf-8",
    )
    (mirror / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.0.1"\n', encoding="utf-8",
    )
    (mirror / "frontend").mkdir()
    (mirror / "frontend" / "package.json").write_text(
        json.dumps({"name": "x", "version": "0.0.1"}, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bump, "SSOT", mirror / "backend/app/__init__.py")
    monkeypatch.setattr(bump, "PYPROJECT", mirror / "backend/pyproject.toml")
    monkeypatch.setattr(bump, "FRONTEND_PKG", mirror / "frontend/package.json")

    bump.write_everywhere(Version.parse("9.8.7b3"))
    assert '__version__ = "9.8.7b3"' in (
        mirror / "backend/app/__init__.py"
    ).read_text()
    assert 'version = "9.8.7b3"' in (
        mirror / "backend/pyproject.toml"
    ).read_text()
    assert json.loads(
        (mirror / "frontend/package.json").read_text()
    )["version"] == "9.8.7b3"

    ok, snap = bump.check_consistent()
    assert ok, snap
    assert snap["ssot"] == "9.8.7b3"
