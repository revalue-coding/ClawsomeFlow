"""Tests for app.services.mcp_install — multi-platform MCP registration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import mcp_install


def test_supported_platforms_includes_core() -> None:
    plats = set(mcp_install.supported_platforms())
    assert {"hermes", "openclaw", "claude", "codex", "gemini", "cursor", "opencode"} <= plats


def test_unknown_platform_raises() -> None:
    with pytest.raises(ValueError):
        mcp_install.install("nope")


def test_hermes_install_default_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    from app.services import hermes_agents

    cfgp = tmp_path / "config.yaml"
    monkeypatch.setattr(hermes_agents, "default_profile_config_path", lambda: cfgp)

    # No --agent → writes into the default profile config.
    res = mcp_install.install("hermes")
    assert res.action == "written"
    entry = yaml.safe_load(cfgp.read_text())["mcp_servers"]["clawsomeflow"]
    assert entry["command"] == "csflow"
    assert entry["args"] == ["mcp", "serve"]
    assert entry["enabled"] is True

    # Preserves other servers on a second (different-name) write.
    mcp_install.install("hermes", name="other")
    servers = yaml.safe_load(cfgp.read_text())["mcp_servers"]
    assert set(servers) == {"clawsomeflow", "other"}

    # Uninstall removes just the named entry.
    assert mcp_install.uninstall("hermes").action == "removed"
    assert "clawsomeflow" not in yaml.safe_load(cfgp.read_text()).get("mcp_servers", {})


def test_print_config_shapes() -> None:
    assert '"mcpServers"' in mcp_install.print_config("claude")
    assert "[mcp_servers.clawsomeflow]" in mcp_install.print_config("codex")
    assert '"mcp"' in mcp_install.print_config("opencode")
    assert "mcp_servers:" in mcp_install.print_config("hermes")


def test_manual_platform_returns_snippet() -> None:
    res = mcp_install.install("openclaw")
    assert res.action == "manual"
    assert "mcpServers" in res.message


def test_json_install_creates_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    res = mcp_install.install("claude", force=True)
    assert res.action == "written"
    data = json.loads((tmp_path / ".claude.json").read_text())
    assert data["mcpServers"]["clawsomeflow"] == {"command": "csflow", "args": ["mcp", "serve"]}
    # Second run is a no-op.
    assert mcp_install.install("claude", force=True).action == "unchanged"


def test_json_install_preserves_existing_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".cursor" / "mcp.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "misc": 1}))
    mcp_install.install("cursor", force=True)
    data = json.loads(p.read_text())
    assert data["misc"] == 1
    assert "other" in data["mcpServers"]
    assert "clawsomeflow" in data["mcpServers"]


def test_json_install_does_not_clobber_unparseable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".gemini" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not valid json")
    res = mcp_install.install("gemini", force=True)
    assert res.action == "unchanged"
    assert p.read_text() == "{not valid json"


def test_codex_install_append_and_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".codex" / "config.toml"
    p.parent.mkdir(parents=True)
    p.write_text('model = "gpt"\n')
    res = mcp_install.install("codex", force=True)
    assert res.action == "written"
    text = p.read_text()
    assert 'model = "gpt"' in text  # preserved
    assert "[mcp_servers.clawsomeflow]" in text
    # Idempotent.
    assert mcp_install.install("codex", force=True).action == "unchanged"
    # Uninstall removes the table but keeps other content.
    assert mcp_install.uninstall("codex").action == "removed"
    after = p.read_text()
    assert "[mcp_servers.clawsomeflow]" not in after
    assert 'model = "gpt"' in after


def test_opencode_install_uses_mcp_local_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    mcp_install.install("opencode", force=True)
    p = tmp_path / ".config" / "opencode" / "opencode.json"
    data = json.loads(p.read_text())
    entry = data["mcp"]["clawsomeflow"]
    assert entry["type"] == "local"
    assert entry["command"] == ["csflow", "mcp", "serve"]
    assert entry["enabled"] is True


def test_absent_cli_without_force_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mcp_install.shutil, "which", lambda _c: None)
    res = mcp_install.install("claude")  # force=False
    assert res.action == "absent"
    assert not (tmp_path / ".claude.json").exists()
