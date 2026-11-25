"""허브 CLI 명령(`sync`·`status`·`dashboard`). Plane 설정 없이도 돌아야 하며, 필요한 값은 환경 변수에서만 읽는다."""

from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TextIO

from ledger.adapters import ForgeAdapter, PmAdapter
from ledger.dashboard import DashboardApp, serve
from ledger.flows import Repo
from ledger.gitea import GiteaClient, GiteaError, GiteaSettings
from ledger.hubstore import HubStore, load_registry, save_registry
from ledger.hubsync import (
    RepoResolveError,
    SyncOutcome,
    json_logger,
    project_root,
    register_repo,
    resolve_repo,
    sync_lock,
    sync_paths,
)
from ledger.hubview import render_text, summary
from ledger.plane import PlaneClient, PlaneError, PlaneSettings
from ledger.ratelimit import default_state_dir, make_shared_limiter

HUB_COMMANDS = ("sync", "status", "dashboard")

_GITEA_PER_MINUTE = 120
_PLANE_PER_MINUTE = 50
_BUSY_TEXT = "다른 대조가 진행 중입니다"
_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
_DEFAULT_BIND = "127.0.0.1"
_DEFAULT_INTERVAL_S = 600
_MIN_INTERVAL_S = 60

_Now = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def add_hub_parsers(sub: argparse._SubParsersAction[Any], repo_parent: argparse.ArgumentParser) -> None:
    sync = sub.add_parser("sync", parents=[repo_parent], help="Gitea·Plane 상태를 허브에 대조")
    sync.add_argument("--quiet", action="store_true", help="표준 출력 없이 실행")
    status = sub.add_parser("status", parents=[repo_parent], help="허브에 모은 요약 출력")
    status.add_argument("--json", action="store_true", dest="as_json", help="JSON 형식으로 출력")
    dashboard = sub.add_parser("dashboard", help="로컬 대시보드(S1) 실행")
    dashboard.add_argument("--bind", help="열 주소(기본 FORGE_LEDGER_DASHBOARD_BIND, 없으면 127.0.0.1)")
    dashboard.add_argument("--port", type=int, default=0, help="포트(기본 0은 빈 포트)")
    dashboard.add_argument("--interval", type=int, default=_DEFAULT_INTERVAL_S, help="대조 주기(초, 최소 60)")


def _make_forge(env: Mapping[str, str]) -> tuple[Optional[ForgeAdapter], str]:
    """(어댑터, 서버 주소). 주소나 토큰이 없으면 어댑터는 `None`이다."""
    url = env.get("FORGE_LEDGER_GITEA_URL", "").strip()
    token = env.get("FORGE_LEDGER_GITEA_TOKEN", "").strip()
    if not url or not token:
        return None, url
    limiter = make_shared_limiter(url, token, env, per_minute=_GITEA_PER_MINUTE)
    return GiteaClient(GiteaSettings(url, token), limiter=limiter), url


def _make_pm(env: Mapping[str, str], workspace: str) -> tuple[Optional[PmAdapter], str]:
    """워크스페이스는 환경 변수가 아니라 저장소의 `ledger.yml` 값이다."""
    url = env.get("FORGE_LEDGER_PLANE_URL", "").strip()
    token = env.get("FORGE_LEDGER_PLANE_TOKEN", "").strip()
    if not workspace or not url or not token:
        return None, url
    limiter = make_shared_limiter(url, token, env, per_minute=_PLANE_PER_MINUTE)
    return PlaneClient(PlaneSettings(url, workspace, token), limiter=limiter), url


def _register_target(repo_arg: Optional[str]) -> Optional[Path]:
    """등록할 저장소 폴더. `--repo`가 없고 현재 폴더가 git 저장소가 아니면 `None`이다."""
    if repo_arg:
        return project_root(Path(repo_arg))
    if not (Repo.discover(Path.cwd()).root / ".git").exists():
        return None
    return project_root(Path.cwd())


def _outcome_line(outcome: SyncOutcome) -> str:
    line = f"{outcome.repo} {outcome.source or '-'}: {outcome.result}"
    return f"{line} ({outcome.error_kind})" if outcome.error_kind else line


def _sync(store: HubStore, paths: list[str], env: Mapping[str, str], now: _Now, err: TextIO) -> list[SyncOutcome]:
    return sync_paths(store, paths, lambda: _make_forge(env), lambda ws: _make_pm(env, ws), now, json_logger(err))


def _run_sync(args: argparse.Namespace, env: Mapping[str, str], now: _Now, out: TextIO, err: TextIO) -> int:
    def say(text: str) -> None:
        if not args.quiet:
            print(text, file=out)

    state_dir = default_state_dir(env)
    with sync_lock(state_dir) as acquired:
        if not acquired:
            say(_BUSY_TEXT)
            return 0
        store = HubStore(state_dir)
        target = _register_target(args.repo)
        if target is not None:
            ref = resolve_repo(target)
            save_registry(state_dir, [*load_registry(state_dir), str(target)])
            register_repo(store, ref)
        paths = [str(target)] if args.repo else load_registry(state_dir)
        outcomes = _sync(store, paths, env, now, err)
    say("\n".join(map(_outcome_line, outcomes)) if outcomes else render_text({"repos": []}))
    return 2 if any(outcome.result == "failed" for outcome in outcomes) else 0


def _run_status(args: argparse.Namespace, env: Mapping[str, str], now: _Now, out: TextIO) -> None:
    data = summary(HubStore(default_state_dir(env)), now())
    print(json.dumps(data, ensure_ascii=False) if args.as_json else render_text(data), file=out)


def _run_dashboard(args: argparse.Namespace, env: Mapping[str, str], now: _Now, out: TextIO, err: TextIO) -> int:
    if args.interval < _MIN_INTERVAL_S:
        print(f"--interval은 {_MIN_INTERVAL_S}초 이상이어야 합니다", file=err)
        return 1
    if not 0 <= args.port <= 65535:  # 범위 밖 값은 bind가 `OverflowError`를 낸다
        print("--port는 0~65535여야 합니다", file=err)
        return 1
    state_dir = default_state_dir(env)
    store = HubStore(state_dir)

    def sync_once() -> None:
        with sync_lock(state_dir) as acquired:
            if acquired:  # 다른 대조가 돌고 있으면 이번 주기는 건너뛴다
                _sync(store, load_registry(state_dir), env, now, err)

    app = DashboardApp(store, secrets.token_urlsafe(32), args.interval, _DASHBOARD_DIR, now)
    bind = args.bind or env.get("FORGE_LEDGER_DASHBOARD_BIND", "").strip() or _DEFAULT_BIND
    return serve(app, bind, args.port, sync_once, out, threading.Event())


def run_hub(
    args: argparse.Namespace,
    env: Mapping[str, str],
    *,
    now: Callable[[], datetime] = _utc_now,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    try:
        if args.command == "sync":
            return _run_sync(args, env, now, out, err)
        if args.command == "status":
            _run_status(args, env, now, out)
            return 0
        if args.command == "dashboard":
            return _run_dashboard(args, env, now, out, err)
        print("지원하지 않는 명령", file=err)
        return 1
    except (RepoResolveError, GiteaError, PlaneError) as exc:  # 문구에 주소·토큰이 없는 예외
        print(str(exc), file=err)
    except (OSError, ValueError, sqlite3.Error) as exc:  # 문구에 무엇이 섞일지 모르므로 종류만 남긴다
        print(f"허브 명령 실패: {type(exc).__name__}", file=err)
    return 0 if args.command == "status" else 1
