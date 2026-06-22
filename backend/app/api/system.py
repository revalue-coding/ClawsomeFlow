"""System utility API endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app import paths
from app.api._auth import current_user
from app.api.errors import ApiError
from app.config import load_config
from app.deployment import get_deployment_capabilities
from app.integrations import git_repo as git_repo_util
from app.logging_setup import get_logger
from app.models import DEFAULT_TARGET_BRANCH
from app.services import update_check
from app.storage import get_storage

logger = get_logger("api.system")

router = APIRouter(prefix="/system", tags=["system"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


UserDep = Annotated[str, Depends(current_user)]


class PickDirectoryPayload(_CamelModel):
    title: str = Field(default="Select workspace directory")
    initial_path: str | None = Field(default=None)


class PickDirectoryResponse(_CamelModel):
    path: str | None


class OpenDirectoryPayload(_CamelModel):
    path: str


class OpenDirectoryResponse(_CamelModel):
    opened: bool
    path: str


class WorkspaceDirectoryListResponse(_CamelModel):
    deployment_mode: Literal["local", "server"]
    items: list[str] = Field(default_factory=list)


class UiCapabilitiesResponse(_CamelModel):
    deployment_mode: Literal["local", "server"]
    allow_native_directory_picker: bool
    native_directory_ui_available: bool
    native_directory_client_colocated: bool


class OwnerKindsFastResponse(_CamelModel):
    persistent_kinds: list[str] = Field(default_factory=list)
    temporary_kinds: list[str] = Field(default_factory=list)


class EnsureGitRepoPayload(_CamelModel):
    path: str
    create_dir_if_missing: bool = True
    initialize_if_missing: bool = True
    create_initial_commit_if_missing: bool = False


class EnsureGitRepoResponse(_CamelModel):
    path: str
    path_exists: bool
    is_git_repo: bool
    has_initial_commit: bool
    created_dir: bool = False
    initialized_repo: bool = False
    created_initial_commit: bool = False
    current_branch: str | None = None


class RepoBranchesPayload(_CamelModel):
    path: str
    preserve_branch: str | None = None


class RepoBranchesResponse(_CamelModel):
    path: str
    path_exists: bool
    is_git_repo: bool
    editable: bool
    current_branch: str
    branches: list[str] = Field(default_factory=list)


# Owner-kind "fast probe": ONLY `which` checks (millisecond-level), no full
# runtime/version probing. Order defines dropdown order in the frontend.
_FAST_PERSISTENT_OWNER_KIND_BINARIES: tuple[tuple[str, str], ...] = (
    ("hermes", "hermes"),
    ("openclaw", "openclaw"),
)

_FAST_TEMP_OWNER_KIND_BINARIES: tuple[tuple[str, str], ...] = (
    ("claude", "claude"),
    ("codex", "codex"),
    # Cursor owner kind must probe the `agent` binary (not `cursor`).
    ("cursor", "agent"),
    ("gemini", "gemini"),
    ("kimi", "kimi"),
    ("qwen", "qwen"),
    ("opencode", "opencode"),
    ("qoder", "qodercli"),
    ("codebuddy", "codebuddy"),
    ("hermes", "hermes"),
)


def _detect_owner_kinds_fast(entries: tuple[tuple[str, str], ...]) -> list[str]:
    kinds: list[str] = []
    for kind, binary in entries:
        if shutil.which(binary):
            kinds.append(kind)
    return kinds


@router.post("/pick-directory", response_model=PickDirectoryResponse)
async def pick_directory(
    request: Request,
    payload: Annotated[PickDirectoryPayload, Body()] = PickDirectoryPayload(),
    _user: UserDep = "",
) -> PickDirectoryResponse:
    """Open a native directory picker and return the chosen absolute path.

    This is intentionally local-only: in server mode the backend host is not
    the user's machine, so opening a native chooser there would be misleading.
    """
    cfg = load_config()
    caps = get_deployment_capabilities(cfg)
    if not caps.allow_native_directory_picker:
        raise ApiError(
            "DIRECTORY_PICKER_UNAVAILABLE",
            "Directory picker is available only in local mode.",
            status_code=409,
        )
    _ensure_native_directory_client_colocated(request, action="pick")
    try:
        selected = await asyncio.to_thread(
            _pick_directory_native,
            title=payload.title,
            initial_path=payload.initial_path,
        )
    except RuntimeError as exc:
        raise ApiError(
            "DIRECTORY_PICKER_UNAVAILABLE",
            str(exc),
            status_code=503,
        ) from exc
    return PickDirectoryResponse(path=selected)


@router.post("/open-directory", response_model=OpenDirectoryResponse)
async def open_directory(
    request: Request,
    payload: Annotated[OpenDirectoryPayload, Body()],
    _user: UserDep = "",
) -> OpenDirectoryResponse:
    """Open a local directory using the system default file manager."""
    cfg = load_config()
    caps = get_deployment_capabilities(cfg)
    if not caps.allow_native_directory_picker:
        raise ApiError(
            "DIRECTORY_OPEN_UNAVAILABLE",
            "Open directory is available only in local mode.",
            status_code=409,
        )
    raw = (payload.path or "").strip()
    if not raw:
        raise ApiError(
            "INVALID_DIRECTORY_PATH",
            "directory path is required",
            status_code=400,
        )
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ApiError(
            "INVALID_DIRECTORY_PATH",
            "directory path must be an absolute path",
            status_code=400,
            details={"path": raw},
        )
    resolved = target.resolve(strict=False)
    if not resolved.exists() or not resolved.is_dir():
        raise ApiError(
            "INVALID_DIRECTORY_PATH",
            "directory path does not exist or is not a directory",
            status_code=400,
            details={"path": str(resolved)},
        )
    _ensure_native_directory_client_colocated(request, action="open")
    try:
        await asyncio.to_thread(_open_directory_native, path=resolved)
    except RuntimeError as exc:
        raise ApiError(
            "DIRECTORY_OPEN_UNAVAILABLE",
            str(exc),
            status_code=503,
        ) from exc
    return OpenDirectoryResponse(opened=True, path=str(resolved))


@router.post("/ensure-git-repo", response_model=EnsureGitRepoResponse)
def ensure_git_repo(
    payload: Annotated[EnsureGitRepoPayload, Body()],
    _user: UserDep = "",
) -> EnsureGitRepoResponse:
    raw = (payload.path or "").strip()
    if not raw:
        raise ApiError(
            "INVALID_REPO_PATH",
            "repo path is required",
            status_code=400,
        )
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ApiError(
            "INVALID_REPO_PATH",
            "repo path must be an absolute path",
            status_code=400,
            details={"path": raw},
        )

    created_dir = False
    try:
        if target.exists():
            if not target.is_dir():
                raise ApiError(
                    "INVALID_REPO_PATH",
                    f"path {str(target)!r} exists but is not a directory",
                    status_code=400,
                    details={"path": str(target)},
                )
        elif payload.create_dir_if_missing:
            target.mkdir(parents=True, exist_ok=True)
            created_dir = True
    except OSError as exc:
        raise ApiError(
            "REPO_CREATE_FAILED",
            f"failed to create repository directory: {exc}",
            status_code=500,
            details={"path": str(target)},
        ) from exc

    exists = target.exists() and target.is_dir()
    is_git_repo = exists and _is_git_repo(target)
    initialized_repo = False
    if exists and not is_git_repo and payload.initialize_if_missing:
        _git_init_repo(target)
        is_git_repo = _is_git_repo(target)
        initialized_repo = is_git_repo
    has_initial_commit = exists and is_git_repo and _git_has_initial_commit(target)
    created_initial_commit = False
    if (
        exists
        and is_git_repo
        and not has_initial_commit
        and payload.create_initial_commit_if_missing
    ):
        _git_create_initial_commit(target)
        has_initial_commit = _git_has_initial_commit(target)
        created_initial_commit = has_initial_commit

    current_branch: str | None = None
    if exists and is_git_repo:
        current_branch = _git_current_branch(target) or DEFAULT_TARGET_BRANCH

    return EnsureGitRepoResponse(
        path=str(target.resolve()),
        path_exists=exists,
        is_git_repo=is_git_repo,
        has_initial_commit=has_initial_commit,
        created_dir=created_dir,
        initialized_repo=initialized_repo,
        created_initial_commit=created_initial_commit,
        current_branch=current_branch,
    )


@router.post("/git-branches", response_model=RepoBranchesResponse)
def repo_branches(
    payload: Annotated[RepoBranchesPayload, Body()],
    _user: UserDep = "",
) -> RepoBranchesResponse:
    raw = (payload.path or "").strip()
    if not raw:
        raise ApiError(
            "INVALID_REPO_PATH",
            "repo path is required",
            status_code=400,
        )
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ApiError(
            "INVALID_REPO_PATH",
            "repo path must be an absolute path",
            status_code=400,
            details={"path": raw},
        )
    path_exists = target.exists() and target.is_dir()
    if not path_exists:
        return RepoBranchesResponse(
            path=str(target.resolve(strict=False)),
            path_exists=False,
            is_git_repo=False,
            editable=False,
            current_branch=DEFAULT_TARGET_BRANCH,
            branches=[DEFAULT_TARGET_BRANCH],
        )
    is_git_repo = _is_git_repo(target)
    if not is_git_repo:
        return RepoBranchesResponse(
            path=str(target.resolve()),
            path_exists=True,
            is_git_repo=False,
            editable=False,
            current_branch=DEFAULT_TARGET_BRANCH,
            branches=[DEFAULT_TARGET_BRANCH],
        )
    branches = git_repo_util.list_flow_target_branches(target)
    current = _git_current_branch(target) or DEFAULT_TARGET_BRANCH
    if current and current not in branches:
        branches = [current, *branches]
    preserve = (payload.preserve_branch or "").strip()
    if (
        preserve
        and preserve not in branches
        and git_repo_util.branch_exists_in_repo(target, preserve)
    ):
        branches = [preserve, *branches]
    if not branches:
        branches = [current]
    return RepoBranchesResponse(
        path=str(target.resolve()),
        path_exists=True,
        is_git_repo=True,
        editable=True,
        current_branch=current,
        branches=branches,
    )


def _pick_directory_macos(*, title: str, initial_dir: Path) -> str | None:
    """macOS native folder picker via AppleScript ``choose folder``.

    macOS uses Aqua/Cocoa, NOT X11, so it has no ``DISPLAY``/``WAYLAND_DISPLAY``
    and tkinter is unreliable under a LaunchAgent-backed service. ``osascript``
    drives the system folder chooser directly. A user cancel surfaces as
    AppleScript error -128, which we map to ``None`` (no selection).
    """
    prompt = (title or "Select workspace directory").replace("\\", "\\\\").replace('"', '\\"')
    default = str(initial_dir).replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'set _default to POSIX file "{default}"\n'
        f'set _folder to choose folder with prompt "{prompt}" default location _default\n'
        "POSIX path of _folder"
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # osascript missing / not launchable
        raise RuntimeError(
            f"failed to launch native directory picker: {exc}"
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # User cancelled the dialog -> not an error, just "no selection".
        if "-128" in err or "User canceled" in err or "User cancelled" in err:
            return None
        raise RuntimeError(err or "native directory picker failed")
    selected = (proc.stdout or "").strip()
    if not selected:
        return None
    return str(Path(selected).expanduser().resolve())


def _pick_directory_native(*, title: str, initial_path: str | None) -> str | None:
    initial_dir = Path(initial_path).expanduser() if initial_path else Path.home()
    if not initial_dir.exists():
        initial_dir = Path.home()

    # macOS uses Aqua/Cocoa (no DISPLAY/WAYLAND_DISPLAY); use AppleScript instead
    # of the X11 display check + tkinter below.
    if sys.platform == "darwin":
        return _pick_directory_macos(title=title, initial_dir=initial_dir)

    if not native_directory_ui_available():
        raise RuntimeError("No GUI display found for native directory picker.")

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on system packages
        raise RuntimeError(
            "tkinter is unavailable; cannot open a native directory picker."
        ) from exc

    root = tk.Tk()
    root.withdraw()
    try:
        root.wm_attributes("-topmost", 1)
    except Exception:
        pass
    try:
        selected = filedialog.askdirectory(
            title=title or "Select workspace directory",
            initialdir=str(initial_dir),
            mustexist=True,
            parent=root,
        )
    finally:
        root.destroy()

    if not selected:
        return None
    return str(Path(selected).expanduser().resolve())


def _open_directory_native(*, path: Path) -> None:
    target = str(path)
    if os.name == "nt":
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return
        except OSError as exc:
            raise RuntimeError(f"failed to open directory: {exc}") from exc

    if not native_directory_ui_available():
        raise RuntimeError("No GUI display found for opening directories.")

    commands: list[list[str]] = [["open", target], ["xdg-open", target], ["gio", "open", target]]
    last_error: OSError | None = None
    for argv in commands:
        if shutil.which(argv[0]) is None:
            continue
        try:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except OSError as exc:
            last_error = exc

    if last_error is not None:
        raise RuntimeError(f"failed to open directory: {last_error}") from last_error
    raise RuntimeError("No supported directory opener found (open / xdg-open / gio).")


def _is_git_repo(path: Path) -> bool:
    return git_repo_util.is_git_repo(path)


def _git_init_repo(path: Path) -> None:
    try:
        git_repo_util.git_init_repo(path)
    except RuntimeError as exc:
        msg = str(exc)
        code = "GIT_INIT_FAILED"
        if "unavailable" in msg:
            raise ApiError(code, msg, status_code=500, details={"path": str(path)}) from exc
        raise ApiError(
            code,
            "git init failed",
            status_code=500,
            details={"path": str(path), "stderr": msg},
        ) from exc


def _git_has_initial_commit(path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    return True


def _git_create_initial_commit(path: Path) -> None:
    try:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=ClawsomeFlow",
                "-c",
                "user.email=clawsomeflow@local",
                "commit",
                "--allow-empty",
                "-m",
                "[clawsomeflow] initialize repository",
            ],
            cwd=str(path),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ApiError(
            "GIT_INIT_COMMIT_FAILED",
            "git command is unavailable",
            status_code=500,
            details={"path": str(path)},
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()[:1000]
        raise ApiError(
            "GIT_INIT_COMMIT_FAILED",
            "git initial commit failed",
            status_code=500,
            details={
                "path": str(path),
                "stderr": stderr,
            },
        ) from exc


def _git_local_branches(path: Path) -> list[str]:
    return git_repo_util.list_local_branches(path)


def _git_current_branch(path: Path) -> str | None:
    return git_repo_util.current_branch(path)


def native_directory_ui_available() -> bool:
    """Whether this host can show a native directory picker or open a folder in a file manager."""
    if os.name == "nt":
        return True
    if sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _is_loopback_client_host(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def _is_ssh_process_name(name: str) -> bool:
    lowered = name.lower()
    return lowered == "sshd" or lowered.startswith("ssh") or lowered.startswith("autossh")


def _proc_tcp_port_hex(port: int) -> str:
    return f"{port & 0xFF:02X}{(port >> 8) & 0xFF:02X}"


def _linux_socket_inodes_for_loopback_port(port: int) -> list[str]:
    port_hex = _proc_tcp_port_hex(port)
    inodes: list[str] = []
    loopback_v4 = f"0100007F:{port_hex}"
    loopback_v6 = f"00000000000000000000000001000000:{port_hex}"

    for proc_path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = proc_path.read_text().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "01":
                continue
            local_addr, rem_addr, inode = parts[1], parts[2], parts[9]
            if any(
                addr.upper() == marker
                for addr in (local_addr, rem_addr)
                for marker in (loopback_v4.upper(), loopback_v6.upper())
            ):
                inodes.append(inode)
    return inodes


def _linux_pids_holding_socket_inode(inode: str) -> list[int]:
    pids: list[int] = []
    needle = f"socket:[{inode}]"
    proc_root = Path("/proc")
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return pids

    for proc_dir in proc_entries:
        if not proc_dir.name.isdigit():
            continue
        fd_dir = proc_dir / "fd"
        try:
            fd_names = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fd_names:
            try:
                if os.readlink(fd) == needle:
                    pids.append(int(proc_dir.name))
                    break
            except OSError:
                continue
    return pids


def _linux_comm_for_pid(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip().lower()
    except OSError:
        return None


def _linux_ssh_processes_for_loopback_port(port: int) -> set[str]:
    names: set[str] = set()
    for inode in _linux_socket_inodes_for_loopback_port(port):
        for pid in _linux_pids_holding_socket_inode(inode):
            comm = _linux_comm_for_pid(pid)
            if comm and _is_ssh_process_name(comm):
                names.add(comm)
    return names


def _ss_line_indicates_ssh_forward(line: str, client_port: int) -> bool:
    parts = line.split()
    if len(parts) < 4:
        return False
    local_addr, peer_addr = parts[2], parts[3]
    peer_v4 = f"127.0.0.1:{client_port}"
    peer_v6 = f"[::1]:{client_port}"
    if local_addr not in (peer_v4, peer_v6) and peer_addr not in (peer_v4, peer_v6):
        return False
    process_blob = line.split("users:", 1)[-1] if "users:" in line else ""
    return any(_is_ssh_process_name(name) for name in re.findall(r'"([^"]+)"', process_blob))


def _lsof_line_indicates_ssh_forward(line: str, client_port: int) -> bool:
    parts = line.split()
    if not parts:
        return False
    if not _is_ssh_process_name(parts[0]):
        return False
    port_token = f":{client_port}"
    return port_token in line and ("->127.0.0.1" in line or "->[::1]" in line or port_token in line)


def _loopback_client_is_ssh_forward(client_host: str, client_port: int) -> bool:
    """True when loopback HTTP peer port belongs to ssh/sshd (any OS, any SSH -L forward)."""
    if client_host in ("testclient",) or client_port <= 0:
        return False
    if not _is_loopback_client_host(client_host):
        return False

    try:
        if sys.platform == "linux" and _linux_ssh_processes_for_loopback_port(client_port):
            return True

        if shutil.which("ss"):
            result = subprocess.run(
                ["ss", "-H", "-tnp"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            for line in result.stdout.splitlines():
                if _ss_line_indicates_ssh_forward(line, client_port):
                    return True

        if shutil.which("lsof"):
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{client_port}", "-sTCP:ESTABLISHED"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            for line in result.stdout.splitlines()[1:]:
                if _lsof_line_indicates_ssh_forward(line, client_port):
                    return True
    except Exception:
        logger.debug("ssh forward detection failed", exc_info=True)

    return False


def native_directory_client_colocated(request: Request) -> bool:
    """Whether this HTTP client shares the server desktop (not SSH port-forwarded)."""
    client = request.client
    if client is None or not _is_loopback_client_host(client.host):
        return False
    if _loopback_client_is_ssh_forward(client.host, client.port or 0):
        return False
    return True


def _ensure_native_directory_client_colocated(request: Request, *, action: Literal["pick", "open"]) -> None:
    if native_directory_client_colocated(request):
        return
    code = "DIRECTORY_PICKER_UNAVAILABLE" if action == "pick" else "DIRECTORY_OPEN_UNAVAILABLE"
    raise ApiError(
        code,
        "Native directory UI is unavailable from this browser session "
        "(remote SSH port-forward). Paste the absolute path manually.",
        status_code=409,
    )


@router.get("/ui-capabilities", response_model=UiCapabilitiesResponse)
def ui_capabilities(request: Request, _user: UserDep = "") -> UiCapabilitiesResponse:
    """Expose deployment + native UI availability for frontend remote-client detection."""
    cfg = load_config()
    caps = get_deployment_capabilities(cfg)
    native_ui = native_directory_ui_available()
    return UiCapabilitiesResponse(
        deployment_mode=cfg.deployment_mode,
        allow_native_directory_picker=caps.allow_native_directory_picker,
        native_directory_ui_available=native_ui,
        native_directory_client_colocated=native_directory_client_colocated(request),
    )


@router.get("/owner-kinds/fast", response_model=OwnerKindsFastResponse)
def owner_kinds_fast(_user: UserDep = "") -> OwnerKindsFastResponse:
    """Fast owner-kind availability probe via `which` only."""
    return OwnerKindsFastResponse(
        persistent_kinds=_detect_owner_kinds_fast(_FAST_PERSISTENT_OWNER_KIND_BINARIES),
        temporary_kinds=_detect_owner_kinds_fast(_FAST_TEMP_OWNER_KIND_BINARIES),
    )


@router.get("/workspace-directories", response_model=WorkspaceDirectoryListResponse)
def list_workspace_directories(
    user: UserDep,
    all_users: Annotated[bool, Query(alias="allUsers")] = False,
) -> WorkspaceDirectoryListResponse:
    """List recorded workspace repo directories for Flow agents.

    - In local mode: only current user's recorded directories.
    - In server mode: default to current user; allUsers=true is currently
      disabled until RBAC lands.
    """
    cfg = load_config()
    caps = get_deployment_capabilities(cfg)
    if all_users and not caps.allow_all_users_query:
        raise ApiError(
            "FORBIDDEN",
            "allUsers=true is disabled in server mode until RBAC is enabled",
            status_code=403,
        )
    owner_user = None if all_users else user
    dirs = _collect_workspace_dirs(owner_user=owner_user)
    return WorkspaceDirectoryListResponse(
        deployment_mode=cfg.deployment_mode,
        items=sorted(dirs),
    )


def _collect_workspace_dirs(*, owner_user: str | None) -> set[str]:
    dirs: set[str] = set()
    try:
        storage = get_storage()
        limit = 200
        offset = 0
        while True:
            flows, _total = storage.flow_list(
                owner_user=owner_user, limit=limit, offset=offset,
            )
            if not flows:
                break
            for flow in flows:
                _extract_repo_dirs_from_spec(flow.spec, out=dirs)
            if len(flows) < limit:
                break
            offset += len(flows)
    except Exception:
        # Fallback: read backup JSON files directly. Keeps endpoint usable
        # even when a storage backend is unavailable during maintenance.
        for p in paths.flows_dir().glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if owner_user and data.get("owner_user") != owner_user:
                continue
            _extract_repo_dirs_from_spec(data.get("spec"), out=dirs)
    return dirs


def _extract_repo_dirs_from_spec(spec: object, *, out: set[str]) -> None:
    if not isinstance(spec, dict):
        return
    agents = spec.get("agents")
    if not isinstance(agents, list):
        return
    for a in agents:
        if not isinstance(a, dict):
            continue
        repo = a.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            continue
        # Keep stable absolute paths for UI dedup/display.
        abs_repo = str(Path(repo).expanduser().resolve())
        out.add(abs_repo)


# ──────────────────────────────────────────────────────────────────────
# Update / self-upgrade
# ──────────────────────────────────────────────────────────────────────


class UpdateStatusResponse(_CamelModel):
    enabled: bool
    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    is_prerelease: bool = False
    upgrade_script_url: str = update_check.UPGRADE_SCRIPT_URL


class TriggerUpgradeResponse(_CamelModel):
    started: bool
    target_version: str | None = None
    via: str  # "systemd-run" | "subprocess"


class TriggerUpgradePayload(_CamelModel):
    # When True, proceed even though ACTIVE_DRIVING runs exist (they will be
    # gracefully aborted by the pre-stop drain when the upgrade restarts the
    # service). The WebUI sets this after the user confirms the warning popup.
    confirm_active_runs: bool = False


class ActiveRunView(_CamelModel):
    id: str
    flow_id: str
    status: str
    started_at: str


class ActiveRunsResponse(_CamelModel):
    count: int
    runs: list[ActiveRunView]


@router.get("/active-runs", response_model=ActiveRunsResponse)
async def active_runs(_user: UserDep = "") -> ActiveRunsResponse:
    """List runs that need a live process (ACTIVE_DRIVING) and would be aborted.

    Used by the WebUI upgrade flow and (indirectly, via the DB) by the CLI
    stop/restart guards to warn before a stop terminates in-flight runs.
    Excludes the PRESERVED states (awaiting_user_review/complaint), which
    survive a restart losslessly and are NOT aborted.
    """
    from app.models import iso_utc

    storage = get_storage()
    runs = storage.list_active_driving_runs()
    items = [
        ActiveRunView(
            id=r.id,
            flow_id=r.flow_id,
            status=r.status.value if hasattr(r.status, "value") else str(r.status),
            started_at=iso_utc(r.started_at),
        )
        for r in runs
    ]
    return ActiveRunsResponse(count=len(items), runs=items)


@router.get("/update-status", response_model=UpdateStatusResponse)
async def update_status(
    force: bool = Query(default=False),
    _user: UserDep = "",
) -> UpdateStatusResponse:
    """Report whether a newer stable release is available.

    Never raises on network failure — a flaky check just reports
    ``updateAvailable=false``. Returns ``enabled=false`` when the check is
    disabled in config, or when running a pre-release (beta) build.
    """
    cfg = load_config()
    if not cfg.update_check_enabled:
        return UpdateStatusResponse(
            enabled=False,
            current_version=update_check.__version__,
        )
    status = await asyncio.to_thread(update_check.compute_update_status, force=force)
    return UpdateStatusResponse(
        enabled=not status.is_prerelease,
        current_version=status.current_version,
        latest_version=status.latest_version,
        update_available=status.update_available,
        is_prerelease=status.is_prerelease,
        upgrade_script_url=status.upgrade_script_url,
    )


@router.post("/upgrade", response_model=TriggerUpgradeResponse)
async def trigger_upgrade(
    payload: Annotated[TriggerUpgradePayload, Body()] = TriggerUpgradePayload(),
    _user: UserDep = "",
) -> TriggerUpgradeResponse:
    """Launch the official upgrade script in the background.

    Guards: the check must be enabled, the current build must be a final
    release (we never auto-upgrade pre-release installs), and an update must
    actually be available. Additionally, if any ACTIVE_DRIVING runs are
    in-flight, the caller must confirm (``confirmActiveRuns=true``) — those runs
    will be gracefully aborted by the pre-stop drain when the service restarts.

    The upgrade script ends by restarting the managed service, which would
    kill any child living in this service's cgroup. So we launch it as an
    independent transient systemd unit when available, falling back to a
    fully detached subprocess otherwise.
    """
    cfg = load_config()
    if not cfg.update_check_enabled:
        raise ApiError(
            "UPDATE_CHECK_DISABLED",
            "Update checking is disabled in configuration.",
            status_code=409,
        )
    if not payload.confirm_active_runs:
        active_count = await asyncio.to_thread(
            get_storage().count_active_driving_runs,
        )
        if active_count > 0:
            raise ApiError(
                "ACTIVE_RUNS_PRESENT",
                f"{active_count} run(s) are still executing; confirm to abort "
                "them and upgrade.",
                status_code=409,
                details={"active_runs": active_count},
            )
    status = await asyncio.to_thread(update_check.compute_update_status, force=True)
    if status.is_prerelease:
        raise ApiError(
            "UPGRADE_NOT_ALLOWED",
            "Auto-upgrade is only offered on stable releases.",
            status_code=409,
        )
    if not status.update_available:
        raise ApiError(
            "NO_UPGRADE_AVAILABLE",
            "Already on the latest stable release.",
            status_code=409,
        )

    via = await asyncio.to_thread(_launch_self_upgrade, status.upgrade_script_url)
    logger.info(
        "self_upgrade_triggered",
        via=via,
        target=status.latest_version,
        url=status.upgrade_script_url,
    )
    return TriggerUpgradeResponse(
        started=True,
        target_version=status.latest_version,
        via=via,
    )


def _use_systemd_run() -> bool:
    """True when we can launch a transient user-scoped systemd unit.

    Linux + systemd only. On macOS (launchd) ``systemd-run`` is absent and
    ``XDG_RUNTIME_DIR`` is unset, so this returns False and we take the
    detached-subprocess path below.
    """
    if shutil.which("systemd-run") is None:
        return False
    # systemd-run --user needs a user manager bus; this env var is the
    # canonical signal that one is reachable.
    return bool(os.environ.get("XDG_RUNTIME_DIR"))


def _launch_self_upgrade(script_url: str) -> str:
    """Start the upgrade script detached from this service's lifecycle.

    The upgrade script ends by restarting the managed service, which must not
    take the upgrade process down with it. The required isolation differs by
    platform:

    * **Linux/systemd** — systemd's default ``KillMode=control-group`` would
      kill a plain ``setsid`` child along with the unit on restart, because
      cgroup membership is *not* escaped by ``setsid``. So we launch the
      upgrade as an independent transient unit (``systemd-run --user``) that
      lives outside ``csflow.service``'s cgroup.
    * **macOS/launchd & non-systemd Linux** — there is no cgroup; launchd
      signals only the job's own process group on ``kickstart -k``. A child in
      a fresh session/process group (``start_new_session=True`` → ``setsid``)
      reparents to launchd and survives. So the detached subprocess below is
      sufficient and is the correct path on macOS.

    Returns the mechanism used ("systemd-run" or "subprocess").
    """
    log_path = paths.logs_dir() / "self-upgrade.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    inner = f"curl -fsSL {script_url} | bash"

    if _use_systemd_run():
        argv = [
            "systemd-run",
            "--user",
            "--collect",
            "--unit",
            "csflow-self-upgrade",
            "--setenv",
            f"HOME={os.path.expanduser('~')}",
            "bash",
            "-lc",
            f"{{ {inner} ; }} >>{str(log_path)!r} 2>&1",
        ]
        try:
            subprocess.run(argv, check=True, capture_output=True, text=True)
            return "systemd-run"
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            logger.warning("self_upgrade_systemd_run_failed", error=detail)
            # Fall through to the detached-subprocess path.

    log_handle = open(log_path, "ab")  # noqa: SIM115 — handed to the child
    try:
        subprocess.Popen(  # noqa: S602 — fixed trusted command, shell needed for the pipe
            inner,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        log_handle.close()
    return "subprocess"

