"""작업 일지 명령과 미전송 기록 동기화."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TextIO

from ledger.adapters import WorklogStore
from ledger.flows import git_toplevel
from ledger.plane import PlaneAuthError, PlaneClient, PlaneError, PlaneSettings, PlaneUnavailable
from ledger.ratelimit import default_state_dir, make_shared_limiter
from ledger.worklog import LocalLog, RemoteEntry, WorklogError, new_entry
from ledger.worklog_plane import PlaneWorklog


@dataclass(frozen=True)
class SyncResult:
    sent: int
    pending: int
    other_workspace: int
    failed: int = 0


class LogInputError(Exception):
    """사용자 입력에 대한 고정 안내."""


def make_worklog_store(env: Mapping[str, str]) -> Optional[WorklogStore]:
    url = env.get("FORGE_LEDGER_PLANE_URL", "").strip()
    token = env.get("FORGE_LEDGER_PLANE_TOKEN", "").strip()
    workspace = env.get("FORGE_LEDGER_PLANE_WORKSPACE", "").strip()
    if not url or not token or not workspace:
        return None
    client = PlaneClient(PlaneSettings(url, workspace, token), limiter=make_shared_limiter(url, token, env))
    return PlaneWorklog(client, env.get("FORGE_LEDGER_WORKLOG_PROJECT", "").strip() or "WLOG", workspace)


def sync_pending(
    local: LocalLog,
    store: WorklogStore,
    workspace: str,
    *,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> SyncResult:
    sent = 0
    failed = 0
    for entry in local.pending(workspace):
        try:
            comment_id = store.post_entry(entry)
        except PlaneAuthError:
            raise
        except (PlaneUnavailable, OSError, IncompleteRead) as exc:
            if on_error:
                on_error(exc)
            break
        except PlaneError as exc:
            failed += 1
            if on_error:
                on_error(exc)
            continue
        local.mark_sent(entry.id, comment_id)
        sent += 1
    return SyncResult(sent, len(local.pending(workspace)), local.pending_other(workspace), failed)


def add_log_parser(sub: argparse._SubParsersAction[Any]) -> None:
    log = sub.add_parser("log", help="작업 일지")
    commands = log.add_subparsers(dest="log_command", required=True)
    commands.add_parser("init", help="작업 일지 프로젝트 초기화")
    add = commands.add_parser("add", help="작업 기록")
    add.add_argument("text")
    add.add_argument("--item", default="")
    add.add_argument("--repo", default=None)
    add.add_argument("--at")
    add.add_argument("--auto", action="store_true")
    add.add_argument("--session", default=None)
    commands.add_parser("sync", help="미전송 기록 동기화")
    show = commands.add_parser("show", help="작업 기록 조회")
    show.add_argument("--date")
    show.add_argument("--from", dest="from_")
    show.add_argument("--to")
    group = show.add_mutually_exclusive_group()
    group.add_argument("--user")
    group.add_argument("--all", action="store_true")


def _dates(args: argparse.Namespace, today: date) -> tuple[date, date]:
    if args.date and (args.from_ or args.to) or bool(args.from_) != bool(args.to):
        raise LogInputError("날짜는 --date 또는 --from과 --to를 함께 지정하세요")
    try:
        start = date.fromisoformat(args.date or args.from_) if args.date or args.from_ else today
        end = date.fromisoformat(args.date or args.to) if args.date or args.to else today
    except ValueError as exc:
        raise LogInputError("날짜 형식이 올바르지 않습니다") from exc
    if start > end:
        raise LogInputError("시작 날짜가 종료 날짜보다 늦습니다")
    return start, end


def _show(
    args: argparse.Namespace, local: LocalLog, store: Optional[WorklogStore], workspace: str, now: datetime, out: TextIO
) -> None:
    start, end = _dates(args, now.astimezone().date())
    entries: list[RemoteEntry] = []
    user = ""
    offline = False
    try:
        if store is None:
            raise PlaneUnavailable("")
        user = store.current_user()
        entries = store.list_entries(start, end, None if args.all else args.user or user)
    except PlaneError as exc:
        if str(exc).startswith("작업 일지 프로젝트 없음:"):
            print(str(exc), file=out)
            offline = True
        elif isinstance(exc, PlaneUnavailable):
            print("Plane 연결 실패: 로컬 기록만 표시", file=out)
            offline = True
        else:
            raise
    except (OSError, IncompleteRead):
        print("Plane 연결 실패: 로컬 기록만 표시", file=out)
        offline = True
    pending = {entry.id for entry in local.pending(workspace)}
    seen = {remote.entry.id for remote in entries}
    if args.all or not args.user or args.user == user:
        for entry in local.entries(start, end):
            if entry.id not in seen and (offline or entry.id in pending) and entry.workspace in ("", workspace):
                entries.append(RemoteEntry(entry, user or "나", ""))
                seen.add(entry.id)
    for remote in sorted(entries, key=lambda value: datetime.fromisoformat(value.entry.at)):
        entry = remote.entry
        parts = [datetime.fromisoformat(entry.at).strftime("%H:%M"), remote.author, entry.host]
        parts.extend(value for value in (entry.repo, entry.item) if value)
        lines = entry.text.split("\n")
        suffix = " (미전송)" if entry.id in pending else ""
        print(" ".join([*parts, lines[0]]) + suffix, file=out)
        for line in lines[1:]:
            print("  " + line, file=out)


def _print_sync_error(exc: Exception, err: TextIO) -> None:
    cause = str(exc) if isinstance(exc, PlaneError) else type(exc).__name__
    if isinstance(exc, PlaneAuthError) and not cause:
        cause = "Plane 인증 실패"
    print(f"전송 실패: {cause}", file=err)


def run_log(
    args: argparse.Namespace,
    env: Mapping[str, str],
    *,
    now: Callable[[], datetime] = datetime.now,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    make_store: Callable[[Mapping[str, str]], Optional[WorklogStore]] = make_worklog_store,
) -> int:
    try:
        local = LocalLog(default_state_dir(env))
        workspace = env.get("FORGE_LEDGER_PLANE_WORKSPACE", "").strip()
        if args.log_command == "init":
            store = make_store(env)
            if store is None:
                print("Plane 토큰 없음", file=err)
                return 1
            store.ensure_project()
            print("작업 일지 프로젝트: " + (env.get("FORGE_LEDGER_WORKLOG_PROJECT", "").strip() or "WLOG"), file=out)
        elif args.log_command == "add":
            try:
                moment = datetime.fromisoformat(args.at) if args.at else now().astimezone()
            except ValueError as exc:
                raise LogInputError("시각 형식이 올바르지 않습니다") from exc
            if moment.utcoffset() is None:
                raise LogInputError("시각에 UTC 오프셋이 필요합니다")
            root = git_toplevel(Path.cwd()) if args.repo is None else None
            repo = args.repo if args.repo is not None else re.sub(r"\s+", "-", root.name) if root else ""
            entry = new_entry(
                args.text,
                source="auto" if args.auto else "manual",
                env=env,
                now=moment,
                repo=repo,
                item=args.item,
                session=args.session or env.get("CLAUDE_CODE_SESSION_ID", ""),
            )
            if args.at:
                entry = replace(entry, at=moment.isoformat(timespec="seconds"))
            local.append(entry)
            errors: list[Exception] = []
            sent = False
            try:
                store = make_store(env)
                if store is not None:
                    try:
                        sync_pending(local, store, workspace, on_error=errors.append)
                    except Exception as exc:
                        errors.append(exc)
                sent = entry.id not in {pending.id for pending in local.pending(workspace)}
            except Exception as exc:
                errors.append(exc)
            if errors:
                _print_sync_error(errors[0], err)
            print(f"기록: {entry.id[:8]} ({'전송' if sent else '미전송'})", file=out)
        elif args.log_command == "sync":
            store = make_store(env)
            result = (
                sync_pending(local, store, workspace, on_error=lambda exc: _print_sync_error(exc, err))
                if store is not None
                else SyncResult(0, len(local.pending(workspace)), local.pending_other(workspace))
            )
            text = f"전송 {result.sent}건, 미전송 {result.pending}건"
            if result.failed:
                text += f", 실패 {result.failed}건"
            if result.other_workspace:
                text += f", 다른 워크스페이스 {result.other_workspace}건"
            print(text, file=out)
            return 1 if result.failed else 0
        elif args.log_command == "show":
            _show(args, local, make_store(env), workspace, now(), out)
        return 0
    except WorklogError as exc:
        print(f"기록 거부: {exc}", file=err)
    except LogInputError as exc:
        print(str(exc), file=err)
    except PlaneAuthError as exc:
        if args.log_command == "sync":
            _print_sync_error(exc, err)
        else:
            print(f"Plane 작업 실패: {exc}", file=err)
    except PlaneError as exc:
        print(f"Plane 작업 실패: {exc}", file=err)
    except (ValueError, OSError, IncompleteRead) as exc:
        print(f"작업 일지 명령 실패: {type(exc).__name__}", file=err)
    return 1
