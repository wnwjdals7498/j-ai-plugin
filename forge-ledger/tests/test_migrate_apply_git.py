from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ledger.facts import parse_facts
from ledger.flows import FlowError, Repo, SecretFound, init_repo
from ledger.history import HistoryEntry, parse_history
from ledger.migrate import load_manifest
from ledger.migrate_apply import AppliedEntry, AppliedState, Outcome, apply_git
from ledger.plane import PlaneUnavailable
from ledger.sections import split_html_sections
from tests.fakes import FakePlane

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "plugins" / "forge-ledger" / "skills" / "ledger-plan" / "templates"

# 검사에 걸리는 가짜 홈 경로. 이 파일 자신은 걸리지 않게 나눠 적는다.
_FAKE_HOME = "/home/" + "alice/notes"
_P = "pmt:demo"
_PROJECT, _D1, _D2, _D3 = f"{_P}/project", f"{_P}/D1", f"{_P}/D2", f"{_P}/D3"
_F1, _F7 = f"{_P}/F1", f"{_P}/F7"
_RESOURCE = f"{_P}/resources/derived/a.txt"
_WORK = f"{_P}/_default/Work1"
_GIT_KEYS = [_PROJECT, _D1, _D2, _D3, _F1, _F7, _RESOURCE]
_RESOURCE_BYTES = "줄 하나\r\n줄 둘 (줄 끝 그대로)\n".encode()


def _decision(key: str, day: str, status: str, title: str, **extra: Any) -> dict[str, Any]:
    record = {"key": key, "kind": "decision", "date": day, "status": status, "title": title}
    return {**record, "detail": f"{title}의 근거 <b> & 둘\n다음 줄", "redactions": [], **extra}


def _fact(key: str, day: str, text: str) -> dict[str, Any]:
    return {"key": key, "kind": "fact", "date": day, "status": "Active", "text": text, "ref": "PR", "redactions": []}


def _manifest() -> dict[str, Any]:
    return {
        "version": 1,
        "slug": "demo",
        "source_dir": "~/src",
        "records": [
            {"key": _PROJECT, "kind": "project", "body_md": "# 목표\n\n본문 | 그대로", "redactions": []},
            _decision(_D1, "2026-11-20", "대체", "첫 결정", replaced_by=_D2),
            _decision(_D2, "2026-11-22", "승인", "둘째 결정"),
            _decision(_D3, "2026-11-21", "폐기", "셋째 결정"),
            _fact(_F1, "2026-11-20", "사실 | 파이프"),
            _fact(_F7, "2026-11-21", "일곱째"),
            {"key": _RESOURCE, "kind": "resource", "path": "derived/a.txt", "redactions": []},
            {
                "key": _WORK,
                "kind": "work",
                "name": "일",
                "status": "Done",
                "labels": [],
                "description_html": "",
                "redactions": [],
            },
        ],
    }


def _record(data: dict[str, Any], key: str) -> dict[str, Any]:
    return next(r for r in data["records"] if r["key"] == key)


class _ApplyCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(self.tmp), XDG_CONFIG_HOME=str(self.tmp / ".config"), GIT_CONFIG_NOSYSTEM="1")
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.source = self.tmp / "src" / "resources" / "derived" / "a.txt"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(_RESOURCE_BYTES)

        root = self.tmp / "sample-app"
        root.mkdir()
        self.repo = Repo(root=root, name=root.name)
        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "migrate/demo")
        self.plane = FakePlane()
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        self.plane.calls.clear()
        self.data = _manifest()
        self.state_path = self.tmp / "state" / "demo.applied.json"

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo.root), *args], check=True, capture_output=True)

    def apply(self, state: AppliedState | None = None) -> list[Outcome]:
        path = self.tmp / "manifest.json"
        path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        state = state if state is not None else AppliedState.load(self.state_path)
        return apply_git(load_manifest(path), self.repo, self.plane, state)

    def actions(self, outcomes: list[Outcome]) -> dict[str, str]:
        return {o.key: o.action for o in outcomes}

    def assert_only(self, outcomes: list[Outcome], changed: dict[str, str]) -> None:
        self.assertEqual(self.actions(outcomes), {**dict.fromkeys(_GIT_KEYS, "same"), **changed})

    def ledger(self, relative: str) -> Path:
        return self.repo.root / ".ledger" / relative

    def history(self) -> list[HistoryEntry]:
        return parse_history(self.ledger("history.md").read_text(encoding="utf-8"))

    def entry(self, original: str) -> HistoryEntry:
        found = [e for e in self.history() if e.detail.startswith(f"원래 ID: demo {original} · ")]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def plane_sections(self) -> dict[str, str]:
        key = next(p["identifier"] for p in self.plane.list_projects())
        self.plane.calls.pop()
        return split_html_sections(self.plane.item_html(key, "[계획서] 결정"))

    def snapshot(self) -> dict[str, bytes]:
        base = self.repo.root / ".ledger"
        return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}

    def writes(self) -> list[str]:
        return [c[0] for c in self.plane.calls if c[0].startswith(("create_", "update_", "delete_", "add_"))]


class ApplyGitTest(_ApplyCase):
    def test_first_apply_writes_everything_in_order(self) -> None:
        outcomes = self.apply()
        self.assertEqual([(o.key, o.action) for o in outcomes], [(key, "created") for key in _GIT_KEYS])
        self.assertEqual(self.ledger("project.md").read_bytes(), "# 목표\n\n본문 | 그대로".encode())
        self.assertEqual(self.ledger("resources/pmt/derived/a.txt").read_bytes(), _RESOURCE_BYTES)
        facts = parse_facts(self.ledger("facts.md").read_text(encoding="utf-8"))
        self.assertEqual(
            [(f.fact_id, f.status, f.created, f.text, f.ref) for f in facts],
            [("F1", "Active", "2026-11-20", "사실 | 파이프", "PR"), ("F7", "Active", "2026-11-21", "일곱째", "PR")],
        )
        # Work 같은 Plane 쪽 레코드는 이 단계에서 만들지 않는다. 결정 항목만 고친다.
        self.assertEqual(self.writes(), ["update_item_description"] * 3)

    def test_decision_ids_keep_original_dates_and_relink_replacements(self) -> None:
        outcomes = {o.key: o for o in self.apply()}
        d1, d2, d3 = self.entry("D1"), self.entry("D2"), self.entry("D3")
        for entry, prefix in ((d1, "D-261120-"), (d2, "D-261122-"), (d3, "D-261121-")):
            self.assertRegex(entry.ref_id, f"^{prefix}[0-9a-f]{{4}}$")
        self.assertEqual((d1.date, d1.kind, d1.projects, d1.title), ("2026-11-20", "decision", "*", "첫 결정"))
        self.assertRegex(d1.detail, rf"^원래 ID: demo D1 · 상태: 대체 · 대체: {d2.ref_id} · plane:SAMPM-\d+@\S+$")
        self.assertRegex(d2.detail, r"^원래 ID: demo D2 · 상태: 승인 · plane:")
        self.assertRegex(d3.detail, r"^원래 ID: demo D3 · 상태: 폐기 · plane:")
        self.assertEqual([outcomes[k].target for k in (_D1, _D2, _D3)], [d1.ref_id, d2.ref_id, d3.ref_id])
        sections = self.plane_sections()
        self.assertEqual(set(sections) & {d1.ref_id, d2.ref_id, d3.ref_id}, {d1.ref_id, d2.ref_id, d3.ref_id})
        self.assertIn(f"원래 ID: demo D1 · 상태: 대체 · 대체: {d2.ref_id}", sections[d1.ref_id])
        self.assertIn("첫 결정의 근거 &lt;b&gt; &amp; 둘<br>다음 줄", sections[d1.ref_id])

    def test_second_run_is_all_same_and_writes_nothing(self) -> None:
        self.apply()
        before = self.snapshot()
        self.plane.calls.clear()
        self.assert_only(self.apply(), {})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.writes(), [])

    def test_title_with_pipe_and_newline_is_recorded_as_one_line(self) -> None:
        _record(self.data, _D3)["title"] = "a | b\nc"
        self.apply()
        self.assertEqual(self.entry("D3").title, "a / b c")
        self.assert_only(self.apply(), {})


class BranchAndGateTest(_ApplyCase):
    def assert_nothing_written(self, error: type[Exception], pattern: str) -> None:
        before = self.snapshot()
        with self.assertRaisesRegex(error, pattern) as ctx:
            self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.state_path.exists())
        self.assertNotIn("alice", str(ctx.exception))

    def test_other_branch_is_refused(self) -> None:
        for branch in ("master-work", "migrate/other", "migrate/demo-2"):
            with self.subTest(branch=branch):
                self.git("checkout", "-q", "-B", branch)
                self.assert_nothing_written(FlowError, r"^브랜치 migrate/demo에서 실행하세요$")

    def test_detached_head_and_non_git_folder_are_refused(self) -> None:
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
        self.git("checkout", "-q", "--detach")
        self.assert_nothing_written(FlowError, "브랜치 migrate/demo에서 실행하세요")
        (self.repo.root / ".git").rename(self.tmp / "moved-git")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.tmp)
        self.assert_nothing_written(FlowError, "브랜치 migrate/demo에서 실행하세요")

    def test_manifest_that_fails_check_writes_nothing(self) -> None:
        _record(self.data, _F7)["text"] = f"위치 {_FAKE_HOME}"
        self.assert_nothing_written(FlowError, "매니페스트 검사 실패: 1건")

    def test_resource_changed_after_check_is_still_refused(self) -> None:
        # 검사 뒤에 파일이 바뀌어도, 실제로 쓰는 바이트를 다시 검사한다.
        self.source.write_text(f"{_FAKE_HOME}\n", encoding="utf-8")
        with patch("ledger.migrate_apply.check_manifest", return_value=[]):
            with self.assertRaisesRegex(SecretFound, "home_path") as ctx:
                self.apply()
        self.assertNotIn("alice", str(ctx.exception))
        self.assertFalse(self.ledger("resources/pmt/derived/a.txt").exists())

    def test_uninitialized_repo_with_decisions_writes_nothing(self) -> None:
        self.ledger("ledger.yml").unlink()
        self.assert_nothing_written(FlowError, "초기화되지 않은 저장소")
        self.assertFalse(self.ledger("project.md").exists())

    def test_symlinked_target_is_never_written(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "victim.md").write_text("keep\n", encoding="utf-8")
        for relative, destination in (
            ("project.md", outside / "victim.md"),
            ("facts.md", outside / "new-facts.md"),
            ("resources", outside),
        ):
            with self.subTest(target=relative):
                link = self.ledger(relative)
                link.symlink_to(destination)
                with self.assertRaisesRegex(FlowError, "심볼릭 링크"):
                    self.apply()
                link.unlink()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["victim.md"])
        self.assertEqual((outside / "victim.md").read_text(encoding="utf-8"), "keep\n")


class ConflictAndUpdateTest(_ApplyCase):
    def edit(self, relative: str, old: str, new: str) -> bytes:
        path = self.ledger(relative)
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")
        return path.read_bytes()

    def test_hand_edited_fact_is_a_conflict_and_stays(self) -> None:
        self.apply()
        edited = self.edit("facts.md", "일곱째", "손으로 고침")
        self.assert_only(self.apply(), {_F7: "conflict"})
        self.assertEqual(self.ledger("facts.md").read_bytes(), edited)

    def test_reformatted_table_and_new_rows_are_not_conflicts(self) -> None:
        self.apply()
        self.edit("facts.md", "| F7 |", "|   F7   |")
        with self.ledger("facts.md").open("a", encoding="utf-8") as handle:
            handle.write("| F8 | Active | 2026-11-26 | 이전 뒤에 더한 사실 |  |\n")
        self.assert_only(self.apply(), {})

    def test_hand_edited_or_deleted_files_and_history_are_conflicts(self) -> None:
        self.apply()
        project = self.edit("project.md", "본문", "고친 본문")
        self.ledger("resources/pmt/derived/a.txt").unlink()
        history = self.edit("history.md", "둘째 결정", "고친 제목")
        self.plane.calls.clear()
        self.assert_only(self.apply(), {_PROJECT: "conflict", _RESOURCE: "conflict", _D2: "conflict"})
        self.assertEqual(self.ledger("project.md").read_bytes(), project)
        self.assertFalse(self.ledger("resources/pmt/derived/a.txt").exists())
        self.assertEqual(self.ledger("history.md").read_bytes(), history)
        self.assertEqual(self.writes(), [])

    def test_manifest_change_updates_untouched_files_and_facts(self) -> None:
        self.apply()
        _record(self.data, _PROJECT)["body_md"] = "# 새 목표\n"
        _record(self.data, _F1)["status"] = "폐기"
        self.source.write_bytes(b"new bytes\n")
        self.assert_only(self.apply(), {_PROJECT: "updated", _F1: "updated", _RESOURCE: "updated"})
        self.assertEqual(self.ledger("project.md").read_bytes(), "# 새 목표\n".encode())
        self.assertEqual(self.ledger("resources/pmt/derived/a.txt").read_bytes(), b"new bytes\n")
        self.assertEqual(parse_facts(self.ledger("facts.md").read_text(encoding="utf-8"))[0].status, "폐기")
        self.assert_only(self.apply(), {})

    def test_changed_decision_is_reported_and_never_rewritten(self) -> None:
        self.apply()
        before = self.snapshot()
        sections = self.plane_sections()
        for name, value in (
            ("title", "바뀐 제목"),
            ("status", "폐기"),
            ("detail", "본문만 바뀜"),
            ("date", "2026-11-01"),
        ):
            with self.subTest(field=name):
                original = copy.deepcopy(self.data)
                _record(self.data, _D2)[name] = value
                self.plane.calls.clear()
                self.assert_only(self.apply(), {_D2: "conflict"})
                self.assertEqual((self.snapshot(), self.writes()), (before, []))
                self.assertEqual(self.plane_sections(), sections)
                self.data = original

    def test_unmanaged_existing_content_is_not_overwritten(self) -> None:
        self.ledger("project.md").write_text("사람이 쓴 문서\n", encoding="utf-8")
        resource = self.ledger("resources/pmt/derived/a.txt")
        resource.mkdir(parents=True)  # 같은 자리에 폴더가 있다
        self.ledger("facts.md").write_text(
            "| ID | 상태 | 생성 | 내용 | 출처 |\n| --- | --- | --- | --- | --- |\n"
            "| F1 | Active | 2026-11-01 | 다른 사실 |  |\n",
            encoding="utf-8",
        )
        actions = self.actions(self.apply())
        self.assertEqual({k for k, a in actions.items() if a == "conflict"}, {_PROJECT, _F1, _RESOURCE})
        self.assertEqual(actions[_F7], "created")
        self.assertEqual(self.ledger("project.md").read_text(encoding="utf-8"), "사람이 쓴 문서\n")
        self.assertIn("다른 사실", self.ledger("facts.md").read_text(encoding="utf-8"))
        self.assertIsNone(AppliedState.load(self.state_path).get(_PROJECT))

    def test_broken_facts_table_stops_with_message(self) -> None:
        self.ledger("facts.md").write_text(
            "| ID | 상태 | 생성 | 내용 | 출처 |\n| --- | --- | --- | --- | --- |\n| F1 | broken |\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(FlowError, "facts.md"):
            self.apply()


class ResumeTest(_ApplyCase):
    """중간에 멈추거나 적용 기록을 잃어도 다시 실행하면 중복이 생기지 않는다."""

    def assert_no_duplicates(self) -> None:
        ids = [self.entry(original).ref_id for original in ("D1", "D2", "D3")]
        html = self.plane.item_html("SAMPM", "[계획서] 결정")
        for ref_id in ids:
            self.assertEqual(html.count(f"<h2>{ref_id} "), 1)
        for original in ("D1", "D2", "D3"):
            self.assertEqual(html.count(f"원래 ID: demo {original} · "), 1)
        self.assertEqual(len([e for e in self.history() if e.detail.startswith("원래 ID: demo ")]), 3)
        self.assertIn(f"대체: {ids[1]}", self.entry("D1").detail)

    def test_state_is_saved_after_each_record(self) -> None:
        real = self.plane.update_item_description
        calls: list[int] = []

        def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(1)
            if len(calls) == 2:
                raise PlaneUnavailable("Plane 요청 실패: HTTP 503")
            return real(*args, **kwargs)

        with patch.object(self.plane, "update_item_description", failing):
            with self.assertRaises(PlaneUnavailable):
                self.apply()
        saved = AppliedState.load(self.state_path)
        done = {key: saved.get(key) for key in (_PROJECT, _D1, _D2, _D3)}
        self.assertTrue(done[_PROJECT] and done[_PROJECT].digest)
        self.assertTrue(done[_D1] and done[_D1].digest)
        for key in (_D2, _D3):  # ID만 예약됐고 아직 쓰지 않았다.
            reserved = done[key]
            assert reserved is not None
            self.assertEqual((reserved.digest, reserved.applied_at), ("", ""))
        self.assertIsNone(saved.get(_F1))
        first_id = self.entry("D1").ref_id
        d2_id_in_d1 = self.entry("D1").detail.split("대체: ")[1].split(" ")[0]

        actions = self.actions(self.apply())
        self.assertEqual((actions[_PROJECT], actions[_D1], actions[_D3]), ("same", "same", "created"))
        # D1이 이미 가리킨 D2의 새 ID는 실패 전에 정한 것이라, D2는 그 ID로 만들어져야 한다.
        self.assertEqual((actions[_D2], self.entry("D2").ref_id), ("created", d2_id_in_d1))
        self.assertEqual(self.entry("D1").ref_id, first_id)
        self.assert_no_duplicates()
        self.assert_only(self.apply(), {})

    def test_all_decision_ids_are_saved_before_the_first_decision_write(self) -> None:
        seen: list[list[str]] = []
        real = self.plane.update_item_description

        def spying(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if not seen:
                state = AppliedState.load(self.state_path)
                seen.append([entry.target for k in (_D1, _D2, _D3) if (entry := state.get(k)) is not None])
            return real(*args, **kwargs)

        with patch.object(self.plane, "update_item_description", spying):
            outcomes = {o.key: o.target for o in self.apply()}
        self.assertEqual(seen, [[outcomes[_D1], outcomes[_D2], outcomes[_D3]]])

    def test_reserved_id_is_replaced_when_the_date_changes_before_writing(self) -> None:
        with patch.object(self.plane, "update_item_description", side_effect=PlaneUnavailable("503")):
            with self.assertRaises(PlaneUnavailable):
                self.apply()
        _record(self.data, _D3)["date"] = "2026-11-25"
        outcomes = {o.key: o for o in self.apply()}
        self.assertEqual(outcomes[_D3].action, "created")
        self.assertRegex(outcomes[_D3].target, r"^D-261125-")

    def test_lost_state_adopts_existing_records(self) -> None:
        self.apply()
        before = self.snapshot()
        self.state_path.unlink()
        self.plane.calls.clear()
        self.assert_only(self.apply(), {})
        self.assertEqual((self.snapshot(), self.writes()), (before, []))
        self.assertIsNotNone(AppliedState.load(self.state_path).get(_D1))
        self.assert_no_duplicates()

    def test_plane_written_but_history_missing_is_resumed(self) -> None:
        with patch("ledger.flows.append_entry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual([e for e in self.history() if e.detail.startswith("원래 ID")], [])
        self.state_path.unlink(missing_ok=True)
        self.plane.calls.clear()
        actions = self.actions(self.apply())
        self.assertEqual([actions[k] for k in (_D1, _D2, _D3)], ["created"] * 3)
        self.assertEqual(self.writes(), ["update_item_description"] * 2)  # D1의 절은 이미 있다
        self.assert_no_duplicates()

    def test_resume_with_changed_manifest_after_plane_write_is_a_conflict(self) -> None:
        with patch("ledger.flows.append_entry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()
        _record(self.data, _D1)["detail"] = "그 사이에 바뀐 본문"
        self.assertEqual(self.actions(self.apply())[_D1], "conflict")
        self.assertEqual([e for e in self.history() if e.detail.startswith("원래 ID: demo D1 ")], [])

    def test_lost_state_with_hand_edited_history_is_a_conflict_not_a_duplicate(self) -> None:
        self.apply()
        self.state_path.unlink()
        path = self.ledger("history.md")
        path.write_text(path.read_text(encoding="utf-8").replace("둘째 결정", "고친 제목"), encoding="utf-8")
        self.plane.calls.clear()
        self.assert_only(self.apply(), {_D2: "conflict"})
        self.assertEqual(self.writes(), [])


class IdRecoveryTest(_ApplyCase):
    def fail_after_first_decision(self) -> str:
        """D1(→D2)만 기록하고 멈춘다. D1이 가리키는 D2의 ID를 돌려준다."""
        real = self.plane.update_item_description
        calls: list[int] = []

        def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append(1)
            if len(calls) == 2:
                raise PlaneUnavailable("Plane 요청 실패: HTTP 503")
            return real(*args, **kwargs)

        with patch.object(self.plane, "update_item_description", failing):
            with self.assertRaises(PlaneUnavailable):
                self.apply()
        return self.entry("D1").detail.split("대체: ")[1].split(" ")[0]

    def retarget_to_new_decision(self, day: str) -> str:
        d4 = f"{_P}/D4"
        self.data["records"].insert(4, _decision(d4, day, "승인", "넷째 결정"))
        _record(self.data, _D1)["replaced_by"] = d4
        return d4

    def test_retargeted_replacement_is_a_conflict_even_on_the_same_date(self) -> None:
        for day in ("2026-11-22", "2026-11-25"):  # D2와 같은 날, 다른 날
            with self.subTest(day=day):
                self.setUp()
                old_id = self.fail_after_first_decision()
                d4 = self.retarget_to_new_decision(day)
                outcomes = {o.key: o for o in self.apply()}
                self.assertEqual(outcomes[_D1].action, "conflict")
                self.assertEqual(outcomes[_D2].target, old_id)  # 예약한 ID는 원래 대상의 것이다.
                self.assertNotEqual(outcomes[d4].target, old_id)
                self.assertRegex(outcomes[d4].target, f"^D-{day[2:4]}{day[5:7]}{day[8:]}-")

    def test_retargeting_an_applied_decision_is_a_conflict(self) -> None:
        self.apply()
        _record(self.data, _D1)["replaced_by"] = _D3
        self.assert_only(self.apply(), {_D1: "conflict"})

    def test_replaced_id_is_recovered_from_the_written_decision_without_state(self) -> None:
        old_id = self.fail_after_first_decision()
        self.state_path.unlink()
        outcomes = {o.key: o for o in self.apply()}
        self.assertEqual((outcomes[_D1].action, outcomes[_D2].action), ("same", "created"))
        self.assertEqual(outcomes[_D2].target, old_id)

    def test_recovered_id_must_match_the_target_date(self) -> None:
        self.fail_after_first_decision()
        self.state_path.unlink()
        d4 = self.retarget_to_new_decision("2026-11-25")
        outcomes = {o.key: o for o in self.apply()}
        self.assertEqual(outcomes[_D1].action, "conflict")
        self.assertRegex(outcomes[d4].target, r"^D-261125-")

    def test_replacement_id_is_read_only_from_the_leading_line(self) -> None:
        # 본문에 비슷한 글이 있어도 ID로 줍지 않는다.
        decoy = "D-261122-dead"
        self.data = {**self.data, "records": [r for r in self.data["records"] if r["key"] in (_D1, _D2)]}
        _record(self.data, _D1).update(status="승인", detail=f"예전 메모 · 대체: {decoy}")
        del _record(self.data, _D1)["replaced_by"]
        with patch("ledger.flows.append_entry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()  # D1의 절만 Plane에 남는다
        self.state_path.unlink(missing_ok=True)
        _record(self.data, _D1)["replaced_by"] = _D2
        outcomes = {o.key: o for o in self.apply()}
        self.assertEqual(outcomes[_D1].action, "conflict")
        self.assertNotEqual(outcomes[_D2].target, decoy)

    def test_original_id_with_markup_characters_is_still_recognised(self) -> None:
        key = f"{_P}/D<9>&x"
        self.data["records"] = [_decision(key, "2026-11-20", "승인", "표시 문자가 든 ID")]
        with patch("ledger.flows.append_entry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()  # Plane 절만 남는다: 절에서 표지를 알아봐야 한다
        self.state_path.unlink(missing_ok=True)
        self.plane.calls.clear()
        resumed = self.apply()
        self.assertEqual((resumed[0].action, self.writes()), ("created", []))
        self.assertEqual(self.plane.item_html("SAMPM", "[계획서] 결정").count("<h2>D-261120-"), 1)
        self.state_path.unlink()
        again = self.apply()
        self.assertEqual([(o.action, o.target) for o in again], [("same", resumed[0].target)])


class RemoteSectionTest(_ApplyCase):
    """적용한 결정도 Plane 절의 글이 달라졌으면 충돌이다."""

    def decision_html(self) -> str:
        return self.plane.item_html("SAMPM", "[계획서] 결정")

    def set_decision_html(self, html: str) -> None:
        pid = next(p["id"] for p in self.plane.list_projects() if p["identifier"] == "SAMPM")
        item = next(i for i in self.plane.list_items(pid) if i["name"] == "[계획서] 결정")
        self.plane.update_item(pid, item["id"], {"description_html": html})
        self.plane.calls.clear()

    def edit_section(self, old: str, new: str) -> None:
        html = self.decision_html()
        self.assertEqual(html.count(old), 1, old)
        self.set_decision_html(html.replace(old, new))

    def assert_conflict_only_for_d2(self) -> None:
        before = (self.snapshot(), self.decision_html())
        for _ in range(2):  # 다시 실행해도 계속 충돌이다.
            self.plane.calls.clear()
            self.assert_only(self.apply(), {_D2: "conflict"})
            self.assertEqual((self.snapshot(), self.decision_html()), before)
            self.assertEqual(self.writes(), [])

    def test_changed_section_text_is_a_conflict_until_restored(self) -> None:
        self.apply()
        original = self.decision_html()
        self.edit_section("둘째 결정의 근거", "Plane에서 고친 근거")
        self.assert_conflict_only_for_d2()
        self.set_decision_html(original)
        self.assert_only(self.apply(), {})

    def test_changed_heading_added_text_and_removed_section_are_conflicts(self) -> None:
        for label in ("제목 수정", "문단 추가", "절 삭제"):
            with self.subTest(case=label):
                self.setUp()
                self.apply()
                ref_id = self.entry("D2").ref_id
                if label == "제목 수정":
                    self.edit_section(f"<h2>{ref_id} 둘째 결정</h2>", f"<h2>{ref_id} 이름을 바꿈</h2>")
                elif label == "문단 추가":
                    self.edit_section(f"<h2>{ref_id} 둘째 결정</h2>", f"<h2>{ref_id} 둘째 결정</h2><p>덧붙인 말</p>")
                else:
                    section = split_html_sections(self.decision_html())[ref_id]
                    self.edit_section(f"<h2>{ref_id} 둘째 결정</h2>{section}", "")
                self.assert_conflict_only_for_d2()

    def test_markup_only_changes_and_new_sections_are_not_conflicts(self) -> None:
        self.apply()
        html = self.decision_html().replace("<p>", '<p class="editor-paragraph">').replace("<br>", "<br/>\n")
        self.set_decision_html(html + "<p></p><h2>D-261126-abcd 따로 더한 결정</h2><p>새 결정</p>")
        self.assert_only(self.apply(), {})

    def test_server_rewriting_text_on_save_is_not_a_false_conflict(self) -> None:
        # 서버가 저장하며 글을 바꿔도, 저장 응답의 글을 기준으로 기록하므로 다음 실행은 같다.
        real = self.plane.update_item_description

        def normalizing(project_id: str, item_id: str, html: str, expected_updated_at: str) -> dict[str, Any]:
            return real(project_id, item_id, html.replace("의 근거", "의  판단 근거"), expected_updated_at)

        with patch.object(self.plane, "update_item_description", normalizing):
            self.apply()
        self.assertIn("판단 근거", self.decision_html())
        self.assert_only(self.apply(), {})
        self.edit_section("둘째 결정의  판단 근거", "다시 고친 근거")
        self.assert_only(self.apply(), {_D2: "conflict"})

    def test_decision_item_is_read_once_per_rerun(self) -> None:
        self.apply()
        self.plane.calls.clear()
        self.assert_only(self.apply(), {})
        self.assertEqual([c[0] for c in self.plane.calls].count("get_item"), 1)

    def test_hand_added_note_on_history_line_is_still_a_conflict(self) -> None:
        self.apply()
        entry = self.entry("D2")
        path = self.ledger("history.md")
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(entry.detail, f"{entry.detail} · 손으로 단 메모"), encoding="utf-8")
        self.assert_only(self.apply(), {_D2: "conflict"})
        self.state_path.unlink()
        self.assert_only(self.apply(), {_D2: "conflict"})


class StateSaveFailureTest(_ApplyCase):
    """대상은 썼는데 적용 기록 저장이 실패해도, 다시 실행하면 같다고 보고 기록을 복구한다."""

    def fail_next_save(self) -> None:
        with patch.object(AppliedState, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply()

    def recorded_digest(self, key: str) -> str:
        entry = AppliedState.load(self.state_path).get(key)
        assert entry is not None
        return entry.digest

    def change(self, key: str) -> None:
        if key == _PROJECT:
            _record(self.data, _PROJECT)["body_md"] = "# 새 목표\n"
        elif key == _F1:
            _record(self.data, _F1)["text"] = "고친 사실"
        else:
            self.source.write_bytes(b"new bytes\n")

    def test_update_written_but_not_recorded_is_repaired(self) -> None:
        for key in (_PROJECT, _F1, _RESOURCE):
            with self.subTest(key=key):
                self.setUp()
                self.apply()
                old = self.recorded_digest(key)
                self.change(key)
                self.fail_next_save()
                written = self.snapshot()
                self.assertEqual(self.recorded_digest(key), old)  # 기록은 옛 값이다.

                self.assert_only(self.apply(), {})
                self.assertEqual(self.snapshot(), written)
                self.assertNotEqual(self.recorded_digest(key), old)
                self.assert_only(self.apply(), {})

    def test_repaired_record_still_protects_later_hand_edits(self) -> None:
        self.apply()
        self.change(_PROJECT)
        self.fail_next_save()
        self.apply()
        self.ledger("project.md").write_text("손으로 고침\n", encoding="utf-8")
        self.assert_only(self.apply(), {_PROJECT: "conflict"})
        self.assertEqual(self.ledger("project.md").read_text(encoding="utf-8"), "손으로 고침\n")

    def test_content_that_matches_neither_old_nor_wanted_is_a_conflict(self) -> None:
        self.apply()
        self.change(_PROJECT)
        self.fail_next_save()
        _record(self.data, _PROJECT)["body_md"] = "# 그 사이에 또 바뀐 목표\n"
        self.assert_only(self.apply(), {_PROJECT: "conflict"})

    def test_current_equal_to_wanted_is_same_even_after_a_manual_change(self) -> None:
        self.apply()
        self.change(_PROJECT)
        self.ledger("project.md").write_bytes("# 새 목표\n".encode())  # 사람이 같은 내용으로 먼저 고침
        self.assert_only(self.apply(), {})
        self.assertEqual(self.recorded_digest(_PROJECT), hashlib.sha256("# 새 목표\n".encode()).hexdigest())

    def test_decision_written_but_not_recorded_is_repaired(self) -> None:
        real = AppliedState.save
        saves: list[int] = []

        def failing_after_first_decision(state: AppliedState, path: Path) -> None:
            entry = state.get(_D1)
            if entry is not None and entry.digest and not saves:
                saves.append(1)
                raise OSError("disk full")
            real(state, path)

        with patch.object(AppliedState, "save", failing_after_first_decision):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.recorded_digest(_D1), "")  # 예약만 남았다.
        actions = self.actions(self.apply())
        self.assertEqual((actions[_D1], actions[_D2]), ("same", "created"))
        self.assertTrue(self.recorded_digest(_D1))
        self.assert_only(self.apply(), {})


class AppliedStateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "migrations" / "demo.applied.json"

    def test_missing_file_is_empty_and_json_format_is_stable(self) -> None:
        state = AppliedState.load(self.path)
        self.assertIsNone(state.get("k"))
        state.put("k", AppliedEntry(target="t", applied_at="2026-11-26T00:00:00Z", digest="d"))
        state.save(self.path)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8")),
            {
                "version": 1,
                "entries": {
                    "k": {
                        "target": "t",
                        "applied_at": "2026-11-26T00:00:00Z",
                        "digest": "d",
                        "remote_updated_at": "",
                    }
                },
            },
        )
        self.assertEqual(AppliedState.load(self.path).get("k"), state.get("k"))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["demo.applied.json"])

    def test_remote_updated_at_round_trips_and_may_be_absent(self) -> None:
        state = AppliedState.load(self.path)
        state.put("k", AppliedEntry("t", "a", "d", remote_updated_at="2026-11-26T01:00:00Z"))
        state.save(self.path)
        loaded = AppliedState.load(self.path).get("k")
        assert loaded is not None
        self.assertEqual(loaded.remote_updated_at, "2026-11-26T01:00:00Z")
        self.path.write_text(
            '{"version": 1, "entries": {"k": {"target": "t", "applied_at": "a", "digest": "d"}}}', encoding="utf-8"
        )
        self.assertEqual(AppliedState.load(self.path).get("k"), AppliedEntry("t", "a", "d"))

    def test_corrupt_file_is_not_treated_as_empty(self) -> None:
        # 빈 기록으로 넘어가면 이미 쓴 것을 다시 쓴다.
        self.path.parent.mkdir(parents=True)
        for text in (
            "not json",
            "[]",
            '{"version": 2, "entries": {}}',
            '{"version": 1}',
            '{"version": 1, "entries": {"k": {"target": "t"}}}',
            '{"version": 1, "entries": {"k": {"target": 1, "applied_at": "a", "digest": "d"}}}',
            '{"version": 1, "entries": {"k": {"target": "t", "applied_at": "a", "digest": "d", "x": "y"}}}',
        ):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(FlowError, "적용 기록"):
                    AppliedState.load(self.path)


if __name__ == "__main__":
    unittest.main()
