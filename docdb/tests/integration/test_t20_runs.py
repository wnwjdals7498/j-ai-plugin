"""T20 실행 결과 적재 및 규칙 R4 수용 테스트."""
import io
import json
from pathlib import Path

import pytest

from docdb import cli, db, relations, runs, workflow
from docdb.actors import Actor
from docdb.errors import DocdbError
from docdb.llm import DocDB

_BASE_PAYLOAD = {
    "run_type": "test",
    "target_ref": "c1234567",
    "result": "PASS",
    "runner": "pytest",
    "trust_source": "ci",
}


def _create_item(con, item_id, title="테스트 항목"):
    """테스트용 항목을 DB에 생성한다."""
    DocDB(con, Actor("u-tester", "human")).put(
        [{"h": f"i:{item_id}", "op": "upsert", "title": title}]
    )


def _create_work(con):
    """테스트용 작업을 DB에 생성하고 ID를 반환한다."""
    client = DocDB(con, Actor("ai-claude", "ai"))
    res = client.put(
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
    return res.split("OK w:", 1)[1].split()[0]


def test_t20_1_ingest_required_keys_only(project_db, frozen_now, capsys):
    con = db.connect(project_db)
    try:
        actor = Actor("u-tester", "human")
        run_id = runs.ingest(con, actor, dict(_BASE_PAYLOAD))
        assert run_id == "RUN-2026-0001"

        row = con.execute(
            "SELECT run_id, run_type, target_ref, result, runner, trust_source, recorded_at "
            "FROM run WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert row == (
            "RUN-2026-0001",
            "test",
            "c1234567",
            "PASS",
            "pytest",
            "ci",
            "2026-10-13T00:00:00+00:00",
        )

        history_row = con.execute(
            "SELECT entity_type, entity_id, op, actor_id FROM change_log "
            "WHERE entity_type='run' AND entity_id=?",
            (run_id,),
        ).fetchone()
        assert history_row == ("run", "RUN-2026-0001", "insert", "u-tester")
    finally:
        con.close()


def test_t20_2_invalid_result_or_missing_keys(project_db):
    con = db.connect(project_db)
    try:
        actor = Actor("u-tester", "human")
        # result="OK" (허용 5종 밖)
        bad_result = dict(_BASE_PAYLOAD, result="OK")
        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, actor, bad_result)
        assert exc_info.value.code == "BAD_ARGS"

        # 필수 키 누락 각각 확인
        for key in ("run_type", "target_ref", "result", "runner", "trust_source"):
            missing = dict(_BASE_PAYLOAD)
            del missing[key]
            with pytest.raises(DocdbError) as exc_info:
                runs.ingest(con, actor, missing)
            assert exc_info.value.code == "BAD_ARGS"
    finally:
        con.close()


def test_t20_3_ai_actor_forces_ai_reported_trust_source(project_db):
    con = db.connect(project_db)
    try:
        actor = Actor("ai-claude", "ai")
        # payload에는 trust_source='ci'로 전달
        payload = dict(_BASE_PAYLOAD, trust_source="ci")
        run_id = runs.ingest(con, actor, payload)
        row = con.execute("SELECT trust_source FROM run WHERE run_id=?", (run_id,)).fetchone()
        assert row == ("ai_reported",)
    finally:
        con.close()


def test_t20_4_raw_log_creates_evidence_artifact(project_db):
    con = db.connect(project_db)
    try:
        actor = Actor("u-tester", "human")
        payload = dict(
            _BASE_PAYLOAD,
            raw_log={"uri": "logs/run.txt", "digest": "sha256:abcd1234abcd1234"},
        )
        run_id = runs.ingest(con, actor, payload)
        run_row = con.execute(
            "SELECT raw_artifact_id FROM run WHERE run_id=?", (run_id,)
        ).fetchone()
        assert run_row is not None
        artifact_id = run_row[0]
        assert artifact_id == "ART-2026-0001"

        art_row = con.execute(
            "SELECT artifact_id, uri, digest, role, source_mode FROM artifact WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        assert art_row == (
            "ART-2026-0001",
            "logs/run.txt",
            "sha256:abcd1234abcd1234",
            "evidence",
            "external_reference",
        )

        # change_log에 artifact 기록 확인
        art_hist = con.execute(
            "SELECT entity_type, entity_id, op FROM change_log WHERE entity_type='artifact' AND entity_id=?",
            (artifact_id,),
        ).fetchone()
        assert art_hist == ("artifact", "ART-2026-0001", "insert")
    finally:
        con.close()


def test_t20_5_covers_creates_r4_relation(project_db):
    con = db.connect(project_db)
    try:
        _create_item(con, "TC-HW-001", "하드웨어 점검 테스트")
        work_id = _create_work(con)
        actor = Actor("u-tester", "human")
        payload = dict(_BASE_PAYLOAD, work_id=work_id, covers=["TC-HW-001"])
        run_id = runs.ingest(con, actor, payload)

        rel = con.execute(
            "SELECT from_type, from_id, rel_type, to_type, to_id, status, source, anchor "
            "FROM relation WHERE from_id=? AND to_id='TC-HW-001'",
            (run_id,),
        ).fetchone()
        assert rel == (
            "run",
            run_id,
            "evidence_for",
            "item",
            "TC-HW-001",
            "confirmed",
            "rule:R4",
            f"r:{run_id}",
        )

        # change_log에 relation R4 기록 확인 (actor_id, work_id 포함)
        rel_hist = con.execute(
            "SELECT entity_type, op, reason, actor_id, work_id FROM change_log "
            "WHERE entity_type='relation' AND reason='rule:R4'",
        ).fetchone()
        assert rel_hist == ("relation", "insert", "rule:R4", "u-tester", work_id)

        # since 출력에 relation 엔터티 줄이 없음을 단정
        since_output = DocDB(con, actor).since(cursor=0)
        assert not any(line.startswith("relation:") for line in since_output.splitlines())
    finally:
        con.close()


def test_t20_6_nonexistent_cover_raises_not_found(project_db):
    con = db.connect(project_db)
    try:
        actor = Actor("u-tester", "human")
        payload = dict(_BASE_PAYLOAD, covers=["TC-NONEXISTENT-999"])
        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, actor, payload)
        assert exc_info.value.code == "NOT_FOUND"

        # 아무것도 저장되지 않았는지 확인
        count = con.execute("SELECT count(*) FROM run").fetchone()[0]
        assert count == 0
        art_count = con.execute("SELECT count(*) FROM artifact").fetchone()[0]
        assert art_count == 0
    finally:
        con.close()


def test_t20_7_findings_saved_or_secret_aborts_all(project_db):
    con = db.connect(project_db)
    try:
        _create_item(con, "TC-HW-001", "하드웨어 점검 테스트")
        actor = Actor("u-tester", "human")

        # 1건 정상 저장
        clean_finding = {
            "code": "DRIFT",
            "severity": "warning",
            "target_type": "item",
            "target_id": "TC-HW-001",
            "message": "드리프트 발견",
            "evidence": "상세 근거",
        }
        payload = dict(_BASE_PAYLOAD, findings=[clean_finding])
        run_id = runs.ingest(con, actor, payload)
        finding_row = con.execute(
            "SELECT run_id, code, severity, target_type, target_id, message, evidence "
            "FROM finding WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert finding_row == (
            run_id,
            "DRIFT",
            "warning",
            "item",
            "TC-HW-001",
            "드리프트 발견",
            "상세 근거",
        )

        finding_hist = con.execute(
            "SELECT entity_type, op FROM change_log WHERE entity_type='finding'"
        ).fetchone()
        assert finding_hist == ("finding", "insert")

        # 가짜 AWS 키 포함 시 SECRET_DETECTED 및 전체 롤백
        fake_secret = "AKIA" + "0" * 16
        secret_finding = {
            "code": "VULN",
            "severity": "critical",
            "target_type": "item",
            "target_id": "TC-HW-001",
            "message": f"비밀키 유출: {fake_secret}",
        }
        secret_payload = dict(_BASE_PAYLOAD, findings=[secret_finding])
        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, actor, secret_payload)
        assert exc_info.value.code == "SECRET_DETECTED"
        assert fake_secret not in str(exc_info.value)
        assert fake_secret not in exc_info.value.detail

        # 새 run이나 finding이 추가되지 않았음 (롤백 확인)
        run_count = con.execute("SELECT count(*) FROM run").fetchone()[0]
        assert run_count == 1
        finding_count = con.execute("SELECT count(*) FROM finding").fetchone()[0]
        assert finding_count == 1

        # limitations에 키 포함 시 SECRET_DETECTED 및 행 변화 없음
        fake_lim = "password = hunter2hunter2"
        lim_payload = dict(_BASE_PAYLOAD, limitations=fake_lim)
        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, actor, lim_payload)
        assert exc_info.value.code == "SECRET_DETECTED"
        assert fake_lim not in str(exc_info.value)
        assert fake_lim not in exc_info.value.detail
        assert con.execute("SELECT count(*) FROM run").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM finding").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0

        # command_argv에 키 포함 시 SECRET_DETECTED 및 행 변화 없음
        fake_argv_key = "AKIA" + "1" * 16
        argv_payload = dict(_BASE_PAYLOAD, command_argv=["x", f"--token={fake_argv_key}"])
        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, actor, argv_payload)
        assert exc_info.value.code == "SECRET_DETECTED"
        assert fake_argv_key not in str(exc_info.value)
        assert fake_argv_key not in exc_info.value.detail
        assert con.execute("SELECT count(*) FROM run").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM finding").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0

        # raw_log.uri에 키 포함 시 SECRET_DETECTED 및 행 변화 없음
        fake_uri_key = "sk-ant-" + "2" * 20
        raw_log_payload = dict(_BASE_PAYLOAD, raw_log={"uri": f"logs/{fake_uri_key}.txt"})
        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, actor, raw_log_payload)
        assert exc_info.value.code == "SECRET_DETECTED"
        assert fake_uri_key not in str(exc_info.value)
        assert fake_uri_key not in exc_info.value.detail
        assert con.execute("SELECT count(*) FROM run").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM finding").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM artifact").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM relation").fetchone()[0] == 0

        # 키 거부 후 정상 실행 적재 시 RUN ID가 건너뛰지 않고 발급됨 (RUN ID 비소비 검증)
        clean_run_id = runs.ingest(con, actor, dict(_BASE_PAYLOAD))
        assert clean_run_id == "RUN-2026-0002"
        assert con.execute("SELECT count(*) FROM run").fetchone()[0] == 2
    finally:
        con.close()


def test_t20_8_same_payload_twice_creates_new_run(project_db):
    con = db.connect(project_db)
    try:
        actor = Actor("u-tester", "human")
        run_id_1 = runs.ingest(con, actor, dict(_BASE_PAYLOAD))
        run_id_2 = runs.ingest(con, actor, dict(_BASE_PAYLOAD))
        assert run_id_1 == "RUN-2026-0001"
        assert run_id_2 == "RUN-2026-0002"
        rows = con.execute("SELECT run_id FROM run ORDER BY run_id").fetchall()
        assert [r[0] for r in rows] == ["RUN-2026-0001", "RUN-2026-0002"]
    finally:
        con.close()


def test_t20_9_failed_run_with_work_id_appears_in_ctx(project_db):
    con = db.connect(project_db)
    try:
        work_id = _create_work(con)
        actor = Actor("u-tester", "human")
        payload = dict(_BASE_PAYLOAD, work_id=work_id, result="FAIL")
        run_id = runs.ingest(con, actor, payload)

        ctx_output = DocDB(con, actor).ctx(work_id)
        assert f"! r:{run_id} test FAIL" in ctx_output.splitlines()
    finally:
        con.close()


def test_t20_10_cli_run_ingest_file_and_stdin(project_db, tmp_path, monkeypatch, capsys):
    # 파일 입력 테스트
    json_path = tmp_path / "run_input.json"
    json_path.write_text(json.dumps(_BASE_PAYLOAD), encoding="utf-8")

    rc = cli.main(["--db", str(project_db), "--actor", "u-tester", "run", "ingest", str(json_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == "OK r:RUN-2026-0001\n"

    # 표준 입력 (-) 테스트
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_BASE_PAYLOAD)))
    rc = cli.main(["--db", str(project_db), "--actor", "u-tester", "run", "ingest", "-"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == "OK r:RUN-2026-0002\n"
