import threading
from pathlib import Path

import pytest

from docdb import db, history, migrate
from docdb.actors import (
    Actor,
    add_actor,
    require_ai,
    require_human,
    resolve_actor,
)
from docdb.errors import DocdbError
from docdb.ids import (
    ensure_not_reused,
    next_id,
    validate,
)


def test_t08_1_validate_doc() -> None:
    validate("doc", "DOC-REQ-ORDERS")
    for bad in ["DOC-", "doc-x", "DOC-A--B", "DOC-" + "A" * 61, "DOC-X\n", "DOC-X\r\n"]:
        with pytest.raises(DocdbError) as exc_info:
            validate("doc", bad)
        assert exc_info.value.code == "ID_FORMAT"


def test_t08_2_validate_item() -> None:
    validate("item", "REQ-HW-001")
    for bad in ["REQ", "req-1", "REQ-HW-001\n", "REQ-HW-001\r\n"]:
        with pytest.raises(DocdbError) as exc_info:
            validate("item", bad)
        assert exc_info.value.code == "ID_FORMAT"

    # work 등 숫자 포함 ID의 끝 줄바꿈 및 비ASCII 숫자 검증 (지적 1: validate("work", ...))
    for bad in ["WORK-2026-0001\n", "WORK-2026-0001\r\n", "WORK-٢٠٢٦-٠٠٠١"]:
        with pytest.raises(DocdbError) as exc_info:
            validate("work", bad)
        assert exc_info.value.code == "ID_FORMAT"


def test_t08_3_next_id_outside_tx(schema_con: object) -> None:
    with pytest.raises(DocdbError) as exc_info:
        next_id(schema_con, "run")
    assert exc_info.value.code == "DB"


def test_t08_4_next_id_empty_db_run(schema_con: object, frozen_now: None) -> None:
    with db.write_tx(schema_con):
        assert next_id(schema_con, "run") == "RUN-2026-0001"


def test_t08_5_next_id_run_existing(schema_con: object, frozen_now: None) -> None:
    with db.write_tx(schema_con):
        schema_con.execute(
            "INSERT INTO run (run_id, run_type, target_ref, runner, result, trust_source, recorded_at) "
            "VALUES (?, 'test', 'ref', 'runner', 'PASS', 'ci', '2026-10-13T00:00:00+00:00')",
            ("RUN-2026-0007",),
        )
        schema_con.execute(
            "INSERT INTO run (run_id, run_type, target_ref, runner, result, trust_source, recorded_at) "
            "VALUES (?, 'test', 'ref', 'runner', 'PASS', 'ci', '2026-10-13T00:00:00+00:00')",
            ("RUN-2025-0100",),
        )
        assert next_id(schema_con, "run") == "RUN-2026-0008"


def test_t08_6_next_id_run_rollover_9999(schema_con: object, frozen_now: None) -> None:
    with db.write_tx(schema_con):
        schema_con.execute(
            "INSERT INTO run (run_id, run_type, target_ref, runner, result, trust_source, recorded_at) "
            "VALUES (?, 'test', 'ref', 'runner', 'PASS', 'ci', '2026-10-13T00:00:00+00:00')",
            ("RUN-2026-9999",),
        )
        assert next_id(schema_con, "run") == "RUN-2026-10000"


def test_t08_7_next_id_work_custom_year(schema_con: object) -> None:
    with db.write_tx(schema_con):
        assert next_id(schema_con, "work", year=2030) == "WORK-2030-0001"


def test_t08_8_concurrent_next_id_threads(tmp_path: Path, frozen_now: None) -> None:
    path = tmp_path / "concurrent_id.sqlite"
    setup_con = db.connect(path, create=True)
    migrate.apply_pending(setup_con)
    setup_con.close()

    results: list[str] = []
    errors: list[BaseException] = []

    def run_worker() -> None:
        try:
            con = db.connect(path)
            try:
                with db.write_tx(con):
                    allocated = next_id(con, "run")
                    con.execute(
                        "INSERT INTO run (run_id, run_type, target_ref, runner, result, trust_source, recorded_at) "
                        "VALUES (?, 'test', 'ref', 'runner', 'PASS', 'ci', '2026-10-13T00:00:00+00:00')",
                        (allocated,),
                    )
                    results.append(allocated)
            finally:
                con.close()
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=run_worker)
    t2 = threading.Thread(target=run_worker)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert results[0] != results[1]
    assert set(results) == {"RUN-2026-0001", "RUN-2026-0002"}


def test_t08_9_ensure_not_reused_deleted(schema_con: object) -> None:
    with db.write_tx(schema_con):
        schema_con.execute(
            "INSERT INTO actor (actor_id, actor_type, name) VALUES ('ai-test', 'ai', '테스트')"
        )
        history.append_change(
            con=schema_con,
            entity_type="document",
            entity_id="DOC-X",
            op="delete",
            before={"title": "삭제 전"},
            after=None,
            actor_id="ai-test",
            work_id=None,
            reason="테스트 삭제",
        )

    with pytest.raises(DocdbError) as exc_info:
        ensure_not_reused(schema_con, "document", "DOC-X")
    assert exc_info.value.code == "ID_FORMAT"

    ensure_not_reused(schema_con, "document", "DOC-NOT-DELETED")


def test_t08_10_add_actor_validation(schema_con: object) -> None:
    add_actor(schema_con, "u-jjm", "human", "정지민", None)

    for bad_id, bad_type in [("U-JJM", "human"), ("ai-x", "human"), ("u-jjm\n", "human"), ("u-jjm\r\n", "human"), ("u-jjm٠", "human")]:
        with pytest.raises(DocdbError) as exc_info:
            add_actor(schema_con, bad_id, bad_type, "잘못된 행위자", None)
        assert exc_info.value.code == "ID_FORMAT"


def test_t08_11_add_actor_records_change_log(schema_con: object) -> None:
    add_actor(schema_con, "u-tester", "human", "테스터", None)

    row = schema_con.execute(
        "SELECT entity_type, entity_id, op, actor_id FROM change_log "
        "WHERE entity_type = 'actor' AND entity_id = 'u-tester'"
    ).fetchone()
    assert row is not None
    assert row[0] == "actor"
    assert row[1] == "u-tester"
    assert row[2] == "insert"
    assert row[3] == "u-tester"

    count = schema_con.execute(
        "SELECT count(*) FROM change_log WHERE entity_type = 'actor' AND entity_id = 'u-tester'"
    ).fetchone()[0]
    assert count == 1


def test_t08_12_resolve_actor(schema_con: object, monkeypatch: pytest.MonkeyPatch) -> None:
    add_actor(schema_con, "u-exp", "human", "명시 행위자", None)
    add_actor(schema_con, "u-env", "human", "환경 행위자", None)
    with db.write_tx(schema_con):
        schema_con.execute(
            "INSERT INTO actor (actor_id, actor_type, name, active) VALUES (?, ?, ?, 0)",
            ("u-inactive", "human", "비활성 행위자"),
        )

    # 1. explicit 우선
    monkeypatch.setenv("DOCDB_ACTOR", "u-env")
    resolved = resolve_actor(schema_con, "u-exp")
    assert resolved.id == "u-exp"
    assert resolved.type == "human"

    # 2. env만
    resolved = resolve_actor(schema_con, None)
    assert resolved.id == "u-env"
    assert resolved.type == "human"

    # 3. 둘 다 없음
    monkeypatch.delenv("DOCDB_ACTOR", raising=False)
    with pytest.raises(DocdbError) as exc_info:
        resolve_actor(schema_con, None)
    assert exc_info.value.code == "BAD_ARGS"

    # 4. 없는 ID
    with pytest.raises(DocdbError) as exc_info:
        resolve_actor(schema_con, "u-nonexistent")
    assert exc_info.value.code == "NOT_FOUND"

    # 5. active=0
    with pytest.raises(DocdbError) as exc_info:
        resolve_actor(schema_con, "u-inactive")
    assert exc_info.value.code == "NOT_FOUND"


def test_t08_13_require_human() -> None:
    # 1. ai
    with pytest.raises(DocdbError) as exc_info:
        require_human(Actor("ai-test", "ai"), interactive=True)
    assert exc_info.value.code == "NEEDS_HUMAN"

    # 2. human 비대화형
    with pytest.raises(DocdbError) as exc_info:
        require_human(Actor("u-test", "human"), interactive=False)
    assert exc_info.value.code == "NEEDS_HUMAN"

    # 3. human 대화형
    require_human(Actor("u-test", "human"), interactive=True)


def test_t08_14_require_ai() -> None:
    # 1. human
    with pytest.raises(DocdbError) as exc_info:
        require_ai(Actor("u-test", "human"))
    assert exc_info.value.code == "BAD_ARGS"

    # 2. ai
    require_ai(Actor("ai-test", "ai"))
