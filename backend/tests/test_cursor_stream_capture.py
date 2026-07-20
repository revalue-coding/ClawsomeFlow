"""Unit tests for Cursor stream-json headless capture (param-fill / decompose)."""

from __future__ import annotations

import json

import pytest

from app.services.task_decompose import capture_cursor_stream_json_result


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream([])
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return int(self.returncode or 0)


@pytest.mark.asyncio
async def test_capture_cursor_stream_json_returns_result_without_waiting_exit() -> None:
    payload = {"type": "result", "subtype": "success", "result": '{"目标事件": "草原游"}'}
    # Trailing junk lines would hang communicate(); stream reader stops at result.
    lines = [
        b'{"type":"system","subtype":"init"}\n',
        (json.dumps(payload) + "\n").encode(),
        b'{"type":"never-read"}\n',
    ]
    text = await capture_cursor_stream_json_result(
        _FakeProc(lines), timeout_sec=5.0, log_context={"purpose": "test"},
    )
    assert text == '{"目标事件": "草原游"}'


@pytest.mark.asyncio
async def test_capture_cursor_stream_json_errors_on_failed_result() -> None:
    payload = {"type": "result", "subtype": "error", "error": "boom"}
    with pytest.raises(RuntimeError, match="boom"):
        await capture_cursor_stream_json_result(
            _FakeProc([(json.dumps(payload) + "\n").encode()]),
            timeout_sec=5.0,
        )
