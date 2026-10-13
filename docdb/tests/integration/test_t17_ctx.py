"""T17 확정 대기 관계와 필수 연결의 ctx·trace 통합 테스트."""
from docdb import db, relations
from docdb.actors import Actor
from docdb.llm import DocDB

NOW = "2026-10-13T00:00:00+00:00"
WORK = "WORK-2026-0001"


def _work(con):
    """컨텍스트 테스트용 작업을 만든다."""
    con.execute(
        "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, opened_at) "
        "VALUES (?, 't', 'feature', '작업', 'active', 'u-tester', ?)",
        (WORK, NOW),
    )


def _document(con, doc_id, kind_id, work_id=None):
    """시작 카탈로그 종류를 연결한 테스트 문서를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, created_at, updated_at, origin_work_id) "
        "VALUES (?, 't', ?, 'living', ?, 'normative', 'desired', 'u-tester', ?, ?, ?)",
        (doc_id, doc_id, kind_id[0], NOW, NOW, work_id),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, ?)", (doc_id, kind_id)
    )


def _propose(con, patches, work_id=None):
    """AI 행위자로 관계를 제안한다."""
    result = DocDB(con, Actor("ai-claude", "ai"), work_id).put(patches)
    assert "NOTHING_APPLIED" not in result
    return result


def test_t17_1_ctx_shows_global_pending_link_count(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _work(con)
        for doc_id in ("DOC-ONE", "DOC-TWO"):
            _document(con, doc_id, "D01")
        _propose(
            con,
            [
                {
                    "op": "link",
                    "from": "w:WORK-2026-0001",
                    "rel": "changes",
                    "to": "d:DOC-ONE",
                    "why": "첫 제안",
                }
            ],
            WORK,
        )
        _propose(
            con,
            [
                {
                    "op": "link",
                    "from": "w:WORK-2026-0001",
                    "rel": "changes",
                    "to": "d:DOC-TWO",
                    "why": "둘째 제안",
                }
            ],
        )

        output = DocDB(con, Actor("ai-claude", "ai")).ctx(WORK)

        assert "! pending_links=2" in output.splitlines()
        assert relations.pending_count(con, WORK) == 1
        assert relations.pending_count(con) == 2
    finally:
        con.close()


def test_t17_2_ctx_omits_zero_pending_link_count(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _work(con)

        output = DocDB(con, Actor("ai-claude", "ai")).ctx(WORK)

        assert not any(line.startswith("! pending_links=") for line in output.splitlines())
    finally:
        con.close()


def test_t17_3_ctx_lists_missing_required_kind_for_related_document(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _work(con)
        _document(con, "DOC-REQ", "D01", WORK)

        output = DocDB(con, Actor("ai-claude", "ai")).ctx(WORK)

        assert "! missing_link d:DOC-REQ k:L02" in output.splitlines()
        assert relations.missing_links(con, ["DOC-REQ"]) == [("DOC-REQ", "L02")]
    finally:
        con.close()


def test_t17_4_ctx_clears_missing_link_after_confirmed_relation(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        _work(con)
        _document(con, "DOC-REQ", "D01", WORK)
        _document(con, "DOC-TEST", "L02")
        _propose(
            con,
            [
                {
                    "op": "link",
                    "from": "d:DOC-REQ",
                    "rel": "references",
                    "to": "d:DOC-TEST",
                    "why": "필수 연결",
                }
            ],
            WORK,
        )
        rel_id = con.execute("SELECT rel_id FROM relation").fetchone()[0]
        assert relations.confirm(con, Actor("u-tester", "human"), [rel_id], True) == [
            f"OK rel={rel_id} confirmed"
        ]

        output = DocDB(con, Actor("ai-claude", "ai")).ctx(WORK)

        assert "! missing_link d:DOC-REQ k:L02" not in output.splitlines()
        assert relations.missing_links(con, ["DOC-REQ"]) == []
    finally:
        con.close()


def test_t17_5_trace_hides_proposed_and_rejected_relations(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        for doc_id in ("DOC-FROM", "DOC-PROPOSED", "DOC-REJECTED", "DOC-CONFIRMED"):
            _document(con, doc_id, "D01")
        _propose(
            con,
            [
                {
                    "op": "link",
                    "from": "d:DOC-FROM",
                    "rel": "references",
                    "to": "d:DOC-PROPOSED",
                    "why": "대기",
                },
                {
                    "op": "link",
                    "from": "d:DOC-FROM",
                    "rel": "references",
                    "to": "d:DOC-REJECTED",
                    "why": "반려 예정",
                },
                {
                    "op": "link",
                    "from": "d:DOC-FROM",
                    "rel": "references",
                    "to": "d:DOC-CONFIRMED",
                    "why": "확정 예정",
                },
            ],
        )
        rel_ids = con.execute("SELECT rel_id FROM relation ORDER BY rel_id").fetchall()
        assert relations.reject(con, Actor("u-tester", "human"), rel_ids[1][0], "반려", True) == (
            f"OK rel={rel_ids[1][0]} rejected"
        )
        assert relations.confirm(con, Actor("u-tester", "human"), [rel_ids[2][0]], True) == [
            f"OK rel={rel_ids[2][0]} confirmed"
        ]

        output = DocDB(con, Actor("ai-claude", "ai")).trace("d:DOC-FROM")

        assert output.splitlines()[1:] == [
            "d:DOC-FROM -references-> d:DOC-CONFIRMED"
        ]
    finally:
        con.close()


def test_t17_6_small_ctx_budget_omits_missing_link_but_keeps_pending_count(
    project_db, frozen_now
):
    con = db.connect(project_db)
    try:
        _work(con)
        _document(con, "DOC-REQ", "D01", WORK)
        _document(con, "DOC-OTHER", "D01")
        _propose(
            con,
            [
                {
                    "op": "link",
                    "from": "d:DOC-REQ",
                    "rel": "references",
                    "to": "d:DOC-OTHER",
                    "why": "대기 관계",
                }
            ],
        )

        output = DocDB(con, Actor("ai-claude", "ai")).ctx(WORK, budget=1)

        assert "! pending_links=1" in output.splitlines()
        assert "! missing_link d:DOC-REQ k:L02" not in output.splitlines()
        assert "omitted=" in output.splitlines()[0]
    finally:
        con.close()
