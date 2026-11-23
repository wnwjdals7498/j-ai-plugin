from __future__ import annotations

import datetime
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

KINDS = ("decision", "phase", "release", "override")
HEADER = "날짜 | 종류 | ID | Plane 프로젝트 | 제목 | 상세@수정 시각"
_HEADER_PARTS = [part.strip() for part in HEADER.split(" | ")]


@dataclass(frozen=True)
class HistoryEntry:
    date: str
    kind: str
    ref_id: str
    projects: str
    title: str
    detail: str


def empty_history() -> str:
    """Return the initial content for an empty history.md file."""
    return f"# 이력\n\n{HEADER}\n"


def format_entry(e: HistoryEntry) -> str:
    """Validate and format a HistoryEntry as a single pipe-delimited line."""
    if e.kind not in KINDS:
        raise ValueError(f"Unknown history kind: {e.kind!r}")
    fields = (e.date, e.kind, e.ref_id, e.projects, e.title, e.detail)
    for value in fields:
        if "|" in value or "\n" in value or "\r" in value:
            raise ValueError(f"Invalid character in history field: {value!r}")
    return " | ".join(fields)


def parse_history(text: str) -> list[HistoryEntry]:
    """Parse history.md text into a list of HistoryEntry objects."""
    entries: list[HistoryEntry] = []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split(" | ")]
        if len(parts) != 6 or parts == _HEADER_PARTS:
            continue
        entries.append(HistoryEntry(*parts))
    return entries


def append_entry(path: Path, e: HistoryEntry) -> None:
    """Append a formatted HistoryEntry to path, creating it with a header if missing."""
    line = format_entry(e)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{empty_history()}{line}\n", encoding="utf-8")
        return
    existing = path.read_text(encoding="utf-8")
    if not existing:
        path.write_text(f"{empty_history()}{line}\n", encoding="utf-8")
        return
    prefix = "" if existing.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{line}\n")


def _default_rand4() -> str:
    return secrets.token_hex(2)


def new_decision_id(
    today: datetime.date,
    existing: set[str],
    rand4: Callable[[], str] = _default_rand4,
) -> str:
    """Generate a unique D-YYMMDD-XXXX decision ID not present in existing."""
    date_part = today.strftime("%y%m%d")
    while True:
        candidate = f"D-{date_part}-{rand4()}"
        if candidate not in existing:
            return candidate
