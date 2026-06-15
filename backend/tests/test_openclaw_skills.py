"""Tests for app.integrations.openclaw_skills — skill installer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import paths
from app.integrations import openclaw_skills as sk


@pytest.fixture
def seeded_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Synthesise a fake skills-source tree under the isolated home."""
    src = paths.skills_source_dir()
    # Two fake skills mimicking the real layout.
    (src / "test-skill-a").mkdir(parents=True, exist_ok=True)
    (src / "test-skill-a" / "SKILL.md").write_text("--- name: A ---")
    (src / "nested-skill-pack" / "nested-skill").mkdir(parents=True, exist_ok=True)
    (src / "nested-skill-pack" / "nested-skill" / "SKILL.md").write_text(
        "--- name: B ---"
    )
    return src


def test_install_then_list(seeded_skills: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    inst = sk.SkillInstaller(workspace, source_root=seeded_skills)
    records = inst.install(("test-skill-a",))
    assert len(records) == 1
    assert (workspace / "skills" / "test-skill-a" / "SKILL.md").exists()
    listed = inst.list_installed()
    assert {r.name for r in listed} == {"test-skill-a"}


def test_install_uses_basename_for_nested_skills(
    seeded_skills: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    inst = sk.SkillInstaller(workspace, source_root=seeded_skills)
    inst.install(("nested-skill-pack/nested-skill",))
    # Installed under skills/<basename>/, NOT under skills/nested-skill-pack/...
    assert (workspace / "skills" / "nested-skill" / "SKILL.md").exists()
    assert not (workspace / "skills" / "nested-skill-pack").exists()


def test_install_missing_source_raises(seeded_skills: Path, tmp_path: Path) -> None:
    inst = sk.SkillInstaller(tmp_path / "ws", source_root=seeded_skills)
    with pytest.raises(FileNotFoundError):
        inst.install(("non-existent-skill",))


def test_install_overwrites_existing(seeded_skills: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    inst = sk.SkillInstaller(workspace, source_root=seeded_skills)
    inst.install(("test-skill-a",))
    # Mutate source, reinstall, verify update propagates.
    (seeded_skills / "test-skill-a" / "SKILL.md").write_text("--- name: A2 ---")
    inst.install(("test-skill-a",))
    text = (workspace / "skills" / "test-skill-a" / "SKILL.md").read_text()
    assert "A2" in text


def test_install_prunes_skills_removed_from_requested_set(
    seeded_skills: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    inst = sk.SkillInstaller(workspace, source_root=seeded_skills)
    inst.install(("test-skill-a", "nested-skill-pack/nested-skill"))
    inst.install(("test-skill-a",))
    assert not (workspace / "skills" / "nested-skill").exists()
    listed = inst.list_installed()
    assert {r.name for r in listed} == {"test-skill-a"}


def test_uninstall_single(seeded_skills: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    inst = sk.SkillInstaller(workspace, source_root=seeded_skills)
    inst.install(("test-skill-a",))
    assert inst.uninstall("test-skill-a") is True
    assert not (workspace / "skills" / "test-skill-a").exists()
    assert inst.uninstall("test-skill-a") is False  # idempotent


def test_uninstall_all_clears_manifest(seeded_skills: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    inst = sk.SkillInstaller(workspace, source_root=seeded_skills)
    inst.install(("test-skill-a", "nested-skill-pack/nested-skill"))
    removed = inst.uninstall_all()
    assert set(removed) == {"test-skill-a", "nested-skill"}
    # Skills dir should be cleaned (no skills, no manifest).
    if (workspace / "skills").exists():
        # Only allowed if non-empty for some other reason — manifest should be gone.
        assert not (workspace / "skills" / ".csflow-installed.json").exists()


def test_seed_skills_source_copies_bundled(tmp_path: Path) -> None:
    """seed_skills_source should populate the home skills-source from bundled source."""
    src = sk.bundled_skill_source_dir()
    if not src.exists():
        pytest.skip("bundled openclaw-agent-source/ not in this build")
    dst = sk.seed_skills_source()
    assert (dst / "self-definition-maintenance" / "SKILL.md").exists()


def test_seed_skills_source_prunes_stale_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "bundled-common-skills"
    (bundled / "new-skill").mkdir(parents=True, exist_ok=True)
    (bundled / "new-skill" / "SKILL.md").write_text("--- name: NEW ---", encoding="utf-8")
    monkeypatch.setattr(sk, "bundled_common_skills_dir", lambda: bundled)

    dst = paths.skills_source_dir()
    stale = dst / "stale-skill"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "SKILL.md").write_text("--- legacy ---", encoding="utf-8")

    sk.seed_skills_source()
    assert not stale.exists()
    assert (dst / "new-skill" / "SKILL.md").exists()


def test_discover_user_agent_skills_scans_directories_dynamically(tmp_path: Path) -> None:
    source_root = tmp_path / "skills"
    (source_root / "z-skill").mkdir(parents=True, exist_ok=True)
    (source_root / "z-skill" / "SKILL.md").write_text("---")
    (source_root / "a-skill").mkdir(parents=True, exist_ok=True)
    (source_root / "a-skill" / "SKILL.md").write_text("---")
    # Directory without SKILL.md should not be treated as installable skill.
    (source_root / "notes").mkdir(parents=True, exist_ok=True)

    discovered = sk.discover_user_agent_skills(source_root=source_root)
    assert discovered == ("a-skill", "z-skill")


def test_install_record_persisted_to_manifest(seeded_skills: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    inst = sk.SkillInstaller(workspace, source_root=seeded_skills)
    inst.install(("test-skill-a",))
    manifest = workspace / "skills" / ".csflow-installed.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    names = [item["name"] for item in data["skills"]]
    assert "test-skill-a" in names
