"""PM 도구와 포지를 서비스 종류와 무관하게 다루는 연결 규격."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol

from ledger.worklog import Entry, RemoteEntry

_FRACTION_RE = re.compile(r"(?<=:\d{2})\.\d+")


class WorklogStore(Protocol):
    def ensure_project(self) -> str: ...
    def ensure_month_item(self, month: str) -> str: ...
    def post_entry(self, entry: Entry) -> str: ...
    def list_entries(self, from_: date, to: date, user: Optional[str] = None) -> list[RemoteEntry]: ...
    def current_user(self) -> str: ...


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    default_branch: str
    html_url: str


@dataclass(frozen=True)
class IssueInfo:
    number: int
    title: str
    labels: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class PullInfo:
    number: int
    title: str
    draft: bool
    created_at: str


@dataclass(frozen=True)
class RunInfo:
    run_id: int
    run_number: int
    event: str
    status: str
    conclusion: str
    workflow: str
    ref: str
    head_sha: str
    pull_numbers: tuple[int, ...]
    started_at: str


@dataclass(frozen=True)
class ProjectInfo:
    id: str
    identifier: str
    name: str


@dataclass(frozen=True)
class CycleInfo:
    id: str
    name: str
    start_date: str
    end_date: str
    total_issues: int
    completed_issues: int


class ForgeAdapter(Protocol):
    """코드 저장소 서비스 읽기 규격."""

    calls: int

    def whoami(self) -> str: ...

    def get_repo(self, owner: str, name: str) -> RepoInfo: ...

    def list_open_issues(self, owner: str, name: str) -> list[IssueInfo]: ...

    def list_open_pulls(self, owner: str, name: str) -> list[PullInfo]: ...

    def list_runs(self, owner: str, name: str, limit: int = 50) -> list[RunInfo]: ...


class PmAdapter(Protocol):
    """작업 관리 도구 읽기 규격."""

    calls: int

    def whoami(self) -> str: ...

    def list_project_infos(self) -> list[ProjectInfo]: ...

    def current_cycle(self, project_id: str) -> Optional[CycleInfo]: ...


class WorkTracker(Protocol):
    """작업 항목을 읽고 쓰는 규격. 항목·상태·라벨·댓글은 도구가 준 딕셔너리 그대로다."""

    def whoami(self) -> str: ...

    def whoami_id(self) -> str:
        """담당자(`assignees`)와 댓글 작성자(`actor`)에 쓰는 사용자 id."""
        ...

    def resolve_code(self, code: str) -> tuple[str, dict[str, Any]]: ...

    def update_item(self, project_id: str, item_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def list_comments(self, project_id: str, item_id: str) -> list[dict[str, Any]]: ...

    def create_comment(self, project_id: str, item_id: str, html: str) -> dict[str, Any]: ...

    def list_states(self, project_id: str) -> list[dict[str, Any]]: ...

    def list_items(self, project_id: str) -> list[dict[str, Any]]: ...

    def list_projects(self) -> list[dict[str, Any]]: ...

    def list_labels(self, project_id: str) -> list[dict[str, Any]]: ...

    def create_label(self, project_id: str, name: str, color: str) -> dict[str, Any]: ...


def parse_run_path(path: str) -> tuple[str, str]:
    """`<워크플로 파일>@<ref>`를 첫 `@`에서 나눈다. `@`이 없으면 ref는 빈 문자열이다."""
    workflow, _, ref = path.partition("@")
    return workflow, ref


def to_utc_iso(value: str) -> str:
    """오프셋이 있는 ISO 8601 시각을 UTC `YYYY-MM-DDTHH:MM:SSZ`로 바꾼다.

    빈 문자열은 그대로 돌려준다. 해석할 수 없거나 오프셋이 없으면 `ValueError`다.
    """
    if not value:
        return ""
    # 소수 초는 버린다. Python 3.9의 fromisoformat은 `Z`와 3·6자리가 아닌 소수 초를 읽지 못한다.
    text = _FRACTION_RE.sub("", value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"오프셋 없는 시각: {value}")
    try:
        utc = parsed.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError(f"범위를 벗어난 시각: {value}") from None
    return utc.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
