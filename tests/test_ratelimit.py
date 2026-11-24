from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ledger.plane import PlaneClient, PlaneSettings
from ledger.ratelimit import (
    LocalRateLimiter,
    SharedRateLimiter,
    bucket_name,
    default_state_dir,
    make_shared_limiter,
)
from tests.fakes import FakePlaneServer

TEST_TOKEN = "plane_api_" + "0" * 32
_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent / "plugins" / "forge-ledger")


class _Clock:
    """가짜 시계: sleep이 시각을 앞당긴다."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class TestSharedRateLimiter(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state"
        self.clock = _Clock()

    def _limiter(self, bucket: str = "abc", per_minute: int = 3) -> SharedRateLimiter:
        return SharedRateLimiter(self.dir, bucket, per_minute=per_minute, clock=self.clock.now, sleep=self.clock.sleep)

    def test_bucket_name_hides_token(self) -> None:
        name = bucket_name("http://plane.example/", TEST_TOKEN)
        self.assertRegex(name, r"^[0-9a-f]{16}$")
        self.assertNotIn(TEST_TOKEN, name)
        self.assertEqual(name, bucket_name("http://plane.example", TEST_TOKEN))
        self.assertNotEqual(name, bucket_name("http://plane.example", TEST_TOKEN + "x"))

    def test_two_instances_share_budget(self) -> None:
        a = self._limiter()
        b = self._limiter()
        a.acquire()
        a.acquire()
        b.acquire()
        self.assertEqual(self.clock.slept, [])
        b.acquire()
        self.assertEqual(self.clock.slept, [60.0])

    def test_old_entries_expire(self) -> None:
        a = self._limiter()
        for _ in range(3):
            a.acquire()
        self.clock.t += 61.0
        a.acquire()
        self.assertEqual(self.clock.slept, [])

    def test_corrupt_state_is_treated_as_empty(self) -> None:
        limiter = self._limiter()
        self.dir.mkdir(parents=True, exist_ok=True)
        state = self.dir / "plane-rate-abc.json"
        state.write_text("not json", encoding="utf-8")
        limiter.acquire()
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), [1000.0])

    def test_clock_going_back_does_not_block(self) -> None:
        limiter = self._limiter(per_minute=2)
        self.dir.mkdir(parents=True, exist_ok=True)
        state = self.dir / "plane-rate-abc.json"
        state.write_text(json.dumps([self.clock.t + 120, self.clock.t + 120]), encoding="utf-8")
        limiter.acquire()
        self.assertEqual(self.clock.slept, [])

    def test_state_file_has_no_token_and_mode_0600(self) -> None:
        bucket = bucket_name("http://plane.example", TEST_TOKEN)
        limiter = self._limiter(bucket=bucket)
        limiter.acquire()
        state = self.dir / f"plane-rate-{bucket}.json"
        self.assertNotIn(TEST_TOKEN, state.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        lock = self.dir / f"plane-rate-{bucket}.lock"
        self.assertTrue(lock.exists())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_recreated_lock_file_keeps_mode_0600(self) -> None:
        limiter = self._limiter()
        lock = self.dir / "plane-rate-abc.lock"
        lock.unlink()
        old_umask = os.umask(0)
        try:
            limiter.acquire()
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_real_processes_share_budget(self) -> None:
        script = (
            "import sys\n"
            f"sys.path.insert(0, {_PLUGIN_ROOT!r})\n"
            "from pathlib import Path\n"
            "from ledger.ratelimit import SharedRateLimiter\n"
            "limiter = SharedRateLimiter(Path(sys.argv[1]), 'real', per_minute=4, window=1.0)\n"
            "for _ in range(3):\n"
            "    limiter.acquire()\n"
        )
        started = time.monotonic()
        procs = [
            subprocess.Popen([sys.executable, "-c", script, str(self.dir)], stderr=subprocess.PIPE) for _ in range(2)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.8)


class TestMakeSharedLimiter(unittest.TestCase):
    def test_falls_back_when_state_dir_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x", encoding="utf-8")
            notes: list[str] = []
            limiter = make_shared_limiter(
                "http://plane.example",
                TEST_TOKEN,
                {"FORGE_LEDGER_STATE_DIR": str(blocker)},
                notify=notes.append,
            )
        self.assertIsInstance(limiter, LocalRateLimiter)
        self.assertEqual(notes, ["공유 호출 한도 파일을 쓸 수 없어 프로세스 안 제한만 씁니다"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root는 읽기 전용 폴더에도 쓸 수 있다")
    def test_falls_back_when_state_dir_read_only_with_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "s"
            env = {"FORGE_LEDGER_STATE_DIR": str(state_dir)}
            make_shared_limiter("http://plane.example", TEST_TOKEN, env, notify=lambda _m: None)
            state_dir.chmod(0o500)
            notes: list[str] = []
            try:
                limiter = make_shared_limiter("http://plane.example", TEST_TOKEN, env, notify=notes.append)
                limiter.acquire()
            finally:
                state_dir.chmod(0o700)
        self.assertIsInstance(limiter, LocalRateLimiter)
        self.assertEqual(notes, ["공유 호출 한도 파일을 쓸 수 없어 프로세스 안 제한만 씁니다"])

    def test_uses_shared_limiter_when_state_dir_usable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notes: list[str] = []
            limiter = make_shared_limiter(
                "http://plane.example",
                TEST_TOKEN,
                {"FORGE_LEDGER_STATE_DIR": str(Path(tmp) / "s")},
                notify=notes.append,
            )
        self.assertIsInstance(limiter, SharedRateLimiter)
        self.assertEqual(notes, [])

    def test_default_state_dir_order(self) -> None:
        env = {
            "FORGE_LEDGER_STATE_DIR": "/a/explicit",
            "XDG_STATE_HOME": "/b/xdg",
            "HOME": "/c/home",
        }
        self.assertEqual(default_state_dir(env), Path("/a/explicit"))
        del env["FORGE_LEDGER_STATE_DIR"]
        self.assertEqual(default_state_dir(env), Path("/b/xdg/forge-ledger"))
        del env["XDG_STATE_HOME"]
        self.assertEqual(default_state_dir(env), Path("/c/home/.local/state/forge-ledger"))


class TestPlaneClientLimiter(unittest.TestCase):
    def test_client_uses_injected_limiter(self) -> None:
        class Counting:
            def __init__(self) -> None:
                self.calls = 0

            def acquire(self) -> None:
                self.calls += 1

        server = FakePlaneServer()
        self.addCleanup(server.close)
        settings = PlaneSettings(base_url=server.base_url, workspace="forge", token=TEST_TOKEN)
        counting = Counting()
        client = PlaneClient(settings, limiter=counting)
        client.list_projects()
        self.assertEqual(counting.calls, 1)


if __name__ == "__main__":
    unittest.main()
