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
from unittest.mock import patch

from ledger.cli import main
from ledger.plane import PlaneUnavailable
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
            "FORGE_LEDGER_STATE_DIR": str(self.tmp / "state"),
        }

    def run_cli(
        self,
        *args: str,
        env: Optional[dict[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        proc_env = {k: v for k, v in os.environ.items() if not k.startswith("FORGE_LEDGER_PLANE_")}
        proc_env["FORGE_LEDGER_STATE_DIR"] = str(self.tmp / "state")
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

    def _monorepo(self) -> tuple[Path, Path]:
        top = self.tmp.resolve() / "mono"
        sub = top / "sub"
        sub.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=top, check=True, capture_output=True)
        return top, sub

    def _main(self, *argv: str, cwd: Optional[Path] = None) -> tuple[int, str]:
        stderr = io.StringIO()
        previous = Path.cwd()
        os.chdir(cwd or previous)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = main(list(argv), env=self._base_env())
        finally:
            os.chdir(previous)
        return code, stderr.getvalue()

    def test_cli_init_with_repo_uses_given_path(self) -> None:
        top, sub = self._monorepo()
        code, err = self._main("init", "--repo", str(sub))
        self.assertEqual(code, 0, err)
        self.assertTrue((sub / ".ledger" / "ledger.yml").is_file())
        self.assertFalse((top / ".ledger").exists())

    def test_cli_init_without_repo_uses_discovered_root(self) -> None:
        top, sub = self._monorepo()
        code, err = self._main("init", cwd=sub)
        self.assertEqual(code, 0, err)
        self.assertTrue((top / ".ledger" / "ledger.yml").is_file())
        self.assertFalse((sub / ".ledger").exists())

    def test_cli_commands_find_ledger_in_subfolder(self) -> None:
        top, sub = self._monorepo()
        self.assertEqual(self._main("init", "--repo", str(sub))[0], 0)
        nested = sub / "a" / "b"
        nested.mkdir(parents=True)

        code, err = self._main("plan", "new", "앱 첫 출시", "--key", "APP", cwd=nested)
        self.assertEqual(code, 0, err)
        # `--repo`로 하위 폴더 안쪽을 가리켜도 init 이외의 명령은 루트를 찾는다.
        body = self.tmp / "dec.html"
        body.write_text("<p>단순 구조 선택</p>", encoding="utf-8")
        code, err = self._main(
            "decide", "구조 결정", "--project", "APP", "--body-file", str(body), "--repo", str(nested)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("구조 결정", (sub / ".ledger" / "history.md").read_text(encoding="utf-8"))
        self.assertFalse((top / ".ledger").exists())
        self.assertFalse((nested / ".ledger").exists())

    def test_cli_main_in_process(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["init", "--repo", str(self.repo)], env=self._base_env())
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertTrue((self.repo / ".ledger" / "ledger.yml").is_file())
        self.assertNotIn(self.token, stdout.getvalue() + stderr.getvalue())

    def test_cli_init_twice_exit_1(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        req_before = len(self.server.requests)
        r = self.run_cli("init")
        self.assertEqual(r.returncode, 1)
        self.assertIn("이미 초기화된 저장소입니다: .ledger/ledger.yml", r.stderr)
        self.assertEqual(len(self.server.requests), req_before)

    def test_cli_create_secret_exit_4(self) -> None:
        bad_env = self._base_env()
        bad_env["FORGE_LEDGER_PLANE_WORKSPACE"] = "ws-10.1.2.3"
        r_ws = self.run_cli("init", env=bad_env)
        self.assertEqual(r_ws.returncode, 4)
        self.assertEqual(len(self.server.requests), 0)
        self.assertNotIn("10.1.2.3", r_ws.stdout + r_ws.stderr)

        self.assertEqual(self.run_cli("init").returncode, 0)
        req_before = len(self.server.requests)
        secret_title = "plane_api_" + "a" * 32
        r_new = self.run_cli("plan", "new", secret_title, "--key", "APP")
        self.assertEqual(r_new.returncode, 4)
        self.assertEqual(len(self.server.requests), req_before)
        self.assertNotIn(secret_title, r_new.stdout + r_new.stderr)

    def test_cli_plan_check_fails_on_history_secret(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)
        history_path = self.repo / ".ledger" / "history.md"
        history_path.write_text(
            history_path.read_text(encoding="utf-8")
            + "| 2026-11-23 | decision | D-261123-0001 | APP | 10.1.2.3 | d |\n",
            encoding="utf-8",
        )
        r = self.run_cli("plan", "check", "--project", "APP", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("비밀값: ipv4", r.stdout)
        self.assertNotIn("10.1.2.3", r.stdout + r.stderr)

    def test_cli_plan_check_auth_and_unavailable_error(self) -> None:
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)

        self.server.force_status = 401
        r_text = self.run_cli("plan", "check", "--project", "APP")
        self.assertEqual(r_text.returncode, 2)
        self.assertIn("Plane 토큰 확인 필요", r_text.stderr)

        r_json = self.run_cli("plan", "check", "--project", "APP", "--json")
        self.assertEqual(r_json.returncode, 2)
        self.assertEqual(json.loads(r_json.stdout)["error"], "auth")

        self.server.force_status = None
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("ledger.plane.PlaneClient.list_projects", side_effect=PlaneUnavailable("down")):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    ["--repo", str(self.repo), "plan", "check", "--project", "APP", "--json"], env=self._base_env()
                )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"], "unavailable")
