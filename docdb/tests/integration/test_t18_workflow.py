"""T18 작업 상태와 인계 수용 테스트."""
import json

from docdb import db, workflow
from docdb.actors import Actor
from docdb.llm import DocDB


def _put_lines(result):
    """put 머리줄 다음의 전체 응답 줄을 반환한다."""
    return result.splitlines()[1:]


def _create_work(con, actor=None):
    """테스트 작업을 만들고 ID를 반환한다."""
    client = DocDB(con, actor or Actor("ai-claude", "ai"))
    result = client.put(
        [
            {
                "h": "w:new",
                "op": "work",
                "title": "서버 점검",
                "work_type": "feature",
                "owner": "u-tester",
                "base_refs": ["pmt:docdb/_default/Work1-1"],
            }
        ]
    )
    assert "NOTHING_APPLIED" not in result
    return result.split("OK w:", 1)[1].split()[0]


def _handoff_payload(**updates):
    """필수 항목을 채운 인계 자료를 만든다."""
    payload = {
        "done": [],
        "undone": [],
        "risks": [],
        "next_actions": ["검토 계속"],
        "not_run": [],
        "as_of": 57,
    }
    payload.update(updates)
    return payload


def test_t18_1_put_creates_work_with_base_refs(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        client = DocDB(con, Actor("ai-claude", "ai"))
        result = client.put(
            [
                {
                    "h": "w:new",
                    "op": "work",
                    "title": "서버 점검",
                    "work_type": "feature",
                    "owner": "u-tester",
                    "base_refs": ["pmt:docdb/_default/Work1-1"],
                }
            ]
        )
        assert _put_lines(result) == [
            "APPLIED ok=1 err=0",
            "OK w:WORK-2026-0001 created",
        ]
        row = con.execute(
            "SELECT work_id, project_id, work_type, title, status, owner_id, base_refs, "
            "result_refs, opened_at, closed_at FROM work WHERE work_id='WORK-2026-0001'"
        ).fetchone()
        assert row == (
            "WORK-2026-0001",
            "t",
            "feature",
            "서버 점검",
            "planned",
            "u-tester",
            '["pmt:docdb/_default/Work1-1"]',
            "[]",
            "2026-10-13T00:00:00+00:00",
            None,
        )
        history_row = con.execute(
            "SELECT entity_type, entity_id, op, before_json, after_json, actor_id, work_id, reason "
            "FROM change_log WHERE entity_type='work' AND entity_id='WORK-2026-0001'"
        ).fetchone()
        assert history_row == (
            "work",
            "WORK-2026-0001",
            "insert",
            None,
            '{"base_refs":["pmt:docdb/_default/Work1-1"],"closed_at":null,'
            '"opened_at":"2026-10-13T00:00:00+00:00","owner_id":"u-tester",'
            '"project_id":"t","result_refs":[],"status":"planned",'
            '"title":"서버 점검","work_id":"WORK-2026-0001","work_type":"feature"}',
            "ai-claude",
            "WORK-2026-0001",
            None,
        )

        result = client.put(
            [
                {
                    "h": "w:new",
                    "op": "work",
                    "title": "  공백 보존  ",
                    "work_type": "feature",
                    "owner": "u-tester",
                    "base_refs": [],
                }
            ],
            reason="put reason",
        )
        assert _put_lines(result) == [
            "APPLIED ok=1 err=0",
            "OK w:WORK-2026-0002 created",
        ]
        assert con.execute(
            "SELECT title FROM work WHERE work_id='WORK-2026-0002'"
        ).fetchone() == ("  공백 보존  ",)
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='work' "
            "AND entity_id='WORK-2026-0002' AND op='insert'"
        ).fetchone() == ("put reason",)

        result = client.put(
            [
                {
                    "h": "w:WORK-2026-0002",
                    "op": "work",
                    "status": "active",
                    "why": "patch why",
                }
            ],
            reason="put fallback",
        )
        assert _put_lines(result) == [
            "APPLIED ok=1 err=0",
            "OK w:WORK-2026-0002 status=active",
        ]
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='work' "
            "AND entity_id='WORK-2026-0002' AND op='update'"
        ).fetchone() == ("patch why",)

        secret = "ghp_" + "a" * 36
        change_count = con.execute(
            "SELECT count(*) FROM change_log WHERE entity_type='work' "
            "AND entity_id='WORK-2026-0001'"
        ).fetchone()[0]
        for patches, put_reason in (
            ([{"h": "w:WORK-2026-0001", "op": "work", "status": "active"}], secret),
            ([{"h": "w:WORK-2026-0001", "op": "work", "status": "active", "why": secret}], None),
        ):
            result = client.put(patches, reason=put_reason)
            assert _put_lines(result) == [
                "NOTHING_APPLIED ok=0 err=1",
                "ERR w:WORK-2026-0001 SECRET_DETECTED github_token",
            ]
            assert secret not in result
            assert con.execute(
                "SELECT count(*) FROM change_log WHERE entity_type='work' "
                "AND entity_id='WORK-2026-0001'"
            ).fetchone()[0] == change_count
            assert con.execute(
                "SELECT status FROM work WHERE work_id='WORK-2026-0001'"
            ).fetchone() == ("planned",)
    finally:
        con.close()


def test_t18_2_put_rejects_nonhuman_owner_and_bad_base_ref(project_db):
    con = db.connect(project_db)
    try:
        client = DocDB(con, Actor("ai-claude", "ai"))
        base = {
            "h": "w:new",
            "op": "work",
            "title": "서버 점검",
            "work_type": "feature",
            "owner": "u-tester",
            "base_refs": ["pmt:docdb/_default/Work1-1"],
        }
        nonhuman = dict(base, owner="ai-claude")
        bad_ref = dict(base, base_refs=["x:1"])
        result = client.put([nonhuman])
        assert _put_lines(result) == [
            "NOTHING_APPLIED ok=0 err=1",
            "ERR w:new BAD_ARGS 작업 소유자는 사람 행위자여야 함",
        ]
        result = client.put([bad_ref])
        assert _put_lines(result) == [
            "NOTHING_APPLIED ok=0 err=1",
            "ERR w:new BAD_ARGS 기준 참조 형식이 올바르지 않음",
        ]
        result = client.put([dict(base, title="  ")])
        assert _put_lines(result) == [
            "NOTHING_APPLIED ok=0 err=1",
            "ERR w:new BAD_ARGS 작업 제목 또는 종류가 올바르지 않음",
        ]
        assert con.execute("SELECT count(*) FROM work").fetchone()[0] == 0
    finally:
        con.close()


def test_t18_3_ai_can_activate_and_review_work(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        client = DocDB(con, Actor("ai-claude", "ai"))
        result = client.put(
            [{"h": f"w:{work_id}", "op": "work", "status": "active"}]
        )
        assert _put_lines(result) == [
            "APPLIED ok=1 err=0",
            f"OK w:{work_id} status=active",
        ]
        result = client.put(
            [{"h": f"w:{work_id}", "op": "work", "status": "review"}]
        )
        assert _put_lines(result) == [
            "APPLIED ok=1 err=0",
            f"OK w:{work_id} status=review",
        ]
        assert con.execute(
            "SELECT status FROM work WHERE work_id=?", (work_id,)
        ).fetchone()[0] == "review"
    finally:
        con.close()


def test_t18_4_ai_cannot_verify_work(project_db, frozen_now, run_cli):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        client = DocDB(con, Actor("ai-claude", "ai"))
        client.put([{"h": f"w:{work_id}", "op": "work", "status": "active"}])
        client.put([{"h": f"w:{work_id}", "op": "work", "status": "review"}])
        result = client.put([{"h": f"w:{work_id}", "op": "work", "status": "verified"}])
        assert _put_lines(result) == [
            "NOTHING_APPLIED ok=0 err=1",
            f"ERR w:{work_id} NEEDS_HUMAN 사람 및 대화형 터미널(TTY)이 필요함",
        ]
        assert con.execute(
            "SELECT status FROM work WHERE work_id=?", (work_id,)
        ).fetchone()[0] == "review"
    finally:
        con.close()
    code, out, err = run_cli(
        ["--db", str(project_db), "--actor", "ai-claude", "work", "status", work_id, "verified"],
        interactive=True,
    )
    assert code == 1
    assert out == ""
    assert err == "ERROR NEEDS_HUMAN 사람 및 대화형 터미널(TTY)이 필요함\n"


def test_t18_5_planned_cannot_close_work(project_db, run_cli):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        result = DocDB(con, Actor("ai-claude", "ai")).put(
            [{"h": f"w:{work_id}", "op": "work", "status": "closed"}]
        )
        assert _put_lines(result) == [
            "NOTHING_APPLIED ok=0 err=1",
            f"ERR w:{work_id} STATUS_FORBIDDEN 허용되지 않는 상태 전이: planned → closed",
        ]
        assert con.execute(
            "SELECT status FROM work WHERE work_id=?", (work_id,)
        ).fetchone()[0] == "planned"
    finally:
        con.close()
    code, out, err = run_cli(
        ["--db", str(project_db), "--actor", "ai-claude", "work", "status", work_id, "closed"],
        interactive=True,
    )
    assert code == 1
    assert out == ""
    assert err == "ERROR STATUS_FORBIDDEN 허용되지 않는 상태 전이: planned → closed\n"


def test_t18_6_cli_human_can_verify_then_close_work(project_db, frozen_now, run_cli):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        client = DocDB(con, Actor("ai-claude", "ai"))
        client.put([{"h": f"w:{work_id}", "op": "work", "status": "active"}])
        client.put([{"h": f"w:{work_id}", "op": "work", "status": "review"}])
    finally:
        con.close()
    base = ["--db", str(project_db), "--actor", "u-tester", "work", "status", work_id]
    code, _out, err = run_cli([*base, "verified"], interactive=True)
    assert code == 0, err
    code, _out, err = run_cli([*base, "closed"], interactive=True)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        status, closed_at = con.execute(
            "SELECT status, closed_at FROM work WHERE work_id=?", (work_id,)
        ).fetchone()
        assert status == "closed"
        assert closed_at == "2026-10-13T00:00:00+00:00"
    finally:
        con.close()


def test_t18_7_put_creates_handoff_and_resume_next_actions(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        client = DocDB(con, Actor("ai-claude", "ai"))
        result = client.put(
            [
                {
                    "h": f"w:{work_id}",
                    "op": "handoff",
                    "from_role": " 구현자 ",
                    "to_role": " 검토자 ",
                    "payload": _handoff_payload(),
                    "why": "handoff why",
                }
            ],
            reason="handoff fallback",
        )
        assert _put_lines(result) == [
            "APPLIED ok=1 err=0",
            "OK handoff=HO-2026-0001",
        ]
        handoff_row = con.execute(
            "SELECT handoff_id, work_id, from_role, to_role, payload, ack_status, created_at "
            "FROM handoff WHERE handoff_id='HO-2026-0001'"
        ).fetchone()
        assert handoff_row == (
            "HO-2026-0001",
            work_id,
            " 구현자 ",
            " 검토자 ",
            '{"as_of":57,"done":[],"next_actions":["검토 계속"],"not_run":[],"risks":[],"undone":[]}',
            "pending",
            "2026-10-13T00:00:00+00:00",
        )
        assert con.execute(
            "SELECT reason FROM change_log WHERE entity_type='handoff' "
            "AND entity_id='HO-2026-0001' AND op='insert'"
        ).fetchone() == ("handoff why",)
        resume = con.execute(
            "SELECT next_actions FROM v_work_resume WHERE work_id=?", (work_id,)
        ).fetchone()[0]
        assert json.loads(resume) == ["검토 계속"]

        secret = "ghp_" + "b" * 36
        before_count = con.execute("SELECT count(*) FROM handoff").fetchone()[0]
        before_history = con.execute(
            "SELECT count(*) FROM change_log WHERE entity_type='handoff'"
        ).fetchone()[0]
        for from_role, to_role, expected_detail in (
            ("   ", "검토자", "인계 역할이 비어 있음"),
            ("구현자", " \t ", "인계 역할이 비어 있음"),
            (secret, "검토자", "github_token"),
            ("구현자", secret, "github_token"),
        ):
            result = client.put(
                [
                    {
                        "h": f"w:{work_id}",
                        "op": "handoff",
                        "from_role": from_role,
                        "to_role": to_role,
                        "payload": _handoff_payload(),
                    }
                ]
            )
            error_code = (
                "SECRET_DETECTED" if expected_detail == "github_token" else "BAD_OP"
            )
            assert _put_lines(result) == [
                "NOTHING_APPLIED ok=0 err=1",
                f"ERR w:{work_id} {error_code} {expected_detail}",
            ]
            assert secret not in result
            assert con.execute("SELECT count(*) FROM handoff").fetchone()[0] == before_count
            assert con.execute(
                "SELECT count(*) FROM change_log WHERE entity_type='handoff'"
            ).fetchone()[0] == before_history
    finally:
        con.close()


def test_t18_8_handoff_requires_all_lists_and_integer_as_of(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        client = DocDB(con, Actor("ai-claude", "ai"))
        missing_risks = _handoff_payload()
        del missing_risks["risks"]
        bad_as_of = _handoff_payload(as_of="x")
        for payload in (missing_risks, bad_as_of):
            result = client.put(
                [
                    {
                        "h": f"w:{work_id}",
                        "op": "handoff",
                        "from_role": "구현자",
                        "to_role": "검토자",
                        "payload": payload,
                    }
                ]
            )
            assert _put_lines(result) == [
                "NOTHING_APPLIED ok=0 err=1",
                f"ERR w:{work_id} BAD_OP 인계 자료에 다섯 목록과 정수 as_of가 필요함",
            ]
        assert con.execute("SELECT count(*) FROM handoff").fetchone()[0] == 0
    finally:
        con.close()


def test_t18_9_cli_ack_requires_human_baseline_and_supports_reject(
    project_db, frozen_now, run_cli
):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        client = DocDB(con, Actor("ai-claude", "ai"))
        for _ in range(2):
            client.put(
                [
                    {
                        "h": f"w:{work_id}",
                        "op": "handoff",
                        "from_role": "구현자",
                        "to_role": "검토자",
                        "payload": _handoff_payload(),
                    }
                ]
            )
        handoff_ids = [row[0] for row in con.execute(
            "SELECT handoff_id FROM handoff ORDER BY handoff_id"
        ).fetchall()]
    finally:
        con.close()

    prefix = ["--db", str(project_db), "--actor", "u-tester", "handoff", "ack"]
    code, out, err = run_cli(
        [*prefix, handoff_ids[0], "--baseline", "git:abc123"], interactive=False
    )
    assert code == 1 and out == ""
    assert err.startswith("ERROR NEEDS_HUMAN ")
    code, out, err = run_cli([*prefix, handoff_ids[0]], interactive=True)
    assert code == 1 and out == ""
    assert err.startswith("ERROR BAD_ARGS ")

    code, out, err = run_cli(
        [
            *prefix,
            handoff_ids[0],
            "--baseline",
            "git:abc123",
            "--evidence",
            "doc:review.md",
        ],
        interactive=True,
    )
    assert code == 0, err
    code, out, err = run_cli(
        [*prefix, handoff_ids[1], "--baseline", "git:def456", "--reject"],
        interactive=True,
    )
    assert code == 0, err
    con = db.connect(project_db)
    try:
        statuses = dict(con.execute("SELECT handoff_id, ack_status FROM handoff"))
        assert statuses[handoff_ids[0]] == "accepted"
        assert statuses[handoff_ids[1]] == "rejected"
    finally:
        con.close()


def test_t18_10_work_and_handoff_writes_are_recorded(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        client = DocDB(con, Actor("ai-claude", "ai"))
        client.put([{"h": f"w:{work_id}", "op": "work", "status": "active"}])
        client.put(
            [
                {
                    "h": f"w:{work_id}",
                    "op": "handoff",
                    "from_role": "구현자",
                    "to_role": "검토자",
                    "payload": _handoff_payload(),
                }
            ]
        )
        handoff_id = con.execute("SELECT handoff_id FROM handoff").fetchone()[0]
        result = workflow.ack_handoff(
            con,
            Actor("u-tester", "human"),
            handoff_id,
            "git:abc123",
            [],
            True,
            True,
        )
        assert result == f"OK handoff={handoff_id} accepted"
        rows = con.execute(
            "SELECT entity_type, entity_id, op, actor_id, work_id FROM change_log "
            "WHERE entity_type IN ('work', 'handoff') ORDER BY change_id"
        ).fetchall()
        assert rows == [
            ("work", work_id, "insert", "ai-claude", work_id),
            ("work", work_id, "update", "ai-claude", work_id),
            ("handoff", handoff_id, "insert", "ai-claude", work_id),
            ("handoff", handoff_id, "update", "u-tester", work_id),
        ]
    finally:
        con.close()


def test_t18_11_ctx_shows_created_work_card(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        output = DocDB(con, Actor("ai-claude", "ai")).ctx(work_id)
        assert output.splitlines()[1:] == [
            f'W {work_id} planned owner=u-tester "서버 점검" handoff=-:-'
        ]
    finally:
        con.close()
