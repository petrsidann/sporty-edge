"""
Cross-process advisory lock for ledger files.

The ledger is written by BOTH the Windows scheduled task and manual runs.
Two processes appending or rewriting data/bets.jsonl at the same instant can
interleave lines or lose a settlement.  exclusive_lock() wraps every
read-modify-write and every append so only one process mutates the ledger at
a time.

    with exclusive_lock(path):
        ... read, modify, write ...

Windows uses msvcrt.locking (LK_LOCK retries ~10s then raises); POSIX uses
fcntl.flock (blocking).  Best-effort by design: if the lock cannot be taken
or the platform has no locking primitive, the block still runs unlocked --
a lost race is recoverable, a crashed pick run is not.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def exclusive_lock(path: str | Path) -> Iterator[bool]:
    """Hold an exclusive lock on ``path`` (a sidecar '<path>.lock' file).

    Yields True when the lock was actually acquired, False when the block is
    running unlocked (best-effort mode).
    """
    lock_path = Path(str(path) + ".lock")
    fh = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+")
    except OSError:
        fh = None

    unlock = None
    if fh is not None:
        try:
            fh.seek(0)
            try:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

                def _unlock_msvcrt() -> None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

                unlock = _unlock_msvcrt
            except ImportError:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)  # noqa: E731
        except OSError:
            unlock = None  # contention timeout or unsupported: run unlocked

    acquired = unlock is not None
    try:
        yield acquired
    finally:
        if fh is not None:
            if unlock is not None:
                with contextlib.suppress(OSError):
                    unlock()
            with contextlib.suppress(OSError):
                fh.close()
