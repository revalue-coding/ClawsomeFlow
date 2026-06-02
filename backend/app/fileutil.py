"""Atomic file writes and advisory file locking.

Mirrors the behaviour of :mod:`clawteam.fileutil` so cross-process
state synchronisation is consistent with ClawTeam's existing on-disk
data files.

Public API:
* :func:`atomic_write_text` — write a string atomically (mkstemp + rename).
* :func:`atomic_write_json` — convenience wrapper for JSON serialisation.
* :func:`file_locked` — context manager holding an exclusive advisory lock
  on a sidecar ``.lock`` file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if sys.platform == "win32":  # pragma: no cover
    import msvcrt
else:
    import fcntl


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write *content* to *path* atomically.

    A unique temporary file is created via ``mkstemp`` in the same
    directory as *path*, written to, then moved into place with
    ``os.replace``. Readers never see a partially-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
) -> None:
    """Atomically write *data* as JSON to *path*."""
    text = json.dumps(data, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    atomic_write_text(path, text)


@contextmanager
def file_locked(path: Path) -> Iterator[None]:
    """Exclusive advisory lock scoped to *path*.

    Creates (or opens) a sidecar ``<path>.lock`` file and holds an
    exclusive advisory lock for the duration of the ``with`` block.
    Serialises concurrent read-modify-write sequences on the same
    logical file across processes.
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        if sys.platform == "win32":  # pragma: no cover
            pos = fh.tell()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            fh.seek(pos)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":  # pragma: no cover
                pos = fh.tell()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                fh.seek(pos)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
