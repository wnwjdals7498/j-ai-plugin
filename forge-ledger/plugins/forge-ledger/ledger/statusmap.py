"""원천 상태를 PM 도구의 상태 그룹과 라벨로 바꾼다. 이전(`migrate apply`)과 작업 명령이 함께 쓴다."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional

# 상태 → (상태 그룹, 함께 다는 라벨). 앞의 여섯은 Work·Item, `Done`·`Active`·`폐기`는 백로그에도 쓴다.
STATUS_MAP: dict[str, tuple[str, Optional[str]]] = {
    "Planned": ("unstarted", None),
    "In Progress": ("started", None),
    "Done": ("completed", None),
    "Paused": ("unstarted", "paused"),
    "Failed": ("cancelled", "failed"),
    "Skipped": ("cancelled", "skipped"),
    "Active": ("backlog", None),
    "폐기": ("cancelled", None),
}

# 없는 라벨을 만들 때 쓰는 색
LABEL_COLOR = "#6b7280"


def pick_state(states: Sequence[Mapping[str, Any]], group: str) -> str:
    """`group`에 속한 첫 상태의 id. 그 그룹의 상태가 없으면 `ValueError`다."""
    for state in states:
        if state.get("group") == group:
            return str(state["id"])
    raise ValueError(f"Plane에 상태 그룹이 없음: {group}")
