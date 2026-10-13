"""데이터 폴더, venv, DB 탐색 경로 모듈."""
import os
import sys
from pathlib import Path

from docdb.errors import DocdbError

DOT_DIR = ".docdb"
DB_FILE = "docdb.sqlite"


def data_dir() -> Path:
    """플러그인 데이터 디렉토리 경로를 반환한다."""
    docdb_home = os.environ.get("DOCDB_HOME")
    if docdb_home:
        return Path(docdb_home)

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "docdb"
        return Path.home() / "AppData" / "Local" / "docdb"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "docdb"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "docdb"
    return Path.home() / ".local" / "share" / "docdb"


def venv_dir() -> Path:
    """데이터 디렉토리 하위의 venv 경로를 반환한다."""
    return data_dir() / "venv"


def venv_python() -> Path:
    """플랫폼별 venv 내부 Python 실행 파일 경로를 반환한다."""
    v_dir = venv_dir()
    if sys.platform == "win32":
        return v_dir / "Scripts" / "python.exe"
    return v_dir / "bin" / "python"


def stamp_path() -> Path:
    """venv 상태 스탬프 파일 경로를 반환한다."""
    return venv_dir() / "docdb-stamp.json"


def backups_dir(project_id: str) -> Path:
    """프로젝트별 백업 저장 디렉토리 경로를 반환한다."""
    return data_dir() / "backups" / project_id


def find_db(start: Path | None = None) -> Path:
    """주어진 위치 또는 환경 변수로부터 상위 디렉토리를 탐색하여 docdb.sqlite 파일 경로를 찾는다."""
    docdb_path = os.environ.get("DOCDB_PATH")
    if docdb_path:
        p = Path(docdb_path)
        if not p.is_file():
            raise DocdbError("NO_DB", f"지정된 DB 파일이 존재하지 않음: {docdb_path}")
        return p

    start_dir: Path
    if start is not None:
        start_dir = start
    else:
        env_start = os.environ.get("DOCDB_START_DIR")
        if env_start:
            start_dir = Path(env_start)
        else:
            start_dir = Path.cwd()

    curr = start_dir.resolve()
    for parent in [curr, *curr.parents]:
        candidate = parent / DOT_DIR / DB_FILE
        if candidate.is_file():
            return candidate

    raise DocdbError("NO_DB", "docctl init 실행 필요")


def project_root(db_path: Path) -> Path:
    """.docdb 디렉토리의 상위 폴더인 프로젝트 루트 경로를 반환한다."""
    return db_path.parent.parent
