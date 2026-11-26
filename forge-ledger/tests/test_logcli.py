from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from http.client import IncompleteRead
from unittest.mock import patch
from pathlib import Path

from ledger.cli import _build_parser
from ledger.logcli import make_worklog_store, run_log
from ledger.plane import PlaneAuthError, PlaneError, PlaneUnavailable
from ledger.worklog import LocalLog, RemoteEntry, new_entry
from ledger.worklog_plane import PlaneWorklog
from tests.fakes import FakePlane as SharedFakePlane

NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)


class FakePlane:
    def __init__(self):
        self.comments = {}
        self.offline = False

    def current_user(self):
        if self.offline:
            raise PlaneUnavailable("offline")
        return "작성자"

    def ensure_project(self):
        return "project"

    def post_entry(self, entry):
        if self.offline:
            raise PlaneUnavailable("offline")
        self.comments.setdefault(entry.id, entry)
        return entry.id

    def list_entries(self, from_, to, user=None):
        if self.offline:
            raise PlaneUnavailable("offline")
        return [
            RemoteEntry(e, "작성자", e.id)
            for e in self.comments.values()
            if from_ <= e.local_date() <= to and user in (None, "작성자")
        ]


class LogCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {
            "FORGE_LEDGER_STATE_DIR": self.tmp.name,
            "FORGE_LEDGER_PLANE_WORKSPACE": "forge",
            "FORGE_LEDGER_WORKLOG_HOST": "machine",
        }
        self.store = FakePlane()
        self.out, self.err = io.StringIO(), io.StringIO()

    def run_cmd(self, *argv, store=True):
        self.out, self.err = io.StringIO(), io.StringIO()
        args = _build_parser().parse_args(["log", *argv])
        return run_log(
            args,
            self.env,
            now=lambda: NOW,
            out=self.out,
            err=self.err,
            make_store=lambda env: self.store if store else None,
        )

    def test_add_offline(self):
        self.assertEqual(self.run_cmd("add", "작업", store=False), 0)
        self.assertEqual(len(LocalLog(Path(self.tmp.name)).pending("forge")), 1)
        self.assertIn("미전송", self.out.getvalue())

    def test_add_then_sync_idempotent(self):
        self.assertEqual(self.run_cmd("add", "작업"), 0)
        self.assertIn("(전송)", self.out.getvalue())
        self.assertEqual(self.run_cmd("sync"), 0)
        self.assertIn("전송 0건, 미전송 0건", self.out.getvalue())

    def test_resync_after_crash(self):
        entry = new_entry("작업", source="manual", env=self.env, now=NOW)
        LocalLog(Path(self.tmp.name)).append(entry)
        fake = SharedFakePlane()
        self.store = PlaneWorklog(fake, "WLOG", "forge")
        project = self.store.ensure_project()
        item = self.store.ensure_month_item(entry.month())
        from ledger.worklog_plane import EXTERNAL_SOURCE, format_comment

        comment = fake.create_comment(
            project, item, format_comment(entry), external_source=EXTERNAL_SOURCE, external_id=entry.id
        )
        self.assertEqual(self.run_cmd("sync"), 0)
        self.assertEqual(len(fake.list_comments(project, item)), 1)
        sent = json.loads((Path(self.tmp.name) / "worklog" / "sent.json").read_text())
        self.assertEqual(sent[entry.id], comment["id"])

    def test_add_rejects_exposure(self):
        self.assertEqual(self.run_cmd("add", "/home/alice/x"), 1)
        self.assertEqual(LocalLog(Path(self.tmp.name)).pending("forge"), [])
        self.assertIn("home_path", self.err.getvalue())
        self.assertNotIn("alice", self.err.getvalue())

    def test_sync_other_workspace(self):
        entry = new_entry("작업", source="manual", env={**self.env, "FORGE_LEDGER_PLANE_WORKSPACE": "other"}, now=NOW)
        LocalLog(Path(self.tmp.name)).append(entry)
        self.assertEqual(self.run_cmd("sync"), 0)
        self.assertIn("다른 워크스페이스 1건", self.out.getvalue())
        self.assertFalse(self.store.comments)

    def test_show_merges_pending(self):
        self.run_cmd("add", "보낸 기록")
        self.run_cmd("add", "남은 기록", store=False)
        self.assertEqual(self.run_cmd("show"), 0)
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        sent = next(line for line in lines if "보낸 기록" in line)
        pending = next(line for line in lines if "남은 기록" in line)
        self.assertNotIn("(미전송)", sent)
        self.assertIn("(미전송)", pending)

    def test_show_server_entry_missing_sent_is_not_duplicated(self):
        entry = new_entry("중복 방지", source="manual", env=self.env, now=NOW)
        LocalLog(Path(self.tmp.name)).append(entry)
        self.store.comments[entry.id] = entry
        self.assertEqual(self.run_cmd("show"), 0)
        self.assertEqual(len(self.out.getvalue().splitlines()), 1)
        self.assertEqual(self.out.getvalue().count("중복 방지"), 1)

    def test_show_offline(self):
        self.run_cmd("add", "남은 기록", store=False)
        self.store.offline = True
        self.assertEqual(self.run_cmd("show"), 0)
        self.assertIn("Plane 연결 실패: 로컬 기록만 표시", self.out.getvalue())
        self.assertIn("남은 기록", self.out.getvalue())

    def test_sync_incomplete_read_keeps_pending(self):
        self.run_cmd("add", "작업", store=False)
        with patch.object(self.store, "post_entry", side_effect=IncompleteRead(b"")):
            self.assertEqual(self.run_cmd("sync"), 0)
        self.assertIn("미전송 1건", self.out.getvalue())

    def test_sync_auth_failure(self):
        self.run_cmd("add", "작업", store=False)
        with patch.object(self.store, "post_entry", side_effect=PlaneAuthError()):
            self.assertEqual(self.run_cmd("sync"), 1)
        self.assertIn("인증 실패", self.err.getvalue())

    def test_show_missing_project(self):
        self.run_cmd("add", "작업", store=False)
        with patch.object(self.store, "list_entries", side_effect=PlaneError("작업 일지 프로젝트 없음: WLOG")):
            self.assertEqual(self.run_cmd("show"), 0)
        self.assertIn("작업 일지 프로젝트 없음: WLOG", self.out.getvalue())
        self.assertIn("작업", self.out.getvalue())
        self.assertNotIn("연결 실패", self.out.getvalue())

    def test_at_requires_offset_and_preserves_offset(self):
        self.assertEqual(self.run_cmd("add", "작업", "--at", "2026-11-26T10:00:00", store=False), 1)
        self.assertEqual(self.run_cmd("add", "작업", "--at", "2026-11-26T10:00:00+03:00", store=False), 0)
        self.assertTrue(LocalLog(Path(self.tmp.name)).pending("forge")[0].at.endswith("+03:00"))

    def test_whitespace_in_fields_is_rejected(self):
        self.assertEqual(self.run_cmd("add", "작업", "--item", "a b", store=False), 1)
        self.assertEqual(LocalLog(Path(self.tmp.name)).pending("forge"), [])

    def test_add_generic_plane_error_remains_saved(self):
        with patch.object(self.store, "post_entry", side_effect=PlaneError("서버 거부")):
            self.assertEqual(self.run_cmd("add", "작업"), 0)
        self.assertIn("(미전송)", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "전송 실패: 서버 거부\n")
        self.assertEqual(len(LocalLog(Path(self.tmp.name)).pending("forge")), 1)

    def test_add_bad_url_saves_before_store_creation(self):
        self.env.update({"FORGE_LEDGER_PLANE_URL": "invalid", "FORGE_LEDGER_PLANE_TOKEN": "sample"})
        args = _build_parser().parse_args(["log", "add", "작업"])
        self.assertEqual(
            run_log(args, self.env, now=lambda: NOW, out=self.out, err=self.err, make_store=make_worklog_store), 0
        )
        self.assertIn("(미전송)", self.out.getvalue())
        self.assertIn("전송 실패: 설정 오류", self.err.getvalue())
        self.assertEqual(len(LocalLog(Path(self.tmp.name)).pending("forge")), 1)

    def test_sync_continues_after_generic_failure(self):
        self.run_cmd("add", "첫째", store=False)
        self.run_cmd("add", "둘째", store=False)
        original = self.store.post_entry

        def post(entry):
            if entry.text == "첫째":
                raise PlaneError("서버 거부")
            return original(entry)

        with patch.object(self.store, "post_entry", side_effect=post):
            self.assertEqual(self.run_cmd("sync"), 1)
        self.assertEqual(self.out.getvalue(), "전송 1건, 미전송 1건, 실패 1건\n")
        self.assertEqual(self.err.getvalue(), "전송 실패: 서버 거부\n")
        self.assertEqual(len(self.store.comments), 1)

    def test_input_errors_are_fixed_and_plane_error_is_explained(self):
        for args, message in [
            (("add", "작업", "--at", "2026-11-26T10:00:00"), "시각에 UTC 오프셋이 필요합니다"),
            (("add", "작업", "--at", "invalid"), "시각 형식이 올바르지 않습니다"),
            (
                ("show", "--date", "2026-11-26", "--from", "2026-11-26"),
                "날짜는 --date 또는 --from과 --to를 함께 지정하세요",
            ),
            (("show", "--date", "invalid"), "날짜 형식이 올바르지 않습니다"),
        ]:
            with self.subTest(args=args):
                self.assertEqual(self.run_cmd(*args), 1)
                self.assertEqual(self.err.getvalue(), message + "\n")
        with patch.object(self.store, "ensure_project", side_effect=PlaneError("서버 거부")):
            self.assertEqual(self.run_cmd("init"), 1)
        self.assertEqual(self.err.getvalue(), "Plane 작업 실패: 서버 거부\n")
