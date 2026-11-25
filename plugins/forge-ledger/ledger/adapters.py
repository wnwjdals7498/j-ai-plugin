"""PM 도구와 포지를 서비스 종류와 무관하게 읽는 연결 규격."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


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


def parse_run_path(path: str) -> tuple[str, str]:
    """`<워크플로 파일>@<ref>`를 첫 `@`에서 나눈다. `@`이 없으면 ref는 빈 문자열이다."""
    workflow, _, ref = path.partition("@")
    return workflow, ref
