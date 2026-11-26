"""`.ledger/facts.md`의 사실 표를 읽고 행 단위로 고친다. 표 밖의 글은 건드리지 않는다."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Optional

_COLUMNS = ("ID", "상태", "생성", "내용", "출처")
_HEADER = f"| {' | '.join(_COLUMNS)} |"
_SEPARATOR = f"|{' --- |' * len(_COLUMNS)}"
_SEPARATOR_CELL_RE = re.compile(r":?-+:?")
_ID_RE = re.compile(r"F([0-9]+)")


@dataclass(frozen=True)
class Fact:
    fact_id: str
    status: str
    created: str
    text: str
    ref: str


def normalize_fact(fact: Fact) -> Fact:
    """표에 적었다가 다시 읽었을 때의 모양: 줄 끝은 `\\n`, 앞뒤 공백은 없다."""
    return Fact(*(value.replace("\r\n", "\n").replace("\r", "\n").strip() for value in astuple(fact)))


def _encode(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("|", "\\|").replace("<br>", "\\<br>")
    return escaped.replace("\n", "<br>")


def _decode(cell: str) -> str:
    text = cell.strip()
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and text[index + 1 : index + 2] in ("\\", "|", "<"):
            out.append(text[index + 1])
            index += 2
        elif text.startswith("<br>", index):
            out.append("\n")
            index += 4
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def format_row(fact: Fact) -> str:
    return f"| {' | '.join(_encode(value) for value in astuple(normalize_fact(fact)))} |"


def _cells(line: str) -> list[str]:
    """`|`로 시작하고 끝나는 표 줄의 칸. 그런 줄이 아니면 빈 목록이다."""
    text = line.strip()
    if not text.startswith("|"):
        return []
    cells: list[str] = []
    current = ""
    index = 1
    while index < len(text):
        if text[index] == "\\":
            current += text[index : index + 2]
            index += 2
            continue
        if text[index] == "|":
            cells.append(current)
            current = ""
        else:
            current += text[index]
        index += 1
    return [] if current.strip() else cells


def _find_table(lines: Sequence[str]) -> Optional[tuple[int, list[tuple[int, Fact]]]]:
    """사실 표를 찾아 (표 다음 줄 번호, [(줄 번호, 사실)])을 낸다.

    깨진 행은 건너뛰지 않고 `ValueError`다. 건너뛰면 없는 사실로 보고 같은 ID를 한 번 더 붙이게 된다.
    """
    for start, line in enumerate(lines):
        if tuple(cell.strip() for cell in _cells(line)) != _COLUMNS:
            continue
        separator = _cells(lines[start + 1]) if start + 1 < len(lines) else []
        if len(separator) != len(_COLUMNS) or not all(_SEPARATOR_CELL_RE.fullmatch(c.strip()) for c in separator):
            raise ValueError(f"사실 표의 {start + 2}번째 줄 형식 오류: 구분 줄이 아님")
        rows: list[tuple[int, Fact]] = []
        end = start + 2
        while end < len(lines) and lines[end].strip().startswith("|"):
            cells = _cells(lines[end])
            if len(cells) != len(_COLUMNS):
                raise ValueError(f"사실 표의 {end + 1}번째 줄 형식 오류: 칸이 {len(_COLUMNS)}개가 아님")
            rows.append((end, normalize_fact(Fact(*(_decode(cell) for cell in cells)))))
            end += 1
        return end, rows
    return None


def parse_facts(text: str) -> list[Fact]:
    table = _find_table(text.splitlines())
    return [] if table is None else [fact for _, fact in table[1]]


def upsert_fact(path: Path, fact: Fact) -> str:
    """ID가 같은 행을 고치거나 표 끝에 행을 더한다. `created`·`updated`·`same` 중 하나를 돌려준다."""
    wanted = normalize_fact(fact)
    row = format_row(wanted)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"# 사실\n\n{_HEADER}\n{_SEPARATOR}\n{row}\n".encode())
        return "created"

    text = path.read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    table = _find_table([line.rstrip("\r\n") for line in lines])
    if table is None:
        gap = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        path.write_bytes(f"{text}{gap}{_HEADER}\n{_SEPARATOR}\n{row}\n".encode())
        return "created"

    end, rows = table
    matches = [(number, current) for number, current in rows if current.fact_id == wanted.fact_id]
    if len(matches) > 1:
        raise ValueError(f"사실 표에 중복된 ID가 있음: {wanted.fact_id}")
    if matches:
        number, current = matches[0]
        if current == wanted:
            return "same"
        ending = lines[number][len(lines[number].rstrip("\r\n")) :]
        lines[number] = row + ending
        result = "updated"
    else:
        if not lines[end - 1].endswith("\n"):
            lines[end - 1] += "\n"
        lines.insert(end, f"{row}\n")
        result = "created"
    path.write_bytes("".join(lines).encode("utf-8"))
    return result


def next_fact_id(facts: Sequence[Fact]) -> str:
    numbers = [int(match.group(1)) for match in (_ID_RE.fullmatch(fact.fact_id) for fact in facts) if match]
    return f"F{max(numbers, default=0) + 1}"
