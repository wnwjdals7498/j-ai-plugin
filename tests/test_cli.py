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
from typing import Optional

from ledger.cli import main
from tests.fakes import FakePlaneServer

_SCRIPT = Path(__file__).resolve().parent.parent / "plugins" / "forge-ledger" / "scripts" / "ledger.py"


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.tmp = Path(self._tmp_dir.name)
        self.repo = self.tmp / "sample-app"
        self.repo.mkdir()
        self.server = FakePlaneServer()
        self.addCleanup(self.server.close)
        self.token = "plane_api_" + "0" * 32

    def _base_env(self) -> dict[str, str]:
        return {
            "FORGE_LEDGER_PLANE_URL": self.server.base_url,
            "FORGE_LEDGER_PLANE_WORKSPACE": "forge",
            "FORGE_LEDGER_PLANE_TOKEN": self.token,
        }

    def run_cli(
        self,
        *args: str,
        env: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        proc_env = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_LEDGER_PLANE_")}
        if env is None:
            proc_env.update(self._base_env())
        else:
            proc_env.update(env)
        cmd = [sys.executable, str(_SCRIPT), "--repo", str(self.repo), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=proc_env, check=False)

    def test_cli_init_new_check(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)
        r = self.run_cli("plan", "check", "--project", "APP", "--json")
        self.assertEqual(r.returncode, 1)
        data = json.loads(r.stdout)
        self.assertEqual(data["exit_code"], 1)
        scopes = {row["scope"] for row in data["results"]}
        self.assertEqual(scopes, {"APP", "repo"})

    def test_cli_missing_settings_exit_2(self) -> None:
        r = self.run_cli("init", env={})
        self.assertEqual(r.returncode, 2)
        self.assertIn("설정 없음: FORGE_LEDGER_PLANE_URL", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_cli_auth_error_exit_2(self) -> None:
        self.server.force_status = 401
        r = self.run_cli("init")
        self.assertEqual(r.returncode, 2)
        self.assertIn("토큰 확인", r.stderr)
        self.assertFalse((self.repo / ".ledger/ledger.yml").exists())

    def test_cli_secret_exit_4(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)
        body_path = self.tmp / "b.html"
        body_path.write_text("<p>plane_api_" + "a" * 32 + "</p>", encoding="utf-8")
        r = self.run_cli("plan", "put", "APP", "PRD3", "--from", str(body_path))
        self.assertEqual(r.returncode, 4)
        self.assertNotIn("plane_api_", r.stdout + r.stderr)

    def test_cli_never_prints_token(self) -> None:
        for args in (("init",), ("plan", "check")):
            r = self.run_cli(*args)
            self.assertNotIn(self.token, r.stdout + r.stderr)

    def test_cli_plan_get_put_and_decide(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)

        new_body = self.tmp / "prd3.html"
        new_body.write_text("<p>사용자 흐름 정리</p>", encoding="utf-8")
        put_res = self.run_cli("plan", "put", "APP", "PRD3", "--from", str(new_body))
        self.assertEqual(put_res.returncode, 0, put_res.stderr)

        get_res = self.run_cli("plan", "get", "APP", "PRD3")
        self.assertEqual(get_res.returncode, 0, get_res.stderr)
        self.assertIn("사용자 흐름 정리", get_res.stdout)

        dec_body = self.tmp / "dec.html"
        dec_body.write_text("<p>단순 구조 선택</p>", encoding="utf-8")
        dec_res = self.run_cli("decide", "아키텍처 결정", "--project", "APP", "--body-file", str(dec_body))
        self.assertEqual(dec_res.returncode, 0, dec_res.stderr)
        dec_star = self.run_cli("decide", "공통 규칙 결정", "--project", "*", "--body-file", str(dec_body))
        self.assertEqual(dec_star.returncode, 0, dec_star.stderr)

        history = (self.repo / ".ledger" / "history.md").read_text(encoding="utf-8")
        self.assertIn("아키텍처 결정", history)
        self.assertIn("공통 규칙 결정", history)

    def test_cli_plan_check_without_project_skips_default_and_prints_table(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)

        r_json = self.run_cli("plan", "check", "--json")
        self.assertEqual(r_json.returncode, 1)
        data = json.loads(r_json.stdout)
        scopes = {row["scope"] for row in data["results"]}
        self.assertEqual(scopes, {"APP", "repo"})

        r_table = self.run_cli("plan", "check")
        self.assertEqual(r_table.returncode, 1)
        self.assertIn("APP:PRD1", r_table.stdout)
        self.assertIn("repo:IMPL1", r_table.stdout)

    def test_cli_conflict_exit_3(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)
        body_path = self.tmp / "prd3.html"
        body_path.write_text("<p>내용</p>", encoding="utf-8")
        self.server.touch_next_get_item = True
        r = self.run_cli("plan", "put", "APP", "PRD3", "--from", str(body_path))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_cli_invalid_input_exit_1(self) -> None:
        r = self.run_cli("plan", "new", "제목", "--key", "bad-key")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

        r_bad_args = self.run_cli("unknown-cmd")
        self.assertEqual(r_bad_args.returncode, 1)
        self.assertEqual(len(r_bad_args.stderr.strip().splitlines()), 1)

    def test_cli_main_in_process(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["init", "--repo", str(self.repo)], env=self._base_env())
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertTrue((self.repo / ".ledger" / "ledger.yml").is_file())
        self.assertNotIn(self.token, stdout.getvalue() + stderr.getvalue())
