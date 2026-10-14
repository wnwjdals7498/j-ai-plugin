"""민감 정보 검사, 정책 예외 및 외부 반출 정책 모듈."""
import re
from datetime import datetime, timedelta

from docdb import clock, db, history, ids
from docdb.actors import Actor, require_human
from docdb.db import Connection
from docdb.errors import DocdbError
from docdb.textnorm import normalize_text

SECRET_PATTERNS: list[tuple[str, str]] = [
    ("aws_access_key", r"AKIA[0-9A-Z]{16}"),
    ("github_token", r"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("anthropic_key", r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai_key", r"sk-(?!ant-)[A-Za-z0-9_-]{20,}"),
    ("slack_token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("telegram_bot_token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("assignment", r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]


def scan_secrets(text: str | None) -> list[str]:
    """텍스트에서 민감 정보 패턴을 탐지하여 검출된 패턴 이름 목록을 반환한다."""
    if not text:
        return []
    matched: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        if re.search(pattern, text):
            matched.append(name)
    return matched


def assert_no_secrets(con: Connection, handle: str, texts: list[str | None]) -> None:
    """텍스트 목록에 민감 정보가 포함되어 있는지 검사한다.

    검출된 패턴이 있고 유효한 예외가 등록되어 있지 않으면 SECRET_DETECTED 오류를 발생시킨다.
    """
    detected: list[str] = []
    for text in texts:
        for name in scan_secrets(text):
            if name not in detected:
                detected.append(name)

    if not detected:
        return

    now = clock.now_iso()
    try:
        row = con.execute(
            """
            SELECT 1
            FROM policy_exception pe
            JOIN actor a ON pe.approved_by = a.actor_id
            WHERE pe.scope = ?
              AND pe.expires_at > ?
              AND a.actor_type = 'human'
            LIMIT 1
            """,
            (f"secret:{handle}", now),
        ).fetchone()
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    if row is None:
        raise DocdbError("SECRET_DETECTED", ", ".join(detected))


def allow_secret(
    con: Connection,
    handle: str,
    reason: str,
    days: int,
    actor: Actor,
    interactive: bool,
) -> str:
    """민감 정보 검사 예외를 등록하고 발급된 예외 ID를 반환한다.

    사람 및 대화형 세션에서만 호출할 수 있으며 이력을 기록한다.
    """
    require_human(actor, interactive)
    if days <= 0:
        raise DocdbError("BAD_ARGS", "유효 기간(days)은 1일 이상이어야 함")

    try:
        proj_row = con.execute("SELECT project_id FROM project LIMIT 1").fetchone()
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    if proj_row is None:
        raise DocdbError("NOT_FOUND", "프로젝트를 찾을 수 없음")
    project_id = proj_row[0]

    now_str = clock.now_iso()
    dt = datetime.fromisoformat(now_str)
    expires_dt = dt + timedelta(days=days)
    expires_at = expires_dt.isoformat(timespec="seconds")

    normalized_reason = normalize_text(reason) or ""
    assert_no_secrets(con, "policy-exception:reason", [normalized_reason])
    scope = f"secret:{handle}"

    with db.write_tx(con):
        exception_id = ids.next_id(con, "exception")
        try:
            con.execute(
                """
                INSERT INTO policy_exception (
                    exception_id, project_id, kind_id, scope, reason, approved_by, expires_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                """,
                (exception_id, project_id, scope, normalized_reason, actor.id, expires_at),
            )
        except db.DatabaseError as exc:
            raise DocdbError("DB", str(exc)[:80]) from exc

        history.append_change(
            con=con,
            entity_type="policy_exception",
            entity_id=exception_id,
            op="insert",
            before=None,
            after={
                "exception_id": exception_id,
                "project_id": project_id,
                "kind_id": None,
                "scope": scope,
                "reason": normalized_reason,
                "approved_by": actor.id,
                "expires_at": expires_at,
            },
            actor_id=actor.id,
            work_id=None,
            reason=normalized_reason,
        )

    return exception_id


def can_export(con: Connection, doc_id: str) -> bool:
    """문서의 외부 전송 정책에 따라 외부 반출 가능 여부를 확인한다.

    export_policy가 'local_only'가 아니면 True를 반환한다.
    """
    try:
        row = con.execute(
            "SELECT export_policy FROM document WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    if row is None:
        raise DocdbError("NOT_FOUND", f"문서를 찾을 수 없음: {doc_id}")

    return row[0] != "local_only"
