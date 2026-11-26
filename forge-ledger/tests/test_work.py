from __future__ import annotations

import html
import io
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ledger import work
from ledger.cli import main
from ledger.flows import SecretFound
from ledger.plane import PlaneUnavailable
from tests.fakes import FakePlane, FakePlaneServer

NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
ME = "user-1"
OTHER = "user-2"


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Tracker(FakePlane):
    """댓글 시각을 시험이 정하고, 댓글을 최신순으로 돌려주는 트래커(서버 순서는 보장되지 않는다)."""

    def __init__(self) -> None:
        super().__init__()
        self.now = NOW
        self._written = 0
        self.after_create: list[tuple[str, datetime, str]] = []  # 내 댓글 직후에 끼워 넣을 댓글
        self.next_ids: list[str] = []  # 다음에 쓰는 댓글들의 id
        self.same_time = False  # 켜면 이어 쓰는 댓글이 같은 작성 시각을 받는다
        self.lose_response = False  # 다음 댓글은 저장한 뒤 응답을 잃는다
        self.project_id = self.create_project("sample", "FL")["id"]
        item = self.create_item(self.project_id, "작업", "", [])
        self.item_id = str(item["id"])
        self.calls.clear()

    def create_comment(self, project_id: str, item_id: str, html: str) -> dict[str, Any]:
        created = super().create_comment(project_id, item_id, html)
        self._written += 0 if self.same_time and self._written else 1
        created["created_at"] = _stamp(self.now + timedelta(milliseconds=self._written))
        if self.next_ids:
            created["id"] = self.next_ids.pop(0)
        self._project_by_id(project_id).comments[item_id][-1].update(created)
        pending, self.after_create = self.after_create, []
        for text, at, actor in pending:
            self.add(text, at, actor)
        if self.lose_response:
            self.lose_response = False
            raise PlaneUnavailable("Plane 연결 실패")
        return created

    def list_comments(self, project_id: str, item_id: str) -> list[dict[str, Any]]:
        return list(reversed(super().list_comments(project_id, item_id)))

    # --- 시험 도우미 ---

    def add(self, text: str, at: datetime, actor: str = OTHER, comment_id: str = "") -> str:
        comments = self._project_by_id(self.project_id).comments.setdefault(self.item_id, [])
        comment_id = comment_id or f"seed-{len(comments)}"
        comments.append(
            {
                "id": comment_id,
                "comment_html": f'<p class="editor">{html.escape(text, quote=False)}</p>',
                "created_at": _stamp(at),
                "actor": actor,
            }
        )
        return comment_id

    def ids(self) -> list[str]:
        return [str(c["id"]) for c in self._project_by_id(self.project_id).comments.get(self.item_id, [])]

    def assign(self, *user_ids: str) -> None:
        self._stored_item(self.project_id, self.item_id)["assignees"] = list(user_ids)

    def item(self) -> dict[str, Any]:
        return self._copy_item(self._stored_item(self.project_id, self.item_id))

    def lines(self) -> list[str]:
        comments = sorted(
            self._project_by_id(self.project_id).comments.get(self.item_id, []), key=lambda c: c["created_at"]
        )
        return [html.unescape(re.sub(r"<[^>]+>", "", c["comment_html"].split("</p>")[0])) for c in comments]

    def group(self) -> str:
        return str(self.item()["state"]).rsplit("-", 1)[-1]

    def label_names(self) -> list[str]:
        names = {label["id"]: label["name"] for label in self._project_by_id(self.project_id).labels}
        return sorted(names[i] for i in self.item()["labels"])

    def writes(self) -> list[str]:
        return [c[0] for c in self.calls if c[0] in ("create_comment", "update_item", "create_label")]


class WorkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = _Tracker()

    def _later(self, minutes: int) -> datetime:
        self.t.now = NOW + timedelta(minutes=minutes)
        return self.t.now

    # 정상 흐름

    def test_full_cycle(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.group(), "started")
        self.assertEqual(self.t.item()["assignees"], [ME])
        work.note(self.t, "FL-1", "모듈 작성", "시험 추가", self._later(1))
        work.verify(self.t, "FL-1", "pytest -q", 0, HEAD, self._later(2))
        work.end(self.t, "FL-1", "done", self._later(3), HEAD, host="alpha", result="완료")

        self.assertEqual(self.t.group(), "completed")
        self.assertEqual(
            self.t.lines(),
            [
                "[착수] alpha 2026-11-26T12:00:00+00:00",
                "[인계] did: 모듈 작성 / next: 시험 추가",
                "[검증] head=aaaaaaa exit=0 cmd=pytest -q",
                "[종료] done result: 완료",
            ],
        )

    def test_verify_limits_go_below_the_first_line(self) -> None:
        work.verify(self.t, "FL-1", "pytest", 1, HEAD, NOW, limits="통합 시험 제외")
        comment = self.t.list_comments(self.t.project_id, self.t.item_id)[0]["comment_html"]
        self.assertEqual(comment, "<p>[검증] head=aaaaaaa exit=1 cmd=pytest</p><p>한계: 통합 시험 제외</p>")

    def test_multiline_values_stay_on_the_first_line(self) -> None:
        work.note(self.t, "FL-1", "첫 줄\n둘째 줄", "a < b", NOW)
        self.assertEqual(self.t.lines(), ["[인계] did: 첫 줄 둘째 줄 / next: a < b"])

    # 점유

    def _claimed_by_other(self, minutes_ago: int) -> None:
        self.t.assign(OTHER)
        self.t.add("[착수] beta 2026-11-26T11:00:00+00:00", NOW - timedelta(minutes=minutes_ago))

    def test_fresh_claim_is_refused_even_with_take(self) -> None:
        self._claimed_by_other(10)
        for take in (False, True):
            with self.assertRaisesRegex(work.WorkError, f"^점유 중: {OTHER} 10분 전$"):
                work.start(self.t, "FL-1", "alpha", NOW, take=take)
        self.assertEqual(self.t.writes(), [])

    def test_claim_is_fresh_at_exactly_the_limit(self) -> None:
        self._claimed_by_other(work.CLAIM_STALE_MINUTES)
        with self.assertRaisesRegex(work.WorkError, "30분 전$"):
            work.start(self.t, "FL-1", "alpha", NOW, take=True)

    def test_stale_claim_needs_take(self) -> None:
        self._claimed_by_other(40)
        with self.assertRaisesRegex(work.WorkError, f"점유 중: {OTHER} 40분 전.*--take"):
            work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.writes(), [])

        work.start(self.t, "FL-1", "alpha", NOW, take=True)
        self.assertEqual(self.t.item()["assignees"], [ME])
        self.assertEqual(self.t.group(), "started")

    def test_heartbeat_comments_keep_the_claim(self) -> None:
        for text in ("[인계] did: a / next: b", "[검증] head=aaaaaaa exit=0 cmd=x"):
            with self.subTest(text=text):
                t = _Tracker()
                t.assign(OTHER)
                t.add("[착수] beta 2026-11-26T11:00:00+00:00", NOW - timedelta(minutes=50))
                t.add(text, NOW - timedelta(minutes=5))
                t.add("관계없는 댓글", NOW - timedelta(minutes=1))
                with self.assertRaisesRegex(work.WorkError, "5분 전$"):
                    work.start(t, "FL-1", "alpha", NOW, take=True)

    def test_assigned_by_hand_without_records_needs_take(self) -> None:
        self.t.assign(OTHER)
        with self.assertRaisesRegex(work.WorkError, f"점유 중: {OTHER}.*--take"):
            work.start(self.t, "FL-1", "alpha", NOW)
        work.start(self.t, "FL-1", "alpha", NOW, take=True)
        self.assertEqual(self.t.item()["assignees"], [ME])

    def test_ended_claim_is_free(self) -> None:
        self._claimed_by_other(10)
        self.t.add("[종료] pause", NOW - timedelta(minutes=2))
        work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.item()["assignees"], [ME])

    def test_same_user_other_host_within_window_is_refused(self) -> None:
        self.t.assign(ME)
        self.t.add("[착수] beta 2026-11-26T11:50:00+00:00", NOW - timedelta(minutes=10), ME)
        with self.assertRaisesRegex(work.WorkError, "beta"):
            work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.item()["assignees"], [ME])
        self.assertEqual(self.t.lines()[-1], f"[착수 취소] alpha 2026-11-26T12:00:00+00:00 ref={self.t.ids()[-2]}")
        self.assertNotIn("update_item", self.t.writes())

    def test_same_host_can_start_again(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        work.start(self.t, "FL-1", "alpha", self._later(5))
        self.assertEqual([line.split()[0] for line in self.t.lines()], ["[착수]", "[착수]"])

    def test_pause_then_another_host_starts_at_once(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        work.end(self.t, "FL-1", "pause", self._later(1), HEAD, host="alpha")
        work.start(self.t, "FL-1", "beta", self._later(2))
        self.assertEqual(self.t.group(), "started")
        self.assertEqual(self.t.label_names(), [])

    # 경합

    def test_race_loser_leaves_the_item_untouched_and_records_cancel(self) -> None:
        before = self.t.item()
        self.t.after_create = [("[착수] beta 2026-11-26T11:59:59+00:00", NOW - timedelta(seconds=1), OTHER)]
        with self.assertRaisesRegex(work.WorkError, "beta"):
            work.start(self.t, "FL-1", "alpha", NOW)

        after = self.t.item()
        self.assertEqual((after["assignees"], after["state"]), (before["assignees"], before["state"]))
        stamp = "2026-11-26T12:00:00+00:00"
        self.assertEqual(
            self.t.lines(),
            [
                "[착수] beta 2026-11-26T11:59:59+00:00",
                f"[착수] alpha {stamp}",
                f"[착수 취소] alpha {stamp} ref={self.t.ids()[0]}",
            ],
        )

    def test_race_loser_does_not_remove_the_winner_assignment(self) -> None:
        # 이긴 쪽이 담당자를 바꾼 뒤에 진 쪽이 끝나도 이긴 쪽 담당이 남는다.
        original = self.t.create_comment

        def create_comment(project_id: str, item_id: str, body: str) -> dict[str, Any]:
            created = original(project_id, item_id, body)
            self.t.assign(OTHER)
            return created

        self.t.after_create = [("[착수] beta 2026-11-26T11:59:59+00:00", NOW - timedelta(seconds=1), OTHER)]
        with patch.object(self.t, "create_comment", create_comment), self.assertRaises(work.WorkError):
            work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.item()["assignees"], [OTHER])

    def test_same_timestamp_race_with_reversed_ids_makes_the_later_starter_lose(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        comments = self.t._project_by_id(self.t.project_id).comments[self.t.item_id]
        comments[0]["id"] = "z-first"  # 먼저 쓴 댓글의 id가 뒤에 정렬된다
        self.t._written = 0  # 다음 댓글도 같은 작성 시각을 받는다
        with self.assertRaisesRegex(work.WorkError, "alpha"):
            work.start(self.t, "FL-1", "beta", NOW)

        self.assertEqual(comments[0]["created_at"], comments[1]["created_at"])
        self.assertLess(comments[1]["id"], comments[0]["id"])
        self.assertEqual(
            comments[-1]["comment_html"],
            f"<p>[착수 취소] beta 2026-11-26T12:00:00+00:00 ref={comments[1]['id']}</p>",
        )

    def test_race_winner_ignores_later_start(self) -> None:
        self.t.after_create = [("[착수] beta 2026-11-26T12:00:01+00:00", NOW + timedelta(seconds=1), OTHER)]
        work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.item()["assignees"], [ME])
        self.assertFalse(any(line.startswith("[착수 취소]") for line in self.t.lines()))

    def test_cancelled_and_old_starts_are_not_rivals(self) -> None:
        self.t.add("[착수] gamma 2026-11-26T11:00:00+00:00", NOW - timedelta(minutes=31))
        beta = self.t.add("[착수] beta 2026-11-26T11:50:00+00:00", NOW - timedelta(minutes=10))
        self.t.add(f"[착수 취소] beta 2026-11-26T11:50:01+00:00 ref={beta}", NOW - timedelta(minutes=9))
        work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.item()["assignees"], [ME])

    def test_cancelled_start_is_not_a_heartbeat(self) -> None:
        self._claimed_by_other(40)
        gamma = self.t.add("[착수] gamma 2026-11-26T11:55:00+00:00", NOW - timedelta(minutes=5))
        self.t.add(f"[착수 취소] gamma 2026-11-26T11:55:01+00:00 ref={gamma}", NOW - timedelta(minutes=4))
        work.start(self.t, "FL-1", "alpha", NOW, take=True)
        self.assertEqual(self.t.item()["assignees"], [ME])

    def test_same_timestamp_cancel_with_reversed_ids_allows_retry(self) -> None:
        self.t.next_ids, self.t.same_time = ["z-start", "a-cancel"], True
        gamma_at = NOW - timedelta(seconds=1)
        self.t.after_create = [("[착수] gamma 2026-11-26T11:59:59+00:00", gamma_at, OTHER)]
        with self.assertRaisesRegex(work.WorkError, "gamma"):
            work.start(self.t, "FL-1", "beta", NOW)
        self.assertEqual(self.t.lines()[-1], "[착수 취소] beta 2026-11-26T12:00:00+00:00 ref=z-start")
        gamma = next(i for i in self.t.ids() if i.startswith("seed-"))
        self.t.add(f"[착수 취소] gamma 2026-11-26T12:00:00+00:00 ref={gamma}", NOW)

        self.t.same_time = False
        work.start(self.t, "FL-1", "delta", self._later(1))
        self.assertEqual(self.t.item()["assignees"], [ME])

    def test_cancel_removes_exactly_the_referenced_start(self) -> None:
        self.t.add("[착수] beta 2026-11-26T11:50:00+00:00", NOW - timedelta(minutes=10), comment_id="b-keep")
        at = NOW - timedelta(minutes=5)
        self.t.add("[착수] beta 2026-11-26T11:55:00+00:00", at, comment_id="z-start")
        self.t.add("[착수 취소] beta 2026-11-26T11:55:00+00:00 ref=z-start", at, comment_id="a-cancel")
        with self.assertRaisesRegex(work.WorkError, "beta의 착수.10분 전"):
            work.start(self.t, "FL-1", "alpha", NOW)

    # 입력 검사

    def test_note_length_limit(self) -> None:
        for did, next_ in (("가" * 401, "b"), ("a", "가" * 401)):
            with self.assertRaisesRegex(work.WorkError, "400자"):
                work.note(self.t, "FL-1", did, next_, NOW)
        self.assertEqual(self.t.writes(), [])
        work.note(self.t, "FL-1", "가" * 400, "나" * 400, NOW)
        self.assertEqual(self.t.writes(), ["create_comment"])

    def test_empty_values_are_rejected(self) -> None:
        with self.assertRaises(work.WorkError):
            work.note(self.t, "FL-1", " ", "b", NOW)
        with self.assertRaises(work.WorkError):
            work.verify(self.t, "FL-1", "", 0, HEAD, NOW)
        with self.assertRaises(work.WorkError):
            work.verify(self.t, "FL-1", "pytest", 0, "", NOW)
        self.assertEqual(self.t.writes(), [])

    def test_exposures_are_never_written(self) -> None:
        leak = "/home/someone/project"
        calls = (
            lambda: work.start(self.t, "FL-1", leak, NOW),
            lambda: work.note(self.t, "FL-1", f"{leak} 수정", "b", NOW),
            lambda: work.verify(self.t, "FL-1", f"pytest {leak}", 0, HEAD, NOW),
            lambda: work.verify(self.t, "FL-1", "pytest", 0, HEAD, NOW, limits=leak),
            lambda: work.end(self.t, "FL-1", "fail", NOW, HEAD, host="alpha", cause=leak, fix="b"),
            lambda: work.end(self.t, "FL-1", "done", NOW, HEAD, host="alpha", result="r", unverified=leak),
        )
        for call in calls:
            with self.assertRaisesRegex(SecretFound, "home_path") as caught:
                call()
            self.assertNotIn(leak, str(caught.exception))
        self.assertEqual(self.t.writes(), [])

    # 종료

    def test_done_requires_verify_of_current_head(self) -> None:
        work.verify(self.t, "FL-1", "pytest", 0, OTHER_HEAD, NOW)
        self.t.calls.clear()
        with self.assertRaisesRegex(work.WorkError, "unverified"):
            work.end(self.t, "FL-1", "done", NOW, HEAD, host="alpha", result="완료")
        self.assertEqual(self.t.writes(), [])

        work.end(self.t, "FL-1", "done", self._later(1), HEAD, host="alpha", result="완료", unverified="문서만 수정")
        self.assertEqual(self.t.lines()[-1], "[종료] done result: 완료 / unverified: 문서만 수정")
        self.assertEqual(self.t.group(), "completed")

    def test_end_modes_map_to_state_and_label(self) -> None:
        cases: tuple[tuple[str, dict[str, str], str, list[str], str], ...] = (
            ("pause", {}, "unstarted", ["paused"], "[종료] pause"),
            ("fail", {"cause": "원인", "fix": "조치"}, "cancelled", ["failed"], "[종료] fail cause: 원인 / fix: 조치"),
            ("skip", {"reason": "불필요"}, "cancelled", ["skipped"], "[종료] skip reason: 불필요"),
        )
        for mode, fields, group, labels, line in cases:
            with self.subTest(mode=mode):
                t = _Tracker()
                work.end(t, "FL-1", mode, NOW, "", host="alpha", **fields)  # type: ignore[arg-type]
                self.assertEqual((t.group(), t.label_names(), t.lines()), (group, labels, [line]))

    def test_status_labels_are_replaced_and_others_kept(self) -> None:
        keep = self.t.create_label(self.t.project_id, "blocked", "#000000")
        old = self.t.create_label(self.t.project_id, "paused", "#000000")
        self.t.update_item(self.t.project_id, self.t.item_id, {"labels": [keep["id"], old["id"]]})
        work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.label_names(), ["blocked"])
        work.end(self.t, "FL-1", "skip", self._later(1), "", host="alpha", reason="불필요")
        self.assertEqual(self.t.label_names(), ["blocked", "skipped"])

    def test_end_requires_mode_fields(self) -> None:
        cases: tuple[tuple[str, dict[str, str]], ...] = (
            ("done", {"unverified": "x"}),
            ("fail", {"cause": "x"}),
            ("fail", {"fix": "x"}),
            ("skip", {}),
            ("later", {}),
        )
        for mode, fields in cases:
            with self.subTest(mode=mode, fields=fields), self.assertRaises(work.WorkError):
                work.end(self.t, "FL-1", mode, NOW, HEAD, host="alpha", **fields)  # type: ignore[arg-type]
        self.assertEqual(self.t.writes(), [])

    # 쓰기 실패

    def test_end_state_update_failure_leaves_no_end_comment_and_rerun_works(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        self.t.fail_on["update_item"] = PlaneUnavailable("Plane 요청 실패: HTTP 500")
        with self.assertRaises(PlaneUnavailable):
            work.end(self.t, "FL-1", "pause", self._later(1), HEAD, host="alpha")
        self.assertEqual([line.split()[0] for line in self.t.lines()], ["[착수]"])
        self.assertEqual(self.t.group(), "started")

        del self.t.fail_on["update_item"]
        work.end(self.t, "FL-1", "pause", self._later(2), HEAD, host="alpha")
        self.assertEqual((self.t.group(), self.t.label_names()), ("unstarted", ["paused"]))
        self.assertEqual(self.t.lines()[1:], ["[종료] pause"])

    def test_end_comment_failure_asks_for_rerun_which_writes_only_the_comment(self) -> None:
        self.t.fail_on["create_comment"] = PlaneUnavailable("Plane 요청 실패: HTTP 500")
        with self.assertRaisesRegex(work.WorkError, "end를 다시 실행.*HTTP 500"):
            work.end(self.t, "FL-1", "pause", NOW, HEAD, host="alpha")
        self.assertEqual(self.t.writes(), ["create_label", "update_item", "create_comment"])  # 댓글은 한 번만 시도

        del self.t.fail_on["create_comment"]
        self.t.calls.clear()
        work.end(self.t, "FL-1", "pause", self._later(1), HEAD, host="alpha")
        self.assertEqual(self.t.writes(), ["create_comment"])
        self.assertEqual(self.t.lines(), ["[종료] pause"])

    def _end_with_lost_response(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        self.t.lose_response = True
        with self.assertRaisesRegex(work.WorkError, "end를 다시 실행"):
            work.end(self.t, "FL-1", "pause", self._later(1), HEAD, host="alpha")

    def test_end_rerun_after_lost_response_is_refused_once_another_host_started(self) -> None:
        self._end_with_lost_response()
        work.start(self.t, "FL-1", "beta", self._later(2))  # 같은 계정의 다른 컴퓨터
        before = (self.t.item(), self.t.lines())
        self.t.calls.clear()
        with self.assertRaisesRegex(work.WorkError, "beta"):
            work.end(self.t, "FL-1", "pause", self._later(3), HEAD, host="alpha")
        self.assertEqual(self.t.writes(), [])
        self.assertEqual((self.t.item(), self.t.lines()), before)

    def test_end_rerun_after_lost_response_succeeds_without_a_second_end_comment(self) -> None:
        self._end_with_lost_response()
        before = self.t.item()
        self.t.calls.clear()
        message = work.end(self.t, "FL-1", "pause", self._later(2), HEAD, host="alpha")
        self.assertIn("[종료] pause", message)
        self.assertEqual(self.t.writes(), [])
        self.assertEqual(self.t.item(), before)
        self.assertEqual([line.split()[0] for line in self.t.lines()], ["[착수]", "[종료]"])

    def test_end_rerun_without_a_start_is_recognised_by_the_same_end_line(self) -> None:
        self.t.lose_response = True
        with self.assertRaises(work.WorkError):
            work.end(self.t, "FL-1", "skip", NOW, "", reason="불필요", host="alpha")
        self.t.calls.clear()
        work.end(self.t, "FL-1", "skip", self._later(1), "", reason="불필요", host="alpha")
        self.assertEqual(self.t.writes(), [])

        work.start(self.t, "FL-1", "beta", self._later(2))
        self.t.calls.clear()
        with self.assertRaisesRegex(work.WorkError, "beta"):
            work.end(self.t, "FL-1", "skip", self._later(3), "", reason="불필요", host="alpha")
        self.assertEqual(self.t.writes(), [])

    def test_cancelled_attempt_by_another_host_does_not_block_end(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        with self.assertRaises(work.WorkError):
            work.start(self.t, "FL-1", "beta", self._later(5))
        work.end(self.t, "FL-1", "pause", self._later(6), HEAD, host="alpha")
        self.assertEqual(self.t.group(), "unstarted")
        self.assertEqual(self.t.lines()[-1], "[종료] pause")

    def test_same_host_name_of_another_user_counts_as_another_computer(self) -> None:
        work.start(self.t, "FL-1", "alpha", NOW)
        self.t.add("[착수] alpha 2026-11-26T12:40:00+00:00", NOW + timedelta(minutes=40), OTHER)
        with self.assertRaisesRegex(work.WorkError, "거부"):
            work.end(self.t, "FL-1", "pause", self._later(41), HEAD, host="alpha")

    def test_next_cycle_on_the_same_host_can_end_again(self) -> None:
        for minute in (0, 2):
            work.start(self.t, "FL-1", "alpha", self._later(minute))
            work.end(self.t, "FL-1", "pause", self._later(minute + 1), HEAD, host="alpha")
        self.assertEqual([line.split()[0] for line in self.t.lines()], ["[착수]", "[종료]"] * 2)

    def test_start_comment_failure_surfaces_and_changes_nothing(self) -> None:
        self.t.fail_on["create_comment"] = PlaneUnavailable("Plane 요청 실패: HTTP 500")
        with self.assertRaises(PlaneUnavailable):
            work.start(self.t, "FL-1", "alpha", NOW)
        self.assertEqual(self.t.writes(), ["create_comment"])  # 한 번만 시도한다


class WorkCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "sample-app"
        self.repo.mkdir()
        self._git("init", "--quiet")
        identity = ("-c", "user.name=tester", "-c", "user.email=tester@example.com")
        self._git(*identity, "commit", "--quiet", "--allow-empty", "-m", "init")
        self.head = self._git("rev-parse", "HEAD")
        self.server = FakePlaneServer()
        self.addCleanup(self.server.close)
        self.env = {
            "FORGE_LEDGER_PLANE_URL": self.server.base_url,
            "FORGE_LEDGER_PLANE_WORKSPACE": "forge",
            "FORGE_LEDGER_PLANE_TOKEN": "plane_api_" + "0" * 32,
            "FORGE_LEDGER_STATE_DIR": str(Path(tmp.name) / "state"),
        }
        self.server.projects["p1"] = {"id": "p1", "identifier": "FL", "name": "sample"}
        self.server.states["p1"] = [
            {"id": f"state-{group}", "group": group}
            for group in ("backlog", "unstarted", "started", "completed", "cancelled")
        ]
        self.server.items["p1"] = {
            "i1": {"id": "i1", "sequence_id": 1, "assignees": [], "labels": [], "updated_at": "2026-11-23T00:00:00Z"}
        }
        host = patch("ledger.cli.socket.gethostname", return_value="alpha.local")
        host.start()
        self.addCleanup(host.stop)

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["work", *args, "--repo", str(self.repo)], env=self.env)
        return code, out.getvalue(), err.getvalue()

    def _comments(self) -> list[str]:
        return [str(c["comment_html"]) for c in self.server.comments.get("i1", [])]

    def test_cycle_through_cli(self) -> None:
        for args in (
            ("start", "FL-1"),
            ("note", "FL-1", "--did", "작성", "--next", "시험"),
            ("verify", "FL-1", "--cmd", "pytest -q", "--exit", "0"),
            ("end", "FL-1", "--done", "--result", "완료"),
        ):
            code, out, err = self._run(*args)
            self.assertEqual(code, 0, err)
            self.assertIn("FL-1", out)
        comments = self._comments()
        # 짧은 호스트 이름만 남긴다. `.local` 이름은 노출 검사에 걸린다.
        self.assertTrue(comments[0].startswith("<p>[착수] alpha "), comments[0])
        self.assertEqual(comments[2], f"<p>[검증] head={self.head[:7]} exit=0 cmd=pytest -q</p>")
        self.assertEqual(self.server.items["p1"]["i1"]["state"], "state-completed")

    def test_verify_run_records_the_real_exit_code(self) -> None:
        code, out, err = self._run("verify", "FL-1", "--cmd", "true && exit 3", "--run", "--limits", "일부")
        self.assertEqual(code, 1, err)
        self.assertIn("exit=3", out)
        self.assertEqual(
            self._comments(),
            [f"<p>[검증] head={self.head[:7]} exit=3 cmd=true &amp;&amp; exit 3</p><p>한계: 일부</p>"],
        )
        self.assertEqual(self._run("verify", "FL-1", "--cmd", "test -d .git", "--run")[0], 0)

    def test_verify_run_refuses_to_record_when_head_changes(self) -> None:
        identity = "-c user.name=tester -c user.email=tester@example.com"
        command = f"git {identity} commit --quiet --allow-empty -m next"
        code, out, err = self._run("verify", "FL-1", "--cmd", command, "--run")
        self.assertNotEqual(self._git("rev-parse", "HEAD"), self.head)
        self.assertEqual(code, 1, out)
        self.assertIn("검증 중 HEAD 변경", err)
        self.assertEqual(self._comments(), [])

    def test_verify_run_checks_exposure_before_running(self) -> None:
        marker = self.repo / "ran"
        code, _, err = self._run("verify", "FL-1", "--cmd", "touch ran # /home/someone", "--run")
        self.assertEqual(code, 4, err)
        self.assertFalse(marker.exists())
        self.assertEqual(self._comments(), [])

    def test_cli_errors(self) -> None:
        code, _, err = self._run("end", "FL-1", "--done", "--result", "완료")
        self.assertEqual(code, 1)
        self.assertIn("unverified", err)
        for args in (
            ("verify", "FL-1", "--cmd", "true"),
            ("verify", "FL-1", "--cmd", "true", "--run", "--exit", "0"),
            ("end", "FL-1"),
            ("end", "FL-1", "--done", "--pause"),
        ):
            self.assertEqual(self._run(*args)[0], 1, args)
        self.assertEqual(self._comments(), [])

    def test_pause_works_outside_git_and_done_does_not(self) -> None:
        self.repo = self.repo.parent / "plain"
        self.repo.mkdir()
        self.assertEqual(self._run("end", "FL-1", "--pause")[0], 0)
        code, _, err = self._run("end", "FL-1", "--done", "--result", "완료")
        self.assertEqual(code, 1)
        self.assertIn("HEAD", err)

    def test_comment_5xx_is_not_retried(self) -> None:
        # 항목 찾기(프로젝트 목록, 항목 목록)는 통과하고 그다음 댓글 생성만 실패한다.
        self.server.status_queue = [200, 200, 500]
        code, out, err = self._run("note", "FL-1", "--did", "a", "--next", "b")
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertIn("HTTP 500", err)
        self.assertEqual([r.method for r in self.server.requests], ["GET", "GET", "POST"])


if __name__ == "__main__":
    unittest.main()
