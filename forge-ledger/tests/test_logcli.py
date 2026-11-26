from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from http.client import IncompleteRead
from unittest.mock import patch
from pathlib import Path

from ledger.cli import _build_parser
from ledger.logcli import run_log
from ledger.plane import PlaneAuthError, PlaneError, PlaneUnavailable
from ledger.worklog import LocalLog, RemoteEntry, new_entry

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
        self.store.comments[entry.id] = entry
        self.assertEqual(self.run_cmd("sync"), 0)
        self.assertEqual(len(self.store.comments), 1)
        self.assertEqual(LocalLog(Path(self.tmp.name)).pending("forge"), [])

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
        self.assertIn("보낸 기록", self.out.getvalue())
        self.assertIn("남은 기록 (미전송)", self.out.getvalue())

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
