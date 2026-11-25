"""허브 대조. 로컬 저장소의 Gitea·Plane 현재 상태를 HubStore에 반영한다.

원격 주소와 토큰에는 비밀값이 섞일 수 있어 store·log·예외 문구에 넘기지 않는다(예외는 종류만 남긴다).
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 # git 원격 주소 조회용(shell=False)
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol, Sequence, TextIO, TypeVar

from ledger.adapters import ForgeAdapter, PmAdapter
from ledger.config import LedgerConfig, load_config
from ledger.flows import Repo
from ledger.gitea import GiteaAuthError, GiteaUnavailable
from ledger.hubstore import LOCK_NAME, EntityRow, HubStore
from ledger.plane import PlaneAuthError, PlaneUnavailable

_GIT_TIMEOUT_SECONDS = 30
_SCP_RE = re.compile(r"^(?:[^/@:]+@)?[^/@:]+:(?P<path>[^:]*)$")  # git@host:owner/name
_LOG_LEVELS = {"ok": "info", "partial": "warning", "skipped": "warning", "failed": "error"}

_Now = Callable[[], datetime]
_Log = Callable[[dict[str, Any]], None]
_Apply = Callable[..., None]  # (kind, rows, close_missing=True)


class RepoResolveError(Exception):
    """로컬 경로에서 저장소 정보를 읽지 못했다."""


@dataclass(frozen=True)
class RepoRef:
    path: str
    owner: str
    name: str
    plane_workspace: str
    plane_keys: tuple[str, ...]


@dataclass(frozen=True)
class SyncOutcome:
    repo: str  # `owner/name`. 어느 저장소인지 모르면 로컬 경로
    source: str  # `gitea`·`plane`. `sync_run`을 남기지 않았으면 빈 문자열
    result: str  # ok, partial, failed, skipped
    api_calls: int
    error_kind: str


class _Counted(Protocol):
    calls: int


_AdapterT = TypeVar("_AdapterT", bound=_Counted)


def parse_remote(url: str) -> tuple[str, str]:
    """git 원격 주소에서 (owner, name)을 뽑는다."""
    path: Optional[str] = None
    if "://" in url:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme in ("http", "https", "ssh") and parsed.netloc:
            path = parsed.path
    else:
        match = _SCP_RE.match(url)
        if match is not None:
            path = match.group("path")
    if path is not None:
        trimmed = path.strip("/")
        if trimmed.endswith(".git"):
            trimmed = trimmed[: -len(".git")].rstrip("/")
        parts = trimmed.split("/")
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]
    raise RepoResolveError("원격 주소에서 owner/name을 읽지 못했다")


def _git(path: Path, *args: str) -> tuple[int, str]:
    """git 명령의 (종료 코드, 앞뒤 공백을 뺀 stdout)."""
    try:
        proc = subprocess.run(  # nosec B603 B607 # git 원격 조회 명령 실행(shell=False)
            ["git", *args],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        raise RepoResolveError("git 원격 조회 실패") from None
    return proc.returncode, proc.stdout.strip()


def _git_remote_url(path: Path) -> str:
    """`git config forge-ledger.remote`가 가리키는 원격(없으면 `origin`)의 주소.

    origin이 Gitea가 아닌 저장소에서 Gitea 원격을 고르는 설정이다.
    """
    code, configured = _git(path, "config", "--get", "forge-ledger.remote")
    remote = configured if code == 0 and configured else "origin"
    code, url = _git(path, "remote", "get-url", "--", remote)
    if code != 0:
        raise RepoResolveError("git 원격 주소를 읽지 못했다")
    return url


def _ledger_config(root: Path) -> Optional[LedgerConfig]:
    path = root / ".ledger" / "ledger.yml"
    try:
        return load_config(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise RepoResolveError("ledger.yml을 읽지 못했다") from None


def project_root(start: Path) -> Path:
    """허브가 저장소로 등록하는 폴더. 규칙은 `Repo.discover`를 따른다."""
    return Repo.discover(start).root


def resolve_repo(path: Path, git_remote: Callable[[Path], str] = _git_remote_url) -> RepoRef:
    owner, name = parse_remote(git_remote(path))
    config = _ledger_config(path)
    if config is None:
        return RepoRef(str(path), owner, name, "", ())
    return RepoRef(str(path), owner, name, config.workspace, tuple(project.key for project in config.projects))


def _stamp(now: _Now) -> str:
    return now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, (GiteaAuthError, PlaneAuthError)):
        return "auth"
    if isinstance(exc, (GiteaUnavailable, PlaneUnavailable)):
        return "unavailable"
    return "error"


def _apply(
    store: HubStore,
    source_id: int,
    repo_id: int,
    now: _Now,
    kind: str,
    rows: Sequence[EntityRow],
    close_missing: bool = True,
) -> None:
    """한 종류를 반영하고, 서버 주소가 바뀌기 전 소스에 남은 같은 종류의 열린 행을 닫는다."""
    stamp = _stamp(now)
    store.apply_kind(source_id, repo_id, kind, rows, stamp, close_missing)
    store.close_other_sources(source_id, repo_id, kind, stamp)


def _sync_gitea(store: HubStore, repo_id: int, ref: RepoRef, forge: ForgeAdapter, apply: _Apply) -> tuple[str, str]:
    """종류마다 따로 조회·반영한다. (result, error_kind)를 돌려준다."""

    def repo_rows() -> list[EntityRow]:
        info = forge.get_repo(ref.owner, ref.name)
        store.set_repo_meta(repo_id, info.default_branch, info.html_url)
        return [EntityRow("repo", f"{ref.owner}/{ref.name}", "open", asdict(info))]

    def issue_rows() -> list[EntityRow]:
        issues = forge.list_open_issues(ref.owner, ref.name)
        return [EntityRow("issue", str(issue.number), "open", asdict(issue)) for issue in issues]

    def pull_rows() -> list[EntityRow]:
        pulls = forge.list_open_pulls(ref.owner, ref.name)
        return [EntityRow("pull", str(pull.number), "open", asdict(pull)) for pull in pulls]

    def run_rows() -> list[EntityRow]:
        runs = forge.list_runs(ref.owner, ref.name)
        return [EntityRow("ci_run", str(run.run_id), "open", asdict(run)) for run in runs]

    steps = (
        ("repo", repo_rows, True),
        ("issue", issue_rows, True),
        ("pull", pull_rows, True),
        ("ci_run", run_rows, False),  # 최근 일부만 조회하므로 목록에 없어도 닫지 않는다
    )
    errors: list[str] = []
    for kind, fetch, close_missing in steps:
        try:
            apply(kind, fetch(), close_missing)
        except Exception as exc:  # 한 종류의 실패는 나머지를 막지 않는다
            errors.append(_error_kind(exc))
    if not errors:
        return "ok", ""
    return ("failed" if len(errors) == len(steps) else "partial"), errors[0]


def _sync_plane(ref: RepoRef, pm: PmAdapter, apply: _Apply) -> tuple[str, str]:
    try:
        ids: dict[str, str] = {}
        rows: list[EntityRow] = []
        if ref.plane_keys:  # 키가 없으면 호출 없이 남은 행만 닫는다
            ids = {info.identifier: info.id for info in pm.list_project_infos()}
            config = _ledger_config(Path(ref.path))
            configured = {} if config is None else {project.key: project for project in config.projects}
            for key in ref.plane_keys:
                project = configured.get(key)
                attrs = {
                    "key": key,
                    "title": "" if project is None else project.title,
                    "default": False if project is None else project.default,
                    "id": ids.get(key, ""),
                    "found": key in ids,
                }
                rows.append(EntityRow("plane_project", key, "open", attrs))
        apply("plane_project", rows)
    except Exception as exc:
        return "failed", _error_kind(exc)

    cycle_error = ""
    try:
        cycles: list[EntityRow] = []
        for key in ref.plane_keys:
            cycle = pm.current_cycle(ids[key]) if key in ids else None
            if cycle is not None:
                cycles.append(EntityRow("cycle", cycle.id, "open", asdict(cycle), parent=("plane_project", key)))
        # 하나라도 조회에 실패하면 반영하지 않는다(실패한 프로젝트의 cycle이 닫히지 않게).
        apply("cycle", cycles)
    except Exception as exc:
        cycle_error = _error_kind(exc)

    if any(key not in ids for key in ref.plane_keys):
        return "partial", "not_found"
    if cycle_error:
        return "partial", cycle_error
    return "ok", ""


def _emit(
    log: _Log,
    now: _Now,
    source: str,
    result: str,
    duration_ms: int,
    correlation_id: str,
    api_calls: int,
    extra: Optional[dict[str, str]] = None,
) -> None:
    event: dict[str, Any] = {
        "ts": _stamp(now),
        "level": _LOG_LEVELS[result],
        "component": "sync",
        "source": source,
        "action": "sync",
        "result": result,
        "duration_ms": duration_ms,
        "correlation_id": correlation_id,
        "api_calls": api_calls,
    }
    log({**event, **(extra or {})})


def _run_source(
    store: HubStore,
    repo_id: int,
    ref: RepoRef,
    source: str,
    base_url: str,
    adapter: Optional[_AdapterT],
    work: Callable[[_AdapterT, _Apply], tuple[str, str]],
    now: _Now,
    log: _Log,
) -> SyncOutcome:
    """소스 하나를 대조하고 `sync_run` 한 줄과 로그 한 줄을 남긴다."""
    source_id = store.source_id(source, base_url)
    run_id = store.start_run(source_id, repo_id, _stamp(now))
    started = time.monotonic()
    api_calls = 0
    if adapter is None:
        result, error_kind = "skipped", "no_token"
    else:
        calls_before = adapter.calls
        result, error_kind = work(adapter, partial(_apply, store, source_id, repo_id, now))
        api_calls = adapter.calls - calls_before
    store.finish_run(run_id, result, api_calls, error_kind, _stamp(now))
    _emit(log, now, source, result, int((time.monotonic() - started) * 1000), f"run-{run_id}", api_calls)
    return SyncOutcome(f"{ref.owner}/{ref.name}", source, result, api_calls, error_kind)


def _repo_plane_workspace(store: HubStore, ref: RepoRef) -> str:
    """저장소의 Plane 워크스페이스. 존재하는 어느 경로에도 `ledger.yml`이 없을 때만 빈 문자열이다.

    지금 대조하는 경로의 값을 먼저 쓴다. 같은 저장소의 다른 경로(모노레포의 상위 폴더 등)에는 `ledger.yml`이 없을 수 있다.
    """
    if ref.plane_workspace:
        return ref.plane_workspace
    repo = next((row for row in store.repos() if (row.owner, row.name) == (ref.owner, ref.name)), None)
    if repo is None:
        return ""
    unreadable = False
    for path in repo.local_paths:
        if path == ref.path or not os.path.isdir(path):
            continue
        try:
            config = _ledger_config(Path(path))
        except RepoResolveError:
            unreadable = True
            continue
        if config is not None and config.workspace:
            return config.workspace
    # 읽지 못한 `ledger.yml`은 없는 것이 아니므로 이전 값을 둔다.
    return repo.plane_workspace if unreadable else ""


def register_repo(store: HubStore, ref: RepoRef) -> tuple[int, str]:
    """경로를 저장소에 등록한다. (repo_id, 저장소의 Plane 워크스페이스)를 돌려준다."""
    workspace = _repo_plane_workspace(store, ref)
    return store.upsert_repo(ref.owner, ref.name, ref.path, workspace), workspace


def sync_repo(
    store: HubStore,
    ref: RepoRef,
    forge: Optional[ForgeAdapter],
    forge_url: str,
    pm: Optional[PmAdapter],
    pm_url: str,
    now: Callable[[], datetime],
    log: Callable[[dict[str, Any]], None],
) -> list[SyncOutcome]:
    repo_id, workspace = register_repo(store, ref)
    outcomes = [
        _run_source(store, repo_id, ref, "gitea", forge_url, forge, partial(_sync_gitea, store, repo_id, ref), now, log)
    ]
    if ref.plane_workspace:
        outcomes.append(_run_source(store, repo_id, ref, "plane", pm_url, pm, partial(_sync_plane, ref), now, log))
    elif not workspace:
        _close_plane_rows(store, repo_id, ref, pm_url, now, log)
    return outcomes


def _close_plane_rows(store: HubStore, repo_id: int, ref: RepoRef, pm_url: str, now: _Now, log: _Log) -> None:
    """`ledger.yml`이 모두 없어진 저장소에 남은 Plane 행을 닫는다.

    Plane을 부르지 않고 `sync_run`도 남기지 않는다. 닫을 때만 로그 한 줄을 남긴다.
    """
    kinds = ("cycle", "plane_project")
    if not any(store.entities(repo_id, kind) for kind in kinds):
        return
    source_id = store.source_id("plane", pm_url)
    for kind in kinds:
        _apply(store, source_id, repo_id, now, kind, [])
    extra = {"repo": f"{ref.owner}/{ref.name}", "error_kind": "no_ledger"}
    _emit(log, now, "plane", "skipped", 0, "", 0, extra)


def _record_missing_path(
    store: HubStore,
    path: str,
    forge_for: Callable[[], tuple[Optional[ForgeAdapter], str]],
    pm_for: Callable[[str], tuple[Optional[PmAdapter], str]],
    now: _Now,
    log: _Log,
) -> list[SyncOutcome]:
    """사라진 경로를 기록한다. 로그에는 경로 대신 `owner/name`을 쓴다.

    저장소의 경로가 모두 사라졌을 때만 소스마다 건너뛴 `sync_run`을 남긴다. 남은 복제본의 결과를 덮지 않기 위해서다.
    """
    extra = {"error_kind": "path_missing"}
    sources: list[tuple[str, str]] = []
    repo = store.find_repo_by_path(str(Path(path)))
    if repo is not None:
        extra["repo"] = f"{repo.owner}/{repo.name}"
        # `os.path.isdir`는 읽을 수 없는 경로에 예외를 내지 않는다.
        if not any(os.path.isdir(local_path) for local_path in repo.local_paths):
            forge_url = forge_for()[1]
            if forge_url:
                sources.append(("gitea", forge_url))
            if repo.plane_workspace:
                sources.append(("plane", pm_for(repo.plane_workspace)[1]))
    if repo is None or not sources:
        _emit(log, now, "", "skipped", 0, "", 0, extra)
        return [SyncOutcome(extra.get("repo", path), "", "skipped", 0, "path_missing")]
    outcomes: list[SyncOutcome] = []
    for source, base_url in sources:
        run_id = store.start_run(store.source_id(source, base_url), repo.id, _stamp(now))
        store.finish_run(run_id, "skipped", 0, "path_missing", _stamp(now))
        _emit(log, now, source, "skipped", 0, f"run-{run_id}", 0, extra)
        outcomes.append(SyncOutcome(extra["repo"], source, "skipped", 0, "path_missing"))
    return outcomes


def sync_paths(
    store: HubStore,
    paths: Sequence[str],
    make_forge: Callable[[], tuple[Optional[ForgeAdapter], str]],
    make_pm: Callable[[str], tuple[Optional[PmAdapter], str]],
    now: Callable[[], datetime],
    log: Callable[[dict[str, Any]], None],
) -> list[SyncOutcome]:
    """경로들을 차례로 대조한다. 한 경로를 읽지 못해도 다음 경로는 대조한다."""
    # 어댑터는 호출 한도를 스스로 세므로 저장소끼리 같은 것을 쓴다.
    forge_for = lru_cache(maxsize=None)(make_forge)
    pm_for = lru_cache(maxsize=None)(make_pm)
    outcomes: list[SyncOutcome] = []
    for path in paths:
        try:
            ref = resolve_repo(Path(path)) if Path(path).is_dir() else None
        except (RepoResolveError, OSError):
            _emit(log, now, "", "failed", 0, "", 0)
            outcomes.append(SyncOutcome(path, "", "failed", 0, "error"))
            continue
        if ref is None:
            outcomes.extend(_record_missing_path(store, path, forge_for, pm_for, now, log))
        else:
            outcomes.extend(sync_repo(store, ref, *forge_for(), *pm_for(ref.plane_workspace), now, log))
    return outcomes


@contextmanager
def sync_lock(state_dir: Path) -> Iterator[bool]:
    """대조를 한 번에 하나만 돌리는 잠금. 이미 잠겨 있으면 기다리지 않고 False를 낸다(POSIX 전용)."""
    import fcntl

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state_dir / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            acquired = False
        else:
            acquired = True
        yield acquired
    finally:
        os.close(fd)  # 닫으면 잠금도 풀린다


def json_logger(stream: TextIO) -> Callable[[dict[str, Any]], None]:
    """사건 하나를 JSON 한 줄로 쓴다."""

    def write(event: dict[str, Any]) -> None:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()

    return write
