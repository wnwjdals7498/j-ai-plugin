from __future__ import annotations

import io
import json
import multiprocessing
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ledger.worklog import Entry, LocalLog, RemoteEntry, WorklogError, host_label, new_entry, validate


def entry(identifier: str, at: str = "2026-11-26T12:00:00+09:00", **kwargs: str) -> Entry:
    values = {"id": identifier, "at": at, "host": "pc", "source": "manual", "text": "작업", "workspace": ""}
    values.update(kwargs)
    return Entry(**values)


def append_many(state: str, prefix: str) -> None:
    log = LocalLog(Path(state))
    for number in range(200):
        log.append(entry(f"{prefix}-{number}"))


class WorklogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.log = LocalLog(self.state)

    def test_append_and_read_roundtrip(self) -> None:
        early = entry("early", "2026-11-26T08:00:00+09:00")
        late = entry("late", "2026-11-26T12:00:00+09:00")
        self.log.append(late)
        self.log.append(early)
        self.assertEqual(self.log.entries(date(2026, 11, 26), date(2026, 11, 26)), [early, late])

    def test_month_file_by_local_time(self) -> None:
        value = entry("dec", "2026-12-01T00:30:00+09:00")
        self.log.append(value)
        self.assertTrue((self.state / "worklog/2026-12.jsonl").exists())
        self.assertEqual(value.month(), "2026-12")
        self.assertEqual(self.log.entries(date(2026, 12, 1), date(2026, 12, 1)), [value])

    def test_validate_rejects(self) -> None:
        for value in ("", "   ", "가" * 401):
            with self.subTest(value=value), self.assertRaises(WorklogError):
                validate(value, "pc", "", "")
        with self.assertRaises(WorklogError) as error:
            validate("/home/alice/x", "pc", "", "")
        self.assertIn("home_path", str(error.exception))
        self.assertNotIn("alice", str(error.exception))
        for field in (("ok", "pc.local", "", ""), ("ok", "pc", "pc.local", ""), ("ok", "pc", "", "pc.local")):
            with self.subTest(field=field), self.assertRaisesRegex(WorklogError, "internal_host"):
                validate(*field)
        validate("가" * 400, "pc", "", "")

    def test_concurrent_append(self) -> None:
        processes = [multiprocessing.Process(target=append_many, args=(str(self.state), str(i))) for i in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            self.assertEqual(process.exitcode, 0)
        lines = (self.state / "worklog/2026-11.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 400)
        self.assertEqual(len({json.loads(line)["id"] for line in lines}), 400)

    def test_pending_and_mark_sent(self) -> None:
        sent = entry("sent", workspace="current")
        other = entry("other", workspace="other")
        blank = entry("blank")
        for value in (sent, other, blank):
            self.log.append(value)
        self.log.mark_sent("sent", "comment")
        self.assertEqual(self.log.pending("current"), [blank])
        self.assertEqual(self.log.pending_other("current"), 1)
        self.assertEqual(json.loads((self.state / "worklog/sent.json").read_text()), {"sent": "comment"})

    def test_permissions(self) -> None:
        self.log.append(entry("one"))
        self.log.mark_sent("one", "comment")
        directory = self.state / "worklog"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for name in ("2026-11.jsonl", "sent.json", "worklog.lock"):
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)

    def test_last_for_session(self) -> None:
        early = entry("early", session="s")
        late = entry("late", "2026-12-01T00:30:00+09:00", session="s")
        for value in (late, entry("other", session="other"), early):
            self.log.append(value)
        self.assertEqual(self.log.last_for_session("s"), late)
        self.assertIsNone(self.log.last_for_session("missing"))

    def test_new_entry_and_host_label(self) -> None:
        now = datetime(2026, 11, 26, 3, 25, tzinfo=timezone.utc)
        env = {"FORGE_LEDGER_WORKLOG_HOST": "desktop", "FORGE_LEDGER_PLANE_WORKSPACE": "team"}
        value = new_entry("작업", source="auto", env=env, now=now, repo="repo", item="A-1", session="s")
        self.assertRegex(value.id, r"^[0-9a-f]{32}$")
        self.assertEqual(value.at, now.astimezone().isoformat(timespec="seconds"))
        self.assertEqual(
            (value.host, value.workspace, value.repo, value.item, value.session),
            ("desktop", "team", "repo", "A-1", "s"),
        )
        self.assertEqual(RemoteEntry(value, "작성자", "comment").entry, value)
        with patch("ledger.worklog.socket.gethostname", return_value="desktop.example"):
            self.assertEqual(host_label({}), "desktop")
            self.assertEqual(host_label({"FORGE_LEDGER_WORKLOG_HOST": ""}), "desktop")
        with self.assertRaises(WorklogError):
            new_entry("", source="manual", env=env, now=now)

    def test_append_rejects_exposure_even_for_direct_entry(self) -> None:
        with self.assertRaises(WorklogError):
            self.log.append(entry("bad", text="/home/alice/x"))
        self.assertFalse((self.state / "worklog/2026-11.jsonl").exists())

    def test_append_after_truncated_line(self) -> None:
        path = self.state / "worklog/2026-11.jsonl"
        path.write_bytes(b'{"text":"partial' + bytes([0xEA]))
        value = entry("new")
        self.log.append(value)
        notice = io.StringIO()
        with redirect_stderr(notice):
            self.assertEqual(self.log.entries(date(2026, 11, 26), date(2026, 11, 26)), [value])
        self.assertEqual(notice.getvalue(), "손상된 일지 건너뜀: 2026-11.jsonl:1\n")
        self.assertNotIn("partial", notice.getvalue())

    def test_corrupt_line_does_not_hide_valid_entries(self) -> None:
        first = entry("first", session="s")
        last = entry("last", "2026-12-01T00:30:00+09:00", session="s", workspace="other")
        self.log.append(first)
        path = self.state / "worklog/2026-11.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write('{"text":"private-content"\n')
        self.log.append(last)
        for operation, expected in (
            (lambda: self.log.entries(date(2026, 11, 1), date(2026, 12, 31)), [first, last]),
            (lambda: self.log.entries(date(2026, 12, 1), date(2026, 12, 1)), [last]),
            (lambda: self.log.pending("current"), [first]),
            (lambda: self.log.pending_other("current"), 1),
            (lambda: self.log.last_for_session("s"), last),
        ):
            notice = io.StringIO()
            with redirect_stderr(notice):
                self.assertEqual(operation(), expected)
            self.assertEqual(notice.getvalue(), "손상된 일지 건너뜀: 2026-11.jsonl:2\n")
            self.assertNotIn("private-content", notice.getvalue())
