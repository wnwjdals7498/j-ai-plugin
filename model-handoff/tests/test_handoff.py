import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "handoff"

loader = importlib.machinery.SourceFileLoader("handoff", str(SCRIPT))
spec = importlib.util.spec_from_loader("handoff", loader)
handoff = importlib.util.module_from_spec(spec)
loader.exec_module(handoff)


def fake_cli(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = dict(os.environ, MODEL_HANDOFF_HOME=str(self.dir / "home"))
        self.work = self.dir / "work"
        self.work.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)
        out = proc.stdout
        try:
            data = json.loads(out)
        except ValueError:
            data = out
        return proc.returncode, data

    def use(self, name, body):
        self.cli("config", "set", f"{name}.command", fake_cli(self.dir, f"fake-{name}", body))

    # ------------------------------------------------------------ config

    def test_order_rejects_unknown_and_duplicate(self):
        self.assertEqual(self.cli("config", "order", "codex,gpt")[0], 2)
        self.assertEqual(self.cli("config", "order", "codex,codex")[0], 2)
        code, cfg = self.cli("config", "order", "agy,codex")
        self.assertEqual(code, 0)
        self.assertEqual(cfg["order"], ["agy", "codex"])

    def test_set_validates_keys_and_minutes(self):
        self.assertEqual(self.cli("config", "set", "codex.colour", "x")[0], 2)
        self.assertEqual(self.cli("config", "set", "default_cooldown_minutes", "abc")[0], 2)
        code, cfg = self.cli("config", "set", "default_cooldown_minutes", "60")
        self.assertEqual((code, cfg["default_cooldown_minutes"]), (0, 60))

    def test_claude_needs_config_dir(self):
        self.use("claude", "echo TASK_DONE T1")
        self.cli("config", "enable", "claude")
        code, st = self.cli("status", "--json")
        claude = next(r for r in st["executors"] if r["name"] == "claude")
        self.assertEqual(claude["state"], "unconfigured")

    # ------------------------------------------------------------ fallback

    def test_quota_on_first_falls_back_to_second(self):
        self.use("codex", "echo \"You've hit your usage limit. try later\"; exit 1")
        self.use("agy", "echo working; echo TASK_DONE T1")
        code, res = self.cli("run", "--cwd", str(self.work), "--prompt", "do T1")
        self.assertEqual(code, 0, res)
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["executor"], "agy")
        self.assertEqual(res["attempts"][0]["limit"], "quota")

        _, st = self.cli("status", "--json")
        states = {r["name"]: r["state"] for r in st["executors"]}
        self.assertEqual(states["codex"], "cooldown")
        self.assertEqual(states["agy"], "ready")

    def test_login_prompt_marks_login_required_and_falls_back(self):
        self.use("agy", "echo 'Authentication required. Please visit the URL to log in:'; echo 'error: authentication failed or timed out'; exit 1")
        self.use("codex", "echo TASK_DONE T1")
        self.cli("config", "order", "agy,codex")
        code, res = self.cli("run", "--cwd", str(self.work), "--prompt", "x")
        self.assertEqual((code, res["executor"]), (0, "codex"))
        self.assertEqual(res["attempts"][0]["limit"], "login")
        _, st = self.cli("status", "--json")
        agy = st["executors"][0]
        self.assertEqual(agy["state"], "login_required")
        self.assertIn("handoff clear agy", agy["reason"])

    def test_cooldown_executor_is_skipped_until_cleared(self):
        self.use("codex", "echo TASK_DONE T1")
        self.use("agy", "echo TASK_DONE T1")
        self.cli("cooldown", "codex", "--minutes", "30")
        _, res = self.cli("run", "--cwd", str(self.work), "--prompt", "x")
        self.assertEqual(res["executor"], "agy")
        self.assertEqual(res["attempts"][0]["skipped"], "cooldown")
        self.cli("clear", "codex")
        _, res = self.cli("run", "--cwd", str(self.work), "--prompt", "x")
        self.assertEqual(res["executor"], "codex")

    def test_all_exhausted_reports_next_available(self):
        self.use("codex", "echo \"You've hit your usage limit\"; exit 1")
        self.use("agy", "echo 'Individual quota reached. Resets in 2h30m'; exit 1")
        code, res = self.cli("run", "--cwd", str(self.work), "--prompt", "x")
        self.assertEqual(code, 3)
        self.assertEqual(res["status"], "unavailable")
        self.assertIsNotNone(res["next_available"])
        state = json.loads((self.dir / "home" / "state.json").read_text())
        agy_wait = state["cooldowns"]["agy"]["until"] - time.time()
        self.assertAlmostEqual(agy_wait, 2.5 * 3600, delta=120)

    def test_non_quota_failure_does_not_fall_back(self):
        self.use("codex", "echo 'TASK_FAIL T1: tests red'; exit 0")
        self.use("agy", "echo TASK_DONE T1")
        code, res = self.cli("run", "--cwd", str(self.work), "--prompt", "x")
        self.assertEqual(code, 1)
        self.assertEqual((res["status"], res["executor"]), ("failed", "codex"))
        self.assertEqual(len(res["attempts"]), 1)

    def test_limit_phrase_in_successful_output_is_ignored(self):
        self.use("codex", "echo \"docs: you've hit your usage limit message\"; echo TASK_DONE T1")
        code, res = self.cli("run", "--cwd", str(self.work), "--prompt", "x")
        self.assertEqual((code, res["executor"]), (0, "codex"))
        _, st = self.cli("status", "--json")
        self.assertEqual(st["executors"][0], {"name": "codex", "state": "ready"})

    def test_executor_runs_in_cwd_and_gets_prompt(self):
        self.use("agy", 'pwd > seen.txt; for a in "$@"; do echo "$a"; done >> seen.txt; echo TASK_DONE T1')
        self.cli("config", "order", "agy")
        code, _ = self.cli("run", "--cwd", str(self.work), "--prompt", "hello T1")
        self.assertEqual(code, 0)
        seen = (self.work / "seen.txt").read_text().splitlines()
        self.assertEqual(seen[0], str(self.work))
        self.assertIn("--new-project", seen)
        self.assertEqual(seen[-1], "hello T1")

    def test_default_permission_is_auto_not_bypass(self):
        opts = handoff.DEFAULT_CONFIG["executors"]
        codex, _ = handoff.build_command("codex", opts["codex"], "p", "/w", 12)
        agy, _ = handoff.build_command("agy", opts["agy"], "p", "/w", 12)
        claude, _ = handoff.build_command("claude", dict(opts["claude"], config_dir="/c"), "p", "/w", 12)
        self.assertIn("--approve-for-me", codex)
        self.assertEqual(agy[1:5], ["--mode", "accept-edits", "--sandbox", "--new-project"])
        self.assertEqual(claude[1:4], ["-p", "--permission-mode", "auto"])
        for cmd in (codex, agy, claude):
            self.assertFalse([a for a in cmd if a.startswith("--dangerously")], cmd)

    def test_codex_auto_allows_network_unless_disabled(self):
        opts = dict(handoff.DEFAULT_CONFIG["executors"]["codex"])
        cmd, _ = handoff.build_command("codex", opts, "p", "/w", 12)
        self.assertIn("sandbox_workspace_write.network_access=true", cmd)
        opts["network"] = False
        cmd, _ = handoff.build_command("codex", opts, "p", "/w", 12)
        self.assertNotIn("sandbox_workspace_write.network_access=true", cmd)

    def test_codex_effort_is_validated_and_passed(self):
        cmd, _ = handoff.build_command("codex", handoff.DEFAULT_CONFIG["executors"]["codex"], "p", "/w", 12)
        self.assertFalse([a for a in cmd if a.startswith("model_reasoning_effort")], cmd)
        self.assertEqual(self.cli("config", "set", "codex.effort", "huge")[0], 2)
        code, cfg = self.cli("config", "set", "codex.effort", "xhigh")
        self.assertEqual(code, 0)
        cmd, _ = handoff.build_command("codex", cfg["executors"]["codex"], "p", "/w", 12)
        self.assertIn("model_reasoning_effort=xhigh", cmd)
        self.assertEqual(cmd[-1], "p")
        code, cfg = self.cli("config", "set", "codex.effort", "null")
        self.assertEqual((code, cfg["executors"]["codex"]["effort"]), (0, None))

    def test_setup_agy_merges_settings_with_backup(self):
        fake_home = self.dir / "userhome"
        settings = fake_home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"theme": "terminal"}')
        self.env["HOME"] = str(fake_home)
        self.use("agy", "echo TASK_DONE T1")
        _, st = self.cli("status", "--json")
        agy = next(r for r in st["executors"] if r["name"] == "agy")
        self.assertIn("handoff setup agy", agy["reason"])

        code, res = self.cli("setup", "agy")
        self.assertEqual(code, 0)
        data = json.loads(settings.read_text())
        self.assertEqual(data, {"theme": "terminal", "toolPermission": "proceed-in-sandbox", "enableTerminalSandbox": True})
        self.assertEqual(json.loads(Path(res["backup"]).read_text()), {"theme": "terminal"})
        _, res = self.cli("setup", "agy")
        self.assertEqual(res["changed"], {})
        _, st = self.cli("status", "--json")
        self.assertEqual(next(r for r in st["executors"] if r["name"] == "agy"), {"name": "agy", "state": "ready"})

    def test_bypass_must_be_set_explicitly(self):
        self.assertEqual(self.cli("config", "set", "codex.permission", "full")[0], 2)
        code, cfg = self.cli("config", "set", "codex.permission", "bypass")
        self.assertEqual(code, 0)
        cmd, _ = handoff.build_command("codex", cfg["executors"]["codex"], "p", "/w", 12)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    # ------------------------------------------------------------ parsing

    def test_parse_reset_seconds(self):
        self.assertEqual(handoff.parse_reset_seconds("quota reached, reset in 143h57m55s"), 143 * 3600 + 57 * 60 + 55)
        self.assertEqual(handoff.parse_reset_seconds("Resets in 4h 23m"), 4 * 3600 + 23 * 60)
        self.assertIsNone(handoff.parse_reset_seconds("try again later"))

    def test_codex_reset_from_session_log(self):
        sessions = self.dir / "codex" / "sessions" / "2026" / "10" / "12"
        sessions.mkdir(parents=True)
        reset = int(time.time()) + 7200
        event = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": reset},
            "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": reset + 99999}}}}
        (sessions / "rollout-x.jsonl").write_text(json.dumps(event) + "\n")
        self.assertEqual(handoff.codex_reset_ts(str(self.dir / "codex"), time.time() - 10), reset)


if __name__ == "__main__":
    unittest.main()
