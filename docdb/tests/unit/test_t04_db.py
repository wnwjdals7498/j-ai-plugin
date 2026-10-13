"""T04 수용 테스트: DB 연결, 기능 검사, 트랜잭션 규칙."""
import threading
import time
from pathlib import Path

import pytest

import docdb.db as db
from docdb.errors import DocdbError


def _connect(path: Path) -> db.Connection:
    """테스트 DB를 생성하고 연결을 반환한다."""
    return db.connect(path, create=True)


def test_t04_1_connect_missing_db(tmp_path: Path) -> None:
    """없는 DB 경로를 열면 NO_DB 오류가 발생한다."""
    with pytest.raises(DocdbError) as exc_info:
        db.connect(tmp_path / "missing.sqlite")

    assert exc_info.value.code == "NO_DB"


def test_t04_2_connect_sets_pragmas(tmp_path: Path) -> None:
    """새 DB 연결에 외래 키, 잠금 대기 시간, WAL 모드를 설정한다."""
    con = _connect(tmp_path / "pragmas.sqlite")
    try:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        con.close()


def test_t04_3_connect_starts_without_transaction(tmp_path: Path) -> None:
    """연결 직후에는 트랜잭션이 열려 있지 않다."""
    con = _connect(tmp_path / "autocommit.sqlite")
    try:
        assert con.in_transaction is False
    finally:
        con.close()


def test_t04_4_check_capabilities(tmp_path: Path) -> None:
    """지원하는 SQLite, JSON 함수, FTS5 trigram 기능을 확인한다."""
    con = _connect(tmp_path / "capabilities.sqlite")
    try:
        db.check_capabilities(con)
    finally:
        con.close()


def test_t04_5_check_capabilities_rejects_old_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite 버전이 최소 요구 버전보다 낮으면 SQLITE_CAPABILITY를 반환한다."""
    con = _connect(tmp_path / "old.sqlite")
    monkeypatch.setattr(db, "sqlite_version", lambda _con: (3, 26, 0))
    try:
        with pytest.raises(DocdbError) as exc_info:
            db.check_capabilities(con)
        assert exc_info.value.code == "SQLITE_CAPABILITY"
    finally:
        con.close()


def test_t04_6_write_tx_commits_for_other_connection(tmp_path: Path) -> None:
    """write_tx가 정상 종료되면 다른 연결에서 기록한 행을 읽을 수 있다."""
    path = tmp_path / "commit.sqlite"
    con = _connect(path)
    other = db.connect(path)
    try:
        with db.write_tx(con):
            con.execute("CREATE TABLE sample (value TEXT)")
            con.execute("INSERT INTO sample VALUES (?)", ("saved",))
        assert other.execute("SELECT value FROM sample").fetchone()[0] == "saved"
    finally:
        other.close()
        con.close()


def test_t04_7_write_tx_rolls_back_and_reraises(tmp_path: Path) -> None:
    """write_tx 안의 예외는 행을 되돌리고 원래 예외를 다시 던진다."""
    con = _connect(tmp_path / "rollback.sqlite")
    try:
        with db.write_tx(con):
            con.execute("CREATE TABLE sample (value TEXT)")
        marker = RuntimeError("rollback marker")
        with pytest.raises(RuntimeError) as exc_info:
            with db.write_tx(con):
                con.execute("INSERT INTO sample VALUES (?)", ("discarded",))
                raise marker
        assert exc_info.value is marker
        assert con.execute("SELECT value FROM sample").fetchall() == []
    finally:
        con.close()


def test_t04_8_write_tx_nested_savepoint_rolls_back_only_inner(
    tmp_path: Path,
) -> None:
    """안쪽 savepoint 실패는 안쪽 행만 되돌리고 바깥 행은 보존한다."""
    con = _connect(tmp_path / "savepoint.sqlite")
    try:
        with db.write_tx(con):
            con.execute("CREATE TABLE sample (value TEXT)")
        with db.write_tx(con):
            con.execute("INSERT INTO sample VALUES (?)", ("outer",))
            with pytest.raises(RuntimeError, match="inner failure"):
                with db.write_tx(con):
                    con.execute("INSERT INTO sample VALUES (?)", ("inner",))
                    raise RuntimeError("inner failure")
        values = [row[0] for row in con.execute("SELECT value FROM sample")]
        assert values == ["outer"]
    finally:
        con.close()


def test_t04_9_write_tx_waits_for_concurrent_writer(tmp_path: Path) -> None:
    """두 번째 연결은 첫 쓰기 트랜잭션 종료를 기다린 뒤 성공한다."""
    path = tmp_path / "concurrent.sqlite"
    setup = _connect(path)
    with db.write_tx(setup):
        setup.execute("CREATE TABLE sample (value TEXT)")
    setup.close()

    first_holds_lock = threading.Event()
    errors: list[BaseException] = []
    waited: list[float] = []

    def hold_first_transaction() -> None:
        # SQLite 연결은 만든 스레드에서만 쓸 수 있으므로 스레드 안에서 연다.
        try:
            con = db.connect(path)
            try:
                with db.write_tx(con):
                    con.execute("INSERT INTO sample VALUES (?)", ("first",))
                    first_holds_lock.set()
                    time.sleep(1)
            finally:
                con.close()
        except BaseException as exc:
            errors.append(exc)
            first_holds_lock.set()

    def write_from_second_connection() -> None:
        try:
            assert first_holds_lock.wait(timeout=2)
            con = db.connect(path)
            try:
                start = time.monotonic()
                with db.write_tx(con):
                    waited.append(time.monotonic() - start)
                    con.execute("INSERT INTO sample VALUES (?)", ("second",))
            finally:
                con.close()
        except BaseException as exc:
            errors.append(exc)

    first_thread = threading.Thread(target=hold_first_transaction)
    second_thread = threading.Thread(target=write_from_second_connection)
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert waited and waited[0] >= 0.3
    check = db.connect(path)
    try:
        values = [row[0] for row in check.execute("SELECT value FROM sample ORDER BY value")]
    finally:
        check.close()
    assert values == ["first", "second"]


def test_t04_10_foreign_key_errors_use_public_aliases(tmp_path: Path) -> None:
    """외래 키 위반은 docdb.db.IntegrityError로 잡을 수 있다."""
    con = _connect(tmp_path / "foreign-key.sqlite")
    try:
        with db.write_tx(con):
            con.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            con.execute(
                "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
            )
            with pytest.raises(db.IntegrityError):
                con.execute("INSERT INTO child VALUES (?)", (99,))
    finally:
        con.close()
