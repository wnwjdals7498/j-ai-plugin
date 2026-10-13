"""T11 CLI 초기화·백업 수용 테스트."""
import os
import subprocess
import sys
from pathlib import Path

from docdb import db, migrate


def _init_args(directory: Path, project: str = "t", owner: str = "u-tester") -> list[str]:
    """초기화 명령의 공통 인자를 만든다."""
    return [
        "init",
        "--project",
        project,
        "--name",
        "테스트",
        "--owner",
        owner,
        "--owner-name",
        "Tester",
        "--dir",
        str(directory),
    ]


def _make_v2_db(path: Path) -> None:
    """첫 두 마이그레이션만 적용한 프로젝트 DB를 만든다."""
    connection = db.connect(path, create=True)
    try:
        for version, schema_path in migrate.schema_files()[:2]:
            sql = schema_path.read_text(encoding="utf-8")
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + sql
                + f"\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        connection.execute(
            "INSERT INTO cat_version "
            "(catalogue_version, schema_version, source_digest, loaded_at) "
            "VALUES ('legacy', '1', ?, '2026-10-13T00:00:00+00:00')",
            ("sha256:" + "0" * 64,),
        )
        connection.execute(
            "INSERT INTO project (project_id, name, catalogue_version) "
            "VALUES ('t', '테스트', 'legacy')"
        )
    finally:
        connection.close()


def test_t11_1_init_creates_project_database_and_seed_data(tmp_path, run_cli):
    code, out, err = run_cli(_init_args(tmp_path))
    db_path = tmp_path / ".docdb" / "docdb.sqlite"
    assert code == 0
    assert err == ""
    assert out.strip() == f"OK init project=t db={db_path}"
    assert db_path.is_file()
    assert (db_path.parent / ".gitignore").read_text(encoding="utf-8") == "*\n"

    connection = db.connect(db_path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrate.latest_version()
        assert connection.execute("SELECT count(*) FROM actor").fetchone()[0] == 4
        assert connection.execute("SELECT project_id FROM project").fetchone()[0] == "t"
        assert connection.execute("SELECT count(*) FROM cat_kind").fetchone()[0] == 18
    finally:
        connection.close()


def test_t11_2_init_existing_project_reports_db_exists(tmp_path, run_cli):
    assert run_cli(_init_args(tmp_path))[0] == 0
    code, out, err = run_cli(_init_args(tmp_path))
    assert code == 3
    assert out == ""
    assert err.startswith("ERROR DB_EXISTS ")


def test_t11_3_init_rejects_invalid_project_slug(tmp_path, run_cli):
    code, out, err = run_cli(_init_args(tmp_path, project="T_1"))
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR BAD_ARGS ")


def test_t11_4_init_rejects_nonhuman_owner(tmp_path, run_cli):
    code, out, err = run_cli(_init_args(tmp_path, owner="ai-x"))
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR ID_FORMAT ")


def test_t11_5_backup_uses_timestamp_and_unique_suffix(
    project_db, frozen_now, run_cli, tmp_path
):
    home = tmp_path / "home"
    args = ["--db", str(project_db), "backup"]
    assert run_cli(args, env={"DOCDB_HOME": str(home)})[0] == 0
    assert run_cli(args, env={"DOCDB_HOME": str(home)})[0] == 0
    backup_dir = home / "backups" / "t"
    assert (backup_dir / "20261013T000000Z-manual.sqlite").is_file()
    assert (backup_dir / "20261013T000000Z-manual-2.sqlite").is_file()


def test_t11_6_backup_preserves_schema_version(
    project_db, frozen_now, run_cli, tmp_path
):
    home = tmp_path / "home"
    code, _out, err = run_cli(
        ["--db", str(project_db), "backup"], env={"DOCDB_HOME": str(home)}
    )
    assert code == 0, err
    backup_file = home / "backups" / "t" / "20261013T000000Z-manual.sqlite"
    source = db.connect(project_db)
    copied = db.connect(backup_file)
    try:
        assert copied.execute("PRAGMA user_version").fetchone()[0] == source.execute(
            "PRAGMA user_version"
        ).fetchone()[0]
    finally:
        copied.close()
        source.close()


def test_t11_7_restore_requires_human_and_creates_pre_restore_backup(
    project_db, frozen_now, run_cli, tmp_path
):
    home = tmp_path / "home"
    env = {"DOCDB_HOME": str(home)}
    assert run_cli(["--db", str(project_db), "backup"], env=env)[0] == 0
    backup_file = home / "backups" / "t" / "20261013T000000Z-manual.sqlite"
    connection = db.connect(project_db)
    try:
        with db.write_tx(connection):
            connection.execute("UPDATE project SET name = '변경됨' WHERE project_id = 't'")
    finally:
        connection.close()

    args = ["--db", str(project_db), "--actor", "u-tester", "restore", str(backup_file)]
    code, out, err = run_cli(args, interactive=False, env=env)
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR NEEDS_HUMAN ")

    code, _out, err = run_cli(args, interactive=True, env=env)
    assert code == 0, err
    restored = db.connect(project_db)
    try:
        assert restored.execute("SELECT name FROM project WHERE project_id = 't'").fetchone()[0] == "테스트"
    finally:
        restored.close()
    assert list((home / "backups" / "t").glob("*-pre-restore*.sqlite"))


def test_t11_8_migrate_applies_pending_versions_after_backup(tmp_path, run_cli):
    old_db = tmp_path / ".docdb" / "docdb.sqlite"
    old_db.parent.mkdir()
    _make_v2_db(old_db)
    home = tmp_path / "home"
    code, _out, err = run_cli(
        ["--db", str(old_db), "migrate"], env={"DOCDB_HOME": str(home)}
    )
    assert code == 0, err

    connection = db.connect(old_db)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == migrate.latest_version()
    finally:
        connection.close()
    assert list((home / "backups" / "t").glob("*-pre-migrate.sqlite"))


def test_t11_9_actor_list_detects_required_migration(tmp_path, run_cli):
    old_db = tmp_path / "old.sqlite"
    _make_v2_db(old_db)
    code, out, err = run_cli(["--db", str(old_db), "actor", "list"])
    assert code == 3
    assert out == ""
    assert err.startswith("ERROR MIGRATION_REQUIRED ")


def test_t11_10_actor_add_requires_human_and_lists_added_actor(
    project_db, run_cli, monkeypatch, capsys
):
    add_args = [
        "--db",
        str(project_db),
        "--actor",
        "u-tester",
        "actor",
        "add",
        "u-new",
        "--type",
        "human",
        "--name",
        "New",
    ]
    code, out, err = run_cli(add_args, interactive=False)
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR NEEDS_HUMAN ")

    code, _out, err = run_cli(add_args, interactive=True)
    assert code == 0, err
    code, out, err = run_cli(["--db", str(project_db), "actor", "list"])
    assert code == 0, err
    assert "u-new" in out

    # 주입 없이 main을 호출: stdin만 TTY면 사람 전용 명령은 거부된다.
    from docdb.cli import main

    other = [*add_args[:-5], "u-c", "--type", "human", "--name", "C"]
    for name in ("DOCDB_HOME", "DOCDB_PATH", "DOCDB_START_DIR", "DOCDB_ACTOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    capsys.readouterr()
    assert main(other) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("ERROR NEEDS_HUMAN ")
    code, out, err = run_cli(["--db", str(project_db), "actor", "list"])
    assert code == 0, err
    assert "u-c" not in out

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    capsys.readouterr()
    assert main(other) == 0
    capsys.readouterr()
    code, out, err = run_cli(["--db", str(project_db), "actor", "list"])
    assert code == 0, err
    assert "u-c" in out


def test_t11_11_verify_history_reports_clean_chain(project_db, run_cli):
    code, out, err = run_cli(["--db", str(project_db), "verify-history"])
    assert code == 0
    assert out.strip() == "OK history"
    assert err == ""


def test_t11_12_python_module_help_succeeds(plugin_root):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(plugin_root / "src")
    result = subprocess.run(
        [sys.executable, "-m", "docdb", "--help"],
        cwd=plugin_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_t11_13_cli_errors_write_one_stderr_line_only(tmp_path, run_cli):
    code, out, err = run_cli(_init_args(tmp_path, project="T_1"))
    assert code == 1
    assert out == ""
    assert len(err.splitlines()) == 1
    assert err.startswith("ERROR BAD_ARGS ")


def test_t11_14_global_db_option_overrides_working_directory(
    project_db, run_cli, tmp_path, monkeypatch
):
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    code, out, err = run_cli(["--db", str(project_db), "actor", "list"])
    assert code == 0, err
    assert "u-tester" in out


def test_t11_15_catalogue_load_requires_human_and_keeps_row_count(
    project_db, starter_paths, run_cli
):
    connection = db.connect(project_db)
    try:
        before = connection.execute("SELECT count(*) FROM cat_kind").fetchone()[0]
    finally:
        connection.close()

    args = [
        "--db",
        str(project_db),
        "--actor",
        "u-tester",
        "catalogue",
        "load",
        str(starter_paths[0]),
    ]
    code, out, err = run_cli(args, interactive=False)
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR NEEDS_HUMAN ")

    code, _out, err = run_cli(args, interactive=True)
    assert code == 0, err
    connection = db.connect(project_db)
    try:
        assert connection.execute("SELECT count(*) FROM cat_kind").fetchone()[0] == before
    finally:
        connection.close()


def test_t11_16_policy_allow_secret_requires_human_and_issues_id(
    project_db, frozen_now, run_cli
):
    args = [
        "--db",
        str(project_db),
        "--actor",
        "u-tester",
        "policy",
        "allow-secret",
        "f:DOC-REQ-HW/D01/overview",
        "--reason",
        "r",
        "--days",
        "7",
    ]
    code, out, err = run_cli(args, interactive=False)
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR NEEDS_HUMAN ")

    code, out, err = run_cli(args, interactive=True)
    assert code == 0, err
    assert out.strip() == "OK PEX-2026-0001"
