from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_CI_DIR = str(ROOT / "ci")
if _CI_DIR not in sys.path:
    sys.path.insert(0, _CI_DIR)

import inputs_hash  # noqa: E402
import scan_changed_secrets  # noqa: E402


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _git_commit(repo: Path, message: str) -> str:
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class TestInputsHash(unittest.TestCase):
    def test_digest_is_hex64(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "ci" / "Dockerfile", "FROM python:3.12-slim-bookworm\n")
            _write(root / "ci" / "image" / "TAG", "1\n")
            digest = inputs_hash.inputs_digest(root)
            self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{64}", digest))

    def test_digest_ignores_creation_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            r1, r2 = Path(tmp1), Path(tmp2)
            _write(r1 / "ci" / "Dockerfile", "FROM scratch\n")
            _write(r1 / "ci" / "image" / "a.txt", "alpha\n")
            _write(r1 / "ci" / "image" / "b.txt", "beta\n")

            _write(r2 / "ci" / "image" / "b.txt", "beta\n")
            _write(r2 / "ci" / "image" / "a.txt", "alpha\n")
            _write(r2 / "ci" / "Dockerfile", "FROM scratch\n")

            self.assertEqual(inputs_hash.inputs_digest(r1), inputs_hash.inputs_digest(r2))

    def test_digest_changes_on_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "ci" / "Dockerfile", "FROM scratch\n")
            _write(root / "ci" / "image" / "TAG", "1")
            d1 = inputs_hash.inputs_digest(root)
            _write(root / "ci" / "image" / "TAG", "2")
            d2 = inputs_hash.inputs_digest(root)
            self.assertNotEqual(d1, d2)

    def test_digest_changes_on_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "ci" / "Dockerfile", "FROM scratch\n")
            _write(root / "ci" / "image" / "a", "same-content")
            d1 = inputs_hash.inputs_digest(root)
            (root / "ci" / "image" / "a").rename(root / "ci" / "image" / "b")
            d2 = inputs_hash.inputs_digest(root)
            self.assertNotEqual(d1, d2)

    def test_check_match_returns_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "ci" / "Dockerfile", "FROM scratch\n")
            _write(root / "ci" / "image" / "TAG", "1")
            record = root / "inputs.sha256"
            record.write_text(inputs_hash.inputs_digest(root) + "\n", encoding="utf-8")
            rc = inputs_hash.main(["--root", str(root), "--check", str(record)])
            self.assertEqual(rc, 0)

    def test_check_mismatch_returns_1_with_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "ci" / "Dockerfile", "FROM scratch\n")
            _write(root / "ci" / "image" / "TAG", "1")
            record = root / "inputs.sha256"
            record.write_text("0" * 64 + "\n", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = inputs_hash.main(["--root", str(root), "--check", str(record)])
            self.assertEqual(rc, 1)
            self.assertIn("검사 이미지 재빌드 필요", err.getvalue())

    def test_check_missing_record_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "ci" / "Dockerfile", "FROM scratch\n")
            missing = root / "does-not-exist.sha256"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = inputs_hash.main(["--root", str(root), "--check", str(missing)])
            self.assertEqual(rc, 1)
            self.assertIn("검사 이미지 재빌드 필요", err.getvalue())


class TestScanChangedSecrets(unittest.TestCase):
    def test_finds_token_with_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "plane_api_" + "a" * 32
            _write(root / "docs" / "a.md", f"line one\nkey {token}\nline three\n")
            findings = scan_changed_secrets.scan_files(root, ["docs/a.md"])
            self.assertEqual(
                findings,
                [scan_changed_secrets.Finding("docs/a.md", 2, "plane_token")],
            )

    def test_skips_tests_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "plane_api_" + "a" * 32
            _write(root / "tests" / "x.py", f"TOKEN = '{token}'\n")
            self.assertEqual(scan_changed_secrets.scan_files(root, ["tests/x.py"]), [])

    def test_skips_missing_and_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_path = root / "data.bin"
            bin_path.write_bytes(b"\xff\xfe\x00\x81")
            self.assertEqual(
                scan_changed_secrets.scan_files(root, ["missing.txt", "data.bin"]),
                [],
            )

    def test_output_hides_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "plane_api_" + "a" * 32
            _write(root / "docs" / "a.md", f"first\nkey {token}\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = scan_changed_secrets.main(["--root", str(root), "--files", "docs/a.md"])
            self.assertEqual(rc, 1)
            stdout = out.getvalue()
            self.assertIn("docs/a.md:2: plane_token", stdout)
            self.assertNotIn("plane_api_", stdout)

    def test_clean_returns_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "docs" / "clean.md", "just normal text\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = scan_changed_secrets.main(["--root", str(root), "--files", "docs/clean.md"])
            self.assertEqual(rc, 0)
            self.assertIn("비밀값 없음", out.getvalue())

    def test_changed_files_uses_diff_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init")
            _write(repo / "a.md", "alpha original\n")
            _write(repo / "b.md", "beta original\n")
            _write(repo / "d.md", "delta line 1\ndelta line 2\ndelta line 3\n")
            _git(repo, "add", "a.md", "b.md", "d.md")
            base = _git_commit(repo, "base")

            _write(repo / "a.md", "alpha modified\n")
            (repo / "b.md").unlink()
            _write(repo / "c.md", "charlie added\n")
            (repo / "d.md").rename(repo / "e.md")
            _git(repo, "add", "-A")
            head = _git_commit(repo, "head")

            self.assertEqual(
                scan_changed_secrets.changed_files(repo, base, head),
                ["a.md", "c.md", "e.md"],
            )

    def test_missing_args_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = scan_changed_secrets.main(["--root", tmp])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
