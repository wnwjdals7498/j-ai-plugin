"""T14 put 항목(i:) upsert 수용 테스트."""
import json
import re

from docdb import db, history, textnorm
from docdb.actors import Actor
from docdb.llm import DocDB

NOW = "2026-10-13T00:00:00+00:00"


def _document(con, doc_id="DOC-T14", export_policy="public"):
    """미니 DB에 D01 문서와 기본 필드를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, export_policy, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', 'D', 'normative', 'desired', 'u-tester', ?, ?, ?)",
        (doc_id, doc_id, export_policy, NOW, NOW),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, 'D01')", (doc_id,)
    )


def _client(con, actor=None, work=None, session=None, model=None):
    """T14 기본 AI 행위자로 DocDB 클라이언트를 만든다."""
    return DocDB(con, actor or Actor("ai-codex", "ai"), work, session, model)


def test_t14_1_upsert_creates_item_and_history(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con, "DOC-T14")
        client = _client(con)
        res = client.put(
            [
                {
                    "h": "i:REQ-HW-009",
                    "op": "upsert",
                    "title": "디스크",
                    "defined_in": "d:DOC-T14",
                }
            ]
        )
        assert "APPLIED ok=1 err=0" in res
        match = re.search(r"OK i:REQ-HW-009 dg=([0-9a-f]{16}) tok=(\d+)", res)
        assert match is not None
        dg = match.group(1)
        tok = int(match.group(2))

        row = con.execute(
            "SELECT item_type, title, defined_in, digest, tok_est FROM item WHERE item_id='REQ-HW-009'"
        ).fetchone()
        assert row is not None
        assert row[0] == "requirement"
        assert row[1] == "디스크"
        assert row[2] == "DOC-T14"
        assert row[3] == dg
        assert row[4] == tok

        c_row = con.execute(
            "SELECT op, entity_type, entity_id, before_json, after_json FROM change_log WHERE entity_id='REQ-HW-009'"
        ).fetchone()
        assert c_row is not None
        assert c_row[0] == "insert"
        assert c_row[1] == "item"
        assert c_row[2] == "REQ-HW-009"
        assert c_row[3] is None
        after = json.loads(c_row[4])
        assert after["title"] == "디스크"
    finally:
        con.close()


def test_t14_2_new_item_without_title_fails(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res = client.put([{"h": "i:REQ-HW-009", "op": "upsert"}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-009 BAD_OP" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-009'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='REQ-HW-009'").fetchone() is None
    finally:
        con.close()


def test_t14_2_non_string_title_fails(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res = client.put([{"h": "i:REQ-HW-009", "op": "upsert", "title": 123}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-009 BAD_OP" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-009'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='REQ-HW-009'").fetchone() is None
    finally:
        con.close()


def test_t14_2_non_string_status_fails(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res = client.put([{"h": "i:REQ-HW-009", "op": "upsert", "title": "정상", "status": 5}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-009 BAD_OP" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-009'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='REQ-HW-009'").fetchone() is None
    finally:
        con.close()


def test_t14_3_unknown_prefix_fails_id_format(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res = client.put([{"h": "i:ZZZ-1", "op": "upsert", "title": "없는접두어"}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:ZZZ-1 ID_FORMAT" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='ZZZ-1'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='ZZZ-1'").fetchone() is None
    finally:
        con.close()


def test_t14_4_attrs_and_attrs_patch_together_fails(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res = client.put(
            [
                {
                    "h": "i:REQ-HW-009",
                    "op": "upsert",
                    "title": "충돌",
                    "attrs": {"a": 1},
                    "attrs_patch": {"b": 2},
                }
            ]
        )
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-009 BAD_OP" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-009'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='REQ-HW-009'").fetchone() is None
    finally:
        con.close()


def test_t14_4_attrs_null_fails(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res = client.put(
            [
                {
                    "h": "i:REQ-HW-009",
                    "op": "upsert",
                    "title": "널속성",
                    "attrs": None,
                }
            ]
        )
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-009 BAD_OP" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-009'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='REQ-HW-009'").fetchone() is None
    finally:
        con.close()


def test_t14_4_attrs_patch_null_fails(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res = client.put(
            [
                {
                    "h": "i:REQ-HW-009",
                    "op": "upsert",
                    "title": "널패치",
                    "attrs_patch": None,
                }
            ]
        )
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-009 BAD_OP" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-009'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='REQ-HW-009'").fetchone() is None
    finally:
        con.close()


def test_t14_5_attrs_patch_shallow_merges(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res1 = client.put(
            [
                {
                    "h": "i:REQ-HW-001",
                    "op": "upsert",
                    "title": "항목1",
                    "attrs": {"a": 1, "b": 2},
                }
            ]
        )
        assert "APPLIED ok=1 err=0" in res1

        res2 = client.put(
            [
                {
                    "h": "i:REQ-HW-001",
                    "op": "upsert",
                    "attrs_patch": {"b": None, "c": 3},
                }
            ]
        )
        assert "APPLIED ok=1 err=0" in res2

        row = con.execute("SELECT attrs FROM item WHERE item_id='REQ-HW-001'").fetchone()
        assert row is not None
        assert json.loads(row[0]) == {"a": 1, "c": 3}
    finally:
        con.close()


def test_t14_6_wrong_expect_causes_conflict(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        res1 = client.put([{"h": "i:REQ-HW-001", "op": "upsert", "title": "항목1"}])
        assert "APPLIED ok=1 err=0" in res1
        match = re.search(r"dg=([0-9a-f]{16})", res1)
        assert match is not None
        actual_dg = match.group(1)

        res2 = client.put(
            [
                {
                    "h": "i:REQ-HW-001",
                    "op": "upsert",
                    "title": "항목2",
                    "expect": "0000000000000000",
                }
            ]
        )
        assert "NOTHING_APPLIED ok=0 err=1" in res2
        assert f"ERR i:REQ-HW-001 CONFLICT now={actual_dg}" in res2
    finally:
        con.close()


def test_t14_7_secret_in_title_detected(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        secret = "AKIA" + "A" * 16
        res = client.put([{"h": "i:REQ-HW-001", "op": "upsert", "title": secret}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-001 SECRET_DETECTED aws_access_key" in res
        assert secret not in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-001'").fetchone() is None
    finally:
        con.close()


def test_t14_8_reusing_deleted_id_fails(mini_db):
    con = db.connect(mini_db)
    try:
        client = _client(con)
        with db.write_tx(con):
            history.append_change(
                con,
                "item",
                "REQ-HW-DEL",
                "delete",
                {"title": "이전"},
                None,
                "u-tester",
                None,
                "삭제",
            )
        res = client.put([{"h": "i:REQ-HW-DEL", "op": "upsert", "title": "재생성"}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "ERR i:REQ-HW-DEL ID_FORMAT" in res
    finally:
        con.close()


def test_t14_9_get_item_level_0_shows_updated_digest(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con, "DOC-T14")
        con.execute(
            "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, opened_at) "
            "VALUES ('WORK-2026-0001', 't', 'feature', '작업', 'active', 'u-tester', ?)",
            (NOW,),
        )
        con.execute(
            "UPDATE document SET origin_work_id='WORK-2026-0001' WHERE doc_id='DOC-T14'"
        )
        client = _client(con)
        res1 = client.put(
            [
                {
                    "h": "i:REQ-HW-001",
                    "op": "upsert",
                    "title": "버전1",
                    "defined_in": "d:DOC-T14",
                }
            ]
        )
        assert "APPLIED ok=1 err=0" in res1
        dg1 = re.search(r"dg=([0-9a-f]{16})", res1).group(1)

        get1 = client.get(["i:REQ-HW-001"], level=0)
        assert f"dg={dg1}" in get1

        ctx1 = client.ctx("WORK-2026-0001")
        assert "i:REQ-HW-001" in ctx1
        item_ctx_line = [l for l in ctx1.splitlines() if l.startswith("i:REQ-HW-001")][0]
        assert "dg=" not in item_ctx_line

        res2 = client.put(
            [
                {
                    "h": "i:REQ-HW-001",
                    "op": "upsert",
                    "title": "버전2",
                    "defined_in": "d:DOC-T14",
                }
            ]
        )
        assert "APPLIED ok=1 err=0" in res2
        dg2 = re.search(r"dg=([0-9a-f]{16})", res2).group(1)
        assert dg1 != dg2

        get2 = client.get(["i:REQ-HW-001"], level=0)
        assert f"dg={dg2}" in get2
    finally:
        con.close()


def test_t14_10_atomic_rollback_on_mixed_patches(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con, "DOC-T14")
        client = _client(con)
        patches = [
            {"h": "i:REQ-HW-001", "op": "upsert", "title": "정상 항목"},
            {"h": "f:DOC-T14/D01/behavior", "op": "bad_operation", "text": "실패할 필드"},
        ]
        res = client.put(patches)
        assert "NOTHING_APPLIED" in res
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-001'").fetchone() is None
        assert con.execute("SELECT 1 FROM change_log WHERE entity_id='REQ-HW-001'").fetchone() is None
    finally:
        con.close()
