from __future__ import annotations

import unittest
from pathlib import Path

from ledger.spec import (
    DOC_BY_CODE,
    DOCS,
    DRAWIO,
    FEATURE_PRD_REQUIRED,
    METHOD_VERSION,
    REPO_FILES,
    UNIT_ITEMS,
    render_docs_reference,
    section_heading,
)

ROOT = Path(__file__).resolve().parent.parent
REF_DOCS = ROOT / "plugins" / "forge-ledger" / "skills" / "ledger-plan" / "references" / "docs.md"


class SpecTest(unittest.TestCase):
    def test_eighteen_docs_in_order(self) -> None:
        self.assertEqual(
            [d.code for d in DOCS],
            [f"PRD{i}" for i in range(1, 10)] + [f"IMPL{i}" for i in range(1, 10)],
        )
        self.assertEqual(METHOD_VERSION, 1)
        self.assertEqual(set(DOC_BY_CODE), {d.code for d in DOCS})
        self.assertEqual(FEATURE_PRD_REQUIRED, ("PRD1", "PRD2", "PRD6", "PRD8", "PRD9"))

    def test_scopes_and_na(self) -> None:
        unit = {d.code for d in DOCS if d.scope == "unit"}
        self.assertEqual(unit, {f"PRD{i}" for i in range(1, 10)} | {"IMPL2", "IMPL3", "IMPL5", "IMPL7"})
        repo = {d.code for d in DOCS if d.scope == "repo"}
        self.assertEqual(repo, {"IMPL1", "IMPL4", "IMPL6", "IMPL8", "IMPL9"})
        na = {d.code for d in DOCS if d.na_allowed}
        self.assertEqual(na, {"PRD7", "PRD8", "IMPL1", "IMPL2", "IMPL3", "IMPL4", "IMPL5", "IMPL8", "IMPL9"})

    def test_containers(self) -> None:
        self.assertEqual(
            UNIT_ITEMS,
            {
                "PRD": "[계획서] PRD",
                "IMPL2": "[계획서] 구현 2 기능 레퍼런스",
                "IMPL3": "[계획서] 구현 3 화면 레퍼런스",
                "IMPL5": "[계획서] 구현 5 알림 처리",
                "IMPL7": "[계획서] 구현 7 단계별 범위",
                "DECISION": "[계획서] 결정",
            },
        )
        self.assertEqual(REPO_FILES["IMPL6"], ".ledger/design/security.md")
        self.assertEqual(
            DRAWIO,
            {
                "IMPL1": ".ledger/design/data-model.drawio",
                "IMPL4": ".ledger/design/logging-flow.drawio",
            },
        )
        for i in range(1, 10):
            self.assertEqual(DOC_BY_CODE[f"PRD{i}"].container, "PRD")
        for code in ("IMPL2", "IMPL3", "IMPL5", "IMPL7"):
            self.assertEqual(DOC_BY_CODE[code].container, code)
        for code, path in REPO_FILES.items():
            self.assertEqual(DOC_BY_CODE[code].container, path)

    def test_headings(self) -> None:
        self.assertEqual(section_heading("PRD2"), "PRD 2 목표 · 성공 지표")
        self.assertEqual(section_heading("IMPL6"), "구현 6 인증 · 보안 처리")
        with self.assertRaises(KeyError):
            section_heading("UNKNOWN")

    def test_reference_in_sync(self) -> None:
        rendered = render_docs_reference()
        lines = [line for line in rendered.splitlines() if line.startswith("|")]
        self.assertEqual(len(lines), 20)
        self.assertIn("문서 | 담는 것 | 첫 질문 | 해당 없음 | 위치 | 검사", lines[0])
        self.assertEqual(REF_DOCS.read_text(encoding="utf-8"), rendered)
