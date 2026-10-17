"""Live TUI dispatch must reach the agent as ONE bracketed paste.

Regression: ``clawteam runtime inject`` pasted without ``-p``, so tmux turned
every newline into an Enter and TUIs submitted the dispatch line by line —
Hermes (``busy_input_mode=interrupt``) folded only the first lines (often half
of an upstream summary) into a user message and the trailing ``## Task`` block
never reached the model.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import app.scheduler.sessions.tmux_live as tmux_live_mod
import app.scheduler.sessions.tmux_paste as tmux_paste_mod
from app.integrations.clawteam_cli import CliInvocationError
from app.models import AgentKind, FlowAgent, MergeStrategy, OnFailure
from app.scheduler.controller import _TMUX_TARGET_NOT_FOUND_RE
from app.scheduler.sessions.tmux_live import TmuxLiveSession
from app.scheduler.sessions.tmux_paste import tmux_paste_and_submit

_MULTILINE = "## ClawsomeFlow Dispatch Context\nupstream: a && b <c>\n\n## Task #t1: x\nbody\n"


class _FakeTmux:
    def __init__(self, *, fail: str | None = None, panes: str = "%1\n") -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.fail = fail
        self.panes = panes

    async def __call__(self, args: list[str], *, stdin: bytes | None = None):
        self.calls.append((args, stdin))
        if args[0] == "list-panes":
            return (0, self.panes, "") if self.panes else (1, "", "can't find window: w")
        if args[0] == self.fail:
            return 1, "", f"{args[0]} boom"
        return 0, "", ""


@pytest.fixture
def _no_submit_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_paste_mod, "_SUBMIT_DELAY_SEC", 0)
    monkeypatch.setattr(tmux_paste_mod, "_SECOND_ENTER_DELAY_SEC", 0)


@pytest.mark.usefixtures("_no_submit_delay")
@pytest.mark.asyncio
async def test_paste_is_bracketed_and_carries_full_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux()
    monkeypatch.setattr(tmux_paste_mod, "_tmux", fake)

    await tmux_paste_and_submit("s:w", _MULTILINE, buffer_name="b1")

    verbs = [args[0] for args, _ in fake.calls]
    assert verbs == ["list-panes", "load-buffer", "paste-buffer", "send-keys", "send-keys"]
    load_args, load_stdin = fake.calls[1]
    assert load_args == ["load-buffer", "-b", "b1", "-"]
    # Verbatim (no XML escaping, nothing dropped); only trailing whitespace trimmed.
    assert load_stdin == _MULTILINE.rstrip().encode("utf-8")
    paste_args, _ = fake.calls[2]
    assert paste_args == ["paste-buffer", "-p", "-d", "-b", "b1", "-t", "s:w"]
    assert fake.calls[3][0] == ["send-keys", "-t", "s:w", "Enter"]


@pytest.mark.usefixtures("_no_submit_delay")
@pytest.mark.asyncio
async def test_missing_pane_matches_controller_self_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(panes="")
    monkeypatch.setattr(tmux_paste_mod, "_tmux", fake)

    with pytest.raises(CliInvocationError) as ei:
        await tmux_paste_and_submit("clawteam-t:alice", "hi", buffer_name="b1")

    assert _TMUX_TARGET_NOT_FOUND_RE.search(ei.value.stderr)
    assert [args[0] for args, _ in fake.calls] == ["list-panes"]


@pytest.mark.usefixtures("_no_submit_delay")
@pytest.mark.asyncio
async def test_failed_paste_drops_buffer_and_never_submits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTmux(fail="paste-buffer")
    monkeypatch.setattr(tmux_paste_mod, "_tmux", fake)

    with pytest.raises(CliInvocationError):
        await tmux_paste_and_submit("s:w", "hi", buffer_name="b1")

    verbs = [args[0] for args, _ in fake.calls]
    assert verbs == ["list-panes", "load-buffer", "paste-buffer", "delete-buffer"]


@pytest.mark.asyncio
async def test_tmux_live_dispatch_uses_bracketed_paste_not_runtime_inject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    async def _paste(target: str, text: str, *, buffer_name: str) -> None:
        calls.append((target, text, buffer_name))

    monkeypatch.setattr(tmux_live_mod, "tmux_paste_and_submit", _paste)
    agent = FlowAgent(
        id="h1", kind=AgentKind.hermes, repo="/tmp/x",
        is_leader=False, merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )
    # A bare object as the ClawTeam CLI: any clawteam call would AttributeError.
    session = TmuxLiveSession(agent=agent, team_name="hyflow-x", run_id="r", cli=object())
    session.set_tmux_target_override("clawteam-hyflow-x:@7")

    await session._do_dispatch(message=_MULTILINE, task_id="t1")

    assert len(calls) == 1
    target, text, buffer_name = calls[0]
    assert target == "clawteam-hyflow-x:@7"
    assert text == _MULTILINE
    assert buffer_name.startswith("csflow-dispatch-h1-")


_RAW_RECORDER = textwrap.dedent(
    """
    import os, sys, tty
    out = open(sys.argv[1], "ab", buffering=0)
    fd = sys.stdin.fileno()
    tty.setraw(fd)
    os.write(sys.stdout.fileno(), b"\\x1b[?2004h> ")
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        out.write(chunk)
    """
)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
@pytest.mark.asyncio
async def test_real_tmux_delivers_one_bracketed_paste_then_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Private tmux server — never the default socket. Short dir: the unix
    # socket path must stay under ~108 bytes, which a pytest tmp_path can exceed.
    sock_dir = tempfile.mkdtemp(prefix="cstmux")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", sock_dir)
    script = tmp_path / "rec.py"
    script.write_text(_RAW_RECORDER, encoding="utf-8")
    record = tmp_path / "rec.bin"
    message = "\n".join(f"line {i}: upstream summary" for i in range(30)) + "\n## Task #t1: x\n"

    code, _out, err = await tmux_paste_mod._tmux([
        "new-session", "-d", "-s", "p", "-x", "200", "-y", "50",
        f"{sys.executable} {script} {record}",
    ])
    assert code == 0, err
    try:
        # The ">" is printed only after the recorder went raw + enabled
        # bracketed paste; pasting earlier would hit cooked-mode CR→LF mapping.
        for _ in range(100):
            _code, pane, _err = await tmux_paste_mod._tmux(["capture-pane", "-p", "-t", "p"])
            if ">" in pane:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(f"recorder never became ready: {pane!r}")

        await tmux_paste_and_submit("p", message, buffer_name="b-test")

        expected_body = message.rstrip().replace("\n", "\r").encode("utf-8")
        data = b""
        for _ in range(50):
            data = record.read_bytes()
            if data.endswith(b"\r\r"):
                break
            await asyncio.sleep(0.1)
        assert data == b"\x1b[200~" + expected_body + b"\x1b[201~\r\r"
        _code, buffers, _err = await tmux_paste_mod._tmux(["list-buffers"])
        assert "b-test" not in buffers
    finally:
        await tmux_paste_mod._tmux(["kill-server"])
        shutil.rmtree(sock_dir, ignore_errors=True)
