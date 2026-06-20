"""Deploy bundled agent source artifacts into ``~/.clawsomeflow/``.

Repository layout (source of truth):

    clawsomeflow-agent-tools/...        # GLOBAL tools — top-level, NOT openclaw-only
    openclaw-agent-source/
      common-agent-source/
        agent-common-rules.md
        skills/...

Runtime mapping under ``~/.clawsomeflow/``:

* ``clawsomeflow-agent-tools`` -> ``.clawsomeflow-agent-tools/`` (deployed
  unconditionally at init/upgrade — every agent kind, incl. temporary TUI
  agents, references these global scripts by absolute path)
* ``common-agent-source``       -> ``.common-agent-source/``

This intentionally avoids mirroring the whole source tree into one hidden
directory: each source area is mapped to its runtime destination explicitly.

Sync policy on redeploy/upgrade:

* common-agent workspace payload: only common skills are synchronized into each
  managed agent workspace; ``AGENTS.md`` only updates the common-rules section
  and preserves user custom section content.
* global tools bundle: exact mirror replacement (stale runtime files removed).
"""

from __future__ import annotations

import importlib.resources
import json
import re
import shutil
from pathlib import Path

from app import paths
from app.logging_setup import get_logger

logger = get_logger("openclaw_agent_source")


TOOLS_SOURCE_DIRNAME = "clawsomeflow-agent-tools"
TOOLS_PACKAGE_NAME = "clawsomeflow_agent_tools"
COMMON_SOURCE_REL = "common-agent-source"
COMMON_RULES_FILE = "agent-common-rules.md"
COMMON_SKILLS_SUBDIR = "skills"
AGENTS_USER_CUSTOM_SECTION_START = "<!-- AGENTS_USER_CUSTOM_SECTION_START -->"
AGENTS_USER_CUSTOM_SECTION_END = "<!-- AGENTS_USER_CUSTOM_SECTION_END -->"
_COMMON_MANIFEST_FILE = ".csflow-common-managed.json"
_AGENTS_CUSTOM_START_RE = re.compile(r"<!--\s*AGENTS_USER_CUSTOM_SECTION_START\s*-->")
_AGENTS_CUSTOM_END_RE = re.compile(r"<!--\s*AGENTS_USER_CUSTOM_SECTION_END\s*-->")


def bundled_agent_source_dir() -> Path:
    """Locate the bundled ``openclaw-agent-source/`` tree."""
    here = Path(__file__).resolve()
    repo_candidate = here.parents[3] / "openclaw-agent-source"
    if repo_candidate.exists():
        return repo_candidate
    try:
        return Path(importlib.resources.files("clawsomeflow_openclaw_agent_source"))
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Cannot locate bundled openclaw-agent-source/. "
            f"Tried {repo_candidate} and packaged "
            "'clawsomeflow_openclaw_agent_source'."
        ) from exc


def bundled_agent_tools_source_dir() -> Path:
    """Locate the bundled top-level ``clawsomeflow-agent-tools/`` tree.

    Lives at the repo root (NOT under ``openclaw-agent-source/``) because these
    tools are global, not OpenClaw-specific. Resolves to the repo checkout in
    dev, or the packaged ``clawsomeflow_agent_tools`` resource in a wheel.
    """
    here = Path(__file__).resolve()
    repo_candidate = here.parents[3] / TOOLS_SOURCE_DIRNAME
    if repo_candidate.exists():
        return repo_candidate
    try:
        return Path(importlib.resources.files(TOOLS_PACKAGE_NAME))
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Cannot locate bundled {TOOLS_SOURCE_DIRNAME}/. "
            f"Tried {repo_candidate} and packaged '{TOOLS_PACKAGE_NAME}'."
        ) from exc


def deploy_agent_tools_bundle() -> Path:
    """Sync global tools into ``~/.clawsomeflow/.clawsomeflow-agent-tools/``."""
    src = bundled_agent_tools_source_dir()
    if not src.is_dir():
        raise FileNotFoundError(f"agent tools source directory not found: {src}")
    dst = paths.openclaw_agent_tools_dir()
    _sync_exact_dir(src, dst)
    logger.info("openclaw_agent_tools_deployed", src=str(src), dst=str(dst))
    return dst


def bundled_common_rules_path() -> Path:
    """Return the bundled ``agent-common-rules.md`` (authoritative source)."""
    rules = _source_subdir(COMMON_SOURCE_REL) / COMMON_RULES_FILE
    if not rules.is_file():
        raise FileNotFoundError(f"Common rules file not found: {rules}")
    return rules


def load_common_rules_text() -> str:
    """Load non-empty common rules from the bundled source tree."""
    text = bundled_common_rules_path().read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("common rules content is empty")
    return text


def deploy_common_agent_source() -> Path:
    """Sync common managed-agent source into ``~/.clawsomeflow/.common-agent-source/``."""
    src = _source_subdir(COMMON_SOURCE_REL)
    dst = paths.common_agent_source_dir()
    _sync_exact_dir(src, dst)
    # Keep runtime mirror aligned with the bundled rules file we actually deploy.
    load_common_rules_text()
    logger.info("common_agent_source_deployed", src=str(src), dst=str(dst))
    return dst


def deploy_common_agent_workspace(
    workspace_dir: Path,
    *,
    overwrite_agents_md: bool = False,
) -> None:
    """Deploy common agent rules + common skills into one runtime workspace.

    AGENTS behavior:
    - Always refresh the latest common-rules section (outside custom area).
    - Preserve user custom content between
      ``AGENTS_USER_CUSTOM_SECTION_START/END`` markers.
    - If markers are missing on a legacy file, preserve the legacy body by
      folding it into the custom section instead of dropping it.
    """
    common_root = deploy_common_agent_source()
    skills_src = common_root / COMMON_SKILLS_SUBDIR
    if not skills_src.exists() or not skills_src.is_dir():
        raise FileNotFoundError(f"Common skills directory not found: {skills_src}")

    workspace_dir.mkdir(parents=True, exist_ok=True)
    agents_md = workspace_dir / "AGENTS.md"
    common_rules = load_common_rules_text()
    existing = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    merged = _merge_agents_md(
        common_rules=common_rules,
        existing_agents_md=existing,
    )
    if overwrite_agents_md or not agents_md.exists() or existing != merged:
        agents_md.write_text(merged, encoding="utf-8")

    _sync_common_workspace_skills(skills_src, workspace_dir)
    _ensure_workspace_env_file(workspace_dir)
    logger.info(
        "common_agent_workspace_deployed",
        workspace=str(workspace_dir),
        common_root=str(common_root),
        overwrite_agents_md=overwrite_agents_md,
    )


def _source_subdir(relative: str) -> Path:
    root = bundled_agent_source_dir()
    p = root / relative
    if not p.exists() or not p.is_dir():
        raise FileNotFoundError(f"OpenClaw source directory not found: {p}")
    return p


def _overlay_tree(src: Path, dst: Path) -> None:
    """Copy ``src`` tree into ``dst`` (overwrite known files, preserve unknown)."""
    if not src.exists():
        raise FileNotFoundError(src)
    for child in src.rglob("*"):
        rel = child.relative_to(src)
        target = dst / rel
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, target)


def _sync_common_workspace_skills(skills_src: Path, workspace_dir: Path) -> None:
    """Sync only common skills into one agent workspace.

    This function intentionally does NOT sync/remove root-level docs. User
    workspace definitions are user-owned and must remain untouched by upgrade.
    """
    manifest_path = workspace_dir / _COMMON_MANIFEST_FILE
    previous_managed = _load_common_manifest(manifest_path)

    mappings: dict[str, Path] = {}
    for child in skills_src.iterdir():
        if not child.is_dir():
            continue
        mappings[f"{COMMON_SKILLS_SUBDIR}/{child.name}"] = child

    new_managed = set(mappings.keys())
    stale_managed = previous_managed - new_managed
    for rel in sorted(stale_managed):
        # Only prune legacy managed skill paths. Never touch non-skill entries
        # from old manifests so user-owned docs/files remain untouched.
        if not rel.startswith(f"{COMMON_SKILLS_SUBDIR}/"):
            continue
        _remove_path(workspace_dir / rel)

    for rel, src_child in mappings.items():
        dst_child = workspace_dir / rel
        _sync_exact_dir(src_child, dst_child)

    _save_common_manifest(manifest_path, new_managed)


def _sync_exact_file(src: Path, dst: Path) -> None:
    """Sync one file exactly. Missing source removes destination."""
    if not src.exists() or src.is_dir():
        _remove_path(dst)
        return
    if dst.exists() and dst.is_dir():
        _remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _sync_exact_dir(src: Path, dst: Path) -> None:
    """Mirror *src* directory to *dst* exactly (remove stale destination paths)."""
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


def _load_common_manifest(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    raw = data.get("managed_paths", [])
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item:
            out.add(item)
    return out


def _save_common_manifest(path: Path, managed_paths: set[str]) -> None:
    path.write_text(
        json.dumps({"managed_paths": sorted(managed_paths)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _merge_agents_md(*, common_rules: str, existing_agents_md: str) -> str:
    """Render AGENTS with latest common rules while preserving custom section."""
    common = common_rules.strip()
    if not common:
        raise ValueError("common rules content is empty")

    custom = _extract_custom_section(existing_agents_md)
    if custom is None:
        custom = _legacy_custom_section_body(existing_agents_md)
    custom = custom.strip() or _default_custom_section_body()

    return (
        f"{common}\n\n"
        f"{AGENTS_USER_CUSTOM_SECTION_START}\n"
        f"{custom}\n"
        f"{AGENTS_USER_CUSTOM_SECTION_END}\n"
    )


def _extract_custom_section(content: str) -> str | None:
    if not content:
        return None
    start_m = _AGENTS_CUSTOM_START_RE.search(content)
    if start_m is None:
        return None
    end_m = _AGENTS_CUSTOM_END_RE.search(content, start_m.end())
    if end_m is None:
        return None
    return content[start_m.end():end_m.start()].strip()


def _default_custom_section_body() -> str:
    return (
        "## AGENTS_USER_CUSTOM_SECTION\n\n"
        "- Add user-defined personalized rules here.\n"
        "- Re-deploy and upgrade only update shared rules outside this section."
    )


def _legacy_custom_section_body(existing_agents_md: str) -> str:
    """Preserve pre-marker AGENTS content by folding it into custom section."""
    raw = (existing_agents_md or "").strip()
    if not raw:
        return _default_custom_section_body()
    return (
        "## AGENTS_USER_CUSTOM_SECTION\n\n"
        "The content below comes from a pre-marker AGENTS.md and was auto-preserved:\n\n"
        f"{raw}"
    )


def _ensure_workspace_env_file(workspace_dir: Path) -> None:
    (workspace_dir / "my-desktop").mkdir(parents=True, exist_ok=True)
    env_path = workspace_dir / ".env"
    if env_path.exists():
        return
    env_path.write_text(
        "# ClawsomeFlow managed agent environment variables.\n"
        "# Add custom KEY=VALUE pairs below.\n",
        encoding="utf-8",
    )


__all__ = [
    "COMMON_RULES_FILE",
    "COMMON_SKILLS_SUBDIR",
    "COMMON_SOURCE_REL",
    "TOOLS_PACKAGE_NAME",
    "TOOLS_SOURCE_DIRNAME",
    "bundled_agent_source_dir",
    "bundled_agent_tools_source_dir",
    "bundled_common_rules_path",
    "deploy_agent_tools_bundle",
    "deploy_common_agent_source",
    "deploy_common_agent_workspace",
    "load_common_rules_text",
]

