import re

import pytest

from docdb import db, history
from docdb.errors import DocdbError
from docdb.textnorm import canonical_json


def _insert_refs(con: object, *, include_work: bool = False) -> None:
    with db.write_tx(con):
        con.execute(
            "INSERT INTO actor (actor_id, actor_type, name) VALUES (?, ?, ?)",
            ("ai-test", "ai", "테스트 에이전트"),
        )
        if include_work:
            con.execute(
                "INSERT INTO cat_version "
                "(catalogue_version, schema_version, source_digest, loaded_at) "
                "VALUES (?, ?, ?, ?)",
                ("test", "1", "digest", "2026-10-13T00:00:00+00:00"),
            )
            con.execute(
                "INSERT INTO project (project_id, name, catalogue_version) VALUES (?, ?, ?)",
                ("PRJ-test", "테스트", "test"),
            )
            con.execute(
                "INSERT INTO work "
                "(work_id, project_id, work_type, title, owner_id, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "WORK-2026-0001",
                    "PRJ-test",
                    "feature",
                    "테스트 작업",
                    "ai-test",
                    "2026-10-13T00:00:00+00:00",
                ),
            )


def _append(con: object, entity_id: str, *, after: dict | None = None) -> int:
    with db.write_tx(con):
        return history.append_change(
            con,
            "document",
            entity_id,
            "insert",
            None,
            after or {"title": "제목"},
            "ai-test",
            None,
            None,
        )


def test_t07_1_append_requires_write_tx(schema_con: object) -> None:
    with pytest.raises(DocdbError) as exc_info:
        history.append_change(
            schema_con, "document", "DOC-A", "insert", None, {}, "ai-test", None, None
        )

    assert exc_info.value.code == "DB"
    assert exc_info.value.detail == "append_change는 write_tx 안에서만"


def test_t07_2_first_change_starts_chain(schema_con: object) -> None:
    _insert_refs(schema_con)
    _append(schema_con, "DOC-A")
    row = schema_con.execute(
        "SELECT seq, prev_change_id, row_digest FROM change_log"
    ).fetchone()

    assert row[0] == 1
    assert row[1] is None
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", row[2])


def test_t07_3_second_change_links_previous(schema_con: object) -> None:
    _insert_refs(schema_con)
    first_id = _append(schema_con, "DOC-A")
    first = schema_con.execute(
        "SELECT row_digest FROM change_log WHERE change_id = ?", (first_id,)
    ).fetchone()
    _append(schema_con, "DOC-A", after={"title": "수정"})
    second = schema_con.execute(
        "SELECT seq, prev_change_id, prev_digest FROM change_log "
        "WHERE entity_type = ? AND entity_id = ? ORDER BY seq DESC LIMIT 1",
        ("document", "DOC-A"),
    ).fetchone()

    assert second[0] == 2
    assert second[1] == first_id
    assert second[2] == first[0]


def test_t07_4_each_entity_has_own_sequence(schema_con: object) -> None:
    _insert_refs(schema_con)
    _append(schema_con, "DOC-A")
    _append(schema_con, "DOC-A", after={"title": "수정"})
    _append(schema_con, "DOC-B")
    rows = schema_con.execute(
        "SELECT entity_id, seq FROM change_log ORDER BY entity_id, seq"
    ).fetchall()

    assert [(row[0], row[1]) for row in rows] == [
        ("DOC-A", 1),
        ("DOC-A", 2),
        ("DOC-B", 1),
    ]


def test_t07_5_row_digest_matches_saved_value(schema_con: object) -> None:
    _insert_refs(schema_con)
    _append(schema_con, "DOC-A", after={"title": "제목"})
    row = schema_con.execute(
        "SELECT entity_type, entity_id, seq, op, prev_digest, row_digest "
        "FROM change_log"
    ).fetchone()

    assert history.row_digest(
        row[0],
        row[1],
        row[2],
        row[3],
        {"title": "제목"},
        row[4],
    ) == row[5]


def test_t07_6_after_json_normalizes_newlines(schema_con: object) -> None:
    _insert_refs(schema_con)
    with db.write_tx(schema_con):
        history.append_change(
            schema_con,
            "document",
            "DOC-A",
            "insert",
            None,
            {"value_text": "a\r\nb"},
            "ai-test",
            None,
            None,
        )
    row = schema_con.execute("SELECT after_json FROM change_log").fetchone()

    assert row[0] == canonical_json({"value_text": "a\nb"})


def test_t07_7_work_and_reason_are_saved(schema_con: object) -> None:
    _insert_refs(schema_con, include_work=True)
    with db.write_tx(schema_con):
        history.append_change(
            schema_con,
            "document",
            "DOC-A",
            "insert",
            None,
            {"title": "제목"},
            "ai-test",
            "WORK-2026-0001",
            "요구에 따라 기록",
        )
    row = schema_con.execute("SELECT work_id, reason FROM change_log").fetchone()

    assert row[0] == "WORK-2026-0001"
    assert row[1] == "요구에 따라 기록"


def test_t07_8_valid_chain_has_no_errors(schema_con: object) -> None:
    _insert_refs(schema_con)
    _append(schema_con, "DOC-A")
    _append(schema_con, "DOC-A", after={"title": "수정"})

    assert history.verify_chain(schema_con) == []


def test_t07_9_tampered_after_json_is_reported(schema_con: object) -> None:
    _insert_refs(schema_con)
    change_id = _append(schema_con, "DOC-A")
    schema_con.execute("DROP TRIGGER change_log_no_update")
    schema_con.execute(
        "UPDATE change_log SET after_json = ? WHERE change_id = ?",
        ('{"title":"변조"}', change_id),
    )

    assert history.verify_chain(schema_con) == ["document|DOC-A seq=1 digest mismatch"]


def test_t07_10_recorded_tables_match_contract() -> None:
    assert history.RECORDED_TABLES == frozenset(
        {
            "actor",
            "project",
            "fact_value",
            "document",
            "document_kind",
            "field_value",
            "item",
            "relation",
            "artifact",
            "work",
            "work_event",
            "run",
            "finding",
            "handoff",
            "approval",
            "policy_exception",
            "link_exemption",
        }
    )
