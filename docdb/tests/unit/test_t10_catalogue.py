"""T10 카탈로그 적재 수용 테스트."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from docdb.actors import Actor
from docdb.catalogue import load, storage_class
from docdb.errors import DocdbError


TABLE_FIELDS = {
    "A04": {"term_id"},
    "B03": {"roles", "accountable"},
    "B04": {"items"},
    "D01": {"requirement_ids", "behavior", "acceptance", "scope", "owners"},
    "D08": {"edges"},
    "F07": {"debt_id"},
    "K01": {"environment_ids"},
    "K02": {"resources", "topology", "owners"},
    "L02": {"test_ids"},
    "L03": {"execution"},
    "L07": {"defects"},
    "P06": {"steps", "rollback", "owners"},
    "R04": {"verification"},
    "S03": {"assets"},
}


EXPECTED_LINKS = {
    ("D01", "L02", 1),
    ("L02", "D01", 1),
    ("L03", "L02", 1),
    ("P06", "K02", 1),
    ("K07", "K02", 1),
    ("R04", "K02", 1),
    ("K02", "K01", 0),
    ("K02", "S03", 0),
    ("B04", "D01", 0),
    ("L07", "L02", 0),
    ("F07", "D01", 0),
}


def _actor(con) -> Actor:
    con.execute(
        "INSERT INTO actor (actor_id, actor_type, name) VALUES (?, ?, ?)",
        ("u-tester", "human", "Tester"),
    )
    return Actor("u-tester", "human")


def _load(con, paths, actor=None):
    return load(con, paths[0], paths[1], actor)


def test_t10_1_starter_loads_eighteen_kinds(schema_con, starter_paths):
    result = _load(schema_con, starter_paths, _actor(schema_con))
    assert result["kinds"] == 18
    assert schema_con.execute("SELECT count(*) FROM cat_kind").fetchone()[0] == 18


def test_t10_2_kind_storage_classes_match_catalogue_table(schema_con, starter_paths):
    data = json.loads(starter_paths[0].read_text(encoding="utf-8"))
    expected = {
        "A01": "view", "D08": "view", "F06": "view",
        "A04": "register", "B04": "register", "F07": "register",
        "L07": "register", "S03": "register",
        "K07": "run", "L03": "run", "R04": "run",
    }
    for kind in data["document_types"]:
        assert storage_class(kind) == expected.get(kind["kind_id"], "narrative")
    _load(schema_con, starter_paths, _actor(schema_con))
    stored = dict(schema_con.execute("SELECT kind_id, storage_class FROM cat_kind"))
    assert stored == {kind: storage_class(row) for row in data["document_types"] for kind in [row["kind_id"]]}


def test_t10_3_item_types_load_eleven_and_skip_twenty_eight(schema_con, starter_paths):
    result = _load(schema_con, starter_paths, _actor(schema_con))
    assert result["item_types"] == 11
    assert result["item_types_skipped"] == 28


def test_t10_4_defined_by_field_uses_first_source_field(schema_con, starter_paths):
    _load(schema_con, starter_paths, _actor(schema_con))
    rows = dict(
        schema_con.execute(
            "SELECT item_type, defined_by_field FROM cat_item_type"
        )
    )
    assert rows["requirement"] == "requirement_ids"
    assert rows["acceptance_criterion"] == "acceptance"
    assert rows["test_case"] == "test_ids"
    assert rows["infra_resource"] == "resources"
    all_types = {
        row["item_type"]: row["defined_by_field"]
        for row in json.loads(starter_paths[1].read_text(encoding="utf-8"))
    }
    assert all_types["known_error"] is None


def test_t10_5_item_type_seed_matches_all_thirty_nine_source_rows(schema_con, starter_paths):
    _load(schema_con, starter_paths, _actor(schema_con))
    root = starter_paths[0].parents[2]
    sql = (root / "docs-db" / "reference" / "docdb-package" / "docdb_seed_item_types.sql").read_text(encoding="utf-8")
    expected = re.findall(
        r"\('([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'\)", sql
    )
    data = json.loads(starter_paths[1].read_text(encoding="utf-8"))
    actual = [
        (row["item_type"], row["id_prefix"], row["defined_by_kind"], row["description"])
        for row in data
    ]
    assert len(actual) == 39
    assert actual == expected


def test_t10_6_kind_links_match_six_required_and_five_optional(schema_con, starter_paths):
    result = _load(schema_con, starter_paths, _actor(schema_con))
    actual = set(
        schema_con.execute(
            "SELECT kind_id, target_kind_id, is_required FROM cat_kind_link"
        )
    )
    assert result["links"] == 11
    assert actual == EXPECTED_LINKS


def test_t10_7_test_ids_extract_relation_matches_contract(schema_con, starter_paths):
    _load(schema_con, starter_paths, _actor(schema_con))
    extract_rel = schema_con.execute(
        "SELECT extract_rel FROM cat_kind_field WHERE kind_id = ? AND field_key = ?",
        ("L02", "test_ids"),
    ).fetchone()[0]
    assert json.loads(extract_rel) == {
        "rel": "verified_by", "from_prefix": "REQ-", "to_prefix": "TC-"
    }


def test_t10_8_added_field_descriptions_mark_design_judgment(starter_paths):
    data = json.loads(starter_paths[0].read_text(encoding="utf-8"))
    for kind in data["document_types"]:
        known = TABLE_FIELDS.get(kind["kind_id"], set())
        for fields in (kind["required_fields"], kind["optional_fields"]):
            for key, value in fields.items():
                description = value["description"] if isinstance(value, dict) else value
                if key not in known:
                    assert "[설계 판단]" in description


def test_t10_9_catalogue_has_only_cap_production_fact(schema_con, starter_paths):
    _load(schema_con, starter_paths, _actor(schema_con))
    assert schema_con.execute("SELECT fact_key FROM cat_fact").fetchall() == [
        ("cap.production",)
    ]


def test_t10_10_mini_catalogue_loads_forty_five_kinds(schema_con, mini_paths):
    result = _load(schema_con, mini_paths, _actor(schema_con))
    assert result["kinds"] == 45
    assert schema_con.execute("SELECT count(*) FROM cat_kind").fetchone()[0] == 45


def test_t10_11_reloading_same_catalogue_keeps_row_counts(schema_con, mini_paths):
    actor = _actor(schema_con)
    _load(schema_con, mini_paths, actor)
    tables = (
        "cat_version", "cat_group", "cat_fact", "cat_kind", "cat_kind_field",
        "cat_kind_link", "cat_kind_alias", "cat_item_type",
    )
    before = {
        table: schema_con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in tables
    }
    _load(schema_con, mini_paths, actor)
    after = {
        table: schema_con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in tables
    }
    assert after == before


def test_t10_12_missing_kind_key_is_bad_args(schema_con, starter_paths, tmp_path):
    actor = _actor(schema_con)
    _load(schema_con, starter_paths, actor)
    before = schema_con.execute("SELECT count(*) FROM cat_kind").fetchone()[0]
    changed = json.loads(starter_paths[0].read_text(encoding="utf-8"))
    kind = next(kind for kind in changed["document_types"] if kind["kind_id"] == "K07")
    del kind["lifecycle"]
    altered_path = tmp_path / "missing_lifecycle.json"
    altered_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    try:
        with pytest.raises(DocdbError) as exc:
            load(schema_con, altered_path, starter_paths[1], actor)
        assert exc.value.code == "BAD_ARGS"
        assert exc.value.detail == "종류 K07 필수 키 누락: lifecycle"
        assert isinstance(exc.value.__cause__, KeyError)
        assert schema_con.execute("SELECT count(*) FROM cat_kind").fetchone()[0] == before
    finally:
        altered_path.unlink(missing_ok=True)


def test_t10_12_removing_kind_used_by_document_is_incompatible(schema_con, starter_paths, tmp_path):
    actor = _actor(schema_con)
    _load(schema_con, starter_paths, actor)
    schema_con.execute(
        "INSERT INTO project (project_id, name, catalogue_version) VALUES (?, ?, ?)",
        ("t", "테스트", "starter-1"),
    )
    schema_con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("DOC-T10", "t", "문서", "living", "D", "normative", "desired", "u-tester", "now", "now"),
    )
    schema_con.execute("INSERT INTO document_kind (doc_id, kind_id) VALUES (?, ?)", ("DOC-T10", "D01"))
    changed = json.loads(starter_paths[0].read_text(encoding="utf-8"))
    changed["document_types"] = [kind for kind in changed["document_types"] if kind["kind_id"] != "D01"]
    altered_path = tmp_path / "missing_kind.json"
    altered_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    try:
        with pytest.raises(DocdbError) as exc:
            load(schema_con, altered_path, starter_paths[1], actor)
        assert exc.value.code == "CATALOGUE_INCOMPATIBLE"
    finally:
        altered_path.unlink(missing_ok=True)


def test_t10_13_primary_alias_selects_g01(schema_con, starter_paths, tmp_path):
    actor = _actor(schema_con)
    data = json.loads(starter_paths[0].read_text(encoding="utf-8"))
    next(kind for kind in data["document_types"] if kind["kind_id"] == "K01")["covered_names"].append("작업 환경")
    altered_path = tmp_path / "duplicate_alias.json"
    altered_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        load(schema_con, altered_path, starter_paths[1], actor)
        rows = dict(
            schema_con.execute(
                "SELECT kind_id, is_primary FROM cat_kind_alias WHERE name = ?",
                ("작업 환경",),
            )
        )
        assert rows == {"G01": 1, "K01": 0}
    finally:
        altered_path.unlink(missing_ok=True)


def test_t10_14_catalogue_digest_and_history_match_source(schema_con, starter_paths):
    actor = _actor(schema_con)
    result = _load(schema_con, starter_paths, actor)
    expected_digest = "sha256:" + hashlib.sha256(starter_paths[0].read_bytes()).hexdigest()
    stored = schema_con.execute(
        "SELECT source_digest FROM cat_version WHERE catalogue_version = ?",
        (result["catalogue_version"],),
    ).fetchone()[0]
    history_count = schema_con.execute(
        "SELECT count(*) FROM change_log WHERE entity_type = 'catalogue'"
    ).fetchone()[0]
    assert stored == expected_digest
    assert history_count == 1
