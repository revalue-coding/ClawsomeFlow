"""WSL2 support: detection matrix + directory interop + zero-impact guards.

The zero-impact contract (CLAUDE.md / DEV plan): everything in
``app/platform_wsl.py`` must be falsy / no-op outside a real WSL distro, and
none of the WSL branches may change behaviour on plain Linux or macOS.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.system as system
from app import platform_wsl
from app.main import create_app

# ──────────────────────────────────────────────────────────────────────
# Detection matrix
# ──────────────────────────────────────────────────────────────────────


def _set_env(monkeypatch, *, platform: str, osrelease: str, interop: bool) -> None:
    monkeypatch.setattr(platform_wsl.sys, "platform", platform)
    monkeypatch.setattr(platform_wsl, "_kernel_osrelease", lambda: osrelease)
    monkeypatch.setattr(platform_wsl, "_has_wsl_interop_markers", lambda: interop)


def test_is_wsl_true_in_real_wsl(monkeypatch) -> None:
    _set_env(
        monkeypatch,
        platform="linux",
        osrelease="5.15.167.4-microsoft-standard-WSL2",
        interop=True,
    )
    assert platform_wsl.is_wsl() is True


def test_is_wsl_false_on_plain_linux(monkeypatch) -> None:
    _set_env(monkeypatch, platform="linux", osrelease="5.15.0-177-generic", interop=False)
    assert platform_wsl.is_wsl() is False


def test_is_wsl_false_on_macos_even_with_markers(monkeypatch) -> None:
    _set_env(
        monkeypatch,
        platform="darwin",
        osrelease="5.15.167.4-microsoft-standard-WSL2",
        interop=True,
    )
    assert platform_wsl.is_wsl() is False


def test_is_wsl_false_in_docker_on_wsl_backend(monkeypatch) -> None:
    # Docker Desktop on Windows: container shares the Microsoft kernel string
    # but has none of the WSL interop mounts -> must NOT be treated as WSL.
    _set_env(
        monkeypatch,
        platform="linux",
        osrelease="5.15.167.4-microsoft-standard-WSL2",
        interop=False,
    )
    assert platform_wsl.is_wsl() is False


def test_is_wsl_false_when_osrelease_unreadable(monkeypatch) -> None:
    _set_env(monkeypatch, platform="linux", osrelease="", interop=True)
    assert platform_wsl.is_wsl() is False


def test_wsl_distro_name_from_env(monkeypatch) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    assert platform_wsl.wsl_distro_name() == "Ubuntu-24.04"
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert platform_wsl.wsl_distro_name() is None


# ──────────────────────────────────────────────────────────────────────
# Windows drive mount (/mnt/<letter>) shape check
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mnt/c/Users/me/project", True),
        ("/mnt/d/repo", True),
        ("/mnt/c", True),
        ("/mnt/wsl/something", False),  # WSL cross-distro mount, not a drive
        ("/mnt/data/repo", False),
        ("/home/me/work/repo", False),
        ("/mnt", False),
    ],
)
def test_is_windows_drive_mount(path: str, expected: bool) -> None:
    assert platform_wsl.is_windows_drive_mount(PurePosixPath(path)) is expected


# ──────────────────────────────────────────────────────────────────────
# wslpath / explorer.exe helpers
# ──────────────────────────────────────────────────────────────────────


def test_windows_path_for_converts_via_wslpath(monkeypatch) -> None:
    monkeypatch.setattr(platform_wsl.shutil, "which", lambda _n: "/usr/bin/wslpath")
    seen: dict[str, list[str]] = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(
            returncode=0, stdout="\\\\wsl.localhost\\Ubuntu\\home\\me\\proj\n", stderr="",
        )

    monkeypatch.setattr(platform_wsl.subprocess, "run", _fake_run)
    out = platform_wsl.windows_path_for(Path("/home/me/proj"))
    assert out == "\\\\wsl.localhost\\Ubuntu\\home\\me\\proj"
    assert seen["argv"] == ["/usr/bin/wslpath", "-w", "/home/me/proj"]


def test_windows_path_for_none_without_wslpath(monkeypatch) -> None:
    monkeypatch.setattr(platform_wsl.shutil, "which", lambda _n: None)
    assert platform_wsl.windows_path_for(Path("/home/me")) is None


def test_windows_path_for_none_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(platform_wsl.shutil, "which", lambda _n: "/usr/bin/wslpath")
    monkeypatch.setattr(
        platform_wsl.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert platform_wsl.windows_path_for(Path("/home/me")) is None


def test_find_windows_explorer_prefers_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(
        platform_wsl.shutil, "which", lambda _n: "/mnt/c/Windows/explorer.exe",
    )
    assert platform_wsl.find_windows_explorer() == "/mnt/c/Windows/explorer.exe"


def test_find_windows_explorer_none_outside_wsl(monkeypatch) -> None:
    monkeypatch.setattr(platform_wsl.shutil, "which", lambda _n: None)
    assert platform_wsl.find_windows_explorer() is None or Path(
        platform_wsl._EXPLORER_FALLBACK
    ).exists()


# ──────────────────────────────────────────────────────────────────────
# open-directory: WSL explorer.exe branch
# ──────────────────────────────────────────────────────────────────────


def test_open_directory_wsl_launches_explorer(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.platform_wsl, "find_windows_explorer", lambda: "/mnt/c/W/e.exe")
    monkeypatch.setattr(
        system.platform_wsl, "windows_path_for", lambda p: "\\\\wsl.localhost\\U\\x",
    )
    seen: dict[str, list[str]] = {}

    def _fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(pid=1234)

    monkeypatch.setattr(system.subprocess, "Popen", _fake_popen)
    assert system._open_directory_wsl(tmp_path) is True
    assert seen["argv"] == ["/mnt/c/W/e.exe", "\\\\wsl.localhost\\U\\x"]


def test_open_directory_wsl_false_without_interop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.platform_wsl, "find_windows_explorer", lambda: None)
    assert system._open_directory_wsl(tmp_path) is False


def test_open_directory_native_uses_wsl_branch_before_gui_check(
    monkeypatch, tmp_path: Path,
) -> None:
    # In WSL with no DISPLAY the explorer.exe branch must win (no RuntimeError).
    monkeypatch.setattr(system.platform_wsl, "is_wsl", lambda: True)
    monkeypatch.setattr(system, "_open_directory_wsl", lambda path: True)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    system._open_directory_native(path=tmp_path)  # must not raise


def test_open_directory_native_unchanged_outside_wsl(monkeypatch, tmp_path: Path) -> None:
    # Zero-impact guard: plain Linux without a display keeps the legacy error.
    monkeypatch.setattr(system.platform_wsl, "is_wsl", lambda: False)
    monkeypatch.setattr(system.os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(RuntimeError, match="No GUI display"):
        system._open_directory_native(path=tmp_path)


def test_open_directory_wsl_falls_back_to_linux_openers(monkeypatch, tmp_path: Path) -> None:
    # WSLg case: explorer interop unavailable but DISPLAY + xdg-open exist.
    monkeypatch.setattr(system.platform_wsl, "is_wsl", lambda: True)
    monkeypatch.setattr(system, "_open_directory_wsl", lambda path: False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        system.shutil, "which", lambda n: "/usr/bin/xdg-open" if n == "xdg-open" else None,
    )
    seen: dict[str, list[str]] = {}

    def _fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(system.subprocess, "Popen", _fake_popen)
    system._open_directory_native(path=tmp_path)
    # The opener chain launches by command name (PATH-resolved at exec time).
    assert seen["argv"][0] == "xdg-open"


# ──────────────────────────────────────────────────────────────────────
# Colocation gate: WSL adjustments (open allowed, pick keeps blocking)
# ──────────────────────────────────────────────────────────────────────


def test_open_directory_allowed_in_wsl_when_not_colocated(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr("app.api.system.native_directory_client_colocated", lambda _r: False)
    monkeypatch.setattr(system.platform_wsl, "is_wsl", lambda: True)
    opened: dict[str, Path] = {}
    monkeypatch.setattr(
        "app.api.system._open_directory_native",
        lambda *, path: opened.__setitem__("path", path),
    )
    with TestClient(create_app()) as client:
        r = client.post("/api/system/open-directory", json={"path": str(tmp_path)})
    assert r.status_code == 200, r.text
    assert opened["path"] == tmp_path.resolve()


def test_open_directory_still_blocked_outside_wsl(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.api.system.native_directory_client_colocated", lambda _r: False)
    monkeypatch.setattr(system.platform_wsl, "is_wsl", lambda: False)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/open-directory", json={"path": str(tmp_path)})
    assert r.status_code == 409
    assert r.json()["error"] == "DIRECTORY_OPEN_UNAVAILABLE"


def test_pick_directory_blocked_in_wsl_with_specific_message(monkeypatch) -> None:
    monkeypatch.setattr("app.api.system.native_directory_client_colocated", lambda _r: False)
    monkeypatch.setattr(system.platform_wsl, "is_wsl", lambda: True)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/pick-directory", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "DIRECTORY_PICKER_UNAVAILABLE"
    assert "WSL" in body["message"]


# ──────────────────────────────────────────────────────────────────────
# ui-capabilities: wslEnvironment flag
# ──────────────────────────────────────────────────────────────────────


def test_ui_capabilities_reports_wsl_environment(monkeypatch) -> None:
    monkeypatch.setattr(system.platform_wsl, "is_wsl", lambda: True)
    with TestClient(create_app()) as client:
        r = client.get("/api/system/ui-capabilities")
    assert r.status_code == 200
    assert r.json()["wslEnvironment"] is True


def test_ui_capabilities_wsl_environment_false_by_default() -> None:
    with TestClient(create_app()) as client:
        r = client.get("/api/system/ui-capabilities")
    assert r.status_code == 200
    assert r.json()["wslEnvironment"] is False


# ──────────────────────────────────────────────────────────────────────
# Flow save warning: repo on a Windows drive mount
# ──────────────────────────────────────────────────────────────────────


def _spec_with_repo(repo: str):
    from app.models import AgentKind, FlowAgent, FlowSpec

    return FlowSpec(
        agents=[FlowAgent(id="worker", kind=AgentKind.claude, repo=repo, is_leader=True)],
        tasks=[],
    )


def test_flow_save_warns_on_windows_mount_repo_in_wsl(monkeypatch) -> None:
    from app.api import flows as flows_api

    monkeypatch.setattr(platform_wsl, "is_wsl", lambda: True)
    warnings = flows_api._wsl_windows_mount_warnings(_spec_with_repo("/mnt/c/Users/me/proj"))
    assert len(warnings) == 1
    assert warnings[0].code == "WSL_WINDOWS_MOUNT_REPO"
    assert warnings[0].details["agentIds"] == ["worker"]


def test_flow_save_no_warning_for_wsl_native_repo(monkeypatch) -> None:
    from app.api import flows as flows_api

    monkeypatch.setattr(platform_wsl, "is_wsl", lambda: True)
    assert flows_api._wsl_windows_mount_warnings(_spec_with_repo("/home/me/proj")) == []


def test_flow_save_no_warning_outside_wsl(monkeypatch) -> None:
    from app.api import flows as flows_api

    monkeypatch.setattr(platform_wsl, "is_wsl", lambda: False)
    assert flows_api._wsl_windows_mount_warnings(_spec_with_repo("/mnt/c/Users/me/proj")) == []
