from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ledger.facts import Fact, next_fact_id, parse_facts, upsert_fact

_HEADER = "| ID | 상태 | 생성 | 내용 | 출처 |"
_SEPARATOR = "| --- | --- | --- | --- | --- |"


class FactsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".ledger" / "facts.md"

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_creates_file_with_table(self) -> None:
        self.assertEqual(upsert_fact(self.path, Fact("F1", "Active", "2026-11-23", "사실 하나", "PR 1")), "created")
        self.assertEqual(
            self.read(), f"# 사실\n\n{_HEADER}\n{_SEPARATOR}\n| F1 | Active | 2026-11-23 | 사실 하나 | PR 1 |\n"
        )

    def test_same_updated_and_appended_rows(self) -> None:
        first = Fact("F1", "Active", "2026-11-23", "사실 하나", "")
        upsert_fact(self.path, first)
        before = self.read()
        self.assertEqual(upsert_fact(self.path, first), "same")
        self.assertEqual(self.read(), before)
        self.assertEqual(upsert_fact(self.path, Fact("F2", "Active", "2026-11-24", "둘", "r")), "created")
        self.assertEqual(upsert_fact(self.path, Fact("F1", "폐기", "2026-11-23", "사실 하나", "")), "updated")
        self.assertEqual([(f.fact_id, f.status) for f in parse_facts(self.read())], [("F1", "폐기"), ("F2", "Active")])

    def test_special_characters_round_trip_in_one_line(self) -> None:
        for text in (
            "a | b",
            "첫 줄\n둘째 줄",
            "글자 <br> 그대로",
            "역슬래시 \\ 와 \\| 조합",
            "끝 역슬래시 \\",
            "| 양끝 |",
            "\\<br>",
        ):
            with self.subTest(text=text):
                self.path.unlink(missing_ok=True)
                fact = Fact("F1", "Active", "2026-11-23", text, text)
                upsert_fact(self.path, fact)
                self.assertEqual(parse_facts(self.read()), [fact])
                self.assertEqual(len(self.read().splitlines()), 5)
                self.assertEqual(upsert_fact(self.path, fact), "same")

    def test_surrounding_space_and_crlf_do_not_count_as_changes(self) -> None:
        upsert_fact(self.path, Fact("F1", "Active", "2026-11-23", "  a\r\nb \n", " r "))
        self.assertEqual(parse_facts(self.read()), [Fact("F1", "Active", "2026-11-23", "a\nb", "r")])
        self.assertEqual(upsert_fact(self.path, Fact("F1", "Active", "2026-11-23", "a\nb", "r")), "same")

    def test_keeps_surrounding_text_and_hand_aligned_rows(self) -> None:
        self.write(
            "# 사실\n\n머리 설명\n\n"
            f"{_HEADER}\n|:---|---:|---|---|---|\n|  F1   | Active |2026-11-23|  하나 |  |\n\n꼬리 메모\n"
        )
        self.assertEqual(upsert_fact(self.path, Fact("F1", "Active", "2026-11-23", "하나", "")), "same")
        self.assertEqual(upsert_fact(self.path, Fact("F2", "Active", "2026-11-23", "둘", "")), "created")
        lines = self.read().splitlines()
        self.assertEqual(lines[2], "머리 설명")
        self.assertEqual(lines[6], "|  F1   | Active |2026-11-23|  하나 |  |")
        self.assertEqual(lines[7], "| F2 | Active | 2026-11-23 | 둘 |  |")
        self.assertEqual(lines[-1], "꼬리 메모")
        self.assertTrue(self.read().endswith("\n"))

    def test_file_without_table_gets_one(self) -> None:
        self.write("# 메모\n\n| 다른 | 표 |\n| --- | --- |\n| a | b |")
        self.assertEqual(upsert_fact(self.path, Fact("F1", "Active", "2026-11-23", "x", "")), "created")
        self.assertTrue(self.read().startswith("# 메모\n\n| 다른 | 표 |\n"))
        self.assertEqual([f.fact_id for f in parse_facts(self.read())], ["F1"])

    def test_broken_row_is_an_error_not_a_missing_fact(self) -> None:
        # 깨진 행을 건너뛰면 같은 ID가 한 번 더 붙는다.
        for row in ("| F1 | Active | 2026-11-23 | 따옴표 | 없는 | 파이프 |", "| F1 | Active |", "| F1 | a | b | c | d"):
            with self.subTest(row=row):
                text = f"{_HEADER}\n{_SEPARATOR}\n{row}\n"
                with self.assertRaisesRegex(ValueError, "3번째 줄"):
                    parse_facts(text)
                self.write(text)
                with self.assertRaises(ValueError):
                    upsert_fact(self.path, Fact("F1", "Active", "2026-11-23", "x", ""))
                self.assertEqual(self.read(), text)

    def test_duplicate_id_is_an_error(self) -> None:
        row = "| F1 | Active | 2026-11-23 | a | b |"
        self.write(f"{_HEADER}\n{_SEPARATOR}\n{row}\n{row}\n")
        with self.assertRaisesRegex(ValueError, "중복"):
            upsert_fact(self.path, Fact("F1", "Active", "2026-11-23", "x", ""))

    def test_parse_without_table_is_empty(self) -> None:
        self.assertEqual(parse_facts(""), [])
        self.assertEqual(parse_facts("# 사실\n"), [])

    def test_next_fact_id(self) -> None:
        self.assertEqual(next_fact_id([]), "F1")
        facts = [Fact(i, "Active", "2026-11-23", "x", "") for i in ("F2", "F10", "X99", "F3a")]
        self.assertEqual(next_fact_id(facts), "F11")


if __name__ == "__main__":
    unittest.main()
