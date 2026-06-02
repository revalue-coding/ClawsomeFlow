"""Skill installer for OpenClaw agents.

OpenClaw skills are stored per agent under workspace ``skills/``.
ClawsomeFlow ships common skills in ``openclaw-agent-source/common-agent-source``
and projects them into ``~/.clawsomeflow/.skills-source/`` on init/upgrade.

This module ONLY touches paths inside ClawsomeFlow's own data directory.
"""

from __future__ import annotations

import importlib.resources
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from app import paths
from app.fileutil import atomic_write_json
from app.logging_setup import get_logger

logger = get_logger("openclaw_skills")


# ──────────────────────────────────────────────────────────────────────
# Standard skill packs
# ──────────────────────────────────────────────────────────────────────

_COMMON_SKILLS_REL = "common-agent-source/skills"
_SKILL_ENTRY_FILENAME = "SKILL.md"


# ──────────────────────────────────────────────────────────────────────
# Source location (bundled vs user-writable)
# ──────────────────────────────────────────────────────────────────────


def bundled_skill_source_dir() -> Path:
    """Path to bundled ``openclaw-agent-source/`` directory shipped with repo.

    Resolves repo-relative for editable installs (``backend/../openclaw-agent-source``)
    and falls back to an importlib-resources package path when installed as a
    wheel/sdist.
    """
    here = Path(__file__).resolve()
    repo_candidate = here.parents[3] / "openclaw-agent-source"
    if repo_candidate.exists():
        return repo_candidate
    try:
        return Path(importlib.resources.files("clawsomeflow_openclaw_agent_source"))
    except ModuleNotFoundError:
        raise RuntimeError(
            "Cannot locate bundled openclaw-agent-source/ — neither "
            f"{repo_candidate} nor packaged "
            "'clawsomeflow_openclaw_agent_source' resource exists"
        )


def bundled_common_skills_dir() -> Path:
    """Directory containing built-in common skills in bundled source tree."""
    common_dir = bundled_skill_source_dir() / _COMMON_SKILLS_REL
    if not common_dir.exists() or not common_dir.is_dir():
        raise FileNotFoundError(f"Common skill source not found: {common_dir}")
    return common_dir


def discover_user_agent_skills(*, source_root: Path | None = None) -> tuple[str, ...]:
    """Discover all installable common skills dynamically by directory scan.

    A directory is treated as one skill iff it is a direct child and contains
    ``SKILL.md``.
    """
    root = source_root or bundled_common_skills_dir()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Skill source root not found: {root}")

    skills: list[str] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if not (child / _SKILL_ENTRY_FILENAME).is_file():
            logger.warning(
                "skill_source_skipped_missing_skill_md",
                skill_dir=str(child),
                expected_file=_SKILL_ENTRY_FILENAME,
            )
            continue
        skills.append(child.name)

    if not skills:
        raise FileNotFoundError(f"No installable skills found under: {root}")
    return tuple(skills)


# Backward-compatible snapshot for modules/tests still importing this symbol.
# Runtime injection logic should call ``discover_user_agent_skills`` directly.
try:
    USER_AGENT_SKILLS = discover_user_agent_skills()
except (FileNotFoundError, RuntimeError):
    USER_AGENT_SKILLS = ()


def seed_skills_source() -> Path:
    """Mirror bundled common skills into ``~/.clawsomeflow/.skills-source/``."""
    dst = paths.skills_source_dir()
    common_src = bundled_common_skills_dir()
    _sync_exact_dir(common_src, dst)
    logger.info("skills_source_seeded", src=str(common_src), dst=str(dst))
    return dst


# ──────────────────────────────────────────────────────────────────────
# Installer
# ──────────────────────────────────────────────────────────────────────


_MANIFEST_FILE = ".csflow-installed.json"


@dataclass(frozen=True)
class InstalledSkill:
    """A record of one skill installed at a workspace."""

    name: str            # logical id (e.g. "csflow-task-decomposer")
    relative_path: str   # source path relative to .skills-source root
    installed_at: str    # absolute install path under workspace/skills/


class SkillInstaller:
    """Install / uninstall ClawsomeFlow skills into an OpenClaw workspace.

    The installer never touches files outside ``workspace_dir/skills/`` and
    keeps a manifest at ``workspace_dir/skills/.csflow-installed.json`` so
    uninstall removes only what we added.

    Usage::

        SkillInstaller(workspace).install(USER_AGENT_SKILLS)
        SkillInstaller(workspace).uninstall_all()
    """

    def __init__(self, workspace_dir: Path, *, source_root: Path | None = None) -> None:
        self.workspace_dir = workspace_dir
        self.skills_dir = workspace_dir / "skills"
        self.source_root = source_root or paths.skills_source_dir()

    def install(self, skills: tuple[str, ...]) -> list[InstalledSkill]:
        """Install each named skill. Returns the records (also persisted)."""
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        installed: list[InstalledSkill] = [
            rec for rec in self._load_manifest() if Path(rec.installed_at).exists()
        ]
        desired_names = {Path(rel).name for rel in skills}
        existing_names = {rec.name for rec in installed}

        for rel in skills:
            src = self.source_root / rel
            if not src.exists() or not src.is_dir():
                raise FileNotFoundError(
                    f"Skill source not found: {src} "
                    "(did you call seed_skills_source()?)"
                )
            name = src.name
            target = self.skills_dir / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
            record = InstalledSkill(
                name=name,
                relative_path=rel,
                installed_at=str(target),
            )
            if name in existing_names:
                installed = [r for r in installed if r.name != name]
            installed.append(record)
            existing_names.add(name)
            logger.info(
                "skill_installed",
                workspace=str(self.workspace_dir),
                skill=name,
                source=str(src),
            )

        stale_records = [rec for rec in installed if rec.name not in desired_names]
        for rec in stale_records:
            target = Path(rec.installed_at)
            _remove_path(target)
            logger.info(
                "skill_removed",
                workspace=str(self.workspace_dir),
                skill=rec.name,
                reason="not_in_requested_skill_set",
            )
        installed = [rec for rec in installed if rec.name in desired_names]
        self._save_manifest(installed)
        return installed

    def uninstall(self, skill_name: str) -> bool:
        """Remove a single skill. Returns True if it was present."""
        installed = self._load_manifest()
        target_record = next((r for r in installed if r.name == skill_name), None)
        if target_record is None:
            return False
        target = Path(target_record.installed_at)
        if target.exists():
            shutil.rmtree(target)
        new_manifest = [r for r in installed if r.name != skill_name]
        self._save_manifest(new_manifest)
        logger.info(
            "skill_uninstalled",
            workspace=str(self.workspace_dir),
            skill=skill_name,
        )
        return True

    def uninstall_all(self) -> list[str]:
        """Remove every skill we ever installed here. Returns names removed."""
        installed = self._load_manifest()
        removed: list[str] = []
        for record in installed:
            target = Path(record.installed_at)
            if target.exists():
                shutil.rmtree(target)
            removed.append(record.name)
        if (self.skills_dir / _MANIFEST_FILE).exists():
            (self.skills_dir / _MANIFEST_FILE).unlink()
        if self.skills_dir.exists() and not any(self.skills_dir.iterdir()):
            self.skills_dir.rmdir()
        logger.info(
            "skills_uninstalled_all",
            workspace=str(self.workspace_dir),
            count=len(removed),
        )
        return removed

    def list_installed(self) -> list[InstalledSkill]:
        """Return the current manifest contents."""
        return self._load_manifest()

    # ── internals ────────────────────────────────────────────────────

    def _manifest_path(self) -> Path:
        return self.skills_dir / _MANIFEST_FILE

    def _load_manifest(self) -> list[InstalledSkill]:
        path = self._manifest_path()
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [InstalledSkill(**item) for item in data.get("skills", [])]

    def _save_manifest(self, records: list[InstalledSkill]) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self._manifest_path(),
            {"skills": [rec.__dict__ for rec in records]},
        )


def _sync_exact_file(src: Path, dst: Path) -> None:
    if not src.exists() or src.is_dir():
        _remove_path(dst)
        return
    if dst.exists() and dst.is_dir():
        _remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _sync_exact_dir(src: Path, dst: Path) -> None:
    """Mirror *src* to *dst* exactly (prune stale paths)."""
    if not src.exists() or not src.is_dir():
        _remove_path(dst)
        return
    if dst.exists() and not dst.is_dir():
        _remove_path(dst)
    dst.mkdir(parents=True, exist_ok=True)

    src_children = {p.name: p for p in src.iterdir()}
    for child in list(dst.iterdir()):
        if child.name not in src_children:
            _remove_path(child)

    for name, src_child in src_children.items():
        dst_child = dst / name
        if src_child.is_dir():
            _sync_exact_dir(src_child, dst_child)
        else:
            _sync_exact_file(src_child, dst_child)


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


__all__ = [
    "USER_AGENT_SKILLS",
    "discover_user_agent_skills",
    "InstalledSkill",
    "SkillInstaller",
    "bundled_skill_source_dir",
    "bundled_common_skills_dir",
    "seed_skills_source",
]
