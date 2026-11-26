"""PMT 이전 매니페스트(v1)의 읽기·검사·미리 보기. Plane과 git에는 쓰지 않는다.

매니페스트는 모델이 만드는 입력이다. 문제 목록과 미리 보기에는 노출 패턴에 걸리는 값을 절대 내지 않는다.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re

# nosemgrep: opt.semgrep-rules.python.lang.security.use-defused-xml.use-defused-xml -- 표준 라이브러리 제약(로컬 drawio)
import xml.etree.ElementTree as ET  # nosec B405 # 표준 라이브러리 제약, ENTITY 선언은 파싱 전에 거부
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional
from urllib.parse import unquote

from ledger.secrets import PATTERN_NAMES, find_exposures

_VERSION = 1
_TOP_FIELDS = ("version", "slug", "source_dir", "records")
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?(Z|[+-][0-9]{2}:[0-9]{2})?")
_MAX_DEPTH_BELOW_WORK = 3
# 정상 매니페스트는 몇 단계뿐이다. 검사는 재귀하므로 읽을 때 깊이를 막는다.
_MAX_JSON_DEPTH = 20
_DRAWIO_NAME_RE = re.compile(r"\.(drawio|dio)(\.|$)")
# 주석은 첫 `-->`에서만 끝나게 해 역추적이 늘지 않게 한다.
_MXFILE_START_RE = re.compile(r"\s*(<\?xml[^>]*\?>\s*)?(<!--(?:(?!-->).)*-->\s*)*<mxfile\b", re.DOTALL)
_MAX_DIAGRAM_BYTES = 20 * 1024 * 1024  # 압축을 푼 다이어그램 한 쪽의 최대 크기
_MAX_EMBED_DEPTH = 3  # SVG 속 mxfile, 압축 본문 속 XML처럼 겹친 단계
_UNPARSABLE_DRAWIO = "draw.io 파일을 XML로 해석할 수 없어 검사할 수 없음"
_UNDECODABLE_DIAGRAM = "압축된 draw.io 다이어그램을 풀 수 없어 검사할 수 없음"

_TEXT = "비어 있지 않은 문자열"
_ANY_TEXT = "문자열"
_TEXT_LIST = "문자열 목록"
_OPTIONAL_TEXT = "없거나 null이거나 비어 있지 않은 문자열"

_COMMON_FIELDS = {"key": _TEXT, "kind": _TEXT, "redactions": _TEXT_LIST}
_PLANE_ITEM_FIELDS = {
    "name": _TEXT,
    "status": _TEXT,
    "labels": _TEXT_LIST,
    "description_html": _ANY_TEXT,
    "link_to": _OPTIONAL_TEXT,
}
_KIND_FIELDS: dict[str, dict[str, str]] = {
    "project": {"body_md": _ANY_TEXT},
    "work": _PLANE_ITEM_FIELDS,
    "item": {**_PLANE_ITEM_FIELDS, "parent": _TEXT, "blocked_by": _TEXT_LIST},
    "comment": {"item": _TEXT, "at": _TEXT, "body_html": _ANY_TEXT},
    "backlog": _PLANE_ITEM_FIELDS,
    "decision": {"date": _TEXT, "status": _TEXT, "title": _TEXT, "detail": _ANY_TEXT, "replaced_by": _OPTIONAL_TEXT},
    "fact": {"date": _TEXT, "status": _TEXT, "text": _TEXT, "ref": _ANY_TEXT},
    "resource": {"path": _TEXT},
}
_WORK_STATUSES = ("Planned", "In Progress", "Done", "Paused", "Failed", "Skipped")
_STATUSES: dict[str, tuple[str, ...]] = {
    "work": _WORK_STATUSES,
    "item": _WORK_STATUSES,
    "backlog": ("Active", "Done", "폐기"),
    "decision": ("승인", "대체", "폐기"),
    "fact": ("Active", "폐기"),
}
# (kind, 참조 필드) → 가리킬 수 있는 kind. link_to는 Plane 항목 id라 여기에 없다.
_REFERENCES: dict[tuple[str, str], tuple[str, ...]] = {
    ("item", "parent"): ("work", "item"),
    ("comment", "item"): ("work", "item", "backlog"),
    ("decision", "replaced_by"): ("decision",),
}
# 미리 보기 견본에 보여 줄 필드
_SAMPLE_FIELD = {
    "project": "body_md",
    "work": "name",
    "item": "name",
    "comment": "body_html",
    "backlog": "name",
    "decision": "title",
    "fact": "text",
    "resource": "path",
}


class ManifestError(ValueError):
    """매니페스트를 읽을 수 없거나 최상위 구조가 아니다. 메시지에 파일 내용을 넣지 않는다."""


@dataclass(frozen=True)
class Manifest:
    """매니페스트 v1. 레코드는 JSON의 필드 이름을 그대로 쓰는 딕셔너리다."""

    version: Any
    slug: str
    source_dir: str
    records: tuple[dict[str, Any], ...]
    unknown_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Problem:
    key: str
    message: str


@dataclass
class ApplyPlan:
    """apply가 할 일. 각 목록은 원천 키다."""

    create: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    conflict: list[str] = field(default_factory=list)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        # 뒤의 값이 앞의 값을 덮으면 앞의 값은 검사를 받지 않는다.
        raise ManifestError("매니페스트에 중복된 필드가 있음")
    return result


def _is_too_deep(data: Any) -> bool:
    pending = [(data, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth > _MAX_JSON_DEPTH:
                return True
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    return False


def load_manifest(path: Path) -> Manifest:
    """JSON을 읽어 최상위 구조만 확인한다. 내용 검사는 `check_manifest`가 한다."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise ManifestError("매니페스트 파일을 읽을 수 없음") from None
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except ManifestError:
        raise
    except UnicodeDecodeError:
        raise ManifestError("매니페스트가 UTF-8이 아님") from None
    except (ValueError, RecursionError):
        raise ManifestError("매니페스트 JSON 형식 오류") from None
    if _is_too_deep(data):
        raise ManifestError("매니페스트의 중첩이 너무 깊음")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ManifestError("매니페스트는 객체이고 records는 객체 목록이어야 함")
    slug = data.get("slug")
    source_dir = data.get("source_dir")
    return Manifest(
        version=data.get("version"),
        slug=slug if isinstance(slug, str) else "",
        source_dir=source_dir if isinstance(source_dir, str) else "",
        records=tuple(records),
        unknown_fields={name: value for name, value in data.items() if name not in _TOP_FIELDS},
    )


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME", "").strip()
    return Path(home) if home else Path(os.path.expanduser("~"))


def resolve_source_dir(m: Manifest, env: Mapping[str, str]) -> Path:
    """`source_dir`의 앞쪽 `~`를 `env`의 홈으로 바꾼다."""
    if m.source_dir == "~" or m.source_dir.startswith("~/"):
        return _home(env) / m.source_dir[2:]
    return Path(m.source_dir)


def _is_slug(slug: str) -> bool:
    return _SLUG_RE.fullmatch(slug) is not None and ".." not in slug


def manifest_path(slug: str, env: Mapping[str, str]) -> Path:
    """`${XDG_STATE_HOME:-~/.local/state}/forge-ledger/migrations/<slug>.json`."""
    if not _is_slug(slug):
        raise ValueError("slug 형식 오류")
    xdg = env.get("XDG_STATE_HOME", "").strip()
    state = Path(xdg) if xdg else _home(env) / ".local" / "state"
    return state / "forge-ledger" / "migrations" / f"{slug}.json"


def _exposures(*texts: str) -> list[str]:
    """원문과 HTML 문자 참조를 푼 글(화면에 보이는 글자)을 모두 검사한다. 정의 순서, 중복 없음."""
    found: set[str] = set()
    for text in texts:
        found.update(find_exposures(text))
        shown = html.unescape(text)
        if shown != text:
            found.update(find_exposures(shown))
    return [name for name in PATTERN_NAMES if name in found]


def _field_name(name: str) -> str:
    return "(가린 필드 이름)" if _exposures(name) else " ".join(name.split())


def _record_label(record: Mapping[str, Any], index: int) -> str:
    """문제 목록에 쓸 이름. 키에 노출 값이 있거나 키가 없으면 위치로 부른다."""
    key = record.get("key")
    if isinstance(key, str) and key.strip() and not _exposures(key):
        return " ".join(key.split())
    return f"records[{index}]"


def _strings(value: Any, where: str) -> Iterator[tuple[str, str]]:
    """(위치, 문자열)을 모두 낸다. 객체의 필드 이름도 문자열로 본다."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{where}[{index}]")
    elif isinstance(value, dict):
        for name, item in value.items():
            inner = f"{where}.{_field_name(name)}" if where else _field_name(name)
            yield f"{inner} (필드 이름)", name
            yield from _strings(item, inner)


def _exposure_problems(label: str, value: Mapping[str, Any]) -> list[Problem]:
    problems = []
    for where, text in _strings(dict(value), ""):
        names = _exposures(text)
        if names:
            problems.append(Problem(label, f"노출 패턴({', '.join(names)}): {where}"))
    return problems


def _has_type(value: Any, expected: str) -> bool:
    if expected == _TEXT_LIST:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if expected == _OPTIONAL_TEXT and value is None:
        return True
    if expected == _ANY_TEXT:
        return isinstance(value, str)
    return isinstance(value, str) and bool(value.strip())


def _is_date(value: str) -> bool:
    if not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_time(value: str) -> bool:
    if not _TIME_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value[:16].replace(" ", "T"), "%Y-%m-%dT%H:%M")
    except ValueError:
        return False
    return True


def _field_problems(record: Mapping[str, Any]) -> list[str]:
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in _KIND_FIELDS:
        return [f"kind가 허용값이 아님(허용: {', '.join(_KIND_FIELDS)})"]
    fields = {**_COMMON_FIELDS, **_KIND_FIELDS[kind]}
    messages = [f"알 수 없는 필드: {_field_name(name)}" for name in record if name not in fields]
    valid: dict[str, Any] = {}
    for name, expected in fields.items():
        if name not in record:
            if expected != _OPTIONAL_TEXT:
                messages.append(f"필드 없음: {name}")
        elif not _has_type(record[name], expected):
            messages.append(f"필드 형식 오류: {name} ({expected}이어야 함)")
        else:
            valid[name] = record[name]
    if kind in _STATUSES and "status" in valid and valid["status"] not in _STATUSES[kind]:
        messages.append(f"status가 허용값이 아님(허용: {', '.join(_STATUSES[kind])})")
    if "date" in valid and not _is_date(valid["date"]):
        messages.append("date 형식 오류 (YYYY-MM-DD)")
    if "at" in valid and not _is_time(valid["at"]):
        messages.append("at 형식 오류 (YYYY-MM-DDTHH:MM)")
    return messages


def _target(record: Mapping[str, Any], name: str, by_key: Mapping[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    value = record.get(name)
    return by_key.get(value) if isinstance(value, str) else None


def _reference_problems(record: Mapping[str, Any], by_key: Mapping[str, dict[str, Any]]) -> list[str]:
    messages = []
    for (kind, name), allowed in _REFERENCES.items():
        value = record.get(name)
        if record.get("kind") != kind or not isinstance(value, str) or not value.strip():
            continue
        target = _target(record, name, by_key)
        if target is None:
            messages.append(f"{name}가 가리키는 키가 없음")
        elif target.get("kind") not in allowed:
            messages.append(f"{name}가 가리키는 레코드의 kind가 맞지 않음(필요: {', '.join(allowed)})")
    return messages


def _depth_problem(record: dict[str, Any], by_key: Mapping[str, dict[str, Any]]) -> Optional[str]:
    """parent 사슬의 순환과 깊이. 끊긴 사슬은 끊긴 레코드에서 참조 문제로 한 번만 알린다."""
    current = record
    seen = [record]
    while current.get("kind") == "item":
        parent = _target(current, "parent", by_key)
        if parent is None or parent.get("kind") not in _REFERENCES[("item", "parent")]:
            return None
        if any(parent is visited for visited in seen):
            return "parent 사슬이 순환함"
        seen.append(parent)
        current = parent
    if len(seen) - 1 > _MAX_DEPTH_BELOW_WORK:
        return f"Work 아래 깊이가 {_MAX_DEPTH_BELOW_WORK}을 넘음"
    return None


class _Unscannable(Exception):
    """내용을 확인할 수 없는 파일. 메시지는 그대로 문제로 쓰며 파일 내용을 담지 않는다."""


def _parse_xml(text: str) -> Optional[ET.Element]:
    if "<!ENTITY" in text:  # draw.io는 쓰지 않는다. 엔티티 확장 공격을 막는다.
        return None
    try:
        # nosemgrep -- 표준 라이브러리 제약(로컬 drawio), 위에서 ENTITY 선언을 거부함
        return ET.fromstring(text.lstrip("\ufeff"))  # nosec B314
    except (ET.ParseError, ValueError):
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _embedded_source(element: ET.Element) -> str:
    """draw.io가 내보낸 SVG는 원본(mxfile 또는 mxGraphModel)을 `content` 속성에 담는다.

    다른 형식의 `content`는 속을 볼 수 없으므로 `_Unscannable`이다.
    """
    if _local_name(element.tag) != "svg":
        return ""
    content = element.get("content", "").strip()
    if content and not content.startswith(("<mxfile", "<mxGraphModel")):
        raise _Unscannable(_UNPARSABLE_DRAWIO)
    return content


def _inflate_diagram(body: str) -> str:
    """압축 본문(base64 → raw deflate → URL 인코딩)을 푼다. 조금이라도 어긋나면 `_Unscannable`."""
    try:
        data = base64.b64decode("".join(body.split()), validate=True)
        inflater = zlib.decompressobj(wbits=-15)
        raw = inflater.decompress(data, _MAX_DIAGRAM_BYTES)
        # 끝나지 않은 스트림(잘림·크기 초과)이나 뒤에 남은 데이터는 검사하지 못한 내용이다.
        if not inflater.eof or inflater.unused_data:
            raise _Unscannable(_UNDECODABLE_DIAGRAM)
        return unquote(raw.decode("utf-8"), errors="strict")
    except (ValueError, zlib.error):
        raise _Unscannable(_UNDECODABLE_DIAGRAM) from None


def _diagram_strings(root: ET.Element, depth: int = 0) -> Iterator[str]:
    """속성·글자(XML 문자 참조는 풀린 상태)와, 압축을 푼 다이어그램의 내용을 낸다."""
    if depth > _MAX_EMBED_DEPTH:
        raise _Unscannable(_UNPARSABLE_DRAWIO)
    for element in root.iter():
        yield from (text for text in (*element.attrib.values(), element.text, element.tail) if text)
        embedded = _embedded_source(element)
        if embedded:
            inner = _parse_xml(embedded)
            if inner is None:
                raise _Unscannable(_UNPARSABLE_DRAWIO)
            yield from _diagram_strings(inner, depth + 1)
        if _local_name(element.tag) != "diagram":
            continue
        # 비압축 다이어그램은 자식 요소만 있다. 직접 든 글자는 압축 본문이다.
        body = (element.text or "") + "".join(child.tail or "" for child in element)
        if body.strip():
            decoded = _inflate_diagram(body)
            yield decoded
            inner = _parse_xml(decoded)
            if inner is not None:
                yield from _diagram_strings(inner, depth + 1)


def _drawio_strings(name: str, text: str) -> list[str]:
    """draw.io 파일이면 원문 밖에 더 검사할 글을 낸다. 이름이나 내용으로 알아본다."""
    if not text.strip():
        return []
    named = _DRAWIO_NAME_RE.search(name.lower()) is not None
    root = _parse_xml(text)
    if root is None:
        if named or _MXFILE_START_RE.match(text.lstrip("\ufeff")):
            raise _Unscannable(_UNPARSABLE_DRAWIO)
        return []
    if not named and not any(_local_name(e.tag) == "mxfile" or _embedded_source(e) for e in root.iter()):
        return []
    return list(_diagram_strings(root))


def load_resource(relative: str, resources: Path) -> tuple[bytes, Optional[str]]:
    """resource를 한 번 읽어 (바이트, 문제)를 낸다. 검사할 수 없는 파일은 통과시키지 않는다.

    문제가 `None`이면 돌려준 바이트가 검사를 통과한 것이다. 쓰는 쪽은 파일을 다시 읽지 말고 이 바이트를 쓴다.
    """
    if (
        "\\" in relative
        or "\0" in relative
        or PurePosixPath(relative).is_absolute()
        or PureWindowsPath(relative).drive
        or any(part in ("", ".", "..") for part in relative.split("/"))
    ):
        return b"", "안전하지 않은 resource 경로(resources 아래 상대 경로만 허용, `..` 금지)"
    target = resources / relative
    try:
        root = resources.resolve()
        if root not in target.resolve().parents:
            return b"", "resource가 resources 폴더 밖을 가리킴"
        if not target.is_file():
            return b"", "resource 파일이 없음"
        data = target.read_bytes()
    except (OSError, RuntimeError):
        return b"", "resource 파일을 읽을 수 없음"
    return data, _content_problem(PurePosixPath(relative).name, data)


def _content_problem(name: str, data: bytes) -> Optional[str]:
    try:
        text = None if b"\0" in data else data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None:
        return "검사할 수 없는 파일(이진 또는 UTF-8 아님)"
    try:
        extra = _drawio_strings(name, text)
    except _Unscannable as exc:
        return str(exc)
    names = _exposures(text, *extra)
    return f"노출 패턴({', '.join(names)}): resource 파일 내용" if names else None


def _manifest_problems(m: Manifest) -> list[Problem]:
    messages = []
    if type(m.version) is not int or m.version != _VERSION:
        messages.append(f"version은 정수 {_VERSION}이어야 함")
    if not _is_slug(m.slug):
        messages.append("slug 형식 오류")
    if not m.source_dir.strip():
        messages.append("source_dir는 비어 있지 않은 문자열이어야 함")
    messages.extend(f"알 수 없는 필드: {_field_name(name)}" for name in m.unknown_fields)
    top = {"slug": m.slug, "source_dir": m.source_dir, **m.unknown_fields}
    return [Problem("manifest", message) for message in messages] + _exposure_problems("manifest", top)


def check_manifest(m: Manifest, source_dir: Path) -> list[Problem]:
    """형식·참조·깊이·노출을 모두 검사해 문제 목록을 낸다. 비어 있으면 통과다."""
    problems = _manifest_problems(m)
    by_key: dict[str, dict[str, Any]] = {}
    for record in m.records:
        key = record.get("key")
        if isinstance(key, str):
            by_key.setdefault(key, record)

    prefix = f"pmt:{m.slug}/"
    for index, record in enumerate(m.records):
        label = _record_label(record, index)
        messages = _field_problems(record)
        key = record.get("key")
        if isinstance(key, str) and key.strip():
            if by_key[key] is not record:
                messages.append("키가 중복됨")
            if not key.startswith(prefix) or key == prefix:
                messages.append("키는 pmt:<slug>/ 로 시작하고 뒤에 위치가 있어야 함")
        messages.extend(_reference_problems(record, by_key))
        depth = _depth_problem(record, by_key)
        if depth:
            messages.append(depth)
        path = record.get("path")
        if record.get("kind") == "resource" and isinstance(path, str) and path.strip():
            resource = load_resource(path, source_dir / "resources")[1]
            if resource:
                messages.append(resource)
        problems.extend(Problem(label, message) for message in messages)
        problems.extend(_exposure_problems(label, record))
    return problems


def _shown(value: Any, limit: int = 80) -> str:
    """미리 보기에 낼 한 줄. 노출 패턴에 걸리면 값 대신 패턴 이름만 낸다."""
    text = value if isinstance(value, str) else ""
    names = _exposures(text)
    if names:
        return f"(가림: {', '.join(names)})"
    single = " ".join(text.split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


def _section(title: str, lines: list[str]) -> list[str]:
    return ["", title, *(lines or ["- 없음"])]


def render_preview(m: Manifest, plan: ApplyPlan) -> str:
    """종류별 개수, 적용 계획, 연결·치환 목록, 종류별 견본 하나를 사람이 읽을 글로 만든다."""

    def key_of(record: Mapping[str, Any]) -> str:
        return _shown(record.get("key"), 110)

    counts = [f"- {kind}: {sum(1 for r in m.records if r.get('kind') == kind)}" for kind in _KIND_FIELDS]
    totals = [
        f"- 새로 만들 것: {len(plan.create)}",
        f"- 바꿀 것: {len(plan.update)}",
        f"- 그대로 둘 것: {len(plan.skip)}",
        f"- 충돌: {len(plan.conflict)}",
    ]
    conflicts = [f"- {_shown(key, 110)}" for key in plan.conflict]
    links = [f"- {key_of(r)} → {_shown(r['link_to'])}" for r in m.records if r.get("link_to")]
    redactions = [
        f"- {key_of(r)}: {_shown(note)}"
        for r in m.records
        if isinstance(r.get("redactions"), list)
        for note in r["redactions"]
    ]
    samples = []
    for kind, name in _SAMPLE_FIELD.items():
        first = next((r for r in m.records if r.get("kind") == kind), None)
        if first is not None:
            samples.append(f"- [{kind}] {key_of(first)}: {_shown(first.get(name), 60)}")

    lines = [f"이전 미리 보기: {_shown(m.slug)}"]
    lines += _section("종류별 개수", counts)
    lines += _section("적용 계획", totals)
    lines += _section("충돌(덮어쓰지 않음)", conflicts)
    lines += _section("기존 항목에 연결(link_to)", links)
    lines += _section("치환 목록", redactions)
    lines += _section("레코드 견본", samples)
    return "\n".join(lines) + "\n"
