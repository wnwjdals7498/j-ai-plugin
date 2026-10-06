from __future__ import annotations

import pytest

from pmt.continuity.changes_core import (
    build_link_index,
    extract_python_symbols,
    normalize_repo_path,
    parse_name_status_z,
)
from pmt.errors import PmtError


def test_name_status_parser_preserves_rename_and_non_ascii_paths():
    raw = "R100\0src/old.py\0src/새\nname.py\0M\0docs/plan.md\0D\0src/gone.py\0".encode()
    assert parse_name_status_z(raw) == [
        {"kind": "renamed", "before_path": "src/old.py", "path": "src/새\nname.py", "similarity": 100},
        {"kind": "modified", "path": "docs/plan.md"},
        {"kind": "deleted", "path": "src/gone.py"},
    ]


def test_name_status_parser_rejects_truncated_git_output():
    with pytest.raises(PmtError, match="incomplete"):
        parse_name_status_z(b"R100\0src/a.py\0")


@pytest.mark.parametrize("path", ["../secret.py", "/etc/passwd", "a/../b.py", "C:/secret.py", "a\\b.py"])
def test_source_path_must_stay_repository_relative(path):
    with pytest.raises(PmtError):
        normalize_repo_path(path)


def test_python_extractor_reports_declared_symbols_and_dynamic_unknowns():
    parsed = extract_python_symbols("src/work.py", b"def build():\n    return 1\n\nclass Store:\n    pass\n")
    assert parsed["status"] == "parsed"
    assert [item["qualified_name"] for item in parsed["symbols"]] == ["src/work.py::build", "src/work.py::Store"]
    dynamic = extract_python_symbols("src/dynamic.py", b"def load(name):\n    return globals()[name]\n\nvalue = getattr(obj, 'x')\n")
    assert dynamic["status"] == "partial"
    assert dynamic["reason_code"] == "dynamic_or_external_binding"


def test_unsupported_and_unparseable_sources_remain_unknown():
    assert extract_python_symbols("src/app.rs", b"fn main() {}")["reason_code"] == "unsupported_language"
    assert extract_python_symbols("src/broken.py", b"def broken(:")["reason_code"] == "python_parse_failed"


def test_graph_ref_is_candidate_and_only_reviewed_mapping_is_verified():
    graph = {"nodes": [
        {"id": "node-1", "file_refs": ["src/"]},
        {"id": "node-2", "file_refs": []},
    ]}
    index = build_link_index(
        source_entries=[
            {"path": "src/app.py", "content": b"def start(): pass\n"},
            {"path": "tests/test_app.py", "content": b"def test_app(): pass\n"},
        ],
        graph=graph,
        scope_hash="a" * 64,
        explicit_mappings=[
            {"path": "tests/test_app.py", "node_id": "node-2", "decision_ref": "decision:1",
             "verified_mapping_ref": "decision:1"},
        ],
    )
    assert index["coverage"]["mapped_count"] == 2
    assert index["coverage"]["unmapped_count"] == 0
    assert index["links"][0]["link_state"] == "extracted_candidate"
    assert index["links"][1]["link_state"] == "verified_mapping"
    assert index["source_inventory_hash"]


def test_caller_review_boolean_without_decision_ref_does_not_verify_mapping():
    index = build_link_index(
        source_entries=[{"path": "src/app.py", "content": b"def start(): pass\n"}],
        graph={"nodes": [{"id": "node-1", "file_refs": []}]},
        scope_hash="b" * 64,
        explicit_mappings=[{"path": "src/app.py", "node_id": "node-1", "reviewed_by": "main"}],
    )
    assert index["coverage"]["unmapped_count"] == 1
    assert index["links"][0]["link_state"] == "review_required"


def test_index_hash_covers_source_scope_and_parser_coverage():
    args = {"source_entries": [{"path": "src/app.py", "content": b"def start(): pass\n"}],
            "graph": {"nodes": []}, "scope_hash": "c" * 64}
    first = build_link_index(**args)
    second = build_link_index(**(args | {"scope_hash": "d" * 64}))
    assert first["index_hash"] != second["index_hash"]
    assert first["coverage"]["unmapped_count"] == 1
