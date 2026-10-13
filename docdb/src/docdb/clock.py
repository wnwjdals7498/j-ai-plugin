"""시각 및 날짜 유틸리티 모듈."""
from datetime import datetime, timezone


def now_iso() -> str:
    """현재 UTC 시각을 ISO-8601 형식(초 단위)으로 반환한다."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_year() -> int:
    """현재 UTC 연도를 반환한다."""
    return datetime.now(timezone.utc).year
