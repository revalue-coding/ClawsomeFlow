"""Tests for local system helper API endpoints."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient

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
    assert body["currentBranch"] == "master"
    assert body["branches"] == ["master"]


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
    ).stdout.strip() or "master"
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

