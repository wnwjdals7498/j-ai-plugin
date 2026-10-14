"""묶음 FC 최종 리뷰 지적 회귀 테스트."""
import json
from pathlib import Path

import pytest

from docdb import catalogue, db, importer, paths
from docdb.actors import Actor
from docdb.errors import DocdbError


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트 백업을 임시 디렉터리에 저장한다."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")


def _scan_cli(run_cli, project_db: Path, folder: Path):
    """프로젝트 안의 폴더를 CLI로 가져온다."""
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
            "approved_providers",
        ]
    )


def test_fix_l3_scan_skips_invalid_uri_paths_and_warns(
    tmp_path: Path, project_db: Path, run_cli
) -> None:
    """URI 규칙에 맞지 않는 이름은 건너뛰고 정상 파일을 등록한다."""
    folder = tmp_path
    (folder / "v1..2.txt").write_text("# Bad\ntext\n", encoding="utf-8")
    (folder / "bad\\path.txt").write_text("# Bad\ntext\n", encoding="utf-8")
    (folder / "x:drive.txt").write_text("# Bad\ntext\n", encoding="utf-8")
    (folder / "요구사항.md").write_text(
        "# 요구사항\n\n## behavior\n정상 파일 내용입니다.\n", encoding="utf-8"
    )

    code, out, err = _scan_cli(run_cli, project_db, folder)

    assert code == 0, err
    assert out == "WARN skipped_paths=3\n"
    assert "v1..2.txt" not in out
    con = db.connect(project_db)
    try:
        uris = {row[0] for row in con.execute("SELECT uri FROM artifact")}
        assert "요구사항.md" in uris
        assert not {"v1..2.txt", "bad\\path.txt", "x:drive.txt"} & uris
        assert con.execute(
            "SELECT value_text FROM field_value WHERE kind_id='D01' AND field_key='behavior'"
        ).fetchone()[0] == "정상 파일 내용입니다."
    finally:
        con.close()


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_fix_l4_next_hides_changed_or_missing_source(
    change: str, tmp_path: Path, project_db: Path, run_cli
) -> None:
    """현재 파일 digest가 다르거나 파일이 없으면 발췌 없이 stale을 표시한다."""
    folder = tmp_path / "source"
    folder.mkdir()
    source = folder / "unknown.txt"
    source.write_text("# Mystery\nOld preview text\n", encoding="utf-8")
    code, _out, err = _scan_cli(run_cli, project_db, folder)
    assert code == 0, err

    if change == "replace":
        source.write_text("# Mystery\nNew preview text\n", encoding="utf-8")
    else:
        source.unlink()

    code, out, err = run_cli(
        ["--db", str(project_db), "--actor", "u-tester", "import", "next"]
    )

    assert code == 0, err
    pending = next(line for line in out.splitlines() if line.startswith("s:"))
    assert pending.endswith(" stale")
    assert "Old preview text" not in out
    assert "New preview text" not in out


def test_fix_l6_import_labels_use_parse_handle(
    tmp_path: Path,
    project_db: Path,
    human: Actor,
    monkeypatch: pytest.MonkeyPatch,
    con,
) -> None:
    """문서·구간 라벨 모두 공통 핸들 파서를 이용한다."""
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "unknown.txt").write_text("# Mystery\nText\n", encoding="utf-8")
    importer.scan(con, human, folder, "u-tester", "approved_providers")
    artifact_id = con.execute("SELECT artifact_id FROM artifact").fetchone()[0]
    segment_id = con.execute("SELECT seg_id FROM import_segment").fetchone()[0]

    original_parse_handle = importer.parse_handle
    parsed: list[str] = []

    def record_parse_handle(value: str):
        parsed.append(value)
        return original_parse_handle(value)

    monkeypatch.setattr(importer, "parse_handle", record_parse_handle)
    doc_label = {"doc": f"d?:{artifact_id}", "kind": "D01"}
    action, _target, error = importer._plan_label(con, doc_label)
    assert error is None
    assert action is not None and action["artifact_id"] == artifact_id

    segment_label = {"seg": f"s:{segment_id}", "skip": True, "why": "검수 제외"}
    action, _target, error = importer._plan_label(con, segment_label)
    assert error is None
    assert action is not None and action["op"] == "skip"
    with pytest.raises(DocdbError) as exc_info:
        importer._plan_label(
            con, {"seg": "s:not-an-id", "skip": True, "why": "검수 제외"}
        )
    assert exc_info.value.code == "BAD_HANDLE"
    result = importer.label(con, human, [segment_label, segment_label])

    assert result == f"ERR s:{segment_id} BAD_ARGS duplicate segment label"
    assert parsed.count(f"d?:{artifact_id}") == 1
    assert parsed.count(f"s:{segment_id}") == 4
    assert parsed.count("s:not-an-id") == 1


def test_fix_l11_init_name_is_normalized_and_secret_rejected(
    tmp_path: Path, run_cli
) -> None:
    """init 이름을 정규화하고 민감 정보가 있으면 DB를 만들지 않는다."""
    normalized_dir = tmp_path / "normalized"
    code, _out, err = run_cli(
        [
            "init",
            "--project",
            "t",
            "--name",
            "Cafe\u0301\r\nproject",
            "--owner",
            "u-tester",
            "--owner-name",
            "Tester",
            "--dir",
            str(normalized_dir),
        ]
    )
    assert code == 0, err
    con = db.connect(normalized_dir / ".docdb" / "docdb.sqlite")
    try:
        assert con.execute("SELECT name FROM project").fetchone()[0] == "Café\nproject"
    finally:
        con.close()

    secret_dir = tmp_path / "secret"
    secret = "AKIA" + "A" * 16
    code, out, err = run_cli(
        [
            "init",
            "--project",
            "t",
            "--name",
            "Project " + secret,
            "--owner",
            "u-tester",
            "--owner-name",
            "Tester",
            "--dir",
            str(secret_dir),
        ]
    )

    assert code == 1
    assert out == ""
    assert err.startswith("ERROR SECRET_DETECTED ")
    assert secret not in err
    assert not (secret_dir / ".docdb" / "docdb.sqlite").exists()


def test_fix_l11_allow_secret_reason_is_normalized_checked_and_not_saved(
    project_db: Path, run_cli
) -> None:
    """예외 사유는 저장 전에 정규화·검사한다."""
    secret = "ghp_" + "a" * 36
    args = [
        "--db",
        str(project_db),
        "--actor",
        "u-tester",
        "policy",
        "allow-secret",
        "f:DOC-X/D01/behavior",
        "--reason",
        "Review " + secret,
        "--days",
        "7",
    ]
    code, out, err = run_cli(args, interactive=True)
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR SECRET_DETECTED ")
    assert secret not in err

    con = db.connect(project_db)
    try:
        assert con.execute("SELECT count(*) FROM policy_exception").fetchone()[0] == 0
    finally:
        con.close()

    args[args.index("Review " + secret)] = "Cafe\u0301\r\nreview"
    code, out, err = run_cli(args, interactive=True)
    assert code == 0, err
    exception_id = out.strip().removeprefix("OK ")
    con = db.connect(project_db)
    try:
        assert con.execute(
            "SELECT reason FROM policy_exception WHERE exception_id=?", (exception_id,)
        ).fetchone()[0] == "Café\nreview"
    finally:
        con.close()


def test_fix_l11_catalogue_checks_nested_string_values_before_writes(
    project_db: Path,
    starter_paths: tuple[Path, Path],
    human: Actor,
    con,
    tmp_path: Path,
) -> None:
    """카탈로그의 중첩 문자열 값에서 비밀을 찾아 적재를 거부한다."""
    catalogue_path, item_types_path = starter_paths
    data = json.loads(catalogue_path.read_text(encoding="utf-8"))
    field = next(
        field
        for kind in data["document_types"]
        for field in kind["required_fields"].values()
        if isinstance(field, dict) and "extract_rel" in field
    )
    secret = "AKIA" + "A" * 16
    field["extract_rel"]["from_prefix"] = "REQ-" + secret
    altered_path = tmp_path / "nested_secret.json"
    altered_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = tuple(
        con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("cat_version", "cat_group", "cat_fact", "cat_kind", "cat_kind_field")
    )

    with pytest.raises(DocdbError) as exc_info:
        catalogue.load(con, altered_path, item_types_path, human)

    assert exc_info.value.code == "SECRET_DETECTED"
    assert secret not in exc_info.value.detail
    after = tuple(
        con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("cat_version", "cat_group", "cat_fact", "cat_kind", "cat_kind_field")
    )
    assert after == before
