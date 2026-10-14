"""작업 상태와 인계 자료의 생성·변경을 처리한다."""
import json
import re

from docdb import clock, db, history, ids, policy, textnorm
from docdb.actors import ACTOR_PATTERNS, Actor, require_human
from docdb.db import Connection
from docdb.errors import DocdbError

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "planned": ("active", "cancelled"),
    "active": ("blocked", "review", "cancelled"),
    "blocked": ("active", "cancelled"),
    "review": ("active", "verified"),
    "verified": ("closed", "active"),
    "closed": (),
    "cancelled": (),
}
AI_ALLOWED: frozenset[str] = frozenset({"planned", "active", "blocked", "review"})
_BASE_REF = re.compile(r"^(pmt|git|doc):.+$", re.ASCII)
_WORK_TYPE = re.compile(r"^[a-z_]+$", re.ASCII)
_HANDOFF_LISTS = ("done", "undone", "risks", "next_actions", "not_run")


def _execute(con: Connection, sql: str, args: tuple = ()):
    """DB 오류를 계약 오류로 바꾸어 SQL을 실행한다."""
    try:
        return con.execute(sql, args)
    except db.DatabaseError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc


def create_work(
    con: Connection,
    actor: Actor,
    title: str,
    work_type: str,
    owner: str,
    base_refs: list[str],
    *,
    reason: str | None = None,
) -> str:
    """작업을 planned 상태로 만들고 발급한 ID를 반환한다."""
    if not isinstance(title, str):
        raise DocdbError("BAD_ARGS", "작업 제목은 문자열이어야 함")
    normalized_title = textnorm.normalize_text(title)
    if (
        not normalized_title
        or not normalized_title.strip()
        or not isinstance(work_type, str)
        or not _WORK_TYPE.fullmatch(work_type)
    ):
        raise DocdbError("BAD_ARGS", "작업 제목 또는 종류가 올바르지 않음")
    if not isinstance(owner, str) or not re.fullmatch(
        ACTOR_PATTERNS["human"], owner, re.ASCII
    ):
        raise DocdbError("BAD_ARGS", "작업 소유자는 사람 행위자여야 함")
    if not isinstance(base_refs, list) or any(
        not isinstance(ref, str) or not _BASE_REF.fullmatch(ref) for ref in base_refs
    ):
        raise DocdbError("BAD_ARGS", "기준 참조 형식이 올바르지 않음")
    if reason is not None and not isinstance(reason, str):
        raise DocdbError("BAD_ARGS", "reason은 문자열이어야 함")
    normalized_reason = textnorm.normalize_text(reason)

    with db.write_tx(con):
        owner_row = _execute(
            con, "SELECT actor_type, active FROM actor WHERE actor_id=?", (owner,)
        ).fetchone()
        if owner_row is None or owner_row[0] != "human" or not owner_row[1]:
            raise DocdbError("BAD_ARGS", "작업 소유자는 등록된 사람 행위자여야 함")
        project_row = _execute(con, "SELECT project_id FROM project LIMIT 1").fetchone()
        if project_row is None:
            raise DocdbError("NOT_FOUND", "프로젝트를 찾을 수 없음")

        work_id = ids.next_id(con, "work")
        refs_json = textnorm.canonical_json(base_refs)
        policy.assert_no_secrets(
            con,
            f"w:{work_id}",
            [normalized_title, work_type, owner, refs_json, normalized_reason],
        )
        opened_at = clock.now_iso()
        after = {
            "work_id": work_id,
            "project_id": project_row[0],
            "work_type": work_type,
            "title": normalized_title,
            "status": "planned",
            "owner_id": owner,
            "base_refs": base_refs,
            "result_refs": [],
            "opened_at": opened_at,
            "closed_at": None,
        }
        _execute(
            con,
            "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, "
            "base_refs, result_refs, opened_at, closed_at) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, NULL)",
            (
                work_id,
                project_row[0],
                work_type,
                normalized_title,
                "planned",
                owner,
                refs_json,
                opened_at,
            ),
        )
        history.append_change(
            con,
            "work",
            work_id,
            "insert",
            None,
            after,
            actor.id,
            work_id,
            normalized_reason,
        )
    return work_id


def set_status(
    con: Connection,
    actor: Actor,
    work_id: str,
    status: str,
    interactive: bool,
) -> str:
    """전이표와 행위자 권한을 검사해 작업 상태를 바꾼다."""
    return _set_status_with_reason(con, actor, work_id, status, interactive, None)


def _set_status_with_reason(
    con: Connection,
    actor: Actor,
    work_id: str,
    status: str,
    interactive: bool,
    reason: str | None,
) -> str:
    """이력 이유를 함께 저장해 작업 상태를 바꾼다."""
    ids.validate("work", work_id)
    with db.write_tx(con):
        row = _execute(
            con, "SELECT status, closed_at FROM work WHERE work_id=?", (work_id,)
        ).fetchone()
        if row is None:
            raise DocdbError("NOT_FOUND", f"작업을 찾을 수 없음: {work_id}")
        current, current_closed_at = row
        if status not in TRANSITIONS.get(current, ()):
            raise DocdbError("STATUS_FORBIDDEN", f"허용되지 않는 상태 전이: {current} → {status}")
        if status not in AI_ALLOWED:
            require_human(actor, interactive)

        closed_at = clock.now_iso() if status == "closed" else current_closed_at
        _execute(
            con,
            "UPDATE work SET status=?, closed_at=? WHERE work_id=?",
            (status, closed_at, work_id),
        )
        history.append_change(
            con,
            "work",
            work_id,
            "update",
            {"status": current, "closed_at": current_closed_at},
            {"status": status, "closed_at": closed_at},
            actor.id,
            work_id,
            reason,
        )
    return f"OK w:{work_id} status={status}"


def create_handoff(
    con: Connection,
    actor: Actor,
    work_id: str,
    from_role: str,
    to_role: str,
    payload: dict,
    *,
    reason: str | None = None,
) -> str:
    """작업에 인계 자료를 추가하고 발급한 인계 ID를 반환한다."""
    ids.validate("work", work_id)
    if (
        not isinstance(from_role, str)
        or not from_role
        or not isinstance(to_role, str)
        or not to_role
        or not isinstance(payload, dict)
        or any(not isinstance(payload.get(key), list) for key in _HANDOFF_LISTS)
        or type(payload.get("as_of")) is not int
    ):
        raise DocdbError("BAD_OP", "인계 자료에 다섯 목록과 정수 as_of가 필요함")

    normalized_from = textnorm.normalize_text(from_role)
    normalized_to = textnorm.normalize_text(to_role)
    if not normalized_from.strip() or not normalized_to.strip():
        raise DocdbError("BAD_OP", "인계 역할이 비어 있음")
    if reason is not None and not isinstance(reason, str):
        raise DocdbError("BAD_ARGS", "reason은 문자열이어야 함")
    normalized_reason = textnorm.normalize_text(reason)
    payload_json = textnorm.canonical_json(payload)
    with db.write_tx(con):
        work_row = _execute(
            con, "SELECT 1 FROM work WHERE work_id=?", (work_id,)
        ).fetchone()
        if work_row is None:
            raise DocdbError("NOT_FOUND", f"작업을 찾을 수 없음: {work_id}")
        handoff_id = ids.next_id(con, "handoff")
        policy.assert_no_secrets(
            con,
            f"w:{work_id}",
            [normalized_from, normalized_to, payload_json, normalized_reason],
        )
        created_at = clock.now_iso()
        payload_value = json.loads(payload_json)
        after = {
            "handoff_id": handoff_id,
            "work_id": work_id,
            "from_role": normalized_from,
            "to_role": normalized_to,
            "payload": payload_value,
            "ack_status": "pending",
            "ack_by": None,
            "ack_at": None,
            "ack_checked_baseline": None,
            "ack_checked_evidence": [],
            "created_at": created_at,
        }
        _execute(
            con,
            "INSERT INTO handoff (handoff_id, work_id, from_role, to_role, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (handoff_id, work_id, normalized_from, normalized_to, payload_json, created_at),
        )
        history.append_change(
            con,
            "handoff",
            handoff_id,
            "insert",
            None,
            after,
            actor.id,
            work_id,
            normalized_reason,
        )
    return f"OK handoff={handoff_id}"


def ack_handoff(
    con: Connection,
    actor: Actor,
    handoff_id: str,
    baseline: str,
    evidence: list[str],
    accept: bool,
    interactive: bool,
) -> str:
    """사람이 확인한 기준과 증거를 저장해 인계를 수락하거나 반려한다."""
    require_human(actor, interactive)
    ids.validate("handoff", handoff_id)
    if not isinstance(baseline, str) or not baseline.strip():
        raise DocdbError("BAD_ARGS", "확인한 기준(baseline)이 필요함")
    if not isinstance(evidence, list) or any(not isinstance(ref, str) for ref in evidence):
        raise DocdbError("BAD_ARGS", "증거는 문자열 목록이어야 함")
    normalized_baseline = textnorm.normalize_text(baseline)
    evidence_json = textnorm.canonical_json(evidence)
    ack_status = "accepted" if accept else "rejected"
    with db.write_tx(con):
        row = _execute(
            con,
            "SELECT work_id, ack_status, ack_by, ack_at, ack_checked_baseline, "
            "ack_checked_evidence FROM handoff WHERE handoff_id=?",
            (handoff_id,),
        ).fetchone()
        if row is None:
            raise DocdbError("NOT_FOUND", f"인계를 찾을 수 없음: {handoff_id}")
        work_id, current_status, current_by, current_at, current_baseline, current_evidence = row
        if current_status != "pending":
            raise DocdbError("BAD_ARGS", "이미 확인한 인계임")
        policy.assert_no_secrets(
            con, f"ho:{handoff_id}", [normalized_baseline, evidence_json]
        )
        ack_at = clock.now_iso()
        _execute(
            con,
            "UPDATE handoff SET ack_status=?, ack_by=?, ack_at=?, ack_checked_baseline=?, "
            "ack_checked_evidence=? WHERE handoff_id=?",
            (
                ack_status,
                actor.id,
                ack_at,
                normalized_baseline,
                evidence_json,
                handoff_id,
            ),
        )
        before = {
            "ack_status": current_status,
            "ack_by": current_by,
            "ack_at": current_at,
            "ack_checked_baseline": current_baseline,
            "ack_checked_evidence": json.loads(current_evidence),
        }
        after = {
            "ack_status": ack_status,
            "ack_by": actor.id,
            "ack_at": ack_at,
            "ack_checked_baseline": normalized_baseline,
            "ack_checked_evidence": json.loads(evidence_json),
        }
        history.append_change(
            con,
            "handoff",
            handoff_id,
            "update",
            before,
            after,
            actor.id,
            work_id,
            None,
        )
    return f"OK handoff={handoff_id} {ack_status}"
