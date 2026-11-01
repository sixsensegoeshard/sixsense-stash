"""Local process lifetime guard, independent of Telegram and bot configuration.

Use one stable lock path for every launch of the same bot. The file deliberately
remains on disk: deleting a locked file could let another process lock a new
inode at the same path. Its existence or contents never establish ownership.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from contextlib import contextmanager


class AlreadyRunning(RuntimeError):
    """Another instance currently holds the requested process lock."""

    def __init__(self, lock_path: str | os.PathLike[str]) -> None:
        self.lock_path = os.fspath(lock_path)
        super().__init__(
            f"Tracker is already running: another instance holds '{self.lock_path}'."
        )


def _acquire(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        # Windows byte-range locks may extend past EOF, so an empty lockfile
        # needs neither a placeholder byte nor writes racing with its owner.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    elif os.name == "posix":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        raise NotImplementedError(f"Process locking is unsupported on {os.name!r}")


def _release(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def single_instance(lock_path: str | os.PathLike[str]) -> Iterator[None]:
    """Hold an exclusive, non-blocking OS lock for the entire ``with`` body.

    Raise AlreadyRunning immediately on contention; propagate other OS errors
    (including permission and path errors). The parent directory must exist.
    Only launches using the same file on a filesystem supporting these locks
    exclude each other; use an absolute path independent of the working directory.

    Normal exit and exceptions unlock and close the descriptor. The OS releases
    it after a crash, including forced termination. Python opens it without exec
    inheritance; avoid forking children while held, since fork inherits locks.
    """
    # Do not truncate or unlink: all contenders must lock the same inode.
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    owner_pid = os.getpid()
    try:
        try:
            _acquire(descriptor)
        except OSError as exc:
            contention = {errno.EACCES, errno.EAGAIN}
            if os.name == "nt":
                contention.add(errno.EDEADLK)
            if exc.errno in contention:
                raise AlreadyRunning(lock_path) from exc
            raise
        try:
            yield
        finally:
            # A forked child must not explicitly unlock its parent's lock.
            if os.getpid() == owner_pid:
                _release(descriptor)
    finally:
        os.close(descriptor)
