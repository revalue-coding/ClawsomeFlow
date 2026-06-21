"""Grouped ``csflow --help`` layout (Lifecycle / Operations / Utilities)."""

from __future__ import annotations

import click
from typer.core import TyperGroup

_LIFECYCLE = (
    "start",
    "stop",
    "status",
    "init",
    "install",
    "serve",
    "doctor",
    "upgrade",
    "uninstall",
)
_OPS = ("flows", "runs", "agents", "logs")
_UTIL = ("version", "api-token")

_CSFLOW_EPILOG = """
Common:
  csflow start              Boot / upgrade / restart the local service
  csflow status             Runtime snapshot (version, mode, paths)
  csflow upgrade            Update package + reconcile local data

Uninstall:
  csflow uninstall --yes              Stop service + unregister OpenClaw (keep data)
  csflow uninstall --purge-data       Also delete ~/.clawsomeflow/ (type PURGE to confirm)
  csflow uninstall --purge-data \\
    --i-understand-this-deletes-everything   Non-interactive full wipe (scripts only)

Docs: https://clawsomeflow.com/docs/
"""


class CsflowHelpGroup(TyperGroup):
    """Click group that prints lifecycle / ops / util sections in ``--help``."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_commands(ctx, formatter)
        self.format_epilog(ctx, formatter)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_text(_CSFLOW_EPILOG.strip())

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        commands: list[tuple[str, str]] = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            commands.append((name, cmd.get_short_help_str() or ""))

        by_name = dict(commands)

        def _section(title: str, names: tuple[str, ...]) -> None:
            rows = [(n, by_name[n]) for n in names if n in by_name]
            if not rows:
                return
            with formatter.section(title):
                formatter.write_dl(rows)

        _section("Lifecycle", _LIFECYCLE)
        _section("Operations", _OPS)
        _section("Utilities", _UTIL)

        known = set(_LIFECYCLE) | set(_OPS) | set(_UTIL)
        extra = [(n, h) for n, h in commands if n not in known]
        if extra:
            with formatter.section("Other"):
                formatter.write_dl(extra)
