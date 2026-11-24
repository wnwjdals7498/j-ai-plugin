"""Scan changed repository files for secret patterns using ledger.secrets."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_ROOT = str(_REPO_ROOT / "plugins" / "forge-ledger")
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from ledger.secrets import find_secrets  # noqa: E402


class Finding(NamedTuple):
    """A detected secret pattern occurrence."""

    path: str
    line: int
    kind: str


def scan_files(root: Path, paths: Sequence[str]) -> list[Finding]:
    """Scan given relative paths under root for secret patterns line by line."""
    findings: list[Finding] = []
    for rel in paths:
        posix_rel = Path(rel).as_posix()
        if posix_rel == "tests" or posix_rel.startswith("tests/"):
            continue
        target = root / rel
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line_text in enumerate(text.splitlines(), start=1):
            for kind in find_secrets(line_text):
                findings.append(Finding(path=rel, line=lineno, kind=kind))
    return findings


def changed_files(root: Path, base: str, head: str) -> list[str]:
    """Return sorted list of added/copied/modified/renamed files between base and head."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"{base}...{head}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(line for line in proc.stdout.splitlines() if line)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for scanning changed or specified files for secrets."""
    parser = argparse.ArgumentParser(description="Scan changed files for secret patterns.")
    parser.add_argument(
        "--root",
        type=Path,
        default=_REPO_ROOT,
        help="Repository root directory.",
    )
    parser.add_argument("--base", default=None, help="Base git revision.")
    parser.add_argument("--head", default=None, help="Head git revision.")
    parser.add_argument("--files", nargs="+", default=None, help="Explicit relative file paths to scan.")
    args = parser.parse_args(argv)

    has_range = args.base is not None or args.head is not None
    has_files = args.files is not None
    if has_files and has_range:
        print("Specify either --files or --base/--head, not both.", file=sys.stderr)
        return 2
    if has_files:
        assert args.files is not None
        paths: Sequence[str] = args.files
    elif args.base is not None and args.head is not None:
        paths = changed_files(args.root, args.base, args.head)
    else:
        print("Specify --files PATH... or both --base SHA and --head SHA.", file=sys.stderr)
        return 2

    findings = scan_files(args.root, paths)
    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.kind}")
        return 1

    print("비밀값 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
