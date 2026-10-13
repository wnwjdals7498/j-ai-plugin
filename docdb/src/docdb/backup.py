"""SQLite 온라인 백업과 복구 기능(C-09)."""
import re
from datetime import datetime, timezone
from pathlib import Path

from docdb import clock, db, paths
from docdb.db import Connection
from docdb.errors import DocdbError

_REASON = re.compile(r"^[a-z0-9-]+$", re.ASCII)


def backup(con: Connection, project_id: str, reason: str) -> Path:
    """연결된 DB를 프로젝트별 백업 폴더에 온라인 백업한다."""
    if not _REASON.fullmatch(reason):
        raise DocdbError("BAD_ARGS", "백업 사유는 영문 소문자, 숫자, 하이픈만 사용해야 함")

    stamp = datetime.fromisoformat(clock.now_iso()).astimezone(timezone.utc)
    stem = f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{reason}"
    directory = paths.backups_dir(project_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        index = 1
        while True:
            suffix = "" if index == 1 else f"-{index}"
            target = directory / f"{stem}{suffix}.sqlite"
            try:
                with target.open("xb"):
                    pass
                break
            except FileExistsError:
                index += 1

        dest_con = db.connect(target, create=True)
        try:
            con.backup(dest_con)
            dest_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dest_con.close()
    except (db.DatabaseError, OSError) as exc:
        if "target" in locals():
            target.unlink(missing_ok=True)
            Path(str(target) + "-wal").unlink(missing_ok=True)
            Path(str(target) + "-shm").unlink(missing_ok=True)
        raise DocdbError("DB", str(exc)[:80]) from exc
    return target


def restore(db_path: Path, backup_file: Path) -> None:
    """백업 파일의 내용을 온라인 백업 API로 기존 DB에 복원한다."""
    target = Path(db_path)
    source_path = Path(backup_file)
    if target.resolve() == source_path.resolve():
        raise DocdbError("BAD_ARGS", "복원 파일과 DB 경로가 같음")
    if not source_path.is_file():
        raise DocdbError("NO_DB", f"백업 파일이 없음: {source_path}")

    source = None
    destination = None
    try:
        source = db.connect(source_path)
        destination = db.connect(target, create=True)
        source.backup(destination)
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except (db.DatabaseError, OSError) as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
