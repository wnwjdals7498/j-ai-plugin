"""SQLite 스키마 마이그레이션 실행기."""
import re
from pathlib import Path

from docdb.db import Connection
from docdb.errors import DocdbError

_SCHEMA_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


def schema_files() -> list[tuple[int, Path]]:
    """연속된 번호의 스키마 파일을 번호순으로 반환한다."""
    files: list[tuple[int, Path]] = []
    for path in _SCHEMA_DIR.iterdir():
        match = _SCHEMA_NAME.fullmatch(path.name)
        if path.is_file() and match is not None:
            files.append((int(match.group(1)), path))
    files.sort(key=lambda entry: entry[0])
    versions = [version for version, _path in files]
    if versions != list(range(1, len(versions) + 1)):
        raise DocdbError("DB", "migration numbers must start at 1 without gaps")
    return files


def latest_version() -> int:
    """현재 설치된 최신 스키마 번호를 반환한다."""
    files = schema_files()
    return files[-1][0] if files else 0


def current_version(con: Connection) -> int:
    """DB의 PRAGMA user_version 값을 반환한다."""
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def ensure_current(con: Connection) -> None:
    """DB가 현재 스키마 버전과 일치하는지 확인한다."""
    current = current_version(con)
    latest = latest_version()
    if current < latest:
        raise DocdbError("MIGRATION_REQUIRED", f"{current}->{latest}")
    if current > latest:
        raise DocdbError("DB_NEWER")


def apply_pending(con: Connection) -> list[int]:
    """아직 적용하지 않은 마이그레이션을 순서대로 원자 적용한다."""
    files = schema_files()
    current = current_version(con)
    latest = files[-1][0] if files else 0
    if current > latest:
        raise DocdbError("DB_NEWER")

    applied: list[int] = []
    for version, path in files:
        if version <= current:
            continue
        try:
            sql = path.read_text(encoding="utf-8")
            con.executescript(
                "BEGIN IMMEDIATE;\n"
                + sql
                + f"\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except Exception as exc:
            if con.in_transaction:
                con.execute("ROLLBACK")
            detail = str(exc)[:80]
            raise DocdbError("DB", f"migration {version:04d}: {detail}") from exc
        applied.append(version)
    return applied
