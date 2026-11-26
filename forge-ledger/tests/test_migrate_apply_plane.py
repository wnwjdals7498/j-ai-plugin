from __future__ import annotations

import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from ledger.cli import main
from ledger.flows import FlowError
from ledger.migrate import ApplyPlan, load_manifest
from ledger.migrate_apply import AppliedState, Outcome, apply_git, apply_plane, plan_apply, run_apply
from ledger.plane import PlaneUnavailable
from ledger.statusmap import STATUS_MAP, pick_state
from tests.fakes import FakePlane
from tests.test_migrate_apply_git import _ApplyCase

_P = "pmt:demo"
_SOURCE = "forge-ledger-pmt"
_W1, _W2 = f"{_P}/_default/Work1", f"{_P}/_default/Work2"
_I1, _I2, _I3, _I4, _I5 = (f"{_P}/_default/Work1-{n}" for n in range(1, 6))
_B1, _B2, _B3 = f"{_P}/B1", f"{_P}/B2", f"{_P}/B3"
_C1, _C2, _C10 = f"{_I1}#c1", f"{_I1}#c2", f"{_I1}#c10"
_CW, _CB = f"{_W1}#c1", f"{_B1}#c1"
_PROJECT, _D1, _F1 = f"{_P}/project", f"{_P}/D1", f"{_P}/F1"
_ITEM_KEYS = [_W1, _W2, _I1, _I3, _I4, _I2, _I5, _B1, _B2, _B3]
_COMMENT_KEYS = [_CW, _C1, _C2, _C10, _CB]
_PLANE_KEYS = [*_ITEM_KEYS, *_COMMENT_KEYS]
_GIT_KEYS = [_PROJECT, _D1, _F1]
_WRITE_METHODS = ("create_item", "create_comment", "create_label", "update_item", "update_comment")


def _item(key: str, kind: str, status: str, name: str, labels: list[str], **extra: Any) -> dict[str, Any]:
    record = {"key": key, "kind": kind, "name": name, "status": status, "labels": labels}
    record.update(description_html=f"<h2>기본 내용</h2><p>{name} 설명</p>", link_to=None, redactions=[])
    if kind == "item":
        record["blocked_by"] = []
    record.update(extra)
    return record


def _comment(key: str, item: str, at: str, text: str) -> dict[str, Any]:
    return {"key": key, "kind": "comment", "item": item, "at": at, "body_html": f"<p>{text}</p>", "redactions": []}


def _full_manifest() -> dict[str, Any]:
    return {
        "version": 1,
        "slug": "demo",
        "source_dir": "~/src",
        "records": [
            {"key": _PROJECT, "kind": "project", "body_md": "# 목표\n", "redactions": []},
            {
                "key": _D1,
                "kind": "decision",
                "date": "2026-11-20",
                "status": "승인",
                "title": "결정",
                "detail": "근거",
                "redactions": [],
            },
            {
                "key": _F1,
                "kind": "fact",
                "date": "2026-11-20",
                "status": "Active",
                "text": "사실",
                "ref": "PR",
                "redactions": [],
            },
            # 파일 순서가 아니라 정해진 순서로 만드는지 보려고 섞어 둔다.
            _item(_B1, "backlog", "Active", "백로그 하나", ["backlog", "kind:req"]),
            _comment(_CB, _B1, "2026-11-20T08:00", "백로그 댓글"),
            _comment(_C10, _I1, "2026-11-23T12:00", "열째"),
            _comment(_C2, _I1, "2026-11-22 11:00", "둘째"),
            _comment(_C1, _I1, "2026-11-22T10:00", "첫째"),
            _item(_I5, "item", "Planned", "깊이 셋", ["kind:job"], parent=_I2),
            _item(_I2, "item", "Paused", "손자", ["kind:test"], parent=_I1, blocked_by=[_I3, _I4]),
            _item(_I1, "item", "In Progress", "자식", ["kind:job"], parent=_W1),
            _item(_I3, "item", "Failed", "실패한 일", ["kind:job"], parent=_W1),
            _item(_I4, "item", "Skipped", "건너뛴 일", [], parent=_W2),
            _item(_W1, "work", "Done", "일 하나", ["work"]),
            _item(_W2, "work", "Planned", "일 둘", ["work"]),
            _comment(_CW, _W1, "2026-11-24T09:00", "Work 댓글"),
            _item(_B2, "backlog", "Done", "백로그 둘", ["backlog"]),
            _item(_B3, "backlog", "폐기", "백로그 셋", ["backlog"]),
        ],
    }


def _record(data: dict[str, Any], key: str) -> dict[str, Any]:
    return next(r for r in data["records"] if r["key"] == key)


def _strip_marker_attribute(fragment: str) -> str:
    """저장하며 알 수 없는 속성을 지우는 서버를 흉내 낸다."""
    import re

    return re.sub(r' data-fl-key="[^"]*"', "", fragment)


class _PlaneCase(_ApplyCase):
    def setUp(self) -> None:
        super().setUp()
        self.data = _full_manifest()
        self.pid = next(p["id"] for p in self.plane.list_projects() if p["identifier"] == "SAMPM")
        self.preexisting = {str(i["id"]) for i in self.plane.list_items(self.pid)}
        self.plane.calls.clear()

    def manifest_file(self) -> Path:
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        return path

    def run_plane(self) -> dict[str, Outcome]:
        state = AppliedState.load(self.state_path)
        outcomes = apply_plane(load_manifest(self.manifest_file()), self.plane, self.pid, state)
        self.assertEqual(len(outcomes), len({o.key for o in outcomes}), outcomes)
        return {o.key: o for o in outcomes}

    def apply_actions(self) -> dict[str, str]:
        return {key: outcome.action for key, outcome in self.run_plane().items()}

    def assert_all_same(self, **changed: str) -> None:
        keys = [r["key"] for r in self.data["records"] if r["kind"] in ("work", "item", "backlog", "comment")]
        self.assertEqual(self.apply_actions(), {**dict.fromkeys(keys, "same"), **changed})

    def stored(self) -> list[dict[str, Any]]:
        """이전으로 생긴 항목(초기화 때 만든 것은 뺀다). 대역의 저장 객체 그대로다."""
        return [i for i in self.plane._project_by_id(self.pid).items if i["id"] not in self.preexisting]

    def by_key(self, key: str) -> dict[str, Any]:
        found = [i for i in self.plane._project_by_id(self.pid).items if i["external_id"] == key]
        self.assertEqual(len(found), 1, (key, found))
        return found[0]

    def comments(self, item_key: str) -> list[str]:
        stored = self.plane._project_by_id(self.pid).comments.get(self.by_key(item_key)["id"], [])
        return [str(c["comment_html"]) for c in stored]

    def label_names(self, item: dict[str, Any]) -> list[str]:
        names = {label["id"]: label["name"] for label in self.plane._project_by_id(self.pid).labels}
        return sorted(names[i] for i in item["labels"])

    def touch(self, item: dict[str, Any]) -> None:
        item["updated_at"] = "2026-11-30T00:00:00Z"

    def edit_in_plane(self, item: dict[str, Any]) -> None:
        """사람이 Plane에서 설명을 고친다. 내용과 수정 시각이 함께 바뀐다."""
        item["description_html"] = "<p>Plane에서 고침</p>"
        self.touch(item)

    def method_calls(self, name: str) -> list[tuple[Any, ...]]:
        return [c for c in self.plane.calls if c[0] == name]

    def snapshot_plane(self) -> str:
        project = self.plane._project_by_id(self.pid)
        return json.dumps([project.items, project.comments, project.labels], ensure_ascii=False, sort_keys=True)

    def assert_exact_result(self) -> None:
        """중단을 거쳤어도 항목·댓글·라벨이 하나씩이어야 한다."""
        self.assertEqual(sorted(str(i["external_id"]) for i in self.stored()), sorted(_ITEM_KEYS))
        counts = {key: len(self.comments(key)) for key in (_I1, _W1, _B1)}
        self.assertEqual(counts, {_I1: 3, _W1: 1, _B1: 1})
        text = "".join(self.comments(_I1))
        for word in ("첫째", "둘째", "열째"):
            self.assertEqual(text.count(word), 1)
        names = [label["name"] for label in self.plane._project_by_id(self.pid).labels]
        self.assertEqual(len(names), len(set(names)), names)


class FinalReviewIntegrationTest(_PlaneCase):
    def test_migrated_blocked_item_follows_prerequisite_state(self) -> None:
        from datetime import datetime, timezone
        from ledger.work import render_next, start, end

        now = datetime.now(timezone.utc)
        self.data["records"] = [
            _item(_W1, "work", "Planned", "선행", ["work"]),
            _item(_W2, "work", "Planned", "부모", ["work"]),
            _item(_I1, "item", "Planned", "후속", [], parent=_W2, blocked_by=[_W1]),
        ]
        self.run_plane()
        code = f"SAMPM-{self.by_key(_W1)['sequence_id']}"
        self.assertNotIn("후속", render_next(self.plane, self.pid, self.repo, now))
        self.plane.update_item(self.pid, self.by_key(_I1)["id"], {"labels": []})
        self.assertNotIn("후속", render_next(self.plane, self.pid, self.repo, now))
        start(self.plane, code, "alpha", now)
        self.assertNotIn("후속", render_next(self.plane, self.pid, self.repo, now))
        end(self.plane, code, "done", now, "a" * 40, result="완료", unverified="사유", host="alpha")
        self.assertIn("후속", render_next(self.plane, self.pid, self.repo, now))
        self.plane.update_item(self.pid, self.by_key(_W1)["id"], {"state": self.pid + "-state-cancelled"})
        self.assertIn("후속", render_next(self.plane, self.pid, self.repo, now))
        self.by_key(_W1)["external_id"] = "missing"
        self.assertNotIn("후속", render_next(self.plane, self.pid, self.repo, now))

    def test_marker_without_record_does_not_hide_changed_comment(self) -> None:
        self.run_plane()
        project = self.plane._project_by_id(self.pid)
        comment = project.comments[self.by_key(_W1)["id"]][0]
        comment["comment_html"] = comment["comment_html"].replace("Work 댓글", "고친 댓글")
        self.state_path.unlink()
        before = self.snapshot_plane()
        self.plane.calls.clear()
        self.assertEqual(self.apply_actions()[_CW], "conflict")
        self.assertEqual(self.snapshot_plane(), before)
        self.assertEqual(self.writes(), [])

    def test_applied_comments_conflict_on_remote_or_manifest_changes(self) -> None:
        for change in ("delete", "edit", "format", "manifest", "marker"):
            with self.subTest(change=change):
                self.setUp()
                self.run_plane()
                project = self.plane._project_by_id(self.pid)
                comments = project.comments[self.by_key(_W1)["id"]]
                if change == "delete":
                    comments.clear()
                elif change == "edit":
                    comments[0]["comment_html"] = comments[0]["comment_html"].replace("Work 댓글", "변경 댓글")
                elif change == "format":
                    comments[0]["comment_html"] = comments[0]["comment_html"].replace(
                        "<p>Work 댓글</p>", "<p><b>Work 댓글</b></p>"
                    )
                elif change == "marker":
                    comments[0]["comment_html"] = "<p>2026-11-24T09:00</p><p>Work 댓글</p>"
                else:
                    _record(self.data, _CW)["body_html"] = "<p>변경 댓글</p>"
                before = self.snapshot_plane()
                self.plane.calls.clear()
                self.assertEqual(self.apply_actions()[_CW], "conflict")
                self.assertEqual(self.snapshot_plane(), before)
                self.assertEqual(self.writes(), [])


class StatusMapTest(unittest.TestCase):
    def test_every_status_maps_to_the_agreed_group_and_label(self) -> None:
        self.assertEqual(
            STATUS_MAP,
            {
                "Planned": ("unstarted", None),
                "In Progress": ("started", None),
                "Done": ("completed", None),
                "Paused": ("unstarted", "paused"),
                "Failed": ("cancelled", "failed"),
                "Skipped": ("cancelled", "skipped"),
                "Active": ("backlog", None),
                "폐기": ("cancelled", None),
            },
        )

    def test_pick_state_uses_the_first_state_of_the_group(self) -> None:
        states = [
            {"id": "a", "group": "started"},
            {"id": "b", "group": "unstarted"},
            {"id": "c", "group": "unstarted"},
        ]
        self.assertEqual(pick_state(states, "unstarted"), "b")
        with self.assertRaisesRegex(ValueError, "completed"):
            pick_state(states, "completed")


class FullSampleTest(_PlaneCase):
    def test_everything_is_created_with_parents_states_labels_and_markers(self) -> None:
        outcomes = self.run_plane()
        self.assertEqual({k: o.action for k, o in outcomes.items()}, dict.fromkeys(_PLANE_KEYS, "created"))
        self.assert_exact_result()
        for key in _ITEM_KEYS:
            item = self.by_key(key)
            self.assertEqual((item["external_source"], item["id"]), (_SOURCE, outcomes[key].target))

        parents = {_I1: _W1, _I2: _I1, _I3: _W1, _I4: _W2, _I5: _I2}
        for key in _ITEM_KEYS:
            expected = self.by_key(parents[key])["id"] if key in parents else None
            self.assertEqual(self.by_key(key)["parent"], expected, key)

        groups = {s["id"]: s["group"] for s in self.plane.list_states(self.pid)}
        expected_states = {
            _W1: ("completed", ["work"]),
            _W2: ("unstarted", ["work"]),
            _I1: ("started", ["kind:job"]),
            _I2: ("unstarted", ["blocked", "kind:test", "paused"]),
            _I3: ("cancelled", ["failed", "kind:job"]),
            _I4: ("cancelled", ["skipped"]),
            _I5: ("unstarted", ["kind:job"]),
            _B1: ("backlog", ["backlog", "kind:req"]),
            _B2: ("completed", ["backlog"]),
            _B3: ("cancelled", ["backlog"]),
        }
        for key, (group, labels) in expected_states.items():
            item = self.by_key(key)
            self.assertEqual((groups[item["state"]], self.label_names(item)), (group, labels), key)

        blocked = f"<p>차단: {_I3}, {_I4}</p><h2>기본 내용</h2><p>손자 설명</p>"
        self.assertEqual(self.by_key(_I2)["description_html"], blocked)
        self.assertEqual(self.by_key(_I1)["description_html"], "<h2>기본 내용</h2><p>자식 설명</p>")

    def test_order_is_work_then_parents_first_then_comments_by_time_then_backlog(self) -> None:
        self.run_plane()
        made = [c[2] for c in self.method_calls("create_item")]
        self.assertEqual(made[:2], ["일 하나", "일 둘"])
        self.assertEqual(made[2:7], ["자식", "실패한 일", "건너뛴 일", "손자", "깊이 셋"])
        self.assertEqual(made[7:], ["백로그 하나", "백로그 둘", "백로그 셋"])
        first_backlog = next(n for n, c in enumerate(self.plane.calls) if c[0] == "create_item" and c[2] == made[7])
        before = [c for c in self.plane.calls[:first_backlog] if c[0] == "create_comment"]
        self.assertEqual(len(before), 4)  # Work·Item 댓글은 백로그보다 먼저, 백로그 댓글은 그 뒤

        self.assertEqual(
            self.comments(_I1),
            [
                f'<p>2026-11-22T10:00</p><p>첫째</p><p data-fl-key="{_C1}"></p>',
                f'<p>2026-11-22 11:00</p><p>둘째</p><p data-fl-key="{_C2}"></p>',
                f'<p>2026-11-23T12:00</p><p>열째</p><p data-fl-key="{_C10}"></p>',
            ],
        )
        self.assertEqual(self.method_calls("update_comment"), [])

    def test_lists_are_read_once_per_run(self) -> None:
        for _ in range(2):
            self.plane.calls.clear()
            self.run_plane()
            for name in ("list_items", "list_states", "list_labels"):
                self.assertEqual(len(self.method_calls(name)), 1, name)
            self.assertEqual(self.method_calls("find_item_by_external"), [])

    def test_second_run_creates_nothing_and_only_reads_the_lists(self) -> None:
        self.run_plane()
        before = self.snapshot_plane()
        self.plane.calls.clear()
        with patch.object(AppliedState, "save") as save:
            self.assert_all_same()
        save.assert_not_called()  # 달라진 것이 없으면 적용 기록도 다시 쓰지 않는다.
        self.assertEqual(self.snapshot_plane(), before)
        self.assertEqual(
            sorted(c[0] for c in self.plane.calls), ["list_comments"] * 3 + ["list_items", "list_labels", "list_states"]
        )

    def test_recorded_time_is_read_after_the_last_comment(self) -> None:
        self.run_plane()
        state = AppliedState.load(self.state_path)
        for key in _ITEM_KEYS:
            entry = state.get(key)
            assert entry is not None
            self.assertEqual(entry.remote_updated_at, self.by_key(key)["updated_at"], key)
            self.assertTrue(entry.applied_at and entry.digest)

    def test_existing_labels_are_reused(self) -> None:
        made = self.plane.create_label(self.pid, "work", "#000000")
        self.plane.calls.clear()
        self.run_plane()
        self.assertNotIn("work", [c[2] for c in self.method_calls("create_label")])
        self.assertIn(made["id"], self.by_key(_W1)["labels"])
        self.assert_exact_result()

    def test_exposed_text_is_refused_before_any_write(self) -> None:
        _record(self.data, _CB)["body_html"] = "<p>/home/" + "alice/run.log</p>"
        with self.assertRaisesRegex(FlowError, "매니페스트 검사 실패: 1건") as ctx:
            self.run_plane()
        self.assertNotIn("alice", str(ctx.exception))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.stored(), [])

    def test_missing_state_group_stops_before_any_write(self) -> None:
        states = [s for s in self.plane.list_states(self.pid) if s["group"] != "cancelled"]
        with patch.object(self.plane, "list_states", return_value=states):
            with self.assertRaisesRegex(ValueError, "cancelled"):
                self.run_plane()
        self.assertEqual(self.writes(), [])


class ResumeTest(_PlaneCase):
    def test_5xx_on_the_fifth_item_then_rerun_makes_only_the_rest(self) -> None:
        real = self.plane.create_item
        for after_create in (False, True):  # 서버가 만들기 전의 5xx와 만든 뒤의 5xx
            with self.subTest(server_created=after_create):
                self.setUp()
                real = self.plane.create_item
                calls: list[int] = []

                def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    calls.append(1)
                    if len(calls) == 5:
                        if after_create:
                            real(*args, **kwargs)
                        raise PlaneUnavailable("Plane 요청 실패: HTTP 503")
                    return real(*args, **kwargs)

                with patch.object(self.plane, "create_item", failing):
                    with self.assertRaises(PlaneUnavailable):
                        self.run_plane()
                done = 5 if after_create else 4
                self.assertEqual(len(self.stored()), done)
                saved = AppliedState.load(self.state_path)
                self.assertEqual(sum(saved.get(key) is not None for key in _ITEM_KEYS), 4)

                actions = self.apply_actions()
                self.assertEqual(sum(actions[k] == "created" for k in _ITEM_KEYS), len(_ITEM_KEYS) - done)
                self.assertNotIn("conflict", actions.values())
                self.assert_exact_result()
                self.assert_all_same()

    def sweep(self, *, strip: bool, lose_state: bool) -> None:
        """모든 Plane 호출 지점에서 한 번씩(보내기 전·처리한 뒤) 실패시키고 다시 실행한다."""
        methods = (*_WRITE_METHODS, "get_item")
        total = 0
        for method in methods:
            for after in (False, True):
                nth = 0
                while True:
                    nth += 1
                    self.setUp()
                    if strip:
                        self.install_stripping_server()
                    if not self.fail_nth(method, nth, after):
                        break  # 그 호출은 이 횟수만큼 일어나지 않는다
                    total += 1
                    with self.subTest(method=method, nth=nth, after=after):
                        if lose_state:
                            self.state_path.unlink(missing_ok=True)
                        actions = self.apply_actions()
                        self.assertNotIn("conflict", actions.values(), actions)
                        self.assert_exact_result()
                        self.assert_settled(strip)
        self.assertGreater(total, 60)

    def assert_settled(self, strip: bool) -> None:
        """다음 실행은 아무것도 쓰지 않는다. 표식 없는 댓글은 속성을 지우는 서버에서만 남을 수 있다."""
        before = self.snapshot_plane()
        actions = self.apply_actions()
        unmarked = {key for key, action in actions.items() if action == "same-unmarked"}
        self.assertEqual({a for k, a in actions.items() if k not in unmarked}, {"same"}, actions)
        self.assertLessEqual(unmarked, set(_COMMENT_KEYS) if strip else set(), actions)
        self.assertEqual(self.snapshot_plane(), before)

    def fail_nth(self, method: str, nth: int, after: bool) -> bool:
        """`method`의 nth번째 호출을 실패시키며 한 번 실행한다. 그 호출이 있었으면 True.

        `save`는 적용 기록 저장이고, 나머지는 Plane 호출이다.
        """
        owner: Any = AppliedState if method == "save" else self.plane
        real = getattr(owner, method)
        calls: list[int] = []

        def failing(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == nth:
                if after:
                    real(*args, **kwargs)
                raise PlaneUnavailable("Plane 요청 실패: HTTP 503")
            return real(*args, **kwargs)

        with patch.object(owner, method, failing):
            try:
                self.run_plane()
            except PlaneUnavailable:
                return True
        return False

    def test_any_single_failed_record_save_converges(self) -> None:
        for strip in (False, True):
            nth = 0
            while True:
                nth += 1
                self.setUp()
                if strip:
                    self.install_stripping_server()
                if not self.fail_nth("save", nth, False):
                    break
                with self.subTest(strip=strip, nth=nth):
                    actions = self.apply_actions()
                    self.assertNotIn("conflict", actions.values(), actions)
                    self.assert_exact_result()
                    self.assert_settled(strip)
            self.assertGreater(nth, 30)

    def test_any_single_failure_while_updating_and_linking_converges(self) -> None:
        total = 0
        for method in (*_WRITE_METHODS, "get_item", "save"):
            for after in (False, True):
                nth = 0
                while True:
                    nth += 1
                    self.setUp()
                    # 첫 적용 뒤: 항목 둘의 내용과 상태를 바꾸고, 새 댓글과 기존 항목 연결을 더한다.
                    self.run_plane()
                    existing = self.plane.create_item(self.pid, "원래 있던 일", "<p>원래</p>", [])
                    self.preexisting.add(existing["id"])
                    link = f"{_P}/_default/Work9"
                    extra = f"{_I3}#c1"
                    self.data["records"] += [
                        _item(link, "work", "Planned", "연결할 일", ["work"], link_to=existing["id"]),
                        _comment(extra, _I3, "2026-11-25T10:00", "새 댓글"),
                    ]
                    _record(self.data, _I1).update(description_html="<p>새 설명</p>", labels=["kind:new"])
                    _record(self.data, _I3)["status"] = "Done"
                    if not self.fail_nth(method, nth, after):
                        break
                    total += 1
                    with self.subTest(method=method, nth=nth, after=after):
                        actions = self.apply_actions()
                        self.assertNotIn("conflict", actions.values(), actions)
                        self.assertEqual(self.by_key(_I1)["description_html"], "<p>새 설명</p>")
                        self.assertEqual(self.by_key(_I3)["state"], f"{self.pid}-state-completed")
                        self.assertEqual(self.by_key(link)["id"], existing["id"])
                        self.assertEqual(len(self.comments(_I3)), 1)
                        self.assertEqual(len(self.stored()), len(_ITEM_KEYS))
                        self.assertEqual(set(self.apply_actions().values()), {"same"})
        self.assertGreater(total, 20)

    def install_stripping_server(self) -> None:
        for name in ("create_comment", "update_comment"):
            real = getattr(self.plane, name)

            def stripping(*args: Any, _real: Callable[..., Any] = real) -> Any:
                return _real(*args[:-1], _strip_marker_attribute(args[-1]))

            patcher = patch.object(self.plane, name, stripping)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_any_single_failure_converges(self) -> None:
        self.sweep(strip=False, lose_state=False)

    def test_any_single_failure_converges_when_the_server_drops_the_attribute(self) -> None:
        self.sweep(strip=True, lose_state=False)

    def test_any_single_failure_converges_even_if_the_record_is_also_lost(self) -> None:
        for strip in (False, True):
            with self.subTest(strip=strip):
                self.sweep(strip=strip, lose_state=True)

    def test_lost_state_adopts_everything(self) -> None:
        self.run_plane()
        before = self.snapshot_plane()
        self.state_path.unlink()
        self.plane.calls.clear()
        self.assert_all_same()
        self.assertEqual((self.snapshot_plane(), self.writes()), (before, []))
        self.plane.calls.clear()
        self.assert_all_same()
        self.assertEqual(len(self.method_calls("list_comments")), 3)  # 기록 복구 뒤에도 원격 댓글을 확인한다

    def interrupt_first_comment(self) -> None:
        """첫 댓글(I1의 것)에서 멈춘다. I1의 기록은 '쓰는 도중'으로 남는다."""
        with patch.object(self.plane, "create_comment", side_effect=PlaneUnavailable("503")):
            with self.assertRaises(PlaneUnavailable):
                self.run_plane()
        entry = AppliedState.load(self.state_path).get(_I1)
        assert entry is not None
        self.assertEqual(entry.remote_updated_at, "")

    def test_edit_after_an_interrupted_comment_is_still_a_conflict(self) -> None:
        for changed_manifest in (False, True):
            with self.subTest(changed_manifest=changed_manifest):
                self.setUp()
                self.interrupt_first_comment()
                edited = self.by_key(_I1)
                edited["description_html"] = "<p>그 사이에 Plane에서 고침</p>"
                self.touch(edited)
                if changed_manifest:
                    _record(self.data, _I1)["description_html"] = "<p>매니페스트도 바뀜</p>"
                for _ in range(2):
                    self.assertEqual(self.apply_actions()[_I1], "conflict")
                    self.assertEqual(self.by_key(_I1)["description_html"], "<p>그 사이에 Plane에서 고침</p>")

    def test_manifest_change_after_an_interrupted_comment_updates_the_untouched_item(self) -> None:
        self.interrupt_first_comment()
        _record(self.data, _I1)["description_html"] = "<p>새 설명</p>"
        self.assertEqual(self.apply_actions()[_I1], "updated")
        self.assertEqual(self.by_key(_I1)["description_html"], "<p>새 설명</p>")
        self.assert_all_same()

    def test_edited_half_written_comment_is_a_conflict_not_a_duplicate(self) -> None:
        self.install_stripping_server()
        with patch.object(self.plane, "update_comment", side_effect=PlaneUnavailable("503")):
            with self.assertRaises(PlaneUnavailable):
                self.run_plane()
        stored = self.plane._project_by_id(self.pid).comments[self.by_key(_I1)["id"]]
        stored[0]["comment_html"] = "<p>표식을 달기 전에 Plane에서 고침</p>"
        self.assertEqual(self.apply_actions()[_C1], "conflict")
        self.assertEqual(self.comments(_I1)[0], "<p>표식을 달기 전에 Plane에서 고침</p>")
        self.assertEqual(sum("첫째" in c for c in self.comments(_I1)), 0)


class ConflictTest(_PlaneCase):
    def setUp(self) -> None:
        super().setUp()
        self.run_plane()
        self.plane.calls.clear()

    def assert_conflict_stays(self, key: str) -> None:
        before = self.snapshot_plane()
        for _ in range(2):
            self.plane.calls.clear()
            self.assert_all_same(**{key: "conflict"})
            self.assertEqual((self.snapshot_plane(), self.writes()), (before, []))

    def test_different_content_with_the_same_updated_at_is_updated(self) -> None:
        # 수정 시각이 같다고 내용까지 같은 것은 아니다. 관리 필드를 먼저 비교한다.
        self.by_key(_I1)["description_html"] = "<p>시각은 그대로인데 달라진 설명</p>"
        manifest = load_manifest(self.manifest_file())
        planned = plan_apply(manifest, self.repo, self.plane, self.pid, AppliedState.load(self.state_path))
        self.assertIn(_I1, planned.update)
        self.plane.calls.clear()
        self.assert_all_same(**{_I1: "updated"})
        self.assertEqual(self.by_key(_I1)["description_html"], "<h2>기본 내용</h2><p>자식 설명</p>")
        self.assertEqual(len(self.method_calls("update_item")), 1)
        self.assert_all_same()

    def test_later_updated_at_with_changed_content_is_a_conflict(self) -> None:
        self.edit_in_plane(self.by_key(_I1))
        self.assert_conflict_stays(_I1)

    def test_only_updated_at_changed_is_same_and_the_new_time_is_recorded(self) -> None:
        self.touch(self.by_key(_I1))
        before = self.snapshot_plane()
        self.assert_all_same()
        self.assertEqual((self.snapshot_plane(), self.writes()), (before, []))
        entry = AppliedState.load(self.state_path).get(_I1)
        assert entry is not None
        self.assertEqual(entry.remote_updated_at, self.by_key(_I1)["updated_at"])
        # 시각을 새로 기록했으므로, 그 뒤에 바뀐 매니페스트는 손대지 않은 항목에 대한 갱신이다.
        _record(self.data, _I1)["description_html"] = "<p>새 설명</p>"
        self.assert_all_same(**{_I1: "updated"})

    def test_content_saved_differently_by_the_server_is_same_when_only_the_time_changed(self) -> None:
        self.setUp_with_rewriting_server()
        self.touch(self.by_key(_I1))
        self.plane.calls.clear()
        self.assert_all_same()
        self.assertEqual(self.writes(), [])

    def setUp_with_rewriting_server(self) -> None:
        """저장하면서 설명의 글을 바꾸는 서버로 처음부터 다시 적용한다."""
        _PlaneCase.setUp(self)
        real = self.plane.create_item

        def rewriting(project_id: str, name: str, description: str, labels: list[str], **kw: Any) -> dict[str, Any]:
            return real(project_id, name, description.replace("설명", "정리된 설명"), labels, **kw)

        with patch.object(self.plane, "create_item", rewriting):
            self.run_plane()

    def test_item_edited_in_plane_is_not_overwritten_even_if_the_manifest_changed(self) -> None:
        for field, value in (
            ("name", "Plane에서 바꾼 이름"),
            ("description_html", "<p>Plane에서 고침</p>"),
            ("state", "other-state"),
            ("labels", []),
        ):
            with self.subTest(field=field):
                self.setUp()
                item = self.by_key(_I1)
                item[field] = value
                self.touch(item)
                _record(self.data, _I1)["description_html"] = "<p>매니페스트도 바뀜</p>"
                self.assert_conflict_stays(_I1)

    def test_manifest_change_updates_an_untouched_item_once(self) -> None:
        _record(self.data, _I1).update(description_html="<p>새 설명</p>", status="Done", labels=["kind:fix"])
        self.assert_all_same(**{_I1: "updated"})
        item = self.by_key(_I1)
        self.assertEqual(item["description_html"], "<p>새 설명</p>")
        self.assertEqual(item["state"], f"{self.pid}-state-completed")
        self.assertEqual(self.label_names(item), ["kind:fix"])
        self.assertEqual(item["parent"], self.by_key(_W1)["id"])
        self.assertEqual(len(self.method_calls("update_item")), 1)
        self.assert_all_same()

    def test_renamed_record_is_a_conflict_because_names_cannot_be_updated(self) -> None:
        _record(self.data, _I1).update(name="새 이름", description_html="<p>새 설명</p>")
        self.assert_conflict_stays(_I1)

    def test_edit_made_while_the_run_is_in_progress_is_not_overwritten(self) -> None:
        _record(self.data, _I1)["description_html"] = "<p>새 설명</p>"
        real = self.plane.list_items

        def list_then_edit(project_id: str) -> list[dict[str, Any]]:
            listed = real(project_id)
            self.by_key(_I1)["description_html"] = "<p>목록을 읽은 뒤 고침</p>"
            self.touch(self.by_key(_I1))
            return listed

        with patch.object(self.plane, "list_items", list_then_edit):
            self.assertEqual(self.apply_actions()[_I1], "conflict")
        self.assertEqual(self.by_key(_I1)["description_html"], "<p>목록을 읽은 뒤 고침</p>")

    def test_edit_made_while_comments_are_written_is_reported_and_stays_a_conflict(self) -> None:
        extra = f"{_I1}#c11"
        self.data["records"].append(_comment(extra, _I1, "2026-11-25T10:00", "나중 댓글"))
        real = self.plane.create_comment

        def edit_then_comment(*args: Any) -> dict[str, Any]:
            self.by_key(_I1)["description_html"] = "<p>댓글을 쓰는 사이에 고침</p>"
            return real(*args)

        with patch.object(self.plane, "create_comment", edit_then_comment):
            self.assert_all_same(**{_I1: "conflict", extra: "created"})
        # 매니페스트를 바꾸지 않은 재실행도 계속 충돌이다. 그 뒤에 바꿔도 덮어쓰지 않는다.
        self.assert_conflict_stays(_I1)
        _record(self.data, _I1)["description_html"] = "<p>매니페스트도 바뀜</p>"
        self.assert_conflict_stays(_I1)
        self.assertEqual(self.by_key(_I1)["description_html"], "<p>댓글을 쓰는 사이에 고침</p>")

    def test_edit_made_between_an_item_write_and_the_reread_is_a_conflict(self) -> None:
        _record(self.data, _I1)["description_html"] = "<p>새 설명</p>"
        real = self.plane.update_item

        def update_then_edit(*args: Any) -> dict[str, Any]:
            response = real(*args)
            self.edit_in_plane(self.by_key(_I1))
            return response

        with patch.object(self.plane, "update_item", update_then_edit):
            self.assertEqual(self.apply_actions()[_I1], "conflict")
        self.assert_conflict_stays(_I1)
        _record(self.data, _I1)["description_html"] = "<p>또 바뀜</p>"
        self.assert_conflict_stays(_I1)

    # --- 지적 2 (Ruling 14 앞부분) ---

    def test_conflicted_item_gets_no_new_comment(self) -> None:
        self.edit_in_plane(self.by_key(_I1))
        extra = f"{_I1}#c11"
        self.data["records"].append(_comment(extra, _I1, "2026-11-25T10:00", "나중 댓글"))
        before = self.snapshot_plane()
        for _ in range(2):
            self.plane.calls.clear()
            self.assert_all_same(**{_I1: "conflict", extra: "conflict"})
            self.assertEqual((self.snapshot_plane(), self.writes()), (before, []))

    def test_deleted_item_is_not_recreated_and_cannot_take_new_records(self) -> None:
        project = self.plane._project_by_id(self.pid)
        project.items.remove(self.by_key(_I2))
        child, extra = f"{_P}/_default/Work1-9", f"{_I2}#c1"
        self.data["records"].append(_item(child, "item", "Planned", "새 자식", [], parent=_I2))
        self.data["records"].append(_comment(extra, _I2, "2026-11-25T10:00", "새 댓글"))
        self.plane.calls.clear()
        self.assert_all_same(**{_I2: "conflict", child: "conflict", extra: "conflict"})
        self.assertEqual(self.writes(), [])

    def test_removed_source_marker_is_a_conflict(self) -> None:
        self.by_key(_I3)["external_id"] = None
        self.plane.calls.clear()
        self.assert_all_same(**{_I3: "conflict"})
        self.assertEqual(self.writes(), [])

    def test_edited_or_deleted_comment_is_left_alone(self) -> None:
        project = self.plane._project_by_id(self.pid)
        comments = project.comments[self.by_key(_I1)["id"]]
        comments[0]["comment_html"] = "<p>고친 댓글</p>"
        del comments[1]
        self.plane.calls.clear()
        self.assert_all_same(**{_C1: "conflict", _C2: "conflict"})
        self.assertEqual(self.writes(), [])
        self.assertEqual(len(self.comments(_I1)), 2)


class LinkToTest(_PlaneCase):
    def setUp(self) -> None:
        super().setUp()
        self.existing = self.plane.create_item(self.pid, "원래 있던 일", "<p>원래 설명</p>", [])
        self.preexisting.add(self.existing["id"])
        self.plane.create_comment(self.pid, self.existing["id"], "<p>원래 댓글</p>")
        _record(self.data, _W1)["link_to"] = self.existing["id"]
        self.plane.calls.clear()

    def linked(self) -> dict[str, Any]:
        return next(i for i in self.plane._project_by_id(self.pid).items if i["id"] == self.existing["id"])

    def test_linked_record_only_gets_the_source_marker(self) -> None:
        outcomes = self.run_plane()
        self.assertEqual((outcomes[_W1].action, outcomes[_W1].target), ("updated", self.existing["id"]))
        self.assertNotIn("일 하나", [c[2] for c in self.method_calls("create_item")])
        updates = [c[3] for c in self.method_calls("update_item")]
        self.assertEqual(updates, [{"external_source": _SOURCE, "external_id": _W1}])
        item = self.linked()
        for field in ("name", "description_html", "state", "labels", "parent"):
            self.assertEqual(item[field], self.existing[field], field)
        self.assertEqual(self.by_key(_I1)["parent"], self.existing["id"])
        self.assertEqual(self.comments(_W1)[0], "<p>원래 댓글</p>")
        self.assertIn("Work 댓글", self.comments(_W1)[1])
        self.assert_all_same()

    def test_linked_item_edited_in_plane_stays_same_and_takes_new_comments(self) -> None:
        self.run_plane()
        self.edit_in_plane(self.linked())
        extra = f"{_W1}#c2"
        self.data["records"].append(_comment(extra, _W1, "2026-11-25T10:00", "나중 댓글"))
        self.assert_all_same(**{extra: "created"})
        self.assertEqual(self.linked()["description_html"], "<p>Plane에서 고침</p>")
        self.assert_all_same()

    # --- 지적 1 (Ruling 13) ---

    def test_unmarked_existing_comment_with_the_same_text_is_never_modified(self) -> None:
        text = "<p>2026-11-24T09:00</p><p>Work 댓글</p>"  # 매니페스트 댓글이 쓰려는 글과 같다
        self.plane.create_comment(self.pid, self.existing["id"], text)
        self.plane.calls.clear()
        planned = self.plan()
        self.assertIn(_CW, planned.skip)
        for _ in range(2):
            self.assertEqual(self.apply_actions()[_CW], "same-unmarked")
        self.assertEqual(self.comments(_W1), ["<p>원래 댓글</p>", text])
        self.assertEqual(self.method_calls("update_comment"), [])
        self.assertIsNone(AppliedState.load(self.state_path).get(_CW))

    def test_one_unmarked_comment_stands_in_for_only_one_record(self) -> None:
        text = "<p>2026-11-24T09:00</p><p>Work 댓글</p>"
        self.plane.create_comment(self.pid, self.existing["id"], text)
        twin = f"{_W1}#c2"
        self.data["records"].append(_comment(twin, _W1, "2026-11-24T09:00", "Work 댓글"))
        actions = self.apply_actions()
        self.assertEqual((actions[_CW], actions[twin]), ("same-unmarked", "created"))
        self.assertEqual(self.comments(_W1)[1], text)
        self.assertEqual(len(self.comments(_W1)), 3)
        actions = self.apply_actions()
        self.assertEqual((actions[_CW], actions[twin]), ("same-unmarked", "same"))

    def test_changing_or_removing_the_link_after_apply_is_a_conflict(self) -> None:
        self.run_plane()
        before = self.snapshot_plane()
        for link in (None, "other-item"):
            with self.subTest(link=link):
                _record(self.data, _W1)["link_to"] = link
                self.plane.calls.clear()
                self.assert_all_same(**{_W1: "conflict"})
                self.assertEqual((self.snapshot_plane(), self.writes()), (before, []))

    def test_missing_or_already_marked_target_is_a_conflict(self) -> None:
        for label, change in (
            ("없는 항목", lambda: _record(self.data, _W1).update(link_to="no-such-item")),
            ("다른 도구의 표식", lambda: self.linked().update(external_source="jira", external_id="X-1")),
            ("다른 원천 키", lambda: self.linked().update(external_source=_SOURCE, external_id=f"{_P}/other")),
        ):
            with self.subTest(case=label):
                self.setUp()
                change()
                marker = (self.linked()["external_source"], self.linked()["external_id"])
                actions = self.apply_actions()
                self.assertEqual(actions[_W1], "conflict")
                self.assertEqual(actions[_I1], "conflict")  # 부모를 정할 수 없다
                self.assertEqual(actions[_CW], "conflict")
                self.assertEqual(actions[_W2], "created")
                self.assertEqual((self.linked()["external_source"], self.linked()["external_id"]), marker)
                self.assertNotIn("일 하나", [str(i["name"]) for i in self.stored()])

    def test_two_records_cannot_take_the_same_target(self) -> None:
        _record(self.data, _W2)["link_to"] = self.existing["id"]
        planned = self.plan()
        self.assertIn(_W2, planned.conflict)
        actions = self.apply_actions()
        self.assertEqual((actions[_W1], actions[_W2]), ("updated", "conflict"))
        self.assertEqual(self.linked()["external_id"], _W1)

    def plan(self) -> ApplyPlan:
        manifest = load_manifest(self.manifest_file())
        return plan_apply(manifest, self.repo, self.plane, self.pid, AppliedState.load(self.state_path))


class CommentMarkerTest(_PlaneCase):
    def strip(self, name: str) -> None:
        real = getattr(self.plane, name)

        def stripping(*args: Any) -> Any:
            return real(*args[:-1], _strip_marker_attribute(args[-1]))

        patcher = patch.object(self.plane, name, stripping)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dropped_attribute_is_replaced_with_a_visible_line(self) -> None:
        self.strip("create_comment")
        self.strip("update_comment")
        self.run_plane()
        self.assertEqual(self.comments(_I1)[0], f"<p>2026-11-22T10:00</p><p>첫째</p><p>fl:{_C1}</p>")
        for html_text in (*self.comments(_I1), *self.comments(_W1), *self.comments(_B1)):
            self.assertRegex(html_text, r"<p>fl:pmt:demo/[^<]+</p>$")
            self.assertNotIn("<p></p>", html_text)
        # 한 번 지워진 것을 본 뒤로는 처음부터 보이는 표식을 쓴다.
        self.assertEqual(len(self.method_calls("update_comment")), 1)
        self.state_path.unlink()
        self.plane.calls.clear()
        self.assert_all_same()
        self.assertEqual(self.writes(), [])

    def test_marker_that_cannot_be_saved_stops_the_run(self) -> None:
        real_create, real_update = self.plane.create_comment, self.plane.update_comment

        def no_marker(fragment: str) -> str:
            return _strip_marker_attribute(fragment).replace("<p>fl:", "<p>")

        with patch.object(self.plane, "create_comment", lambda p, i, h: real_create(p, i, no_marker(h))):
            with patch.object(self.plane, "update_comment", lambda p, i, c, h: real_update(p, i, c, no_marker(h))):
                with self.assertRaisesRegex(FlowError, "댓글 표식이 저장되지 않음"):
                    self.run_plane()

    def test_key_prefix_of_another_comment_is_not_the_same_marker(self) -> None:
        # #c10의 표식이 있어도 #c1은 아직 없는 댓글이다.
        self.data["records"] = [r for r in self.data["records"] if r["key"] != _C1]
        self.run_plane()
        self.data["records"].append(_comment(_C1, _I1, "2026-11-22T10:00", "첫째"))
        self.assert_all_same(**{_C1: "created"})
        self.assert_exact_result()

    def test_marker_text_quoted_in_the_middle_of_a_body_is_not_a_marker(self) -> None:
        self.data["records"] = [r for r in self.data["records"] if r["key"] not in (_C2, _C10)]
        _record(self.data, _C1)["body_html"] = f"<p>fl:{_C2}</p><p>첫째</p>"
        self.run_plane()
        self.data["records"].append(_comment(_C2, _I1, "2026-11-22T11:00", "둘째"))
        self.assert_all_same(**{_C2: "created"})

    def test_identical_comments_are_both_created(self) -> None:
        _record(self.data, _C2).update(at="2026-11-22T10:00", body_html="<p>첫째</p>")
        self.run_plane()
        self.assertEqual(sum("첫째" in c for c in self.comments(_I1)), 2)


class CommentTimeOrderTest(_PlaneCase):
    def use_timezone(self, name: str) -> None:
        self.addCleanup(time.tzset)  # 환경을 되돌린 뒤에 부르도록 먼저 등록한다
        patcher = patch.dict(os.environ, {"TZ": name})
        patcher.start()
        self.addCleanup(patcher.stop)
        time.tzset()

    def test_comments_are_ordered_by_instant_and_naive_times_are_local(self) -> None:
        self.use_timezone("KST-9")  # POSIX 표기로 UTC+9
        _record(self.data, _C1).update(at="2026-11-22T10:00+09:00", body_html="<p>첫째</p>")  # 01:00Z
        _record(self.data, _C2).update(at="2026-11-22 10:30", body_html="<p>둘째</p>")  # 현지 10:30 = 01:30Z
        _record(self.data, _C10).update(at="2026-11-22T02:00:00Z", body_html="<p>열째</p>")
        self.run_plane()
        words = [next(w for w in ("첫째", "둘째", "열째") if w in text) for text in self.comments(_I1)]
        self.assertEqual(words, ["첫째", "둘째", "열째"])
        self.assertIn("<p>2026-11-22 10:30</p>", self.comments(_I1)[1])  # 첫 줄은 원래 표기 그대로다

    def test_unreadable_time_fails_the_check_before_any_write(self) -> None:
        for value in ("2026-11-22T10:00+99:00", "2026-11-22T10:00:99"):
            with self.subTest(value=value):
                _record(self.data, _C1)["at"] = value
                with self.assertRaisesRegex(FlowError, "매니페스트 검사 실패: 1건"):
                    self.run_plane()
                self.assertEqual(self.writes(), [])


class PlanApplyTest(_PlaneCase):
    def plan(self) -> ApplyPlan:
        manifest = load_manifest(self.manifest_file())
        return plan_apply(manifest, self.repo, self.plane, self.pid, AppliedState.load(self.state_path))

    def everything(self) -> tuple[str, dict[str, bytes], bytes]:
        state = self.state_path.read_bytes() if self.state_path.exists() else b""
        return self.snapshot_plane(), self.snapshot(), state

    def apply_all(self) -> list[Outcome]:
        manifest = load_manifest(self.manifest_file())
        state = AppliedState.load(self.state_path)
        return apply_git(manifest, self.repo, self.plane, state) + apply_plane(manifest, self.plane, self.pid, state)

    def test_fresh_plan_lists_every_record_as_create_and_writes_nothing(self) -> None:
        self.git("checkout", "-q", "-B", "elsewhere")  # 미리 보기는 어느 브랜치에서도 된다
        before = self.everything()
        plan = self.plan()
        self.assertEqual(sorted(plan.create), sorted([*_GIT_KEYS, *_PLANE_KEYS]))
        self.assertEqual((plan.update, plan.skip, plan.conflict), ([], [], []))
        self.assertEqual((self.everything(), self.writes()), (before, []))
        self.assertFalse(self.state_path.exists())

    def test_plan_matches_what_apply_then_does(self) -> None:
        self.apply_all()
        self.assertEqual(sorted(self.plan().skip), sorted([*_GIT_KEYS, *_PLANE_KEYS]))
        extra = f"{_I1}#c11"
        self.data["records"].append(_comment(extra, _I1, "2026-11-25T10:00", "나중 댓글"))
        _record(self.data, _I3)["description_html"] = "<p>새 설명</p>"
        _record(self.data, _F1)["text"] = "고친 사실"
        _record(self.data, _PROJECT)["body_md"] = "# 새 목표\n"
        self.edit_in_plane(self.by_key(_I4))
        before = self.everything()
        self.plane.calls.clear()
        plan = self.plan()
        self.assertEqual((self.everything(), self.writes()), (before, []))

        outcomes = self.apply_all()
        actual = {
            action: sorted(o.key for o in outcomes if o.action == action)
            for action in ("created", "updated", "same", "conflict")
        }
        self.assertEqual(
            actual,
            {
                "created": sorted(plan.create),
                "updated": sorted(plan.update),
                "same": sorted(plan.skip),
                "conflict": sorted(plan.conflict),
            },
        )
        self.assertEqual((plan.create, plan.conflict), ([extra], [_I4]))
        self.assertEqual(sorted(plan.update), sorted([_I3, _F1, _PROJECT]))

    def test_plan_does_not_repair_the_applied_record(self) -> None:
        self.apply_all()
        self.state_path.unlink()
        self.assertEqual(sorted(self.plan().skip), sorted([*_GIT_KEYS, *_PLANE_KEYS]))
        self.assertFalse(self.state_path.exists())


class CliTest(_PlaneCase):
    def setUp(self) -> None:
        super().setUp()
        self.env = {
            "HOME": str(self.tmp),
            "XDG_STATE_HOME": str(self.tmp / "xdg"),
            "FORGE_LEDGER_STATE_DIR": str(self.tmp / "limits"),
            "FORGE_LEDGER_PLANE_URL": "http://127.0.0.1:9",
            "FORGE_LEDGER_PLANE_WORKSPACE": "forge",
            "FORGE_LEDGER_PLANE_TOKEN": "t",
        }
        self.applied = self.tmp / "xdg" / "forge-ledger" / "migrations" / "demo.applied.json"
        patcher = patch("ledger.cli.PlaneClient", return_value=self.plane)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, command: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["--repo", str(self.repo.root), "migrate", command, str(self.manifest_file())], env=self.env)
        return rc, out.getvalue(), err.getvalue()

    def row(self, out: str, kind: str) -> list[int]:
        line = next(line for line in out.splitlines() if line.startswith(f"{kind} | "))
        return [int(cell) for cell in line.split(" | ")[1:]]

    def test_preview_prints_the_plan_and_writes_nothing(self) -> None:
        before = (self.snapshot_plane(), self.snapshot())
        rc, out, err = self.cli("preview")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn(f"- 새로 만들 것: {len(_GIT_KEYS) + len(_PLANE_KEYS)}", out.splitlines())
        self.assertEqual(((self.snapshot_plane(), self.snapshot()), self.writes()), (before, []))
        self.assertFalse(self.applied.parent.exists())

    def test_apply_prints_a_table_per_kind_and_rerun_creates_nothing(self) -> None:
        rc, out, err = self.cli("apply")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("종류 | 새로 만듦 | 바꿈 | 그대로 | 충돌", out.splitlines())
        for kind, count in (("project", 1), ("decision", 1), ("fact", 1), ("work", 2), ("item", 5), ("comment", 5)):
            self.assertEqual(self.row(out, kind), [count, 0, 0, 0], kind)
        self.assertEqual(self.row(out, "backlog"), [3, 0, 0, 0])
        self.assertTrue(self.applied.is_file())  # 매니페스트가 어디 있든 정해진 자리에 남긴다
        self.assert_exact_result()

        rc, out, _ = self.cli("apply")
        self.assertEqual(rc, 0, out)
        for kind, count in (("decision", 1), ("work", 2), ("item", 5), ("comment", 5), ("backlog", 3)):
            self.assertEqual(self.row(out, kind), [0, 0, count, 0], kind)
        self.assert_exact_result()

    def test_conflict_is_listed_and_exits_3(self) -> None:
        self.cli("apply")
        self.edit_in_plane(self.by_key(_I1))
        before = self.snapshot_plane()
        rc, out, _ = self.cli("apply")
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.row(out, "item"), [0, 0, 4, 1])
        self.assertIn(f"- {_I1} → {self.by_key(_I1)['id']}", out.splitlines())
        self.assertEqual(self.snapshot_plane(), before)
        rc, out, _ = self.cli("preview")
        self.assertEqual(rc, 0, out)
        self.assertIn("- 충돌: 1", out.splitlines())

    def test_failed_check_writes_nothing(self) -> None:
        _record(self.data, _I1)["name"] = "/home/" + "alice"
        for command in ("preview", "apply"):
            with self.subTest(command=command):
                rc, out, err = self.cli(command)
                self.assertEqual(rc, 1)
                self.assertIn("문제 1건", out)
                self.assertNotIn("alice", out + err)
                self.assertEqual(self.writes(), [])
                self.assertFalse(self.applied.exists())

    def test_unmarked_comment_is_counted_as_same_and_listed(self) -> None:
        self.cli("apply")
        project = self.plane._project_by_id(self.pid)
        comment = project.comments[self.by_key(_W1)["id"]][0]
        comment["comment_html"] = "<p>2026-11-24T09:00</p><p>Work 댓글</p>"  # 표식이 지워졌다
        applied = AppliedState.load(self.applied)
        self.applied.unlink()
        self.assertIsNotNone(applied.get(_CW))
        rc, out, _ = self.cli("apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.row(out, "comment"), [0, 0, 5, 0])
        self.assertIn(f"- {_CW} → {comment['id']} (same-unmarked)", out.splitlines())

    def test_plane_failure_exits_2_and_the_next_run_resumes(self) -> None:
        with patch.object(self.plane, "create_comment", side_effect=PlaneUnavailable("Plane 요청 실패: HTTP 503")):
            rc, _, err = self.cli("apply")
        self.assertEqual(rc, 2)
        self.assertIn("HTTP 503", err)
        rc, out, _ = self.cli("apply")
        self.assertEqual(rc, 0, out)
        self.assert_exact_result()

    def test_run_apply_uses_the_given_env_and_does_not_commit(self) -> None:
        env = {**self.env, "HOME": str(self.tmp / "other-home")}
        self.data["records"].append(
            {"key": f"{_P}/resources/derived/a.txt", "kind": "resource", "path": "derived/a.txt", "redactions": []}
        )
        source = self.tmp / "other-home" / "src" / "resources" / "derived" / "a.txt"
        source.parent.mkdir(parents=True)
        source.write_text("다른 홈의 파일\n", encoding="utf-8")
        self.assertNotEqual(os.environ["HOME"], env["HOME"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_apply(self.manifest_file(), self.repo, self.plane, env), 0)
        self.assertEqual(self.ledger("resources/pmt/derived/a.txt").read_text(encoding="utf-8"), "다른 홈의 파일\n")
        self.assertRaises(Exception, self.git, "rev-parse", "--verify", "HEAD")

    def test_repo_without_ledger_is_refused(self) -> None:
        self.ledger("ledger.yml").unlink()
        for command in ("preview", "apply"):
            with self.subTest(command=command):
                rc, _, err = self.cli(command)
                self.assertEqual(rc, 1)
                self.assertIn("초기화되지 않은 저장소", err)
                self.assertEqual(self.writes(), [])


class FakeContractTest(unittest.TestCase):
    def test_fake_comment_update_touches_the_item(self) -> None:
        plane = FakePlane()
        pid = plane.create_project("p", "P")["id"]
        item = plane.create_item(pid, "n", "", [])
        comment = plane.create_comment(pid, item["id"], "<p>a</p>")
        before = plane.get_item(pid, item["id"])["updated_at"]
        updated = plane.update_comment(pid, item["id"], comment["id"], "<p>b</p>")
        self.assertEqual(updated["comment_html"], "<p>b</p>")
        self.assertEqual([c["comment_html"] for c in plane.list_comments(pid, item["id"])], ["<p>b</p>"])
        self.assertGreater(plane.get_item(pid, item["id"])["updated_at"], before)


if __name__ == "__main__":
    unittest.main()
