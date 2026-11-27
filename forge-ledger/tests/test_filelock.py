from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path

from ledger.filelock import acquire, locked, release

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "forge-ledger"


class FileLockTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "x.lock"

    def _open(self) -> int:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        return fd

    def test_second_holder_is_busy_until_release(self) -> None:
        first, second = self._open(), self._open()
        acquire(first)
        with self.assertRaises(BlockingIOError):
            acquire(second, blocking=False)
        release(first)
        with locked(second, blocking=False):
            with self.assertRaises(BlockingIOError):
                acquire(first, blocking=False)
        acquire(first, blocking=False)
        release(first)

    def test_lock_ignores_file_position(self) -> None:
        first, second = self._open(), self._open()
        os.write(first, b"abc")  # 위치가 끝으로 가도 같은 자리를 잠가야 한다
        with locked(first):
            with self.assertRaises(BlockingIOError):
                acquire(second, blocking=False)

    def test_release_without_lock_is_quiet(self) -> None:
        release(self._open())


class HookImportTest(unittest.TestCase):
    def test_hooks_import_on_this_platform(self) -> None:
        """훅이 모듈을 불러오는 단계에서 죽지 않는다(Windows의 fcntl 부재 회귀 시험)."""
        for name in ("session_start", "pre_tool_use", "stop"):
            proc = subprocess.run(  # nosec B603
                [sys.executable, "-c", f"import hooks.{name}"],
                cwd=_PLUGIN_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
