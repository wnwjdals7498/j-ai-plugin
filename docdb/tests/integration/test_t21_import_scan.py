"""T21 import 분할과 scan 수용 테스트."""
import json
import re
import shutil
import unicodedata
from pathlib import Path

import pytest

from docdb import db, history, importer, paths, textnorm
from docdb.actors import Actor
from docdb.errors import DocdbError


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
    for name in ("요구사항.md", "서버 목록.md", "notes.txt", "bad.bin", "bom.md", "crlf.md"):
        shutil.copyfile(source / name, folder / name)
    return folder


def _scan(run_cli, project_db: Path, folder: Path, policy: str = "approved_providers"):
    """임시 프로젝트에서 import scan 명령을 실행한다."""
    return run_cli(
        [
            "--db",
            str(project_db),
            "--actor",
            "u-tester",
            "import",
            "scan",
            str(folder),
            "--owner",
            "u-tester",
            "--export-policy",
            policy,
        ]
    )


def _artifact(con, uri: str) -> str:
    """URI에 해당하는 artifact ID를 반환한다."""
    return con.execute("SELECT artifact_id FROM artifact WHERE uri=?", (uri,)).fetchone()[0]


def test_t21_1_split_ignores_fenced_headings(plugin_root):
    """분할은 펜스 안 제목을 제외하고 실제 제목을 평면 구간으로 나눈다."""
    raw = (plugin_root / "tests" / "fixtures" / "import" / "요구사항.md").read_bytes()
    segments = importer.split_segments(raw)

    assert [(segment["seq"], segment["heading"]) for segment in segments] == [
        (0, "요구사항"),
        (1, "behavior"),
        (2, "기타 메모"),
        (3, "scope"),
    ]
    wide_space = importer.split_segments("　\n# A\n".encode("utf-8"))
    assert len(wide_space) == 1
    assert [(segment["seq"], segment["heading"]) for segment in wide_space] == [(0, "A")]
    assert importer.split_segments("　\n".encode("utf-8")) == []


def test_t21_2_segment_offsets_select_exact_text(plugin_root):
    """구간 오프셋은 원본 바이트를 잘라 같은 텍스트를 돌려준다."""
    source = plugin_root / "tests" / "fixtures" / "import"
    for name in ("요구사항.md", "crlf.md", "bom.md"):
        raw = (source / name).read_bytes()
        for segment in importer.split_segments(raw):
            assert raw[segment["start_off"] : segment["end_off"]].decode("utf-8") == segment["text"]


def test_t21_3_crlf_offsets_and_digest_are_normalized(
    plugin_root, run_cli, project_db, import_folder
):
    """CRLF 오프셋은 바이트 기준이고 digest는 LF 파일과 같다."""
    source = plugin_root / "tests" / "fixtures" / "import"
    raw = (source / "crlf.md").read_bytes()
    assert b"\r\n" in raw  # 픽스처의 CRLF가 보존되어야 이 행이 의미를 가진다
    lf_raw = raw.replace(b"\r\n", b"\n")
    segments = importer.split_segments(raw)
    lf_segments = importer.split_segments(lf_raw)

    assert [raw[s["start_off"] : s["end_off"]].decode("utf-8") for s in segments] == [
        s["text"] for s in segments
    ]
    assert [textnorm.digest16(s["text"]) for s in segments] == [
        textnorm.digest16(s["text"]) for s in lf_segments
    ]
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        artifact_id = _artifact(con, "source/crlf.md")
        stored = con.execute(
            "SELECT start_off, end_off, text_digest FROM import_segment "
            "WHERE artifact_id=? ORDER BY seq",
            (artifact_id,),
        ).fetchall()
        assert [row[2] for row in stored] == [textnorm.digest16(s["text"]) for s in segments]
        assert all(
            raw[start:end].decode("utf-8") == segment["text"]
            for (start, end, _), segment in zip(stored, segments)
        )
    finally:
        con.close()


def test_t21_4_scan_rejects_folder_outside_project(project_db, tmp_path):
    """scan은 프로젝트 루트 밖의 폴더를 BAD_ARGS로 거부한다."""
    con = db.connect(project_db)
    try:
        with pytest.raises(DocdbError) as exc_info:
            importer.scan(con, Actor("u-tester", "human"), tmp_path.parent, "u-tester", "approved_providers")
        assert exc_info.value.code == "BAD_ARGS"
        docdb_dir = project_db.parent
        nested_docdb_dir = docdb_dir / "nested"
        nested_docdb_dir.mkdir()
        for folder in (docdb_dir, nested_docdb_dir):
            with pytest.raises(DocdbError) as exc_info:
                importer.scan(
                    con,
                    Actor("u-tester", "human"),
                    folder,
                    "u-tester",
                    "approved_providers",
                )
            assert exc_info.value.code == "BAD_ARGS"
    finally:
        con.close()


def test_t21_5_scan_registers_files_and_handles_bom(run_cli, project_db, import_folder):
    """scan은 여섯 파일을 등록하고 잘못된 UTF-8과 BOM을 처리한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        rows = con.execute("SELECT uri, digest FROM artifact ORDER BY uri").fetchall()
        assert {uri for uri, _digest in rows} == {
            "source/bad.bin",
            "source/bom.md",
            "source/crlf.md",
            "source/notes.txt",
            "source/서버 목록.md",
            "source/요구사항.md",
        }
        assert all("\\" not in uri and not uri.startswith("/") for uri, _digest in rows)
        bad_id = _artifact(con, "source/bad.bin")
        assert con.execute(
            "SELECT count(*) FROM import_segment WHERE artifact_id=?", (bad_id,)
        ).fetchone()[0] == 0
        bom_id = _artifact(con, "source/bom.md")
        first = con.execute(
            "SELECT heading, start_off, end_off FROM import_segment "
            "WHERE artifact_id=? ORDER BY seq LIMIT 1",
            (bom_id,),
        ).fetchone()
        assert first[0:2] == ("요구사항", 3)
        bom_raw = (import_folder / "bom.md").read_bytes()
        assert bom_raw[first[1] : first[2]].decode("utf-8").startswith("# 요구사항\n")
    finally:
        con.close()

    nfd_heading = unicodedata.normalize("NFD", "한글 제목")
    (import_folder / "nfd.md").write_text(f"# {nfd_heading}\n", encoding="utf-8")
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        stored_heading = con.execute(
            "SELECT s.heading FROM import_segment s JOIN artifact a USING (artifact_id) "
            "WHERE a.uri='source/nfd.md'"
        ).fetchone()[0]
        assert stored_heading == unicodedata.normalize("NFC", nfd_heading)
    finally:
        con.close()


def test_t21_6_scan_maps_document_kinds(run_cli, project_db, import_folder):
    """파일 제목이나 첫 H1이 카탈로그 종류를 결정한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        mapped = {
            uri: kind
            for uri, kind in con.execute(
                "SELECT a.uri, s.kind_id FROM artifact a JOIN import_segment s USING (artifact_id) "
                "WHERE a.uri IN ('source/요구사항.md','source/bom.md','source/crlf.md',"
                "'source/서버 목록.md') GROUP BY a.uri, s.kind_id"
            )
        }
        assert mapped == {
            "source/요구사항.md": "D01",
            "source/bom.md": "D01",
            "source/crlf.md": "D01",
            "source/서버 목록.md": "K02",
        }
        notes_id = _artifact(con, "source/notes.txt")
        assert con.execute(
            "SELECT count(*) FROM import_segment WHERE artifact_id=? AND kind_id IS NOT NULL",
            (notes_id,),
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_t21_7_ruled_behavior_is_applied_without_heading(run_cli, project_db, import_folder):
    """규칙이 맞는 behavior 본문은 제목 없이 필드에 적용된다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        row = con.execute(
            "SELECT v.value_text, s.status FROM field_value v JOIN import_segment s "
            "ON s.doc_id=v.doc_id AND s.kind_id=v.kind_id AND s.field_key=v.field_key "
            "JOIN artifact a ON a.artifact_id=s.artifact_id "
            "WHERE a.uri='source/요구사항.md' AND s.heading='behavior'"
        ).fetchone()
        assert row == ("첫 번째 요구사항 동작입니다.\n두 번째 요구사항 동작입니다.", "applied")
    finally:
        con.close()


def test_t21_8_imported_document_uses_owner_policy_and_fallback_id(run_cli, project_db, import_folder):
    """한글 파일명 문서는 순번 ID와 요청한 소유자·전송 정책을 사용한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        row = con.execute(
            "SELECT d.doc_id, d.owner_id, d.export_policy FROM document d "
            "JOIN import_segment s ON s.doc_id=d.doc_id JOIN artifact a USING (artifact_id) "
            "WHERE a.uri='source/요구사항.md' AND s.heading='behavior'"
        ).fetchone()
        assert row is not None
        doc_id, owner, export_policy = row
        assert re.fullmatch(r"DOC-D01-IMP-\d{4}", doc_id)
        assert owner == "u-tester" and export_policy == "approved_providers"
        kind_change = con.execute(
            "SELECT after_json, actor_id, work_id FROM change_log "
            "WHERE entity_type='document_kind' AND entity_id=? AND op='insert'",
            (f"{doc_id}|D01",),
        ).fetchone()
        assert kind_change is not None
        assert json.loads(kind_change[0]) == {"doc_id": doc_id, "kind_id": "D01"}
        assert kind_change[1:] == ("u-tester", None)

        deleted_doc_id = "DOC-D01-REMOVED"
        with db.write_tx(con):
            history.append_change(
                con,
                "document",
                deleted_doc_id,
                "delete",
                {"doc_id": deleted_doc_id},
                None,
                "u-tester",
                None,
                None,
            )
        assert importer._new_doc_id(
            con, "D01", Path("removed.md"), "ART-2026-0001"
        ) == f"{deleted_doc_id}-2"
    finally:
        con.close()


def test_t21_9_empty_ruled_segment_is_skipped(run_cli, project_db, import_folder):
    """빈 scope 구간은 skipped 상태와 empty 사유를 저장한다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        row = con.execute(
            "SELECT s.status, s.why FROM import_segment s JOIN artifact a USING (artifact_id) "
            "WHERE a.uri='source/요구사항.md' AND s.heading='scope'"
        ).fetchone()
        assert row == ("skipped", "empty")
        document_count = con.execute("SELECT count(*) FROM document").fetchone()[0]
    finally:
        con.close()

    (import_folder / "empty.md").write_text(
        "# 요구사항\n\n## scope\n\n## behavior\n\n", encoding="utf-8"
    )
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        empty_rows = con.execute(
            "SELECT s.heading, s.status, s.why, s.doc_id FROM import_segment s "
            "JOIN artifact a USING (artifact_id) WHERE a.uri='source/empty.md' "
            "AND s.field_key IS NOT NULL ORDER BY s.seq"
        ).fetchall()
        assert empty_rows == [
            ("scope", "skipped", "empty", None),
            ("behavior", "skipped", "empty", None),
        ]
        assert con.execute("SELECT count(*) FROM document").fetchone()[0] == document_count
    finally:
        con.close()


def test_t21_10_resources_rule_creates_item(run_cli, project_db, import_folder):
    """K02 resources 필드 적용은 R1으로 RES 항목을 만든다."""
    code, _out, err = _scan(run_cli, project_db, import_folder)
    assert code == 0, err
    con = db.connect(project_db)
    try:
        row = con.execute(
            "SELECT item_id, title, defined_in FROM item WHERE item_id='RES-DB01'"
        ).fetchone()
        assert row is not None
        assert row[0:2] == ("RES-DB01", "운영 DB")
        assert con.execute(
            "SELECT value_text FROM field_value WHERE doc_id=? AND kind_id='K02' AND field_key='resources'",
            (row[2],),
        ).fetchone()[0] == "RES-DB01 운영 DB"
    finally:
        con.close()


def test_t21_11_rescan_skips_existing_artifacts_and_segments(run_cli, project_db, import_folder):
    """같은 원본을 다시 scan해도 artifact나 구간을 중복 추가하지 않는다."""
    first_code, _out, first_err = _scan(run_cli, project_db, import_folder)
    assert first_code == 0, first_err
    con = db.connect(project_db)
    try:
        tables = ("artifact", "import_segment", "document", "field_value", "change_log")
        before = tuple(
            con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
        )
    finally:
        con.close()
    second_code, _out, second_err = _scan(run_cli, project_db, import_folder)
    assert second_code == 0, second_err
    con = db.connect(project_db)
    try:
        after = tuple(
            con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
        )
        assert after == before
    finally:
        con.close()

    (import_folder / "heading-secret.md").write_text(
        "## api_key: AKIA1234567890123456\n본문\n", encoding="utf-8"
    )
    secret_code, _out, secret_err = _scan(run_cli, project_db, import_folder)
    assert secret_code == 1
    assert "ERROR SECRET_DETECTED" in secret_err
    con = db.connect(project_db)
    try:
        after_secret = tuple(
            con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables
        )
        assert after_secret == before
    finally:
        con.close()


def test_t21_12_scan_creates_pre_import_backup(run_cli, project_db, import_folder, tmp_path, monkeypatch):
    """scan을 시작하면 pre-import-scan 백업을 만든다."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    code, _out, err = _scan(run_cli, project_db, import_folder)

    assert code == 0, err
    backups = list((tmp_path / "data" / "backups" / "t").glob("*-pre-import-scan.sqlite"))
    assert backups and all(path.is_file() for path in backups)
