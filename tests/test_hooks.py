from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hooks.pre_tool_use import should_block
from hooks.session_start import build_exports, summary
from ledger.config import LedgerConfig, PlaneProject, save_config
from ledger.history import HistoryEntry, append_entry

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


class HooksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.envfile = self.tmp_path / "claude.env"
        self.repo = self.tmp_path / "repo"
        self.repo.mkdir()

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
            env={**self.opts, "CLAUDE_ENV_FILE": str(self.envfile), "CLAUDE_PROJECT_DIR": str(self.repo)},
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
        r = run_hook("session_start.py", {}, env={"CLAUDE_PROJECT_DIR": "/nonexistent"})
        self.assertEqual(r.returncode, 0)

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


if __name__ == "__main__":
    unittest.main()
