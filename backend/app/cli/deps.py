"""Dependency check + interactive install hints.

Drives ``csflow doctor`` and the first-time check inside ``csflow start``.
For each required tool we record:

* ``check`` — async/sync probe returning ``Status``.
* ``install_hint`` — distro-aware command(s) the user can copy / let us run.

We deliberately don't try to run package managers ourselves without
explicit consent — `csflow start` prompts the user for each missing
piece and only proceeds with the ones they say yes to.

Required tools:
    python  >= 3.10   (already enforced by pyproject)
    git     >= 2.25
    tmux    >= 2.x
    clawteam with `runtime` subcommand (runtime inject required)

Optional tools:
    node    >= 22.12  (for OpenClaw)
    openclaw (only needed when using OpenClaw agents)
    non-OpenClaw agent CLIs (claude / codex / agent / hermes)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from app.integrations.openclaw_cli import resolve_openclaw_executable
from app.runtime_bins import resolve_binary

console = Console()
_CLAWTEAM_UPSTREAM_GIT_URL = "https://github.com/HKUDS/ClawTeam.git"


@dataclass
class Status:
    name: str
    ok: bool
    found_version: str | None
    detail: str
    install_hint: str


@dataclass
class InstallResult:
    name: str
    ok: bool
    detail: str


@dataclass
class AgentToolStatus:
    kind: str
    label: str
    binary: str
    available: bool
    found_version: str | None
    detail: str


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run *cmd* and return stripped stdout, or None if it failed."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or out.stderr or "").strip()


def _exec(
    cmd: list[str],
    *,
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Run *cmd* and return (ok, output)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, output


def _running_inside_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _as_root_cmd(cmd: list[str], *, non_interactive: bool) -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    if shutil.which("sudo"):
        prefix = ["sudo", "-n"] if non_interactive else ["sudo"]
        return prefix + cmd
    return cmd


def _parse_semver(s: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _meets(version: tuple[int, int, int] | None, minimum: tuple[int, int, int]) -> bool:
    return version is not None and version >= minimum


def _hint_for(tool: str) -> str:
    """Return a copy-paste install command appropriate to the OS."""
    macos = sys.platform == "darwin"
    debianish = shutil.which("apt-get") is not None
    nvm = shutil.which("nvm") is not None or "NVM_DIR" in shutil.os.environ

    if tool == "python":
        return (
            "Install Python 3.10+: pyenv / homebrew / your distro's package manager"
            if macos
            else "Install Python 3.10+: `sudo apt install python3.10 python3.10-venv` "
                 "or pyenv (https://github.com/pyenv/pyenv)"
        )
    if tool == "git":
        return "brew install git" if macos else (
            "sudo apt install -y git" if debianish else "Install git for your OS"
        )
    if tool == "tmux":
        return "brew install tmux" if macos else (
            "sudo apt install -y tmux" if debianish else "Install tmux for your OS"
        )
    if tool == "node":
        return (
            "Use nvm (recommended): "
            "`nvm install 22 && nvm use 22 && nvm alias default 22`"
            if nvm or True
            else "Install Node.js 22+ from https://nodejs.org/"
        )
    if tool == "clawteam":
        return (
            "git clone https://github.com/HKUDS/ClawTeam.git && pip install -U ./ClawTeam "
            "(requires `clawteam runtime` subcommand)"
        )
    if tool == "openclaw":
        return "npm install -g openclaw"
    if tool == "hermes":
        return "pip install hermes-agent"
    if tool == "cursor":
        bootstrap_cmd = _cursor_bootstrap_command()
        if bootstrap_cmd:
            return (
                "Cursor desktop does not always pre-install the `agent` binary. "
                f"Run `{bootstrap_cmd}` once to bootstrap Cursor Agent, "
                "then ensure `~/.local/bin` is in PATH and open a new shell."
            )
        return (
            "Install Cursor CLI and bootstrap Agent once (for example: "
            "`cursor agent --help`; on macOS if `cursor` is missing in PATH, "
            "use `/Applications/Cursor.app/Contents/Resources/app/bin/cursor "
            "agent --help`), then ensure `~/.local/bin` is in PATH. "
            "See https://cursor.com/docs/cli/installation"
        )
    return ""


# ── Checks ────────────────────────────────────────────────────────────


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _cursor_bootstrap_command() -> str | None:
    cursor_bin = shutil.which("cursor")
    if cursor_bin:
        return "cursor agent --help"
    if sys.platform == "darwin":
        mac_cursor = Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor")
        if _is_executable_file(mac_cursor):
            return f"{mac_cursor} agent --help"
    return None


def _probe_cli_readiness(executable: str, *, timeout: float = 2.0) -> tuple[bool, str | None]:
    version = (
        _run([executable, "--version"], timeout=timeout)
        or _run([executable, "version"], timeout=timeout)
        or _run([executable, "-v"], timeout=timeout)
    )
    if version is not None:
        text = version.strip()
        if text:
            return True, text
        return True, f"{Path(executable).name} (version command succeeded)"
    if _run([executable, "--help"], timeout=timeout) is not None:
        return True, f"{Path(executable).name} (reachable; version output unavailable)"
    return False, None


def _agent_runtime_setup_hint(kind: str) -> str:
    if kind == "claude":
        return (
            "Install Claude Code CLI and verify with `claude --version`."
        )
    if kind == "codex":
        return "Install Codex CLI and verify with `codex --version`."
    if kind == "hermes":
        return (
            "Install Hermes CLI (`pip install -U hermes-agent`) and verify with "
            "`hermes --version`."
        )
    if kind == "cursor":
        bootstrap_cmd = _cursor_bootstrap_command()
        if bootstrap_cmd:
            return (
                "Cursor desktop alone is not enough; run "
                f"`{bootstrap_cmd}` once to bootstrap `agent`, then ensure "
                "`~/.local/bin` is in PATH and open a new shell."
            )
        return (
            "Install Cursor CLI and bootstrap Agent once. If `cursor` is not in "
            "PATH on macOS, run `/Applications/Cursor.app/Contents/Resources/app/bin/"
            "cursor agent --help`, then ensure `~/.local/bin` is in PATH."
        )
    return "Install the CLI and verify it with `<tool> --version`."


def _probe_non_openclaw_agent_tool(
    *,
    kind: str,
    label: str,
    binary: str,
) -> AgentToolStatus:
    candidates = [binary]
    resolved: str | None = None
    resolved_name: str | None = None
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            resolved = found
            resolved_name = candidate
            break
    if resolved is None:
        display_binary = "/".join(candidates)
        hint = _agent_runtime_setup_hint(kind)
        return AgentToolStatus(
            kind=kind,
            label=label,
            binary=binary,
            available=False,
            found_version=None,
            detail=f"{display_binary} not found in PATH. {hint}",
        )

    ready, detected = _probe_cli_readiness(resolved, timeout=2.0)
    if not ready:
        hint = _agent_runtime_setup_hint(kind)
        return AgentToolStatus(
            kind=kind,
            label=label,
            binary=binary,
            available=False,
            found_version=None,
            detail=(
                f"found `{resolved_name}` at {resolved}, but command probe failed. "
                f"{hint}"
            ),
        )
    return AgentToolStatus(
        kind=kind,
        label=label,
        binary=binary,
        available=True,
        found_version=detected,
        detail=resolved,
    )


def check_python() -> Status:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 10)
    return Status(
        name="python",
        ok=ok,
        found_version=f"{v.major}.{v.minor}.{v.micro}",
        detail="" if ok else "ClawsomeFlow runtime baseline is Python 3.10+.",
        install_hint=_hint_for("python"),
    )


def check_git() -> Status:
    out = _run(["git", "--version"])
    sem = _parse_semver(out or "")
    ok = _meets(sem, (2, 25, 0))
    return Status(
        name="git", ok=ok,
        found_version=out,
        detail="" if ok else "git ≥ 2.25 required (worktree support).",
        install_hint=_hint_for("git"),
    )


def check_tmux() -> Status:
    out = _run(["tmux", "-V"])
    sem = _parse_semver(out or "")
    ok = sem is not None and sem >= (2, 0, 0)
    return Status(
        name="tmux", ok=ok,
        found_version=out,
        detail="" if ok else "tmux is required by ClawTeam spawn backend.",
        install_hint=_hint_for("tmux"),
    )


def check_node() -> Status:
    out = _run(["node", "--version"])
    if out is None:
        return Status(
            name="node", ok=False, found_version=None,
            detail="Node.js not found — required only if you use OpenClaw agents.",
            install_hint=_hint_for("node"),
        )
    sem = _parse_semver(out)
    ok = _meets(sem, (22, 12, 0))
    return Status(
        name="node", ok=ok,
        found_version=out,
        detail="" if ok else "OpenClaw requires Node.js ≥ 22.12.",
        install_hint=_hint_for("node"),
    )


def check_clawteam() -> Status:
    clawteam_bin = resolve_binary("clawteam")
    if clawteam_bin is None:
        return Status(
            name="clawteam", ok=False, found_version=None,
            detail="`clawteam` CLI not found.",
            install_hint=_hint_for("clawteam"),
        )
    out = _run([clawteam_bin, "--version"])
    if out is None:
        return Status(
            name="clawteam", ok=False, found_version=None,
            detail=f"`clawteam` executable is unusable: {clawteam_bin}",
            install_hint=_hint_for("clawteam"),
        )
    runtime_help = _run([clawteam_bin, "runtime", "--help"])
    ok = runtime_help is not None
    return Status(
        name="clawteam", ok=ok, found_version=out,
        detail="" if ok else (
            "Installed clawteam lacks `runtime` subcommand "
            f"(resolved binary: {clawteam_bin})."
        ),
        install_hint=_hint_for("clawteam"),
    )


def check_openclaw() -> Status:
    openclaw_bin = resolve_openclaw_executable()
    if openclaw_bin is None:
        return Status(
            name="openclaw",
            ok=False,
            found_version=None,
            detail="`openclaw` CLI not found.",
            install_hint=_hint_for("openclaw"),
        )
    out = _run([openclaw_bin, "--version"])
    if out is None:
        return Status(
            name="openclaw", ok=False, found_version=None,
            detail=f"`openclaw` executable is unusable: {openclaw_bin}",
            install_hint=_hint_for("openclaw"),
        )
    return Status(
        name="openclaw", ok=True, found_version=out,
        detail="", install_hint=_hint_for("openclaw"),
    )


def check_hermes() -> Status:
    hermes_bin = shutil.which("hermes")
    if hermes_bin is None:
        return Status(
            name="hermes",
            ok=False,
            found_version=None,
            detail="`hermes` CLI not found — required only if you use Hermes agents.",
            install_hint=_hint_for("hermes"),
        )
    # Presence on PATH == usable. The version string is best-effort only:
    # `hermes --version` runs a synchronous update-check (a git fetch that can
    # take well over the probe timeout on a stale checkout), so gating
    # availability on it made a perfectly usable binary look "unusable" and the
    # UI report "Hermes 不可用". Probe for a version but never fail on it.
    out = (
        _run([hermes_bin, "--version"])
        or _run([hermes_bin, "version"])
    )
    return Status(
        name="hermes", ok=True, found_version=out or "hermes available",
        detail="", install_hint=_hint_for("hermes"),
    )


def check_cursor() -> Status:
    row = _probe_non_openclaw_agent_tool(
        kind="cursor",
        label="Cursor",
        binary="agent",
    )
    return Status(
        name="cursor",
        ok=row.available,
        found_version=row.found_version,
        detail="" if row.available else row.detail,
        install_hint=_hint_for("cursor"),
    )


def _clawteam_install_specs() -> list[tuple[str, str]]:
    """Preferred clawteam install candidates in order."""
    specs: list[tuple[str, str]] = []

    overridden = os.environ.get("CSFLOW_CLAWTEAM_SOURCE", "").strip()
    if overridden:
        override_path = Path(overridden).expanduser()
        if override_path.is_dir():
            specs.append(("override-source", str(override_path)))
        else:
            specs.append(("override-spec", overridden))
    else:
        local_source = Path.home() / "ClawTeam"
        if (local_source / "pyproject.toml").is_file():
            specs.append(("local-source", str(local_source)))

    specs.append(("pypi", "clawteam"))

    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for origin, spec in specs:
        if spec in seen:
            continue
        seen.add(spec)
        deduped.append((origin, spec))
    return deduped


def _install_clawteam_from_upstream_clone(pip_cmd: list[str]) -> tuple[bool, str]:
    """Clone upstream ClawTeam source and install it with pip."""
    clone_root = Path(tempfile.mkdtemp(prefix="csflow-clawteam-src-")).resolve()
    checkout = clone_root / "ClawTeam"
    try:
        ok, out = _exec(
            ["git", "clone", "--depth", "1", _CLAWTEAM_UPSTREAM_GIT_URL, str(checkout)],
            timeout=1800.0,
        )
        if not ok:
            return False, out or "git clone clawteam failed"

        ok, out = _exec([*pip_cmd, "--upgrade", str(checkout)], timeout=1800.0)
        if not ok:
            return False, out or "pip install from cloned clawteam source failed"
        return True, ""
    finally:
        shutil.rmtree(clone_root, ignore_errors=True)


# ── Aggregator ────────────────────────────────────────────────────────


REQUIRED = ("python", "git", "tmux", "clawteam")
OPTIONAL = ("node", "openclaw", "hermes", "cursor")

_CHECKS: dict[str, Callable[[], Status]] = {
    "python": check_python,
    "git": check_git,
    "tmux": check_tmux,
    "node": check_node,
    "clawteam": check_clawteam,
    "openclaw": check_openclaw,
    "hermes": check_hermes,
    "cursor": check_cursor,
}

_NON_OPENCLAW_AGENT_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("claude", "Claude Code", "claude"),
    ("codex", "Codex", "codex"),
    ("cursor", "Cursor", "agent"),
    ("hermes", "Hermes", "hermes"),
)


def run_all() -> dict[str, Status]:
    return {name: fn() for name, fn in _CHECKS.items()}


def render_table(results: dict[str, Status]) -> Table:
    table = Table(title="🦞 ClawsomeFlow — dependency check", show_lines=False)
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("Version", style="dim")
    table.add_column("Notes")
    for name in (*REQUIRED, *OPTIONAL):
        s = results[name]
        required = name in REQUIRED
        if s.ok:
            mark = "[green]✓[/green]"
        elif required:
            mark = "[red]✗ required[/red]"
        else:
            mark = "[yellow]✗ optional[/yellow]"
        notes = s.detail
        if not s.ok:
            notes = f"{notes}\n[dim]→ {s.install_hint}[/dim]".strip()
        table.add_row(name, mark, s.found_version or "—", notes)
    return table


def render_agent_platform_summary(*, console: Console) -> None:
    """Print OpenClaw + non-OpenClaw runtime availability summary."""
    openclaw = check_openclaw()
    if not openclaw.ok:
        console.print(
            "[yellow]OpenClaw is not installed: this does not affect non-OpenClaw agents "
            "(Claude/Codex/Cursor/Hermes), but OpenClaw agents are unavailable "
            "(auto-install is not performed).[/yellow]"
        )
        console.print("")

    tool_rows = check_non_openclaw_agent_tools()
    table = Table(title="Agent Runtime Check")
    table.add_column("Tool", style="bold")
    table.add_column("Status")
    table.add_column("Detected Info", style="dim")

    available_labels: list[str] = []
    missing_labels: list[str] = []
    if openclaw.ok:
        available_labels.append("OpenClaw")
        table.add_row(
            "OpenClaw",
            "[green]Available[/green]",
            openclaw.found_version or "openclaw --version",
        )
    else:
        missing_labels.append("OpenClaw")
        table.add_row(
            "OpenClaw",
            "[yellow]Not installed[/yellow]",
            openclaw.detail or openclaw.install_hint,
        )

    for row in tool_rows:
        if row.available:
            available_labels.append(row.label)
            detected = row.found_version or row.detail
            table.add_row(row.label, "[green]Available[/green]", detected)
        else:
            missing_labels.append(row.label)
            table.add_row(row.label, "[yellow]Unavailable[/yellow]", row.detail)

    console.print(table)
    console.print(
        f"[bold]Currently available agents:[/bold] "
        + (", ".join(available_labels) if available_labels else "(none)")
    )
    console.print(
        f"[bold]Also supported (setup required):[/bold] "
        + (", ".join(missing_labels) if missing_labels else "(none)")
    )
    console.print(
        "[dim]Note: deployment/upgrade does not auto-restore removed OpenClaw registrations; "
        "use the \"Restore Agent\" action in the UI when needed.[/dim]"
    )


def fatal_missing(results: dict[str, Status]) -> list[str]:
    """Return required-but-missing tool names (start refuses to proceed if any)."""
    return [n for n in REQUIRED if not results[n].ok]


def check_non_openclaw_agent_tools() -> list[AgentToolStatus]:
    """Check availability of non-OpenClaw Agent runtimes supported by service."""
    rows: list[AgentToolStatus] = []
    for kind, label, binary in _NON_OPENCLAW_AGENT_TOOLS:
        rows.append(
            _probe_non_openclaw_agent_tool(
                kind=kind,
                label=label,
                binary=binary,
            )
        )
    return rows


def install_tool(name: str, *, non_interactive: bool = False) -> InstallResult:
    """Attempt to install a dependency and then re-check it."""
    if name == "python":
        return InstallResult(
            name=name,
            ok=False,
            detail="Current process Python is fixed; install Python 3.10+ manually and rerun.",
        )

    if name in {"git", "tmux"}:
        if shutil.which("apt-get"):
            cmd = _as_root_cmd(
                ["apt-get", "install", "-y", name],
                non_interactive=non_interactive,
            )
            ok, out = _exec(cmd, timeout=1800.0)
            if not ok:
                return InstallResult(name=name, ok=False, detail=out or f"failed to install {name}")
        elif shutil.which("brew"):
            ok, out = _exec(["brew", "install", name], timeout=1800.0)
            if not ok:
                return InstallResult(name=name, ok=False, detail=out or f"failed to install {name}")
        else:
            return InstallResult(
                name=name,
                ok=False,
                detail=f"No supported package manager for {name}. { _hint_for(name) }",
            )
        check = _CHECKS[name]()
        return InstallResult(name=name, ok=check.ok, detail="" if check.ok else check.detail)

    if name == "clawteam":
        pip_cmd = [sys.executable, "-m", "pip", "install"]
        if not _running_inside_virtualenv():
            # System Python installs should avoid global-site mutation.
            pip_cmd.append("--user")
        failures: list[str] = []
        for origin, spec in _clawteam_install_specs():
            cmd = [*pip_cmd, "--upgrade", spec]
            ok, out = _exec(cmd, timeout=1800.0)
            if not ok:
                failures.append(f"{origin}: {out or 'pip install clawteam failed'}")
                continue

            check = check_clawteam()
            if check.ok:
                return InstallResult(name=name, ok=True, detail="")
            failures.append(
                f"{origin}: {check.detail or 'clawteam runtime command still unavailable'}"
            )

        clone_ok, clone_detail = _install_clawteam_from_upstream_clone(pip_cmd)
        if clone_ok:
            check = check_clawteam()
            if check.ok:
                return InstallResult(name=name, ok=True, detail="")
            failures.append(
                "upstream-clone: "
                f"{check.detail or 'clawteam runtime command still unavailable'}"
            )
        else:
            failures.append(f"upstream-clone: {clone_detail}")
        detail = " ; ".join(failures) if failures else "pip install clawteam failed"
        return InstallResult(name=name, ok=False, detail=detail)

    if name == "node":
        if shutil.which("apt-get"):
            cmd = _as_root_cmd(
                ["apt-get", "install", "-y", "nodejs", "npm"],
                non_interactive=non_interactive,
            )
            ok, out = _exec(cmd, timeout=1800.0)
            if not ok:
                return InstallResult(name=name, ok=False, detail=out or "failed to install nodejs/npm")
        elif shutil.which("brew"):
            ok, out = _exec(["brew", "install", "node"], timeout=1800.0)
            if not ok:
                return InstallResult(name=name, ok=False, detail=out or "failed to install node")
        else:
            return InstallResult(
                name=name,
                ok=False,
                detail=f"No supported package manager for node. { _hint_for(name) }",
            )
        check = check_node()
        return InstallResult(name=name, ok=check.ok, detail="" if check.ok else check.detail)

    if name == "openclaw":
        npm = shutil.which("npm")
        if not npm:
            node_result = install_tool("node", non_interactive=non_interactive)
            if not node_result.ok:
                return InstallResult(
                    name=name,
                    ok=False,
                    detail=(
                        "npm is required to install openclaw and automatic node install failed: "
                        f"{node_result.detail}"
                    ),
                )
            npm = shutil.which("npm")
            if not npm:
                return InstallResult(
                    name=name,
                    ok=False,
                    detail="npm is still missing after node installation attempt.",
                )

        attempts: list[list[str]] = [[npm, "install", "--global", "openclaw"]]
        if hasattr(os, "geteuid") and os.geteuid() != 0 and shutil.which("sudo"):
            sudo_attempt = _as_root_cmd(
                ["npm", "install", "--global", "openclaw"],
                non_interactive=non_interactive,
            )
            if sudo_attempt not in attempts:
                attempts.append(sudo_attempt)

        failures: list[str] = []
        for cmd in attempts:
            ok, out = _exec(cmd, timeout=1800.0)
            if not ok:
                failures.append(out or "npm install openclaw failed")
                continue
            check = check_openclaw()
            if check.ok:
                return InstallResult(name=name, ok=True, detail="")
            failures.append(check.detail or "openclaw command still unavailable")

        return InstallResult(
            name=name,
            ok=False,
            detail=" ; ".join(failures) if failures else "failed to install openclaw",
        )

    if name == "hermes":
        pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "hermes-agent"]
        if not _running_inside_virtualenv():
            pip_cmd.insert(4, "--user")
        ok, out = _exec(pip_cmd, timeout=1800.0)
        if not ok:
            return InstallResult(
                name=name,
                ok=False,
                detail=out or "failed to install hermes-agent",
            )
        check = check_hermes()
        return InstallResult(name=name, ok=check.ok, detail="" if check.ok else check.detail)

    if name == "cursor":
        return InstallResult(
            name=name,
            ok=False,
            detail=(
                "Automatic Cursor installation is not supported. "
                f"{_hint_for('cursor')}"
            ),
        )

    return InstallResult(name=name, ok=False, detail=f"Unknown tool: {name}")


__all__ = [
    "AgentToolStatus",
    "InstallResult",
    "OPTIONAL",
    "REQUIRED",
    "Status",
    "check_clawteam",
    "check_cursor",
    "check_git",
    "check_hermes",
    "check_node",
    "check_non_openclaw_agent_tools",
    "check_openclaw",
    "check_python",
    "check_tmux",
    "fatal_missing",
    "install_tool",
    "render_agent_platform_summary",
    "render_table",
    "run_all",
]
