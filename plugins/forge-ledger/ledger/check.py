from __future__ import annotations

import html as html_lib
import json
import re

# nosemgrep: opt.semgrep-rules.python.lang.security.use-defused-xml.use-defused-xml -- 표준 라이브러리 제약(로컬 drawio)
import xml.etree.ElementTree as ET  # nosec B405 # 표준 라이브러리 제약, 로컬 drawio만 파싱
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

from ledger.secrets import find_secrets
from ledger.sections import html_table_rows, section_state, split_html_sections, split_md_sections
from ledger.spec import DOC_BY_CODE, DOCS, DRAWIO, METHOD_VERSION, REPO_FILES, UNIT_ITEMS

_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_RE = re.compile(r"<table(?:\s[^>]*)?>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr(?:\s[^>]*)?>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th(?:\s[^>]*)?>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_LI_RE = re.compile(r"<li(?:\s[^>]*)?>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_MD_SEP_RE = re.compile(r"^(?:\|\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?|\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?)$")
_MD_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+")
_MD_LIST_RE = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])\s+")
_LEDGER_META_FILES: tuple[tuple[str, str], ...] = (
    ("history.md", ".ledger/history.md"),
    ("ledger.yml", ".ledger/ledger.yml"),
)

_PRD1_REQUIRED_FIELDS = ("결정권자", "진행 상태", "목표 출시일")
_PRD1_VALID_STATES = frozenset({"초안", "검토", "승인", "진행", "종료"})
_PRD5_CATEGORIES = ("사용자", "기술", "사업")

SourceErrorKind = Literal["auth", "unavailable"]


@dataclass
class DocResult:
    """단일 문서 검사 결과."""

    scope: str
    code: str
    state: str
    reasons: list[str]


@dataclass
class CheckReport:
    """전체 계획서 검사 보고."""

    results: list[DocResult]
    exit_code: int
    error: Optional[SourceErrorKind] = None

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "method_version": METHOD_VERSION,
            "exit_code": self.exit_code,
            "results": [
                {
                    "scope": r.scope,
                    "code": r.code,
                    "state": r.state,
                    "reasons": r.reasons,
                }
                for r in self.results
            ],
        }
        if self.error is not None:
            payload["error"] = self.error
        return json.dumps(payload, ensure_ascii=False)


class UnitDocSource(Protocol):
    """단위 프로젝트 계획서 컨테이너 HTML 원천."""

    def get_container_html(self, project_key: str, container: str) -> Optional[str]: ...


class SourceUnavailable(Exception):
    """문서 원천에 연결할 수 없거나 읽기에 실패했을 때 발생한다."""

    def __init__(self, message: str = "", kind: SourceErrorKind = "unavailable") -> None:
        super().__init__(message)
        self.kind: SourceErrorKind = kind


def _strip_tags(raw: str) -> str:
    return html_lib.unescape(_TAG_RE.sub("", raw)).strip()


def _header_col_index(table_html: str, header_name: str) -> tuple[bool, Optional[int]]:
    for tr_match in _TR_RE.finditer(table_html):
        ths = [_strip_tags(m.group(1)) for m in _TH_RE.finditer(tr_match.group(1))]
        if not ths:
            continue
        for idx, text in enumerate(ths):
            if header_name in text:
                return (True, idx)
        return (True, None)
    return (False, None)


def _table_col_empty(body: str, header_name: str, default_idx: int) -> bool:
    tables = [m.group(1) for m in _TABLE_RE.finditer(body)]
    if not tables:
        tables = [body]

    matched_tables: list[tuple[str, int]] = []
    headerless_tables: list[str] = []
    for table_html in tables:
        has_header, col_idx = _header_col_index(table_html, header_name)
        if has_header:
            if col_idx is not None:
                matched_tables.append((table_html, col_idx))
        else:
            headerless_tables.append(table_html)

    targets = matched_tables if matched_tables else [(t, default_idx) for t in headerless_tables]
    if not targets:
        return True

    for table_html, col_idx in targets:
        rows = html_table_rows(table_html)
        if not rows:
            return True
        if any(len(row) <= col_idx or not row[col_idx].strip() for row in rows):
            return True
    return False


def _has_keyword_value(body: str, keyword: str) -> bool:
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if keyword not in line:
            continue
        after = line.split(keyword, 1)[1].lstrip("*_`:： \t").rstrip("# \t")
        if after:
            return True

        heading_match = _MD_HEADING_RE.match(line)
        if heading_match is not None:
            heading_level = len(heading_match.group(1))
            for next_line in lines[idx + 1 :]:
                if not next_line.strip():
                    continue
                next_heading = _MD_HEADING_RE.match(next_line)
                if next_heading is not None and len(next_heading.group(1)) <= heading_level:
                    break
                return True
            continue

        list_match = _MD_LIST_RE.match(line)
        if list_match is not None:
            base_indent = len(list_match.group(1).expandtabs(4))
            for next_line in lines[idx + 1 :]:
                if not next_line.strip():
                    continue
                if _MD_HEADING_RE.match(next_line) is not None:
                    break
                expanded = next_line.expandtabs(4)
                next_indent = len(expanded) - len(expanded.lstrip(" "))
                if next_indent <= base_indent:
                    break
                return True
            continue

        for next_line in lines[idx + 1 :]:
            if not next_line.strip():
                continue
            if _MD_HEADING_RE.match(next_line) is not None:
                break
            return True
    return False


def _has_md_threat(body: str) -> bool:
    lines = body.splitlines()
    found_threat_table = False
    for idx, line in enumerate(lines):
        if "|" in line and "위협" in line and idx + 1 < len(lines) and _MD_SEP_RE.match(lines[idx + 1].strip()):
            found_threat_table = True
            for row_line in lines[idx + 2 :]:
                stripped = row_line.strip()
                if not stripped or "|" not in stripped:
                    break
                if _MD_SEP_RE.match(stripped):
                    continue
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if any(cells):
                    return True
    if found_threat_table:
        return False
    return _has_keyword_value(body, "위협")


def _secret_reasons(body: str) -> list[str]:
    found = find_secrets(body)
    if not found:
        return []
    return [f"비밀값: {', '.join(found)}"]


def _extract_secret_names(reasons: list[str]) -> set[str]:
    names: set[str] = set()
    for reason in reasons:
        if reason.startswith("비밀값: "):
            names.update(part.strip() for part in reason[len("비밀값: ") :].split(",") if part.strip())
    return names


def _merge_secret_reasons(reasons: list[str], extra_names: list[str]) -> list[str]:
    if not extra_names:
        return reasons
    merged = _extract_secret_names(reasons) | set(extra_names)
    other = [r for r in reasons if not r.startswith("비밀값: ")]
    return [f"비밀값: {', '.join(sorted(merged))}", *other]


def doc_rules(code: str, body: str, repo_root: Optional[Path]) -> list[str]:
    """문서 코드별 형식 규칙과 비밀값 규칙을 검사해 실패 사유 목록을 돌려준다."""
    reasons = _secret_reasons(body)

    spec = DOC_BY_CODE.get(code)
    if spec is not None and spec.na_allowed and section_state(body, True)[0] == "na":
        return reasons

    if code == "PRD1":
        rows = html_table_rows(body)
        fields = {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
        for name in _PRD1_REQUIRED_FIELDS:
            if not fields.get(name):
                reasons.append(f"필드 없음: {name}")
        status = fields.get("진행 상태", "")
        if status and status not in _PRD1_VALID_STATES:
            reasons.append("진행 상태 값")
    elif code == "PRD2":
        if _table_col_empty(body, "확인 방법", 2):
            reasons.append("확인 방법 빈칸")
    elif code == "PRD4":
        text = _strip_tags(body)
        if "고르지 않은 안" not in text:
            reasons.append("고르지 않은 안 없음")
        else:
            after = text.split("고르지 않은 안", 1)[1].lstrip(":： \t\r\n")
            if not after:
                reasons.append("고르지 않은 안 없음")
    elif code == "PRD5":
        rows = html_table_rows(body)
        categories = {row[0].strip() for row in rows if row}
        for cat in _PRD5_CATEGORIES:
            if cat not in categories:
                reasons.append(f"구분 없음: {cat}")
    elif code == "PRD8":
        if _table_col_empty(body, "기본값", 3):
            reasons.append("기본값 빈칸")
    elif code == "PRD9":
        items = [_strip_tags(m.group(1)) for m in _LI_RE.finditer(body)]
        if not any(items):
            reasons.append("제외 항목 없음")
    elif code in ("IMPL1", "IMPL4"):
        if repo_root is not None and code in DRAWIO:
            drawio_path = repo_root / DRAWIO[code]
            try:
                # nosemgrep: opt.semgrep-rules.python.lang.security.use-defused-xml-parse.use-defused-xml-parse -- 표준 라이브러리 제약(로컬 drawio)
                ET.parse(drawio_path)  # nosec B314 # 표준 라이브러리 제약, 로컬 drawio만 파싱
            except (OSError, ET.ParseError):
                reasons.append("drawio 파싱 실패")
    elif code == "IMPL6":
        if not _has_md_threat(body):
            reasons.append("위협 없음")
    elif code == "IMPL7":
        if _table_col_empty(body, "완료 기준", 2):
            reasons.append("완료 기준 빈칸")
    elif code == "IMPL8":
        if not _has_keyword_value(body, "결함 흐름"):
            reasons.append("결함 흐름 없음")
    elif code == "IMPL9":
        if not _has_keyword_value(body, "되돌리기"):
            reasons.append("되돌리기 없음")

    return reasons


def check_unit(project_key: str, source: UnitDocSource) -> list[DocResult]:
    """단위 프로젝트 범위 문서 13종과 컨테이너 원문 전체의 비밀값을 검사한다."""
    container_raw: dict[str, Optional[str]] = {}
    container_sections: dict[str, Optional[dict[str, str]]] = {}
    reported_secrets: dict[str, set[str]] = {}
    results: list[DocResult] = []

    for doc in DOCS:
        if doc.scope != "unit":
            continue
        if doc.container not in container_sections:
            html = source.get_container_html(project_key, doc.container)
            container_raw[doc.container] = html
            container_sections[doc.container] = None if html is None else split_html_sections(html)

        sections = container_sections[doc.container]
        if sections is None:
            results.append(DocResult(scope=project_key, code=doc.code, state="missing", reasons=["문서 없음"]))
            continue

        body = sections.get(doc.code)
        if body is None:
            results.append(DocResult(scope=project_key, code=doc.code, state="missing", reasons=["절 없음"]))
            continue

        state, state_reasons = section_state(body, doc.na_allowed)
        if state == "written":
            reasons = doc_rules(doc.code, body, None)
        else:
            reasons = [*state_reasons, *_secret_reasons(body)]
        reported_secrets.setdefault(doc.container, set()).update(_extract_secret_names(reasons))
        results.append(DocResult(scope=project_key, code=doc.code, state=state, reasons=reasons))

    for container, raw_html in container_raw.items():
        if raw_html is None:
            continue
        extra = sorted(set(find_secrets(raw_html)) - reported_secrets.get(container, set()))
        if extra:
            results.append(
                DocResult(scope=project_key, code=container, state="written", reasons=[f"비밀값: {', '.join(extra)}"])
            )

    for container in UNIT_ITEMS:
        if container in container_raw:
            continue
        extra_html = source.get_container_html(project_key, container)
        if extra_html is None:
            continue
        found = find_secrets(extra_html)
        if found:
            results.append(
                DocResult(scope=project_key, code=container, state="written", reasons=[f"비밀값: {', '.join(found)}"])
            )

    return results


def check_repo(repo_root: Path) -> list[DocResult]:
    """저장소 범위 문서 5종(원문 전체 포함)과 drawio 파일을 검사한다."""
    results: list[DocResult] = []

    for doc in DOCS:
        if doc.scope != "repo":
            continue
        path = repo_root / REPO_FILES[doc.code]
        if not path.is_file():
            results.append(DocResult(scope="repo", code=doc.code, state="missing", reasons=["파일 없음"]))
            continue

        md = path.read_text(encoding="utf-8")
        extra_secrets: set[str] = set(find_secrets(md))
        if doc.code in DRAWIO:
            drawio_path = repo_root / DRAWIO[doc.code]
            if drawio_path.is_file():
                try:
                    extra_secrets.update(find_secrets(drawio_path.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    pass

        sections = split_md_sections(md)
        body = sections.get(doc.code)
        if body is None:
            reasons = _merge_secret_reasons(["절 없음"], sorted(extra_secrets))
            results.append(DocResult(scope="repo", code=doc.code, state="missing", reasons=reasons))
            continue

        state, state_reasons = section_state(body, doc.na_allowed)
        if state == "written":
            reasons = doc_rules(doc.code, body, repo_root)
        else:
            reasons = [*state_reasons, *_secret_reasons(body)]
        reasons = _merge_secret_reasons(reasons, sorted(extra_secrets))
        results.append(DocResult(scope="repo", code=doc.code, state=state, reasons=reasons))

    return results


def _check_ledger_meta_secrets(repo_root: Path) -> list[DocResult]:
    results: list[DocResult] = []
    for code, rel_path in _LEDGER_META_FILES:
        path = repo_root / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = find_secrets(text)
        if found:
            results.append(DocResult(scope="repo", code=code, state="written", reasons=[f"비밀값: {', '.join(found)}"]))
    return results


def run_check(repo_root: Path, project_keys: list[str], source: UnitDocSource) -> CheckReport:
    """지정한 프로젝트들과 저장소 문서 및 .ledger 메타 파일을 모두 검사해 보고서를 만든다."""
    results: list[DocResult] = []
    try:
        for key in project_keys:
            results.extend(check_unit(key, source))
    except SourceUnavailable as exc:
        return CheckReport(results=[], exit_code=2, error=exc.kind)

    results.extend(check_repo(repo_root))
    results.extend(_check_ledger_meta_secrets(repo_root))
    failed = any(r.state == "missing" or bool(r.reasons) for r in results)
    return CheckReport(results=results, exit_code=1 if failed else 0)
