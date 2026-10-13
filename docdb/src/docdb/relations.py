"""관계 제안, 사람 검토와 변경 이력을 관리한다."""
from docdb import actors, clock, db, history, policy, textnorm
from docdb.actors import Actor
from docdb.db import Connection
from docdb.errors import DocdbError

REL_TYPES: tuple[str, ...] = (
    "references",
    "implements",
    "verified_by",
    "constrained_by",
    "depends_on",
    "uses_data",
    "visualizes",
    "generated_from",
    "changes",
    "supersedes",
    "evidence_for",
    "handoff_to",
)

_ENDPOINTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "references": (("document", "item", "artifact", "work", "run"),
                   ("document", "item", "artifact", "work", "run")),
    "implements": (("item", "artifact"), ("item",)),
    "verified_by": (("item",), ("item", "run")),
    "constrained_by": (("item", "document"), ("item", "document")),
    "depends_on": (("item",), ("item",)),
    "uses_data": (("item",), ("item",)),
    "visualizes": (("item",), ("item",)),
    "generated_from": (("artifact", "document"), ("artifact", "document", "item")),
    "changes": (("work",), ("document", "item")),
    "supersedes": (("document", "item"), ("document", "item")),
    "evidence_for": (("run", "artifact"), ("item", "work", "document")),
    "handoff_to": (("work",), ("work",)),
}

_HANDLES: dict[str, tuple[str, str]] = {
    "d": ("document", "doc_id"),
    "i": ("item", "item_id"),
    "a": ("artifact", "artifact_id"),
    "w": ("work", "work_id"),
    "r": ("run", "run_id"),
}
_PREFIXES = {entity: prefix for prefix, (entity, _column) in _HANDLES.items()}
_RELATION_COLUMNS = (
    "from_type",
    "from_id",
    "rel_type",
    "to_type",
    "to_id",
    "is_required",
    "target_revision_id",
    "anchor",
    "reason",
    "created_at",
    "status",
    "source",
    "proposed_by",
    "decided_by",
    "decided_at",
)


def _execute(con: Connection, sql: str, args: tuple | list = ()):
    """SQL 오류를 docdb의 DB 오류로 바꾼다."""
    try:
        return con.execute(sql, args)
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc


def _why(con: Connection, handle: str, value: str | None) -> str:
    """이유를 정규화하고 빈 값과 민감 정보를 거부한다."""
    if not isinstance(value, str):
        raise DocdbError("BAD_OP", "관계 이유 why가 필요함")
    normalized = textnorm.normalize_text(value)
    if not normalized or not normalized.strip():
        raise DocdbError("BAD_OP", "관계 이유 why가 비어 있음")
    policy.assert_no_secrets(con, handle, [normalized])
    return normalized


def _endpoint(con: Connection, handle: str) -> tuple[str, str]:
    """지원 핸들의 종류와 ID를 검사하고 대상 행을 확인한다."""
    from docdb.llm import parse_handle

    kind, key = parse_handle(handle)
    target = _HANDLES.get(kind)
    if target is None:
        raise DocdbError("UNSUPPORTED_HANDLE", "관계 끝점은 d:, i:, w:, r:, a:만 지원함")
    entity_type, column = target
    entity_id = key[0]
    table = entity_type
    if _execute(con, f"SELECT 1 FROM {table} WHERE {column}=?", (entity_id,)).fetchone() is None:
        raise DocdbError("NOT_FOUND", f"관계 끝점을 찾을 수 없음: {handle}")
    return entity_type, entity_id


def _snapshot(con: Connection, rel_id: int) -> dict | None:
    """관계의 이력 대상 컬럼을 사전으로 반환한다."""
    row = _execute(
        con,
        "SELECT " + ", ".join(_RELATION_COLUMNS) + " FROM relation WHERE rel_id=?",
        (rel_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_RELATION_COLUMNS, row))


def _insert_work(con: Connection, rel_id: int) -> str | None:
    """관계의 최신 삽입 이력에 기록한 작업 ID를 가져온다."""
    row = _execute(
        con,
        "SELECT work_id FROM change_log WHERE entity_type='relation' AND entity_id=? "
        "AND op='insert' ORDER BY seq DESC LIMIT 1",
        (str(rel_id),),
    ).fetchone()
    return row[0] if row is not None else None


def _record(
    con: Connection,
    rel_id: int,
    op: str,
    before: dict | None,
    after: dict | None,
    actor: Actor,
    reason: str | None,
    work_id: str | None = None,
) -> None:
    """관계 변경 이력을 같은 트랜잭션에 추가한다."""
    history.append_change(
        con,
        "relation",
        str(rel_id),
        op,
        before,
        after,
        actor.id,
        work_id if op == "insert" else _insert_work(con, rel_id),
        reason,
    )


def _error_line(rel_id: object, err: DocdbError) -> str:
    """관계별 오류 응답 줄을 만든다."""
    detail = textnorm.q(err.detail) if err.code == "DB" else err.detail
    return f"ERR rel={rel_id} {err.code}" + (f" {detail}" if detail else "")


def _propose(
    con: Connection,
    actor: Actor,
    from_h: str,
    rel: str,
    to_h: str,
    why: str,
    work_id: str | None,
) -> str:
    """작업 문맥을 포함해 관계를 대기 상태로 제안하고 이력을 남긴다."""
    if actor.type == "team":
        raise DocdbError("BAD_ARGS", "팀 행위자는 관계를 제안할 수 없음")
    if actor.type not in ("ai", "human"):
        raise DocdbError("BAD_ARGS", "관계 제안 행위자 종류가 올바르지 않음")
    if not isinstance(rel, str) or rel not in REL_TYPES:
        raise DocdbError("BAD_OP", f"알 수 없는 관계 종류: {rel}")
    normalized_why = _why(con, str(from_h), why)

    with db.write_tx(con):
        from_type, from_id = _endpoint(con, from_h)
        to_type, to_id = _endpoint(con, to_h)
        from_allowed, to_allowed = _ENDPOINTS[rel]
        if (
            from_type not in from_allowed
            or to_type not in to_allowed
            or (rel == "supersedes" and from_type != to_type)
        ):
            raise DocdbError("BAD_OP", "관계 종류와 끝점 조합이 올바르지 않음")

        duplicate = _execute(
            con,
            "SELECT rel_id, status FROM relation WHERE from_type=? AND from_id=? "
            "AND rel_type=? AND to_type=? AND to_id=?",
            (from_type, from_id, rel, to_type, to_id),
        ).fetchone()
        if duplicate is not None:
            rel_id, status = duplicate
            if status == "rejected":
                raise DocdbError("REJECTED_BEFORE", f"rel={rel_id}")
            raise DocdbError("DUPLICATE", f"rel={rel_id} status={status}")

        source = "agent" if actor.type == "ai" else "human"
        created_at = clock.now_iso()
        cursor = _execute(
            con,
            "INSERT INTO relation (from_type, from_id, rel_type, to_type, to_id, reason, "
            "created_at, status, source, proposed_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
            (from_type, from_id, rel, to_type, to_id, normalized_why, created_at, source, actor.id),
        )
        rel_id = int(cursor.lastrowid)
        after = {
            "from_type": from_type,
            "from_id": from_id,
            "rel_type": rel,
            "to_type": to_type,
            "to_id": to_id,
            "reason": normalized_why,
            "created_at": created_at,
            "status": "proposed",
            "source": source,
            "proposed_by": actor.id,
        }
        _record(con, rel_id, "insert", None, after, actor, normalized_why, work_id)
    return f"OK rel={rel_id} proposed"


def propose(con: Connection, actor: Actor, from_h: str, rel: str, to_h: str, why: str) -> str:
    """관계를 검증해 대기 상태로 제안하고 삽입 이력을 남긴다."""
    return _propose(con, actor, from_h, rel, to_h, why, None)


def withdraw(con: Connection, actor: Actor, rel_id: int, why: str) -> str:
    """제안자가 자기 대기 관계를 철회하고 삭제 이력을 남긴다."""
    if isinstance(rel_id, bool) or not isinstance(rel_id, int) or rel_id < 1:
        raise DocdbError("BAD_OP", "rel_id는 양의 정수여야 함")
    normalized_why = _why(con, f"rel:{rel_id}", why)
    with db.write_tx(con):
        before = _snapshot(con, rel_id)
        if before is None or before["status"] != "proposed" or before["proposed_by"] != actor.id:
            raise DocdbError("NEEDS_HUMAN", "제안자만 대기 관계를 철회할 수 있음")
        _execute(con, "DELETE FROM relation WHERE rel_id=?", (rel_id,))
        _record(con, rel_id, "delete", before, None, actor, normalized_why)
    return f"OK unlink rel={rel_id}"


def review(con: Connection, work_id: str | None = None) -> list[str]:
    """확정 대기 관계를 관계 ID 순으로 목록 줄로 반환한다."""
    clauses = ["status='proposed'"]
    args: list[str] = []
    if work_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM change_log c WHERE c.entity_type='relation' "
            "AND c.entity_id=CAST(relation.rel_id AS TEXT) AND c.op='insert' AND c.work_id=? "
            "AND c.change_id=(SELECT max(c2.change_id) FROM change_log c2 "
            "WHERE c2.entity_type='relation' AND c2.entity_id=CAST(relation.rel_id AS TEXT) "
            "AND c2.op='insert'))"
        )
        args.append(work_id)
    rows = _execute(
        con,
        "SELECT rel_id, from_type, from_id, rel_type, to_type, to_id, proposed_by, reason "
        "FROM relation WHERE " + " AND ".join(clauses) + " ORDER BY rel_id",
        args,
    ).fetchall()
    return [
        f"rel={rel_id} {_PREFIXES[from_type]}:{from_id} -{rel_type}-> "
        f"{_PREFIXES[to_type]}:{to_id} by={actor_id} why={textnorm.q(why or '')}"
        for rel_id, from_type, from_id, rel_type, to_type, to_id, actor_id, why in rows
    ]


def confirm(con: Connection, actor: Actor, rel_ids: list[int], interactive: bool) -> list[str]:
    """사람이 대화형 세션에서 대기 관계를 확정한다."""
    actors.require_human(actor, interactive)
    results: list[str] = []
    for rel_id in rel_ids:
        try:
            if isinstance(rel_id, bool) or not isinstance(rel_id, int) or rel_id < 1:
                raise DocdbError("BAD_ARGS", "rel_id는 양의 정수여야 함")
            with db.write_tx(con):
                before = _snapshot(con, rel_id)
                if before is None:
                    raise DocdbError("NOT_FOUND", f"관계를 찾을 수 없음: {rel_id}")
                if before["status"] != "proposed":
                    raise DocdbError("BAD_OP", "대기 관계만 확정할 수 있음")
                decided_at = clock.now_iso()
                _execute(
                    con,
                    "UPDATE relation SET status='confirmed', decided_by=?, decided_at=? "
                    "WHERE rel_id=?",
                    (actor.id, decided_at, rel_id),
                )
                changed_before = {
                    key: before[key] for key in ("status", "decided_by", "decided_at")
                }
                changed_after = {
                    "status": "confirmed",
                    "decided_by": actor.id,
                    "decided_at": decided_at,
                }
                _record(con, rel_id, "update", changed_before, changed_after, actor, None)
            results.append(f"OK rel={rel_id} confirmed")
        except DocdbError as exc:
            results.append(_error_line(rel_id, exc))
    return results


def reject(con: Connection, actor: Actor, rel_id: int, why: str, interactive: bool) -> str:
    """사람이 대화형 세션에서 대기 관계를 반려하고 이유를 기록한다."""
    actors.require_human(actor, interactive)
    if isinstance(rel_id, bool) or not isinstance(rel_id, int) or rel_id < 1:
        raise DocdbError("BAD_ARGS", "rel_id는 양의 정수여야 함")
    normalized_why = _why(con, f"rel:{rel_id}", why)
    with db.write_tx(con):
        before = _snapshot(con, rel_id)
        if before is None:
            raise DocdbError("NOT_FOUND", f"관계를 찾을 수 없음: {rel_id}")
        if before["status"] != "proposed":
            raise DocdbError("BAD_OP", "대기 관계만 반려할 수 있음")
        decided_at = clock.now_iso()
        _execute(
            con,
            "UPDATE relation SET status='rejected', reason=?, decided_by=?, decided_at=? "
            "WHERE rel_id=?",
            (normalized_why, actor.id, decided_at, rel_id),
        )
        changed_before = {
            key: before[key] for key in ("status", "reason", "decided_by", "decided_at")
        }
        changed_after = {
            "status": "rejected",
            "reason": normalized_why,
            "decided_by": actor.id,
            "decided_at": decided_at,
        }
        _record(con, rel_id, "update", changed_before, changed_after, actor, normalized_why)
    return f"OK rel={rel_id} rejected"


def remove(con: Connection, actor: Actor, rel_id: int, why: str, interactive: bool) -> str:
    """사람이 대화형 세션에서 규칙 관계가 아닌 관계를 삭제한다."""
    actors.require_human(actor, interactive)
    if isinstance(rel_id, bool) or not isinstance(rel_id, int) or rel_id < 1:
        raise DocdbError("BAD_ARGS", "rel_id는 양의 정수여야 함")
    normalized_why = _why(con, f"rel:{rel_id}", why)
    with db.write_tx(con):
        before = _snapshot(con, rel_id)
        if before is None:
            raise DocdbError("NOT_FOUND", f"관계를 찾을 수 없음: {rel_id}")
        if before["source"].startswith("rule:"):
            raise DocdbError("BAD_OP", "규칙 관계는 원본 본문을 고쳐서 바꿈")
        _execute(con, "DELETE FROM relation WHERE rel_id=?", (rel_id,))
        _record(con, rel_id, "delete", before, None, actor, normalized_why)
    return f"OK rel={rel_id} removed"


def pending_count(con: Connection, work_id: str | None = None) -> int:
    """확정 대기 관계 수를 전체 또는 삽입 작업별로 반환한다."""
    clauses = ["status='proposed'"]
    args: list[str] = []
    if work_id is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM change_log c WHERE c.entity_type='relation' "
            "AND c.entity_id=CAST(relation.rel_id AS TEXT) AND c.op='insert' AND c.work_id=? "
            "AND c.change_id=(SELECT max(c2.change_id) FROM change_log c2 "
            "WHERE c2.entity_type='relation' AND c2.entity_id=CAST(relation.rel_id AS TEXT) "
            "AND c2.op='insert'))"
        )
        args.append(work_id)
    row = _execute(
        con,
        "SELECT count(*) FROM relation WHERE " + " AND ".join(clauses),
        args,
    ).fetchone()
    return int(row[0])
