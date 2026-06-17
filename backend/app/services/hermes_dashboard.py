"""Ensure the Hermes web dashboard is running (``hermes dashboard``).

Default bind: ``127.0.0.1:9119``. Used by the WebUI "to Hermes" action so
operators are not asked to start the dashboard manually.

"Already running" is decided by an **HTTP health check** (not a bare TCP probe):
we confirm the listener actually answers with the Hermes dashboard HTML, so an
unrelated service squatting on the port is never mistaken for Hermes. If the
default port is occupied by such a foreign service we auto-switch to the next
free port in a small scan range; if none is free we raise a clear error.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from app.logging_setup import get_logger
from app.services import subprocess_registry as _subproc_registry
from app.services.hermes_agents import HermesUnavailable, hermes_executable

logger = get_logger("services.hermes_dashboard")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9119
CHAT_PATH = "/chat"
# Number of consecutive ports to scan from DEFAULT_PORT when the requested port
# is taken by a foreign service (9119..9128).
_PORT_SCAN_RANGE = 10
_START_LOCK = threading.Lock()


def dashboard_url(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}{CHAT_PATH}"


def _port_open(host: str, port: int, *, timeout_sec: float = 0.4) -> bool:
    """Fast TCP check: is *anything* listening on host:port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _looks_like_hermes(body: str) -> bool:
    """Does this HTTP response body look like the Hermes dashboard index?

    The Hermes dashboard serves an SPA index whose bootstrap injects
    ``window.__HERMES_SESSION_TOKEN__`` / ``__HERMES_DASHBOARD_EMBEDDED_CHAT__``
    and a ``<title>Hermes Agent - Dashboard</title>``. Match those markers
    case-insensitively so a random TCP/HTTP service is not mistaken for Hermes.
    """
    low = body.lower()
    return "__hermes_" in low or "hermes agent" in low


def _http_is_hermes(host: str, port: int, *, timeout_sec: float = 1.5) -> bool:
    """HTTP health check: confirm the listener really is the Hermes dashboard."""
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:  # noqa: S310 (loopback)
            body = resp.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        # URLError covers HTTPError (4xx/5xx from a foreign HTTP server) and
        # connection/timeout failures; either way it is not a usable Hermes.
        return False
    return _looks_like_hermes(body)


def _classify(host: str, port: int) -> str:
    """Classify a port as ``free`` | ``hermes`` | ``foreign``."""
    if not _port_open(host, port):
        return "free"
    if _http_is_hermes(host, port):
        return "hermes"
    return "foreign"


def _wait_hermes(host: str, port: int, *, deadline: float) -> bool:
    """Poll until the Hermes dashboard actually serves HTTP (not just binds)."""
    while time.monotonic() < deadline:
        if _classify(host, port) == "hermes":
            return True
        time.sleep(0.4)
    return False


def _spawn_dashboard(
    *, exe: str, host: str, port: int, skip_build: bool, profile: str | None = None
) -> subprocess.Popen:
    # ``-p <profile>`` is a GLOBAL flag and must precede the ``dashboard``
    # subcommand so the dashboard binds to that profile's HERMES_HOME (and thus
    # shows that agent's sessions, not the root home's).
    argv = [exe]
    if profile:
        argv += ["-p", profile]
    argv += ["dashboard", "--no-open", "--host", host, "--port", str(port)]
    if skip_build:
        argv.append("--skip-build")
    return subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# Profile-scoped dashboards we launched, keyed by profile id → (proc, port).
# A foreign/root hermes on a port serves a *different* home, so we cannot reuse
# it for a profile; we must track our own instance per profile.
_PROFILE_DASHBOARDS: dict[str, tuple[subprocess.Popen, int]] = {}


def ensure_hermes_dashboard_url(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    startup_timeout_sec: float = 120.0,
    profile: str | None = None,
) -> str:
    """Start Hermes dashboard if needed; return the chat URL.

    With ``profile=None`` (root home): reuse any already-running Hermes dashboard
    found on the scan range, else spawn one on the first free port.

    With ``profile`` set: launch (and track) a dashboard scoped to that profile
    (``hermes -p <id> dashboard``) on its own port, so the WebUI opens *that
    agent's* sessions. A running root/foreign dashboard is never reused for a
    profile (it serves a different home). Reuses our previously-tracked instance
    for the same profile if it is still alive.

    Raises :class:`HermesUnavailable` if the CLI is missing, every scanned port
    is held by a foreign service, or the dashboard fails to come up in time.
    """
    exe = hermes_executable()
    if not exe:
        raise HermesUnavailable("`hermes` CLI not found on PATH")

    candidates = [port + i for i in range(_PORT_SCAN_RANGE)]

    if profile is None:
        # Reuse pass (no lock): return immediately if Hermes is already serving.
        for cand in candidates:
            if _classify(host, cand) == "hermes":
                return dashboard_url(host=host, port=cand)
    else:
        # Reuse our own tracked instance for this profile if still alive+serving.
        tracked = _PROFILE_DASHBOARDS.get(profile)
        if tracked is not None:
            proc, tport = tracked
            if proc.poll() is None and _classify(host, tport) == "hermes":
                return dashboard_url(host=host, port=tport)
            _PROFILE_DASHBOARDS.pop(profile, None)

    with _START_LOCK:
        if profile is None:
            # Re-check under the lock (another request may have just started it).
            for cand in candidates:
                if _classify(host, cand) == "hermes":
                    return dashboard_url(host=host, port=cand)
        else:
            tracked = _PROFILE_DASHBOARDS.get(profile)
            if tracked is not None and tracked[0].poll() is None and (
                _classify(host, tracked[1]) == "hermes"
            ):
                return dashboard_url(host=host, port=tracked[1])

        # A profile dashboard must get its OWN port — only ``free`` ports are
        # eligible (never reuse a port already serving some other home).
        free_port = next((c for c in candidates if _classify(host, c) == "free"), None)
        if free_port is None:
            raise HermesUnavailable(
                f"No free port for the Hermes dashboard in {port}-{port + _PORT_SCAN_RANGE - 1} "
                f"(all occupied by other services). Free a port or stop the conflicting service."
            )

        deadline = time.monotonic() + startup_timeout_sec
        proc: subprocess.Popen | None = None
        for skip_build in (True, False):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            proc = _spawn_dashboard(
                exe=exe, host=host, port=free_port, skip_build=skip_build, profile=profile
            )
            logger.info(
                "hermes_dashboard_spawn",
                host=host,
                requested_port=port,
                port=free_port,
                skip_build=skip_build,
                profile=profile or "",
                pid=proc.pid,
            )
            if _wait_hermes(host, free_port, deadline=min(deadline, time.monotonic() + remaining)):
                if profile is not None:
                    _PROFILE_DASHBOARDS[profile] = (proc, free_port)
                    _subproc_registry.register(proc)
                return dashboard_url(host=host, port=free_port)
            if proc.poll() is not None and skip_build:
                continue
            break

        if proc is not None and proc.poll() is None:
            proc.terminate()
        raise HermesUnavailable(
            f"Hermes dashboard did not become ready on {host}:{free_port} within "
            f"{int(startup_timeout_sec)}s"
        )
