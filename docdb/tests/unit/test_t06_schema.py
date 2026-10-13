"""T06 수용 테스트: 스키마 마이그레이션과 제약."""
import pytest

import docdb.db as db
import docdb.migrate as migrate
from tests.helpers import seed_min


_NOW = "2026-10-13T00:00:00Z"


def _insert_relation(
    con: db.Connection,
    *,
    source: str = "agent",
    status: str = "proposed",
    proposed_by: str | None = "ai-claude",
    from_type: str = "item",
    from_id: str = "REQ-1",
    rel_type: str = "verified_by",
    to_type: str = "item",
    to_id: str = "TC-1",
) -> None:
    con.execute(
        "INSERT INTO relation (from_type, from_id, rel_type, to_type, to_id, created_at, "
        "status, source, proposed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (from_type, from_id, rel_type, to_type, to_id, _NOW, status, source, proposed_by),
    )


def _insert_document(con: db.Connection, doc_id: str, kind_id: str) -> None:
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', 'D', 'normative', 'desired', 'u-tester', ?, ?)",
        (doc_id, doc_id, _NOW, _NOW),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, ?)", (doc_id, kind_id)
    )


def _insert_item(
    con: db.Connection, item_id: str, item_type: str, title: str
) -> None:
    con.execute(
        "INSERT INTO item (item_id, project_id, item_type, title, created_at, updated_at) "
        "VALUES (?, 't', ?, ?, ?, ?)",
        (item_id, item_type, title, _NOW, _NOW),
    )


def _insert_artifact(con: db.Connection, artifact_id: str) -> None:
    con.execute(
        "INSERT INTO artifact (artifact_id, uri, role, source_mode) VALUES (?, ?, ?, ?)",
        (artifact_id, "docs/source.md", "primary", "authored"),
    )


def test_t06_1_latest_version_is_eight() -> None:
    """최신 마이그레이션 번호는 8이다."""
    assert migrate.latest_version() == 8


def test_t06_2_relation_status_columns_and_default(schema_con: db.Connection) -> None:
    """관계 상태 컬럼이 있고 status 기본값은 confirmed이다."""
    seed_min(schema_con)
    columns = {
        row[1] for row in schema_con.execute("PRAGMA table_info(relation)").fetchall()
    }
    assert {"status", "source", "proposed_by", "decided_by", "decided_at"} <= columns

    schema_con.execute(
        "INSERT INTO relation (from_type, from_id, rel_type, to_type, to_id, created_at, "
        "source, proposed_by) VALUES ('item', 'REQ-1', 'verified_by', 'item', "
        "'TC-1', ?, 'human', 'u-tester')",
        (_NOW,),
    )
    assert schema_con.execute("SELECT status FROM relation").fetchone()[0] == "confirmed"


def test_t06_3_agent_cannot_insert_confirmed_relation(schema_con: db.Connection) -> None:
    """AI 출처 관계는 confirmed 상태로 넣을 수 없다."""
    seed_min(schema_con)
    with pytest.raises(db.IntegrityError):
        _insert_relation(schema_con, status="confirmed")


def test_t06_4_rule_relation_cannot_claim_ai_proposer(schema_con: db.Connection) -> None:
    """규칙 출처 관계에 AI 제안자를 지정할 수 없다."""
    seed_min(schema_con)
    with pytest.raises(db.IntegrityError):
        _insert_relation(schema_con, source="rule:R3", status="confirmed")


def test_t06_5_agent_can_propose_relation(schema_con: db.Connection) -> None:
    """AI 출처의 proposed 관계는 허용한다."""
    seed_min(schema_con)
    _insert_relation(schema_con)
    assert schema_con.execute("SELECT status FROM relation").fetchone()[0] == "proposed"


def test_t06_6_ai_cannot_decide_relation(schema_con: db.Connection) -> None:
    """AI 행위자는 proposed 관계를 확정할 수 없다."""
    seed_min(schema_con)
    _insert_relation(schema_con)
    with pytest.raises(db.IntegrityError):
        schema_con.execute(
            "UPDATE relation SET status='confirmed', decided_by='ai-claude' WHERE rel_id=1"
        )


def test_t06_7_human_can_confirm_relation(schema_con: db.Connection) -> None:
    """사람 행위자는 proposed 관계를 확정할 수 있다."""
    seed_min(schema_con)
    _insert_relation(schema_con)
    schema_con.execute(
        "UPDATE relation SET status='confirmed', decided_by='u-tester', decided_at=? "
        "WHERE rel_id=1",
        (_NOW,),
    )
    assert schema_con.execute("SELECT status FROM relation").fetchone()[0] == "confirmed"


def test_t06_8_confirmed_relation_cannot_be_rejected(schema_con: db.Connection) -> None:
    """확정된 관계는 rejected 상태로 바꿀 수 없다."""
    seed_min(schema_con)
    _insert_relation(schema_con, source="human", status="confirmed", proposed_by="u-tester")
    with pytest.raises(db.IntegrityError):
        schema_con.execute(
            "UPDATE relation SET status='rejected', decided_by='u-tester' WHERE rel_id=1"
        )


def test_t06_9_rule_relation_can_be_confirmed(schema_con: db.Connection) -> None:
    """규칙 추출 관계는 제안자 없이 confirmed로 넣을 수 있다."""
    seed_min(schema_con)
    _insert_relation(schema_con, source="rule:R2", status="confirmed", proposed_by=None)
    assert schema_con.execute("SELECT status, proposed_by FROM relation").fetchone() == (
        "confirmed", None
    )


def test_t06_10_relation_endpoint_tuple_is_unique(schema_con: db.Connection) -> None:
    """같은 출발점·관계 종류·도착점은 한 번만 저장한다."""
    seed_min(schema_con)
    _insert_relation(schema_con)
    with pytest.raises(db.IntegrityError):
        _insert_relation(schema_con)


def test_t06_11_required_link_view_ignores_proposals(schema_con: db.Connection) -> None:
    """필수 연결 뷰는 미연결과 제안에서 한 행, 확정 뒤에는 0행을 낸다."""
    seed_min(schema_con)
    _insert_document(schema_con, "DOC-REQ", "D01")
    _insert_document(schema_con, "DOC-TEST", "L02")
    assert schema_con.execute("SELECT count(*) FROM v_missing_required_links").fetchone()[0] == 1

    _insert_relation(
        schema_con,
        from_type="document",
        from_id="DOC-REQ",
        to_type="document",
        to_id="DOC-TEST",
        rel_type="references",
    )
    assert schema_con.execute("SELECT count(*) FROM v_missing_required_links").fetchone()[0] == 1
    schema_con.execute(
        "UPDATE relation SET status='confirmed', decided_by='u-tester' WHERE rel_id=1"
    )
    assert schema_con.execute("SELECT count(*) FROM v_missing_required_links").fetchone()[0] == 0


def test_t06_12_rtm_shows_confirmed_relations_only(schema_con: db.Connection) -> None:
    """RTM 뷰는 proposed 관계를 숨기고 confirmed 관계를 표시한다."""
    seed_min(schema_con)
    _insert_item(schema_con, "REQ-RTM", "requirement", "요구사항")
    _insert_item(schema_con, "TC-RTM", "test_case", "시험")
    _insert_relation(schema_con, from_id="REQ-RTM", to_id="TC-RTM")

    assert schema_con.execute(
        "SELECT verified_by FROM v_rtm WHERE requirement_id='REQ-RTM'"
    ).fetchone()[0] is None
    schema_con.execute(
        "UPDATE relation SET status='confirmed', decided_by='u-tester' WHERE rel_id=1"
    )
    assert schema_con.execute(
        "SELECT verified_by FROM v_rtm WHERE requirement_id='REQ-RTM'"
    ).fetchone()[0] == "TC-RTM"


def test_t06_13_item_fts_delete_and_integrity(schema_con: db.Connection) -> None:
    """항목 삭제는 전문 검색 색인을 지우고 FTS 무결성 검사를 통과한다."""
    seed_min(schema_con)
    _insert_item(schema_con, "REQ-FTS", "requirement", "violetneedle phrase")
    count = lambda: schema_con.execute(
        "SELECT count(*) FROM fts_text WHERE fts_text MATCH ?", ("violetneedle",)
    ).fetchone()[0]
    assert count() == 1

    schema_con.execute("DELETE FROM item WHERE item_id='REQ-FTS'")
    assert count() == 0
    schema_con.execute("INSERT INTO fts_text(fts_text) VALUES('integrity-check')")


def test_t06_14_import_segment_rejects_unknown_status(schema_con: db.Connection) -> None:
    """import_segment는 계약에 없는 상태를 거부한다."""
    seed_min(schema_con)
    _insert_artifact(schema_con, "ART-2026-0001")
    with pytest.raises(db.IntegrityError):
        schema_con.execute(
            "INSERT INTO import_segment (artifact_id, seq, start_off, end_off, text_digest, "
            "status) VALUES ('ART-2026-0001', 1, 0, 10, 'digest', 'done')"
        )


def test_t06_15_catalogue_extension_columns_and_json_check(
    schema_con: db.Connection,
) -> None:
    """카탈로그 확장 컬럼이 존재하고 extract_rel은 JSON 형식을 검사한다."""
    seed_min(schema_con)
    field_columns = {
        row[1] for row in schema_con.execute("PRAGMA table_info(cat_kind_field)").fetchall()
    }
    item_type_columns = {
        row[1] for row in schema_con.execute("PRAGMA table_info(cat_item_type)").fetchall()
    }
    assert "extract_rel" in field_columns
    assert "defined_by_field" in item_type_columns
    with pytest.raises(db.IntegrityError):
        schema_con.execute(
            "UPDATE cat_kind_field SET extract_rel='{bad' WHERE kind_id='D01' "
            "AND field_key='behavior'"
        )


def test_t06_16_id_format_triggers_enforce_contract(schema_con: db.Connection) -> None:
    """ID 트리거는 허용 형식만 통과시킨다."""
    seed_min(schema_con)
    with pytest.raises(db.IntegrityError):
        schema_con.execute(
            "INSERT INTO run (run_id, run_type, target_ref, runner, result, trust_source, "
            "recorded_at) VALUES ('RUN-0001', 'test', 'ref', 'runner', 'PASS', 'manual', ?)",
            (_NOW,),
        )
    schema_con.execute(
        "INSERT INTO run (run_id, run_type, target_ref, runner, result, trust_source, "
        "recorded_at) VALUES ('RUN-2026-0001', 'test', 'ref', 'runner', 'PASS', 'manual', ?)",
        (_NOW,),
    )

    for doc_id in ("doc-x", "DOC-A--B", "DOC-" + "A" * 61):
        with pytest.raises(db.IntegrityError):
            schema_con.execute(
                "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
                "baseline_mode, owner_id, created_at, updated_at) "
                "VALUES (?, 't', 'bad', 'living', 'D', 'normative', 'desired', 'u-tester', ?, ?)",
                (doc_id, _NOW, _NOW),
            )

    schema_con.execute(
        "INSERT INTO work (work_id, project_id, work_type, title, owner_id, opened_at) "
        "VALUES ('WORK-2026-0001', 't', 'test', 'test', 'u-tester', ?) ",
        (_NOW,),
    )
    schema_con.execute(
        "INSERT INTO handoff (handoff_id, work_id, from_role, to_role, payload, created_at) "
        "VALUES ('HO-2026-0001', 'WORK-2026-0001', 'agent', 'human', '{}', ?)",
        (_NOW,),
    )
    schema_con.execute(
        "INSERT INTO approval (approval_id, work_id, approver_id, decision, decided_at) "
        "VALUES ('APR-2026-0001', 'WORK-2026-0001', 'u-tester', 'approved', ?)",
        (_NOW,),
    )
    _insert_artifact(schema_con, "ART-2026-0001")


def test_t06_17_call_log_has_session_ref(schema_con: db.Connection) -> None:
    """호출 로그에 세션 참조 컬럼이 있다."""
    columns = {row[1] for row in schema_con.execute("PRAGMA table_info(call_log)").fetchall()}
    assert "session_ref" in columns
