"""Tests for app.integrations.openclaw_agent_source."""

from __future__ import annotations

from pathlib import Path

from app import paths
from app.integrations import openclaw_agent_source as src


def test_deploy_common_agent_source_syncs_hidden_common_dir() -> None:
    dst = src.deploy_common_agent_source()
    assert dst.name == ".common-agent-source"
    assert (dst / "agent-common-rules.md").exists()
    assert (dst / "skills" / "csflow-task-decomposer" / "SKILL.md").exists()
    assert (dst / "cron-jobs" / "entropy-management.json").exists()


def test_deploy_common_agent_source_prunes_stale_files() -> None:
    dst = src.deploy_common_agent_source()
    stale = dst / "skills" / "obsolete-common-skill"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "SKILL.md").write_text("legacy", encoding="utf-8")
    src.deploy_common_agent_source()
    assert not stale.exists()


def test_deploy_common_agent_workspace_reads_bundled_rules_when_runtime_mirror_empty(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    deployed_rules = paths.common_agent_source_dir() / src.COMMON_RULES_FILE
    src.deploy_common_agent_source()
    deployed_rules.write_text("", encoding="utf-8")

    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=True)

    agents_md = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "Shared Rules for ClawsomeFlow Managed Agents" in agents_md


def test_deploy_agent_tools_bundle_syncs_hidden_tools_dir() -> None:
    dst = src.deploy_agent_tools_bundle()
    assert dst.name == ".clawsomeflow-agent-tools"
    assert (
        dst / "scripts" / "heartbeat" / "add-heartbeat-task.sh"
    ).exists()
    assert (
        dst / "scripts" / "heartbeat" / "remove-heartbeat-task.sh"
    ).exists()


def test_deploy_agent_tools_bundle_prunes_stale_files() -> None:
    dst = src.deploy_agent_tools_bundle()
    stale = dst / "scripts" / "obsolete-tool.sh"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("#!/usr/bin/env bash\necho legacy\n", encoding="utf-8")
    src.deploy_agent_tools_bundle()
    assert not stale.exists()


def test_deploy_common_agent_workspace_writes_agents_and_skills(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=True)
    agents_text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "Shared Rules for ClawsomeFlow Managed Agents" in agents_text
    assert "AGENTS_USER_CUSTOM_SECTION" in agents_text
    assert "INDEX.md" in agents_text
    assert (
        workspace / "skills" / "csflow-task-decomposer" / "SKILL.md"
    ).exists()
    assert (
        workspace / "skills" / "self-skills-heartbeats-maintenance" / "SKILL.md"
    ).exists()
    assert (
        workspace / "skills" / "self-definition-maintenance" / "SKILL.md"
    ).exists()
    assert (workspace / ".env").exists()
    assert (workspace / "my-desktop").is_dir()
    assert (workspace / ".csflow-common-managed.json").exists()
    assert not (workspace / ".csflow-agent-tools").exists()


def test_deploy_common_agent_workspace_updates_common_and_preserves_custom_section(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agents_md = workspace / "AGENTS.md"
    agents_md.write_text(
        "\n".join([
            "# 旧通用规则",
            "",
            "<!-- AGENTS_USER_CUSTOM_SECTION_START -->",
            "## AGENTS_USER_CUSTOM_SECTION",
            "",
            "- custom-keep-me",
            "<!-- AGENTS_USER_CUSTOM_SECTION_END -->",
            "",
        ]),
        encoding="utf-8",
    )
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=False)
    text = agents_md.read_text(encoding="utf-8")
    assert "旧通用规则" not in text
    assert "Shared Rules for ClawsomeFlow Managed Agents" in text
    assert "custom-keep-me" in text


def test_deploy_common_agent_workspace_preserves_legacy_unmarked_agents_md(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agents_md = workspace / "AGENTS.md"
    agents_md.write_text("legacy-user-rules", encoding="utf-8")
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=False)
    text = agents_md.read_text(encoding="utf-8")
    assert "Shared Rules for ClawsomeFlow Managed Agents" in text
    assert "AGENTS_USER_CUSTOM_SECTION_START" in text
    assert "legacy-user-rules" in text


def test_deploy_common_agent_workspace_recognizes_compact_custom_markers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agents_md = workspace / "AGENTS.md"
    agents_md.write_text(
        "\n".join([
            "# 旧通用规则",
            "",
            "<!--AGENTS_USER_CUSTOM_SECTION_START-->",
            "## AGENTS_USER_CUSTOM_SECTION",
            "",
            "- keep-compact-marker-content",
            "<!--AGENTS_USER_CUSTOM_SECTION_END-->",
            "",
        ]),
        encoding="utf-8",
    )
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=False)
    text = agents_md.read_text(encoding="utf-8")
    assert "Shared Rules for ClawsomeFlow Managed Agents" in text
    assert "keep-compact-marker-content" in text


def test_deploy_common_agent_workspace_preserves_when_marker_is_broken(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    agents_md = workspace / "AGENTS.md"
    agents_md.write_text(
        "\n".join([
            "# legacy-with-broken-marker",
            "<!-- AGENTS_USER_CUSTOM_SECTION_START -->",
            "## AGENTS_USER_CUSTOM_SECTION",
            "- keep-even-if-end-marker-missing",
        ]),
        encoding="utf-8",
    )
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=False)
    text = agents_md.read_text(encoding="utf-8")
    assert "Shared Rules for ClawsomeFlow Managed Agents" in text
    assert "keep-even-if-end-marker-missing" in text
    assert "legacy-with-broken-marker" in text


def test_deploy_common_agent_workspace_prunes_stale_managed_skill_dirs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=True)
    stale = workspace / "skills" / "obsolete-common-skill"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "SKILL.md").write_text("legacy", encoding="utf-8")
    # If a stale path is explicitly tracked as previously managed common skill,
    # redeploy should prune it to keep runtime common skills in sync.
    (workspace / ".csflow-common-managed.json").write_text(
        '{"managed_paths": ["skills/obsolete-common-skill"]}',
        encoding="utf-8",
    )
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=False)
    assert not stale.exists()


def test_deploy_common_agent_workspace_keeps_user_workspace_docs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    custom_doc = workspace / "IDENTITY.md"
    custom_doc.write_text("user-owned identity doc", encoding="utf-8")
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=True)
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=False)
    assert custom_doc.exists()
    assert custom_doc.read_text(encoding="utf-8") == "user-owned identity doc"


def test_deploy_common_agent_workspace_preserves_custom_skills_and_non_skill_legacy_manifest(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worker-workspace"
    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=True)

    custom_skill = workspace / "skills" / "user-custom-skill"
    custom_skill.mkdir(parents=True, exist_ok=True)
    (custom_skill / "SKILL.md").write_text("custom", encoding="utf-8")

    stale_common = workspace / "skills" / "obsolete-common-skill"
    stale_common.mkdir(parents=True, exist_ok=True)
    (stale_common / "SKILL.md").write_text("legacy", encoding="utf-8")

    # Simulate a legacy manifest that previously tracked a non-skill path.
    (workspace / "README.md").write_text("user-readme", encoding="utf-8")
    (workspace / ".csflow-common-managed.json").write_text(
        '{"managed_paths": ["skills/obsolete-common-skill", "README.md"]}',
        encoding="utf-8",
    )

    src.deploy_common_agent_workspace(workspace, overwrite_agents_md=False)

    # Stale managed common skill should be pruned.
    assert not stale_common.exists()
    # User custom skill should stay untouched.
    assert (custom_skill / "SKILL.md").exists()
    # Legacy non-skill manifest entries must not be removed anymore.
    assert (workspace / "README.md").read_text(encoding="utf-8") == "user-readme"
