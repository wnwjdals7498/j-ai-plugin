#!/usr/bin/env python3
"""Require a worklog before stopping when the session has unlogged tool use."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_PLUGIN_ROOT = str(Path(__file__).resolve().parents[1])
if not sys.path or sys.path[0] != _PLUGIN_ROOT:
    sys.path[:] = [_PLUGIN_ROOT, *(p for p in sys.path if p != _PLUGIN_ROOT)]

try:
    from ledger.ratelimit import default_state_dir  # noqa: E402
    from ledger.worklog import LocalLog  # noqa: E402
except Exception:  # 플러그인 모듈을 못 읽어도 세션을 막지 않는다(fail-open)
    if __name__ == "__main__":
        sys.exit(0)
    raise

AUTO_INTERVAL_MINUTES = 60


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


def decide(payload: dict[str, Any], env: Mapping[str, str], now: datetime, local: LocalLog) -> Optional[str]:
    """Return the instruction when the current session needs a worklog."""
    if any(
        env.get(key, "").strip().lower() in {"false", "0", "no", "off"}
        for key in ("FORGE_LEDGER_WORKLOG_AUTO", "CLAUDE_PLUGIN_OPTION_WORKLOG_AUTO")
    ):
        return None
    session = payload.get("session_id")
    if not session or payload.get("stop_hook_active"):
        return None
    last = local.last_for_session(session)
    last_at = _timestamp(last.at) if last else None
    if last_at is not None and now - last_at <= timedelta(minutes=AUTO_INTERVAL_MINUTES):
        return None
    with Path(payload["transcript_path"]).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("type") != "assistant":
                continue
            content = row.get("message", {}).get("content", [])
            if not isinstance(content, list) or not any(item.get("type") == "tool_use" for item in content):
                continue
            if last_at is None or _timestamp(row["timestamp"]).replace(microsecond=0) > last_at:
                root = env.get("CLAUDE_PLUGIN_ROOT") or _PLUGIN_ROOT
                return (
                    f'작업 일지를 남긴 뒤 종료하라: python3 "{root}/scripts/ledger.py" '
                    f'log add --auto --session {session} "<이번 세션에서 한 일 1~3줄>"'
                )
    return None


def main() -> int:
    """Fail open without exposing transcript contents on errors."""
    try:
        payload = json.load(sys.stdin)
        env = os.environ
        local = LocalLog(default_state_dir(env))
        instruction = decide(payload, env, datetime.now(timezone.utc), local)
        if instruction:
            print(instruction, file=sys.stderr)
            return 2
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
