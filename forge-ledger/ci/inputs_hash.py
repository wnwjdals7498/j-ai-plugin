"""Compute and verify SHA-256 digest of CI image input files."""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

_REBUILD_MSG = "검사 이미지 재빌드 필요: bash scripts/ci-image-build.sh"


def image_input_paths(root: Path) -> list[Path]:
    """Return ci/Dockerfile and all files under ci/image/ sorted by relative POSIX path."""
    paths: list[Path] = []
    dockerfile = root / "ci" / "Dockerfile"
    if dockerfile.is_file():
        paths.append(dockerfile)
    image_dir = root / "ci" / "image"
    if image_dir.is_dir():
        for candidate in image_dir.rglob("*"):
            if candidate.is_file():
                paths.append(candidate)
    return sorted(paths, key=lambda p: p.relative_to(root).as_posix())


def inputs_digest(root: Path) -> str:
    """Compute SHA-256 hex digest over sorted relative POSIX paths and file contents."""
    hasher = hashlib.sha256()
    for path in image_input_paths(root):
        rel = path.relative_to(root).as_posix().encode("utf-8")
        hasher.update(rel)
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for printing or checking CI image input digest."""
    parser = argparse.ArgumentParser(description="Compute or verify CI image input digest.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root directory.",
    )
    parser.add_argument(
        "--check",
        type=Path,
        default=None,
        help="Path to recorded digest file to compare against.",
    )
    args = parser.parse_args(argv)

    digest = inputs_digest(args.root)
    if args.check is None:
        print(digest)
        return 0

    try:
        recorded = args.check.read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""

    if recorded == digest:
        return 0

    print(_REBUILD_MSG, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
