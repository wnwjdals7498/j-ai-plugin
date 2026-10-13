"""T12 LLM 읽기·필드 쓰기 수용 테스트."""
import json

from docdb import db, history, textnorm
from docdb.actors import Actor
from docdb.llm import DocDB


NOW = "2026-10-13T00:00:00+00:00"


def _document(con, doc_id="DOC-T12", export_policy="public", work_id=None):
    """미니 DB에 D01 문서와 행동 필드를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, export_policy, work_id, origin_work_id, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', 'D', 'normative', 'desired', 'u-tester', ?, NULL, ?, ?, ?)",
        (doc_id, doc_id, export_policy, work_id, NOW, NOW),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, 'D01')", (doc_id,)
    )


def _client(con, actor=None, work=None, session=None, model=None):
    """T12 기본 AI 행위자로 DocDB 클라이언트를 만든다."""
    return DocDB(con, actor or Actor("ai-codex", "ai"), work, session, model)


def test_t12_29_put_normalizes_line_endings_and_accepts_returned_digest(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con)
        client = _client(con)
        handle = "f:DOC-T12/D01/behavior"
        result = client.put([{"h": handle, "text": "a\r\nb"}])
        digest = textnorm.digest16("a\nb")
        assert f"OK {handle} dg={digest} " in result
        retry = client.put([{"h": handle, "text": "c", "expect": digest}])
        assert "NOTHING_APPLIED" not in retry
        assert "CONFLICT" not in retry
        assert con.execute(
            "SELECT value_text, digest FROM field_value WHERE doc_id='DOC-T12' "
            "AND kind_id='D01' AND field_key='behavior'"
        ).fetchone() == ("c", textnorm.digest16("c"))
    finally:
        con.close()


def test_t12_30_put_rejects_fake_aws_key_and_rolls_back(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con)
        client = _client(con)
        handle = "f:DOC-T12/D01/behavior"
        secret = "AKIA" + "A" * 16
        cases = [
            ({"h": handle, "text": "é", "why": secret}, None),
            ({"h": handle, "text": "é"}, secret),
        ]
        for patch, reason in cases:
            before_values = con.execute("SELECT * FROM field_value").fetchall()
            before_changes = con.execute("SELECT * FROM change_log").fetchall()
            result = client.put([patch], reason=reason)
            error_line = next(line for line in result.splitlines() if line.startswith("ERR "))
            assert "NOTHING_APPLIED" in result
            assert error_line == f"ERR {handle} SECRET_DETECTED aws_access_key"
            assert secret not in error_line
            assert con.execute("SELECT * FROM field_value").fetchall() == before_values
            assert con.execute("SELECT * FROM change_log").fetchall() == before_changes
    finally:
        con.close()


def test_t12_31_get_redacts_local_only_field_body(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con, export_policy="local_only")
        client = _client(con)
        client.put([{"h": "f:DOC-T12/D01/behavior", "text": "숨겨진 본문"}])
        result = client.get(["f:DOC-T12/D01/behavior"], level=2)
        line = next(line for line in result.splitlines() if line.startswith("f:"))
        assert line.endswith("policy=local_only")
        assert "숨겨진 본문" not in result
    finally:
        con.close()


def test_t12_32_find_redacts_local_only_match_excerpt(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con, export_policy="local_only")
        client = _client(con)
        client.put([{"h": "f:DOC-T12/D01/behavior", "text": "비공개어휘가 들어간 본문"}])
        result = client.find("비공개어휘")
        assert "f:DOC-T12/D01/behavior policy=local_only" in result
        assert "들어간 본문" not in result
        assert result.splitlines()[1:] == ["f:DOC-T12/D01/behavior policy=local_only"]
    finally:
        con.close()


def test_t12_33_ctx_and_since_redact_local_only_summary_and_diff(mini_db):
    con = db.connect(mini_db)
    try:
        con.execute(
            "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, opened_at) "
            "VALUES ('WORK-2026-0001', 't', 'feature', '작업', 'active', 'u-tester', ?)",
            (NOW,),
        )
        _document(con, export_policy="local_only", work_id="WORK-2026-0001")
        client = _client(con, work="WORK-2026-0001")
        handle = "f:DOC-T12/D01/behavior"
        client.put([{"h": handle, "text": "처음에는 안전한 내용"}])
        context = client.ctx("WORK-2026-0001")
        field_line = next(line for line in context.splitlines() if line.startswith(handle))
        assert field_line.endswith("policy=local_only")
        assert "처음에는 안전한 내용" not in context

        cursor = con.execute("SELECT max(change_id) FROM change_log").fetchone()[0]
        client.put([{"h": handle, "text": "새로운 비공개 내용"}])
        delta = client.since(cursor, work="WORK-2026-0001")
        assert "policy=local_only" in delta
        assert "ch" in delta
        assert "새로운 비공개 내용" not in delta
    finally:
        con.close()


def test_t12_34_call_log_stores_session_and_model(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con)
        client = _client(con, session="s1", model="m1")
        client.get(["d:DOC-T12"])
        assert con.execute(
            "SELECT session_ref FROM call_log WHERE tool='get' ORDER BY call_id DESC LIMIT 1"
        ).fetchone() == ("s1/m1",)
    finally:
        con.close()


def test_t12_35_put_records_valid_change_log_chain(mini_db):
    con = db.connect(mini_db)
    try:
        _document(con)
        client = _client(con)
        client.put([{"h": "f:DOC-T12/D01/behavior", "text": "이력 검증"}])
        row = con.execute(
            "SELECT row_digest FROM change_log WHERE entity_type='field_value' "
            "AND entity_id='DOC-T12|D01|behavior'"
        ).fetchone()
        assert row is not None
        assert len(row[0]) == 71 and row[0].startswith("sha256:")
        assert history.verify_chain(con, "field_value", "DOC-T12|D01|behavior") == []
    finally:
        con.close()


def test_t12_36_get_reports_bad_handle_in_its_slot(mini_db):
    con = db.connect(mini_db)
    try:
        result = _client(con).get(["x:1"])
        assert "\nERR x:1 BAD_HANDLE" in result
    finally:
        con.close()


def test_t12_37_cli_get_and_put_match_docdb_output(mini_db, run_cli, tmp_path):
    con = db.connect(mini_db)
    clone = None
    try:
        _document(con)
        con.close()
        con = None
        clone_path = tmp_path / "clone.sqlite"
        source = db.connect(mini_db)
        clone = db.connect(clone_path, create=True)
        source.backup(clone)
        source.close()
        client = _client(clone)
        expected_get = client.get(["d:DOC-T12"])
        code, out, err = run_cli(
            ["--db", str(mini_db), "--actor", "ai-codex", "get", "d:DOC-T12"]
        )
        assert (code, out.strip(), err) == (0, expected_get, "")

        clone.close()
        clone = None
        source = db.connect(mini_db)
        clone = db.connect(clone_path, create=True)
        source.backup(clone)
        source.close()
        expected_put = _client(clone).put(
            [{"h": "f:DOC-T12/D01/behavior", "text": "CLI 쓰기"}]
        )
        code, out, err = run_cli(
            [
                "--db",
                str(mini_db),
                "--actor",
                "ai-codex",
                "put",
                json.dumps([{"h": "f:DOC-T12/D01/behavior", "text": "CLI 쓰기"}], ensure_ascii=False),
            ]
        )
        assert (code, out.strip(), err) == (0, expected_put, "")
    finally:
        if clone is not None:
            clone.close()
        if con is not None:
            con.close()
