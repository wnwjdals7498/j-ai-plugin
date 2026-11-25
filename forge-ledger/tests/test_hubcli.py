from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from ledger import hubcli
from ledger.cli import main
from ledger.config import LedgerConfig, PlaneProject, save_config
from ledger.hubstore import load_registry
from ledger.hubsync import sync_lock
from ledger.ratelimit import RateLimiter, make_shared_limiter
from tests.fakes import FakePlaneServer
from tests.hub_fakes import FakeGiteaServer

_SCRIPT = Path(__file__).resolve().parent.parent / "plugins" / "forge-ledger" / "scripts" / "ledger.py"
_EMPTY_TEXT = "등록된 저장소 없음 — ledger sync로 등록하세요"
_BUSY_TEXT = "다른 대조가 진행 중입니다"


class _CountingLimiter:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self) -> None:
        self.acquired += 1


class HubCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.state_dir = self.tmp / "state"
        self.gitea = FakeGiteaServer()
        self.addCleanup(self.gitea.close)
        self.gitea.issues = [
            {"number": n, "title": f"이슈 {n}", "labels": [], "created_at": "2026-11-20T00:00:00Z"} for n in (1, 2, 3)
        ]
        self.plane = FakePlaneServer()
        self.addCleanup(self.plane.close)
        self.plane.projects = {"p1": {"id": "p1", "identifier": "HUB", "name": "허브"}}
        self.plane.cycles = {
            "p1": [
                {"id": "c1", "name": "허브 골격", "end_date": "2026-11-29", "total_issues": 8, "completed_issues": 3}
            ]
        }
        self.gitea_token = "tok" + "g" * 30
        self.plane_token = "tok" + "p" * 30
        self.env = {
            "FORGE_LEDGER_STATE_DIR": str(self.state_dir),
            "FORGE_LEDGER_GITEA_URL": self.gitea.base_url,
            "FORGE_LEDGER_GITEA_TOKEN": self.gitea_token,
            "FORGE_LEDGER_PLANE_URL": self.plane.base_url,
            "FORGE_LEDGER_PLANE_TOKEN": self.plane_token,
        }
        self.repo = self._git_repo("work", "git@host:o/n.git")
        self._write_ledger(self.repo)

    def _git_repo(self, name: str, remote: str) -> Path:
        root = self.tmp / name
        root.mkdir()
        for command in (["git", "init", "-q"], ["git", "remote", "add", "origin", remote]):
            subprocess.run(command, cwd=root, check=True, capture_output=True)
        return root

    def _write_ledger(self, root: Path) -> None:
        config = LedgerConfig(method_version=1, workspace="forge", projects=[PlaneProject("HUB", "허브", True)])
        save_config(config, root / ".ledger" / "ledger.yml")

    def _cli(self, *args: str, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        previous = os.getcwd()
        os.chdir(cwd if cwd is not None else self.tmp)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(list(args), env=self.env if env is None else env)
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue()

    def _status(self) -> dict[str, Any]:
        code, out, _ = self._cli("status", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        return {repo["repo"]: repo for repo in data["repos"]}

    def test_status_empty_message(self) -> None:
        code, out, err = self._cli("status")
        self.assertEqual((code, out, err), (0, _EMPTY_TEXT + "\n", ""))

    def test_sync_then_status_json(self) -> None:
        code, out, err = self._cli("sync", "--repo", str(self.repo))
        self.assertEqual(code, 0, err)
        self.assertIn("o/n gitea: ok", out)
        self.assertIn("o/n plane: ok", out)
        logs = [json.loads(line) for line in err.splitlines()]
        self.assertEqual([(line["source"], line["result"]) for line in logs], [("gitea", "ok"), ("plane", "ok")])
        self.assertEqual(load_registry(self.state_dir), [str(self.repo)])

        repo = self._status()["o/n"]
        self.assertEqual(repo["open_issues"], 3)
        cycle = repo["projects"][0]["cycle"]
        self.assertEqual((cycle["name"], cycle["completed"], cycle["total"]), ("허브 골격", 3, 8))
        self.assertEqual(
            {name: item["state"] for name, item in repo["sources"].items()}, {"gitea": "ok", "plane": "ok"}
        )

        code, text, _ = self._cli("status")
        self.assertEqual(code, 0)
        self.assertIn("열린 결함 (Gitea): 3", text)

        stored = [path.read_bytes() for path in self.state_dir.iterdir()]
        for token in (self.gitea_token, self.plane_token):
            self.assertNotIn(token, out + err + text)
            for content in stored:
                self.assertNotIn(token.encode(), content)

    def test_sync_quiet_has_no_stdout(self) -> None:
        code, out, err = self._cli("sync", "--repo", str(self.repo), "--quiet")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(len(err.splitlines()), 2)

    def test_sync_lock_busy_exit_zero(self) -> None:
        with sync_lock(self.state_dir) as acquired:
            self.assertTrue(acquired)
            code, out, _ = self._cli("sync", "--repo", str(self.repo))
            quiet = self._cli("sync", "--repo", str(self.repo), "--quiet")
        self.assertEqual((code, out), (0, _BUSY_TEXT + "\n"))
        self.assertEqual(quiet[:2], (0, ""))
        self.assertEqual(self.gitea.requests, [])
        self.assertEqual(load_registry(self.state_dir), [])

    def test_sync_failed_exit_two(self) -> None:
        self.gitea.status_queue = [401] * 4
        code, out, err = self._cli("sync", "--repo", str(self.repo))
        self.assertEqual(code, 2, err)
        self.assertIn("o/n gitea: failed (auth)", out)
        self.assertEqual(self._status()["o/n"]["sources"]["gitea"]["state"], "failed")

    def test_sync_unresolvable_remote_exit_one(self) -> None:
        broken = self._git_repo("broken", "https://host/only")
        for args, cwd in ((("sync", "--repo", str(broken)), None), (("sync",), broken)):
            code, out, err = self._cli(*args, cwd=cwd)
            self.assertEqual((code, out), (1, ""), args)
            self.assertIn("원격 주소", err)
        self.assertEqual(load_registry(self.state_dir), [])
        self.assertEqual(self._status(), {})

    def test_sync_without_repo_registers_cwd_and_syncs_all(self) -> None:
        self.assertEqual(self._cli("sync", "--repo", str(self.repo))[0], 0)
        other = self._git_repo("other", "git@host:o/other.git")
        (other / "sub").mkdir()
        code, out, err = self._cli("sync", cwd=other / "sub")
        self.assertEqual(code, 0, err)
        self.assertEqual(load_registry(self.state_dir), sorted([str(self.repo), str(other)]))
        self.assertIn("o/n gitea: ok", out)
        self.assertIn("o/other gitea: ok", out)

    def test_sync_with_repo_syncs_only_that_repo(self) -> None:
        other = self._git_repo("other", "git@host:o/other.git")
        self.assertEqual(self._cli("sync", "--repo", str(other))[0], 0)
        code, out, _ = self._cli("sync", "--repo", str(self.repo))
        self.assertEqual(code, 0)
        self.assertNotIn("o/other", out)

    def test_sync_uses_ledger_folder_inside_larger_repo(self) -> None:
        mono = self._git_repo("mono", "git@host:o/n.git")
        nested = mono / "tool"
        (nested / "src").mkdir(parents=True)
        self._write_ledger(nested)
        for args, cwd in ((("sync", "--repo", str(nested / "src")), None), (("sync",), nested / "src")):
            code, out, err = self._cli(*args, cwd=cwd)
            self.assertEqual(code, 0, err)
            self.assertIn("o/n plane: ok", out)
            self.assertEqual(load_registry(self.state_dir), [str(nested)])

    def test_registration_keeps_workspace_of_other_path(self) -> None:
        mono = self._git_repo("mono", "git@host:o/n.git")
        nested = mono / "tool"
        self._write_ledger(nested)
        self.assertEqual(self._cli("sync", "--repo", str(nested))[0], 0)
        # 등록한 뒤 대조 전에 실패해도 등록 단계가 Plane 설정을 지우지 않는다.
        env = {**self.env, "FORGE_LEDGER_GITEA_URL": "no-scheme"}
        self.assertEqual(self._cli("sync", "--repo", str(mono), env=env)[0], 1)
        self.assertEqual(list(self._status()["o/n"]["sources"]), ["gitea", "plane"])

    def test_sync_outside_git_syncs_registered_only(self) -> None:
        code, out, _ = self._cli("sync")
        self.assertEqual((code, out), (0, _EMPTY_TEXT + "\n"))
        self.assertEqual(self._cli("sync", "--repo", str(self.repo))[0], 0)
        code, out, _ = self._cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("o/n gitea: ok", out)
        self.assertEqual(load_registry(self.state_dir), [str(self.repo)])

    def test_sync_skips_source_without_token(self) -> None:
        env = {key: value for key, value in self.env.items() if key != "FORGE_LEDGER_GITEA_TOKEN"}
        code, out, err = self._cli("sync", "--repo", str(self.repo), env=env)
        self.assertEqual(code, 0, err)
        self.assertIn("o/n gitea: skipped (no_token)", out)
        self.assertEqual(self.gitea.requests, [])

    def test_sync_bad_url_exit_one(self) -> None:
        env = {**self.env, "FORGE_LEDGER_GITEA_URL": "no-scheme"}
        code, _, err = self._cli("sync", "--repo", str(self.repo), env=env)
        self.assertEqual(code, 1)
        self.assertIn("설정 오류: FORGE_LEDGER_GITEA_URL", err)
        self.assertNotIn(self.gitea_token, err)

    def test_clients_use_shared_limiters(self) -> None:
        made: list[tuple[str, str, int]] = []
        limiters: dict[str, _CountingLimiter] = {}

        def spy(base_url: str, token: str, env: Mapping[str, str], per_minute: int = 50) -> RateLimiter:
            self.assertIs(env, self.env)
            made.append((base_url, token, per_minute))
            return limiters.setdefault(base_url, _CountingLimiter())

        self.assertIs(hubcli.make_shared_limiter, make_shared_limiter)
        with patch.object(hubcli, "make_shared_limiter", spy):
            self.assertEqual(self._cli("sync", "--repo", str(self.repo))[0], 0)
        self.assertEqual(
            sorted(made),
            sorted([(self.gitea.base_url, self.gitea_token, 120), (self.plane.base_url, self.plane_token, 50)]),
        )
        self.assertEqual(limiters[self.gitea.base_url].acquired, len(self.gitea.requests))
        self.assertEqual(limiters[self.plane.base_url].acquired, len(self.plane.requests))

    def test_hub_commands_do_not_require_plane_env(self) -> None:
        env = {"FORGE_LEDGER_STATE_DIR": str(self.state_dir)}
        code, out, err = self._cli("status", env=env)
        self.assertEqual((code, out, err), (0, _EMPTY_TEXT + "\n", ""))
        code, out, _ = self._cli("sync", "--repo", str(self.repo), env=env)
        self.assertEqual(code, 0)
        self.assertIn("o/n plane: skipped (no_token)", out)

    def test_script_entry_runs_status(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "status"],
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "FORGE_LEDGER_STATE_DIR": str(self.state_dir)},
            check=False,
        )
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, _EMPTY_TEXT + "\n", ""))


if __name__ == "__main__":
    unittest.main()
