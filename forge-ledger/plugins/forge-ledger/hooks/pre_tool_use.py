#!/usr/bin/env python3
"""PreToolUse hook: block secrets in `.ledger/` writes and Plane MCP inputs."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if not sys.path or sys.path[0] != _PLUGIN_ROOT:
    sys.path[:] = [_PLUGIN_ROOT, *(p for p in sys.path if p != _PLUGIN_ROOT)]

try:
    from ledger.secrets import find_secrets  # noqa: E402
except Exception:  # 플러그인 모듈을 못 읽어도 세션을 막지 않는다(fail-open)
    if __name__ == "__main__":
        sys.exit(0)
    raise

_FILE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
_PLANE_MCP_PREFIX = "mcp__plugin_forge-ledger_plane__"


def _is_ledger_path(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    return "/.ledger/" in normalized or normalized.startswith(".ledger/")


def _extract_file_texts(tool_input: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ("content", "new_string"):
        val = tool_input.get(key)
        if isinstance(val, str):
            texts.append(val)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for item in edits:
            if isinstance(item, dict):
                new_str = item.get("new_string")
                if isinstance(new_str, str):
                    texts.append(new_str)
    return texts


def _format_block_message(names: list[str]) -> str:
    return f"forge-ledger: 비밀값({', '.join(names)})이 있어 쓰기를 막았습니다"


def should_block(payload: dict[str, Any]) -> Optional[str]:
    """Return a block reason message if payload contains secrets in protected targets."""
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None

    found: set[str] = set()
    if tool_name in _FILE_WRITE_TOOLS:
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not _is_ledger_path(file_path):
            return None
        for text in _extract_file_texts(tool_input):
            found.update(find_secrets(text))
    elif tool_name.startswith(_PLANE_MCP_PREFIX):
        serialized = json.dumps(tool_input, ensure_ascii=False)
        found.update(find_secrets(serialized))
    else:
        return None

    if not found:
        return None
    return _format_block_message(sorted(found))


def main() -> int:
    """Run PreToolUse hook; exit 2 when blocked, fail open (0) on any other error."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        reason = should_block(payload)
    except Exception:
        return 0

    if reason is not None:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
