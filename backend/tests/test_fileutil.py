"""Tests for :mod:`app.fileutil`."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.fileutil import atomic_write_json, atomic_write_text, file_locked


class TestAtomicWrite:
    def test_text_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "f.txt"
        atomic_write_text(target, "hello world")
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_json_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "f.json"
        atomic_write_json(target, {"a": 1, "b": [2, 3]})
        assert json.loads(target.read_text()) == {"a": 1, "b": [2, 3]}

    def test_no_partial_on_error(self, tmp_path: Path, monkeypatch) -> None:
        """If serialisation fails, the existing file must remain intact."""
        target = tmp_path / "f.json"
        target.write_text("ORIGINAL")

        # Force atomic_write_text to raise mid-flight.
        import app.fileutil as fu

        original = fu.os.replace

        def boom(*_args, **_kwargs):
            raise OSError("simulated")

        monkeypatch.setattr(fu.os, "replace", boom)
        try:
            atomic_write_text(target, "NEW")
        except OSError:
            pass
        # Original content preserved, no leftover .tmp files in directory.
        assert target.read_text() == "ORIGINAL"
        assert not list(tmp_path.glob("*.tmp"))


class TestFileLocked:
    def test_serialises_concurrent_threads(self, tmp_path: Path) -> None:
        """Two threads racing on the same file_locked region must not interleave."""
        path = tmp_path / "shared.txt"
        path.write_text("0")
        increments = 200
        threads = 4

        def worker() -> None:
            for _ in range(increments):
                with file_locked(path):
                    val = int(path.read_text())
                    path.write_text(str(val + 1))

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert int(path.read_text()) == increments * threads
