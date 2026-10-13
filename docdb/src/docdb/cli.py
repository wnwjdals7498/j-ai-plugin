"""docctl 명령줄 인터페이스(C-24)."""
import argparse
import contextlib
import io
import re
import sys
from pathlib import Path

from docdb import actors, backup, catalogue, db, history, migrate, paths, policy
from docdb.actors import Actor
from docdb.errors import DocdbError, format_error

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


def _init(args: argparse.Namespace) -> int:
    """새 프로젝트 DB, 카탈로그, 기본 행위자를 만든다."""
    if not _PROJECT_ID.fullmatch(args.project):
        raise DocdbError("BAD_ARGS", "프로젝트 ID 형식이 올바르지 않음")
    if not re.fullmatch(actors.ACTOR_PATTERNS["human"], args.owner, re.ASCII):
        raise DocdbError("ID_FORMAT", f"사람 행위자 ID 형식 위반: {args.owner}")

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
            con.execute(
                "INSERT INTO project (project_id, name, catalogue_version) VALUES (?, ?, ?)",
                (args.project, args.name, version),
            )
            history.append_change(
                con=con,
                entity_type="project",
                entity_id=args.project,
                op="insert",
                before=None,
                after={
                    "project_id": args.project,
                    "name": args.name,
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
