"""Ensure the Hermes web dashboard is running (``hermes dashboard``).

Default bind: ``127.0.0.1:9119``. Used by the WebUI "to Hermes" action so
operators are not asked to start the dashboard manually.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time

from app.logging_setup import get_logger
from app.services.hermes_agents import HermesUnavailable, hermes_executable

logger = get_logger("services.hermes_dashboard")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9119
CHAT_PATH = "/chat"
_START_LOCK = threading.Lock()


def dashboard_url(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}{CHAT_PATH}"


def _port_open(host: str, port: int, *, timeout_sec: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, *, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.4)
    return False


def _spawn_dashboard(*, exe: str, host: str, port: int, skip_build: bool) -> subprocess.Popen:
    argv = [exe, "dashboard", "--no-open", "--host", host, "--port", str(port)]
    if skip_build:
        argv.append("--skip-build")
    return subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_hermes_dashboard_url(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    startup_timeout_sec: float = 120.0,
) -> str:
    """Start Hermes dashboard if needed; return the chat URL."""
    exe = hermes_executable()
    if not exe:
        raise HermesUnavailable("`hermes` CLI not found on PATH")

    url = dashboard_url(host=host, port=port)
    if _port_open(host, port):
        return url

    with _START_LOCK:
        if _port_open(host, port):
            return url

        deadline = time.monotonic() + startup_timeout_sec
        proc: subprocess.Popen | None = None
        for skip_build in (True, False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            proc = _spawn_dashboard(exe=exe, host=host, port=port, skip_build=skip_build)
            logger.info(
                "hermes_dashboard_spawn",
                host=host,
                port=port,
                skip_build=skip_build,
                pid=proc.pid,
            )
            if _wait_port(host, port, deadline=min(deadline, time.monotonic() + remaining)):
                return url
            if proc.poll() is not None and skip_build:
                continue
            break

        if proc is not None and proc.poll() is None:
            proc.terminate()
        raise HermesUnavailable(
            f"Hermes dashboard did not become ready on {host}:{port} within "
            f"{int(startup_timeout_sec)}s"
        )
