"""Managed user-service helpers for ClawsomeFlow runtime.

Design goal:
`csflow start` / `csflow install` / `csflow upgrade` should converge to the
same end state: background service enabled + restarted + auto-start ready.

Backends:
- Linux: systemd --user
- macOS: launchd (LaunchAgents)
"""

from __future__ import annotations

import getpass
import os
import platform
import plistlib
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path

from app.runtime_bins import current_python_bindir, resolve_binary

DEFAULT_SERVICE_NAME = "csflow"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17017
DEFAULT_LAUNCHD_LABEL_PREFIX = "dev.clawsomeflow"
_ENV_PASSTHROUGH_KEYS = (
    "CSFLOW_HOME",
    "CSFLOW_USER",
    "CSFLOW_DISABLE_BOARD",
    "CLAWTEAM_DATA_DIR",
    "CLAWTEAM_USER",
    "OPENCLAW_HOME",
)


class ServiceError(RuntimeError):
    """Raised when managed user-service operations fail."""


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _require_command(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    raise ServiceError(f"Required command not found: {name}")


def _listening_pids_for_port(port: int) -> list[int]:
    """Best-effort discovery of LISTEN pids bound to ``port``."""
    pids: set[int] = set()

    if shutil.which("lsof"):
        proc = _run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
        )
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))
    if pids:
        return sorted(pids)

    if shutil.which("ss"):
        proc = _run(["ss", "-ltnp", f"sport = :{port}"], check=False)
        if proc.returncode == 0:
            for pid in re.findall(r"pid=(\d+)", proc.stdout or ""):
                pids.add(int(pid))
    return sorted(pids)


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_cmdline(pid: int) -> str:
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_cmdline.exists():
            raw = proc_cmdline.read_bytes()
            text = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
            if text:
                return text
    except OSError:
        pass

    proc = _run(["ps", "-p", str(pid), "-o", "command="], check=False)
    if proc.returncode == 0:
        return (proc.stdout or "").strip()
    return ""


def _pid_owned_by_current_user(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}").stat().st_uid == os.getuid()
    except OSError:
        # Fallback for environments without /proc.
        proc = _run(["ps", "-o", "uid=", "-p", str(pid)], check=False)
        if proc.returncode != 0:
            return False
        uid = (proc.stdout or "").strip()
        return uid.isdigit() and int(uid) == os.getuid()


def _looks_like_stale_clawsomeflow_listener(cmdline: str) -> bool:
    normalized = " ".join(cmdline.split()).lower()
    return "uvicorn" in normalized and "app.main:app" in normalized


def _terminate_pid(pid: int, *, grace_seconds: float = 8.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.1)
    return not _pid_exists(pid)


def _cleanup_stale_port_conflicts(port: int) -> list[int]:
    """Kill stale manual uvicorn listeners left by older ClawsomeFlow runs."""
    reclaimed: list[int] = []
    for pid in _listening_pids_for_port(port):
        if pid == os.getpid():
            continue
        if not _pid_owned_by_current_user(pid):
            continue
        cmdline = _pid_cmdline(pid)
        if not _looks_like_stale_clawsomeflow_listener(cmdline):
            continue
        if _terminate_pid(pid):
            reclaimed.append(pid)
    return reclaimed


def _describe_port_listeners(port: int) -> list[str]:
    listeners: list[str] = []
    for pid in _listening_pids_for_port(port):
        cmdline = _pid_cmdline(pid) or "(unknown command)"
        listeners.append(f"pid={pid} cmd={cmdline}")
    return listeners


def service_name() -> str:
    return os.environ.get("CSFLOW_SERVICE_NAME", DEFAULT_SERVICE_NAME)


def _service_manager() -> str:
    forced = os.environ.get("CSFLOW_SERVICE_MANAGER", "").strip().lower()
    if forced:
        if forced in {"systemd", "launchd"}:
            return forced
        raise ServiceError(
            "Invalid CSFLOW_SERVICE_MANAGER; expected 'systemd' or 'launchd'."
        )
    return "launchd" if platform.system() == "Darwin" else "systemd"


def service_status_hint() -> str:
    if _service_manager() == "launchd":
        targets = [_launchd_target_for_domain(d) for d in _launchd_domain_candidates()]
        if len(targets) == 1:
            return f"launchctl print {targets[0]}"
        return " or ".join(f"launchctl print {t}" for t in targets)
    return f"systemctl --user status {service_name()}"


def _dedupe_path_entries(*chunks: str) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        for item in chunk.split(":"):
            entry = item.strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            ordered.append(entry)
    return ":".join(ordered)


def _runtime_environment_map(*, runtime_bin_dir: str | None = None) -> dict[str, str]:
    if os.environ.get("CSFLOW_SERVICE_PATH"):
        path_value = os.environ["CSFLOW_SERVICE_PATH"]
    else:
        path_value = _dedupe_path_entries(
            runtime_bin_dir or "",
            os.environ.get("CSFLOW_RUNTIME_BIN_DIR", ""),
            f"{Path.home()}/.clawsomeflow/.venv/bin",
            f"{Path.home()}/.local/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            os.environ.get("PATH", ""),
        )
    env_map: dict[str, str] = {
        "PATH": path_value
    }
    for key in _ENV_PASSTHROUGH_KEYS:
        value = os.environ.get(key)
        if value:
            env_map[key] = value
    return env_map


def _resolve_csflow_bin() -> str:
    csflow_bin = resolve_binary("csflow")
    if csflow_bin:
        return csflow_bin
    # Last resort: same bindir as current Python runtime.
    sibling = current_python_bindir() / "csflow"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)
    raise ServiceError("Required command not found: csflow")


def _systemd_unit_dir() -> Path:
    configured = os.environ.get("CSFLOW_SYSTEMD_USER_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_unit_path() -> Path:
    return _systemd_unit_dir() / f"{service_name()}.service"


def _launchd_label() -> str:
    configured = os.environ.get("CSFLOW_LAUNCHD_LABEL")
    if configured and configured.strip():
        return configured.strip()
    return f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.{service_name().replace('_', '-')}"


def _launchd_dir() -> Path:
    configured = os.environ.get("CSFLOW_LAUNCHD_USER_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_plist_path() -> Path:
    return _launchd_dir() / f"{_launchd_label()}.plist"


def _launchd_logs_dir() -> Path:
    configured = os.environ.get("CSFLOW_LAUNCHD_LOG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Logs" / "ClawsomeFlow"


def _launchd_domain() -> str:
    candidates = _launchd_domain_candidates()
    return candidates[0]


def _launchd_domain_candidates() -> list[str]:
    configured = os.environ.get("CSFLOW_LAUNCHD_DOMAIN", "").strip()
    if configured:
        return [configured]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    # ``gui/<uid>`` for desktop sessions; ``user/<uid>`` for headless/SSH cases.
    return [f"gui/{uid}", f"user/{uid}"]


def _launchd_target() -> str:
    return f"{_launchd_domain()}/{_launchd_label()}"


def _launchd_target_for_domain(domain: str) -> str:
    return f"{domain}/{_launchd_label()}"


def unit_path() -> Path:
    if _service_manager() == "launchd":
        return _launchd_plist_path()
    return _systemd_unit_path()


def _resource_directives_from_env() -> list[str]:
    directives: list[str] = []
    env_to_unit = (
        ("CSFLOW_SERVICE_SLICE", "Slice"),
        ("CSFLOW_SERVICE_CPU_QUOTA", "CPUQuota"),
        ("CSFLOW_SERVICE_CPU_WEIGHT", "CPUWeight"),
        ("CSFLOW_SERVICE_MEMORY_MAX", "MemoryMax"),
        ("CSFLOW_SERVICE_IO_WEIGHT", "IOWeight"),
    )
    for env_key, unit_key in env_to_unit:
        value = os.environ.get(env_key)
        if value:
            directives.append(f"{unit_key}={value}")
    return directives


def _environment_directives_from_env(*, runtime_bin_dir: str | None = None) -> list[str]:
    directives: list[str] = []
    for key, value in _runtime_environment_map(runtime_bin_dir=runtime_bin_dir).items():
        escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
        directives.append(f'Environment="{key}={escaped}"')
    return directives


def _unit_text(
    *,
    csflow_bin: str,
    host: str,
    port: int,
    runtime_bin_dir: str | None = None,
) -> str:
    body = [
        "[Unit]",
        "Description=ClawsomeFlow backend (local mode)",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        *_environment_directives_from_env(runtime_bin_dir=runtime_bin_dir),
        f"ExecStart={csflow_bin} serve --host {host} --port {port}",
        "Restart=on-failure",
        "RestartSec=3",
        "KillMode=mixed",
        "TimeoutStopSec=30",
        *_resource_directives_from_env(),
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(body)


def _launchd_plist_bytes(
    *,
    csflow_bin: str,
    host: str,
    port: int,
    runtime_bin_dir: str | None = None,
) -> bytes:
    logs_dir = _launchd_logs_dir()
    label = _launchd_label()
    payload = {
        "Label": label,
        "ProgramArguments": [
            csflow_bin,
            "serve",
            "--host",
            host,
            "--port",
            str(port),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": _runtime_environment_map(runtime_bin_dir=runtime_bin_dir),
        "StandardOutPath": str(logs_dir / f"{service_name()}.out.log"),
        "StandardErrorPath": str(logs_dir / f"{service_name()}.err.log"),
    }
    return plistlib.dumps(payload, sort_keys=False)


def ensure_user_service_file(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> Path:
    backend = _service_manager()
    csflow_bin = _resolve_csflow_bin()
    runtime_bin_dir = str(Path(csflow_bin).expanduser().resolve().parent)
    if backend == "launchd":
        _require_command("launchctl")
        logs_dir = _launchd_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        plist_dir = _launchd_dir()
        plist_dir.mkdir(parents=True, exist_ok=True)
        path = _launchd_plist_path()
        desired = _launchd_plist_bytes(
            csflow_bin=csflow_bin,
            host=host,
            port=port,
            runtime_bin_dir=runtime_bin_dir,
        )
        current = path.read_bytes() if path.exists() else b""
        if current != desired:
            path.write_bytes(desired)
        return path

    _require_command("systemctl")
    unit_dir = _systemd_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = _systemd_unit_path()
    desired = _unit_text(
        csflow_bin=csflow_bin,
        host=host,
        port=port,
        runtime_bin_dir=runtime_bin_dir,
    )
    current = path.read_text() if path.exists() else ""
    if current != desired:
        path.write_text(desired)
    return path


def _linger_is_enabled(user: str) -> bool:
    _require_command("loginctl")
    proc = _run(
        ["loginctl", "show-user", user, "-p", "Linger", "--value"],
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip().lower() == "yes"


def ensure_linger(*, user: str | None = None, non_interactive: bool = False) -> None:
    """Ensure loginctl linger is enabled so user services survive reboot."""
    if _service_manager() != "systemd":
        return
    user_name = user or getpass.getuser()
    _require_command("loginctl")
    if _linger_is_enabled(user_name):
        return

    local_try = _run(["loginctl", "enable-linger", user_name], check=False)
    if local_try.returncode == 0 and _linger_is_enabled(user_name):
        return

    sudo_cmd = ["sudo"]
    if non_interactive:
        sudo_cmd.append("-n")
    sudo_cmd.extend(["loginctl", "enable-linger", user_name])
    sudo_try = _run(sudo_cmd, check=False)
    if sudo_try.returncode == 0 and _linger_is_enabled(user_name):
        return

    stderr = (sudo_try.stderr or local_try.stderr).strip()
    raise ServiceError(
        "Failed to enable user linger. "
        "Run `sudo loginctl enable-linger <user>` and retry."
        + (f" Details: {stderr}" if stderr else "")
    )


def stop_if_running() -> bool:
    """Stop the user service if active. Returns whether a stop happened."""
    if _service_manager() == "launchd":
        _require_command("launchctl")
        label = _launchd_label()
        loaded = _run(["launchctl", "list", label], check=False)
        if loaded.returncode != 0:
            return False
        bootout_error = ""
        for domain in _launchd_domain_candidates():
            target = _launchd_target_for_domain(domain)
            bootout = _run(["launchctl", "bootout", target], check=False)
            if bootout.returncode == 0:
                return True
            detail = (bootout.stderr or bootout.stdout or "").strip()
            if detail:
                bootout_error = detail
        unload = _run(["launchctl", "unload", str(_launchd_plist_path())], check=False)
        if unload.returncode == 0:
            return True
        raise ServiceError(
            f"Failed to stop launchd service {label}: "
            f"{(bootout_error or unload.stderr or unload.stdout or '').strip()}"
        )

    _require_command("systemctl")
    svc_name = service_name()
    active = _run(
        ["systemctl", "--user", "is-active", svc_name], check=False,
    )
    if active.returncode == 0 and active.stdout.strip() == "active":
        _run(["systemctl", "--user", "stop", svc_name])
        return True
    return False


def disable_if_enabled() -> bool:
    """Disable managed service auto-start. Returns whether disable happened."""
    if _service_manager() == "launchd":
        _require_command("launchctl")
        disabled_any = False
        for domain in _launchd_domain_candidates():
            target = _launchd_target_for_domain(domain)
            disabled = _run(["launchctl", "disable", target], check=False)
            if disabled.returncode == 0:
                disabled_any = True
        return disabled_any

    _require_command("systemctl")
    svc_name = service_name()
    enabled = _run(["systemctl", "--user", "is-enabled", svc_name], check=False)
    if enabled.returncode == 0:
        _run(["systemctl", "--user", "disable", svc_name])
        return True
    return False


def stop_disable_and_release_port(
    *,
    port: int,
) -> tuple[bool, bool, list[int], list[str]]:
    """Stop service, disable auto-start, reclaim stale listeners on ``port``."""
    stopped = stop_if_running()
    disabled = disable_if_enabled()
    reclaimed = _cleanup_stale_port_conflicts(port)
    remaining = _describe_port_listeners(port)
    return stopped, disabled, reclaimed, remaining


def describe_port_listeners(port: int) -> list[str]:
    """Public wrapper to inspect active listeners on ``port``."""
    return _describe_port_listeners(port)


def restart_and_enable(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, non_interactive: bool = False) -> None:
    """Ensure service file, then restart and enable auto-start."""
    if _service_manager() == "launchd":
        _require_command("launchctl")
        path = ensure_user_service_file(host=host, port=port)
        # Best-effort unload old definition before bootstrap.
        for domain in _launchd_domain_candidates():
            _run(["launchctl", "bootout", _launchd_target_for_domain(domain)], check=False)
        _run(["launchctl", "unload", str(path)], check=False)

        boot_ok = False
        active_domain = _launchd_domain_candidates()[0]
        boot_errors: list[str] = []
        for domain in _launchd_domain_candidates():
            boot = _run(["launchctl", "bootstrap", domain, str(path)], check=False)
            if boot.returncode == 0:
                boot_ok = True
                active_domain = domain
                break
            detail = (boot.stderr or boot.stdout or "").strip()
            if detail:
                boot_errors.append(f"{domain}: {detail}")

        if not boot_ok:
            load = _run(["launchctl", "load", "-w", str(path)], check=False)
            if load.returncode != 0:
                raise ServiceError(
                    "Failed to bootstrap launchd service. "
                    + (" ; ".join(boot_errors) or (load.stderr or load.stdout or "").strip())
                )

        target = _launchd_target_for_domain(active_domain)
        _run(["launchctl", "enable", target], check=False)
        _run(["launchctl", "kickstart", "-k", target], check=False)
        status = _run(["launchctl", "print", target], check=False)
        if status.returncode != 0:
            for domain in _launchd_domain_candidates():
                probe_target = _launchd_target_for_domain(domain)
                status = _run(["launchctl", "print", probe_target], check=False)
                if status.returncode == 0:
                    return
        if status.returncode != 0:
            detail = (status.stderr or status.stdout or "").strip()
            label = _launchd_label()
            if detail:
                raise ServiceError(f"{label} launchd service did not load correctly: {detail}")
            raise ServiceError(f"{label} launchd service did not load correctly.")
        return

    # Linux systemd path.
    svc_name = service_name()
    ensure_user_service_file(host=host, port=port)
    ensure_linger(non_interactive=non_interactive)
    _cleanup_stale_port_conflicts(port)
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", svc_name])
    restart = _run(["systemctl", "--user", "restart", svc_name], check=False)
    if restart.returncode != 0:
        reclaimed = _cleanup_stale_port_conflicts(port)
        if reclaimed:
            restart = _run(["systemctl", "--user", "restart", svc_name], check=False)
    if restart.returncode != 0:
        listeners = _describe_port_listeners(port)
        detail = (restart.stderr or restart.stdout or "").strip()
        if listeners:
            detail = (
                f"{detail} Port {port} listeners: " + "; ".join(listeners)
            ).strip()
        raise ServiceError(
            f"Failed to restart {svc_name}. " + (detail or "unknown error")
        )
    active = _run(
        ["systemctl", "--user", "is-active", svc_name], check=False,
    )
    if active.returncode != 0 or active.stdout.strip() != "active":
        raise ServiceError(
            f"{svc_name} service did not reach active state after restart."
        )

