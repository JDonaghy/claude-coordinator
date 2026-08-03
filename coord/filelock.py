"""``flock``-based advisory file lock, shared by every coord process (#1616).

Extracted verbatim from :mod:`coord.drive`, where it lived while the drive
was the only thing that took ``~/.coord/notify.lock``.  #1616 gave the daemon
its own pipeline clock (:func:`coord.notify.run_drain`), which must serialise
against a live ``coord drive``'s nudge — and "the same lock" is only a real
claim if both sides use the same *class* on the same *path*, not two
independent reimplementations that happen to agree on a filename today.

``coord.drive`` re-exports ``FileLock``/``LockBusy`` from here, so every
existing ``from coord.drive import FileLock`` keeps working and every existing
lock keeps its identity.

Why ``flock(2)`` and not a pidfile: ``flock`` is released by the kernel when
the holding process dies, so a killed drive (or a ``coord-serve`` restart —
exactly the deploy step #1616 requires) can never strand the pipeline behind
a lock nobody holds.  The lock is advisory and per-open-file-description, so
the two callers must both go through this class for it to mean anything.

POSIX-only, like the rest of the fleet's session machinery (see
``coord/interactive.py``'s note on ``fcntl``).
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


class LockBusy(Exception):
    """Someone else holds the lock."""


@dataclass
class FileLock:
    """``flock``-based advisory lock, the Python twin of the bash ``flock -n``."""

    path: Path
    _fd: int | None = field(default=None, init=False, repr=False)

    def acquire(self, timeout: float | None = 0.0) -> None:
        """Take the lock.  ``timeout=0`` is non-blocking; ``None`` blocks forever."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusy(str(self.path)) from exc
                time.sleep(0.25)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire(timeout=None)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def notify_lock_path() -> Path:
    """The one lock that serialises pipeline-advancing side effects.

    A function rather than a module constant so tests (and a relocated
    ``$HOME``) resolve it at call time — a constant captured at import would
    freeze whatever ``Path.home()`` was when the module first loaded, which on
    the daemon is process start.
    """
    return Path.home() / ".coord" / "notify.lock"


def drive_queue_lock_path() -> Path:
    """The lock that keeps ``coord drive-queue tick`` from stacking (#1754).

    Deliberately NOT :func:`notify_lock_path`: a tick fetches the board and
    may spend several seconds waiting for ``coord drive --tmux`` to confirm a
    live session, and holding the pipeline's own lock for that would stall
    ``coord notify``/``run_drain`` for no reason.  A tick advances nothing by
    itself — it launches a drive, and *that* drive takes the notify lock when
    it nudges.

    Same call-time resolution rationale as above.
    """
    return Path.home() / ".coord" / "drive-queue.lock"
