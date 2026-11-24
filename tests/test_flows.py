from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ledger.check import SourceUnavailable, run_check
from ledger.config import load_config
from ledger.flows import (
    FlowError,
    PlaneDocSource,
    Repo,
    SecretFound,
    decide,
    init_repo,
    maintenance_key,
    plan_get,
    plan_new,
    plan_put,
    plane_project_name,
)
from ledger.history import parse_history
from ledger.plane import PlaneAuthError, PlaneConflict, PlaneUnavailable
from ledger.sections import split_html_sections
from ledger.spec import UNIT_ITEMS
from tests.fakes import FakePlane

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "plugins" / "forge-ledger" / "skills" / "ledger-plan" / "templates"


class FlowsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        repo_dir = Path(self._tmp.name) / "sample-app"
        repo_dir.mkdir()
        self.repo = Repo(root=repo_dir, name=repo_dir.name)
        self.plane = FakePlane()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_maintenance_key(self) -> None:
        self.assertEqual(maintenance_key("sample-app", set()), "SAMPM")
        self.assertEqual(maintenance_key("sample-app", {"SAMPM"}), "SAMPM2")

    def test_maintenance_key_fallback(self) -> None:
        self.assertEqual(maintenance_key("한글저장소", set()), "REPOM")

    def test_repo_discover_fallback_and_git(self) -> None:
        non_git = Path(self._tmp.name) / "plain-dir"
        non_git.mkdir()
        discovered = Repo.discover(non_git)
        self.assertEqual(discovered.root, non_git.resolve())
        self.assertEqual(discovered.name, "plain-dir")

        git_root = Path(self._tmp.name) / "git-repo"
        nested = git_root / "a" / "b"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=git_root, check=True, capture_output=True)
        discovered_git = Repo.discover(nested)
        self.assertEqual(discovered_git.root, git_root.resolve())
        self.assertEqual(discovered_git.name, "git-repo")

    def test_init_creates_skeleton_and_maintenance_project(self) -> None:
        cfg = init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        self.assertTrue((self.repo.root / ".ledger/design/security.md").exists())
        self.assertEqual([(p.key, p.title, p.default) for p in cfg.projects], [("SAMPM", "sample app 유지보수", True)])
        self.assertEqual(self.plane.item_names("SAMPM"), ["[계획서] 결정"])
        self.assertEqual(self.plane.labels("SAMPM"), ["계획서"])
        label_calls = [c for c in self.plane.calls if c[0] == "create_label"]
        self.assertEqual([(c[2], c[3]) for c in label_calls], [("계획서", "#6b7280")])

    def test_init_does_not_overwrite(self) -> None:
        (self.repo.root / ".ledger").mkdir()
        (self.repo.root / ".ledger/design").mkdir()
        (self.repo.root / ".ledger/design/data.md").write_text("사람이 쓴 내용", encoding="utf-8")
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        self.assertEqual((self.repo.root / ".ledger/design/data.md").read_text(encoding="utf-8"), "사람이 쓴 내용")

    def test_init_writes_nothing_on_plane_error(self) -> None:
        self.plane.fail_on["create_item"] = PlaneUnavailable("down")
        with self.assertRaises(PlaneUnavailable):
            init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        self.assertFalse((self.repo.root / ".ledger").exists())

    def test_plan_new_twice(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "알림 기능", "NOTI", TEMPLATES, date(2026, 11, 24))
        cfg = load_config(self.repo.root / ".ledger/ledger.yml")
        self.assertEqual([p.key for p in cfg.projects], ["SAMPM", "APP", "NOTI"])
        self.assertEqual(sorted(self.plane.item_names("APP")), sorted(UNIT_ITEMS.values()))
        self.assertEqual(self.plane.module_members("APP", "계획서"), self.plane.item_ids("APP"))
        self.assertEqual(self.plane.cycles("APP"), ["프로토타입"])
        label_calls = [c for c in self.plane.calls if c[0] == "create_label"]
        self.assertEqual([(c[2], c[3]) for c in label_calls], [("계획서", "#6b7280")] * 3)
        entries = parse_history((self.repo.root / ".ledger/history.md").read_text(encoding="utf-8"))
        self.assertEqual(
            [(e.kind, e.projects, e.title) for e in entries],
            [("decision", "APP", "단위 프로젝트 APP 시작"), ("decision", "NOTI", "단위 프로젝트 NOTI 시작")],
        )
        self.assertRegex(entries[0].detail, r"^plane:APP-\d+@")

    def test_init_uses_plane_safe_name(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        create_calls = [c for c in self.plane.calls if c[0] == "create_project"]
        self.assertEqual([(c[1], c[2]) for c in create_calls], [("sample app 유지보수", "SAMPM")])
        cfg = load_config(self.repo.root / ".ledger/ledger.yml")
        self.assertEqual(cfg.projects[0].title, "sample app 유지보수")

    def test_plan_new_sanitizes_title(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "결제 v2.0 (시험)", "PAY", TEMPLATES, date(2026, 11, 23))
        create_calls = [c for c in self.plane.calls if c[0] == "create_project"]
        self.assertEqual(create_calls[-1][1], "결제 v2 0 시험")
        cfg = load_config(self.repo.root / ".ledger/ledger.yml")
        self.assertEqual(cfg.projects[-1].title, "결제 v2 0 시험")

    def test_plan_new_rejects_title_without_usable_chars(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        calls = len(self.plane.calls)
        with self.assertRaisesRegex(FlowError, "제목에 Plane 프로젝트 이름으로 쓸 수 있는 글자가 없습니다"):
            plan_new(self.repo, self.plane, "---", "PAY", TEMPLATES, date(2026, 11, 23))
        self.assertEqual(len(self.plane.calls), calls)

    def test_plane_project_name(self) -> None:
        for ch in "&+,:;$^}{*=?@#|'<>.()%!-":
            self.assertEqual(plane_project_name(f"a{ch}b"), "a b", ch)
        self.assertEqual(plane_project_name("  결제_v2   시험  "), "결제_v2 시험")
        self.assertEqual(plane_project_name("---"), "")

    def test_plan_new_rejects_existing_key(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        calls = len(self.plane.calls)
        with self.assertRaisesRegex(FlowError, "이미 붙은 키: APP"):
            plan_new(self.repo, self.plane, "다시", "APP", TEMPLATES, date(2026, 11, 23))
        self.assertEqual(len(self.plane.calls), calls)

    def test_plan_new_rejects_invalid_key_before_plane_calls(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        calls = len(self.plane.calls)
        for bad_key in ("", "app", "APP-1", "ABCDEFGHIJKLM"):
            with self.assertRaises(FlowError):
                plan_new(self.repo, self.plane, "잘못된 키", bad_key, TEMPLATES, date(2026, 11, 23))
        self.assertEqual(len(self.plane.calls), calls)

    def test_put_get_and_secret(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        updated_at = plan_put(self.repo, self.plane, "APP", "PRD9", "<ul><li>모바일 앱</li></ul>")
        self.assertTrue(updated_at)
        self.assertIn("모바일 앱", plan_get(self.repo, self.plane, "APP", "PRD9"))
        with self.assertRaises(SecretFound):
            plan_put(self.repo, self.plane, "APP", "PRD3", "<p>10.1.2.3</p>")
        with self.assertRaisesRegex(FlowError, "저장소 문서"):
            plan_put(self.repo, self.plane, "APP", "IMPL1", "<p>x</p>")

    def test_put_conflict_when_changed_between_read_and_write(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        self.plane.touch_on_next_get("APP", "[계획서] PRD")
        with self.assertRaises(PlaneConflict):
            plan_put(self.repo, self.plane, "APP", "PRD9", "<ul><li>x</li></ul>")

    def test_decide_star_goes_to_default_project(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        e = decide(self.repo, self.plane, "로그 보관 180일", "*", "<p>근거</p>", date(2026, 12, 8))
        self.assertRegex(e.ref_id, r"^D-261208-[0-9a-f]{4}$")
        self.assertEqual(e.projects, "*")
        self.assertRegex(e.detail, r"^plane:SAMPM-\d+@")
        self.assertIn(e.ref_id, split_html_sections(self.plane.item_html("SAMPM", "[계획서] 결정")))

    def test_decide_for_specific_project_and_secret_rejection(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        e = decide(self.repo, self.plane, "모듈 분리", "APP", "<p>선택 이유</p>", date(2026, 12, 9))
        self.assertEqual(e.projects, "APP")
        self.assertRegex(e.detail, r"^plane:APP-\d+@")
        self.assertIn(e.ref_id, split_html_sections(self.plane.item_html("APP", "[계획서] 결정")))

        with self.assertRaises(SecretFound):
            decide(self.repo, self.plane, "토큰", "APP", "<p>10.20.30.40</p>", date(2026, 12, 9))

    def test_plane_doc_source_and_unavailable_conversion(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        cfg = load_config(self.repo.root / ".ledger/ledger.yml")
        src = PlaneDocSource(self.plane, cfg)

        html = src.get_container_html("APP", "PRD")
        self.assertIsNotNone(html)
        self.assertIn("PRD 1 문서 머리", html or "")
        self.assertIsNone(src.get_container_html("NOPE", "PRD"))

        rep = run_check(self.repo.root, ["APP"], src)
        self.assertEqual(rep.exit_code, 1)

        self.plane.fail_on["get_item"] = PlaneUnavailable("timeout")
        with self.assertRaises(SourceUnavailable):
            PlaneDocSource(self.plane, cfg).get_container_html("APP", "PRD")
        self.assertEqual(run_check(self.repo.root, ["APP"], PlaneDocSource(self.plane, cfg)).exit_code, 2)

        self.plane.fail_on.clear()
        self.plane.fail_on["list_projects"] = PlaneAuthError("unauthorized")
        with self.assertRaises(SourceUnavailable):
            PlaneDocSource(self.plane, cfg).get_container_html("APP", "PRD")

    def test_init_repo_rejects_already_initialized(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        ledger_path = self.repo.root / ".ledger/ledger.yml"
        before_text = ledger_path.read_text(encoding="utf-8")
        calls_before = len(self.plane.calls)

        with self.assertRaisesRegex(FlowError, r"이미 초기화된 저장소입니다: \.ledger/ledger\.yml"):
            init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))

        self.assertEqual(len(self.plane.calls), calls_before)
        self.assertEqual(ledger_path.read_text(encoding="utf-8"), before_text)

    def test_create_flows_reject_secrets_before_plane_calls(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        calls_before = len(self.plane.calls)

        with self.assertRaises(SecretFound):
            plan_new(self.repo, self.plane, "제목 plane_api_" + "a" * 32, "SEC1", TEMPLATES, date(2026, 11, 23))
        with self.assertRaises(SecretFound):
            plan_new(self.repo, self.plane, "서버 10.1.2.3 연동", "SEC2", TEMPLATES, date(2026, 11, 23))
        self.assertEqual(len(self.plane.calls), calls_before)

        ip_dir = Path(self._tmp.name) / "app-10.1.2.3"
        ip_dir.mkdir()
        ip_repo = Repo(root=ip_dir, name=ip_dir.name)
        plane_ip = FakePlane()
        with self.assertRaises(SecretFound):
            init_repo(ip_repo, plane_ip, "forge", TEMPLATES, date(2026, 11, 23))
        self.assertEqual(len(plane_ip.calls), 0)

        ws_dir = Path(self._tmp.name) / "clean-ws-app"
        ws_dir.mkdir()
        ws_repo = Repo(root=ws_dir, name=ws_dir.name)
        plane_ws = FakePlane()
        with self.assertRaises(SecretFound):
            init_repo(ws_repo, plane_ws, "ws-10.2.3.4", TEMPLATES, date(2026, 11, 23))
        self.assertEqual(len(plane_ws.calls), 0)

    def test_init_repo_rolls_back_project_on_failure(self) -> None:
        self.plane.fail_on["create_label"] = PlaneUnavailable("label fail")
        with self.assertRaises(PlaneUnavailable):
            init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        self.plane.fail_on.clear()
        self.assertEqual(self.plane.list_projects(), [])

        plane_del_fail = FakePlane()
        plane_del_fail.fail_on["create_label"] = PlaneUnavailable("label fail")
        plane_del_fail.fail_on["delete_project"] = PlaneUnavailable("delete fail")
        with self.assertRaises(PlaneUnavailable) as ctx:
            init_repo(self.repo, plane_del_fail, "forge", TEMPLATES, date(2026, 11, 23))
        self.assertTrue(
            str(ctx.exception).endswith(" (정리 실패: Plane 프로젝트 SAMPM 수동 삭제 필요)"),
            str(ctx.exception),
        )

    def test_plan_new_rolls_back_only_new_project_on_failure(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))

        self.plane.fail_on["create_label"] = PlaneAuthError("Plane 토큰 확인 필요")
        with self.assertRaises(PlaneAuthError):
            plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        self.plane.fail_on.clear()
        self.assertEqual([p["identifier"] for p in self.plane.list_projects()], ["SAMPM"])

        with patch("ledger.flows.save_config", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                plan_new(self.repo, self.plane, "저장 실패", "DISK", TEMPLATES, date(2026, 11, 23))
        self.assertEqual([p["identifier"] for p in self.plane.list_projects()], ["SAMPM"])

        self.plane.fail_on["create_label"] = PlaneUnavailable("label fail")
        self.plane.fail_on["delete_project"] = PlaneUnavailable("delete fail")
        with self.assertRaises(PlaneUnavailable) as ctx:
            plan_new(self.repo, self.plane, "정리 실패", "APP2", TEMPLATES, date(2026, 11, 23))
        self.assertTrue(
            str(ctx.exception).endswith(" (정리 실패: Plane 프로젝트 APP2 수동 삭제 필요)"),
            str(ctx.exception),
        )

    def test_plan_new_rejects_trailing_newline_key_before_plane_calls(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        calls_before = len(self.plane.calls)
        with self.assertRaises(FlowError):
            plan_new(self.repo, self.plane, "개행 키", "APP\n", TEMPLATES, date(2026, 11, 23))
        self.assertEqual(len(self.plane.calls), calls_before)

    def test_plane_doc_source_sets_source_unavailable_kind(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
        cfg = load_config(self.repo.root / ".ledger/ledger.yml")

        self.plane.fail_on["list_projects"] = PlaneAuthError("unauthorized")
        with self.assertRaises(SourceUnavailable) as auth_ctx:
            PlaneDocSource(self.plane, cfg).get_container_html("APP", "PRD")
        self.assertEqual(auth_ctx.exception.kind, "auth")

        self.plane.fail_on.clear()
        self.plane.fail_on["get_item"] = PlaneUnavailable("timeout")
        with self.assertRaises(SourceUnavailable) as unavail_ctx:
            PlaneDocSource(self.plane, cfg).get_container_html("APP", "PRD")
        self.assertEqual(unavail_ctx.exception.kind, "unavailable")

    def test_plan_new_history_failure_restores_ledger_yml(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
        ledger_path = self.repo.root / ".ledger/ledger.yml"
        history_path = self.repo.root / ".ledger/history.md"
        ledger_before = ledger_path.read_bytes()
        history_before = history_path.read_bytes()

        with patch("ledger.flows.append_entry", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))

        self.assertEqual(ledger_path.read_bytes(), ledger_before)
        self.assertEqual(history_path.read_bytes(), history_before)
        self.assertEqual([p["identifier"] for p in self.plane.list_projects()], ["SAMPM"])

    def test_plan_new_reports_when_ledger_restore_fails(self) -> None:
        init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))

        with patch("ledger.flows.append_entry", side_effect=OSError("disk full")):
            with patch.object(Path, "write_bytes", side_effect=OSError("read-only")):
                with self.assertRaises(OSError) as ctx:
                    plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))

        self.assertIn("ledger.yml 복원 실패", str(ctx.exception))
        self.assertIn("APP", str(ctx.exception))
        self.assertEqual([p["identifier"] for p in self.plane.list_projects()], ["SAMPM"])


if __name__ == "__main__":
    unittest.main()
