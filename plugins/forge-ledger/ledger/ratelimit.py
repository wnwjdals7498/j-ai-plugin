from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

_WINDOW_SECONDS = 60.0
_MIN_SLEEP_SECONDS = 0.05
_FALLBACK_NOTICE = "공유 호출 한도 파일을 쓸 수 없어 프로세스 안 제한만 씁니다"
_notified_stderr = False


class RateLimiter(Protocol):
    """Plane 호출 직전에 부르는 호출 한도 제한기."""

    def acquire(self) -> None: ...


class LocalRateLimiter:
    """프로세스 안에서만 호출 횟수를 세는 제한기."""

    def __init__(
        self,
        per_minute: int = 50,
        window: float = _WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute는 1 이상이어야 한다")
        self._per_minute = per_minute
        self._window = window
        self._clock = clock
        self._sleep = sleep
        self._timestamps: deque[float] = deque()

    def _expire(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self._window:
            self._timestamps.popleft()

    def acquire(self) -> None:
        now = self._clock()
        self._expire(now)
        if len(self._timestamps) >= self._per_minute:
            wait = self._window - (now - self._timestamps[0])
            if wait > 0:
                self._sleep(wait)
            now = self._clock()
            self._expire(now)
            if len(self._timestamps) >= self._per_minute:
                self._timestamps.popleft()
        self._timestamps.append(now)


class SharedRateLimiter:
    """잠금 파일과 상태 파일로 여러 프로세스가 호출 한도를 함께 쓰는 제한기(POSIX 전용)."""

    def __init__(
        self,
        state_dir: Path,
        bucket: str,
        per_minute: int = 50,
        window: float = _WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        import fcntl  # POSIX 전용: 없으면 ImportError를 make_shared_limiter가 받는다

        if per_minute < 1:
            raise ValueError("per_minute는 1 이상이어야 한다")
        self._flock = fcntl.flock
        self._lock_ex = fcntl.LOCK_EX
        self._per_minute = per_minute
        self._window = window
        self._clock = clock
        self._sleep = sleep
        self._dir = state_dir
        self._state_path = state_dir / f"plane-rate-{bucket}.json"
        self._lock_path = state_dir / f"plane-rate-{bucket}.lock"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)

    def _read(self) -> list[float]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        values: list[float] = []
        for item in data:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                return []
            values.append(float(item))
        return values

    def _write(self, timestamps: list[float]) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".plane-rate-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(timestamps, fh)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._state_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _try_take(self) -> Optional[float]:
        """한도에 여유가 있으면 한 칸을 잡고 None, 아니면 기다릴 초를 돌려준다."""
        with open(self._lock_path, "a+b") as lock:
            self._flock(lock.fileno(), self._lock_ex)
            now = self._clock()
            timestamps = [t for t in self._read() if now - self._window < t <= now + self._window]
            if len(timestamps) < self._per_minute:
                timestamps.append(now)
                self._write(timestamps)
                return None
            return self._window - (now - min(timestamps))

    def acquire(self) -> None:
        while True:
            wait = self._try_take()
            if wait is None:
                return
            self._sleep(max(wait, _MIN_SLEEP_SECONDS))


def bucket_name(base_url: str, token: str) -> str:
    """토큰을 드러내지 않는 상태 파일 구분자(sha256 앞 16자)."""
    digest = hashlib.sha256((base_url.rstrip("/") + "\n" + token).encode("utf-8")).hexdigest()
    return digest[:16]


def default_state_dir(env: Mapping[str, str]) -> Path:
    explicit = env.get("FORGE_LEDGER_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit)
    xdg = env.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg) / "forge-ledger"
    home = env.get("HOME", "").strip()
    if home:
        return Path(home) / ".local" / "state" / "forge-ledger"
    return Path(os.path.expanduser("~/.local/state/forge-ledger"))


def _notify_stderr_once(message: str) -> None:
    global _notified_stderr
    if _notified_stderr:
        return
    _notified_stderr = True
    print(message, file=sys.stderr)


def make_shared_limiter(
    base_url: str,
    token: str,
    env: Mapping[str, str],
    per_minute: int = 50,
    notify: Optional[Callable[[str], None]] = None,
) -> RateLimiter:
    """공유 제한기를 만들고, 쓸 수 없으면 알린 뒤 프로세스 안 제한기로 돌아간다."""
    try:
        return SharedRateLimiter(default_state_dir(env), bucket_name(base_url, token), per_minute=per_minute)
    except (ImportError, OSError):
        (notify if notify is not None else _notify_stderr_once)(_FALLBACK_NOTICE)
        return LocalRateLimiter(per_minute=per_minute)
