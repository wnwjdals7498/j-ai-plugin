from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path
from typing import Mapping, NoReturn, Optional

from ledger import flows
from ledger.check import CheckReport, run_check
from ledger.config import load_config
from ledger.hubcli import HUB_COMMANDS, add_hub_parsers, run_hub
from ledger.logcli import add_log_parser, run_log
from ledger.migrate import check_manifest, load_manifest, resolve_source_dir
from ledger.miniyaml import MiniYamlError
from ledger.plane import (
    PlaneAuthError,
    PlaneClient,
    PlaneConflict,
    PlaneError,
    PlaneSettings,
    PlaneUnavailable,
)
from ledger.ratelimit import make_shared_limiter
from ledger.secrets import find_secrets

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "skills" / "ledger-plan" / "templates"


class _CliInputError(Exception):
    """CLI 인자 파싱 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _CliInputError(message)


def _sanitize_error(message: str, token: Optional[str]) -> str:
    single = " ".join(message.splitlines()).strip()
    if token and token in single:
        single = single.replace(token, "[REDACTED]")
    if find_secrets(single):
        return "오류 메시지에 민감 정보가 포함되어 생략함"
    return single or "알 수 없는 오류"


def _emit_error(message: str, token: Optional[str]) -> None:
    print(_sanitize_error(message, token), file=sys.stderr)


def _build_parser() -> _ArgumentParser:
    repo_parent = _ArgumentParser(add_help=False)
    repo_parent.add_argument("--repo", default=argparse.SUPPRESS, help="작업 저장소 경로")

    parser = _ArgumentParser(prog="ledger", description="forge-ledger 계획서 관리 CLI")
    parser.add_argument("--repo", default=None, help="작업 저장소 경로")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[repo_parent], help="저장소와 유지보수 프로젝트 초기화")

    plan_parser = sub.add_parser("plan", parents=[repo_parent], help="계획서 관리 명령")
    plan_sub = plan_parser.add_subparsers(dest="plan_command", required=True)

    p_new = plan_sub.add_parser("new", parents=[repo_parent], help="단위 프로젝트 생성")
    p_new.add_argument("title", help="단위 프로젝트 제목")
    p_new.add_argument("--key", required=True, help="프로젝트 키")

    p_check = plan_sub.add_parser("check", parents=[repo_parent], help="계획서 형식·비밀값 검사")
    p_check.add_argument("--project", default=None, help="검사할 프로젝트 키")
    p_check.add_argument("--json", action="store_true", dest="as_json", help="JSON 형식으로 출력")

    p_get = plan_sub.add_parser("get", parents=[repo_parent], help="문서 절 본문 조회")
    p_get.add_argument("key", help="프로젝트 키")
    p_get.add_argument("doc", help="문서 코드")

    p_put = plan_sub.add_parser("put", parents=[repo_parent], help="문서 절 본문 갱신")
    p_put.add_argument("key", help="프로젝트 키")
    p_put.add_argument("doc", help="문서 코드")
    p_put.add_argument("--from", required=True, dest="from_file", help="본문 HTML 파일 경로")

    p_decide = sub.add_parser("decide", parents=[repo_parent], help="결정 항목 기록")
    p_decide.add_argument("title", help="결정 제목")
    p_decide.add_argument("--project", required=True, help="대상 프로젝트 키 또는 *")
    p_decide.add_argument("--body-file", required=True, dest="body_file", help="결정 본문 HTML 파일 경로")

    migrate_parser = sub.add_parser("migrate", parents=[repo_parent], help="PMT 이전 명령")
    migrate_sub = migrate_parser.add_subparsers(dest="migrate_command", required=True)
    m_check = migrate_sub.add_parser("check", parents=[repo_parent], help="매니페스트 형식·노출 검사")
    m_check.add_argument("file", help="매니페스트 JSON 경로")

    add_hub_parsers(sub, repo_parent)
    add_log_parser(sub)

    return parser


def _render_check_table(report: CheckReport) -> str:
    lines = ["문서 | 상태 | 이유", "--- | --- | ---"]
    for row in report.results:
        reason = ", ".join(row.reasons) if row.reasons else "-"
        lines.append(f"{row.scope}:{row.code} | {row.state} | {reason}")
    return "\n".join(lines)


def _run_plan_check(
    repo: flows.Repo,
    client: PlaneClient,
    project: Optional[str],
    as_json: bool,
    token: Optional[str],
) -> int:
    cfg_path = repo.root / ".ledger" / "ledger.yml"
    if not cfg_path.is_file():
        raise flows.FlowError("초기화되지 않은 저장소: .ledger/ledger.yml 없음")
    cfg = load_config(cfg_path)

    if project is not None:
        if not any(p.key == project for p in cfg.projects):
            raise flows.FlowError(f"등록되지 않은 프로젝트 키: {project}")
        project_keys = [project]
    else:
        project_keys = [p.key for p in cfg.projects if not p.default]

    source = flows.PlaneDocSource(client, cfg)
    report = run_check(repo.root, project_keys, source)
    if as_json:
        print(report.to_json())
    elif report.exit_code == 2:
        if report.error == "auth":
            _emit_error("Plane 토큰 확인 필요", token)
        else:
            _emit_error("Plane 연결 실패", token)
    else:
        print(_render_check_table(report))
    return report.exit_code


def _run_migrate_check(path: Path, env: Mapping[str, str]) -> int:
    """매니페스트만 읽는다. Plane 설정이나 작업 저장소가 필요 없다."""
    manifest = load_manifest(path)
    problems = check_manifest(manifest, resolve_source_dir(manifest, env))
    if problems:
        for problem in problems:
            print(f"{problem.key}: {problem.message}")
        print(f"문제 {len(problems)}건")
        return 1
    print(f"검사 통과: 레코드 {len(manifest.records)}건")
    return 0


def main(
    argv: Optional[list[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    effective_env: Mapping[str, str] = os.environ if env is None else env
    token = effective_env.get("FORGE_LEDGER_PLANE_TOKEN", "").strip() or None

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0
    except _CliInputError as exc:
        _emit_error(str(exc), token)
        return 1

    if args.command == "log":
        return run_log(args, effective_env, out=sys.stdout, err=sys.stderr)

    if args.command in HUB_COMMANDS:
        return run_hub(args, effective_env, out=sys.stdout, err=sys.stderr)

    if args.command == "migrate":
        try:
            return _run_migrate_check(Path(args.file), effective_env)
        except (OSError, ValueError) as exc:
            _emit_error(str(exc), token)
            return 1

    try:
        if args.command == "init" and args.repo:
            repo = flows.Repo.at(Path(args.repo))  # `.ledger/`를 새로 만들 곳이라 위로 찾지 않는다
        else:
            repo = flows.Repo.discover(Path(args.repo) if args.repo else Path.cwd())
        settings = PlaneSettings.from_env(effective_env)
        found_ws = find_secrets(settings.workspace)
        if found_ws:
            raise flows.SecretFound(f"비밀값 감지: {', '.join(found_ws)}")
        client = PlaneClient(
            settings,
            limiter=make_shared_limiter(settings.base_url, settings.token, effective_env),
        )
        today = date.today()

        if args.command == "init":
            cfg = flows.init_repo(repo, client, settings.workspace, _TEMPLATES_DIR, today)
            print(f"초기화 완료: {cfg.default_project().key} ({cfg.workspace})")
            return 0

        if args.command == "plan":
            if args.plan_command == "new":
                res = flows.plan_new(repo, client, args.title, args.key, _TEMPLATES_DIR, today)
                print(f"생성 완료: {args.key} ({res.decision_id})")
                return 0
            if args.plan_command == "check":
                return _run_plan_check(repo, client, args.project, args.as_json, token)
            if args.plan_command == "get":
                body = flows.plan_get(repo, client, args.key, args.doc)
                sys.stdout.write(body)
                return 0
            if args.plan_command == "put":
                body_html = Path(args.from_file).read_text(encoding="utf-8")
                updated_at = flows.plan_put(repo, client, args.key, args.doc, body_html)
                print(f"갱신 완료: {args.key} {args.doc} ({updated_at})")
                return 0

        if args.command == "decide":
            body_html = Path(args.body_file).read_text(encoding="utf-8")
            entry = flows.decide(repo, client, args.title, args.project, body_html, today)
            print(f"결정 기록 완료: {entry.ref_id} ({entry.projects})")
            return 0

        _emit_error("지원하지 않는 명령", token)
        return 1
    except flows.SecretFound as exc:
        _emit_error(str(exc), token)
        return 4
    except PlaneConflict as exc:
        _emit_error(str(exc), token)
        return 3
    except (PlaneAuthError, PlaneUnavailable, PlaneError) as exc:
        _emit_error(str(exc), token)
        return 2
    except (flows.FlowError, MiniYamlError, OSError, ValueError) as exc:
        _emit_error(str(exc), token)
        return 1
