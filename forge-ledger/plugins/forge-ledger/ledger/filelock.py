"""여러 프로세스가 함께 쓰는 파일 잠금. POSIX는 flock, Windows는 msvcrt.locking을 쓴다."""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator

_WINDOWS_RETRY_SECONDS = 0.05


def acquire(fd: int, blocking: bool = True) -> None:
    """fd에 배타 잠금을 잡는다. blocking=False에서 이미 잠겨 있으면 BlockingIOError를 낸다."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)  # msvcrt는 현재 위치부터 잠근다. 0번 바이트 하나로 통일한다
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not blocking:
                    raise BlockingIOError(str(exc)) from exc
            time.sleep(_WINDOWS_RETRY_SECONDS)  # LK_LOCK은 10초 뒤 포기하므로 직접 기다린다
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def release(fd: int) -> None:
    """acquire로 잡은 잠금을 푼다. 실패해도 fd를 닫으면 풀리므로 무시한다."""
    try:
        _unlock(fd)
    except OSError:
        pass


@contextmanager
def locked(fd: int, blocking: bool = True) -> Iterator[None]:
    """with 블록 동안 fd에 배타 잠금을 잡는다."""
    acquire(fd, blocking)
    try:
        yield
    finally:
        release(fd)
