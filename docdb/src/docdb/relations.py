"""관계 제안, 사람 검토와 변경 이력을 관리한다."""
import json
import re

from docdb import actors, clock, db, history, ids, policy, textnorm
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


def missing_links(
    con: Connection, doc_ids: list[str] | None = None
) -> list[tuple[str, str]]:
    """필수 연결이 누락된 문서와 대상 종류를 반환한다."""
    if doc_ids is not None and not doc_ids:
        return []
    sql = "SELECT doc_id, target_kind_id FROM v_missing_required_links"
    args: list[str] = []
    if doc_ids is not None:
        sql += " WHERE doc_id IN (" + ",".join("?" for _ in doc_ids) + ")"
        args.extend(doc_ids)
    rows = _execute(con, sql + " ORDER BY doc_id, target_kind_id", args).fetchall()
    return [(doc_id, kind_id) for doc_id, kind_id in rows]


_R1_TITLE_STRIP = re.compile(r"^[\s:|\-–—.)\]]+")


def _item_types(con: Connection) -> list[tuple[str, str, str, str | None]]:
    """카탈로그의 항목 종류·접두어·정의 필드 정보를 가져온다."""
    return _execute(
        con,
        "SELECT item_type, id_prefix, defined_by_kind, defined_by_field "
        "FROM cat_item_type ORDER BY length(id_prefix) DESC",
    ).fetchall()


def _id_pattern(item_types: list[tuple[str, str, str, str | None]]) -> re.Pattern | None:
    """카탈로그 접두어로 계약의 항목 ID 정규식을 만든다."""
    prefixes = sorted({row[1] for row in item_types}, key=len, reverse=True)
    if not prefixes:
        return None
    choices = "|".join(re.escape(prefix) for prefix in prefixes)
    return re.compile(r"\b(?:" + choices + r")[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.ASCII)


def _item_type(item_id: str, item_types: list[tuple[str, str, str, str | None]]) -> str | None:
    """가장 긴 일치 접두어의 항목 종류를 반환한다."""
    for item_type, prefix, _kind_id, _field_key in item_types:
        if item_id.startswith(prefix):
            return item_type
    return None


def _matched_ids(
    text: str | None,
    pattern: re.Pattern | None,
    known: set[str],
) -> set[str]:
    """본문에서 알려진 항목 ID만 추출한다."""
    if not text or pattern is None:
        return set()
    return {match.group(0) for match in pattern.finditer(text) if match.group(0) in known}


def _rule_relation(
    con: Connection,
    actor: Actor,
    from_type: str,
    from_id: str,
    rel_type: str,
    to_type: str,
    to_id: str,
    rule: str,
    anchor: str,
    work_id: str | None,
) -> None:
    """같은 끝점 관계가 없을 때 확정 규칙 관계와 삽입 이력을 기록한다."""
    duplicate = _execute(
        con,
        "SELECT 1 FROM relation WHERE from_type=? AND from_id=? AND rel_type=? "
        "AND to_type=? AND to_id=?",
        (from_type, from_id, rel_type, to_type, to_id),
    ).fetchone()
    if duplicate is not None:
        return

    source = f"rule:{rule}"
    created_at = clock.now_iso()
    cursor = _execute(
        con,
        "INSERT INTO relation (from_type, from_id, rel_type, to_type, to_id, anchor, reason, "
        "created_at, status, source, proposed_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, NULL)",
        (
            from_type,
            from_id,
            rel_type,
            to_type,
            to_id,
            anchor,
            source,
            created_at,
            source,
        ),
    )
    rel_id = int(cursor.lastrowid)
    after = {
        "from_type": from_type,
        "from_id": from_id,
        "rel_type": rel_type,
        "to_type": to_type,
        "to_id": to_id,
        "is_required": 0,
        "target_revision_id": None,
        "anchor": anchor,
        "reason": source,
        "created_at": created_at,
        "status": "confirmed",
        "source": source,
        "proposed_by": None,
        "decided_by": None,
        "decided_at": None,
    }
    _record(con, rel_id, "insert", None, after, actor, source, work_id)


def _sync_rule_relations(
    con: Connection,
    actor: Actor,
    from_type: str,
    from_id: str,
    rel_type: str,
    to_type: str,
    desired: set[str],
    rule: str,
    anchor: str,
    work_id: str | None,
) -> None:
    """규칙·anchor가 소유한 관계를 원하는 도착 ID 집합에 맞춘다."""
    source = f"rule:{rule}"
    rows = _execute(
        con,
        "SELECT rel_id, to_id FROM relation WHERE from_type=? AND from_id=? AND rel_type=? "
        "AND to_type=? AND source=? AND anchor=?",
        (from_type, from_id, rel_type, to_type, source, anchor),
    ).fetchall()
    existing = {to_id: rel_id for rel_id, to_id in rows}
    for rel_id, to_id in rows:
        if to_id in desired:
            continue
        before = _snapshot(con, rel_id)
        if before is None:
            continue
        _execute(con, "DELETE FROM relation WHERE rel_id=?", (rel_id,))
        _record(con, rel_id, "delete", before, None, actor, source)

    for to_id in sorted(desired - set(existing)):
        _rule_relation(
            con,
            actor,
            from_type,
            from_id,
            rel_type,
            to_type,
            to_id,
            rule,
            anchor,
            work_id,
        )


def _r1(
    con: Connection,
    actor: Actor,
    doc_id: str,
    kind_id: str,
    field_key: str,
    text: str | None,
    work_id: str | None,
    item_types: list[tuple[str, str, str, str | None]],
) -> list[str]:
    """정의 필드의 항목을 만들거나 제목을 갱신하고 건너뛴 ID를 알린다."""
    targets = {
        item_type: prefix
        for item_type, prefix, defined_kind, defined_field in item_types
        if defined_kind == kind_id and defined_field == field_key
    }
    if not targets or not text:
        return []
    pattern = _id_pattern(item_types)
    if pattern is None:
        return []

    doc = _execute(con, "SELECT project_id FROM document WHERE doc_id=?", (doc_id,)).fetchone()
    if doc is None:
        raise DocdbError("NOT_FOUND", f"문서를 찾을 수 없음: {doc_id}")
    notes: list[str] = []
    for line in text.splitlines():
        for match in pattern.finditer(line):
            item_id = match.group(0)
            item_type = _item_type(item_id, item_types)
            if item_type not in targets:
                continue
            title = _R1_TITLE_STRIP.sub("", line[match.end() :]).strip()[:200] or item_id
            title = textnorm.normalize_text(title) or item_id
            current = _execute(
                con,
                "SELECT item_type, title, status, defined_in, attrs FROM item WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if current is not None and current[3] not in (None, doc_id):
                notes.append(f"NOTE R1 skip {item_id} defined_in={current[3] or ''}")
                continue

            handle = f"i:{item_id}"
            policy.assert_no_secrets(con, handle, [title])
            attrs = json.loads(current[4]) if current is not None else {}
            status = current[2] if current is not None else None
            canon = textnorm.canonical_json(
                {
                    "title": title,
                    "status": status,
                    "attrs": attrs,
                    "defined_in": f"d:{doc_id}",
                }
            )
            digest = textnorm.digest16(canon)
            token_est = textnorm.tok_est(canon)
            now = clock.now_iso()
            if current is None:
                ids.validate("item", item_id)
                ids.ensure_not_reused(con, "item", item_id)
                _execute(
                    con,
                    "INSERT INTO item (item_id, project_id, item_type, title, status, defined_in, "
                    "attrs, created_at, updated_at, digest, tok_est) "
                    "VALUES (?, ?, ?, ?, NULL, ?, '{}', ?, ?, ?, ?)",
                    (item_id, doc[0], item_type, title, doc_id, now, now, digest, token_est),
                )
                history.append_change(
                    con,
                    "item",
                    item_id,
                    "insert",
                    None,
                    {
                        "title": title,
                        "status": None,
                        "attrs": {},
                        "defined_in": doc_id,
                        "item_type": item_type,
                    },
                    actor.id,
                    work_id,
                    "rule:R1",
                )
            elif current[1] != title or current[3] is None:
                _execute(
                    con,
                    "UPDATE item SET title=?, defined_in=?, updated_at=?, digest=?, tok_est=? "
                    "WHERE item_id=?",
                    (title, doc_id, now, digest, token_est, item_id),
                )
                history.append_change(
                    con,
                    "item",
                    item_id,
                    "update",
                    {
                        "title": current[1],
                        "status": status,
                        "attrs": attrs,
                        "defined_in": current[3],
                    },
                    {
                        "title": title,
                        "status": status,
                        "attrs": attrs,
                        "defined_in": doc_id,
                    },
                    actor.id,
                    work_id,
                    "rule:R1",
                )
    return notes


def _r2(
    con: Connection,
    actor: Actor,
    doc_id: str,
    work_id: str | None,
    item_types: list[tuple[str, str, str, str | None]],
) -> None:
    """문서의 모든 필드에 언급된 외부 정의 항목의 R2 관계를 맞춘다."""
    rows = _execute(con, "SELECT item_id, defined_in FROM item").fetchall()
    known = {item_id for item_id, _defined_in in rows}
    defined_in = {item_id: owner for item_id, owner in rows}
    pattern = _id_pattern(item_types)
    desired: set[str] = set()
    fields = _execute(
        con,
        "SELECT value_text FROM field_value WHERE doc_id=? AND value_text IS NOT NULL",
        (doc_id,),
    ).fetchall()
    for (value,) in fields:
        for item_id in _matched_ids(value, pattern, known):
            if defined_in[item_id] != doc_id:
                desired.add(item_id)
    _sync_rule_relations(
        con,
        actor,
        "document",
        doc_id,
        "references",
        "item",
        desired,
        "R2",
        f"d:{doc_id}",
        work_id,
    )


def _r3(
    con: Connection,
    actor: Actor,
    doc_id: str,
    kind_id: str,
    field_key: str,
    text: str | None,
    work_id: str | None,
    item_types: list[tuple[str, str, str, str | None]],
) -> None:
    """추출 규칙이 지정한 같은 줄 항목 쌍의 R3 관계를 맞춘다."""
    row = _execute(
        con,
        "SELECT extract_rel FROM cat_kind_field WHERE kind_id=? AND field_key=?",
        (kind_id, field_key),
    ).fetchone()
    if row is None or row[0] is None:
        return
    spec = json.loads(row[0])
    rel_type = spec.get("rel")
    from_prefix = spec.get("from_prefix")
    to_prefix = spec.get("to_prefix")
    if (
        rel_type not in REL_TYPES
        or not isinstance(from_prefix, str)
        or not isinstance(to_prefix, str)
    ):
        return
    from_allowed, to_allowed = _ENDPOINTS[rel_type]
    if "item" not in from_allowed or "item" not in to_allowed:
        return

    items = _execute(con, "SELECT item_id FROM item").fetchall()
    known = {item_id for (item_id,) in items}
    pattern = _id_pattern(item_types)
    desired: set[tuple[str, str, str]] = set()
    if text and pattern is not None:
        for line in text.splitlines():
            ids_on_line = _matched_ids(line, pattern, known)
            from_ids = sorted(item_id for item_id in ids_on_line if item_id.startswith(from_prefix))
            to_ids = sorted(item_id for item_id in ids_on_line if item_id.startswith(to_prefix))
            for from_id in from_ids:
                for to_id in to_ids:
                    desired.add((from_id, rel_type, to_id))

    rows = _execute(
        con,
        "SELECT rel_id, from_id, rel_type, to_id FROM relation WHERE from_type='item' "
        "AND to_type='item' AND source='rule:R3' AND anchor=?",
        (f"f:{doc_id}/{kind_id}/{field_key}",),
    ).fetchall()
    existing = {
        (from_id, existing_rel, to_id): rel_id
        for rel_id, from_id, existing_rel, to_id in rows
    }
    for relation_key, rel_id in existing.items():
        if relation_key in desired:
            continue
        before = _snapshot(con, rel_id)
        if before is None:
            continue
        _execute(con, "DELETE FROM relation WHERE rel_id=?", (rel_id,))
        _record(con, rel_id, "delete", before, None, actor, "rule:R3")
    for from_id, desired_rel, to_id in sorted(desired - set(existing)):
        _rule_relation(
            con,
            actor,
            "item",
            from_id,
            desired_rel,
            "item",
            to_id,
            "R3",
            f"f:{doc_id}/{kind_id}/{field_key}",
            work_id,
        )


def after_field_write(
    con: Connection,
    actor: Actor,
    doc_id: str,
    kind_id: str,
    field_key: str,
    text: str | None,
    work_id: str | None,
) -> list[str]:
    """필드 저장 뒤 R1·R2·R3·R5를 실행하고 안내 줄을 돌려준다."""
    with db.write_tx(con):
        item_types = _item_types(con)
        notes = _r1(con, actor, doc_id, kind_id, field_key, text, work_id, item_types)
        _r2(con, actor, doc_id, work_id, item_types)
        _r3(con, actor, doc_id, kind_id, field_key, text, work_id, item_types)
        if work_id:
            _rule_relation(
                con,
                actor,
                "work",
                work_id,
                "changes",
                "document",
                doc_id,
                "R5",
                f"w:{work_id}",
                work_id,
            )
        return notes


def after_item_write(
    con: Connection,
    actor: Actor,
    item_id: str,
    work_id: str | None,
) -> list[str]:
    """항목 저장 뒤 작업 문맥의 R5 변경 관계를 기록한다."""
    if not work_id:
        return []
    with db.write_tx(con):
        _rule_relation(
            con,
            actor,
            "work",
            work_id,
            "changes",
            "item",
            item_id,
            "R5",
            f"w:{work_id}",
            work_id,
        )
    return []


def apply_run_coverage(
    con: Connection,
    actor: Actor,
    run_id: str,
    item_ids: list[str],
) -> None:
    """실행 결과의 covers 항목에 대해 R4 evidence_for 규칙 관계를 기록한다."""
    row = _execute(con, "SELECT work_id FROM run WHERE run_id=?", (run_id,)).fetchone()
    work_id = row[0] if row is not None else None
    with db.write_tx(con):
        for item_id in sorted(item_ids):
            _rule_relation(
                con,
                actor,
                "run",
                run_id,
                "evidence_for",
                "item",
                item_id,
                "R4",
                f"r:{run_id}",
                work_id,
            )
