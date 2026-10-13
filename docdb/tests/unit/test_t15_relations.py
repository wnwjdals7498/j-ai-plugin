"""T15 관계 제안·철회와 사람 확정 CLI 수용 테스트."""
import json

from docdb import db, relations
from docdb.actors import Actor
from docdb.llm import DocDB

NOW = "2026-10-13T00:00:00+00:00"


def _document(con, doc_id="DOC-T15"):
    """미니 DB에 관계와 필드 쓰기에서 사용할 문서를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, export_policy, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', 'D', 'normative', 'desired', 'u-tester', 'public', ?, ?)",
        (doc_id, doc_id, NOW, NOW),
    )
    con.execute("INSERT INTO document_kind (doc_id, kind_id) VALUES (?, 'D01')", (doc_id,))


def _item(con, item_id):
    """항목 하나를 put으로 생성한다."""
    result = DocDB(con, Actor("ai-codex", "ai")).put(
        [{"h": f"i:{item_id}", "op": "upsert", "title": item_id}]
    )
    assert "APPLIED ok=1 err=0" in result


def _link(con, from_h, rel="references", to_h="i:TC-T15-001", actor=None, work=None, why="근거"):
    """관계를 제안하고 응답과 정수 관계 ID를 반환한다."""
    client = DocDB(con, actor or Actor("ai-claude", "ai"), work)
    result = client.put(
        [{"op": "link", "from": from_h, "rel": rel, "to": to_h, "why": why}]
    )
    row = con.execute("SELECT max(rel_id) FROM relation").fetchone()
    return result, row[0]


def _work(con, work_id="WORK-2026-0001"):
    """작업 이력 필터용 작업을 만든다."""
    con.execute(
        "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, opened_at) "
        "VALUES (?, 't', 'feature', '작업', 'active', 'u-tester', ?)",
        (work_id, NOW),
    )
    return work_id


def _cli_args(mini_db, actor, *args):
    """임시 프로젝트 DB와 행위자를 지정한 CLI 인자를 만든다."""
    return ["--db", str(mini_db), "--actor", actor, *args]


def test_t15_1_ai_link_proposes_relation(mini_db):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        result, rel_id = _link(con, "i:REQ-T15-001")
        assert f"OK rel={rel_id} proposed" in result
        assert con.execute(
            "SELECT source, proposed_by, status FROM relation WHERE rel_id=?", (rel_id,)
        ).fetchone() == ("agent", "ai-claude", "proposed")
    finally:
        con.close()


def test_t15_2_link_requires_nonempty_why(mini_db):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        client = DocDB(con, Actor("ai-codex", "ai"))
        missing = client.put(
            [{"op": "link", "from": "i:REQ-T15-001", "rel": "references", "to": "i:TC-T15-001"}]
        )
        empty = client.put(
            [{"op": "link", "from": "i:REQ-T15-001", "rel": "references", "to": "i:TC-T15-001", "why": "  "}]
        )
        assert "ERR i:REQ-T15-001 BAD_OP" in missing
        assert "ERR i:REQ-T15-001 BAD_OP" in empty
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
    finally:
        con.close()


def test_t15_3_unknown_relation_type_is_bad_op(mini_db):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        result, _ = _link(con, "i:REQ-T15-001", rel="owns")
        assert "ERR i:REQ-T15-001 BAD_OP" in result
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
    finally:
        con.close()


def test_t15_4_missing_endpoint_is_not_found(mini_db):
    con = db.connect(mini_db)
    try:
        _item(con, "TC-T15-001")
        result, _ = _link(con, "i:REQ-T15-999")
        assert "ERR i:REQ-T15-999 NOT_FOUND" in result
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
    finally:
        con.close()


def test_t15_5_field_endpoint_is_unsupported(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con)
        _item(con, "TC-T15-001")
        result, _ = _link(con, "f:DOC-T15/D01/behavior")
        assert "ERR f:DOC-T15/D01/behavior UNSUPPORTED_HANDLE" in result
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
    finally:
        con.close()


def test_t15_6_duplicate_proposal_reports_existing_status(mini_db):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        first, rel_id = _link(con, "i:REQ-T15-001")
        second, _ = _link(con, "i:REQ-T15-001")
        assert f"OK rel={rel_id} proposed" in first
        assert f"ERR i:REQ-T15-001 DUPLICATE rel={rel_id} status=proposed" in second
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 1
    finally:
        con.close()


def test_t15_7_rejected_relation_cannot_be_proposed_again(mini_db):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        _, rel_id = _link(con, "i:REQ-T15-001")
        assert relations.reject(con, Actor("u-tester", "human"), rel_id, "검토 반려", True)
        result, _ = _link(con, "i:REQ-T15-001")
        assert f"ERR i:REQ-T15-001 REJECTED_BEFORE rel={rel_id}" in result
        assert con.execute(
            "SELECT status FROM relation WHERE rel_id=?", (rel_id,)
        ).fetchone() == ("rejected",)
    finally:
        con.close()


def test_t15_8_unlink_allows_only_proposers_pending_relations(mini_db):
    con = db.connect(mini_db)
    try:
        for item_id in ("REQ-T15-001", "REQ-T15-002", "TC-T15-001"):
            _item(con, item_id)
        _, own_id = _link(con, "i:REQ-T15-001")
        own = DocDB(con, Actor("ai-claude", "ai")).put(
            [{"op": "unlink", "rel_id": own_id, "why": "잘못 제안함"}]
        )
        assert f"OK unlink rel={own_id}" in own
        assert con.execute("SELECT 1 FROM relation WHERE rel_id=?", (own_id,)).fetchone() is None

        _, other_id = _link(con, "i:REQ-T15-002", actor=Actor("ai-codex", "ai"))
        other = DocDB(con, Actor("ai-claude", "ai")).put(
            [{"op": "unlink", "rel_id": other_id, "why": "철회 시도"}]
        )
        assert f"ERR rel={other_id} NEEDS_HUMAN" in other

        _, confirmed_id = _link(con, "i:REQ-T15-001", to_h="i:REQ-T15-002")
        relations.confirm(con, Actor("u-tester", "human"), [confirmed_id], True)
        confirmed = DocDB(con, Actor("ai-claude", "ai")).put(
            [{"op": "unlink", "rel_id": confirmed_id, "why": "철회 시도"}]
        )
        assert f"ERR rel={confirmed_id} NEEDS_HUMAN" in confirmed
    finally:
        con.close()


def test_t15_9_cli_confirm_requires_human_and_interactive_session(mini_db, run_cli, frozen_now):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        _, rel_id = _link(con, "i:REQ-T15-001")
    finally:
        con.close()

    code, _out, err = run_cli(_cli_args(mini_db, "ai-codex", "link", "confirm", str(rel_id)))
    assert code == 1
    assert "ERROR NEEDS_HUMAN" in err
    code, out, err = run_cli(
        _cli_args(mini_db, "u-tester", "link", "confirm", str(rel_id)), interactive=True
    )
    assert code == 0, err
    assert f"OK rel={rel_id} confirmed" in out

    con = db.connect(mini_db)
    try:
        assert con.execute(
            "SELECT status, decided_by, decided_at FROM relation WHERE rel_id=?", (rel_id,)
        ).fetchone() == ("confirmed", "u-tester", NOW)
        change = con.execute(
            "SELECT op, actor_id, before_json, after_json FROM change_log "
            "WHERE entity_type='relation' "
            "AND entity_id=? ORDER BY seq DESC LIMIT 1",
            (str(rel_id),),
        ).fetchone()
        assert change[:2] == ("update", "u-tester")
        assert json.loads(change[2]) == {
            "status": "proposed",
            "decided_by": None,
            "decided_at": None,
        }
        assert json.loads(change[3]) == {
            "status": "confirmed",
            "decided_by": "u-tester",
            "decided_at": NOW,
        }
    finally:
        con.close()


def test_t15_10_cli_reject_requires_why_and_records_rejection(mini_db, run_cli):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        _, rel_id = _link(con, "i:REQ-T15-001")
    finally:
        con.close()

    code, _out, err = run_cli(
        _cli_args(mini_db, "u-tester", "link", "reject", str(rel_id)), interactive=True
    )
    assert code == 1
    assert "ERROR BAD_ARGS" in err
    code, out, err = run_cli(
        _cli_args(mini_db, "u-tester", "link", "reject", str(rel_id), "--why", "범위 밖"),
        interactive=True,
    )
    assert code == 0, err
    assert f"OK rel={rel_id} rejected" in out
    con = db.connect(mini_db)
    try:
        assert con.execute(
            "SELECT status, reason, decided_by FROM relation WHERE rel_id=?", (rel_id,)
        ).fetchone() == ("rejected", "범위 밖", "u-tester")
    finally:
        con.close()


def test_t15_11_cli_remove_deletes_confirmed_but_forbids_rule_relation(mini_db, run_cli):
    con = db.connect(mini_db)
    try:
        _document(con)
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
        _, confirmed_id = _link(con, "i:REQ-T15-001")
        relations.confirm(con, Actor("u-tester", "human"), [confirmed_id], True)
        con.execute(
            "INSERT INTO relation (from_type, from_id, rel_type, to_type, to_id, anchor, reason, "
            "created_at, status, source, proposed_by) "
            "VALUES ('document', 'DOC-T15', 'references', 'item', 'REQ-T15-001', 'd:DOC-T15', "
            "'규칙', ?, 'confirmed', 'rule:R1', NULL)",
            (NOW,),
        )
        rule_id = con.execute("SELECT max(rel_id) FROM relation").fetchone()[0]
    finally:
        con.close()

    code, out, err = run_cli(
        _cli_args(
            mini_db, "u-tester", "link", "remove", str(confirmed_id), "--why", "중복"
        ),
        interactive=True,
    )
    assert code == 0, err
    assert f"OK rel={confirmed_id} removed" in out
    code, _out, err = run_cli(
        _cli_args(mini_db, "u-tester", "link", "remove", str(rule_id), "--why", "검토"),
        interactive=True,
    )
    assert code == 1
    assert "ERROR BAD_OP" in err
    con = db.connect(mini_db)
    try:
        assert con.execute("SELECT 1 FROM relation WHERE rel_id=?", (confirmed_id,)).fetchone() is None
        assert con.execute(
            "SELECT op, after_json FROM change_log WHERE entity_type='relation' AND entity_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (str(confirmed_id),),
        ).fetchone() == ("delete", None)
        assert con.execute("SELECT 1 FROM relation WHERE rel_id=?", (rule_id,)).fetchone() is not None
    finally:
        con.close()


def test_t15_12_link_review_formats_and_sorts_pending_relations(mini_db, run_cli):
    con = db.connect(mini_db)
    try:
        for item_id in ("REQ-T15-001", "REQ-T15-002", "TC-T15-001"):
            _item(con, item_id)
        for rel_id, from_id, why in (
            (2, "REQ-T15-002", "second reason"),
            (1, "REQ-T15-001", "first reason"),
        ):
            con.execute(
                "INSERT INTO relation (rel_id, from_type, from_id, rel_type, to_type, to_id, "
                "reason, created_at, status, source, proposed_by) "
                "VALUES (?, 'item', ?, 'references', 'item', 'TC-T15-001', ?, ?, "
                "'proposed', 'agent', 'ai-codex')",
                (rel_id, from_id, why, NOW),
            )
        con.execute("PRAGMA reverse_unordered_selects = ON")
        rows = relations.review(con)
        assert rows == [
            'rel=1 i:REQ-T15-001 -references-> i:TC-T15-001 by=ai-codex why="first reason"',
            'rel=2 i:REQ-T15-002 -references-> i:TC-T15-001 by=ai-codex why="second reason"',
        ]
    finally:
        con.close()

    code, out, err = run_cli(_cli_args(mini_db, "u-tester", "link", "review"))
    assert code == 0, err
    assert out.splitlines() == rows


def test_t15_13_pending_count_and_link_review_filter_by_insert_work(mini_db, run_cli):
    con = db.connect(mini_db)
    try:
        work_id = _work(con)
        for item_id in ("REQ-T15-001", "REQ-T15-002", "TC-T15-001"):
            _item(con, item_id)
        work_result, work_rel_id = _link(con, "i:REQ-T15-001", work=work_id)
        assert f"OK rel={work_rel_id} proposed" in work_result
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 1
        other_result, _ = _link(con, "i:REQ-T15-002")
        assert "OK rel=" in other_result
        assert relations.pending_count(con) == 2
        assert relations.pending_count(con, work_id) == 1
        filtered = relations.review(con, work_id)
        assert len(filtered) == 1
        assert filtered[0].startswith(f"rel={work_rel_id} ")
    finally:
        con.close()

    code, out, err = run_cli(
        _cli_args(mini_db, "u-tester", "link", "review", "--work", work_id)
    )
    assert code == 0, err
    assert out.splitlines() == [filtered[0]]


def test_t15_14_invalid_link_rolls_back_field_write(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con)
        _item(con, "REQ-T15-001")
        client = DocDB(con, Actor("ai-codex", "ai"))
        result = client.put(
            [
                {"h": "f:DOC-T15/D01/behavior", "op": "set", "text": "변경 내용"},
                {
                    "op": "link",
                    "from": "i:REQ-T15-001",
                    "rel": "owns",
                    "to": "d:DOC-T15",
                    "why": "잘못된 관계",
                },
            ]
        )
        assert "NOTHING_APPLIED ok=1 err=1" in result
        assert "ERR i:REQ-T15-001 BAD_OP" in result
        assert con.execute("SELECT count(*) FROM field_value").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
        assert con.execute(
            "SELECT count(*) FROM change_log WHERE entity_type='field_value'"
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_t15_15_disallowed_changes_and_supersedes_endpoints_are_bad_op(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con)
        _item(con, "REQ-T15-001")
        client = DocDB(con, Actor("ai-codex", "ai"))
        result = client.put(
            [
                {
                    "op": "link",
                    "from": "i:REQ-T15-001",
                    "rel": "changes",
                    "to": "d:DOC-T15",
                    "why": "금지 조합",
                },
                {
                    "op": "link",
                    "from": "d:DOC-T15",
                    "rel": "supersedes",
                    "to": "i:REQ-T15-001",
                    "why": "금지 조합",
                },
            ]
        )
        assert "NOTHING_APPLIED ok=0 err=2" in result
        assert result.count(" BAD_OP") == 2
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
    finally:
        con.close()


def test_t15_16_human_cli_put_link_stays_proposed(mini_db, run_cli):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
    finally:
        con.close()
    patch = {
        "op": "link",
        "from": "i:REQ-T15-001",
        "rel": "verified_by",
        "to": "i:TC-T15-001",
        "why": "사람이 제안",
    }
    code, out, err = run_cli(
        _cli_args(mini_db, "u-tester", "put", json.dumps([patch], ensure_ascii=False))
    )
    assert code == 0, err
    assert "OK rel=" in out and " proposed" in out
    con = db.connect(mini_db)
    try:
        assert con.execute(
            "SELECT source, proposed_by, status FROM relation"
        ).fetchone() == ("human", "u-tester", "proposed")
    finally:
        con.close()


def test_t15_17_team_actor_cannot_propose_link(mini_db, run_cli):
    con = db.connect(mini_db)
    try:
        _item(con, "REQ-T15-001")
        _item(con, "TC-T15-001")
    finally:
        con.close()
    patch = {
        "op": "link",
        "from": "i:REQ-T15-001",
        "rel": "references",
        "to": "i:TC-T15-001",
        "why": "팀 제안",
    }
    code, out, err = run_cli(
        _cli_args(mini_db, "team-ci", "put", json.dumps([patch], ensure_ascii=False))
    )
    assert code == 0, err
    assert "ERR i:REQ-T15-001 BAD_ARGS" in out
    con = db.connect(mini_db)
    try:
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0
    finally:
        con.close()
