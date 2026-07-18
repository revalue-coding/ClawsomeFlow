"""``csflow external`` — external execution node collaboration setup.

Sub-commands (all opt-in; nothing here runs at init/upgrade, so the feature
adds zero upgrade-parity surface):

* ``pair-token <name>``        — generate/show an INBOUND pairing credential
  (what a remote ClawsomeFlow presents to delegate Flows to THIS instance).
* ``pair-token-remove <name>`` — revoke an inbound pairing credential.
* ``add-remote <name> <secret>`` — store an OUTBOUND credential for a remote
  instance (referenced from Flow specs via ``external.pairTokenRef``).
* ``remove-remote <name>``     — drop an outbound credential.
* ``list``                     — show inbound + outbound credential names.
* ``expose on|off``            — toggle non-loopback access to
  ``/api/external/*`` (widens the service bind; restart to apply).
* ``callback-url [<url>]``     — show/set the base URL remote executors use
  to call back into this instance.
"""

from __future__ import annotations

import secrets as _secrets

import typer

from app import config as cfg_mod

app = typer.Typer(no_args_is_help=True)


def _save(cfg) -> None:
    cfg_mod.save_config(cfg)


@app.command(name="pair-token")
def pair_token(
    name: str = typer.Argument(..., help="Credential name (e.g. 'machine-b')."),
    rotate: bool = typer.Option(
        False, "--rotate", help="Regenerate even if the name already exists.",
    ),
) -> None:
    """Generate (or show) an inbound pairing credential for /api/external/delegate."""
    cfg = cfg_mod.load_config()
    tokens = dict(cfg.external_pair_tokens or {})
    if name in tokens and not rotate:
        typer.echo(tokens[name])
        return
    secret = _secrets.token_urlsafe(32)
    tokens[name] = secret
    _save(cfg.model_copy(update={"external_pair_tokens": tokens}))
    typer.echo(secret)
    typer.echo(
        "✓ Stored. Configure it on the DELEGATING instance with:\n"
        f"  csflow external add-remote {name} <the-secret-above>",
        err=True,
    )


@app.command(name="pair-token-remove")
def pair_token_remove(
    name: str = typer.Argument(..., help="Credential name to revoke."),
) -> None:
    """Revoke an inbound pairing credential."""
    cfg = cfg_mod.load_config()
    tokens = dict(cfg.external_pair_tokens or {})
    if name not in tokens:
        typer.echo(f"✗ No inbound credential named {name!r}.", err=True)
        raise typer.Exit(code=1)
    tokens.pop(name)
    _save(cfg.model_copy(update={"external_pair_tokens": tokens}))
    typer.echo(f"✓ Revoked inbound credential {name!r}.")


@app.command(name="add-remote")
def add_remote(
    name: str = typer.Argument(..., help="Local reference name (used as pairTokenRef)."),
    secret: str = typer.Argument(..., help="The secret generated on the remote instance."),
) -> None:
    """Store an outbound credential for delegating Flows TO a remote instance."""
    cfg = cfg_mod.load_config()
    targets = dict(cfg.external_remote_targets or {})
    targets[name] = secret.strip()
    _save(cfg.model_copy(update={"external_remote_targets": targets}))
    typer.echo(
        f"✓ Stored outbound credential {name!r}. Reference it from a Flow's "
        f"external node as pairTokenRef={name!r}."
    )


@app.command(name="remove-remote")
def remove_remote(
    name: str = typer.Argument(..., help="Outbound credential name to drop."),
) -> None:
    """Drop an outbound remote credential."""
    cfg = cfg_mod.load_config()
    targets = dict(cfg.external_remote_targets or {})
    if name not in targets:
        typer.echo(f"✗ No outbound credential named {name!r}.", err=True)
        raise typer.Exit(code=1)
    targets.pop(name)
    _save(cfg.model_copy(update={"external_remote_targets": targets}))
    typer.echo(f"✓ Removed outbound credential {name!r}.")


@app.command(name="list")
def list_credentials() -> None:
    """List inbound + outbound credential names (secrets are not printed)."""
    cfg = cfg_mod.load_config()
    typer.echo("Inbound pairing credentials (external_pair_tokens):")
    for name in sorted(cfg.external_pair_tokens or {}):
        typer.echo(f"  - {name}")
    if not cfg.external_pair_tokens:
        typer.echo("  (none)")
    typer.echo("Outbound remote credentials (external_remote_targets):")
    for name in sorted(cfg.external_remote_targets or {}):
        typer.echo(f"  - {name}")
    if not cfg.external_remote_targets:
        typer.echo("  (none)")
    typer.echo(
        f"Expose /api/external to non-loopback callers: "
        f"{'ON' if cfg.external_api_expose else 'off'}"
    )
    typer.echo(f"Callback base URL: {cfg.external_callback_base_url or '(unset)'}")


@app.command()
def expose(
    state: str = typer.Argument(..., help="'on' or 'off'."),
) -> None:
    """Allow (or forbid) non-loopback access to the /api/external/* surface.

    Turning this on also makes ``csflow serve`` bind 0.0.0.0 — every other
    /api path still rejects non-loopback Hosts. Requires a service restart
    (``csflow start``) to take effect. The api_token guard is initialised
    first so widening the bind can never expose an unguarded main surface.
    """
    normalized = state.strip().lower()
    if normalized not in ("on", "off"):
        typer.echo("✗ Expected 'on' or 'off'.", err=True)
        raise typer.Exit(code=1)
    from app.integrations.internal_token import ensure_api_token_initialised

    cfg = cfg_mod.load_config()
    if normalized == "on":
        cfg = ensure_api_token_initialised(cfg)
    _save(cfg.model_copy(update={"external_api_expose": normalized == "on"}))
    typer.echo(
        f"✓ external_api_expose = {normalized}. Restart the service "
        "(csflow start) to apply."
    )


@app.command(name="callback-url")
def callback_url(
    url: str = typer.Argument(
        None,
        help="Base URL remote executors should call back to "
        "(e.g. http://my-host:17017). Omit to show the current value.",
    ),
) -> None:
    """Show or set the callback base URL embedded in outbound dispatches."""
    cfg = cfg_mod.load_config()
    if url is None:
        typer.echo(cfg.external_callback_base_url or "(unset)")
        return
    cleaned = url.strip().rstrip("/") or None
    _save(cfg.model_copy(update={"external_callback_base_url": cleaned}))
    typer.echo(f"✓ external_callback_base_url = {cleaned or '(unset)'}")
