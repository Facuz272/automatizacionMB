"""One engine run at a time per data directory (cron overlaps, a manual run during the daily one).

A file lock (fcntl.flock) next to state.db. The OS releases it when the process ends, even on a crash
or kill -9, so it never goes stale. It is re-entrant inside one process (`daily` → `pipeline.run`
→ `finalize`), and the holder's pid and command are written in the file for `preflight`.
"""

from __future__ import annotations

import fcntl
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import settings


class RunLocked(RuntimeError):
    pass


_held: dict[str, tuple[object, int]] = {}      # path -> (open file, depth)


def lock_path() -> Path:
    return Path(settings.state_db).with_name("engine.lock")


def holder(path: Path | None = None) -> str | None:
    """Text written by the current holder, or None when nobody holds the lock."""
    path = path or lock_path()
    if not path.exists():
        return None
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            return fh.read().strip() or "otro proceso"
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None


@contextmanager
def hold(what: str = "") -> Iterator[None]:
    path = lock_path()
    key = str(path)
    if key in _held:                                   # re-entrant in this process
        fh, depth = _held[key]
        _held[key] = (fh, depth + 1)
        try:
            yield
        finally:
            fh, depth = _held[key]
            _held[key] = (fh, depth - 1)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        who = fh.read().strip()
        fh.close()
        raise RunLocked(f"Hay otra corrida del motor en curso ({who or 'pid desconocido'}); se cancela esta.") from None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()} · {what or ' '.join(sys.argv[1:])} · {datetime.now(timezone.utc).isoformat()}")
    fh.flush()
    _held[key] = (fh, 1)
    try:
        yield
    finally:
        _, depth = _held[key]
        if depth <= 1:
            del _held[key]
            fh.seek(0)
            fh.truncate()
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        else:
            _held[key] = (fh, depth - 1)
