#!/usr/bin/env python3
"""SessionStart hook: export Plane userConfig options and print ledger summary."""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if not sys.path or sys.path[0] != _PLUGIN_ROOT:
    sys.path[:] = [_PLUGIN_ROOT, *(p for p in sys.path if p != _PLUGIN_ROOT)]

from ledger.config import load_config  # noqa: E402
from ledger.history import parse_history  # noqa: E402

_OPTION_EXPORTS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_PLUGIN_OPTION_PLANE_URL", "FORGE_LEDGER_PLANE_URL"),
    ("CLAUDE_PLUGIN_OPTION_PLANE_WORKSPACE", "FORGE_LEDGER_PLANE_WORKSPACE"),
    ("CLAUDE_PLUGIN_OPTION_PLANE_API_KEY", "FORGE_LEDGER_PLANE_TOKEN"),
)


def build_exports(env: Mapping[str, str]) -> list[str]:
    """Build shell export lines from non-empty plugin option environment variables."""
    lines: list[str] = []
    for src_key, dst_key in _OPTION_EXPORTS:
        val = env.get(src_key, "")
        if not val:
            continue
        lines.append(f"export {dst_key}={shlex.quote(val)}")
    return lines


def summary(repo_root: Path) -> str:
    """Return a single-line summary for a repository containing `.ledger/`."""
    ledger_dir = repo_root / ".ledger"
    config_path = ledger_dir / "ledger.yml"
    if not ledger_dir.is_dir() or not config_path.is_file():
        return ""

    cfg = load_config(config_path)
    try:
        default_key = cfg.default_project().key
    except LookupError:
        default_key = cfg.projects[0].key if cfg.projects else "없음"

    history_path = ledger_dir / "history.md"
    entries = parse_history(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
    decisions = [entry for entry in entries if entry.kind == "decision"]
    if decisions:
        last_id = decisions[-1].ref_id
    elif entries:
        last_id = entries[-1].ref_id
    else:
        last_id = "없음"

    return (
        f"forge-ledger: Plane 프로젝트 {len(cfg.projects)}개(기본 {default_key}), "
        f"결정 {len(decisions)}건, 마지막 {last_id}"
    )


def _append_exports(env_file: Path, lines: list[str]) -> None:
    if not lines:
        return
    prefix = ""
    if env_file.is_file():
        existing = env_file.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            prefix = "\n"
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}" + "\n".join(lines) + "\n")


def main() -> int:
    """Run SessionStart hook; fail open on any error."""
    try:
        env = os.environ
        env_file = env.get("CLAUDE_ENV_FILE", "")
        if env_file:
            _append_exports(Path(env_file), build_exports(env))

        project_dir = env.get("CLAUDE_PROJECT_DIR", "")
        if project_dir:
            line = summary(Path(project_dir))
            if line:
                print(line)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
