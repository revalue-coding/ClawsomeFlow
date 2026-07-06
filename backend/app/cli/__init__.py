"""``csflow`` command-line interface (entry point in pyproject.toml).

Sub-commands (Phase 9 + post-MVP):

Lifecycle:
* ``csflow start``      — dep check + init/upgrade if needed + restart user service
* ``csflow stop``       — kill the running uvicorn (PID file based)
* ``csflow status``     — runtime + on-disk snapshot
* ``csflow init``       — install-or-upgrade entry (unified upgrade pipeline)
* ``csflow install``    — alias of ``csflow init``
* ``csflow serve``      — boot uvicorn (skip init / dep-check)
* ``csflow doctor``     — dependency + configuration audit
* ``csflow upgrade``    — user-facing stable package upgrade + reconcile
* ``csflow uninstall``  — remove OpenClaw integration (``--purge-data`` wipes local data)
* ``csflow version``    — print the package version

Ops:
* ``csflow flows``      — list / show
* ``csflow runs``       — list / start / show / abort / merge
* ``csflow agents``     — list / remove / reinstall-skills
* ``csflow logs``       — tail logs (incl. ``verify-anti-loop``)
"""

from __future__ import annotations

import sys

import typer

from app import __version__, logging_setup
from app.cli._help import CsflowHelpGroup

app = typer.Typer(
    name="csflow",
    help="ClawsomeFlow — vertical agent workflow orchestration platform.",
    no_args_is_help=True,
    add_completion=False,
    cls=CsflowHelpGroup,
)


def _cli_logs_to_stderr() -> bool:
    """Only `csflow serve` should mirror structured logs to stderr."""
    return len(sys.argv) >= 2 and sys.argv[1] == "serve"


logging_setup.configure_logging(to_file=True, to_stderr=_cli_logs_to_stderr())


@app.command()
def version() -> None:
    """Print the ClawsomeFlow version."""
    typer.echo(__version__)


@app.command(name="api-token")
def api_token(
    rotate: bool = typer.Option(
        False, "--rotate", help="Generate a new token (invalidates the old one)."
    ),
) -> None:
    """Show (or rotate) the bearer token guarding the local /api surface.

    External services on this host call the API with this token:
    ``Authorization: Bearer <token>`` against ``http://127.0.0.1:<port>/api``.
    The token lives only in the private ``~/.clawsomeflow/config.json``.
    """
    from app import config as cfg_mod
    from app.integrations.internal_token import ensure_api_token_initialised

    cfg = cfg_mod.load_config()
    if rotate:
        cfg = cfg.model_copy(update={"api_token": None})
    new_cfg = ensure_api_token_initialised(cfg)
    if new_cfg is not cfg or rotate:
        cfg_mod.save_config(new_cfg)
        if rotate:
            typer.echo("✓ Rotated api_token (run `csflow start` to apply).", err=True)
    typer.echo(new_cfg.api_token)


# ── lifecycle commands ────────────────────────────────────────────────

# Importing these registers their @app.command decorators on `app`.
from app.cli import (  # noqa: E402,F401  (side-effect imports)
    init as _init_mod,
    serve as _serve_mod,
    start as _start_mod,
    stop as _stop_mod,
    status as _status_mod,
    doctor as _doctor_mod,
    upgrade as _upgrade_mod,
    uninstall as _uninstall_mod,
    logs as _logs_mod,
)

# ── ops sub-apps ──────────────────────────────────────────────────────

from app.cli.ops import flows as _flows_mod  # noqa: E402
from app.cli.ops import runs as _runs_mod  # noqa: E402
from app.cli.ops import agents as _agents_mod  # noqa: E402

app.add_typer(_flows_mod.app, name="flows", help="Flow CRUD shortcuts.")
app.add_typer(_runs_mod.app, name="runs", help="Run trigger / inspect / abort.")
app.add_typer(_agents_mod.app, name="agents", help="OpenClaw agent management.")
