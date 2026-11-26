from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_CI_DIR = str(ROOT / "ci")
if _CI_DIR not in sys.path:
    sys.path.insert(0, _CI_DIR)

import scan_exposure  # noqa: E402

_TOKEN = "plane_api_" + "a" * 32


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@contextlib.contextmanager
def _inside(path: Path) -> Iterator[None]:
    before = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


class _RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            HOME=str(self.tmp),
            XDG_CONFIG_HOME=str(self.tmp / ".config"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CEILING_DIRECTORIES=str(self.tmp),
        )
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        _git(self.repo, "init")

    def run_main(self, *args: str, cwd: Path | None = None, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with _inside(cwd or self.repo), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = scan_exposure.main(list(args))
        return rc, out.getvalue(), err.getvalue()


class TestScanText(unittest.TestCase):
    def test_reports_path_line_pattern(self) -> None:
        findings = scan_exposure.scan_text("docs/a.md", f"one\nkey {_TOKEN}\nthree\n", [])
        self.assertEqual(findings, [scan_exposure.Finding("docs/a.md", 2, "plane_token")])

    def test_only_newline_counts_as_line_break(self) -> None:
        findings = scan_exposure.scan_text("a", f"one\x0c\u2028still one\nkey {_TOKEN}\r\n", [])
        self.assertEqual(findings, [scan_exposure.Finding("a", 2, "plane_token")])

    def test_allow_rule_needs_both_glob_and_pattern(self) -> None:
        text = f"{_TOKEN} 10.1.2.3"
        rules = [scan_exposure.AllowRule("docs/*.md", "plane_token", "가짜 값")]
        self.assertEqual(
            scan_exposure.scan_text("docs/a.md", text, rules),
            [scan_exposure.Finding("docs/a.md", 1, "ipv4")],
        )
        self.assertEqual(
            [f.pattern for f in scan_exposure.scan_text("src/a.md", text, rules)],
            ["ipv4", "plane_token"],
        )


class TestLoadAllow(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".exposure-allow"

    def test_missing_file_has_no_rules(self) -> None:
        self.assertEqual(scan_exposure.load_allow(self.path), [])

    def test_reads_rules_and_skips_comments(self) -> None:
        _write(self.path, "# 설명\n\ntests/a.py\temail\t가짜 값\ndocs/*\tipv4\t설명용 예시\n")
        self.assertEqual(
            scan_exposure.load_allow(self.path),
            [
                scan_exposure.AllowRule("tests/a.py", "email", "가짜 값"),
                scan_exposure.AllowRule("docs/*", "ipv4", "설명용 예시"),
            ],
        )

    def test_rejects_bad_lines(self) -> None:
        for line in (
            "a\temail\t",
            "a\temail\t   ",
            "a\temail",
            "a email reason",
            "\temail\t사유",
            "a\tno_such_pattern\t사유",
        ):
            with self.subTest(line=line):
                _write(self.path, line + "\n")
                with self.assertRaises(scan_exposure.UsageError):
                    scan_exposure.load_allow(self.path)


class TestAllMode(_RepoCase):
    def test_scans_whole_repo_from_subfolder_including_tests_dir(self) -> None:
        _write(self.repo / "top.md", "a.b@corp.co.kr\n")
        _write(self.repo / "sub" / "tests" / "x.py", f"x\nT = '{_TOKEN}'\n")
        _write(self.repo / "sub" / "clean.md", "normal text\n")
        _git(self.repo, "add", "-A")
        rc, out, _ = self.run_main("--all", cwd=self.repo / "sub")
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines(), ["sub/tests/x.py:2:plane_token", "top.md:1:email"])
        self.assertNotIn("plane_api_", out)
        self.assertNotIn("corp.co.kr", out)

    def test_clean_repo_returns_0(self) -> None:
        _write(self.repo / "a.md", "normal text\n")
        _git(self.repo, "add", "-A")
        rc, out, _ = self.run_main("--all")
        self.assertEqual(rc, 0)
        self.assertIn("노출 없음", out)

    def test_skips_binary_and_non_utf8_and_deleted(self) -> None:
        (self.repo / "nul.bin").write_bytes(b"\x00 10.1.2.3\n")
        (self.repo / "latin1.txt").write_bytes(b"\xff\xfe 10.1.2.3\n")
        _write(self.repo / "gone.md", "10.1.2.3\n")
        _git(self.repo, "add", "-A")
        (self.repo / "gone.md").unlink()
        rc, out, err = self.run_main("--all")
        self.assertEqual((rc, err), (0, ""), out)

    def test_scans_symlink_target_not_followed_file(self) -> None:
        (self.repo / "link").symlink_to("/home/alice/secret")
        _git(self.repo, "add", "-A")
        rc, out, _ = self.run_main("--all")
        self.assertEqual((rc, out.splitlines()), (1, ["link:1:home_path"]))

    def test_allow_file_at_toplevel_applies(self) -> None:
        _write(self.repo / "tests" / "x.py", f"T = '{_TOKEN}'\n")
        _write(self.repo / ".exposure-allow", "tests/x.py\tplane_token\t가짜 값\n")
        _write(self.repo / "sub" / "keep.md", "text\n")
        _git(self.repo, "add", "-A")
        rc, out, _ = self.run_main("--all", cwd=self.repo / "sub")
        self.assertEqual(rc, 0, out)

    def test_empty_reason_returns_2(self) -> None:
        _write(self.repo / ".exposure-allow", "tests/x.py\tplane_token\t\n")
        rc, _, err = self.run_main("--all")
        self.assertEqual(rc, 2)
        self.assertIn(".exposure-allow:1", err)


class TestStagedMode(_RepoCase):
    def test_scans_index_content_not_worktree(self) -> None:
        _write(self.repo / "old.md", "10.1.2.3\n")
        _commit(self.repo, "base")
        _write(self.repo / "a.md", f"key {_TOKEN}\n")
        _git(self.repo, "add", "a.md")
        _write(self.repo / "a.md", "cleaned only in worktree\n")
        _write(self.repo / "unstaged.md", "10.1.2.3\n")
        rc, out, _ = self.run_main("--staged")
        self.assertEqual((rc, out.splitlines()), (1, ["a.md:1:plane_token"]))

    def test_works_before_first_commit(self) -> None:
        _write(self.repo / "한글 이름.md", "10.1.2.3\n")
        _git(self.repo, "add", "-A")
        rc, out, _ = self.run_main("--staged")
        self.assertEqual((rc, out.splitlines()), (1, ["한글 이름.md:1:ipv4"]))

    def test_nothing_staged_returns_0(self) -> None:
        rc, _, _ = self.run_main("--staged")
        self.assertEqual(rc, 0)


class TestRangeMode(_RepoCase):
    def test_scans_changed_files_across_whole_repo(self) -> None:
        _write(self.repo / "unchanged.md", "10.1.2.3\n")
        _write(self.repo / "gone.md", "10.1.2.3\n")
        _write(self.repo / "moved.md", "line 1\nline 2\nline 3\n10.1.2.3\n")
        _write(self.repo / "sub" / "a.md", "alpha\n")
        base = _commit(self.repo, "base")
        (self.repo / "gone.md").unlink()
        (self.repo / "moved.md").rename(self.repo / "renamed.md")
        _write(self.repo / "other" / "x.md", "a.b@corp.co.kr\n")
        _write(self.repo / "sub" / "a.md", f"key {_TOKEN}\n")
        head = _commit(self.repo, "head")
        _write(self.repo / "sub" / "a.md", "worktree differs from head\n")

        rc, out, _ = self.run_main("--range", f"{base}..{head}", cwd=self.repo / "sub")
        self.assertEqual(rc, 1)
        self.assertEqual(
            out.splitlines(),
            ["other/x.md:1:email", "renamed.md:4:ipv4", "sub/a.md:1:plane_token"],
        )

    def test_ignores_changes_made_only_on_base_side(self) -> None:
        _write(self.repo / "a.md", "alpha\n")
        fork = _commit(self.repo, "fork")
        _write(self.repo / "base-only.md", "10.1.2.3\n")
        base = _commit(self.repo, "base moves on")
        _git(self.repo, "checkout", "-q", "-b", "topic", fork)
        _write(self.repo / "b.md", "beta\n")
        head = _commit(self.repo, "topic")
        rc, out, _ = self.run_main("--range", f"{base}..{head}")
        self.assertEqual(rc, 0, out)

    def test_bad_range_returns_2(self) -> None:
        _commit(self.repo, "base")
        for value in ("HEAD", "..HEAD", "HEAD..", "HEAD...HEAD", "--all..HEAD", "nope..HEAD"):
            with self.subTest(value=value):
                rc, _, err = self.run_main(f"--range={value}")
                self.assertEqual(rc, 2)
                self.assertTrue(err)


class TestAllowFileSource(_RepoCase):
    """--staged·--range는 검사 대상과 같은 상태의 .exposure-allow를 쓴다."""

    _RULE = "a.md\tipv4\t가짜 값\n"

    def test_staged_ignores_allow_file_only_in_worktree(self) -> None:
        _write(self.repo / "a.md", "10.1.2.3\n")
        _git(self.repo, "add", "a.md")
        _write(self.repo / ".exposure-allow", self._RULE)
        rc, out, _ = self.run_main("--staged")
        self.assertEqual((rc, out.splitlines()), (1, ["a.md:1:ipv4"]))

    def test_staged_ignores_unstaged_edit_of_allow_file(self) -> None:
        _write(self.repo / ".exposure-allow", "# 규칙 없음\n")
        _commit(self.repo, "base")
        _write(self.repo / "a.md", "10.1.2.3\n")
        _git(self.repo, "add", "a.md")
        _write(self.repo / ".exposure-allow", self._RULE)
        rc, out, _ = self.run_main("--staged")
        self.assertEqual((rc, out.splitlines()), (1, ["a.md:1:ipv4"]))

    def test_staged_uses_allow_file_from_index(self) -> None:
        _write(self.repo / "a.md", "10.1.2.3\n")
        _write(self.repo / ".exposure-allow", self._RULE)
        _git(self.repo, "add", "-A")
        (self.repo / ".exposure-allow").unlink()
        rc, out, _ = self.run_main("--staged")
        self.assertEqual(rc, 0, out)

    def test_range_ignores_allow_file_not_in_target_commit(self) -> None:
        base = _commit(self.repo, "base")
        _write(self.repo / "a.md", "10.1.2.3\n")
        head = _commit(self.repo, "head")
        _write(self.repo / ".exposure-allow", self._RULE)
        _git(self.repo, "add", ".exposure-allow")
        rc, out, _ = self.run_main("--range", f"{base}..{head}")
        self.assertEqual((rc, out.splitlines()), (1, ["a.md:1:ipv4"]))

    def test_range_uses_allow_file_from_target_commit(self) -> None:
        base = _commit(self.repo, "base")
        _write(self.repo / "a.md", "10.1.2.3\n")
        _write(self.repo / ".exposure-allow", self._RULE)
        head = _commit(self.repo, "head")
        _write(self.repo / ".exposure-allow", "a.md\tipv4\t\n")
        _commit(self.repo, "later commit breaks the allow file")
        (self.repo / ".exposure-allow").unlink()
        rc, out, err = self.run_main("--range", f"{base}..{head}", cwd=self.repo)
        self.assertEqual(rc, 0, out + err)

    def test_bad_allow_file_in_checked_state_returns_2(self) -> None:
        base = _commit(self.repo, "base")
        _write(self.repo / ".exposure-allow", "a.md\tipv4\t\n")
        _git(self.repo, "add", ".exposure-allow")
        rc, _, err = self.run_main("--staged")
        self.assertEqual(rc, 2)
        self.assertIn(".exposure-allow:1", err)
        head = _commit(self.repo, "head")
        (self.repo / ".exposure-allow").unlink()
        rc, _, err = self.run_main("--range", f"{base}..{head}")
        self.assertEqual(rc, 2)
        self.assertIn(".exposure-allow:1", err)


class TestHistoryMode(_RepoCase):
    def test_reports_added_lines_of_all_commits_without_values(self) -> None:
        _write(self.repo / "a.md", f"keep\nkey {_TOKEN}\n")
        first = _commit(self.repo, "first")
        _write(self.repo / "a.md", "keep\n")
        _commit(self.repo, "remove")
        _git(self.repo, "checkout", "-q", "-b", "side")
        _write(self.repo / "my dir" / "한글.md", "++ 10.1.2.3\n++ a.b@corp.co.kr\n")
        second = _commit(self.repo, "side")
        _git(self.repo, "checkout", "-q", "-")

        rc, out, _ = self.run_main("--history")
        self.assertEqual(rc, 1)
        self.assertEqual(
            sorted(out.splitlines()),
            sorted(
                [
                    f"{first}:a.md:plane_token",
                    f"{second}:my dir/한글.md:ipv4",
                    f"{second}:my dir/한글.md:email",
                ]
            ),
        )

    def test_ignores_user_diff_config(self) -> None:
        _write(self.repo / "a.md", "10.1.2.3\n")
        head = _commit(self.repo, "first")
        _git(self.repo, "config", "diff.noprefix", "true")
        _git(self.repo, "config", "color.ui", "always")
        rc, out, _ = self.run_main("--history")
        self.assertEqual((rc, out.splitlines()), (1, [f"{head}:a.md:ipv4"]))

    def test_skips_binary_and_survives_non_utf8(self) -> None:
        (self.repo / "nul.bin").write_bytes(b"\x00 10.1.2.3\n")
        (self.repo / "latin1.txt").write_bytes(b"\xff\xfe plain\n")
        _commit(self.repo, "first")
        rc, out, err = self.run_main("--history")
        self.assertEqual((rc, err), (0, ""), out)

    def test_skips_whole_non_utf8_file_even_on_valid_lines(self) -> None:
        # 잘못된 바이트와 패턴이 같은 파일에 있으면 그 파일 전체를 건너뛴다.
        (self.repo / "mixed.txt").write_bytes(b"10.1.2.3\n\xff\xfe\n")
        (self.repo / "same-line.txt").write_bytes(b"\xff 10.1.2.3\n")
        _write(self.repo / "ok.md", "a.b@corp.co.kr\n")
        head = _commit(self.repo, "first")
        rc, out, _ = self.run_main("--history")
        self.assertEqual((rc, out.splitlines()), (1, [f"{head}:ok.md:email"]))

    def test_judges_encoding_per_file_version(self) -> None:
        (self.repo / "a.txt").write_bytes(b"10.1.2.3\n")
        first = _commit(self.repo, "valid")
        (self.repo / "a.txt").write_bytes(b"10.1.2.3\n\xff\na.b@corp.co.kr\n")
        _commit(self.repo, "becomes invalid")
        rc, out, _ = self.run_main("--history")
        self.assertEqual((rc, out.splitlines()), (1, [f"{first}:a.txt:ipv4"]))

    def test_skips_file_with_late_nul_byte(self) -> None:
        # git은 앞부분에 널 바이트가 없으면 글자 파일로 보여 준다.
        (self.repo / "late.dat").write_bytes(b"10.1.2.3\n" + b"x" * 9000 + b"\x00\n")
        _commit(self.repo, "first")
        rc, out, _ = self.run_main("--history")
        self.assertEqual(rc, 0, out)

    def test_allow_rule_applies(self) -> None:
        _write(self.repo / "a.md", "10.1.2.3\n")
        _write(self.repo / ".exposure-allow", "a.md\tipv4\t가짜 값\n")
        _commit(self.repo, "first")
        rc, out, _ = self.run_main("--history")
        self.assertEqual(rc, 0, out)


class TestStdinMode(_RepoCase):
    def test_label_prefixes_findings_without_values(self) -> None:
        rc, out, _ = self.run_main("--stdin", "--label", "plane:FL-3", stdin=f"first\nkey {_TOKEN}\n")
        self.assertEqual((rc, out.splitlines()), (1, ["plane:FL-3:2:plane_token"]))

    def test_default_label(self) -> None:
        rc, out, _ = self.run_main("--stdin", stdin="10.1.2.3")
        self.assertEqual((rc, out.splitlines()), (1, ["stdin:1:ipv4"]))

    def test_works_outside_git_repo(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        rc, out, _ = self.run_main("--stdin", cwd=outside, stdin="10.1.2.3")
        self.assertEqual((rc, out.splitlines()), (1, ["stdin:1:ipv4"]))

    def test_non_utf8_bytes_do_not_hide_findings(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ci" / "scan_exposure.py"), "--stdin"],
            cwd=self.repo,
            input=b"\xff 10.1.2.3\n",
            check=False,
            capture_output=True,
        )
        self.assertEqual((proc.returncode, proc.stdout), (1, b"stdin:1:ipv4\n"), proc.stderr)


class TestUsage(_RepoCase):
    def test_usage_errors_return_2(self) -> None:
        for args in ((), ("--all", "--staged"), ("--all", "--label", "x"), ("--range",), ("--unknown",)):
            with self.subTest(args=args):
                rc, _, err = self.run_main(*args)
                self.assertEqual(rc, 2)
                self.assertTrue(err)

    def test_outside_git_repo_returns_2(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        rc, _, err = self.run_main("--all", cwd=outside)
        self.assertEqual(rc, 2)
        self.assertIn("git", err)


if __name__ == "__main__":
    unittest.main()
