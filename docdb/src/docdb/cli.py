"""docctl 명령줄 인터페이스(C-24)."""
import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

from docdb import (
    actors,
    backup,
    catalogue,
    db,
    history,
    importer,
    migrate,
    paths,
    policy,
    profile,
    relations,
    runs,
    textnorm,
    workflow,
)
from docdb.actors import Actor
from docdb.errors import DocdbError, format_error
from docdb.llm import DocDB

_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$", re.ASCII)


def _parser() -> argparse.ArgumentParser:
    """T11에서 제공하는 CLI 명령 파서를 만든다."""
    parser = argparse.ArgumentParser(prog="docctl")
    parser.add_argument("--db", metavar="PATH")
    parser.add_argument("--actor", metavar="ID")
    parser.add_argument("--work", metavar="WORK-ID")
    parser.add_argument("--session", metavar="S")
    parser.add_argument("--model", metavar="M")
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init")
    init_parser.add_argument("--project", required=True)
    init_parser.add_argument("--name", required=True)
    init_parser.add_argument("--owner", required=True)
    init_parser.add_argument("--owner-name", required=True)
    init_parser.add_argument("--catalogue", type=Path)
    init_parser.add_argument("--dir", type=Path, default=Path.cwd())

    commands.add_parser("migrate")
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--reason", default="manual")
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("file", type=Path)

    actor_parser = commands.add_parser("actor")
    actor_commands = actor_parser.add_subparsers(dest="actor_command", required=True)
    actor_commands.add_parser("list")
    actor_add = actor_commands.add_parser("add")
    actor_add.add_argument("id")
    actor_add.add_argument("--type", required=True, choices=("human", "ai", "team"))
    actor_add.add_argument("--name", required=True)

    history_parser = commands.add_parser("verify-history")
    history_parser.add_argument("--entity", nargs=2, metavar=("TYPE", "ID"))

    catalogue_parser = commands.add_parser("catalogue")
    catalogue_commands = catalogue_parser.add_subparsers(
        dest="catalogue_command", required=True
    )
    catalogue_load = catalogue_commands.add_parser("load")
    catalogue_load.add_argument("json", type=Path)
    catalogue_load.add_argument("--item-types", type=Path)

    policy_parser = commands.add_parser("policy")
    policy_commands = policy_parser.add_subparsers(dest="policy_command", required=True)
    allow_parser = policy_commands.add_parser("allow-secret")
    allow_parser.add_argument("handle")
    allow_parser.add_argument("--reason", required=True)
    allow_parser.add_argument("--days", type=int, required=True)

    ctx_parser = commands.add_parser("ctx")
    ctx_parser.add_argument("work_id")
    ctx_parser.add_argument("--budget", type=int, default=1500)

    get_parser = commands.add_parser("get")
    get_parser.add_argument("handles", nargs="+")
    get_parser.add_argument("--level", type=int, default=1)
    get_parser.add_argument("--known", default="{}", help='{"handle":"dg"} JSON')
    get_parser.add_argument("--budget", type=int, default=2000)

    find_parser = commands.add_parser("find")
    find_parser.add_argument("query")
    find_parser.add_argument("--limit", type=int, default=10)

    trace_parser = commands.add_parser("trace")
    trace_parser.add_argument("handle")
    trace_parser.add_argument("--rel", nargs="*")
    trace_parser.add_argument("--depth", type=int, default=1)

    since_parser = commands.add_parser("since")
    since_parser.add_argument("cursor", type=int)
    since_parser.add_argument("--work-only", action="store_true")

    put_parser = commands.add_parser("put")
    put_parser.add_argument("patches", help="JSON 배열 또는 @파일")
    put_parser.add_argument("--reason")

    work_parser = commands.add_parser("work")
    work_commands = work_parser.add_subparsers(dest="work_command", required=True)
    work_status = work_commands.add_parser("status")
    work_status.add_argument("work_id")
    work_status.add_argument("status")

    handoff_parser = commands.add_parser("handoff")
    handoff_commands = handoff_parser.add_subparsers(
        dest="handoff_command", required=True
    )
    handoff_ack = handoff_commands.add_parser("ack")
    handoff_ack.add_argument("handoff_id")
    handoff_ack.add_argument("--baseline", required=True)
    handoff_ack.add_argument("--evidence", nargs="*", default=[])
    handoff_ack.add_argument("--reject", action="store_true")

    link_parser = commands.add_parser("link")
    link_commands = link_parser.add_subparsers(dest="link_command", required=True)
    link_review = link_commands.add_parser("review")
    link_review.add_argument("--work", dest="link_work")
    link_confirm = link_commands.add_parser("confirm")
    link_confirm.add_argument("rel_ids", nargs="+", type=int)
    link_reject = link_commands.add_parser("reject")
    link_reject.add_argument("rel_id", type=int)
    link_reject.add_argument("--why", required=True)
    link_remove = link_commands.add_parser("remove")
    link_remove.add_argument("rel_id", type=int)
    link_remove.add_argument("--why", required=True)

    fact_parser = commands.add_parser("fact")
    fact_commands = fact_parser.add_subparsers(dest="fact_command", required=True)
    fact_commands.add_parser("list")
    fact_set = fact_commands.add_parser("set")
    fact_set.add_argument("fact_key")
    fact_set.add_argument("value", choices=("true", "false", "unknown"))
    fact_set.add_argument("--reason", required=True)

    commands.add_parser("applicability")

    run_parser = commands.add_parser("run")
    run_commands = run_parser.add_subparsers(dest="run_command", required=True)
    run_ingest = run_commands.add_parser("ingest")
    run_ingest.add_argument("file", help="JSON 파일 경로 또는 - (표준 입력)")

    import_parser = commands.add_parser("import")
    import_commands = import_parser.add_subparsers(dest="import_command", required=True)
    import_scan = import_commands.add_parser("scan")
    import_scan.add_argument("folder", type=Path)
    import_scan.add_argument("--owner", required=True)
    import_scan.add_argument(
        "--export-policy",
        choices=("public", "approved_providers", "local_only"),
        default="approved_providers",
    )
    import_next = import_commands.add_parser("next")
    import_next.add_argument("--budget", type=int, default=1500)
    import_label = import_commands.add_parser("label")
    import_label.add_argument("labels", help="JSON 배열 또는 @파일")
    import_commands.add_parser("report")
    return parser


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """인자를 읽고 파서 오류를 계약 오류로 바꾼다."""
    parser = _parser()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if "-h" in args or "--help" in args:
            return parser.parse_args(args)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return parser.parse_args(args)
    except SystemExit as exc:
        if exc.code == 0:
            raise
        raise DocdbError("BAD_ARGS", "명령 인자가 올바르지 않음") from exc
    except argparse.ArgumentError as exc:
        raise DocdbError("BAD_ARGS", "명령 인자가 올바르지 않음") from exc


def _db_path(db_arg: str | None) -> Path:
    """전역 경로나 현재 프로젝트 탐색으로 DB 경로를 정한다."""
    return Path(db_arg) if db_arg is not None else paths.find_db()


def _project_id(con: db.Connection) -> str:
    """DB에 저장된 프로젝트 ID를 반환한다."""
    row = con.execute("SELECT project_id FROM project LIMIT 1").fetchone()
    if row is None:
        raise DocdbError("NOT_FOUND", "프로젝트를 찾을 수 없음")
    return str(row[0])


def _human_actor(con: db.Connection, actor_id: str | None, interactive: bool) -> Actor:
    """사람 행위자와 대화형 세션을 요구한다."""
    if not interactive:
        raise DocdbError("NEEDS_HUMAN", "사람 및 대화형 터미널(TTY)이 필요함")
    actor = actors.resolve_actor(con, actor_id)
    actors.require_human(actor, interactive)
    return actor


def _llm_command(con: db.Connection, args: argparse.Namespace) -> int:
    """LLM 조회·필드 쓰기 명령을 실행하고 응답을 stdout에 쓴다."""
    actor = actors.resolve_actor(con, args.actor)
    work = args.work or os.environ.get("DOCDB_WORK")
    session = args.session or os.environ.get("DOCDB_SESSION")
    model = args.model or os.environ.get("DOCDB_MODEL")
    client = DocDB(con, actor, work, session, model)
    if args.command == "ctx":
        result = client.ctx(args.work_id, args.budget)
    elif args.command == "get":
        try:
            known = json.loads(args.known)
        except json.JSONDecodeError as exc:
            raise DocdbError("BAD_ARGS", "known은 JSON 객체여야 함") from exc
        if not isinstance(known, dict):
            raise DocdbError("BAD_ARGS", "known은 JSON 객체여야 함")
        result = client.get(args.handles, args.level, known, args.budget)
    elif args.command == "find":
        result = client.find(args.query, args.limit)
    elif args.command == "trace":
        result = client.trace(args.handle, args.rel, args.depth)
    elif args.command == "since":
        result = client.since(args.cursor, work if args.work_only else None)
    else:
        raw = args.patches
        if raw.startswith("@"):
            try:
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            except OSError as exc:
                raise DocdbError("BAD_ARGS", "put 파일을 읽을 수 없음") from exc
        try:
            patches = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DocdbError("BAD_ARGS", "put 입력이 올바른 JSON이 아님") from exc
        result = client.put(patches, args.reason)
    sys.stdout.write(result + "\n")
    return 0


def _init(args: argparse.Namespace) -> int:
    """새 프로젝트 DB, 카탈로그, 기본 행위자를 만든다."""
    if not _PROJECT_ID.fullmatch(args.project):
        raise DocdbError("BAD_ARGS", "프로젝트 ID 형식이 올바르지 않음")
    if not re.fullmatch(actors.ACTOR_PATTERNS["human"], args.owner, re.ASCII):
        raise DocdbError("ID_FORMAT", f"사람 행위자 ID 형식 위반: {args.owner}")
    project_name = textnorm.normalize_text(args.name) or ""

    root = args.dir.resolve()
    project_dir = root / paths.DOT_DIR
    db_path = project_dir / paths.DB_FILE
    if db_path.exists():
        raise DocdbError("DB_EXISTS", f"이미 초기화된 DB가 있음: {db_path}")

    starter = Path(__file__).resolve().parents[2] / "catalogue" / "starter.json"
    item_types = starter.with_name("item_types.json")
    catalogue_path = args.catalogue or starter
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / ".gitignore").write_text("*\n", encoding="utf-8")
        con = db.connect(db_path, create=True)
    except OSError as exc:
        raise DocdbError("DB", str(exc)[:80]) from exc

    try:
        db.check_capabilities(con)
        migrate.apply_pending(con)
        base_actors = (
            (args.owner, "human", args.owner_name),
            ("ai-claude", "ai", "Claude Code"),
            ("ai-codex", "ai", "Codex"),
            ("team-ci", "team", "CI"),
        )
        for actor_id, actor_type, name in base_actors:
            actors.add_actor(con, actor_id, actor_type, name, None)
        owner = Actor(args.owner, "human")
        catalogue.load(con, catalogue_path, item_types, owner)
        version = con.execute(
            "SELECT catalogue_version FROM cat_version ORDER BY loaded_at DESC LIMIT 1"
        ).fetchone()[0]
        with db.write_tx(con):
            policy.assert_no_secrets(con, f"project:{args.project}", [project_name])
            con.execute(
                "INSERT INTO project (project_id, name, catalogue_version) VALUES (?, ?, ?)",
                (args.project, project_name, version),
            )
            history.append_change(
                con=con,
                entity_type="project",
                entity_id=args.project,
                op="insert",
                before=None,
                after={
                    "project_id": args.project,
                    "name": project_name,
                    "catalogue_version": version,
                },
                actor_id=owner.id,
                work_id=None,
                reason=None,
            )
    except BaseException:
        con.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        raise
    con.close()
    sys.stdout.write(f"OK init project={args.project} db={db_path}\n")
    return 0


def _run(args: argparse.Namespace, interactive: bool) -> int:
    """선택한 T11 명령을 실행한다."""
    if args.command == "init":
        return _init(args)

    db_path = _db_path(args.db)
    if args.command == "migrate":
        con = db.connect(db_path)
        try:
            db.check_capabilities(con)
            backup.backup(con, _project_id(con), "pre-migrate")
            migrate.apply_pending(con)
        finally:
            con.close()
        return 0

    if args.command == "restore":
        con = db.connect(db_path)
        try:
            actor = _human_actor(con, args.actor, interactive)
            backup.backup(con, _project_id(con), "pre-restore")
        finally:
            con.close()
        backup.restore(db_path, args.file)
        return 0

    con = db.open_project_db(db_path=db_path)
    try:
        if args.command in {"ctx", "get", "find", "trace", "since", "put"}:
            return _llm_command(con, args)

        if args.command == "link":
            if args.link_command == "review":
                work_id = args.link_work or args.work
                for line in relations.review(con, work_id):
                    sys.stdout.write(line + "\n")
                return 0
            actor = _human_actor(con, args.actor, interactive)
            if args.link_command == "confirm":
                lines = relations.confirm(con, actor, args.rel_ids, interactive)
                for line in lines:
                    sys.stdout.write(line + "\n")
                return 1 if any(line.startswith("ERR ") for line in lines) else 0
            if args.link_command == "reject":
                result = relations.reject(con, actor, args.rel_id, args.why, interactive)
            else:
                result = relations.remove(con, actor, args.rel_id, args.why, interactive)
            sys.stdout.write(result + "\n")
            return 0

        if args.command == "work":
            actor = actors.resolve_actor(con, args.actor)
            result = workflow.set_status(
                con, actor, args.work_id, args.status, interactive
            )
            sys.stdout.write(result + "\n")
            return 0

        if args.command == "handoff":
            actor = actors.resolve_actor(con, args.actor)
            result = workflow.ack_handoff(
                con,
                actor,
                args.handoff_id,
                args.baseline,
                args.evidence,
                not args.reject,
                interactive,
            )
            sys.stdout.write(result + "\n")
            return 0

        if args.command == "backup":
            backup.backup(con, _project_id(con), args.reason)
            return 0

        if args.command == "actor":
            if args.actor_command == "list":
                rows = con.execute(
                    "SELECT actor_id, actor_type, name FROM actor ORDER BY actor_id"
                ).fetchall()
                for actor_id, _actor_type, _name in rows:
                    sys.stdout.write(f"{actor_id}\n")
                return 0
            actor = _human_actor(con, args.actor, interactive)
            actors.add_actor(con, args.id, args.type, args.name, actor)
            return 0

        if args.command == "verify-history":
            entity_type, entity_id = args.entity if args.entity else (None, None)
            problems = history.verify_chain(con, entity_type, entity_id)
            if problems:
                for line in problems:
                    sys.stdout.write(line + "\n")
                return 1
            sys.stdout.write("OK history\n")
            return 0

        if args.command == "catalogue":
            actor = _human_actor(con, args.actor, interactive)
            item_types_path = args.item_types or (
                Path(__file__).resolve().parents[2] / "catalogue" / "item_types.json"
            )
            catalogue.load(con, args.json, item_types_path, actor)
            return 0

        if args.command == "policy":
            actor = _human_actor(con, args.actor, interactive)
            exception_id = policy.allow_secret(
                con, args.handle, args.reason, args.days, actor, interactive
            )
            sys.stdout.write(f"OK {exception_id}\n")
            return 0

        if args.command == "fact":
            project_id = _project_id(con)
            if args.fact_command == "list":
                rows = con.execute(
                    """SELECT f.fact_key, v.value
                    FROM cat_fact f
                    LEFT JOIN fact_value v ON v.project_id = ? AND v.fact_key = f.fact_key
                    WHERE f.fact_type = 'cap'
                    ORDER BY f.fact_key""",
                    (project_id,),
                ).fetchall()
                for key, val in rows:
                    if val == 1:
                        st = "true"
                    elif val == 0:
                        st = "false"
                    else:
                        st = "unknown"
                    sys.stdout.write(f"{key} {st}\n")
                return 0

            actor = _human_actor(con, args.actor, interactive)
            if args.value == "true":
                b_val = True
            elif args.value == "false":
                b_val = False
            else:
                b_val = None
            profile.set_fact(
                con=con,
                actor=actor,
                project_id=project_id,
                fact_key=args.fact_key,
                value=b_val,
                reason=args.reason,
                interactive=interactive,
            )
            return 0

        if args.command == "applicability":
            project_id = _project_id(con)
            counts = profile.recompute(con, project_id)
            sys.stdout.write(
                f"true={counts.get('true', 0)} false={counts.get('false', 0)} unknown={counts.get('unknown', 0)}\n"
            )
            return 0

        if args.command == "run":
            actor = actors.resolve_actor(con, args.actor)
            if args.file == "-":
                raw = sys.stdin.read()
            else:
                try:
                    raw = Path(args.file).read_text(encoding="utf-8")
                except OSError as exc:
                    raise DocdbError("BAD_ARGS", f"파일을 읽을 수 없음: {args.file}") from exc
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise DocdbError("BAD_ARGS", "올바른 JSON이 아님") from exc
            if not isinstance(payload, dict):
                raise DocdbError("BAD_ARGS", "JSON 객체여야 함")
            run_id = runs.ingest(con, actor, payload)
            sys.stdout.write(f"OK r:{run_id}\n")
            return 0

        if args.command == "import":
            if args.import_command == "scan":
                actor = actors.resolve_actor(con, args.actor)
                result = importer.scan(
                    con, actor, args.folder, args.owner, args.export_policy
                )
                if result:
                    sys.stdout.write(result + "\n")
                    return int(any(line.startswith("ERR ") for line in result.splitlines()))
                return 0
            if args.import_command == "next":
                result = importer.next_batch(con, args.budget)
                sys.stdout.write(result + "\n")
                return 0
            if args.import_command == "label":
                raw = args.labels
                if raw.startswith("@"):
                    try:
                        raw = Path(raw[1:]).read_text(encoding="utf-8")
                    except OSError as exc:
                        raise DocdbError("BAD_ARGS", "label 파일을 읽을 수 없음") from exc
                try:
                    labels = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise DocdbError("BAD_ARGS", "label 입력이 올바른 JSON이 아님") from exc
                if not isinstance(labels, list):
                    raise DocdbError("BAD_ARGS", "label 입력은 JSON 배열이어야 함")
                result = importer.label(con, actors.resolve_actor(con, args.actor), labels)
                if result:
                    sys.stdout.write(result + "\n")
                    return 1 if any(line.startswith("ERR ") for line in result.splitlines()) else 0
                return 0
            result = importer.report(con)
            sys.stdout.write(result + "\n")
            return 0

        raise DocdbError("BAD_ARGS", "지원하지 않는 명령")
    finally:
        con.close()


def main(argv: list[str] | None = None, *, interactive: bool | None = None) -> int:
    """인자를 실행하고 오류를 계약 형식으로 stderr에 출력한다."""
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        args = _parse_args(argv)
        return _run(args, interactive)
    except SystemExit as exc:
        return int(exc.code or 0)
    except DocdbError as exc:
        sys.stderr.write(format_error(exc) + "\n")
        return exc.exit_code
