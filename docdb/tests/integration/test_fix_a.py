"""최종 브랜치 리뷰 수정의 회귀 검사."""
import pytest

from docdb import db, workflow
from docdb.actors import Actor
from docdb.errors import DocdbError
from docdb.llm import DocDB

NOW = "2026-10-13T00:00:00+00:00"


def _work(con, work_id="WORK-2026-0001"):
    """테스트용 작업을 만든다."""
    con.execute(
        "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, opened_at) "
        "VALUES (?, 't', 'feature', '테스트 작업', 'active', 'u-tester', ?)",
        (work_id, NOW),
    )
    return work_id


def _document(con, doc_id, export_policy="public", origin_work_id=None):
    """테스트용 D01 문서를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, export_policy, origin_work_id, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', 'D', 'normative', 'desired', 'u-tester', ?, ?, ?, ?)",
        (doc_id, doc_id, export_policy, origin_work_id, NOW, NOW),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, 'D01')", (doc_id,)
    )


def _put_lines(result):
    """put 결과에서 머리줄을 제외한다."""
    return result.splitlines()[1:]


def test_fix_h1_local_only_items_redacted_in_all_read_tools(project_db):
    con = db.connect(project_db)
    try:
        work_id = _work(con)
        _document(con, "DOC-LOCAL", "local_only", work_id)
        client = DocDB(con, Actor("ai-claude", "ai"), work_id)
        cursor = con.execute("SELECT coalesce(max(change_id), 0) FROM change_log").fetchone()[0]
        client.put(
            [
                {
                    "h": "i:REQ-HW-001",
                    "op": "upsert",
                    "title": "임시 제목",
                    "status": "proposed",
                    "attrs": {"customer": "ACME"},
                    "defined_in": "d:DOC-LOCAL",
                }
            ]
        )
        client.put(
            [
                {
                    "h": "f:DOC-LOCAL/D01/requirement_ids",
                    "op": "set",
                    "text": "REQ-HW-001: 기밀 고객 ACME 계약 내용",
                }
            ]
        )

        get_output = client.get(["i:REQ-HW-001"])
        get_line = next(line for line in get_output.splitlines() if line.startswith("i:"))
        assert get_line.startswith(
            "i:REQ-HW-001 requirement proposed policy=local_only dg="
        )
        assert "ACME" not in get_line
        assert "attrs=" not in get_line

        find_output = client.find("ACME")
        find_lines = find_output.splitlines()[1:]
        assert "i:REQ-HW-001 policy=local_only" in find_lines
        assert all("ACME" not in line for line in find_lines)

        since_output = client.since(cursor, work=work_id)
        item_line = next(line for line in since_output.splitlines() if line.startswith("i:"))
        assert item_line.endswith(" policy=local_only")
        assert "new=" not in item_line and "old=" not in item_line
        assert "ACME" not in item_line

        ctx_output = client.ctx(work_id)
        ctx_item = next(line for line in ctx_output.splitlines() if line.startswith("i:"))
        assert ctx_item == "i:REQ-HW-001 requirement proposed policy=local_only"
        assert "ACME" not in ctx_item
        assert "attrs=" not in ctx_item
    finally:
        con.close()


def test_fix_m1_item_status_is_checked_for_secrets(mini_db):
    con = db.connect(mini_db)
    try:
        secret = "ghp_" + "a" * 36
        result = DocDB(con, Actor("ai-claude", "ai")).put(
            [{"h": "i:REQ-HW-009", "op": "upsert", "title": "안전한 제목", "status": secret}]
        )

        assert _put_lines(result) == [
            "NOTHING_APPLIED ok=0 err=1",
            "ERR i:REQ-HW-009 SECRET_DETECTED github_token",
        ]
        assert secret not in result
        assert con.execute("SELECT 1 FROM item WHERE item_id='REQ-HW-009'").fetchone() is None
    finally:
        con.close()


def test_fix_m3_patch_value_types_and_interface_errors(project_db, monkeypatch):
    con = db.connect(project_db)
    try:
        work_id = _work(con)
        _document(con, "DOC-M3")
        client = DocDB(con, Actor("ai-claude", "ai"))
        item_handle = "i:REQ-HW-009"
        field_handle = "f:DOC-M3/D01/requirement_ids"
        link = {
            "op": "link",
            "from": f"w:{work_id}",
            "rel": "references",
            "to": "d:DOC-M3",
            "why": "검증",
        }
        invalid_patches = [
            {"h": field_handle, "op": "set", "text": 123},
            {"h": field_handle, "op": "set", "text": "정상", "why": 5},
            {"h": field_handle, "op": "replace_section", "anchor": 5, "text": "값"},
            {"h": field_handle, "op": "set", "text": "정상", "sum": 5},
            {"h": item_handle, "op": "upsert", "title": 5},
            {"h": item_handle, "op": "upsert", "title": "정상", "status": 5},
            {"h": field_handle, "op": "set", "text": "정상", "state": 5},
            {"h": item_handle, "op": "upsert", "title": "정상", "expect": 5},
            {"h": item_handle, "op": "upsert", "title": "정상", "defined_in": {}},
            {"h": item_handle, "op": "upsert", "title": "정상", "attrs": []},
            {"h": item_handle, "op": "upsert", "title": "정상", "attrs_patch": []},
            dict(link, **{"from": 5}),
            dict(link, rel=5),
            dict(link, **{"to": {}}),
            dict(link, why=5),
            {
                "h": f"w:{work_id}",
                "op": "handoff",
                "from_role": "작성자",
                "to_role": "검토자",
                "payload": [],
            },
        ]
        for patch in invalid_patches:
            handle = patch.get("h", "?")
            result = client.put([patch])
            assert _put_lines(result)[0] == "NOTHING_APPLIED ok=0 err=1"
            assert _put_lines(result)[1].startswith(f"ERR {handle} BAD_OP")

        result = client.put(
            [
                {"h": field_handle, "op": "set", "text": "앞선 유효 패치"},
                {"h": field_handle, "op": "set", "text": "정상", "why": 5},
            ]
        )
        assert _put_lines(result)[0] == "NOTHING_APPLIED ok=1 err=1"
        assert con.execute(
            "SELECT 1 FROM field_value WHERE doc_id='DOC-M3'"
        ).fetchone() is None
        assert con.execute(
            "SELECT 1 FROM change_log WHERE entity_type='field_value'"
        ).fetchone() is None

        original_x = client.x
        state = {"raised": False}

        def raise_interface_error_once(sql, args=()):
            if not state["raised"]:
                state["raised"] = True
                raise db.dbapi2.InterfaceError("bad bind")
            return original_x(sql, args)

        monkeypatch.setattr(client, "x", raise_interface_error_once)
        result = client.put([{"h": field_handle, "op": "set", "text": "정상"}])
        assert state["raised"]
        assert _put_lines(result) == [
            "NOTHING_APPLIED ok=0 err=1",
            f'ERR {field_handle} DB "bad bind"',
        ]
    finally:
        con.close()


def test_fix_m4_ctx_ignores_proposed_work_document_links(project_db):
    con = db.connect(project_db)
    try:
        work_id = _work(con)
        _document(con, "DOC-PROPOSED")
        result = DocDB(con, Actor("ai-claude", "ai")).put(
            [
                {
                    "op": "link",
                    "from": f"w:{work_id}",
                    "rel": "references",
                    "to": "d:DOC-PROPOSED",
                    "why": "제안 관계",
                }
            ]
        )
        assert "APPLIED ok=1 err=0" in result
        assert con.execute("SELECT status FROM relation").fetchone() == ("proposed",)

        output = DocDB(con, Actor("ai-claude", "ai")).ctx(work_id)

        assert "D DOC-PROPOSED" not in output
    finally:
        con.close()


def test_fix_l2_error_details_do_not_echo_unvalidated_values(project_db):
    con = db.connect(project_db)
    try:
        work_id = _work(con)
        _document(con, "DOC-L2")
        client = DocDB(con, Actor("ai-claude", "ai"))
        custom_op = "PRIVATE-OP-MARKER"
        result = client.put(
            [{"h": "f:DOC-L2/D01/requirement_ids", "op": custom_op, "text": "값"}]
        )
        assert custom_op not in result
        assert "ERR f:DOC-L2/D01/requirement_ids BAD_OP" in result

        anchor = "PRIVATE-ANCHOR-MARKER"
        result = client.put(
            [
                {
                    "h": "f:DOC-L2/D01/requirement_ids",
                    "op": "replace_section",
                    "anchor": anchor,
                    "text": "값",
                }
            ]
        )
        assert anchor not in result
        assert "ERR f:DOC-L2/D01/requirement_ids ANCHOR_NOT_FOUND" in result

        item_op = "PRIVATE-ITEM-OP-MARKER"
        result = client.put([{"h": "i:REQ-HW-011", "op": item_op, "title": "값"}])
        assert item_op not in result
        assert "ERR i:REQ-HW-011 BAD_OP" in result

        private_status = "PRIVATE-STATUS-MARKER"
        result = client.put(
            [{"h": f"w:{work_id}", "op": "work", "status": private_status}]
        )
        assert private_status not in result
        assert f"ERR w:{work_id} STATUS_FORBIDDEN" in result
    finally:
        con.close()


def test_fix_l5_workflow_create_functions_accept_keyword_reason(project_db):
    con = db.connect(project_db)
    try:
        actor = Actor("ai-claude", "ai")
        work_id = workflow.create_work(
            con,
            actor,
            "작업 제목",
            "feature",
            "u-tester",
            [],
            reason="작업 생성 사유",
        )
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='work' AND entity_id=?",
            (work_id,),
        ).fetchone() == ("작업 생성 사유",)

        payload = {
            "done": [],
            "undone": [],
            "risks": [],
            "next_actions": [],
            "not_run": [],
            "as_of": 1,
        }
        workflow.create_handoff(
            con,
            actor,
            work_id,
            "작성자",
            "검토자",
            payload,
            reason="인계 생성 사유",
        )
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='handoff'"
        ).fetchone() == ("인계 생성 사유",)

        before = con.execute("SELECT count(*) FROM work").fetchone()[0]
        secret = "ghp_" + "c" * 36
        with pytest.raises(DocdbError, match="SECRET_DETECTED"):
            workflow.create_work(
                con,
                actor,
                "다른 작업",
                "feature",
                "u-tester",
                [],
                reason=secret,
            )
        assert con.execute("SELECT count(*) FROM work").fetchone()[0] == before

        handoff_count = con.execute("SELECT count(*) FROM handoff").fetchone()[0]
        with pytest.raises(DocdbError, match="SECRET_DETECTED"):
            workflow.create_handoff(
                con,
                actor,
                work_id,
                "작성자",
                "검토자",
                payload,
                reason=secret,
            )
        assert con.execute("SELECT count(*) FROM handoff").fetchone()[0] == handoff_count
    finally:
        con.close()


def test_fix_l5_put_calls_public_workflow_creation_functions(project_db, monkeypatch):
    con = db.connect(project_db)
    try:
        actor = Actor("ai-claude", "ai")
        calls = []
        create_work = workflow.create_work
        create_handoff = workflow.create_handoff

        def record_work(*args, **kwargs):
            calls.append(("work", kwargs.get("reason")))
            return create_work(*args, **kwargs)

        def record_handoff(*args, **kwargs):
            calls.append(("handoff", kwargs.get("reason")))
            return create_handoff(*args, **kwargs)

        monkeypatch.setattr(workflow, "create_work", record_work)
        monkeypatch.setattr(workflow, "create_handoff", record_handoff)
        client = DocDB(con, actor)
        work_reason = "patch work reason"
        work_result = client.put(
            [
                {
                    "h": "w:new",
                    "op": "work",
                    "title": "put 작업",
                    "work_type": "feature",
                    "owner": "u-tester",
                    "base_refs": [],
                    "why": work_reason,
                }
            ],
            reason="fallback reason",
        )
        assert "APPLIED ok=1 err=0" in work_result
        work_id = con.execute("SELECT work_id FROM work").fetchone()[0]
        payload = {
            "done": [],
            "undone": [],
            "risks": [],
            "next_actions": [],
            "not_run": [],
            "as_of": 1,
        }
        handoff_result = client.put(
            [
                {
                    "h": f"w:{work_id}",
                    "op": "handoff",
                    "from_role": "작성자",
                    "to_role": "검토자",
                    "payload": payload,
                    "why": "patch handoff reason",
                }
            ],
            reason="fallback handoff reason",
        )
        assert "APPLIED ok=1 err=0" in handoff_result
        assert calls == [
            ("work", work_reason),
            ("handoff", "patch handoff reason"),
        ]
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='work' AND entity_id=?",
            (work_id,),
        ).fetchone() == (work_reason,)
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='handoff'"
        ).fetchone() == ("patch handoff reason",)
    finally:
        con.close()
