from pathlib import Path
from typing import Iterator

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
