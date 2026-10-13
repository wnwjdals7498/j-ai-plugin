"""DB 연결, 기능 검사, 트랜잭션 규칙(C-08). pysqlite3는 이 모듈에서만 import한다."""
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ContextManager, Iterator

from pysqlite3 import dbapi2

from docdb.errors import DocdbError
from docdb import paths

DatabaseError = dbapi2.DatabaseError
IntegrityError = dbapi2.IntegrityError
Connection = Any

_MIN_SQLITE = (3, 34, 0)

# 연결별 write_tx 중첩 깊이(id(con) -> 깊이). 깊이 0이 되면 항목을 지운다.
_DEPTH: dict[int, int] = {}


def connect(path: Path, *, create: bool = False) -> Connection:
    """SQLite 연결을 연다. 자동 커밋 모드이며 트랜잭션은 write_tx로만 연다."""
    if not create and not Path(path).is_file():
        raise DocdbError("NO_DB", f"DB 파일이 없음: {path}")
    con = dbapi2.connect(str(path), isolation_level=None, timeout=5.0)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        sys.stderr.write(f"WARN journal_mode={mode}\n")
    return con


def sqlite_version(con: Connection) -> tuple[int, int, int]:
    """연결이 쓰는 SQLite 버전을 (major, minor, patch)로 반환한다."""
    text = con.execute("SELECT sqlite_version()").fetchone()[0]
    parts = [int(p) for p in str(text).split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def check_capabilities(con: Connection) -> None:
    """SQLite 버전, JSON 함수, FTS5 trigram 지원을 확인한다."""
    version = sqlite_version(con)
    if version < _MIN_SQLITE:
        raise DocdbError("SQLITE_CAPABILITY", "SQLite 3.34 이상 필요: " + ".".join(map(str, version)))
    try:
        ok = con.execute("SELECT json_valid('{}')").fetchone()[0]
    except DatabaseError as exc:
        raise DocdbError("SQLITE_CAPABILITY", "JSON 함수 없음") from exc
    if ok != 1:
        raise DocdbError("SQLITE_CAPABILITY", "JSON 함수 없음")
    try:
        con.execute("CREATE VIRTUAL TABLE temp._cap USING fts5(x, tokenize='trigram')")
    except DatabaseError as exc:
        raise DocdbError("SQLITE_CAPABILITY", "FTS5 trigram 없음") from exc
    con.execute("DROP TABLE temp._cap")


@contextmanager
def _write_tx(con: Connection) -> Iterator[None]:
    """write_tx 본체: 바깥은 BEGIN IMMEDIATE, 안쪽은 SAVEPOINT."""
    key = id(con)
    depth = _DEPTH.get(key, 0)
    nested = con.in_transaction
    if nested:
        name = f"sp_{depth + 1}"
        con.execute(f"SAVEPOINT {name}")
    else:
        con.execute("BEGIN IMMEDIATE")
    _DEPTH[key] = depth + 1
    try:
        yield
    except BaseException:
        if nested:
            con.execute(f"ROLLBACK TO {name}")
            con.execute(f"RELEASE {name}")
        else:
            con.execute("ROLLBACK")
        raise
    else:
        if nested:
            con.execute(f"RELEASE {name}")
        else:
            con.execute("COMMIT")
    finally:
        if depth:
            _DEPTH[key] = depth
        else:
            _DEPTH.pop(key, None)


def write_tx(con: Connection) -> ContextManager[None]:
    """쓰기 트랜잭션 컨텍스트. 이미 트랜잭션 안이면 SAVEPOINT를 쓴다."""
    return _write_tx(con)


def open_project_db(start: Path | None = None, db_path: Path | None = None) -> Connection:
    """프로젝트 DB를 열고 SQLite 기능과 스키마 버전을 확인한다."""
    selected_path = db_path if db_path is not None else paths.find_db(start)
    con = connect(Path(selected_path))
    try:
        check_capabilities(con)
        from docdb import migrate

        migrate.ensure_current(con)
    except BaseException:
        con.close()
        raise
    return con
