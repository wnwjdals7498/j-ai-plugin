from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hooks import stop
from ledger.worklog import Entry, LocalLog, new_entry

NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


class StopHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.log_store = LocalLog(self.path / "state")
        self.transcript = self.path / "transcript.jsonl"
        self.payload = {"session_id": "session-one", "transcript_path": str(self.transcript)}
        self.env = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
        self.write_transcript([])

    def write_transcript(self, rows: list[dict]) -> None:
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def tool(self, at: datetime) -> dict:
        return {
            "type": "assistant",
            "timestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "message": {"content": [{"type": "tool_use"}]},
        }

    def add_log(self, at: datetime) -> None:
        self.log_store.append(
            Entry(
                id="entry",
                at=at.astimezone(timezone(timedelta(hours=9))).isoformat(),
                host="host",
                source="auto",
                text="작업",
                workspace="",
                session="session-one",
            )
        )

    def decide(self):
        return stop.decide(self.payload, self.env, NOW, self.log_store)

    def test_no_tool_use(self) -> None:
        self.assertIsNone(self.decide())

    def test_no_log_and_tool_use_exact_instruction(self) -> None:
        self.write_transcript([self.tool(NOW)])
        self.assertEqual(
            self.decide(),
            '작업 일지를 남긴 뒤 종료하라: python3 "/plugin/scripts/ledger.py" '
            'log add --auto --session session-one "<이번 세션에서 한 일 1~3줄>"',
        )

    def test_recent_log_and_boundary(self) -> None:
        for minutes in (60, 10):
            with self.subTest(minutes=minutes):
                self.add_log(NOW - timedelta(minutes=minutes))
                self.write_transcript([self.tool(NOW)])
                self.assertIsNone(self.decide())

    def test_old_log_and_later_tool(self) -> None:
        self.add_log(NOW - timedelta(minutes=61))
        self.write_transcript([self.tool(NOW - timedelta(minutes=1))])
        self.assertIsNotNone(self.decide())

    def test_only_tool_before_or_at_log(self) -> None:
        self.add_log(NOW - timedelta(minutes=61))
        self.write_transcript([self.tool(NOW - timedelta(minutes=62)), self.tool(NOW - timedelta(minutes=61))])
        self.assertIsNone(self.decide())

    def test_tool_milliseconds_in_same_second_as_log(self) -> None:
        logged_at = NOW - timedelta(minutes=61) + timedelta(milliseconds=500)
        entry = new_entry("작업", source="auto", env={}, now=logged_at, session="session-one")
        self.log_store.append(entry)
        tool_row = self.tool(logged_at)
        tool_row["timestamp"] = (
            (logged_at - timedelta(milliseconds=300)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        self.write_transcript([tool_row])
        self.assertIsNone(self.decide())

    def test_active_and_disabled_and_missing_session(self) -> None:
        self.write_transcript([self.tool(NOW)])
        self.payload["stop_hook_active"] = True
        self.assertIsNone(self.decide())
        self.payload["stop_hook_active"] = False
        for key in ("FORGE_LEDGER_WORKLOG_AUTO", "CLAUDE_PLUGIN_OPTION_WORKLOG_AUTO"):
            for value in ("false", "0", "NO", "off"):
                with self.subTest(key=key, value=value):
                    self.env[key] = value
                    self.assertIsNone(self.decide())
                    del self.env[key]
        for session in ("", None):
            self.payload["session_id"] = session
            self.assertIsNone(self.decide())

    def test_main_fail_open_and_no_transcript_output(self) -> None:
        for kind in ("missing", "broken", "state"):
            with self.subTest(kind=kind):
                if kind == "broken":
                    self.transcript.write_text("private broken transcript", encoding="utf-8")
                elif kind == "missing":
                    self.transcript.unlink(missing_ok=True)
                else:
                    self.write_transcript([self.tool(NOW)])
                state = self.path / "state-file" if kind == "state" else self.path / "main-state"
                if kind == "state":
                    state.write_text("file", encoding="utf-8")
                stderr = io.StringIO()
                with (
                    patch("sys.stdin", io.StringIO(json.dumps(self.payload))),
                    patch.dict("os.environ", {"FORGE_LEDGER_STATE_DIR": str(state)}, clear=True),
                    redirect_stderr(stderr),
                ):
                    self.assertEqual(stop.main(), 0)
                self.assertEqual(stderr.getvalue(), "")

    def test_main_blocks_with_instruction(self) -> None:
        self.write_transcript([self.tool(NOW)])
        stderr = io.StringIO()
        env = {**self.env, "FORGE_LEDGER_STATE_DIR": str(self.path / "main-state")}
        with (
            patch("sys.stdin", io.StringIO(json.dumps(self.payload))),
            patch.dict("os.environ", env, clear=True),
            redirect_stderr(stderr),
        ):
            self.assertEqual(stop.main(), 2)
        self.assertIn("log add --auto --session session-one", stderr.getvalue())
