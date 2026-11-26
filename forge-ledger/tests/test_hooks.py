from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hooks import session_start
from hooks.pre_tool_use import should_block
from hooks.session_start import build_exports, hub_summary, maybe_refresh, summary
from ledger.config import LedgerConfig, PlaneProject, save_config
from ledger.history import HistoryEntry, append_entry
from ledger.hubstore import DB_NAME, HubStore, save_registry

PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "plugins" / "forge-ledger"
HOOKS_DIR = PLUGIN_ROOT / "hooks"


def run_hook(
    script_name: str,
    payload: dict[str, object] | None = None,
    *,
    env: dict[str, str] | None = None,
    raw_stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    script = HOOKS_DIR / script_name
    stdin_data = raw_stdin if raw_stdin is not None else json.dumps(payload or {})
    run_env = {"PATH": os.environ.get("PATH", "")}
    if env:
        run_env.update(env)
    return subprocess.run(
        [sys.executable, str(script)],
        input=stdin_data,
        text=True,
        capture_output=True,
        env=run_env,
        check=False,
    )


NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class HooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.envfile = self.tmp_path / "claude.env"
        self.repo = self.tmp_path.resolve() / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True, capture_output=True)
        # 훅이 실제 사용자의 상태 폴더를 읽지 않게 모든 실행에 넘긴다.
        self.state_dir = self.tmp_path / "state"
        self.state_env = {"FORGE_LEDGER_STATE_DIR": str(self.state_dir)}

        cfg = LedgerConfig(
            method_version=1,
            workspace="forge",
            projects=[
                PlaneProject(key="CORE", title="Core", default=True),
                PlaneProject(key="WEB", title="Web"),
                PlaneProject(key="OPS", title="Ops"),
            ],
        )
        save_config(cfg, self.repo / ".ledger" / "ledger.yml")
        append_entry(
            self.repo / ".ledger" / "history.md",
            HistoryEntry(
                date="2026-11-23",
                kind="decision",
                ref_id="D-261123-0001",
                projects="CORE",
                title="초기 결정",
                detail="2026-11-23T10:00:00Z",
            ),
        )
        append_entry(
            self.repo / ".ledger" / "history.md",
            HistoryEntry(
                date="2026-11-23",
                kind="decision",
                ref_id="D-261123-0002",
                projects="CORE,WEB",
                title="두 번째 결정",
                detail="2026-11-23T11:00:00Z",
            ),
        )

        self.opts = {
            "CLAUDE_PLUGIN_OPTION_PLANE_URL": "http://h:1",
            "CLAUDE_PLUGIN_OPTION_PLANE_WORKSPACE": "forge",
            "CLAUDE_PLUGIN_OPTION_PLANE_API_KEY": "plane_api_" + "a" * 32,
            "CLAUDE_PLUGIN_OPTION_PLANE_MCP_URL": "http://h:2/http/api-key/mcp",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_plugin_json_worklog_options(self) -> None:
        config = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
        self.assertEqual(config["worklog_project_key"]["default"], "WLOG")
        self.assertEqual(config["worklog_host_label"]["default"], "")
        self.assertIs(config["worklog_auto"]["default"], True)

    def test_session_start_worklog_pending(self) -> None:
        from ledger.worklog import LocalLog, new_entry

        env = {
            **self.state_env,
            "CLAUDE_PROJECT_DIR": str(self.repo),
            "CLAUDE_PLUGIN_OPTION_PLANE_API_KEY": "sample",
            "CLAUDE_PLUGIN_OPTION_WORKLOG_AUTO": "true",
        }
        local = LocalLog(self.state_dir)
        for _ in range(3):
            local.append(new_entry("작업", source="manual", env=env, now=NOW))
        out = io.StringIO()
        spawned = []
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(session_start, "_spawn_detached", lambda argv, child: spawned.append(argv)),
            redirect_stdout(out),
        ):
            self.assertEqual(session_start.main(), 0)
        self.assertIn("작업 일지 미전송 3건", out.getvalue())
        self.assertEqual(spawned[0][-2:], ["log", "sync"])
        self.assertIn("FORGE_LEDGER_WORKLOG_AUTO", " ".join(build_exports(env)))

    def test_worklog_corrupt_sent_preserves_summary_and_refresh(self) -> None:
        worklog = self.state_dir / "worklog"
        worklog.mkdir(parents=True)
        (worklog / "sent.json").write_text("{broken", encoding="utf-8")
        env = {**self.state_env, "CLAUDE_PROJECT_DIR": str(self.repo)}
        out = io.StringIO()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(session_start, "maybe_refresh") as refresh,
            redirect_stdout(out),
        ):
            self.assertEqual(session_start.main(), 0)
        self.assertIn("forge-ledger: Plane 프로젝트", out.getvalue())
        refresh.assert_called_once()

    def test_worklog_pending_long_summary_preserves_suffix(self) -> None:
        from ledger.worklog import LocalLog, new_entry

        env = {**self.state_env, "CLAUDE_PROJECT_DIR": str(self.repo)}
        LocalLog(self.state_dir).append(new_entry("작업", source="manual", env=env, now=NOW))
        out = io.StringIO()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(session_start, "hub_summary", return_value="가" * 2000),
            redirect_stdout(out),
        ):
            self.assertEqual(session_start.main(), 0)
        self.assertLessEqual(len(out.getvalue().rstrip("\n")), 1499)
        self.assertTrue(out.getvalue().endswith("\n작업 일지 미전송 1건\n"))

    def test_exports_from_plugin_options(self) -> None:
        env = {
            "CLAUDE_PLUGIN_OPTION_PLANE_URL": "http://h:1",
            "CLAUDE_PLUGIN_OPTION_PLANE_WORKSPACE": "forge",
            "CLAUDE_PLUGIN_OPTION_PLANE_API_KEY": "plane_api_" + "a" * 32,
        }
        self.assertEqual(
            build_exports(env),
            [
                "export FORGE_LEDGER_PLANE_URL=http://h:1",
                "export FORGE_LEDGER_PLANE_WORKSPACE=forge",
                "export FORGE_LEDGER_PLANE_TOKEN=plane_api_" + "a" * 32,
            ],
        )

    def test_exports_skips_empty_quotes_special_and_excludes_mcp_url(self) -> None:
        env = {
            "CLAUDE_PLUGIN_OPTION_PLANE_URL": "http://h:1/a b",
            "CLAUDE_PLUGIN_OPTION_PLANE_WORKSPACE": "",
            "CLAUDE_PLUGIN_OPTION_PLANE_MCP_URL": "http://h:2/mcp",
        }
        self.assertEqual(
            build_exports(env),
            ["export FORGE_LEDGER_PLANE_URL='http://h:1/a b'"],
        )

    def test_session_start_writes_env_file_and_prints_summary(self) -> None:
        r = run_hook(
            "session_start.py",
            {},
            env={
                **self.opts,
                **self.state_env,
                "CLAUDE_ENV_FILE": str(self.envfile),
                "CLAUDE_PROJECT_DIR": str(self.repo),
            },
        )
        self.assertEqual(r.returncode, 0)
        env_text = self.envfile.read_text(encoding="utf-8")
        self.assertIn("FORGE_LEDGER_PLANE_TOKEN", env_text)
        self.assertNotIn("MCP_URL", env_text)
        self.assertIn("forge-ledger: Plane 프로젝트 3개", r.stdout)
        self.assertIn("기본 CORE", r.stdout)
        self.assertIn("결정 2건", r.stdout)
        self.assertIn("마지막 D-261123-0002", r.stdout)
        self.assertNotIn("plane_api_", r.stdout)
        self.assertLessEqual(len(r.stdout), 1500)

    def test_summary_missing_ledger_returns_empty(self) -> None:
        empty_repo = self.tmp_path / "no-ledger"
        empty_repo.mkdir()
        self.assertEqual(summary(empty_repo), "")

    def test_session_start_fail_open(self) -> None:
        r = run_hook("session_start.py", {}, env={**self.state_env, "CLAUDE_PROJECT_DIR": "/nonexistent"})
        self.assertEqual(r.returncode, 0)

    def _register(self, synced_at: datetime | None = None, source: str = "gitea") -> int:
        store = HubStore(self.state_dir)
        repo_id = store.upsert_repo("o", "n", str(self.repo), "forge")
        save_registry(self.state_dir, [str(self.repo)])
        if synced_at is not None:
            self._record_run(store, repo_id, source, synced_at)
        return repo_id

    def _record_run(self, store: HubStore, repo_id: int, source: str, moment: datetime) -> None:
        source_id = store.source_id(source, f"http://{source}.invalid")
        run_id = store.start_run(source_id, repo_id, _iso(moment))
        store.finish_run(run_id, "ok", 1, "", _iso(moment))

    def _refresh(self, env: dict[str, str]) -> tuple[bool, list[tuple[list[str], dict[str, str]]]]:
        spawned: list[tuple[list[str], dict[str, str]]] = []
        started = maybe_refresh(
            self.repo, {**self.state_env, **env}, NOW, lambda argv, child: spawned.append((argv, child))
        )
        return started, spawned

    def test_exports_include_gitea_and_bind(self) -> None:
        token = "tok" + "g" * 30
        env = {
            "CLAUDE_PLUGIN_OPTION_GITEA_URL": "http://h:3",
            "CLAUDE_PLUGIN_OPTION_GITEA_TOKEN": token,
            "CLAUDE_PLUGIN_OPTION_DASHBOARD_BIND": "127.0.0.1",
        }
        self.assertEqual(
            build_exports(env),
            [
                "export FORGE_LEDGER_GITEA_URL=http://h:3",
                f"export FORGE_LEDGER_GITEA_TOKEN={token}",
                "export FORGE_LEDGER_DASHBOARD_BIND=127.0.0.1",
            ],
        )

    def test_hub_summary_none_without_db_or_registration(self) -> None:
        self.assertIsNone(hub_summary(self.repo, self.state_env, NOW))
        self.assertFalse((self.state_dir / DB_NAME).exists())
        HubStore(self.state_dir).upsert_repo("o", "other", str(self.tmp_path / "other"), "")
        self.assertIsNone(hub_summary(self.repo, self.state_env, NOW))

    def test_session_start_prints_hub_summary_when_registered(self) -> None:
        self._register(NOW)
        (self.repo / "sub").mkdir()
        for project_dir in (self.repo, self.repo / "sub"):
            r = run_hook("session_start.py", {}, env={**self.state_env, "CLAUDE_PROJECT_DIR": str(project_dir)})
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            self.assertTrue(r.stdout.startswith("forge-ledger 허브: o/n ("), r.stdout)
            self.assertIn("열린 결함 (Gitea): 0", r.stdout)
            self.assertNotIn("Plane 프로젝트 3개", r.stdout)
            self.assertLessEqual(len(r.stdout.rstrip("\n")), 1500)

    def test_session_start_fail_open_with_corrupt_db(self) -> None:
        self._register()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.state_dir / DB_NAME}{suffix}").unlink(missing_ok=True)
        (self.state_dir / DB_NAME).write_bytes(b"not a database" * 100)
        r = run_hook("session_start.py", {}, env={**self.state_env, "CLAUDE_PROJECT_DIR": str(self.repo)})
        self.assertEqual(r.returncode, 0)
        self.assertIn("forge-ledger: Plane 프로젝트 3개", r.stdout)

    def test_session_start_falls_back_when_db_cannot_open(self) -> None:
        self.state_dir.mkdir()
        (self.state_dir / DB_NAME).write_bytes(b"")
        (self.state_dir / "hub-db.lock").mkdir()  # 잠금 파일을 열 수 없게 한다
        r = run_hook("session_start.py", {}, env={**self.state_env, "CLAUDE_PROJECT_DIR": str(self.repo)})
        self.assertEqual(r.returncode, 0)
        self.assertIn("forge-ledger: Plane 프로젝트 3개", r.stdout)

    def test_main_falls_back_to_discovered_root_from_subfolder(self) -> None:
        nested = self.repo / "a" / "b"
        nested.mkdir(parents=True)
        env = {**self.state_env, "PATH": os.environ.get("PATH", ""), "CLAUDE_PROJECT_DIR": str(nested)}
        out = io.StringIO()
        with patch.dict(os.environ, env, clear=True):
            with redirect_stdout(out):
                self.assertEqual(session_start.main(), 0)
        self.assertFalse((self.state_dir / DB_NAME).exists())
        self.assertIn("forge-ledger: Plane 프로젝트 3개", out.getvalue())

    def test_maybe_refresh_spawns_without_token_in_argv(self) -> None:
        self._register(NOW - timedelta(seconds=601))
        gitea_token = "tok" + "g" * 30
        plane_token = "tok" + "p" * 30
        started, spawned = self._refresh(
            {
                "CLAUDE_PLUGIN_OPTION_GITEA_TOKEN": gitea_token,
                "CLAUDE_PLUGIN_OPTION_PLANE_API_KEY": plane_token,
                "FORGE_LEDGER_GITEA_TOKEN": "stale",
            }
        )
        self.assertTrue(started)
        [(argv, child)] = spawned
        self.assertEqual(
            argv,
            [sys.executable, str(PLUGIN_ROOT / "scripts" / "ledger.py"), "sync", "--repo", str(self.repo), "--quiet"],
        )
        for token in (gitea_token, plane_token):
            self.assertNotIn(token, " ".join(argv))
        self.assertEqual(
            (child["FORGE_LEDGER_GITEA_TOKEN"], child["FORGE_LEDGER_PLANE_TOKEN"]), (gitea_token, plane_token)
        )
        self.assertEqual(child["FORGE_LEDGER_STATE_DIR"], str(self.state_dir))

    def test_maybe_refresh_skips_when_fresh(self) -> None:
        repo_id = self._register(NOW - timedelta(hours=1), "gitea")
        # gitea·plane 중 가장 최근 시작이 기준이다. 600초까지는 새 것으로 본다.
        self._record_run(HubStore(self.state_dir), repo_id, "plane", NOW - timedelta(seconds=600))
        self.assertEqual(self._refresh({"FORGE_LEDGER_GITEA_TOKEN": "tok"}), (False, []))

    def test_maybe_refresh_without_runs_needs_registration_and_token(self) -> None:
        env = {"CLAUDE_PLUGIN_OPTION_PLANE_API_KEY": "tok"}
        self.assertEqual(self._refresh(env), (False, []))  # 등록 전
        save_registry(self.state_dir, [str(self.repo)])
        self.assertEqual(self._refresh({}), (False, []))
        self.assertTrue(self._refresh(env)[0])  # DB가 아직 없어도 다시 대조한다
        self.assertFalse((self.state_dir / DB_NAME).exists())
        self._register()
        self.assertTrue(self._refresh(env)[0])  # 실행 기록이 없다

    def test_default_spawn_is_detached_and_silent(self) -> None:
        with patch.object(session_start.subprocess, "Popen") as popen:
            session_start._spawn_detached(["python3", "x"], {"A": "1"})
        popen.assert_called_once_with(
            ["python3", "x"],
            env={"A": "1"},
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_main_prints_stored_values_then_refreshes(self) -> None:
        self._register()
        env = {
            **self.state_env,
            "PATH": os.environ.get("PATH", ""),
            "CLAUDE_PROJECT_DIR": str(self.repo),
            "CLAUDE_PLUGIN_OPTION_GITEA_TOKEN": "tok",
        }
        out = io.StringIO()
        printed_before_spawn: list[str] = []
        with patch.dict(os.environ, env, clear=True):
            with patch.object(session_start, "_spawn_detached", lambda *_: printed_before_spawn.append(out.getvalue())):
                with redirect_stdout(out):
                    self.assertEqual(session_start.main(), 0)
        [text] = printed_before_spawn
        self.assertIn("forge-ledger 허브: o/n (수집 전)", text)

    def test_block_secret_in_ledger_write(self) -> None:
        p = {"tool_name": "Write", "tool_input": {"file_path": "/r/.ledger/design/data.md", "content": "10.1.2.3"}}
        blocked = should_block(p)
        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertIn("ipv4", blocked)
        r = run_hook("pre_tool_use.py", p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("forge-ledger: 비밀값(ipv4)이 있어 쓰기를 막았습니다", r.stderr)
        self.assertNotIn("10.1.2.3", r.stderr)

    def test_block_secret_in_multiedit_ledger(self) -> None:
        p = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "/r/.ledger/spec.md",
                "edits": [{"old_string": "a", "new_string": "ghp_" + "x" * 24}],
            },
        }
        blocked = should_block(p)
        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertIn("github_token", blocked)

    def test_allow_other_paths_and_clean(self) -> None:
        self.assertIsNone(
            should_block({"tool_name": "Write", "tool_input": {"file_path": "/r/src/a.py", "content": "10.1.2.3"}})
        )
        self.assertIsNone(
            should_block({"tool_name": "Edit", "tool_input": {"file_path": "/r/.ledger/x.md", "new_string": "깨끗"}})
        )

    def test_block_plane_mcp_input(self) -> None:
        p = {
            "tool_name": "mcp__plugin_forge-ledger_plane__workitem",
            "tool_input": {"description_html": "plane_api_" + "b" * 32},
        }
        self.assertIsNotNone(should_block(p))

    def test_pre_tool_use_fail_open_on_bad_json(self) -> None:
        r = run_hook("pre_tool_use.py", raw_stdin="not json")
        self.assertEqual(r.returncode, 0)

    def test_block_ipv6_and_allow_non_ipv6(self) -> None:
        p = {"tool_name": "Write", "tool_input": {"file_path": "/r/.ledger/design/data.md", "content": "fd00::42"}}
        blocked = should_block(p)
        self.assertIsNotNone(blocked)
        assert blocked is not None
        self.assertIn("ipv6", blocked)
        self.assertIsNone(
            should_block(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": "/r/.ledger/design/data.md", "content": "::1 10:30:00 std::vector"},
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
