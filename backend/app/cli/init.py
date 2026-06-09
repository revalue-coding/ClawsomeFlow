"""``csflow init`` / ``csflow install`` — one-click install entrypoint.

Deployment modes:

* **local** (default) — single-user; SQLite + in-process locks. Zero
  external infrastructure, just a `pip install`.
* **server** — temporarily disabled for public users.

Unified behavior:
* first-time install (no ``~/.clawsomeflow``): run normal first-deploy flow
  (schema init + optional OpenClaw runtime bootstrap + marker write).
* already deployed (``~/.clawsomeflow`` exists): ``init/install`` directly
  delegates to the same reconcile pipeline as ``csflow upgrade-runtime``.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from app import bootstrap, config as cfg_mod
from app.cli import app
from app.cli._openclaw_runtime import ensure_openclaw_version_compatible_or_exit
from app.cli.deps import render_agent_platform_summary
from app.cli._user_service import ServiceError, restart_and_enable
from app.config import (
    DEFAULT_CLAWTEAM_BOARD_PORT,
    DEFAULT_PORT,
    BrokerConfig,
    Config,
    StorageConfig,
)

console = Console()
_SERVER_MODE_DISABLED_MSG = (
    "--mode server is temporarily disabled and not open to users yet. "
    "Please use --mode local."
)


@app.command(name="install")
@app.command(name="init")
def init(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="HTTP port for the backend."),
    user: str | None = typer.Option(
        None, "--user", "-u", help="Default user (omit to keep current OS user).",
    ),
    board_port: int = typer.Option(
        DEFAULT_CLAWTEAM_BOARD_PORT, "--board-port",
        help="Port for the bundled `clawteam board serve` subprocess.",
    ),
    mode: str = typer.Option(
        "local", "--mode", "-m",
        help="Deployment mode. Currently only 'local' is available; "
             "'server' is temporarily disabled.",
    ),
    pg_url: str | None = typer.Option(
        None, "--pg", "--postgres",
        help="Reserved for future server mode (currently disabled).",
    ),
    redis_url: str | None = typer.Option(
        None, "--redis",
        help="Reserved for future server mode (currently disabled).",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing config.json.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip confirmations.",
    ),
    skip_openclaw: bool = typer.Option(
        False, "--skip-openclaw",
        help="Skip OpenClaw integration bootstrap.",
    ),
    restart_service: bool = typer.Option(
        True,
        "--restart-service/--no-restart-service",
        help="Restart managed background service after install/init finishes.",
    ),
) -> None:
    """Initialise config, then run the unified upgrade pipeline."""
    if mode not in ("local", "server"):
        raise typer.BadParameter(f"--mode must be 'local' or 'server', got {mode!r}")
    if mode == "server":
        raise typer.BadParameter(_SERVER_MODE_DISABLED_MSG)
    if not skip_openclaw:
        ensure_openclaw_version_compatible_or_exit(
            action_label="install/init",
            console=console,
        )

    home_preexisting = cfg_mod.paths.clawsomeflow_home_exists()
    home_path = cfg_mod.paths.clawsomeflow_home_path()
    bootstrap.ensure_data_layout()
    cfg_path = cfg_mod.paths.config_path()

    if home_preexisting and not force:
        console.print(
            f"[dim]Existing deployment detected at {home_path}. "
            "install/init now delegates to the unified upgrade pipeline "
            "(upgrade/redeploy path). Use --force to overwrite config.json "
            "before upgrade.[/dim]"
        )

    storage_cfg = (
        StorageConfig(kind="postgres", url=pg_url) if mode == "server"
        else StorageConfig(kind="sqlite", url=None)
    )
    broker_cfg = (
        BrokerConfig(kind="redis", url=redis_url) if mode == "server"
        else None
    )

    cfg: Config
    if cfg_path.exists() and not force:
        cfg = cfg_mod.load_config()
        cfg = cfg.model_copy(update={
            "deployment_mode": mode,
            "csflow_port": port,
            "clawteam_board_port": board_port,
            "storage": storage_cfg,
            "broker": broker_cfg,
            **({"default_user": user} if user else {}),
        })
    else:
        cfg = Config(
            deployment_mode=mode,
            csflow_port=port,
            clawteam_board_port=board_port,
            storage=storage_cfg,
            broker=broker_cfg,
            **({"default_user": user} if user else {}),
        )
    # Auto-provision the private secrets (HMAC secret for the internal-API
    # loopback + long-lived api_token guarding the public /api). Generated here
    # so they exist before the backend serves; stored only in the private
    # config.json (gitignored, never committed).
    from app.integrations.internal_token import (
        ensure_api_token_initialised,
        ensure_secret_initialised,
    )

    cfg = ensure_secret_initialised(cfg)
    cfg = ensure_api_token_initialised(cfg)
    cfg_mod.save_config(cfg)
    console.print(f"[green]✓[/green] Wrote config: [dim]{cfg_path}[/dim]")
    console.print(
        f"[green]✓[/green] Data home: [dim]{home_path}[/dim]"
    )
    storage_label = (
        f"postgres → [dim]{pg_url}[/dim]" if mode == "server"
        else "sqlite (local)"
    )
    broker_label = f"redis → [dim]{redis_url}[/dim]" if mode == "server" else "in-process"
    console.print(
        f"  [dim]mode[/dim] {mode}   "
        f"[dim]port[/dim] {cfg.csflow_port}   "
        f"[dim]board[/dim] {cfg.clawteam_board_port}   "
        f"[dim]user[/dim] {cfg.default_user}\n"
        f"  [dim]storage[/dim] {storage_label}\n"
        f"  [dim]broker[/dim]  {broker_label}"
    )

    from app import __version__, upgrade as upgrade_mod

    # Existing deployment: install/init acts as a unified upgrade entrypoint.
    if home_preexisting:
        from app.cli.upgrade import _render_report

        if skip_openclaw:
            console.print(
                "[yellow]⚠[/yellow] --skip-openclaw set: unified upgrade will skip "
                "OpenClaw redeploy and per-user runtime material refresh (skills + common cron)."
            )
        try:
            report = upgrade_mod.run_upgrade(
                config=cfg,
                target_version=__version__,
                include_openclaw=not skip_openclaw,
                include_user_agent_skill_refresh=not skip_openclaw,
                include_frontend_build=True,
            )
        except Exception as exc:
            console.print(f"[red]✗[/red] Unified upgrade failed: {exc}")
            raise typer.Exit(code=1)
        _render_report(report)
        if not report.ok:
            raise typer.Exit(code=1)
        if restart_service:
            try:
                restart_and_enable(
                    host="127.0.0.1",
                    port=cfg.csflow_port,
                    non_interactive=False,
                )
            except ServiceError as exc:
                console.print(f"[red]✗ Failed to restart service:[/red] {exc}")
                raise typer.Exit(code=1)
            console.print("[green]✓[/green] Background service restarted.")
        console.print("")
        render_agent_platform_summary(console=console)
        return

    # First-time install: DO NOT route through upgrade pipeline.
    try:
        from app.storage import get_storage

        get_storage(cfg)
        console.print(
            f"[green]✓[/green] {storage_cfg.kind} schema ready"
        )
    except NotImplementedError as exc:
        console.print(
            f"[yellow]⚠[/yellow] Storage backend not yet implemented: {exc}"
        )
        console.print(
            "  PostgreSQL backend lands in P1; for now run with --mode local."
        )
    except Exception as exc:
        console.print(f"[red]✗[/red] Storage init failed: {exc}")
        raise typer.Exit(code=1)

    if skip_openclaw:
        console.print(
            "[yellow]⚠[/yellow] Skipped OpenClaw registration "
            "(re-run without --skip-openclaw to enable OpenClaw agent creation bootstrap)."
        )
    else:
        try:
            from app.integrations.openclaw_install import install_into_openclaw

            result = asyncio.run(install_into_openclaw(config=cfg))
        except FileNotFoundError as exc:
            console.print(
                f"[yellow]⚠[/yellow] OpenClaw not configured ({exc}). "
                "Skipped OpenClaw integration bootstrap."
            )
        except Exception as exc:
            console.print(
                f"[yellow]⚠[/yellow] OpenClaw integration error: {exc}\n"
                "  You can re-run [bold]csflow install[/bold] later to retry."
            )
        else:
            console.print(
                "[green]✓[/green] OpenClaw integration ready: "
                f"gateway chat {'enabled' if result.gateway_chat_endpoint_enabled else 'skipped'}"
            )
            console.print(
                "[dim]Note: this step does not auto-restore removed OpenClaw registrations; "
                "restore manually via the \"Restore Agent\" action in UI.[/dim]"
            )

    try:
        upgrade_mod.write_marker(__version__)
        # Fresh install already reflects the current schema → mark all migrations
        # as applied so they never run later (direction-safe migration ledger).
        upgrade_mod.seed_fresh_migration_ledger()
        console.print(
            f"[green]✓[/green] Version marker → [dim]{__version__}[/dim]"
        )
    except Exception as exc:
        console.print(
            f"[yellow]⚠[/yellow] Could not write version marker: {exc}"
        )

    if restart_service:
        try:
            restart_and_enable(
                host="127.0.0.1",
                port=cfg.csflow_port,
                non_interactive=False,
            )
        except ServiceError as exc:
            console.print(f"[red]✗ Failed to restart service:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print("[green]✓[/green] Background service restarted.")
    console.print("")
    render_agent_platform_summary(console=console)
