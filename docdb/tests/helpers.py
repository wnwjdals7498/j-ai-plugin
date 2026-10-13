"""테스트용 최소 카탈로그와 프로젝트 자료를 채운다."""
from docdb.db import Connection


def seed_min(con: Connection) -> None:
    """스키마 동작 테스트가 재사용할 최소 카탈로그와 행위자를 넣는다."""
    con.executemany(
        "INSERT INTO cat_version VALUES (?, ?, ?, ?)",
        [("test-1", "1", "digest", "2026-10-13T00:00:00Z")],
    )
    con.executemany(
        "INSERT INTO cat_group VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("D", "docs", "문서", "Documents", "owner", None),
            ("L", "tests", "검증", "Tests", "owner", None),
        ],
    )
    con.executemany(
        "INSERT INTO cat_kind VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "D01", "D01", "living narrative", "living narrative", "D", "living",
                "mandatory", "narrative", "owner", None, None, "{}", "{}", "[]",
            ),
            (
                "L02", "L02", "living narrative", "living narrative", "L", "living",
                "mandatory", "narrative", "owner", None, None, "{}", "{}", "[]",
            ),
        ],
    )
    con.executemany(
        "INSERT INTO cat_kind_field (kind_id, field_key, is_required, description, sort_order) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("D01", "behavior", 1, "동작", 1),
            ("L02", "test_ids", 1, "검증 항목", 1),
        ],
    )
    con.executemany(
        "INSERT INTO cat_item_type (item_type, id_prefix, defined_by_kind, description) "
        "VALUES (?, ?, ?, ?)",
        [
            ("requirement", "REQ-", "D01", "요구사항"),
            ("test_case", "TC-", "L02", "시험 사례"),
        ],
    )
    con.execute(
        "INSERT INTO cat_kind_link (kind_id, target_kind_id, is_required) VALUES (?, ?, ?)",
        ("D01", "L02", 1),
    )
    con.execute(
        "INSERT INTO project (project_id, name, catalogue_version) VALUES (?, ?, ?)",
        ("t", "test", "test-1"),
    )
    con.executemany(
        "INSERT INTO actor (actor_id, actor_type, name) VALUES (?, ?, ?)",
        [("u-tester", "human", "Tester"), ("ai-claude", "ai", "Claude")],
    )
