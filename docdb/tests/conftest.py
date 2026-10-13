from pathlib import Path
from typing import Callable, Iterator

import pytest


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """시각 함수를 고정해 테스트 결과를 일정하게 한다."""
    import docdb.clock

    monkeypatch.setattr(docdb.clock, "now_iso", lambda: "2026-10-13T00:00:00+00:00")
    monkeypatch.setattr(docdb.clock, "utc_year", lambda: 2026)


@pytest.fixture
def plugin_root() -> Path:
    """플러그인 루트 경로를 돌려준다."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def schema_con(tmp_path: Path) -> Iterator[object]:
    """임시 DB에 현재 스키마를 적용한 연결을 제공한다."""
    import docdb.db as db
    import docdb.migrate as migrate

    con = db.connect(tmp_path / "s.sqlite", create=True)
    try:
        migrate.apply_pending(con)
        yield con
    finally:
        con.close()


@pytest.fixture
def starter_paths(plugin_root: Path) -> tuple[Path, Path]:
    """시작 카탈로그와 항목 종류 파일 경로를 돌려준다."""
    return (
        plugin_root / "catalogue" / "starter.json",
        plugin_root / "catalogue" / "item_types.json",
    )


@pytest.fixture
def mini_paths(plugin_root: Path) -> tuple[Path, Path]:
    """미니 카탈로그 픽스처와 항목 종류 파일 경로를 돌려준다."""
    return (
        plugin_root / "tests" / "fixtures" / "mini_catalogue.json",
        plugin_root / "catalogue" / "item_types.json",
    )


@pytest.fixture
def project_db(tmp_path: Path, run_cli: Callable) -> Path:
    """표준 카탈로그를 적재한 임시 프로젝트 DB를 만든다."""
    code, _out, err = run_cli(
        [
            "init",
            "--project",
            "t",
            "--name",
            "테스트",
            "--owner",
            "u-tester",
            "--owner-name",
            "Tester",
            "--dir",
            str(tmp_path),
        ]
    )
    assert code == 0, err
    return tmp_path / ".docdb" / "docdb.sqlite"


@pytest.fixture
def mini_db(tmp_path: Path, run_cli: Callable, mini_paths: tuple[Path, Path]) -> Path:
    """미니 카탈로그를 적재한 임시 프로젝트 DB를 만든다."""
    code, _out, err = run_cli(
        [
            "init",
            "--project",
            "t",
            "--name",
            "테스트",
            "--owner",
            "u-tester",
            "--owner-name",
            "Tester",
            "--catalogue",
            str(mini_paths[0]),
            "--dir",
            str(tmp_path),
        ]
    )
    assert code == 0, err
    return tmp_path / ".docdb" / "docdb.sqlite"


@pytest.fixture
def con(project_db: Path) -> Iterator[object]:
    """임시 프로젝트 DB 연결을 제공하고 끝에 닫는다."""
    import docdb.db as db

    connection = db.connect(project_db)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def human():
    """사람 행위자를 돌려준다."""
    from docdb.actors import Actor

    return Actor("u-tester", "human")


@pytest.fixture
def agent():
    """AI 행위자를 돌려준다."""
    from docdb.actors import Actor

    return Actor("ai-claude", "ai")


@pytest.fixture
def ci():
    """팀 행위자를 돌려준다."""
    from docdb.actors import Actor

    return Actor("team-ci", "team")


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> Callable:
    """CLI를 실행하고 종료 코드, stdout, stderr를 반환한다."""
    def run(
        args: list[str],
        interactive: bool = False,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        from docdb.cli import main

        for name in (
            "DOCDB_HOME",
            "DOCDB_PATH",
            "DOCDB_START_DIR",
            "DOCDB_ACTOR",
            "DOCDB_WORK",
            "DOCDB_SESSION",
            "DOCDB_MODEL",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        code = main(args, interactive=interactive)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run
