"""Smoke-check an installed forge-ledger plugin with a given Python interpreter."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess  # nosec B404 # 설치된 플러그인 확인용(shell=False)
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple, Optional

TIMEOUT_SECONDS = 30
BLOCK_MESSAGE = "forge-ledger: 비밀값(ipv4)이 있어 쓰기를 막았습니다"
_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"


class SmokeResult(NamedTuple):
    """Outcome of one smoke check."""

    name: str
    ok: bool
    detail: str


def _run(
    argv: Sequence[str], *, stdin: str = "", extra_env: Optional[dict[str, str]] = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ.get("PATH", "")}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(  # nosec B603 # 인자 목록 실행, shell 미사용
        list(argv),
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )


def _check_cli_help(root: Path, python: str) -> SmokeResult:
    proc = _run([python, str(root / "scripts" / "ledger.py"), "--help"])
    if proc.returncode != 0:
        return SmokeResult("cli_help", False, f"종료 {proc.returncode}")
    missing = [word for word in ("init", "plan") if word not in proc.stdout]
    if missing:
        return SmokeResult("cli_help", False, "출력에 없는 명령: " + ", ".join(missing))
    return SmokeResult("cli_help", True, "")


def _hook_commands(data: object) -> list[str]:
    commands: list[str] = []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return commands
    for groups in hooks.values():
        for group in groups if isinstance(groups, list) else []:
            entries = group.get("hooks") if isinstance(group, dict) else None
            for entry in entries if isinstance(entries, list) else []:
                command = entry.get("command") if isinstance(entry, dict) else None
                if isinstance(command, str):
                    commands.append(command)
    return commands


def _check_hooks_json(root: Path, python: str) -> SmokeResult:
    del python
    try:
        data = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return SmokeResult("hooks_json", False, f"hooks.json을 읽지 못함: {type(exc).__name__}")
    commands = _hook_commands(data)
    if not commands:
        return SmokeResult("hooks_json", False, "훅 명령이 없음")
    missing: list[str] = []
    for command in commands:
        try:
            tokens = shlex.split(command.replace(_ROOT_VAR, str(root)))
        except ValueError:
            return SmokeResult("hooks_json", False, "훅 명령을 해석하지 못함")
        files = [token for token in tokens if token.startswith(str(root))]
        if not files:
            missing.append(command)
        missing.extend(token for token in files if not Path(token).is_file())
    if missing:
        return SmokeResult("hooks_json", False, "없는 파일: " + ", ".join(missing))
    return SmokeResult("hooks_json", True, "")


def _check_session_start(root: Path, python: str) -> SmokeResult:
    proc = _run(
        [python, str(root / "hooks" / "session_start.py")],
        stdin="{}",
        extra_env={"CLAUDE_PROJECT_DIR": "/nonexistent"},
    )
    if proc.returncode != 0:
        return SmokeResult("session_start", False, f"종료 {proc.returncode}")
    return SmokeResult("session_start", True, "")


def _write_payload(file_path: str, content: str) -> str:
    return json.dumps({"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}})


def _check_pre_tool_use_block(root: Path, python: str) -> SmokeResult:
    fake_ip = "10." + "1.2.3"
    proc = _run(
        [python, str(root / "hooks" / "pre_tool_use.py")],
        stdin=_write_payload("/r/.ledger/design/data.md", fake_ip),
    )
    if proc.returncode != 2:
        return SmokeResult("pre_tool_use_block", False, f"종료 {proc.returncode}(기대 2)")
    if BLOCK_MESSAGE not in proc.stderr:
        return SmokeResult("pre_tool_use_block", False, "표준 오류에 차단 문구가 없음")
    if fake_ip in proc.stderr:
        return SmokeResult("pre_tool_use_block", False, "표준 오류에 비밀값이 그대로 나옴")
    return SmokeResult("pre_tool_use_block", True, "")


def _check_pre_tool_use_allow(root: Path, python: str) -> SmokeResult:
    proc = _run(
        [python, str(root / "hooks" / "pre_tool_use.py")],
        stdin=_write_payload("/r/src/a.py", "깨끗"),
    )
    if proc.returncode != 0:
        return SmokeResult("pre_tool_use_allow", False, f"종료 {proc.returncode}(기대 0)")
    return SmokeResult("pre_tool_use_allow", True, "")


_CHECKS: list[tuple[str, Callable[[Path, str], SmokeResult]]] = [
    ("cli_help", _check_cli_help),
    ("hooks_json", _check_hooks_json),
    ("session_start", _check_session_start),
    ("pre_tool_use_block", _check_pre_tool_use_block),
    ("pre_tool_use_allow", _check_pre_tool_use_allow),
]


def run_smoke(plugin_root: Path, python: str) -> list[SmokeResult]:
    """Run every smoke check in order and return all results (no early exit)."""
    results: list[SmokeResult] = []
    for name, check in _CHECKS:
        try:
            results.append(check(plugin_root, python))
        except subprocess.TimeoutExpired:
            results.append(SmokeResult(name, False, f"{TIMEOUT_SECONDS}초 안에 끝나지 않음"))
        except OSError as exc:
            results.append(SmokeResult(name, False, f"실행하지 못함: {type(exc).__name__}"))
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry: print one line per check; exit 1 if any check failed."""
    parser = argparse.ArgumentParser(description="설치된 forge-ledger 플러그인 확인")
    parser.add_argument("--root", required=True, type=Path, help="플러그인 폴더(plugins/forge-ledger)")
    parser.add_argument("--python", required=True, help="확인에 쓸 Python 해석기")
    args = parser.parse_args(argv)

    results = run_smoke(args.root, args.python)
    for result in results:
        status = "통과" if result.ok else "실패"
        line = f"{result.name}: {status}"
        if result.detail:
            line += f" {result.detail}"
        print(line)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
