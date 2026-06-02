"""Operational sub-apps for ``csflow``.

Each module exposes a typer ``app`` mounted by ``csflow.app.add_typer``
(see :mod:`app.cli`). They talk to the running backend over HTTP — never
import storage / scheduler internals — so they keep working when the
backend is on a different host.
"""
