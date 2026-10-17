"""Deliver a multi-line dispatch into a live TUI pane as ONE bracketed paste.

A plain ``tmux paste-buffer`` writes the buffer as if it were typed and turns
every LF into CR — i.e. an Enter keypress. A TUI therefore submits the
dispatch line by line: Hermes (``busy_input_mode=interrupt``) interrupts on
the first line and folds whatever lines have arrived by then into one
truncated user message, so the ``## Task`` block at the tail never reaches the
model; other TUIs only survive it through their own paste heuristics.

``paste-buffer -p`` wraps the buffer in ``ESC[200~ … ESC[201~`` whenever the
application has enabled bracketed paste (every supported TUI does once its
composer is up — the same path a human ``prefix ]`` / terminal paste takes),
so the whole dispatch is ONE paste event and only the explicit Enter below
submits it. An application that never enables bracketed paste receives the
same bytes as a plain paste, so this is never worse than before.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from app.integrations.clawteam_cli import CLI_TIMEOUT_EXIT_CODE, CliInvocationError

_TMUX_CMD_TIMEOUT_SEC = 30.0
# Same submit cadence ClawTeam's runtime inject used (paste → Enter → Enter);
# the second Enter is a no-op on an empty composer in every supported TUI.
_SUBMIT_DELAY_SEC = 0.5
_SECOND_ENTER_DELAY_SEC = 0.3


async def _tmux(args: list[str], *, stdin: bytes | None = None) -> tuple[int, str, str]:
    argv = ["tmux", *args]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin), timeout=_TMUX_CMD_TIMEOUT_SEC,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return CLI_TIMEOUT_EXIT_CODE, "", f"tmux {args[0]} timed out"
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _raise(args: list[str], code: int, stderr: str) -> None:
    raise CliInvocationError(argv=["tmux", *args], exit_code=code or 1, stderr=stderr)


async def tmux_paste_and_submit(target: str, text: str, *, buffer_name: str) -> None:
    """Paste *text* into *target* as one bracketed paste, then press Enter.

    Raises :class:`CliInvocationError`; a missing pane is reported as
    ``tmux target '<target>' not found`` (the wording the controller's
    dispatch self-heal matches on).
    """
    probe = ["list-panes", "-t", target, "-F", "#{pane_id}"]
    code, out, _err = await _tmux(probe)
    if code != 0 or not out.strip():
        _raise(probe, code, f"tmux target '{target}' not found")

    load = ["load-buffer", "-b", buffer_name, "-"]
    code, _out, err = await _tmux(load, stdin=text.rstrip().encode("utf-8"))
    if code != 0:
        _raise(load, code, err)

    paste = ["paste-buffer", "-p", "-d", "-b", buffer_name, "-t", target]
    try:
        code, _out, err = await _tmux(paste)
        if code != 0:
            _raise(paste, code, err)
    except BaseException:
        with contextlib.suppress(Exception):
            await _tmux(["delete-buffer", "-b", buffer_name])
        raise

    for delay in (_SUBMIT_DELAY_SEC, _SECOND_ENTER_DELAY_SEC):
        await asyncio.sleep(delay)
        enter = ["send-keys", "-t", target, "Enter"]
        code, _out, err = await _tmux(enter)
        if code != 0:
            _raise(enter, code, err)


__all__ = ["tmux_paste_and_submit"]
