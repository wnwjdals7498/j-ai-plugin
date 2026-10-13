"""T05 수용 테스트: 마이그레이션과 원본 스키마."""
import hashlib
import re
from pathlib import Path

import pytest

import docdb.db as db
import docdb.migrate as migrate
from docdb.errors import DocdbError


def test_t05_1_schema_files_are_contiguous() -> None:
    """마이그레이션 번호가 1부터 빈틈없이 이어진다."""
    versions = [version for version, _path in migrate.schema_files()]

    assert versions[:2] == [1, 2]
    assert versions == list(range(1, len(versions) + 1))


def test_t05_2_initial_schema_files_match_sources(plugin_root: Path) -> None:
    """첫 두 마이그레이션 파일은 원본 SQL과 바이트 단위로 같다."""
    source_root = plugin_root.parent / "docs-db" / "reference" / "docdb-package"
    expected = [
        (source_root / "docdb_schema.sql", plugin_root / "src/docdb/schema/0001_base.sql"),
        (
            source_root / "docdb_schema_token_ext.sql",
            plugin_root / "src/docdb/schema/0002_token_ext.sql",
        ),
    ]

    for source, migration_file in expected:
        assert hashlib.sha256(migration_file.read_bytes()).digest() == hashlib.sha256(
            source.read_bytes()
        ).digest()


def test_t05_3_apply_pending_creates_initial_schema(tmp_path: Path) -> None:
    """새 DB에 모든 마이그레이션을 적용하고 0001의 테이블을 만든다."""
    con = db.connect(tmp_path / "fresh.sqlite", create=True)
    try:
        applied = migrate.apply_pending(con)
        versions = [version for version, _path in migrate.schema_files()]
        sql = migrate.schema_files()[0][1].read_text(encoding="utf-8")
        table_names = set(
            re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)
        )
        created_names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        assert applied == versions
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrate.latest_version()
        assert len(table_names) == 36
        assert table_names <= created_names
    finally:
        con.close()


def test_t05_4_apply_pending_is_idempotent(schema_con: db.Connection) -> None:
    """이미 최신인 DB에는 마이그레이션을 적용하지 않는다."""
    assert migrate.apply_pending(schema_con) == []


def test_t05_5_ensure_current_reports_required_migration(
    schema_con: db.Connection,
) -> None:
    """오래된 DB는 MIGRATION_REQUIRED와 현재·최신 버전을 알린다."""
    schema_con.execute("PRAGMA user_version = 1")

    with pytest.raises(DocdbError) as exc_info:
        migrate.ensure_current(schema_con)

    assert exc_info.value.code == "MIGRATION_REQUIRED"
    assert exc_info.value.detail == f"1->{migrate.latest_version()}"


def test_t05_6_ensure_current_rejects_newer_database(
    schema_con: db.Connection,
) -> None:
    """현재 실행기보다 높은 DB 버전은 DB_NEWER로 거부한다."""
    schema_con.execute("PRAGMA user_version = 99")

    with pytest.raises(DocdbError) as exc_info:
        migrate.ensure_current(schema_con)

    assert exc_info.value.code == "DB_NEWER"


def test_t05_7_failed_migration_rolls_back_and_preserves_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQL 오류가 나면 해당 파일의 변경과 버전 증가를 모두 되돌린다."""
    bad_sql = tmp_path / "0001_bad.sql"
    bad_sql.write_text(
        "CREATE TABLE should_rollback (id INTEGER);\nCREATE TABLE broken (",
        encoding="utf-8",
    )
    monkeypatch.setattr(migrate, "schema_files", lambda: [(1, bad_sql)])
    con = db.connect(tmp_path / "failed.sqlite", create=True)
    try:
        with pytest.raises(DocdbError) as exc_info:
            migrate.apply_pending(con)

        assert exc_info.value.code == "DB"
        assert exc_info.value.detail.startswith("migration 0001:")
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("should_rollback",),
        ).fetchone() is None
    finally:
        con.close()


def test_t05_8_open_project_db_checks_schema_version(tmp_path: Path) -> None:
    """open_project_db는 최신 DB를 열고 오래된 DB는 MIGRATION_REQUIRED로 거부한다."""
    latest_path = tmp_path / "latest.sqlite"
    con = db.connect(latest_path, create=True)
    try:
        migrate.apply_pending(con)
    finally:
        con.close()

    opened = db.open_project_db(db_path=latest_path)
    try:
        assert migrate.current_version(opened) == migrate.latest_version()
    finally:
        opened.close()

    old_path = tmp_path / "old.sqlite"
    old = db.connect(old_path, create=True)
    old.close()
    with pytest.raises(DocdbError) as exc_info:
        db.open_project_db(db_path=old_path)

    assert exc_info.value.code == "MIGRATION_REQUIRED"
