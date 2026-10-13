"""T23 템플릿 요약과 put의 sum 통합 테스트."""
import json
import re

import pytest

from docdb import clock, db, summaries, textnorm
from docdb.actors import Actor
from docdb.errors import DocdbError
from docdb.llm import DocDB
from tests.legacy.test_token_port import (
    token_scenario,  # noqa: F401
    test_t12_23_get_shows_fresh_and_stale_summary,
    test_t12_24_summary_staleness_view_marks_changed_source,
)

NOW = "2026-10-13T00:00:00+00:00"


def _document(con, doc_id="DOC-T23", kind_id="D01", origin_work_id=None):
    """테스트 문서를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, export_policy, origin_work_id, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', ?, 'normative', 'desired', 'u-tester', 'public', ?, ?, ?)",
        (doc_id, doc_id, kind_id[0], origin_work_id, NOW, NOW),
    )
    con.execute(
        "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, ?)", (doc_id, kind_id)
    )


def _client(con, actor=None, work=None):
    return DocDB(con, actor or Actor("ai-claude", "ai"), work=work)


def test_t23_1_template_summaries(project_db, frozen_now):
    """행 1: template: 항목 / JSON 객체 필드 / JSON 배열 필드 / 실행"""
    con = db.connect(project_db)
    try:
        client = _client(con)
        # 1. 항목 생성
        client.put([{"h": "i:REQ-HW-001", "op": "upsert", "title": "디스크 경고"}])
        tmpl_item = summaries.template(con, "i:REQ-HW-001")
        assert tmpl_item == "REQ-HW-001 - 디스크 경고"

        # 2. JSON 객체 필드 및 배열 필드
        # D01 카탈로그에 존재하는 필드인 behavior를 활용하거나 cat_kind_field에 등록
        _document(con, "DOC-T23-JSON", "D01")
        con.execute(
            "INSERT INTO cat_kind_field (kind_id, field_key, is_required, description, sort_order) "
            "VALUES ('D01', 'custom_obj', 0, '임의 JSON 필드', 99), "
            "('D01', 'custom_arr', 0, '임의 JSON 배열 필드', 100)"
        )

        # JSON 객체
        obj_payload = json.dumps({"b": 2, "a": 1})
        con.execute(
            "INSERT INTO field_value (doc_id, kind_id, field_key, fill_state, format, value_json, author_id, updated_at) "
            "VALUES ('DOC-T23-JSON', 'D01', 'custom_obj', 'filled', 'json', ?, 'u-tester', ?)",
            (obj_payload, NOW),
        )
        tmpl_obj = summaries.template(con, "f:DOC-T23-JSON/D01/custom_obj")
        assert tmpl_obj == "json keys=a,b"

        # JSON 배열
        arr_payload = json.dumps([1, 2, 3])
        con.execute(
            "INSERT INTO field_value (doc_id, kind_id, field_key, fill_state, format, value_json, author_id, updated_at) "
            "VALUES ('DOC-T23-JSON', 'D01', 'custom_arr', 'filled', 'json', ?, 'u-tester', ?)",
            (arr_payload, NOW),
        )
        tmpl_arr = summaries.template(con, "f:DOC-T23-JSON/D01/custom_arr")
        assert tmpl_arr == "json list n=3"

        # 3. 실행 (run)
        con.execute(
            "INSERT INTO run (run_id, run_type, target_ref, result, runner, trust_source, recorded_at) "
            "VALUES ('RUN-2026-0001', 'test', 'git:c1', 'PASS', 'pytest', 'ci', ?)",
            (NOW,),
        )
        tmpl_run = summaries.template(con, "r:RUN-2026-0001")
        assert tmpl_run == "RUN-2026-0001 test PASS"
    finally:
        con.close()


def test_t23_2_put_with_sum_within_limit_saved_and_shown_in_level_1(project_db, frozen_now):
    """행 2: put + sum(60 tok 이하) -> 저장, get level=1에 sum=<요약>"""
    con = db.connect(project_db)
    try:
        _document(con, "DOC-T23")
        client = _client(con)
        handle = "f:DOC-T23/D01/behavior"
        sum_text = "임계값 85% 설정 및 경고 동작"
        res = client.put([{"h": handle, "text": "본문 내용입니다.", "sum": sum_text}])
        assert "APPLIED ok=1 err=0" in res

        # entity_summary 테이블에 저장 확인
        row = con.execute(
            "SELECT summary, author_type, author_id FROM entity_summary WHERE handle=?",
            (handle,),
        ).fetchone()
        assert row is not None
        assert row[0] == sum_text
        assert row[1] == "llm"
        assert row[2] == "ai-claude"

        # get level=1 확인
        get_res = client.get([handle], level=1)
        body = "본문 내용입니다."
        expected = (
            f'{handle} dg={textnorm.digest16(body)} tok={textnorm.tok_est(body)} sum="{sum_text}"'
        )
        assert get_res.splitlines()[1] == expected
    finally:
        con.close()


def test_t23_3_put_with_sum_exceeding_limit_rejected_and_nothing_applied(project_db, frozen_now):
    """행 3: sum 61 tok 이상 -> ERR … SUMMARY_TOO_LONG, 필드도 적용 안 됨"""
    con = db.connect(project_db)
    try:
        _document(con, "DOC-T23")
        client = _client(con)
        handle = "f:DOC-T23/D01/behavior"
        # 61 tok 이상 생성 (한글 1글자 = 1 tok)
        long_sum = "요약" * 65
        res = client.put([{"h": handle, "text": "적용되면 안 되는 본문", "sum": long_sum}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        tok = textnorm.tok_est(long_sum)
        assert tok > 60
        assert (
            f"ERR f:DOC-T23/D01/behavior SUMMARY_TOO_LONG 요약 토큰 추정치({tok})가 60를 초과함"
            in res.splitlines()
        )

        # 필드도 적용되지 않았는지 확인
        field_row = con.execute(
            "SELECT value_text FROM field_value WHERE doc_id='DOC-T23' AND kind_id='D01' AND field_key='behavior'"
        ).fetchone()
        assert field_row is None
        # entity_summary에도 저장되지 않았는지 확인
        summary_row = con.execute(
            "SELECT summary FROM entity_summary WHERE handle=?", (handle,)
        ).fetchone()
        assert summary_row is None
    finally:
        con.close()


def test_t23_4_field_update_without_sum_marks_summary_stale(project_db, frozen_now):
    """행 4: 요약 후 sum 없이 필드 수정 -> get level=1에 sum=stale"""
    con = db.connect(project_db)
    try:
        _document(con, "DOC-T23")
        client = _client(con)
        handle = "f:DOC-T23/D01/behavior"
        sum_text = "초기 요약"
        client.put([{"h": handle, "text": "초기 본문", "sum": sum_text}])

        get1 = client.get([handle], level=1)
        assert get1.splitlines()[1] == (
            f'{handle} dg={textnorm.digest16("초기 본문")} tok={textnorm.tok_est("초기 본문")} '
            f'sum="{sum_text}"'
        )

        # sum 없이 필드 수정
        client.put([{"h": handle, "text": "수정된 본문"}])

        get2 = client.get([handle], level=1)
        assert get2.splitlines()[1] == (
            f'{handle} dg={textnorm.digest16("수정된 본문")} tok={textnorm.tok_est("수정된 본문")} '
            'sum=stale head="수정된 본문"'
        )
    finally:
        con.close()


def test_t23_5_fake_secret_in_sum_rejected(project_db, frozen_now):
    """행 5: sum에 가짜 키 -> SECRET_DETECTED"""
    con = db.connect(project_db)
    try:
        _document(con, "DOC-T23")
        client = _client(con)
        handle = "f:DOC-T23/D01/behavior"
        fake_key = "AKIA" + "A" * 16
        res = client.put([{"h": handle, "text": "정상 본문", "sum": f"요약 {fake_key}"}])
        assert "NOTHING_APPLIED ok=0 err=1" in res
        assert "SECRET_DETECTED" in res
        assert fake_key not in res

        # 필드 및 요약 미적용 확인
        assert con.execute(
            "SELECT 1 FROM field_value WHERE doc_id='DOC-T23' AND kind_id='D01' AND field_key='behavior'"
        ).fetchone() is None
        assert con.execute("SELECT 1 FROM entity_summary WHERE handle=?", (handle,)).fetchone() is None
    finally:
        con.close()


def test_t23_6_ctx_item_and_run_lines_use_template_summary(project_db, frozen_now):
    """행 6: ctx의 항목·실행 줄 -> 템플릿 요약 사용"""
    con = db.connect(project_db)
    try:
        # 작업 생성
        client = _client(con)
        res_work = client.put(
            [
                {
                    "h": "w:new",
                    "op": "work",
                    "title": "테스트 작업",
                    "work_type": "feature",
                    "owner": "u-tester",
                    "base_refs": ["pmt:docdb/_default/Work1"],
                }
            ]
        )
        work_id = res_work.split("OK w:", 1)[1].split()[0]
        work_client = DocDB(con, Actor("ai-claude", "ai"), work=work_id)

        # 관련 문서 및 항목 생성
        _document(con, "DOC-T23-WORK", "D01", origin_work_id=work_id)
        work_client.put(
            [
                {
                    "h": "i:REQ-HW-001",
                    "op": "upsert",
                    "title": "디스크 경고",
                    "defined_in": "d:DOC-T23-WORK",
                }
            ]
        )

        # 실패한 실행 결과 삽입
        con.execute(
            "INSERT INTO run (run_id, work_id, run_type, target_ref, result, runner, trust_source, recorded_at) "
            "VALUES ('RUN-2026-0001', ?, 'test', 'git:c1', 'FAIL', 'pytest', 'ci', ?)",
            (work_id, NOW),
        )

        ctx_output = work_client.ctx(work_id)

        # 항목 줄 확인: 템플릿 요약 형식 "REQ-HW-001 - 디스크 경고"
        # 원본 docctl_llm.ctx/_item_line 형식 그대로(template을 쓰지 않음)
        assert 'i:REQ-HW-001 requirement - "디스크 경고"' in ctx_output.splitlines()

        # 실행 줄 확인: 템플릿 요약 형식 "RUN-2026-0001 test FAIL"
        assert "! r:RUN-2026-0001 test FAIL" in ctx_output.splitlines()
    finally:
        con.close()


def test_t23_7_legacy_s9_port_check_continues_to_pass(tmp_path, run_cli, mini_paths):
    """행 7: 이식 S9 검사 -> 계속 통과"""
    from tests.legacy.test_token_port import _seed

    # 1. test_t12_23
    dir1 = tmp_path / "db1"
    dir1.mkdir()
    code1, _, _ = run_cli([
        "init", "--project", "t", "--name", "테스트", "--owner", "u-tester", "--owner-name", "Tester",
        "--catalogue", str(mini_paths[0]), "--dir", str(dir1),
    ])
    assert code1 == 0
    con1 = db.connect(dir1 / ".docdb" / "docdb.sqlite")
    try:
        docs1 = _seed(con1)
        test_t12_23_get_shows_fresh_and_stale_summary((con1, docs1, DocDB(con1, Actor("ai-codex", "ai"))))
    finally:
        con1.close()

    # 2. test_t12_24
    dir2 = tmp_path / "db2"
    dir2.mkdir()
    code2, _, _ = run_cli([
        "init", "--project", "t", "--name", "테스트", "--owner", "u-tester", "--owner-name", "Tester",
        "--catalogue", str(mini_paths[0]), "--dir", str(dir2),
    ])
    assert code2 == 0
    con2 = db.connect(dir2 / ".docdb" / "docdb.sqlite")
    try:
        docs2 = _seed(con2)
        test_t12_24_summary_staleness_view_marks_changed_source((con2, docs2, DocDB(con2, Actor("ai-codex", "ai"))))
    finally:
        con2.close()


@pytest.mark.parametrize("bad_sum", [123, ["a"], "  "])
def test_t23_2_put_with_non_string_or_blank_sum_is_bad_op(project_db, frozen_now, bad_sum):
    """행 2 회귀: sum이 문자열이 아니거나 공백뿐 -> BAD_OP, 전체 취소, 저장 변화 없음"""
    con = db.connect(project_db)
    try:
        _document(con, "DOC-T23")
        client = _client(con)
        handle = "f:DOC-T23/D01/behavior"
        before = (
            con.execute("SELECT count(*) FROM field_value").fetchone()[0],
            con.execute("SELECT count(*) FROM entity_summary").fetchone()[0],
        )
        res = client.put([{"h": handle, "text": "적용되면 안 되는 본문", "sum": bad_sum}])
        lines = res.splitlines()
        assert "NOTHING_APPLIED ok=0 err=1" in lines
        assert any(line.startswith(f"ERR {handle} BAD_OP") for line in lines)
        after = (
            con.execute("SELECT count(*) FROM field_value").fetchone()[0],
            con.execute("SELECT count(*) FROM entity_summary").fetchone()[0],
        )
        assert after == before
    finally:
        con.close()
