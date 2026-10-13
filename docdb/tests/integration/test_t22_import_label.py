"""T22 import next, label, report 수용 테스트."""
import json
import re
import shutil
from pathlib import Path

import pytest

from docdb import db, paths


@pytest.fixture(autouse=True)
def import_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """백업을 테스트 임시 디렉터리에 저장한다."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")


@pytest.fixture
def import_folder(tmp_path: Path, plugin_root: Path) -> Path:
    """가져오기 픽스처를 임시 프로젝트 폴더로 복사한다."""
    folder = tmp_path / "source"
    folder.mkdir()
    source = plugin_root / "tests" / "fixtures" / "import"
    for name in (
        "요구사항.md",
        "서버 목록.md",
        "notes.txt",
        "bad.bin",
        "bom.md",
        "crlf.md",
    ):
        shutil.copyfile(source / name, folder / name)
    return folder


def _scan(run_cli, project_db: Path, folder: Path, export_policy: str = "approved_providers"):
    """임시 프로젝트에서 import scan 명령을 실행한다."""
    return run_cli(
        [
            "--db",
            str(project_db),
            "--actor",
            "ai-codex",
            "import",
            "scan",
            str(folder),
            "--owner",
            "u-tester",
            "--export-policy",
            export_policy,
        ]
    )


def _next(run_cli, project_db: Path, *extra: str):
    """임시 프로젝트에서 import next 명령을 실행한다."""
    return run_cli(
        ["--db", str(project_db), "--actor", "ai-codex", "import", "next", *extra]
    )


def _label(run_cli, project_db: Path, labels: list[dict]):
    """임시 프로젝트에서 JSON 라벨 배열을 적용한다."""
    return run_cli(
        [
            "--db",
            str(project_db),
            "--actor",
            "ai-codex",
            "import",
            "label",
            json.dumps(labels, ensure_ascii=False),
        ]
    )


def _segment_id(con, uri: str, heading: str | None) -> int:
    """원본 경로와 구간 제목으로 구간 ID를 찾는다."""
    if heading is None:
        row = con.execute(
            "SELECT s.seg_id FROM import_segment s JOIN artifact a USING (artifact_id) "
            "WHERE a.uri=? AND s.heading IS NULL",
            (uri,),
        ).fetchone()
    else:
        row = con.execute(
            "SELECT s.seg_id FROM import_segment s JOIN artifact a USING (artifact_id) "
            "WHERE a.uri=? AND s.heading=?",
            (uri, heading),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_t22_1_next_lists_unknown_documents_before_pending_segments(
    run_cli, project_db, import_folder
):
    """next는 미분류 파일을 먼저 내고 구간 제목과 순서를 표시한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err

    code, out, err = _next(run_cli, project_db)
    assert code == 0, err
    lines = out.splitlines()
    assert re.fullmatch(r"#import-next pending=\d+ tok=\d+/1500 omitted=\d+", lines[0])
    doc_line = next(i for i, line in enumerate(lines) if "source/notes.txt" in line)
    segment_line = next(i for i, line in enumerate(lines) if '"기타 메모"' in line)
    assert re.fullmatch(r"d\?:ART-\d{4}-\d{4,} source/notes\.txt heads=.*", lines[doc_line])
    assert re.fullmatch(
        r's:\d+ "기타 메모" tok=\d+ \| .+ \| cand=[A-Z0-9]+/[A-Za-z0-9_]+(?:,[A-Z0-9]+/[A-Za-z0-9_]+)*',
        lines[segment_line],
    )
    assert doc_line < segment_line


def test_t22_2_candidates_are_stable_and_limited_to_five(run_cli, project_db, import_folder):
    """필드 후보는 입력이 같으면 순서가 같고 최대 다섯 개다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err

    first_code, first, first_err = _next(run_cli, project_db)
    second_code, second, second_err = _next(run_cli, project_db)
    assert first_code == second_code == 0, first_err + second_err
    first_line = next(line for line in first.splitlines() if '"기타 메모"' in line)
    second_line = next(line for line in second.splitlines() if '"기타 메모"' in line)
    first_match = re.search(r"cand=([^ ]+)", first_line)
    second_match = re.search(r"cand=([^ ]+)", second_line)
    assert first_match is not None and second_match is not None
    assert first_match.group(1) == second_match.group(1)
    assert len(first_match.group(1).split(",")) <= 5


def test_t22_3_next_budget_reports_omitted_rows(run_cli, project_db, import_folder):
    """작은 출력 예산은 생략 행 수를 보고한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err

    code, out, err = _next(run_cli, project_db, "--budget", "30")
    assert code == 0, err
    match = re.search(r"omitted=(\d+)", out.splitlines()[0])
    assert match is not None and int(match.group(1)) > 0


def test_t22_4_local_only_segment_has_no_excerpt(run_cli, project_db, import_folder):
    """scan run 옵션으로 정책을 판정하고 local_only 발췌를 생략한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        run = con.execute(
            "SELECT run_id, target_ref, runner, trust_source, command_argv, env_snapshot "
            "FROM run WHERE run_type='import'"
        ).fetchone()
        assert run is not None
        run_id, target_ref, runner, trust_source, argv, snapshot = run
        assert con.execute(
            "SELECT count(*) FROM run WHERE run_type='import'"
        ).fetchone()[0] == 1
        assert target_ref == "source" and runner == "docctl"
        assert trust_source == "ai_reported"
        assert json.loads(argv) == [
            "import", "scan", str(import_folder), "--owner", "u-tester",
            "--export-policy", "approved_providers",
        ]
        assert json.loads(snapshot) == {
            "owner": "u-tester", "export_policy": "approved_providers"
        }
        assert con.execute(
            "SELECT count(*) FROM artifact WHERE access_policy_ref=?",
            (f"r:{run_id}",),
        ).fetchone()[0] == 6
        applied_options = con.execute(
            "SELECT d.owner_id, d.export_policy FROM document d "
            "JOIN import_segment s ON s.doc_id=d.doc_id JOIN artifact a USING (artifact_id) "
            "WHERE a.uri='source/요구사항.md' AND s.heading='behavior'"
        ).fetchone()
        assert applied_options == ("u-tester", "approved_providers")
        regular_seg_id = _segment_id(con, "source/요구사항.md", "기타 메모")
        missing_ref_seg_id = _segment_id(con, "source/notes.txt", None)
        notes_artifact = con.execute(
            "SELECT artifact_id FROM artifact WHERE uri='source/notes.txt'"
        ).fetchone()[0]
        with db.write_tx(con):
            con.execute(
                "UPDATE artifact SET access_policy_ref=NULL WHERE artifact_id=?",
                (notes_artifact,),
            )
    finally:
        con.close()

    source = import_folder / "요구사항.md"
    isolated = import_folder.parent / "local-only"
    isolated.mkdir()
    shutil.copyfile(source, isolated / source.name)
    code, _out, err = _scan(run_cli, project_db, isolated, "local_only")
    assert code == 0, err

    con = db.connect(project_db)
    try:
        local_run = con.execute(
            "SELECT run_id, target_ref, env_snapshot FROM run WHERE run_type='import' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert local_run is not None
        local_run_id, local_target, local_snapshot = local_run
        assert con.execute(
            "SELECT count(*) FROM run WHERE run_type='import'"
        ).fetchone()[0] == 2
        assert local_target == "local-only"
        assert json.loads(local_snapshot) == {
            "owner": "u-tester", "export_policy": "local_only"
        }
        assert con.execute(
            "SELECT count(*) FROM artifact WHERE access_policy_ref=?",
            (f"r:{local_run_id}",),
        ).fetchone()[0] == 1
        local_seg_id = _segment_id(con, "local-only/요구사항.md", "기타 메모")
    finally:
        con.close()

    code, out, err = _next(run_cli, project_db)
    assert code == 0, err
    lines = out.splitlines()
    local_line = next(line for line in lines if line.startswith(f"s:{local_seg_id} "))
    assert re.fullmatch(
        rf's:{local_seg_id} "기타 메모" tok=\d+ \| policy=local_only',
        local_line,
    )
    regular_line = next(line for line in lines if line.startswith(f"s:{regular_seg_id} "))
    assert "코드 블록 밖의 메모입니다." in regular_line
    assert not regular_line.endswith("policy=local_only")
    missing_ref_line = next(line for line in lines if line.startswith(f"s:{missing_ref_seg_id} "))
    assert re.fullmatch(
        rf's:{missing_ref_seg_id} "" tok=\d+ \| policy=local_only',
        missing_ref_line,
    )


def test_t22_5_document_label_reapplies_field_rules(run_cli, project_db, import_folder):
    """문서 종류 라벨은 파일 구간에 필드 규칙을 다시 적용한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        artifact_id = con.execute(
            "SELECT artifact_id FROM artifact WHERE uri='source/notes.txt'"
        ).fetchone()[0]
    finally:
        con.close()

    code, out, err = _label(
        run_cli,
        project_db,
        [{"doc": f"d?:{artifact_id}", "kind": "G01"}],
    )
    assert code == 0, err
    assert out == f"OK d?:{artifact_id} kind=G01 ruled=0\n"
    con = db.connect(project_db)
    try:
        note_state = con.execute(
            "SELECT kind_id, field_key, status, rule, doc_id FROM import_segment "
            "WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        assert note_state == ("G01", None, "pending", None, None)
        note_seg_id = _segment_id(con, "source/notes.txt", None)
        requirements_id = con.execute(
            "SELECT artifact_id FROM artifact WHERE uri='source/요구사항.md'"
        ).fetchone()[0]
        before = con.execute(
            "SELECT seg_id, heading, status, doc_id FROM import_segment "
            "WHERE artifact_id=? ORDER BY seq",
            (requirements_id,),
        ).fetchall()
        old_doc_ids = {row[3] for row in before}
        assert len(old_doc_ids - {None}) == 1
        assert [(row[1], row[2]) for row in before] == [
            ("요구사항", "pending"),
            ("behavior", "applied"),
            ("기타 메모", "pending"),
            ("scope", "skipped"),
        ]
        initial_doc_count = con.execute("SELECT count(*) FROM document").fetchone()[0]
        assert con.execute(
            "SELECT rule FROM import_segment WHERE artifact_id=? AND heading='behavior'",
            (requirements_id,),
        ).fetchone()[0] == "field"
    finally:
        con.close()

    code, out, err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{note_seg_id}", "kind": "G01", "field": "overview"}],
    )
    assert code == 0, err
    assert out == f"OK s:{note_seg_id} applied\n"
    con = db.connect(project_db)
    try:
        labeled_doc = con.execute(
            "SELECT d.owner_id, d.export_policy, s.rule, r.env_snapshot "
            "FROM document d JOIN import_segment s ON s.doc_id=d.doc_id "
            "JOIN artifact a USING (artifact_id) JOIN run r "
            "ON a.access_policy_ref='r:' || r.run_id WHERE s.seg_id=?",
            (note_seg_id,),
        ).fetchone()
        assert labeled_doc is not None
        owner, export_policy, rule, snapshot = labeled_doc
        options = json.loads(snapshot)
        assert (owner, export_policy, rule) == (
            options["owner"], options["export_policy"], "agent"
        )
        assert options == {"owner": "u-tester", "export_policy": "approved_providers"}
        old_doc_count = con.execute("SELECT count(*) FROM document").fetchone()[0]
        assert old_doc_count == initial_doc_count + 1
    finally:
        con.close()

    code, out, err = _label(
        run_cli,
        project_db,
        [{"doc": f"d?:{requirements_id}", "kind": "D01"}],
    )
    assert code == 0, err
    assert out == f"OK d?:{requirements_id} kind=D01 ruled=2\n"
    con = db.connect(project_db)
    try:
        after = con.execute(
            "SELECT seg_id, heading, status, doc_id FROM import_segment "
            "WHERE artifact_id=? ORDER BY seq",
            (requirements_id,),
        ).fetchall()
        assert after == before
        assert con.execute("SELECT count(*) FROM document").fetchone()[0] == old_doc_count
        assert {row[3] for row in after} == old_doc_ids
    finally:
        con.close()


def test_t22_6_segment_label_appends_to_existing_field(run_cli, project_db, import_folder):
    """구간 라벨은 같은 문서 필드에 기존 본문 뒤로 적용한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        seg_id = _segment_id(con, "source/요구사항.md", "기타 메모")
        before_state = con.execute(
            "SELECT status, doc_id, rule FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone()
        doc_id = con.execute(
            "SELECT doc_id FROM import_segment WHERE artifact_id=(SELECT artifact_id "
            "FROM import_segment WHERE seg_id=?) AND doc_id IS NOT NULL ORDER BY seq LIMIT 1",
            (seg_id,),
        ).fetchone()[0]
        current = con.execute(
            "SELECT value_text FROM field_value WHERE kind_id='D01' "
            "AND field_key='behavior' AND doc_id IN (SELECT s.doc_id FROM import_segment s "
            "JOIN artifact a USING (artifact_id) WHERE a.uri='source/요구사항.md' "
            "AND s.doc_id IS NOT NULL)",
        ).fetchone()[0]
    finally:
        con.close()

    code, out, err = _label(
        run_cli,
        project_db,
        [
            {"seg": f"s:{seg_id}", "kind": "D01", "field": "behavior"},
            {"seg": f"s:{seg_id}", "kind": "D01", "field": "behavior"},
        ],
    )
    assert code == 1
    assert out == f"ERR s:{seg_id} BAD_ARGS duplicate segment label\n"
    con = db.connect(project_db)
    try:
        state = con.execute(
            "SELECT status, doc_id, rule FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone()
        assert state == before_state
        unchanged = con.execute(
            "SELECT value_text FROM field_value WHERE doc_id=? "
            "AND kind_id='D01' AND field_key='behavior'",
            (doc_id,),
        ).fetchone()[0]
        assert unchanged == current
    finally:
        con.close()

    code, out, err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{seg_id}", "kind": "D01", "field": "behavior"}],
    )
    assert code == 0, err
    assert out == f"OK s:{seg_id} applied\n"
    con = db.connect(project_db)
    try:
        value = con.execute(
            "SELECT value_text FROM field_value WHERE doc_id=(SELECT doc_id "
            "FROM import_segment WHERE seg_id=?) AND kind_id='D01' AND field_key='behavior'",
            (seg_id,),
        ).fetchone()[0]
        status, rule = con.execute(
            "SELECT status, rule FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone()
    finally:
        con.close()
    expected_body = "```\n# 코드 제목\n## 기타 메모\n```\n\n코드 블록 밖의 메모입니다."
    assert value == current + "\n\n" + expected_body
    assert (status, rule) == ("applied", "agent")

    code, out, err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{seg_id}", "kind": "D01", "field": "behavior"}],
    )
    assert code == 1
    assert out == f"ERR s:{seg_id} BAD_ARGS already applied\n"
    con = db.connect(project_db)
    try:
        assert con.execute(
            "SELECT status, rule FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone() == ("applied", "agent")
        assert con.execute(
            "SELECT value_text FROM field_value WHERE doc_id=(SELECT doc_id "
            "FROM import_segment WHERE seg_id=?) AND kind_id='D01' AND field_key='behavior'",
            (seg_id,),
        ).fetchone()[0] == value
    finally:
        con.close()


def test_t22_7_skip_label_stores_reason(run_cli, project_db, import_folder):
    """skip 라벨은 구간을 skipped로 바꾸고 사유를 저장한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        seg_id = _segment_id(con, "source/요구사항.md", "기타 메모")
    finally:
        con.close()

    code, out, err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{seg_id}", "skip": True, "why": "목차"}],
    )
    assert code == 0, err
    assert out == f"OK s:{seg_id} skipped\n"
    con = db.connect(project_db)
    try:
        assert con.execute(
            "SELECT status, why, rule FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone() == ("skipped", "목차", "agent")
    finally:
        con.close()

    code, out, err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{seg_id}", "skip": True, "why": "목차"}],
    )
    assert code == 1
    assert out == f"ERR s:{seg_id} BAD_ARGS already skipped\n"
    con = db.connect(project_db)
    try:
        assert con.execute(
            "SELECT status, why, rule FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone() == ("skipped", "목차", "agent")
    finally:
        con.close()


def test_t22_8_stale_label_cancels_batch_and_marks_segment(run_cli, project_db, tmp_path):
    """변경된 원본의 라벨은 stale로 남고 같은 묶음의 다른 라벨도 취소한다."""
    folder = tmp_path / "source-stale"
    folder.mkdir()
    source = folder / "요구사항.md"
    source.write_text(
        "# 요구사항\n\n## behavior\n기존 본문\n\n## 기타 메모\n첫 메모\n\n"
        "## 추가 메모\n둘째 메모\n",
        encoding="utf-8",
    )
    code, _out, err = _scan(run_cli, project_db, folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        stale_id = _segment_id(con, "source-stale/요구사항.md", "기타 메모")
        other_id = _segment_id(con, "source-stale/요구사항.md", "추가 메모")
    finally:
        con.close()
    source.write_text(
        "# 요구사항\n\n## behavior\n기존 본문\n\n## 기타 메모\n바뀐 메모\n\n"
        "## 추가 메모\n둘째 메모\n",
        encoding="utf-8",
    )

    code, out, err = _label(
        run_cli,
        project_db,
        [
            {"seg": f"s:{stale_id}", "kind": "D01", "field": "behavior"},
            {"seg": f"s:{other_id}", "skip": True, "why": "목차"},
        ],
    )
    assert code == 1
    assert f"ERR s:{stale_id} IMPORT_STALE" in out
    con = db.connect(project_db)
    try:
        assert con.execute(
            "SELECT status FROM import_segment WHERE seg_id=?", (stale_id,)
        ).fetchone()[0] == "stale"
        assert con.execute(
            "SELECT status, why FROM import_segment WHERE seg_id=?", (other_id,)
        ).fetchone() == ("pending", None)
    finally:
        con.close()


def test_t22_9_label_rejects_unknown_keys_and_wrong_kind(run_cli, project_db, import_folder):
    """라벨 키 오류는 BAD_OP, 기존 문서 종류와 다른 라벨은 BAD_ARGS다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        seg_id = _segment_id(con, "source/요구사항.md", "기타 메모")
        before_status = con.execute(
            "SELECT status FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone()[0]
    finally:
        con.close()

    code, out, _err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{seg_id}", "kind": "D01", "field": "behavior", "extra": 1}],
    )
    assert code == 1
    assert out == f"ERR s:{seg_id} BAD_OP 구간 라벨 키가 올바르지 않음\n"
    code, out, _err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{seg_id}", "kind": "G01", "field": "behavior"}],
    )
    assert code == 1
    assert out == f"ERR s:{seg_id} BAD_ARGS 구간에 지정된 문서 종류와 라벨이 다름\n"
    con = db.connect(project_db)
    try:
        artifact_id = con.execute(
            "SELECT artifact_id FROM artifact WHERE uri='source/요구사항.md'"
        ).fetchone()[0]
    finally:
        con.close()
    code, out, _err = _label(
        run_cli,
        project_db,
        [{"doc": f"d?:{artifact_id}", "kind": "G01"}],
    )
    assert code == 1
    assert out == (
        f"ERR d?:{artifact_id} BAD_ARGS 문서 종류와 라벨이 일치하지 않음\n"
    )
    con = db.connect(project_db)
    try:
        assert con.execute(
            "SELECT status FROM import_segment WHERE seg_id=?", (seg_id,)
        ).fetchone()[0] == before_status
    finally:
        con.close()


def test_t22_10_report_counts_files_segments_and_statuses(run_cli, project_db, import_folder):
    """report는 가져온 파일과 구간의 상태별 수를 형식대로 출력한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    code, out, err = run_cli(
        ["--db", str(project_db), "--actor", "ai-codex", "import", "report"]
    )
    assert code == 0, err
    assert out.splitlines() == [
        "#import-report files=6 supported=5 unsupported=1 segments=10",
        "status pending=6 ruled=0 labeled=0 skipped=1 applied=3 stale=0",
    ]


def test_t22_11_label_creates_pre_import_backup(run_cli, project_db, import_folder, tmp_path):
    """label을 시작하면 pre-import-label 백업을 만든다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        seg_id = _segment_id(con, "source/요구사항.md", "기타 메모")
    finally:
        con.close()

    code, _out, err = _label(
        run_cli,
        project_db,
        [{"seg": f"s:{seg_id}", "skip": True, "why": "목차"}],
    )
    assert code == 0, err
    backups = paths.backups_dir("t")
    assert list(backups.glob("*-pre-import-label*.sqlite"))
