from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ledger.check import check_repo, check_unit, doc_rules, run_check
from tests.fakes import DictSource, RaisingSource

ROOT = Path(__file__).resolve().parent.parent
FILLED_DIR = ROOT / "tests" / "fixtures" / "filled"
LEDGER_TEMPLATES = ROOT / "plugins" / "forge-ledger" / "skills" / "ledger-plan" / "templates" / "ledger"

FILLED: dict[str, str] = {
    name: (FILLED_DIR / f"{name}.html").read_text(encoding="utf-8")
    for name in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")
}


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.filled_repo = tmp_path / "filled"
        shutil.copytree(FILLED_DIR / ".ledger", self.filled_repo / ".ledger")
        self.template_repo = tmp_path / "template"
        shutil.copytree(LEDGER_TEMPLATES, self.template_repo / ".ledger")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_filled_project_passes(self) -> None:
        src = DictSource({("APP", c): FILLED[c] for c in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")})
        rep = run_check(self.filled_repo, ["APP"], src)
        self.assertEqual(rep.exit_code, 0, rep.to_json())
        self.assertEqual(len(rep.results), 18)

    def test_template_fails(self) -> None:
        rep = run_check(self.template_repo, ["APP"], DictSource.from_templates("APP"))
        self.assertEqual(rep.exit_code, 1)
        self.assertTrue(all(r.state == "missing" for r in rep.results))

    def test_missing_container_is_missing(self) -> None:
        rep = run_check(self.filled_repo, ["APP"], DictSource({}))
        self.assertEqual({r.state for r in rep.results if r.scope == "APP"}, {"missing"})

    def test_rules(self) -> None:
        self.assertIn(
            "확인 방법 빈칸",
            doc_rules(
                "PRD2",
                "<table><tr><th>목표</th><th>지표</th><th>확인 방법</th></tr><tr><td>G1</td><td>x</td><td></td></tr></table>",
                None,
            ),
        )
        self.assertIn(
            "진행 상태 값",
            doc_rules(
                "PRD1",
                "<table><tr><td>결정권자</td><td>a</td></tr><tr><td>진행 상태</td><td>모름</td></tr><tr><td>목표 출시일</td><td>미정</td></tr></table>",
                None,
            ),
        )
        self.assertIn("제외 항목 없음", doc_rules("PRD9", "<p>없다</p>", None))
        self.assertIn("고르지 않은 안 없음", doc_rules("PRD4", "<p>A안</p>", None))

    def test_secret_fails(self) -> None:
        src = DictSource({("APP", "PRD"): FILLED["PRD"].replace("미정", "10.1.2.3")})
        rep = run_check(self.filled_repo, ["APP"], src)
        self.assertIn("비밀값: ipv4", rep.to_json())

    def test_unavailable_exit_2(self) -> None:
        rep = run_check(self.filled_repo, ["APP"], RaisingSource())
        self.assertEqual(rep.exit_code, 2)

    def test_additional_rules(self) -> None:
        self.assertIn(
            "필드 없음: 결정권자",
            doc_rules("PRD1", "<table><tr><td>진행 상태</td><td>초안</td></tr><tr><td>목표 출시일</td><td>미정</td></tr></table>", None),
        )
        self.assertIn(
            "구분 없음: 사업",
            doc_rules(
                "PRD5",
                "<table><tr><th>구분</th><th>가정</th></tr><tr><td>사용자</td><td>a</td></tr><tr><td>기술</td><td>b</td></tr></table>",
                None,
            ),
        )
        self.assertIn(
            "기본값 빈칸",
            doc_rules(
                "PRD8",
                "<table><tr><th>ID</th><th>질문</th><th>영향</th><th>기본값</th><th>필요 시점</th></tr><tr><td>Q1</td><td>q</td><td>i</td><td></td><td>P0</td></tr></table>",
                None,
            ),
        )
        self.assertEqual(doc_rules("PRD8", "<p>해당 없음: 미결 질문 없음</p>", None), [])
        self.assertIn("위협 없음", doc_rules("IMPL6", "- **외부 노출**: 사내망", None))
        self.assertIn(
            "완료 기준 빈칸",
            doc_rules(
                "IMPL7",
                "<table><tr><th>단계</th><th>만드는 것</th><th>완료 기준</th><th>갱신할 구현 문서</th></tr><tr><td>P0</td><td>설치</td><td></td><td>6</td></tr></table>",
                None,
            ),
        )
        self.assertIn("결함 흐름 없음", doc_rules("IMPL8", "- **정기 점검**: 주 1회", None))
        self.assertIn("되돌리기 없음", doc_rules("IMPL9", "- **배포 대상**: 허브", None))

    def test_drawio_parse_failure_and_missing_repo_file(self) -> None:
        bad_drawio = self.filled_repo / ".ledger" / "design" / "data-model.drawio"
        bad_drawio.write_text("<broken", encoding="utf-8")
        self.assertIn("drawio 파싱 실패", doc_rules("IMPL1", "본문", self.filled_repo))

        empty_repo = Path(self._tmp.name) / "empty"
        empty_repo.mkdir()
        results = check_repo(empty_repo)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r.state == "missing" for r in results))

    def test_report_json_shape_and_check_unit(self) -> None:
        src = DictSource({("APP", c): FILLED[c] for c in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")})
        unit_results = check_unit("APP", src)
        self.assertEqual(len(unit_results), 13)
        rep = run_check(self.filled_repo, ["APP"], src)
        data = json.loads(rep.to_json())
        self.assertEqual(data["method_version"], 1)
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(len(data["results"]), 18)
        self.assertEqual(set(data["results"][0].keys()), {"scope", "code", "state", "reasons"})
