from __future__ import annotations

import unittest
from datetime import date

from ledger.worklog import Entry
from tests.fakes import FakePlane


class WorklogPlaneTest(unittest.TestCase):
    def setUp(self) -> None:
        from ledger.worklog_plane import PlaneWorklog

        self.fake = FakePlane().as_user("user-a", "가 이름")
        self.store = PlaneWorklog(self.fake, "WLOG", "workspace")
        self.entry = Entry(
            "a" * 32, "2026-11-26T11:25:00+09:00", "host", "manual", "첫 <줄>\n둘 & 셋", "workspace", "repo", "FL-12"
        )

    def test_format_parse_roundtrip(self) -> None:
        from ledger.worklog_plane import EXTERNAL_SOURCE, format_comment, parse_comment

        comment = {
            "external_source": EXTERNAL_SOURCE,
            "external_id": self.entry.id,
            "comment_html": format_comment(self.entry),
        }
        self.assertEqual(parse_comment(comment, "workspace"), self.entry)

    def test_parse_sanitized_comment(self) -> None:
        from ledger.worklog_plane import EXTERNAL_SOURCE, parse_comment

        comment = {
            "external_source": EXTERNAL_SOURCE,
            "external_id": self.entry.id,
            "comment_html": (
                "<p>[일지] 2026-11-26T11:25:00+09:00 pc-1 manual repo=r item=FL-1</p>"
                "<p>a 'q' \"d\" &lt;x&gt; &amp; y</p><p>둘째 줄</p>"
            ),
        }
        entry = parse_comment(comment, "workspace")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual((entry.text, entry.repo, entry.item), ("a 'q' \"d\" <x> & y\n둘째 줄", "r", "FL-1"))

    def test_list_missing_project_does_not_create(self) -> None:
        from ledger.plane import PlaneError

        with self.assertRaisesRegex(PlaneError, "작업 일지 프로젝트 없음: WLOG"):
            self.store.list_entries(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(self.fake.calls, [("list_projects",)])

    def test_post_entry_idempotent(self) -> None:
        first = self.store.post_entry(self.entry)
        self.assertEqual(self.store.post_entry(self.entry), first)
        pid = self.store.ensure_project()
        item = self.store.ensure_month_item("2026-11")
        self.assertEqual(len(self.fake.list_comments(pid, item)), 1)

    def test_month_item_race(self) -> None:
        from ledger.plane import PlaneDuplicate

        self.fake.fail_on["create_item"] = PlaneDuplicate("item-1")
        self.assertEqual(self.store.ensure_month_item("2026-11"), "item-1")

    def test_list_entries_other_user(self) -> None:
        from ledger.worklog_plane import PlaneWorklog

        self.store.post_entry(self.entry)
        other = PlaneWorklog(self.fake.as_user("user-b", "나 이름"), "WLOG", "workspace")
        entries = other.list_entries(date(2026, 11, 1), date(2026, 11, 30), user="가 이름")
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].entry, entries[0].author), (self.entry, "가 이름"))

    def test_foreign_comment_ignored(self) -> None:
        from ledger.worklog_plane import EXTERNAL_SOURCE, format_comment

        pid = self.store.ensure_project()
        item = self.store.ensure_month_item("2026-11")
        other = self.fake.as_user("user-b", "나 이름")
        other.create_comment(
            pid, item, format_comment(self.entry), external_source=EXTERNAL_SOURCE, external_id=self.entry.id
        )
        self.assertEqual(self.store.list_entries(date(2026, 11, 1), date(2026, 11, 30)), [])

    def test_current_user(self) -> None:
        self.assertEqual(self.store.current_user(), "가 이름")
