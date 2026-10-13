"""ID 형식 검증, 순번 자동 발급 및 재사용 금지 검사 모듈."""
import re

from docdb import clock, db
from docdb.db import Connection
from docdb.errors import DocdbError

PATTERNS: dict[str, str] = {
    "doc": r"^DOC-[A-Z0-9]+(-[A-Z0-9]+)*$",
    "item": r"^[A-Z0-9]+-[A-Z0-9]+(-[A-Z0-9]+)*$",
    "work": r"^WORK-\d{4}-\d{4,}$",
    "run": r"^RUN-\d{4}-\d{4,}$",
    "handoff": r"^HO-\d{4}-\d{4,}$",
    "approval": r"^APR-\d{4}-\d{4,}$",
    "artifact": r"^ART-\d{4}-\d{4,}$",
    "exception": r"^PEX-\d{4}-\d{4,}$",
}

PREFIXES: dict[str, str] = {
    "work": "WORK",
    "run": "RUN",
    "handoff": "HO",
    "approval": "APR",
    "artifact": "ART",
    "exception": "PEX",
}

_KIND_TABLE_COL: dict[str, tuple[str, str]] = {
    "work": ("work", "work_id"),
    "run": ("run", "run_id"),
    "handoff": ("handoff", "handoff_id"),
    "approval": ("approval", "approval_id"),
    "artifact": ("artifact", "artifact_id"),
    "exception": ("policy_exception", "exception_id"),
}


def validate(kind: str, value: str) -> None:
    """ID가 지정된 종류의 형식 규격에 맞는지 검사한다.

    위반 시 ID_FORMAT 오류를 발생시킨다.
    """
    if not isinstance(value, str) or not value:
        raise DocdbError("ID_FORMAT", "ID는 비어 있지 않은 문자열이어야 함")
    pattern = PATTERNS.get(kind)
    if pattern is None:
        raise DocdbError("BAD_ARGS", f"알 수 없는 ID 종류: {kind}")
    if kind in ("doc", "item") and len(value) > 64:
        raise DocdbError("ID_FORMAT", f"{kind} ID 길이는 64자 이하이어야 함")
    if not re.fullmatch(pattern, value, re.ASCII):
        raise DocdbError("ID_FORMAT", f"{kind} ID 형식이 올바르지 않음: {value}")


def next_id(con: Connection, kind: str, year: int | None = None) -> str:
    """해당 종류의 다음 순번 ID를 발급한다.

    반드시 write_tx 안에서 호출해야 하며, 트랜잭션 밖이면 DB 오류를 발생시킨다.
    """
    if not getattr(con, "in_transaction", False):
        raise DocdbError("DB", "next_id는 write_tx 안에서만")
    if kind not in PREFIXES:
        raise DocdbError("BAD_ARGS", f"알 수 없는 자동 발급 종류: {kind}")

    target_year = year if year is not None else clock.utc_year()
    prefix = PREFIXES[kind]
    table, col = _KIND_TABLE_COL[kind]
    like_pattern = f"{prefix}-{target_year}-%"

    try:
        cursor = con.execute(
            f"SELECT {col} FROM {table} WHERE {col} LIKE ?",
            (like_pattern,),
        )
        rows = cursor.fetchall()
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    max_seq = 0
    for (val,) in rows:
        if val and "-" in val:
            part = val.rsplit("-", 1)[-1]
            if part.isdigit():
                num = int(part)
                if num > max_seq:
                    max_seq = num

    next_seq = max_seq + 1
    return f"{prefix}-{target_year}-{next_seq:04d}"


def ensure_not_reused(con: Connection, entity_type: str, entity_id: str) -> None:
    """삭제된 ID인지 change_log에서 확인하고 삭제된 이력이 있으면 ID_FORMAT 오류를 발생시킨다."""
    try:
        row = con.execute(
            "SELECT 1 FROM change_log WHERE entity_type = ? AND entity_id = ? AND op = 'delete' LIMIT 1",
            (entity_type, entity_id),
        ).fetchone()
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    if row is not None:
        raise DocdbError("ID_FORMAT", "삭제된 ID 재사용 금지")
