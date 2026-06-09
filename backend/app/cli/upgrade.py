"""Upgrade commands.

- ``csflow upgrade``: end-user stable package upgrade entrypoint.
- ``csflow upgrade-runtime``: internal data/runtime reconcile pipeline.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import typer
from rich.console import Console
from rich.table import Table

from app import __version__, config as cfg_mod, paths, upgrade
from app.cli import app
from app.cli._openclaw_runtime import ensure_openclaw_version_compatible_or_exit
from app.cli.deps import render_agent_platform_summary
from app.cli._runtime import is_alive, read_pid, remove_pid, stop_process
from app.cli._user_service import ServiceError, restart_and_enable, stop_if_running

console = Console()
_DEFAULT_UPGRADE_SCRIPT_URL = "https://clawsomeflow.com/upgrade.sh"
_UPGRADE_SCRIPT_URL_ENV = "CSFLOW_UPGRADE_SCRIPT_URL"


def _hosted_upgrade_script_url() -> str:
    configured = os.environ.get(_UPGRADE_SCRIPT_URL_ENV, "").strip()
    return configured or _DEFAULT_UPGRADE_SCRIPT_URL


def _run_hosted_stable_upgrader() -> int:
    url = _hosted_upgrade_script_url()
    bash_bin = shutil.which("bash")
    curl_bin = shutil.which("curl")
    if not bash_bin or not curl_bin:
        missing: list[str] = []
        if not bash_bin:
            missing.append("bash")
        if not curl_bin:
            missing.append("curl")
        console.print(
            f"[red]Cannot run hosted upgrader; missing: {', '.join(missing)}.[/red]"
        )
        console.print(
            "Run the stable upgrader directly when tooling is available:\n"
            f"  [bold]curl -fsSL {url} | bash[/bold]"
        )
        return 1

    command = f"{shlex.quote(curl_bin)} -fsSL {shlex.quote(url)} | {shlex.quote(bash_bin)}"
    return subprocess.run([bash_bin, "-lc", command], check=False).returncode


def _run_internal_upgrade_pipeline(
    *,
    yes: bool = True,
    dry_run: bool = False,
    force: bool = False,
    restart_service: bool = True,
) -> None:
    """Legacy internal pipeline: reconcile data/runtime to current package."""
    home_exists = paths.clawsomeflow_home_exists()
    needs, marker = upgrade.needs_upgrade()

    console.print(
        f"[bold]🦞 csflow upgrade[/bold]   "
        f"package=[cyan]{__version__}[/cyan]   "
        f"marker=[cyan]{marker or '(none)'}[/cyan]"
    )

    if not home_exists and not force:
        console.print(
            "[yellow]No existing ~/.clawsomeflow data directory found.[/yellow] "
            "Run [bold]csflow install[/bold] (or [bold]csflow init[/bold]) first."
        )
        raise typer.Exit(code=0)

    ensure_openclaw_version_compatible_or_exit(
        action_label="upgrade-runtime",
        console=console,
    )

    if home_exists and not needs and not force:
        console.print(
            "[dim]Version already current. Running safe re-deploy anyway.[/dim]"
        )

    service_stopped = False
    if restart_service:
        try:
            service_stopped = stop_if_running()
        except ServiceError:
            # Continue with PID fallback; some dev/test environments may not
            # have a user service yet.
            service_stopped = False

    pid = read_pid()
    if pid and is_alive(pid):
        console.print(f"[dim]Stopping running backend (pid {pid}) before upgrade...[/dim]")
        if stop_process(pid, grace_seconds=8.0):
            remove_pid()
        else:
            console.print(
                f"[red]Refusing to upgrade:[/red] failed to stop backend pid {pid}."
            )
            raise typer.Exit(code=1)

    if dry_run:
        action = (
            f"upgrade [cyan]{marker or 'legacy'}[/cyan] → [cyan]{__version__}[/cyan]"
            if needs
            else f"re-deploy [cyan]{__version__}[/cyan] (version unchanged)"
        )
        console.print(
            f"[yellow][dry-run][/yellow] Would {action}"
        )
        console.print(
            "  Steps:\n"
            "    1. Rebuild frontend/dist when running from editable source tree\n"
            "    2. Apply DB migrations (if any in registry for this range)\n"
            "    3. Re-create / migrate schema (idempotent)\n"
            "    4. Re-seed bundled skills into ~/.clawsomeflow/.skills-source/\n"
            "    5. Re-deploy OpenClaw common/tools payloads (optional)\n"
            "    6. Re-deploy per-user agent runtime materials (skills + built-in cron jobs only)\n"
            "       (will NOT auto-restore removed OpenClaw registrations)\n"
            "    7. Write marker → " + __version__ + "\n"
            "    8. Restart managed background service"
        )
        raise typer.Exit(code=0)

    if not yes:
        if needs:
            msg = (
                f"Upgrade [cyan]{marker or 'legacy'}[/cyan] → "
                f"[cyan]{__version__}[/cyan]?"
            )
        else:
            msg = (
                f"Run safe re-deploy on current [cyan]{__version__}[/cyan] data?"
            )
        if not typer.confirm(msg, default=True):
            raise typer.Exit(code=0)

    report = upgrade.run_upgrade(include_frontend_build=True)
    _render_report(report)
    if not report.ok:
        raise typer.Exit(code=1)
    if restart_service:
        try:
            cfg = cfg_mod.load_config()
            restart_and_enable(
                host="127.0.0.1",
                port=cfg.csflow_port,
                non_interactive=yes,
            )
        except ServiceError as exc:
            console.print(f"[red]✗ Upgrade succeeded but service restart failed:[/red] {exc}")
            raise typer.Exit(code=1)
        state_label = "restarted" if service_stopped else "started/restarted"
        console.print(f"[green]✓[/green] Background service {state_label}.")
    console.print("")
    render_agent_platform_summary(console=console)


@app.command(name="upgrade")
def upgrade_cmd() -> None:
    """Upgrade to latest stable release (user-facing command)."""
    console.print(
        "[bold]🦞 csflow upgrade[/bold]   "
        "upgrading to latest stable release via hosted script"
    )
    exit_code = _run_hosted_stable_upgrader()
    raise typer.Exit(code=exit_code)


@app.command(name="upgrade-runtime", hidden=True)
def upgrade_runtime_cmd(
    yes: bool = typer.Option(
        True,
        "--yes/--no-yes",
        help="Auto-confirm upgrade actions (default: yes). Use --no-yes to prompt.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would happen; don't write to disk.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Run upgrade pipeline even without an existing data directory.",
    ),
    restart_service: bool = typer.Option(
        True,
        "--restart-service/--no-restart-service",
        help="Restart managed background service after a successful upgrade.",
    ),
) -> None:
    """Internal reconcile command used by installer/deploy scripts."""
    _run_internal_upgrade_pipeline(
        yes=yes,
        dry_run=dry_run,
        force=force,
        restart_service=restart_service,
    )


def _render_report(report: upgrade.UpgradeReport) -> None:
    t = Table(title="Upgrade report", show_lines=False)
    t.add_column("Step")
    t.add_column("Result")

    t.add_row(
        "from → to",
        f"{report.from_version or '(none)'} → {report.to_version}",
    )
    t.add_row(
        "migrations",
        ", ".join(report.migrations_run) or "(none needed)",
    )
    frontend_result = report.frontend_build_status
    if report.frontend_build_detail:
        frontend_result = f"{frontend_result} ({report.frontend_build_detail})"
    t.add_row("frontend bundle", frontend_result)
    t.add_row(
        "schema",
        "[green]ready[/green]" if report.schema_ready else "[red]failed[/red]",
    )
    t.add_row(
        "skills source",
        "[green]reseeded[/green]" if report.skills_reseeded else "[yellow]skipped[/yellow]",
    )
    t.add_row(
        "openclaw",
        report.openclaw_status or "[yellow]skipped[/yellow]",
    )
    t.add_row(
        "runtime registration restore",
        "manual only (via UI \"Restore Agent\")",
    )
    t.add_row(
        "redeploy",
        "[green]done[/green]" if report.redeploy_performed else "[yellow]partial[/yellow]",
    )
    if report.user_agent_skill_results:
        n_agents = len(report.user_agent_skill_results)
        n_skills = sum(len(v) for v in report.user_agent_skill_results.values())
        t.add_row("user-agent skills", f"refreshed {n_skills} skills across {n_agents} agents")
    else:
        t.add_row("user-agent skills", "(no agents)")
    if report.user_agent_cron_sync_results:
        n_agents = len(report.user_agent_cron_sync_results)
        n_ok = sum(1 for ok in report.user_agent_cron_sync_results.values() if ok)
        t.add_row("user-agent common cron", f"sync ok for {n_ok}/{n_agents} agents")
    else:
        t.add_row("user-agent common cron", "(no agents)")
    t.add_row(
        "marker write",
        "[green]ok[/green]" if report.marker_written else "[yellow]skipped[/yellow]",
    )
    console.print(t)

    if report.repair_warnings:
        console.print(
            "\n[yellow]Optional repairs that did not complete "
            "(the service is fully usable; fix these when convenient):[/yellow]"
        )
        for w in report.repair_warnings:
            console.print(f"  • {w}")

    if report.errors:
        console.print("\n[red]Errors:[/red]")
        for e in report.errors:
            console.print(f"  • {e}")
    else:
        console.print(
            f"\n[bold green]✓ Upgraded to {report.to_version}.[/bold green]"
        )
