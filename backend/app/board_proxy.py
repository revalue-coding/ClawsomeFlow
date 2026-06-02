"""Manage a `clawteam board serve` child process for local mode.

Per plan §11.5 / DEV.md: the ClawTeam Board is reused as the per-team
kanban + inbox UI. ``RunDetail`` iframes it under the URL stored in
``Run.clawteamBoardUrl``. Local mode auto-spawns the daemon as a child
of the FastAPI process so the user only thinks about one URL; server
mode reverse-proxies through nginx and never touches a subprocess.

Public API:
* :class:`BoardProxyManager` — start/stop helper used by the FastAPI
  lifespan in local mode.
* :func:`get_board_proxy` — module-level singleton.

Process model:
* Spawn ``clawteam board serve --port {clawteam_board_port} --host
  127.0.0.1`` (no team arg → "show all"; the front-end appends
  ``?team={team}`` per Run).
* Capture stdout/stderr to ``~/.clawsomeflow/.logs/clawteam-board.log``
  (rotated only on restart — the daemon's own output is low-volume).
* On lifespan shutdown, send SIGTERM, then SIGKILL after a 5s grace
  period.
* Local-mode startup now treats board readiness as mandatory. We try to
  auto-recover common issues (stale listeners / transient startup races)
  before returning failure to the app lifespan.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from app import paths
from app.config import Config, load_config
from app.deployment import get_deployment_capabilities
from app.logging_setup import get_logger
from app.runtime_bins import resolve_binary

logger = get_logger("board_proxy")


class BoardProxyManager:
    """Owns a single ``clawteam board serve`` subprocess for local mode."""

    def __init__(self, *, config: Config | None = None) -> None:
        self._cfg = config or load_config()
        self._proc: Optional[subprocess.Popen] = None
        self._log_handle = None
        self._last_error: str | None = None
        self._clawteam_bin: str | None = None

    @property
    def port(self) -> int:
        return self._cfg.clawteam_board_port

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        """Try to spawn the board. Returns True on success."""
        self._last_error = None
        if self.is_running():
            return True
        # Server mode reverse-proxies through nginx and shouldn't spawn here.
        if not get_deployment_capabilities(self._cfg).auto_spawn_board_proxy:
            logger.debug("board_proxy_skip_server_mode")
            self._last_error = "board proxy disabled in server mode"
            return False
        self._clawteam_bin = resolve_binary("clawteam")
        if not self._clawteam_bin:
            self._last_error = "`clawteam` binary not found in PATH"
            logger.warning(
                "board_proxy_clawteam_missing",
                detail="`clawteam` binary not on PATH; iframe will 502 — install with `pip install clawteam`",
            )
            return False

        if not self._verify_board_subcommand():
            return False

        if self._reuse_existing_listener_if_possible():
            return True
        if self._last_error:
            return False

        for attempt in range(1, 4):
            if not self._spawn_board_once():
                time.sleep(0.15 * attempt)
                continue

            if self._wait_until_ready(timeout_seconds=4.0):
                assert self._proc is not None
                logger.info(
                    "board_proxy_started",
                    pid=self._proc.pid,
                    port=self.port,
                    log=str(paths.logs_dir() / "clawteam-board.log"),
                    attempt=attempt,
                )
                self._last_error = None
                return True

            self._last_error = self._startup_failure_detail()
            logger.warning(
                "board_proxy_start_attempt_failed",
                port=self.port,
                attempt=attempt,
                error=self._last_error,
            )
            self._kill_current_proc()
            time.sleep(0.25 * attempt)

        if self._last_error is None:
            self._last_error = "unknown board startup failure"
        logger.error(
            "board_proxy_start_failed",
            port=self.port,
            error=self._last_error,
        )
        self._close_log_handle()
        return False

    def _reuse_existing_listener_if_possible(self) -> bool:
        """Reuse compatible listener; replace when version mismatch is detected."""
        listeners = self._listening_pids_for_port()
        if not listeners:
            return False

        board_pids: list[int] = []
        other: list[str] = []
        for pid in listeners:
            cmdline = self._pid_cmdline(pid)
            lowered = cmdline.lower()
            if (
                "clawteam" in lowered
                and "board" in lowered
                and "serve" in lowered
            ):
                board_pids.append(pid)
            else:
                other.append(f"pid={pid} cmd={cmdline or '(unknown)'}")

        if other:
            self._last_error = (
                f"port {self.port} already occupied by non-clawteam-board listener(s): "
                + "; ".join(other)
            )
            logger.error(
                "board_proxy_port_conflict_non_clawteam_board",
                port=self.port,
                listeners=other,
            )
            return False

        desired_bin = self._clawteam_bin or resolve_binary("clawteam")
        if not desired_bin:
            self._last_error = "`clawteam` binary not found in PATH"
            return False
        desired_version = self._binary_version(desired_bin)

        mismatched: list[tuple[int, str | None, str | None]] = []
        for pid in board_pids:
            running_exe = self._pid_executable(pid)
            running_version = self._binary_version(running_exe)
            if self._board_listener_is_compatible(
                desired_bin=desired_bin,
                desired_version=desired_version,
                running_exe=running_exe,
                running_version=running_version,
            ):
                continue
            mismatched.append((pid, running_exe, running_version))

        if not mismatched:
            logger.info(
                "board_proxy_reusing_existing_listener",
                port=self.port,
                pids=board_pids,
                version=desired_version,
            )
            self._last_error = None
            return True

        replaced = self._replace_mismatched_board_listeners(
            board_pids=board_pids,
            mismatched=mismatched,
            desired_bin=desired_bin,
            desired_version=desired_version,
        )
        if replaced:
            # Caller will continue to spawn the target-version board process.
            self._last_error = None
            return False
        return False

    def _board_listener_is_compatible(
        self,
        *,
        desired_bin: str,
        desired_version: str | None,
        running_exe: str | None,
        running_version: str | None,
    ) -> bool:
        desired_real = Path(desired_bin).expanduser().resolve()
        if running_exe:
            try:
                running_real = Path(running_exe).expanduser().resolve()
            except OSError:
                running_real = None
            if running_real is not None and running_real == desired_real:
                return True
        if desired_version and running_version and desired_version == running_version:
            return True
        # No reliable signature available: keep user's listener instead of forcing restart.
        if running_exe is None and running_version is None:
            return True
        return False

    def _replace_mismatched_board_listeners(
        self,
        *,
        board_pids: list[int],
        mismatched: list[tuple[int, str | None, str | None]],
        desired_bin: str,
        desired_version: str | None,
    ) -> bool:
        for pid, running_exe, running_version in mismatched:
            if not self._pid_owned_by_current_user(pid):
                self._last_error = (
                    f"found incompatible clawteam board listener pid={pid} "
                    "but it is not owned by current user; cannot auto-upgrade"
                )
                logger.error(
                    "board_proxy_version_mismatch_not_owned",
                    port=self.port,
                    pid=pid,
                    running_exe=running_exe,
                    running_version=running_version,
                    desired_bin=desired_bin,
                    desired_version=desired_version,
                )
                return False

        logger.warning(
            "board_proxy_replacing_mismatched_listener",
            port=self.port,
            mismatched=[
                {
                    "pid": pid,
                    "running_exe": running_exe,
                    "running_version": running_version,
                }
                for pid, running_exe, running_version in mismatched
            ],
            desired_bin=desired_bin,
            desired_version=desired_version,
        )

        for pid in board_pids:
            if not self._terminate_pid(pid):
                self._last_error = (
                    f"failed to stop existing clawteam board listener pid={pid}"
                )
                logger.error(
                    "board_proxy_replace_failed_to_stop_pid",
                    port=self.port,
                    pid=pid,
                )
                return False

        remaining = self._listening_pids_for_port()
        if remaining:
            listeners = [
                f"pid={pid} cmd={self._pid_cmdline(pid) or '(unknown)'}"
                for pid in remaining
            ]
            self._last_error = (
                f"failed to replace existing board listener on port {self.port}: "
                + "; ".join(listeners)
            )
            logger.error(
                "board_proxy_replace_port_still_busy",
                port=self.port,
                listeners=listeners,
            )
            return False
        return True

    def _verify_board_subcommand(self) -> bool:
        clawteam_bin = self._clawteam_bin or resolve_binary("clawteam")
        if not clawteam_bin:
            self._last_error = "`clawteam` binary not found in PATH"
            return False
        probe = subprocess.run(
            [clawteam_bin, "board", "serve", "--help"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return True
        self._last_error = (
            "clawteam board command unavailable: "
            + (probe.stderr or "").strip()[:300]
        ).strip()
        logger.warning(
            "board_proxy_clawteam_board_subcommand_missing",
            error=self._last_error,
        )
        return False

    def _spawn_board_once(self) -> bool:
        log_path = paths.logs_dir() / "clawteam-board.log"
        if self._log_handle is None:
            try:
                self._log_handle = open(log_path, "ab", buffering=0)
            except OSError as exc:
                logger.warning("board_proxy_logfile_open_failed", error=str(exc))
                self._log_handle = None

        env = os.environ.copy()
        if self._cfg.clawteam_data_dir:
            env["CLAWTEAM_DATA_DIR"] = self._cfg.clawteam_data_dir
        env.setdefault("CLAWTEAM_USER", self._cfg.default_user)

        argv = [
            self._clawteam_bin or "clawteam",
            "board",
            "serve",
            "--port",
            str(self.port),
            "--host",
            "127.0.0.1",
        ]
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=self._log_handle if self._log_handle else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                env=env,
                # Place into its own process group so SIGTERM kills children too.
                start_new_session=True,
            )
            return True
        except OSError as exc:
            self._last_error = f"spawn failed: {exc}"
            logger.warning("board_proxy_spawn_failed", error=str(exc))
            self._proc = None
            return False

    def _wait_until_ready(self, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._proc is None:
                return False
            if self._proc.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return True
            except OSError:
                time.sleep(0.1)
        return False

    def _startup_failure_detail(self) -> str:
        listeners = self._listening_pids_for_port()
        if self._proc is not None and self._proc.poll() is not None:
            return f"clawteam board exited early (exit_code={self._proc.returncode})"
        if listeners:
            rendered = "; ".join(
                f"pid={pid} cmd={self._pid_cmdline(pid) or '(unknown)'}"
                for pid in listeners
            )
            return f"port {self.port} still occupied ({rendered})"
        return f"timed out waiting for board on 127.0.0.1:{self.port}"

    def _listening_pids_for_port(self) -> list[int]:
        pids: set[int] = set()
        if shutil.which("lsof"):
            proc = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{self.port}", "-sTCP:LISTEN", "-t"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
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
            proc = subprocess.run(
                ["ss", "-ltnp", f"sport = :{self.port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                for pid in re.findall(r"pid=(\d+)", proc.stdout or ""):
                    pids.add(int(pid))
        return sorted(pids)

    def _cleanup_stale_board_processes(self) -> list[int]:
        reclaimed: list[int] = []
        for pid in self._listening_pids_for_port():
            if pid == os.getpid():
                continue
            if not self._pid_owned_by_current_user(pid):
                continue
            cmdline = self._pid_cmdline(pid)
            if "clawteam" not in cmdline.lower():
                continue
            if "board" not in cmdline.lower() or "serve" not in cmdline.lower():
                continue
            if self._terminate_pid(pid):
                reclaimed.append(pid)
        return reclaimed

    def _pid_executable(self, pid: int) -> str | None:
        exe_link = Path(f"/proc/{pid}/exe")
        try:
            if exe_link.exists():
                return str(exe_link.resolve())
        except OSError:
            pass

        cmdline = self._pid_cmdline(pid).strip()
        if not cmdline:
            return None
        first = cmdline.split()[0].strip()
        if "/" not in first:
            return None
        candidate = Path(first).expanduser()
        if not candidate.exists():
            return None
        try:
            return str(candidate.resolve())
        except OSError:
            return str(candidate)

    def _binary_version(self, executable: str | None) -> str | None:
        if not executable:
            return None
        try:
            proc = subprocess.run(
                [executable, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        text = (proc.stdout or proc.stderr or "").strip()
        if not text:
            return None
        return text.splitlines()[0].strip().lower()

    def _pid_cmdline(self, pid: int) -> str:
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            if proc_cmdline.exists():
                raw = proc_cmdline.read_bytes()
                text = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
                if text:
                    return text
        except OSError:
            pass
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
        return ""

    def _pid_owned_by_current_user(self, pid: int) -> bool:
        try:
            return Path(f"/proc/{pid}").stat().st_uid == os.getuid()
        except OSError:
            proc = subprocess.run(
                ["ps", "-o", "uid=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                return False
            uid = (proc.stdout or "").strip()
            return uid.isdigit() and int(uid) == os.getuid()

    def _terminate_pid(self, pid: int) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._pid_exists(pid):
                return True
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if not self._pid_exists(pid):
                return True
            time.sleep(0.1)
        return not self._pid_exists(pid)

    def _pid_exists(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _kill_current_proc(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.terminate()
            except Exception:
                pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _close_log_handle(self) -> None:
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None

    async def stop(self, *, grace_seconds: float = 5.0) -> None:
        """Send SIGTERM, then SIGKILL after *grace_seconds*."""
        if not self.is_running():
            self._close_log_handle()
            return
        proc = self._proc
        assert proc is not None
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Wait for graceful exit.
        for _ in range(int(grace_seconds * 10)):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2.0)
        logger.info("board_proxy_stopped", pid=proc.pid, exit_code=proc.returncode)
        self._proc = None
        self._close_log_handle()


# ── singleton ──────────────────────────────────────────────────────────

_singleton: BoardProxyManager | None = None


def get_board_proxy(config: Config | None = None) -> BoardProxyManager:
    global _singleton
    if _singleton is None:
        _singleton = BoardProxyManager(config=config)
    return _singleton


def reset_board_proxy() -> None:
    global _singleton
    _singleton = None


__all__ = ["BoardProxyManager", "get_board_proxy", "reset_board_proxy"]
