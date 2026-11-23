from __future__ import annotations

import html as html_lib
import re

from ledger.spec import DOC_BY_CODE

_TAG_RE = re.compile(r"<[^>]+>")
_H2_RE = re.compile(r"<h2(?:\s[^>]*)?>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_MD_H2_RE = re.compile(r"^##[ \t]+([^\r\n]+)$", re.MULTILINE)
_DOC_HEADING_RE = re.compile(r"^(PRD|구현)\s+(\d)(?!\d)")
_DECISION_HEADING_RE = re.compile(r"^(D-\d{6}-[0-9a-f]{4})\b")
_NA_RE = re.compile(r"^해당 없음:\s*(.*)$", re.DOTALL)
_TR_RE = re.compile(r"<tr(?:\s[^>]*)?>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<(t[hd])(?:\s[^>]*)?>(.*?)</\1>", re.IGNORECASE | re.DOTALL)


def _strip_tags(raw: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", raw)).strip()


def _heading_key(raw_heading: str, *, is_html: bool = False) -> str | None:
    text = _strip_tags(raw_heading) if is_html else raw_heading.strip()
    doc_match = _DOC_HEADING_RE.match(text)
    if doc_match is not None:
        prefix = "PRD" if doc_match.group(1) == "PRD" else "IMPL"
        code = f"{prefix}{doc_match.group(2)}"
        return code if code in DOC_BY_CODE else None
    decision_match = _DECISION_HEADING_RE.match(text)
    if decision_match is not None:
        return decision_match.group(1)
    return None


def _iter_html_sections(html: str) -> list[tuple[str | None, int, int]]:
    matches = list(_H2_RE.finditer(html))
    result: list[tuple[str | None, int, int]] = []
    for idx, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(html)
        result.append((_heading_key(match.group(1), is_html=True), body_start, body_end))
    return result


def split_html_sections(html: str) -> dict[str, str]:
    """HTML 문서에서 h2 절을 문서 코드 또는 결정 ID 키로 나눈다."""
    return {key: html[start:end] for key, start, end in _iter_html_sections(html) if key is not None}


def split_md_sections(md: str) -> dict[str, str]:
    """Markdown 문서에서 '## ' 절을 문서 코드 또는 결정 ID 키로 나눈다."""
    matches = list(_MD_H2_RE.finditer(md))
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        key = _heading_key(match.group(1), is_html=False)
        if key is None:
            continue
        body_start = match.end()
        if md.startswith("\r\n", body_start):
            body_start += 2
        elif md.startswith("\n", body_start):
            body_start += 1
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
        sections[key] = md[body_start:body_end]
    return sections


def replace_html_section(html: str, code: str, body: str) -> str:
    """HTML에서 지정한 코드의 h2 절 본문을 교체한다. 없으면 KeyError."""
    for key, start, end in _iter_html_sections(html):
        if key == code:
            return f"{html[:start]}{body}{html[end:]}"
    raise KeyError(code)


def append_html_section(html: str, heading: str, body: str) -> str:
    """HTML 끝에 새 h2 절을 덧붙인다."""
    escaped = html_lib.escape(heading, quote=False)
    return f"{html}<h2>{escaped}</h2>{body}"


def section_state(body: str, na_allowed: bool) -> tuple[str, list[str]]:
    """절 본문의 작성 상태(written|na|missing)와 미충족 사유 목록을 돌려준다."""
    text = _strip_tags(body)
    if not text:
        return ("missing", ["비어 있음"])
    if "{{" in text:
        return ("missing", ["자리표시자 남음"])
    na_match = _NA_RE.match(text)
    if na_match is not None:
        reason = na_match.group(1).strip()
        if not reason:
            return ("missing", ["이유 없음"])
        if not na_allowed:
            return ("missing", ["해당 없음 불가"])
        return ("na", [])
    return ("written", [])


def html_table_rows(body: str) -> list[list[str]]:
    """HTML 표에서 th만 있는 머리 행을 빼고 셀 텍스트 행 목록을 돌려준다."""
    rows: list[list[str]] = []
    for tr_match in _TR_RE.finditer(body):
        cells = list(_CELL_RE.finditer(tr_match.group(1)))
        if not cells:
            continue
        if all(cell.group(1).lower() == "th" for cell in cells):
            continue
        rows.append([_strip_tags(cell.group(2)) for cell in cells])
    return rows
