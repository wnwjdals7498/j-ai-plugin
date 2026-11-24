from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_CI_DIR = str(ROOT / "ci")
if _CI_DIR not in sys.path:
    sys.path.insert(0, _CI_DIR)

import install_smoke  # noqa: E402

PLUGIN = ROOT / "plugins" / "forge-ledger"
EXPECTED_NAMES = ["cli_help", "hooks_json", "session_start", "pre_tool_use_block", "pre_tool_use_allow"]
FAKE_IP = "10." + "1.2.3"


def _copy_plugin(dest_parent: Path) -> Path:
    dest = dest_parent / "forge-ledger"
    shutil.copytree(PLUGIN, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


class TestInstallSmoke(unittest.TestCase):
    def test_repo_plugin_passes_all_checks(self) -> None:
        results = install_smoke.run_smoke(PLUGIN, sys.executable)
        self.assertEqual([r.name for r in results], EXPECTED_NAMES)
        for result in results:
            self.assertTrue(result.ok, f"{result.name}: {result.detail}")

    def test_detects_hook_that_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_plugin(Path(tmp))
            (root / "hooks" / "pre_tool_use.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
            results = install_smoke.run_smoke(root, sys.executable)
        self.assertEqual([r.name for r in results], EXPECTED_NAMES)
        by_name = {r.name: r.ok for r in results}
        self.assertFalse(by_name["pre_tool_use_block"])
        for name in EXPECTED_NAMES:
            if name != "pre_tool_use_block":
                self.assertTrue(by_name[name], name)

    def test_detects_missing_hook_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_plugin(Path(tmp))
            (root / "hooks" / "session_start.py").unlink()
            results = install_smoke.run_smoke(root, sys.executable)
        by_name = {r.name: r.ok for r in results}
        self.assertFalse(by_name["hooks_json"])
        self.assertFalse(by_name["session_start"])
        self.assertTrue(by_name["cli_help"])

    def test_main_exit_codes_and_output(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install_smoke.main(["--root", str(PLUGIN), "--python", sys.executable])
        self.assertEqual(code, 0)
        self.assertIn("pre_tool_use_block: 통과", out.getvalue())

        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_plugin(Path(tmp))
            (root / "hooks" / "pre_tool_use.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = install_smoke.main(["--root", str(root), "--python", sys.executable])
        self.assertEqual(code, 1)
        self.assertIn("실패", out.getvalue())

    def test_output_has_no_fake_secret(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            install_smoke.main(["--root", str(PLUGIN), "--python", sys.executable])
        self.assertNotIn(FAKE_IP, out.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_plugin(Path(tmp))
            # 훅이 비밀값을 그대로 되풀이해 출력해도 detail에는 담기지 않아야 한다.
            (root / "hooks" / "pre_tool_use.py").write_text(
                "import sys\nsys.stderr.write(sys.stdin.read())\nsys.exit(0)\n", encoding="utf-8"
            )
            results = install_smoke.run_smoke(root, sys.executable)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                install_smoke.main(["--root", str(root), "--python", sys.executable])
        for result in results:
            self.assertNotIn(FAKE_IP, result.detail)
        self.assertNotIn(FAKE_IP, out.getvalue())


if __name__ == "__main__":
    unittest.main()
