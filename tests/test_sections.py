from __future__ import annotations

import unittest

from ledger.sections import (
    append_html_section,
    html_table_rows,
    replace_html_section,
    section_state,
    split_html_sections,
    split_md_sections,
)
from ledger.spec import DOC_BY_CODE, section_heading


class SectionsTest(unittest.TestCase):
    def test_split_and_replace(self) -> None:
        html = "<h2>PRD 1 문서 머리</h2><p>a</p><h2>PRD 2 목표 · 성공 지표</h2><p>b</p>"
        self.assertEqual(split_html_sections(html), {"PRD1": "<p>a</p>", "PRD2": "<p>b</p>"})
        out = replace_html_section(html, "PRD2", "<p>c</p>")
        self.assertEqual(split_html_sections(out)["PRD2"], "<p>c</p>")
        self.assertEqual(split_html_sections(out)["PRD1"], "<p>a</p>")

    def test_split_survives_plane_sanitizing(self) -> None:
        html = '<h2 class="editor-heading-block">PRD 9 제외 범위</h2><ul class="list"><li><p>x</p></li></ul>'
        self.assertIn("PRD9", split_html_sections(html))

    def test_decision_heading_key(self) -> None:
        html = append_html_section("", "D-261123-7f3a 위키는 생성 구역만", "<p>본문</p>")
        self.assertIn("D-261123-7f3a", split_html_sections(html))

    def test_states(self) -> None:
        self.assertEqual(section_state("<p>{{목표}}</p>", True)[0], "missing")
        self.assertEqual(section_state("", True)[0], "missing")
        self.assertEqual(section_state("<p>해당 없음: 화면 없는 일</p>", True), ("na", []))
        self.assertEqual(section_state("<p>해당 없음: </p>", True)[0], "missing")
        self.assertEqual(section_state("<p>해당 없음: 이유</p>", False)[0], "missing")
        self.assertEqual(section_state("<p>내용</p>", False), ("written", []))

    def test_md_sections(self) -> None:
        md = "# 데이터\n\n## 구현 1 데이터 보관 설계\n본문\n"
        self.assertEqual(split_md_sections(md)["IMPL1"].strip(), "본문")

    def test_replace_missing_raises_and_preserves_attributes(self) -> None:
        html = '<h2 class="editor-heading-block">PRD 2 목표</h2><p>old</p>'
        with self.assertRaises(KeyError):
            replace_html_section(html, "PRD9", "<p>new</p>")
        replaced = replace_html_section(html, "PRD2", "<p>new</p>")
        self.assertEqual(replaced, '<h2 class="editor-heading-block">PRD 2 목표</h2><p>new</p>')

    def test_inner_heading_tags_and_spec_integration(self) -> None:
        html = append_html_section(
            "<h2><span>PRD 1</span> 문서 머리</h2><p>a</p>", section_heading("PRD7"), "<p>해당 없음: 백엔드 전용</p>"
        )
        sections = split_html_sections(html)
        self.assertEqual(sections["PRD1"], "<p>a</p>")
        self.assertEqual(section_state(sections["PRD7"], DOC_BY_CODE["PRD7"].na_allowed), ("na", []))
        self.assertEqual(
            section_state(sections["PRD7"], DOC_BY_CODE["PRD1"].na_allowed), ("missing", ["해당 없음 불가"])
        )
        self.assertEqual(section_state("<p>해당 없음:   </p>", True), ("missing", ["이유 없음"]))
        self.assertEqual(section_state("<p>해당 없음: {{이유}}</p>", True)[0], "missing")

    def test_md_sections_multiple_and_subheadings(self) -> None:
        md = "# 로깅\n\n" "## 구현 4 로깅 처리 레퍼런스\n### 세부\n내용1\n" "## D-261123-7f3a 로그 포맷\n내용2\n"
        sections = split_md_sections(md)
        self.assertEqual(sections["IMPL4"].strip(), "### 세부\n내용1")
        self.assertEqual(sections["D-261123-7f3a"].strip(), "내용2")

    def test_html_table_rows(self) -> None:
        table = (
            "<table>"
            "<tr><th><p> 지표 </p></th><th>목표</th></tr>"
            "<tr><td><p> 응답시간 </p></td><td>200ms <span>이하</span></td></tr>"
            "<tr><th>행제목</th><td>값</td></tr>"
            "<tr></tr>"
            "</table>"
        )
        self.assertEqual(
            html_table_rows(table),
            [["응답시간", "200ms 이하"], ["행제목", "값"]],
        )
