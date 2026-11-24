from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ledger.check import SourceUnavailable, check_repo, check_unit, doc_rules, run_check
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
        self.assertNotIn("error", data)

    def test_run_check_detects_secrets_in_history_and_ledger_yml(self) -> None:
        src = DictSource({("APP", c): FILLED[c] for c in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")})
        history_path = self.filled_repo / ".ledger" / "history.md"
        history_path.write_text("# 이력\n| 2026-11-23 | decision | D-261123-0001 | APP | 서버 10.9.8.7 | detail |\n", encoding="utf-8")

        rep = run_check(self.filled_repo, ["APP"], src)
        raw_json = rep.to_json()
        self.assertEqual(rep.exit_code, 1)
        self.assertIn("비밀값: ipv4", raw_json)
        self.assertNotIn("10.9.8.7", raw_json)

        history_path.unlink()
        ledger_yml = self.filled_repo / ".ledger" / "ledger.yml"
        secret_tok = "plane_api_" + "b" * 32
        ledger_yml.write_text(f"method_version: 1\nplane:\n  workspace: {secret_tok}\n", encoding="utf-8")
        rep2 = run_check(self.filled_repo, ["APP"], src)
        raw_json2 = rep2.to_json()
        self.assertEqual(rep2.exit_code, 1)
        self.assertIn("비밀값: plane_token", raw_json2)
        self.assertNotIn(secret_tok, raw_json2)

    def test_run_check_detects_secrets_outside_sections_and_in_decision_and_drawio(self) -> None:
        docs = {("APP", c): FILLED[c] for c in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")}
        docs[("APP", "PRD")] = "<p>절 밖 10.1.2.3</p>" + docs[("APP", "PRD")]
        rep = run_check(self.filled_repo, ["APP"], DictSource(docs))
        raw = rep.to_json()
        self.assertEqual(rep.exit_code, 1)
        self.assertIn("비밀값: ipv4", raw)
        self.assertNotIn("10.1.2.3", raw)

        docs2 = {("APP", c): FILLED[c] for c in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")}
        docs2[("APP", "DECISION")] = "<h2>D-261123-0001 결정</h2><p>2001:db8::42</p>"
        rep2 = run_check(self.filled_repo, ["APP"], DictSource(docs2))
        raw2 = rep2.to_json()
        self.assertEqual(rep2.exit_code, 1)
        self.assertIn("비밀값: ipv6", raw2)
        self.assertNotIn("2001:db8::42", raw2)

        sec_path = self.filled_repo / ".ledger" / "design" / "security.md"
        orig_sec = sec_path.read_text(encoding="utf-8")
        sec_path.write_text("절 밖 메모 10.2.3.4\n\n" + orig_sec, encoding="utf-8")
        clean_src = DictSource({("APP", c): FILLED[c] for c in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")})
        rep3 = run_check(self.filled_repo, ["APP"], clean_src)
        raw3 = rep3.to_json()
        self.assertEqual(rep3.exit_code, 1)
        self.assertIn("비밀값: ipv4", raw3)
        self.assertNotIn("10.2.3.4", raw3)
        sec_path.write_text(orig_sec, encoding="utf-8")

        drawio_path = self.filled_repo / ".ledger" / "design" / "data-model.drawio"
        orig_drawio = drawio_path.read_text(encoding="utf-8")
        drawio_path.write_text(orig_drawio.replace("</mxfile>", "<!-- 10.3.4.5 --></mxfile>"), encoding="utf-8")
        rep4 = run_check(self.filled_repo, ["APP"], clean_src)
        raw4 = rep4.to_json()
        self.assertEqual(rep4.exit_code, 1)
        self.assertIn("비밀값: ipv4", raw4)
        self.assertNotIn("10.3.4.5", raw4)

    def test_md_threat_table_variants_and_multiple_tables(self) -> None:
        self.assertIn("위협 없음", doc_rules("IMPL6", "| 위협 | 대응 |\n|--|--|\n", None))
        self.assertIn("위협 없음", doc_rules("IMPL6", "| 위협 | 대응 |\n|:--|--:|\n", None))
        self.assertEqual(doc_rules("IMPL6", "| 위협 | 대응 |\n|--|--|\n| 유출 | 차단 |\n", None), [])

        multi = (
            "| 접근 경로 | 비고 |\n"
            "| --- | --- |\n"
            "| 사내망 | 위협 모니터링 필요 |\n\n"
            "| 위협 | 대응 |\n"
            "|--|--|\n"
            "| 토큰 유출 | 훅 차단 |\n"
        )
        self.assertEqual(doc_rules("IMPL6", multi, None), [])

        two_threat_tables = (
            "| 위협 | 대응 |\n"
            "|--|--|\n\n"
            "| 위협 | 대응 |\n"
            "| --- | --- |\n"
            "| 위조 요청 | 서명 검증 |\n"
        )
        self.assertEqual(doc_rules("IMPL6", two_threat_tables, None), [])

    def test_multiline_keyword_values(self) -> None:
        self.assertEqual(doc_rules("IMPL9", "### 되돌리기\n1. 이전 태그 재기동", None), [])
        self.assertIn("되돌리기 없음", doc_rules("IMPL9", "### 되돌리기\n\n### 버전 규칙\nSemVer", None))
        self.assertEqual(doc_rules("IMPL8", "- **결함 흐름**:\n  - 1단계: 로그 확인", None), [])
        self.assertIn("결함 흐름 없음", doc_rules("IMPL8", "- **결함 흐름**:\n- **정기 점검**: 주 1회", None))

    def test_html_table_multiple_tables_isolated(self) -> None:
        aux_plus_main = (
            "<table><tr><th>비고</th></tr><tr><td>참고</td></tr></table>"
            "<table><tr><th>목표</th><th>지표</th><th>확인 방법</th></tr>"
            "<tr><td>G1</td><td>x</td><td>테스트</td></tr></table>"
        )
        self.assertEqual(doc_rules("PRD2", aux_plus_main, None), [])

        two_targets_second_empty = (
            "<table><tr><th>목표</th><th>지표</th><th>확인 방법</th></tr>"
            "<tr><td>G1</td><td>x</td><td>테스트</td></tr></table>"
            "<table><tr><th>목표</th><th>지표</th><th>확인 방법</th></tr>"
            "<tr><td>G2</td><td>y</td><td></td></tr></table>"
        )
        self.assertIn("확인 방법 빈칸", doc_rules("PRD2", two_targets_second_empty, None))

        header_only = "<table><tr><th>목표</th><th>지표</th><th>확인 방법</th></tr></table>"
        self.assertIn("확인 방법 빈칸", doc_rules("PRD2", header_only, None))

    def test_unavailable_report_error_field(self) -> None:
        class _AuthSource:
            def get_container_html(self, project_key: str, container: str) -> str:
                raise SourceUnavailable("auth failed", kind="auth")

        rep_auth = run_check(self.filled_repo, ["APP"], _AuthSource())
        self.assertEqual(rep_auth.exit_code, 2)
        self.assertEqual(json.loads(rep_auth.to_json())["error"], "auth")

        rep_unavail = run_check(self.filled_repo, ["APP"], RaisingSource())
        self.assertEqual(rep_unavail.exit_code, 2)
        self.assertEqual(json.loads(rep_unavail.to_json())["error"], "unavailable")
