"""Small cross-platform exclusive file lock for participant CLI state.

The server remains Linux-only, but the dependency-free participant CLI also runs on Windows.
Keep platform imports inside their branches so importing :mod:`glassboard.cli` never requires
``fcntl`` on Windows.
"""
from __future__ import annotations

import os


if os.name == "nt":  # pragma: no cover - exercised by Windows CI
    import msvcrt

    def lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            os.write(fd, b"\0")
        except OSError:
            pass
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
