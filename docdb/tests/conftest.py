from pathlib import Path

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
