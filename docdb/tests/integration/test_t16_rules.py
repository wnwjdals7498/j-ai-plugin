"""T16 규칙 추출 R1·R2·R3·R5 통합 테스트."""
import json

from docdb import db, relations
from docdb.actors import Actor
from docdb.llm import DocDB

NOW = "2026-10-13T00:00:00+00:00"


def _document(con, doc_id, kind_id):
    """시작 카탈로그의 종류를 연결한 테스트 문서를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', ?, 'normative', 'desired', 'u-tester', ?, ?)",
        (doc_id, doc_id, kind_id[0], NOW, NOW),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, ?)", (doc_id, kind_id)
    )


def _put(con, doc_id, kind_id, field_key, text, work=None):
    """필드 하나를 에이전트 put으로 기록한다."""
    return DocDB(con, Actor("ai-claude", "ai"), work).put(
        [{"h": f"f:{doc_id}/{kind_id}/{field_key}", "op": "set", "text": text}]
    )


def _item(con, item_id, title, defined_in=None):
    """기존 항목을 에이전트 put으로 만든다."""
    patch = {"h": f"i:{item_id}", "op": "upsert", "title": title}
    if defined_in is not None:
        patch["defined_in"] = f"d:{defined_in}"
    return DocDB(con, Actor("ai-claude", "ai")).put([patch])


def test_t16_1_definition_extracts_items_and_records_rule_reason(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        result = _put(
            con,
            "DOC-REQ-HW",
            "D01",
            "requirement_ids",
            "REQ-HW-001: 디스크 경고\nREQ-HW-002 - 메모리 경고",
        )
        assert "APPLIED ok=1 err=0" in result
        assert con.execute(
            "SELECT item_id, title, defined_in FROM item WHERE item_id LIKE 'REQ-HW-%' "
            "ORDER BY item_id"
        ).fetchall() == [
            ("REQ-HW-001", "디스크 경고", "DOC-REQ-HW"),
            ("REQ-HW-002", "메모리 경고", "DOC-REQ-HW"),
        ]
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='item' AND op='insert' "
            "ORDER BY entity_id"
        ).fetchall() == [("rule:R1",), ("rule:R1",)]
    finally:
        con.close()


def test_t16_2_definition_updates_title_without_deleting_missing_item(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 경고\nREQ-HW-002: 메모리 경고")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 사용률 경고")
        assert con.execute(
            "SELECT item_id, title FROM item WHERE item_id LIKE 'REQ-HW-%' ORDER BY item_id"
        ).fetchall() == [
            ("REQ-HW-001", "디스크 사용률 경고"),
            ("REQ-HW-002", "메모리 경고"),
        ]
    finally:
        con.close()


def test_t16_3_other_document_definition_is_skipped_with_note(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _document(con, "DOC-REQ-HW-OTHER", "D01")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 원래 제목")
        result = _put(
            con, "DOC-REQ-HW-OTHER", "D01", "requirement_ids", "REQ-HW-001: 새 제목"
        )
        assert [
            line for line in result.splitlines() if line.startswith("NOTE R1")
        ] == ["NOTE R1 skip REQ-HW-001 defined_in=DOC-REQ-HW"]
        assert con.execute(
            "SELECT title, defined_in FROM item WHERE item_id='REQ-HW-001'"
        ).fetchone() == ("원래 제목", "DOC-REQ-HW")

        counts_before = tuple(
            con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("item", "relation", "change_log")
        )
        notes = relations.after_field_write(
            con,
            Actor("ai-claude", "ai"),
            "DOC-REQ-HW-OTHER",
            "D01",
            "requirement_ids",
            "REQ-HW-001: 새 제목",
            None,
        )
        counts_after = tuple(
            con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("item", "relation", "change_log")
        )
        assert counts_after == counts_before
        assert notes == ["NOTE R1 skip REQ-HW-001 defined_in=DOC-REQ-HW"]
    finally:
        con.close()


def test_t16_3_null_defined_in_item_is_adopted_by_definition(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _item(con, "REQ-HW-003", "에이전트 생성 항목")
        assert con.execute(
            "SELECT defined_in FROM item WHERE item_id='REQ-HW-003'"
        ).fetchone() == (None,)

        result = _put(
            con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-003: 문서 정의 제목"
        )

        assert not any(line.startswith("NOTE R1") for line in result.splitlines())
        assert con.execute(
            "SELECT title, defined_in FROM item WHERE item_id='REQ-HW-003'"
        ).fetchone() == ("문서 정의 제목", "DOC-REQ-HW")
        row = con.execute(
            "SELECT op, before_json, after_json, reason FROM change_log "
            "WHERE entity_type='item' AND entity_id='REQ-HW-003' ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
        assert row[0] == "update"
        assert json.loads(row[1])["defined_in"] is None
        assert json.loads(row[2])["defined_in"] == "DOC-REQ-HW"
        assert json.loads(row[1])["title"] == "에이전트 생성 항목"
        assert json.loads(row[2])["title"] == "문서 정의 제목"
        assert row[3] == "rule:R1"
    finally:
        con.close()


def test_t16_4_external_existing_item_mention_creates_r2_relation(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _document(con, "DOC-K02", "K02")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 경고")
        _put(con, "DOC-K02", "K02", "overview", "대상 REQ-HW-001")
        assert con.execute(
            "SELECT from_type, from_id, rel_type, to_type, to_id, source, status, anchor "
            "FROM relation"
        ).fetchall() == [
            ("document", "DOC-K02", "references", "item", "REQ-HW-001", "rule:R2", "confirmed", "d:DOC-K02")
        ]
    finally:
        con.close()


def test_t16_5_removed_mention_deletes_r2_relation_with_history(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _document(con, "DOC-K02", "K02")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 경고")
        client = DocDB(con, Actor("ai-claude", "ai"))
        cursor = client.cursor()
        _put(con, "DOC-K02", "K02", "overview", "대상 REQ-HW-001")
        rel_id = con.execute("SELECT rel_id FROM relation").fetchone()[0]
        link_result = client.put(
            [
                {
                    "op": "link",
                    "from": "d:DOC-K02",
                    "rel": "references",
                    "to": "d:DOC-REQ-HW",
                    "why": "문서 간 제안 관계",
                }
            ]
        )
        assert "proposed" in link_result
        agent_rel_id = con.execute(
            "SELECT rel_id FROM relation WHERE source='agent'"
        ).fetchone()[0]

        _put(con, "DOC-K02", "K02", "overview", "대상 자원")
        assert con.execute("SELECT 1 FROM relation WHERE rel_id=?", (rel_id,)).fetchone() is None
        row = con.execute(
            "SELECT op, reason, before_json, after_json FROM change_log "
            "WHERE entity_type='relation' AND entity_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (str(rel_id),),
        ).fetchone()
        assert row[:2] == ("delete", "rule:R2")
        before = json.loads(row[2])
        assert (
            before["from_type"],
            before["from_id"],
            before["rel_type"],
            before["to_type"],
            before["to_id"],
            before["source"],
            before["anchor"],
        ) == (
            "document",
            "DOC-K02",
            "references",
            "item",
            "REQ-HW-001",
            "rule:R2",
            "d:DOC-K02",
        )
        assert row[3] is None

        since = client.since(cursor)
        since_lines = since.splitlines()
        assert not any(line.startswith(f"relation:{rel_id} ") for line in since_lines)
        agent_line = next(
            line for line in since_lines if line.startswith(f"relation:{agent_rel_id} ")
        )
        assert agent_line.startswith(f"relation:{agent_rel_id} x1 ")
    finally:
        con.close()


def test_t16_6_unknown_and_self_defined_items_do_not_create_r2(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-K02", "K02")
        _item(con, "REQ-HW-010", "K02 자체 항목", "DOC-K02")
        _put(con, "DOC-K02", "K02", "overview", "REQ-HW-999와 REQ-HW-010")
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
    finally:
        con.close()


def test_t16_7_test_definition_and_same_line_pair_create_r3(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _document(con, "DOC-TC-HW", "L02")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 경고")
        _put(con, "DOC-TC-HW", "L02", "test_ids", "TC-HW-001 REQ-HW-001 디스크 경고 확인")
        assert con.execute(
            "SELECT item_id, item_type, title, defined_in FROM item WHERE item_id='TC-HW-001'"
        ).fetchone() == (
            "TC-HW-001",
            "test_case",
            "REQ-HW-001 디스크 경고 확인",
            "DOC-TC-HW",
        )
        assert con.execute(
            "SELECT from_id, rel_type, to_id, source, status, anchor FROM relation "
            "WHERE source='rule:R3'"
        ).fetchall() == [
            ("REQ-HW-001", "verified_by", "TC-HW-001", "rule:R3", "confirmed", "f:DOC-TC-HW/L02/test_ids")
        ]
        assert con.execute(
            "SELECT verified_by FROM v_rtm WHERE requirement_id='REQ-HW-001'"
        ).fetchone() == ("TC-HW-001",)
    finally:
        con.close()


def test_t16_8_removing_pair_member_deletes_r3_relation_with_history(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _document(con, "DOC-TC-HW", "L02")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 경고")
        _put(con, "DOC-TC-HW", "L02", "test_ids", "TC-HW-001 REQ-HW-001 디스크 경고 확인")
        rel_id = con.execute(
            "SELECT rel_id FROM relation WHERE source='rule:R3'"
        ).fetchone()[0]
        _put(con, "DOC-TC-HW", "L02", "test_ids", "TC-HW-001 디스크 경고 확인")
        assert con.execute("SELECT 1 FROM relation WHERE rel_id=?", (rel_id,)).fetchone() is None
        row = con.execute(
            "SELECT op, reason, before_json, after_json FROM change_log "
            "WHERE entity_type='relation' AND entity_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (str(rel_id),),
        ).fetchone()
        assert row[:2] == ("delete", "rule:R3")
        before = json.loads(row[2])
        assert (
            before["from_type"],
            before["from_id"],
            before["rel_type"],
            before["to_type"],
            before["to_id"],
            before["source"],
            before["anchor"],
        ) == (
            "item",
            "REQ-HW-001",
            "verified_by",
            "item",
            "TC-HW-001",
            "rule:R3",
            "f:DOC-TC-HW/L02/test_ids",
        )
        assert row[3] is None
    finally:
        con.close()


def test_t16_9_two_field_writes_create_one_r5_work_relation(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        con.execute(
            "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, opened_at) "
            "VALUES ('WORK-2026-0001', 't', 'feature', '작업', 'active', 'u-tester', ?)",
            (NOW,),
        )
        _put(con, "DOC-REQ-HW", "D01", "behavior", "첫 변경", "WORK-2026-0001")
        _put(con, "DOC-REQ-HW", "D01", "behavior", "두 번째 변경", "WORK-2026-0001")
        DocDB(con, Actor("ai-claude", "ai"), "WORK-2026-0001").put(
            [{"h": "i:REQ-HW-002", "op": "upsert", "title": "다른 요구사항"}]
        )
        assert con.execute(
            "SELECT from_type, from_id, rel_type, to_type, to_id, source, status, anchor "
            "FROM relation ORDER BY to_type"
        ).fetchall() == [
            (
                "work",
                "WORK-2026-0001",
                "changes",
                "document",
                "DOC-REQ-HW",
                "rule:R5",
                "confirmed",
                "w:WORK-2026-0001",
            ),
            (
                "work",
                "WORK-2026-0001",
                "changes",
                "item",
                "REQ-HW-002",
                "rule:R5",
                "confirmed",
                "w:WORK-2026-0001",
            ),
        ]
    finally:
        con.close()


def test_t16_10_rejected_agent_relation_is_not_recreated_by_rule(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _document(con, "DOC-K02", "K02")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 경고")
        proposed = relations.propose(
            con,
            Actor("ai-claude", "ai"),
            "d:DOC-K02",
            "references",
            "i:REQ-HW-001",
            "요구사항 참조",
        )
        rel_id = int(proposed.rsplit("=", 1)[1].split()[0])
        relations.reject(con, Actor("u-tester", "human"), rel_id, "반려", True)
        _put(con, "DOC-K02", "K02", "overview", "대상 REQ-HW-001")
        assert con.execute(
            "SELECT rel_id, source, status FROM relation"
        ).fetchall() == [(rel_id, "agent", "rejected")]
    finally:
        con.close()


def test_t16_11_repeated_field_content_adds_no_relation_history(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _document(con, "DOC-REQ-HW", "D01")
        _document(con, "DOC-K02", "K02")
        _put(con, "DOC-REQ-HW", "D01", "requirement_ids", "REQ-HW-001: 디스크 경고")
        text = "대상 REQ-HW-001"
        _put(con, "DOC-K02", "K02", "overview", text)
        relation_rows_before = con.execute(
            "SELECT * FROM relation ORDER BY rel_id"
        ).fetchall()
        relation_history_before = con.execute(
            "SELECT * FROM change_log WHERE entity_type='relation' ORDER BY change_id"
        ).fetchall()
        _put(con, "DOC-K02", "K02", "overview", text)
        assert con.execute("SELECT * FROM relation ORDER BY rel_id").fetchall() == relation_rows_before
        assert con.execute(
            "SELECT * FROM change_log WHERE entity_type='relation' ORDER BY change_id"
        ).fetchall() == relation_history_before
    finally:
        con.close()
