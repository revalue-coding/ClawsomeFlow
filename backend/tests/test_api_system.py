"""Tests for local system helper API endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

import app.api.system as system
from app.main import create_app


def test_pick_directory_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system._pick_directory_native",
        lambda **_: "/tmp/example-repo",
    )
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/pick-directory",
            json={"title": "pick", "initialPath": "/tmp"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "/tmp/example-repo"


def test_pick_directory_cancel_returns_null(monkeypatch) -> None:
    monkeypatch.setattr("app.api.system._pick_directory_native", lambda **_: None)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/pick-directory", json={})
    assert r.status_code == 200
    assert r.json()["path"] is None


def test_pick_directory_local_only(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="server"),
    )
    with TestClient(create_app()) as client:
        r = client.post("/api/system/pick-directory", json={})
    assert r.status_code == 409
    assert r.json()["error"] == "DIRECTORY_PICKER_UNAVAILABLE"


def test_pick_directory_runtime_error_mapped(monkeypatch) -> None:
    def _boom(**_):
        raise RuntimeError("no display")

    monkeypatch.setattr("app.api.system._pick_directory_native", _boom)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/pick-directory", json={})
    assert r.status_code == 503
    assert r.json()["error"] == "DIRECTORY_PICKER_UNAVAILABLE"


def test_pick_directory_macos_uses_osascript(monkeypatch, tmp_path: Path) -> None:
    # macOS regression: must NOT require DISPLAY/WAYLAND_DISPLAY (Aqua/Cocoa) and
    # should drive the native folder chooser via `osascript choose folder`.
    monkeypatch.setattr(system.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    seen: dict[str, list[str]] = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return SimpleNamespace(returncode=0, stdout=f"{tmp_path}/\n", stderr="")

    monkeypatch.setattr(system.subprocess, "run", _fake_run)
    result = system._pick_directory_native(title="pick", initial_path=str(tmp_path))
    assert result == str(tmp_path.resolve())
    assert seen["argv"][0] == "osascript"
    assert any("choose folder" in part for part in seen["argv"])


def test_pick_directory_macos_cancel_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(system.sys, "platform", "darwin")

    def _fake_run(argv, **kwargs):
        return SimpleNamespace(
            returncode=1, stdout="", stderr="execution error: User canceled. (-128)",
        )

    monkeypatch.setattr(system.subprocess, "run", _fake_run)
    assert system._pick_directory_native(title="x", initial_path=str(tmp_path)) is None


def test_pick_directory_linux_without_display_raises(monkeypatch) -> None:
    # Non-macOS path is unchanged: no X11 display -> clear error.
    monkeypatch.setattr(system.sys, "platform", "linux")
    monkeypatch.setattr(system.os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(RuntimeError, match="No GUI display"):
        system._pick_directory_native(title="x", initial_path=None)


def test_open_directory_success(monkeypatch, tmp_path: Path) -> None:
    opened: dict[str, Path] = {}

    def _fake_open(*, path: Path) -> None:
        opened["path"] = path

    monkeypatch.setattr("app.api.system._open_directory_native", _fake_open)
    target = tmp_path / "my-desktop"
    target.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/open-directory",
            json={"path": str(target)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["opened"] is True
    assert body["path"] == str(target.resolve())
    assert opened["path"] == target.resolve()


def test_open_directory_local_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="server"),
    )
    target = tmp_path / "my-desktop"
    target.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/open-directory", json={"path": str(target)})
    assert r.status_code == 409
    assert r.json()["error"] == "DIRECTORY_OPEN_UNAVAILABLE"


def test_open_directory_rejects_relative_path() -> None:
    with TestClient(create_app()) as client:
        r = client.post("/api/system/open-directory", json={"path": "relative/path"})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_DIRECTORY_PATH"


def test_open_directory_rejects_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    with TestClient(create_app()) as client:
        r = client.post("/api/system/open-directory", json={"path": str(target)})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_DIRECTORY_PATH"


def test_open_directory_runtime_error_mapped(monkeypatch, tmp_path: Path) -> None:
    def _boom(**_):
        raise RuntimeError("no display")

    monkeypatch.setattr("app.api.system._open_directory_native", _boom)
    target = tmp_path / "my-desktop"
    target.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app()) as client:
        r = client.post("/api/system/open-directory", json={"path": str(target)})
    assert r.status_code == 503
    assert r.json()["error"] == "DIRECTORY_OPEN_UNAVAILABLE"


def test_ui_capabilities_local_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    with TestClient(create_app()) as client:
        r = client.get("/api/system/ui-capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deploymentMode"] == "local"
    assert body["allowNativeDirectoryPicker"] is True
    assert body["nativeDirectoryClientColocated"] is True


def test_ui_capabilities_client_not_colocated_same_platform_ssh(monkeypatch) -> None:
    """Linux browser + Linux server over SSH -L must still be treated as remote."""
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    monkeypatch.setattr("app.api.system.native_directory_ui_available", lambda: True)
    monkeypatch.setattr("app.api.system._loopback_client_is_ssh_forward", lambda _h, _p: True)
    with TestClient(create_app()) as client:
        r = client.get(
            "/api/system/ui-capabilities",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nativeDirectoryUiAvailable"] is True
    assert body["nativeDirectoryClientColocated"] is False


def test_ui_capabilities_same_platform_without_ssh_forward(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    monkeypatch.setattr("app.api.system._loopback_client_is_ssh_forward", lambda _h, _p: False)
    with TestClient(create_app()) as client:
        r = client.get(
            "/api/system/ui-capabilities",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
        )
    assert r.status_code == 200, r.text
    assert r.json()["nativeDirectoryClientColocated"] is True


def test_ui_capabilities_client_not_colocated_ssh_forward(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    monkeypatch.setattr("app.api.system.native_directory_ui_available", lambda: True)
    monkeypatch.setattr("app.api.system._loopback_client_is_ssh_forward", lambda _h, _p: True)
    with TestClient(create_app()) as client:
        r = client.get("/api/system/ui-capabilities")
    assert r.status_code == 200, r.text
    assert r.json()["nativeDirectoryClientColocated"] is False


def test_pick_directory_blocked_when_client_not_colocated(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    monkeypatch.setattr(
        "app.api.system.native_directory_client_colocated",
        lambda _request: False,
    )
    with TestClient(create_app()) as client:
        r = client.post("/api/system/pick-directory", json={})
    assert r.status_code == 409
    assert r.json()["error"] == "DIRECTORY_PICKER_UNAVAILABLE"


def test_open_directory_blocked_when_client_not_colocated(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    monkeypatch.setattr(
        "app.api.system.native_directory_client_colocated",
        lambda _request: False,
    )
    with TestClient(create_app()) as client:
        r = client.post("/api/system/open-directory", json={"path": str(target)})
    assert r.status_code == 409
    assert r.json()["error"] == "DIRECTORY_OPEN_UNAVAILABLE"


def test_ui_capabilities_server_mode_disables_native_picker(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="server"),
    )
    with TestClient(create_app()) as client:
        r = client.get("/api/system/ui-capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deploymentMode"] == "server"
    assert body["allowNativeDirectoryPicker"] is False


def test_ui_capabilities_native_ui_false_without_display(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    monkeypatch.setattr("app.api.system.native_directory_ui_available", lambda: False)
    with TestClient(create_app()) as client:
        r = client.get("/api/system/ui-capabilities")
    assert r.status_code == 200, r.text
    assert r.json()["nativeDirectoryUiAvailable"] is False


def test_workspace_directories_local_scoped_to_current_user(monkeypatch) -> None:
    monkeypatch.setenv("CSFLOW_USER", "alice")
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="local"),
    )
    captured: dict[str, object] = {}

    def _fake_collect(*, owner_user: str | None) -> set[str]:
        captured["owner_user"] = owner_user
        return {"/srv/repo-a", "/srv/repo-b"}

    monkeypatch.setattr("app.api.system._collect_workspace_dirs", _fake_collect)
    with TestClient(create_app()) as client:
        r = client.get("/api/system/workspace-directories")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deploymentMode"] == "local"
    assert body["items"] == ["/srv/repo-a", "/srv/repo-b"]
    assert captured["owner_user"] == "alice"


def test_workspace_directories_server_all_users(monkeypatch) -> None:
    monkeypatch.setenv("CSFLOW_USER", "alice")
    monkeypatch.setattr(
        "app.api.system.load_config",
        lambda: SimpleNamespace(deployment_mode="server"),
    )
    captured: dict[str, object] = {}

    def _fake_collect(*, owner_user: str | None) -> set[str]:
        captured["owner_user"] = owner_user
        return {"/srv/shared/repo"}

    monkeypatch.setattr("app.api.system._collect_workspace_dirs", _fake_collect)
    with TestClient(create_app()) as client:
        r = client.get("/api/system/workspace-directories?allUsers=true")
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "FORBIDDEN"
    assert captured.get("owner_user") is None


def test_ensure_git_repo_creates_and_initializes(tmp_path: Path) -> None:
    target = tmp_path / "new-repo"
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/ensure-git-repo",
            json={
                "path": str(target),
                "createDirIfMissing": True,
                "initializeIfMissing": True,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pathExists"] is True
    assert body["isGitRepo"] is True
    assert body["hasInitialCommit"] is False
    assert body["createdDir"] is True
    assert body["initializedRepo"] is True
    assert body["createdInitialCommit"] is False
    assert (target / ".git").exists()


def test_ensure_git_repo_returns_current_branch(tmp_path: Path) -> None:
    target = tmp_path / "repo-current-branch"
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/ensure-git-repo",
            json={
                "path": str(target),
                "createDirIfMissing": True,
                "initializeIfMissing": True,
                "createInitialCommitIfMissing": True,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["isGitRepo"] is True
    assert body["currentBranch"]
    proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=target,
        capture_output=True,
        text=True,
        check=True,
    )
    assert body["currentBranch"] == proc.stdout.strip()


def test_ensure_git_repo_can_create_initial_commit(tmp_path: Path) -> None:
    target = tmp_path / "repo-with-initial-commit"
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/ensure-git-repo",
            json={
                "path": str(target),
                "createDirIfMissing": True,
                "initializeIfMissing": True,
                "createInitialCommitIfMissing": True,
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["isGitRepo"] is True
    assert body["hasInitialCommit"] is True
    assert body["createdInitialCommit"] is True


def test_ensure_git_repo_rejects_relative_path() -> None:
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/ensure-git-repo",
            json={"path": "relative/repo"},
        )
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "INVALID_REPO_PATH"


def test_repo_branches_non_git_defaults_master_readonly(tmp_path: Path) -> None:
    target = tmp_path / "not-a-git"
    target.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/git-branches",
            json={"path": str(target)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pathExists"] is True
    assert body["isGitRepo"] is False
    assert body["editable"] is False
    assert body["currentBranch"] == "main"
    assert body["branches"] == ["main"]


def test_repo_branches_lists_local_heads(tmp_path: Path) -> None:
    target = tmp_path / "git-branches"
    with TestClient(create_app()) as client:
        ensured = client.post(
            "/api/system/ensure-git-repo",
            json={
                "path": str(target),
                "createDirIfMissing": True,
                "initializeIfMissing": True,
                "createInitialCommitIfMissing": True,
            },
        )
        assert ensured.status_code == 200, ensured.text
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=target,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() or "main"
    subprocess.run(["git", "checkout", "-b", "feature/demo"], cwd=target, check=True)
    subprocess.run(["git", "checkout", base_branch], cwd=target, check=True)
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/git-branches",
            json={"path": str(target)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pathExists"] is True
    assert body["isGitRepo"] is True
    assert body["editable"] is True
    assert body["currentBranch"] == base_branch
    assert base_branch in body["branches"]
    assert "feature/demo" in body["branches"]


def test_repo_branches_excludes_clawteam_agent_refs(tmp_path: Path) -> None:
    target = tmp_path / "git-branches-filter"
    with TestClient(create_app()) as client:
        ensured = client.post(
            "/api/system/ensure-git-repo",
            json={
                "path": str(target),
                "createDirIfMissing": True,
                "initializeIfMissing": True,
                "createInitialCommitIfMissing": True,
            },
        )
        assert ensured.status_code == 200, ensured.text
    subprocess.run(["git", "branch", "clawteam/run-1/agent-a"], cwd=target, check=True)
    subprocess.run(["git", "branch", "feature/demo"], cwd=target, check=True)
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/git-branches",
            json={"path": str(target)},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "clawteam/run-1/agent-a" not in body["branches"]
    assert "feature/demo" in body["branches"]
    assert "main" in body["branches"]


def test_repo_branches_preserve_existing_branch_even_when_filtered(
    tmp_path: Path,
) -> None:
    target = tmp_path / "git-branches-preserve"
    with TestClient(create_app()) as client:
        ensured = client.post(
            "/api/system/ensure-git-repo",
            json={
                "path": str(target),
                "createDirIfMissing": True,
                "initializeIfMissing": True,
                "createInitialCommitIfMissing": True,
            },
        )
        assert ensured.status_code == 200, ensured.text
    subprocess.run(["git", "branch", "clawteam/run-1/agent-a"], cwd=target, check=True)
    with TestClient(create_app()) as client:
        r = client.post(
            "/api/system/git-branches",
            json={
                "path": str(target),
                "preserveBranch": "clawteam/run-1/agent-a",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "clawteam/run-1/agent-a" in body["branches"]

    with TestClient(create_app()) as client:
        r2 = client.post(
            "/api/system/git-branches",
            json={
                "path": str(target),
                "preserveBranch": "missing-branch",
            },
        )
    assert r2.status_code == 200, r2.text
    assert "missing-branch" not in r2.json()["branches"]


def test_ss_line_indicates_ssh_forward_local_port() -> None:
    line = "ESTAB 0 0 127.0.0.1:54321 127.0.0.1:17017 users:((\"sshd\",pid=99,fd=3))"
    assert system._ss_line_indicates_ssh_forward(line, 54321) is True


def test_ss_line_indicates_ssh_forward_ignores_non_ssh() -> None:
    line = "ESTAB 0 0 127.0.0.1:54321 127.0.0.1:17017 users:((\"python3\",pid=99,fd=3))"
    assert system._ss_line_indicates_ssh_forward(line, 54321) is False


def test_linux_ssh_process_detection_via_proc(monkeypatch) -> None:
    monkeypatch.setattr(
        system,
        "_linux_socket_inodes_for_loopback_port",
        lambda _port: ["12345"],
    )
    monkeypatch.setattr(system, "_linux_pids_holding_socket_inode", lambda _inode: [4242])
    monkeypatch.setattr(system, "_linux_comm_for_pid", lambda _pid: "sshd")
    assert system._linux_ssh_processes_for_loopback_port(54321) == {"sshd"}
    assert system._loopback_client_is_ssh_forward("127.0.0.1", 54321) is True

