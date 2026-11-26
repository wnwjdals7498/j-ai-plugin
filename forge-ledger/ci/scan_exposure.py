"""Scan repository content for exposed sensitive values using ledger.secrets.

Findings are reported by location and pattern name only; matched values are never printed.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess  # nosec B404 # git 조회용(shell=False)
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import NamedTuple, NoReturn, Optional

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent / "plugins" / "forge-ledger")
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from ledger.secrets import PATTERN_NAMES, find_exposures  # noqa: E402

ALLOW_FILE = ".exposure-allow"


class AllowRule(NamedTuple):
    """One exception: findings of `pattern` in paths matching `path_glob` are ignored."""

    path_glob: str
    pattern: str
    reason: str


class Finding(NamedTuple):
    """A detected exposure occurrence."""

    path: str
    line: int
    pattern: str


class UsageError(Exception):
    """Wrong arguments, a malformed allow file, or an unreadable repository."""


def load_allow(path: Path) -> list[AllowRule]:
    """Read the allow file at path; a missing file means no rules."""
    if not path.is_file():
        return []
    return _parse_allow(path.read_text(encoding="utf-8"))


def _parse_allow(text: str) -> list[AllowRule]:
    """Parse `<path glob>TAB<pattern name>TAB<reason>` lines."""
    rules: list[AllowRule] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split("\t")]
        where = f"{ALLOW_FILE}:{lineno}"
        if len(fields) != 3 or not all(fields):
            raise UsageError(f"{where}: '<경로 glob>\\t<패턴 이름>\\t<사유>' 형식이 아니거나 빈 칸이 있음")
        rule = AllowRule(*fields)
        if rule.pattern not in PATTERN_NAMES:
            raise UsageError(f"{where}: 알 수 없는 패턴 이름")
        rules.append(rule)
    return rules


def _is_allowed(rel: str, pattern: str, rules: Sequence[AllowRule]) -> bool:
    return any(rule.pattern == pattern and fnmatch.fnmatchcase(rel, rule.path_glob) for rule in rules)


def scan_text(rel: str, text: str, rules: Sequence[AllowRule]) -> list[Finding]:
    """Scan text line by line, dropping findings covered by an allow rule."""
    return [
        Finding(path=rel, line=lineno, pattern=pattern)
        for lineno, line in enumerate(text.split("\n"), start=1)
        for pattern in find_exposures(line.rstrip("\r"))
        if not _is_allowed(rel, pattern, rules)
    ]


def _run_git(cwd: Optional[Path], *args: str) -> bytes:
    try:
        proc = subprocess.run(  # nosec B603 B607 # 고정 git 하위 명령을 인자 목록으로 실행(shell=False)
            ["git", *args], cwd=cwd, check=False, capture_output=True
        )
    except OSError as exc:
        raise UsageError(f"git 실행 실패: {exc.strerror}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise UsageError(f"git {args[0]} 실패: {detail}")
    return proc.stdout


def _toplevel() -> Path:
    return Path(os.fsdecode(_run_git(None, "rev-parse", "--show-toplevel").rstrip(b"\n")))


def _listed_paths(top: Path, *args: str) -> list[str]:
    return sorted({os.fsdecode(item) for item in _run_git(top, *args).split(b"\0") if item})


def _changed_paths(top: Path, *revisions: str) -> list[str]:
    # --no-renames: 이름만 바꾼 파일도 새 경로가 추가(A)로 잡힌다.
    return _listed_paths(
        top,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--diff-filter=ACMT",
        "--ignore-submodules=all",
        *revisions,
        "--",
    )


def _tracked_files(top: Path) -> Iterator[tuple[str, bytes]]:
    for rel in _listed_paths(top, "ls-files", "-z"):
        target = top / rel
        if target.is_symlink():
            yield rel, os.fsencode(os.readlink(target))  # 저장소에 올라가는 내용은 링크 대상 문자열이다
        elif target.is_file():  # 작업 폴더에서 지운 파일과 서브모듈 폴더는 건너뛴다
            yield rel, target.read_bytes()


def _staged_files(top: Path) -> Iterator[tuple[str, bytes]]:
    for rel in _changed_paths(top, "--cached"):
        yield rel, _run_git(top, "cat-file", "blob", f":0:{rel}")


def _resolve_commit(top: Path, revision: str) -> str:
    return _run_git(top, "rev-parse", "--verify", f"{revision}^{{commit}}").decode("ascii").strip()


def _resolve_range(top: Path, spec: str) -> tuple[str, str]:
    base, separator, head = spec.partition("..")
    if not separator or not base or not head or base.startswith("-") or head.startswith(("-", ".")):
        raise UsageError("--range 값은 BASE..HEAD 형식이어야 함")
    return _resolve_commit(top, base), _resolve_commit(top, head)


def _range_files(top: Path, base_sha: str, head_sha: str) -> Iterator[tuple[str, bytes]]:
    # 세 점 비교: 갈라진 뒤 base 쪽에서만 바뀐 파일은 빼고, 내용은 head의 것을 읽는다.
    for rel in _changed_paths(top, f"{base_sha}...{head_sha}"):
        yield rel, _run_git(top, "cat-file", "blob", f"{head_sha}:{rel}")


def _staged_allow(top: Path) -> list[AllowRule]:
    if not _run_git(top, "ls-files", "-z", "--", ALLOW_FILE):
        return []
    return _parse_allow(_run_git(top, "cat-file", "blob", f":0:{ALLOW_FILE}").decode("utf-8"))


def _commit_allow(top: Path, commit: str) -> list[AllowRule]:
    if not _run_git(top, "ls-tree", "-z", commit, "--", ALLOW_FILE):
        return []
    return _parse_allow(_run_git(top, "cat-file", "blob", f"{commit}:{ALLOW_FILE}").decode("utf-8"))


def _decode(data: bytes) -> Optional[str]:
    """Return text, or None for binary (NUL byte) and non-UTF-8 content."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan_files(files: Iterator[tuple[str, bytes]], rules: Sequence[AllowRule]) -> list[str]:
    report: list[str] = []
    for rel, data in files:
        text = _decode(data)
        if text is not None:
            report.extend(f"{f.path}:{f.line}:{f.pattern}" for f in scan_text(rel, text, rules))
    return report


def _diff_path(header: str) -> str:
    """Path from the text after `+++ ` in a patch header."""
    path = header.rstrip("\t")
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path[2:] if path.startswith("b/") else path


def _scan_history(top: Path, rules: Sequence[AllowRule]) -> list[str]:
    """Scan added lines of every commit; report `commit:path:pattern` once each."""
    output = _run_git(
        top,
        "-c",
        "core.quotepath=false",
        "log",
        "-p",
        "--all",
        "--format=commit:%H",
        # 사용자 git 설정이 출력 형식을 바꾸지 않게 고정한다.
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-show-signature",
        "--full-index",
        "--ignore-submodules=all",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    )
    report: dict[str, None] = {}
    is_text: dict[str, bool] = {}
    commit = path = blob = ""
    in_hunk = False
    for raw in output.split(b"\n"):
        # 패치에는 여러 파일의 바이트가 섞여 있다. 파일의 인코딩은 아래에서 blob 전체로 판정한다.
        line = raw.decode("utf-8", errors="replace")
        if line.startswith("commit:"):
            commit, in_hunk = line[len("commit:") :], False
        elif line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            if not line.startswith("+"):
                continue
            findings = scan_text(path, line[1:], rules)
            if not findings:
                continue
            if blob not in is_text:
                is_text[blob] = _decode(_run_git(top, "cat-file", "blob", blob)) is not None
            if is_text[blob]:
                for finding in findings:
                    report[f"{commit}:{path}:{finding.pattern}"] = None
        elif line.startswith("index "):
            blob = line.split()[1].partition("..")[2]  # `index <이전>..<이후> [mode]`
        elif line.startswith("+++ "):
            path = _diff_path(line[len("+++ ") :])
    return list(report)


def _read_stdin() -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    # 보낼 본문 검사이므로 깨진 바이트가 있어도 건너뛰지 않고 나머지를 검사한다.
    data: bytes = buffer.read()
    return data.decode("utf-8", errors="replace")


def _stdin_rules() -> list[AllowRule]:
    try:
        top = _toplevel()
    except UsageError:
        return []  # git 저장소 밖에서는 허용 규칙 없이 검사한다
    return load_allow(top / ALLOW_FILE)


def _scan_stdin(label: str) -> list[str]:
    rules = _stdin_rules()
    return [f"{f.path}:{f.line}:{f.pattern}" for f in scan_text(label, _read_stdin(), rules)]


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(f"사용 오류: {message}")


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="scan_exposure.py", description="저장소 내용의 민감값 노출을 검사한다.", allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="커밋하려고 올린 파일")
    mode.add_argument("--range", metavar="A..B", help="A와 갈라진 뒤 B까지 바뀐 파일")
    mode.add_argument("--all", action="store_true", help="저장소의 추적 파일 전체")
    mode.add_argument("--history", action="store_true", help="모든 커밋의 추가 줄")
    mode.add_argument("--stdin", action="store_true", help="표준 입력의 본문")
    parser.add_argument("--label", metavar="NAME", help="--stdin 결과에 쓸 이름")
    return parser


def _report(args: argparse.Namespace) -> list[str]:
    if args.stdin:
        return _scan_stdin(args.label or "stdin")
    top = _toplevel()
    # --staged·--range는 작업 폴더가 아니라 검사 대상과 같은 상태의 허용 파일을 쓴다.
    if args.staged:
        return _scan_files(_staged_files(top), _staged_allow(top))
    if args.range is not None:
        base_sha, head_sha = _resolve_range(top, args.range)
        return _scan_files(_range_files(top, base_sha, head_sha), _commit_allow(top, head_sha))
    rules = load_allow(top / ALLOW_FILE)
    if args.history:
        return _scan_history(top, rules)
    return _scan_files(_tracked_files(top), rules)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns 0 when clean, 1 on findings, 2 on usage errors."""
    try:
        args = _build_parser().parse_args(argv)
        if args.label is not None and not args.stdin:
            raise UsageError("--label은 --stdin과 함께만 쓸 수 있음")
        report = _report(args)
    except (UsageError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if report:
        print("\n".join(report))
        return 1
    print("노출 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
