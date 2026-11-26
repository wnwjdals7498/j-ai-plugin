from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
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
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", message)
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


def _fake_command(bin_dir: Path, name: str, log: Path) -> None:
    """Create an executable that appends its arguments to log, one per line."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(bin_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f'#!/bin/sh\nprintf \'%s\\n\' "$@" >> "{log}"\n')


class TestShellScripts(unittest.TestCase):
    """Run the CI shell scripts in a repo whose forge-ledger folder is a subfolder."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.sub = self.repo / "forge-ledger"
        for rel in ("ci/run-checks.sh", "scripts/ci-local.sh", "ci/image/TAG"):
            (self.sub / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.sub / rel)
        _git(self.repo, "init")
        _git(self.repo, "add", "-A")
        self.base = _git_commit(self.repo, "base")
        _write(self.sub / "a.md", "next\n")
        _git(self.repo, "add", "-A")
        _git_commit(self.repo, "head")

        self.log = self.tmp / "calls.log"
        bin_dir = self.tmp / "bin"
        for name in ("gitleaks", "python3.9", "docker"):
            _fake_command(bin_dir, name, self.log)
        home = self.tmp / "home"
        home.mkdir()
        dropped = {"BASE_SHA", "HEAD_SHA", "BASE_BRANCH"}
        self.env = {k: v for k, v in os.environ.items() if k not in dropped and not k.startswith("GIT_")}
        self.env.update(
            HOME=str(home),
            XDG_CONFIG_HOME=str(home / ".config"),
            GIT_CONFIG_NOSYSTEM="1",
            TMPDIR=str(self.tmp),
            CI_VENV_DIR=str(self.tmp / "venv"),
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        )

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", *args],
            cwd=self.sub,
            env={**self.env, **env},
            check=False,
            capture_output=True,
            text=True,
        )

    def _calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def _range_arg(self) -> str:
        calls = self._calls()
        self.assertIn("ci/scan_exposure.py", calls)
        return calls[calls.index("--range") + 1]

    def test_secrets_default_base_branch_is_master(self) -> None:
        _git(self.repo, "update-ref", "refs/remotes/origin/master", self.base)
        proc = self._run("ci/run-checks.sh", "secrets")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._range_arg(), f"{self.base}..HEAD")

    def test_secrets_base_branch_override(self) -> None:
        _git(self.repo, "update-ref", "refs/remotes/origin/dev", self.base)
        proc = self._run("ci/run-checks.sh", "secrets", BASE_BRANCH="dev")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._range_arg(), f"{self.base}..HEAD")

    def test_secrets_missing_base_branch_names_it(self) -> None:
        proc = self._run("ci/run-checks.sh", "secrets")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("origin/master과의 merge-base를 구할 수 없음", proc.stderr)

    def test_secrets_gitleaks_limited_to_subfolder(self) -> None:
        proc = self._run("ci/run-checks.sh", "secrets", BASE_SHA=self.base)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--log-opts=--full-history --relative -- .", self._calls())

    def test_secrets_trusts_repo_toplevel_on_foreign_owner(self) -> None:
        proc = self._run(
            "ci/run-checks.sh",
            "secrets",
            BASE_SHA=self.base,
            GIT_TEST_ASSUME_DIFFERENT_OWNER="1",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = (self.tmp / "home" / ".gitconfig").read_text(encoding="utf-8")
        self.assertIn(f"directory = {self.repo}\n", config)

    def test_ci_local_mounts_toplevel_and_works_in_subfolder(self) -> None:
        _git(self.repo, "update-ref", "refs/remotes/origin/master", self.base)
        proc = self._run("scripts/ci-local.sh", "lint")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self._calls()
        self.assertEqual(calls[calls.index("-w") + 1], "/src/forge-ledger")
        self.assertTrue(calls[calls.index("-v") + 1].endswith(":/src"))
        self.assertIn(f"BASE_SHA={self.base}", calls)
        self.assertEqual(calls[-2:], ["ci/run-checks.sh", "lint"])

    def test_ci_local_base_branch_override(self) -> None:
        _git(self.repo, "update-ref", "refs/remotes/origin/dev", self.base)
        proc = self._run("scripts/ci-local.sh", "lint", BASE_BRANCH="dev")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"BASE_SHA={self.base}", self._calls())


if __name__ == "__main__":
    unittest.main()
