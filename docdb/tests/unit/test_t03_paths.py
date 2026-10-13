"""데이터 폴더, venv, DB 탐색 경로 테스트."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from docdb.errors import DocdbError
from docdb.paths import (
    DB_FILE,
    DOT_DIR,
    backups_dir,
    data_dir,
    find_db,
    project_root,
    stamp_path,
    venv_dir,
    venv_python,
)


def test_t03_1_docdb_home_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOCDB_HOME이 설정되어 있으면 최우선으로 해당 경로를 반환한다."""
    monkeypatch.setenv("DOCDB_HOME", "/x")
    assert data_dir() == Path("/x")


def test_t03_2_linux_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """linux에서 XDG_DATA_HOME이 설정되어 있으면 해당 경로 하위 docdb를 반환한다."""
    monkeypatch.delenv("DOCDB_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    assert data_dir() == Path("/xdg/docdb")


def test_t03_3_linux_default_local_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """linux에서 XDG_DATA_HOME이 없으면 ~/.local/share/docdb를 반환한다."""
    monkeypatch.delenv("DOCDB_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: Path("/h"))
    assert data_dir() == Path("/h/.local/share/docdb")


def test_t03_4_darwin_app_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """darwin에서는 ~/Library/Application Support/docdb를 반환한다."""
    monkeypatch.delenv("DOCDB_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: Path("/h"))
    assert data_dir() == Path("/h/Library/Application Support/docdb")


def test_t03_5_win32_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    """win32에서는 LOCALAPPDATA 또는 ~/AppData/Local/docdb를 반환한다."""
    monkeypatch.delenv("DOCDB_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "/L")
    assert data_dir() == Path("/L/docdb")

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", lambda: Path("/h"))
    assert data_dir() == Path("/h/AppData/Local/docdb")


def test_t03_6_venv_python_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    """venv_python()이 win32에서는 Scripts/python.exe, linux에서는 bin/python을 반환한다."""
    monkeypatch.setenv("DOCDB_HOME", "/custom")

    monkeypatch.setattr(sys, "platform", "win32")
    assert venv_python() == Path("/custom/venv/Scripts/python.exe")

    monkeypatch.setattr(sys, "platform", "linux")
    assert venv_python() == Path("/custom/venv/bin/python")


def test_t03_7_docdb_path_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DOCDB_PATH가 지정된 경우 파일이 존재하면 그 경로를, 없으면 NO_DB 오류를 발생시킨다."""
    db_file = tmp_path / "custom.sqlite"
    db_file.touch()

    monkeypatch.setenv("DOCDB_PATH", str(db_file))
    assert find_db() == db_file

    missing_db = tmp_path / "missing.sqlite"
    monkeypatch.setenv("DOCDB_PATH", str(missing_db))
    with pytest.raises(DocdbError) as exc_info:
        find_db()
    assert exc_info.value.code == "NO_DB"


def test_t03_8_find_db_upwards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """상위 디렉토리를 탐색하여 .docdb/docdb.sqlite를 찾는다."""
    monkeypatch.delenv("DOCDB_PATH", raising=False)
    monkeypatch.delenv("DOCDB_START_DIR", raising=False)

    proj_dir = tmp_path / "a"
    deep_dir = proj_dir / "b" / "c"
    deep_dir.mkdir(parents=True)

    db_dir = proj_dir / DOT_DIR
    db_dir.mkdir(parents=True)
    target_db = db_dir / DB_FILE
    target_db.touch()

    assert find_db(start=deep_dir) == target_db.resolve()


def test_t03_9_find_db_start_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start가 None일 때 DOCDB_START_DIR 환경 변수를 시작 위치로 사용한다."""
    monkeypatch.delenv("DOCDB_PATH", raising=False)

    proj_dir = tmp_path / "a"
    deep_dir = proj_dir / "b"
    deep_dir.mkdir(parents=True)

    db_dir = proj_dir / DOT_DIR
    db_dir.mkdir(parents=True)
    target_db = db_dir / DB_FILE
    target_db.touch()

    monkeypatch.setenv("DOCDB_START_DIR", str(deep_dir))
    assert find_db(start=None) == target_db.resolve()


def test_t03_10_find_db_nowhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """어디에도 DB가 없으면 DocdbError(NO_DB)와 종료 코드 3을 반환한다."""
    monkeypatch.delenv("DOCDB_PATH", raising=False)
    monkeypatch.delenv("DOCDB_START_DIR", raising=False)

    empty_dir = tmp_path / "empty" / "sub"
    empty_dir.mkdir(parents=True)

    with pytest.raises(DocdbError) as exc_info:
        find_db(start=empty_dir)
    assert exc_info.value.code == "NO_DB"
    assert exc_info.value.exit_code == 3
    assert "docctl init 실행 필요" in exc_info.value.detail


def test_t03_11_project_root(tmp_path: Path) -> None:
    """project_root는 db_path의 부모의 부모(.docdb의 부모)를 반환한다."""
    proj_dir = tmp_path / "a"
    db_path = proj_dir / DOT_DIR / DB_FILE
    assert project_root(db_path) == proj_dir


def test_t03_12_import_boundaries() -> None:
    """docdb.paths import 시 pysqlite3 및 docdb.db가 로드되지 않는다."""
    code = (
        "import sys\n"
        "import docdb.paths\n"
        "assert 'pysqlite3' not in sys.modules\n"
        "assert 'docdb.db' not in sys.modules\n"
        "print('import-ok')\n"
    )
    env = os.environ.copy()
    src_dir = str(Path(__file__).resolve().parent.parent.parent / "src")
    env["PYTHONPATH"] = src_dir
    res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert "import-ok" in res.stdout
