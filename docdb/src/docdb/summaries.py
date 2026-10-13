"""구조 값의 템플릿 요약과 서술 필드의 에이전트 요약을 관리한다."""
import json

from docdb import clock, policy, textnorm
from docdb.actors import Actor
from docdb.db import Connection
from docdb.errors import DocdbError

MAX_SUM_TOK: int = 60


def template(con: Connection, handle: str) -> str | None:
    """구조 값(항목, JSON 필드, 실행)의 요약 문자열을 계산한다(저장하지 않음)."""
    from docdb.llm import parse_handle

    kind, keys = parse_handle(handle)
    if kind == "i":
        item_id = keys[0]
        row = con.execute(
            "SELECT item_id, status, title FROM item WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        item_id_val, status, title = row
        summary_text = f"{item_id_val} {status or '-'} {title}"
        return summary_text[:200]
    elif kind == "r":
        run_id = keys[0]
        row = con.execute(
            "SELECT run_id, run_type, result FROM run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        run_id_val, run_type, result = row
        return f"{run_id_val} {run_type} {result}"
    elif kind == "f":
        doc_id, kind_id, field_key = keys
        row = con.execute(
            "SELECT format, value_json FROM field_value WHERE doc_id = ? AND kind_id = ? AND field_key = ?",
            (doc_id, kind_id, field_key),
        ).fetchone()
        if row is None:
            return None
        fmt, value_json = row
        if fmt != "json" or value_json is None:
            return None
        parsed = json.loads(value_json)  # 스키마 CHECK(json_valid)가 유효성을 보장한다
        if isinstance(parsed, dict):
            keys_str = ",".join(sorted(str(k) for k in parsed.keys()))
            res = f"json keys={keys_str}"
            return res[:120]
        elif isinstance(parsed, list):
            return f"json list n={len(parsed)}"
        return None
    return None


def store(con: Connection, actor: Actor, handle: str, text: str, source_digest: str) -> None:
    """에이전트가 제출한 요약(sum)을 검증 후 entity_summary 테이블에 저장한다."""
    tok = textnorm.tok_est(text)
    if tok > MAX_SUM_TOK:
        raise DocdbError("SUMMARY_TOO_LONG", f"요약 토큰 추정치({tok})가 {MAX_SUM_TOK}를 초과함")
    policy.assert_no_secrets(con, handle, [text])
    now = clock.now_iso()
    con.execute(
        """
        INSERT INTO entity_summary (handle, summary, source_digest, tok_est, author_type, author_id, created_at)
        VALUES (?, ?, ?, ?, 'llm', ?, ?)
        ON CONFLICT(handle) DO UPDATE SET
            summary = excluded.summary,
            source_digest = excluded.source_digest,
            tok_est = excluded.tok_est,
            author_type = excluded.author_type,
            author_id = excluded.author_id,
            created_at = excluded.created_at
        """,
        (handle, text, source_digest, tok, actor.id, now),
    )


def lookup(con: Connection, handle: str, current_digest: str) -> tuple[str, str | None]:
    """entity_summary에 저장된 요약을 조회하여 상태('fresh'|'stale'|'none')와 요약을 반환한다."""
    row = con.execute(
        "SELECT summary, source_digest FROM entity_summary WHERE handle = ?",
        (handle,),
    ).fetchone()
    if row is None:
        return "none", None
    summary_text, source_digest = row
    if source_digest == current_digest:
        return "fresh", summary_text
    return "stale", summary_text
