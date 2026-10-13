"""change_log 이력의 해시 체인을 기록하고 검증한다."""
import json

from docdb import clock, db
from docdb.db import Connection
from docdb.errors import DocdbError
from docdb.textnorm import canonical_json, normalize_text, sha256_hex

RECORDED_TABLES: frozenset[str] = frozenset(
    {
        "actor",
        "project",
        "fact_value",
        "document",
        "document_kind",
        "field_value",
        "item",
        "relation",
        "artifact",
        "work",
        "work_event",
        "run",
        "finding",
        "handoff",
        "approval",
        "policy_exception",
        "link_exemption",
    }
)


def row_digest(
    entity_type: str,
    entity_id: str,
    seq: int,
    op: str,
    after: dict | None,
    prev_digest: str | None,
) -> str:
    """변경 행과 직전 해시를 묶은 sha256 digest를 계산한다."""
    payload = {
        "t": entity_type,
        "e": entity_id,
        "seq": seq,
        "op": op,
        "after": after,
        "prev": prev_digest,
    }
    return "sha256:" + sha256_hex(canonical_json(payload).encode("utf-8"))


def append_change(
    con: Connection,
    entity_type: str,
    entity_id: str,
    op: str,
    before: dict | None,
    after: dict | None,
    actor_id: str,
    work_id: str | None,
    reason: str | None,
) -> int:
    """write_tx 안에서 이력 행을 추가하고 change_id를 반환한다."""
    if not db._DEPTH.get(id(con), 0):
        raise DocdbError("DB", "append_change는 write_tx 안에서만")

    try:
        previous = con.execute(
            "SELECT change_id, seq, row_digest FROM change_log "
            "WHERE entity_type = ? AND entity_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (entity_type, entity_id),
        ).fetchone()
        if previous is None:
            seq = 1
            prev_change_id = None
            prev_digest = None
        else:
            prev_change_id, previous_seq, prev_digest = previous
            seq = previous_seq + 1

        before_json = canonical_json(before) if before is not None else None
        after_json = canonical_json(after) if after is not None else None
        normalized_reason = normalize_text(reason)
        digest = row_digest(entity_type, entity_id, seq, op, after, prev_digest)
        cursor = con.execute(
            "INSERT INTO change_log "
            "(entity_type, entity_id, seq, prev_change_id, op, before_json, after_json, "
            "actor_id, work_id, reason, changed_at, row_digest, prev_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_type,
                entity_id,
                seq,
                prev_change_id,
                op,
                before_json,
                after_json,
                actor_id,
                work_id,
                normalized_reason,
                clock.now_iso(),
                digest,
                prev_digest,
            ),
        )
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc
    return int(cursor.lastrowid)


def verify_chain(
    con: Connection,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[str]:
    """선택한 엔터티의 이력 digest를 다시 계산해 불일치 행을 반환한다."""
    clauses: list[str] = []
    params: list[str] = []
    if entity_type is not None:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    try:
        rows = con.execute(
            "SELECT entity_type, entity_id, seq, op, after_json, prev_digest, row_digest "
            "FROM change_log" + where + " ORDER BY entity_type, entity_id, seq",
            params,
        ).fetchall()
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    errors: list[str] = []
    for entity_type_value, entity_id_value, seq, op, after_json, prev_digest, stored_digest in rows:
        try:
            after = json.loads(after_json) if after_json is not None else None
        except json.JSONDecodeError:
            after = None
            valid_json = False
        else:
            valid_json = True
        expected = row_digest(
            entity_type_value,
            entity_id_value,
            seq,
            op,
            after,
            prev_digest,
        )
        if not valid_json or expected != stored_digest:
            errors.append(f"{entity_type_value}|{entity_id_value} seq={seq} digest mismatch")
    return errors
