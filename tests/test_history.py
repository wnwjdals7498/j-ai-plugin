from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ledger.history import (
    HistoryEntry,
    append_entry,
    empty_history,
    format_entry,
    new_decision_id,
    parse_history,
)


class HistoryTest(unittest.TestCase):
    def test_format_and_parse(self) -> None:
        e = HistoryEntry(
            "2026-11-23",
            "decision",
            "D-261123-7f3a",
            "APP",
            "위키는 생성 구역만 쓴다",
            "plane:APP-3@2026-11-23T14:02Z",
        )
        line = format_entry(e)
        self.assertEqual(
            line,
            "2026-11-23 | decision | D-261123-7f3a | APP | 위키는 생성 구역만 쓴다 | plane:APP-3@2026-11-23T14:02Z",
        )
        self.assertEqual(parse_history(empty_history() + line + "\n"), [e])

    def test_crlf_and_header_skipped(self) -> None:
        text = empty_history().replace("\n", "\r\n") + "2026-12-05 | phase | proto | APP | 프로토타입 종료 | -\r\n"
        self.assertEqual(parse_history(text)[0].kind, "phase")

    def test_pipe_in_title_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_entry(HistoryEntry("2026-11-23", "decision", "D-261123-0000", "*", "a | b", "-"))

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_entry(HistoryEntry("2026-11-23", "note", "x", "*", "t", "-"))

    def test_decision_id_retries_on_collision(self) -> None:
        seq = iter(["7f3a", "7f3a", "0b1c"])
        got = new_decision_id(date(2026, 11, 23), {"D-261123-7f3a"}, lambda: next(seq))
        self.assertEqual(got, "D-261123-0b1c")

    def test_append_entry_creates_and_appends(self) -> None:
        e1 = HistoryEntry("2026-11-23", "decision", "D-261123-7f3a", "APP", "첫 결정", "-")
        e2 = HistoryEntry("2026-11-24", "release", "v0.1.0", "APP", "첫 배포", "tag:v0.1.0")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".ledger" / "history.md"
            append_entry(path, e1)
            append_entry(path, e2)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(parse_history(text), [e1, e2])

    def test_decision_id_default_rand4(self) -> None:
        got = new_decision_id(date(2026, 11, 23), set())
        self.assertRegex(got, r"^D-261123-[0-9a-f]{4}$")
