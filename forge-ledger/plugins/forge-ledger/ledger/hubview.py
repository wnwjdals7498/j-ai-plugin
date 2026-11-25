"""S1 수치 계산과 글 출력. 화면·API·`status`·SessionStart가 함께 쓰며 HubStore만 읽는다(설계 6.4)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from ledger.hubstore import HubStore, RepoRow, RunRow

CI_WINDOW = 50
PENDING_1ST = ("missing_docs", "penpot_changes", "open_alerts", "unclassified")
STATE_LABELS = {
    "ok": "정상",
    "stale": "수집 지연",
    "failed": "연결 실패",
    "partial": "일부 실패",
    "skipped": "토큰 없음",
    "never": "수집 전",
}

_PATH_MISSING_LABEL = "경로 없음"  # `skipped`의 다른 이유(error_kind=path_missing)
_STALE_GRACE_SECONDS = 300
_SOURCES = ("gitea", "plane")
_SOURCE_NAMES = {"gitea": "Gitea", "plane": "Plane"}
_PENDING_NAMES = {
    "missing_docs": "빠진 문서",
    "penpot_changes": "Penpot 변경",
    "open_alerts": "열린 알림",
    "unclassified": "미분류",
}
_EMPTY_TEXT = "등록된 저장소 없음 — ledger sync로 등록하세요"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def count_ci_failures(runs: Sequence[dict[str, Any]], open_pulls: set[int], default_branch: str) -> int:
    """(대상, 워크플로)마다 최신 완료 실행이 실패면 1로 센다. 대상은 열린 PR과 기본 브랜치 push다."""
    latest: dict[tuple[Optional[int], str], str] = {}  # (PR 번호, 기본 브랜치는 None; 워크플로) → conclusion
    for run in sorted(runs, key=lambda item: item["run_number"], reverse=True)[:CI_WINDOW]:
        if run["status"] != "completed":
            continue
        targets: list[Optional[int]] = [number for number in run["pull_numbers"] if number in open_pulls]
        if run["event"] == "push" and run["ref"] == f"refs/heads/{default_branch}":
            targets.append(None)
        for target in targets:
            latest.setdefault((target, run["workflow"]), run["conclusion"])
    return sum(1 for conclusion in latest.values() if conclusion == "failure")


def source_state(last: Optional[RunRow], last_good: Optional[RunRow], now: datetime, interval_s: int) -> str:
    """수집 상태. `last_good`은 결과가 ok·partial인 마지막 실행이다."""
    if last is not None and not last.result:
        last = last_good  # 끝나지 않은 실행은 결과가 없으므로 직전 성공으로 판단한다
    if last is None:
        return "never"
    if last.result in ("failed", "skipped"):
        return last.result
    if last_good is not None and last_good.finished_at is not None:
        if now - _moment(last_good.finished_at) > timedelta(seconds=interval_s + _STALE_GRACE_SECONDS):
            return "stale"
    return "partial" if last.result == "partial" else "ok"


def _source_status(store: HubStore, repo_id: int, source: str, now: datetime, interval_s: int) -> dict[str, Any]:
    last = store.last_run(repo_id, source)
    last_good = store.last_good_run(repo_id, source)
    state = source_state(last, last_good, now, interval_s)
    label = STATE_LABELS[state]
    if state == "skipped" and last is not None and last.error_kind == "path_missing":
        label = _PATH_MISSING_LABEL
    return {
        "state": state,
        "label": label,
        "last_ok_at": None if last_good is None else last_good.finished_at,
        "last_result": None if last is None else last.result,
        "api_calls": 0 if last is None else last.api_calls,
    }


def _projects(store: HubStore, repo_id: int, today: date) -> list[dict[str, Any]]:
    cycles: dict[Optional[str], dict[str, Any]] = {}
    for view in store.entities(repo_id, "cycle"):
        end_date = view.attrs["end_date"]
        cycles[view.parent_ext_key] = {
            "name": view.attrs["name"],
            "start_date": view.attrs["start_date"],
            "end_date": end_date,
            "completed": view.attrs["completed_issues"],
            "total": view.attrs["total_issues"],
            "gate_days": (date.fromisoformat(end_date[:10]) - today).days if end_date else None,
        }
    return [
        {
            "key": view.attrs["key"],
            "title": view.attrs["title"],
            "default": view.attrs["default"],
            "found": view.attrs["found"],
            "cycle": cycles.get(view.ext_key),
        }
        for view in store.entities(repo_id, "plane_project")
    ]


def repo_summary(store: HubStore, repo: RepoRow, now: datetime, interval_s: int = 600) -> dict[str, Any]:
    pulls = store.entities(repo.id, "pull")
    waiting = [view.attrs for view in pulls if not view.attrs["draft"]]
    ages = [(now - _moment(attrs["created_at"])).days for attrs in waiting if attrs["created_at"]]
    runs = [view.attrs for view in store.entities(repo.id, "ci_run")]
    sources: dict[str, dict[str, Any]] = {}
    for source in _SOURCES:
        status = _source_status(store, repo.id, source, now, interval_s)
        sources[source] = {key: status[key] for key in ("state", "label", "last_ok_at")}
    return {
        "repo": f"{repo.owner}/{repo.name}",
        "html_url": repo.html_url,
        "projects": _projects(store, repo.id, now.astimezone(timezone.utc).date()),
        "open_issues": len(store.entities(repo.id, "issue")),
        "review_prs": {"count": len(waiting), "oldest_days": max(ages, default=None)},
        "ci_failures": count_ci_failures(runs, {int(view.ext_key) for view in pulls}, repo.default_branch),
        "ci_window": CI_WINDOW,
        "sources": sources,
        "last_sync_at": max((item["last_ok_at"] for item in sources.values() if item["last_ok_at"]), default=None),
        "pending": list(PENDING_1ST),
    }


def summary(store: HubStore, now: datetime, interval_s: int = 600) -> dict[str, Any]:
    return {
        "generated_at": _stamp(now),
        "interval_s": interval_s,
        "repos": [repo_summary(store, repo, now, interval_s) for repo in store.repos()],
    }


def health(store: HubStore, now: datetime, interval_s: int = 600) -> dict[str, Any]:
    return {
        "generated_at": _stamp(now),
        "sources": [
            {
                "repo": f"{repo.owner}/{repo.name}",
                "source": source,
                **_source_status(store, repo.id, source, now, interval_s),
            }
            for repo in store.repos()
            for source in _SOURCES
        ],
    }


def _project_line(project: dict[str, Any]) -> str:
    head = f"  {project['key']} {project['title']}".rstrip()
    if not project["found"]:
        return f"{head}: 프로젝트 없음"
    cycle = project["cycle"]
    if cycle is None:
        return f"{head}: cycle 없음, 게이트 1 예정: -"
    gate = "-" if cycle["gate_days"] is None else f"{cycle['gate_days']}일"
    return f"{head}: {cycle['name']} {cycle['completed']}/{cycle['total']}, 게이트 1 예정: {gate}"


def _source_line(source: str, status: dict[str, Any]) -> str:
    line = f"  {_SOURCE_NAMES[source]}: {status['label']}"
    if status["last_ok_at"] is None:
        return line
    return f"{line} (마지막 성공 {status['last_ok_at']})"


def _repo_lines(repo_sum: dict[str, Any]) -> list[str]:
    """저장소 하나의 본문. 잘려도 수치가 남도록 길이가 늘어나는 프로젝트 줄을 뒤에 둔다."""
    review = repo_sum["review_prs"]
    oldest = "" if review["oldest_days"] is None else f" (가장 오래된 {review['oldest_days']}일)"
    return [
        f"  열린 결함 (Gitea): {repo_sum['open_issues']}",
        f"  리뷰 대기 PR: {review['count']}{oldest}",
        f"  CI 실패: {repo_sum['ci_failures']} (최근 실행 {repo_sum['ci_window']}건 기준)",
        *(_source_line(source, status) for source, status in repo_sum["sources"].items()),
        *(_project_line(project) for project in repo_sum["projects"]),
        f"  1차에서 제공: {', '.join(_PENDING_NAMES[key] for key in repo_sum['pending'])}",
    ]


def render_text(summary: dict[str, Any]) -> str:
    if not summary["repos"]:
        return _EMPTY_TEXT
    return "\n\n".join("\n".join([repo["repo"], *_repo_lines(repo)]) for repo in summary["repos"])


def render_session(repo_sum: dict[str, Any], now: datetime, limit: int = 1500) -> str:
    synced = repo_sum["last_sync_at"]
    if synced is None:
        age = "수집 전"
    else:
        minutes = max(0, int((now - _moment(synced)).total_seconds() // 60))
        age = f"마지막 수집 {minutes}분 전"
    text = "\n".join([f"forge-ledger 허브: {repo_sum['repo']} ({age})", *_repo_lines(repo_sum)])
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
