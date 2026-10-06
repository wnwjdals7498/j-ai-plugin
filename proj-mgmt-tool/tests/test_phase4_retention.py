"""Actual project cleanup respects current metadata/evidence and unknown effects."""
from pmt.continuity.storage import ContinuityStore
from pmt.db import Database
from pmt.service import execute
from pmt.util import new_id


def _setup(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    base = {"request_id": new_id(), "protocol_version": 1, "actor": "main", "session_id": "session"}
    result, code = execute(db, base | {"operation": "create_scope", "payload": {"kind": "project", "slug": "p"}})
    assert code == 0
    return db, base | {"request_id": new_id(), "scope_id": result["result"]["scope_id"], "operation": "prune_continuity", "payload": {}}


def test_old_unreferenced_metadata_pruned_and_current_chain_preserved(tmp_path):
    db, req = _setup(tmp_path)
    store = ContinuityStore(db)
    with db.write() as conn:
        dependency = store.put(conn, req, "basis", {"source_ref": "known"})
        history = store.put(conn, req, "checkpoint", {"old": True})
        orphan = store.put(conn, req, "overview", {"unused": True})
        foreign = store.put(conn, req | {"actor": "another"}, "detail", {"unused": True}, visibility="private")
        current = store.put(conn, req, "checkpoint", {"basis_ref": dependency["id"], "parent_checkpoint_ref": history["id"]})
        store.advance_pointer(conn, req, {"purpose": "current"}, current["id"], 0)
        conn.execute("UPDATE continuity_objects SET created_at='2020-01-01T00:00:00Z'")
    dry, code = execute(db, req)
    assert code == 0 and dry["result"]["candidate_count"] == 2
    applied, code = execute(db, req | {"request_id": new_id(), "payload": {"dry_run": False}})
    assert code == 0 and applied["result"]["removed_count"] == 2
    with db.connect() as conn:
        remaining = {row[0] for row in conn.execute("SELECT id FROM continuity_objects")}
        assert remaining == {dependency["id"], foreign["id"], current["id"]}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unresolved_effect_keeps_its_old_source_reference(tmp_path):
    db, req = _setup(tmp_path)
    store = ContinuityStore(db)
    with db.write() as conn:
        basis = store.put(conn, req, "basis", {"source_ref": "old"})
        orphan = store.put(conn, req, "overview", {"unused": True})
        store.begin_effect(conn, req, "alignment", {"basis_ref": basis["id"]})
        conn.execute("UPDATE continuity_objects SET created_at='2020-01-01T00:00:00Z'")
    applied, code = execute(db, req | {"request_id": new_id(), "payload": {"dry_run": False}})
    assert code == 0 and applied["result"]["removed_count"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT id FROM continuity_objects WHERE id=?", (basis["id"],)).fetchone()
        assert not conn.execute("SELECT id FROM continuity_objects WHERE id=?", (orphan["id"],)).fetchone()
        assert conn.execute("SELECT state FROM continuity_journal").fetchone()[0] == "prepared"


def test_corrupt_reference_metadata_blocks_cleanup_without_removing_objects(tmp_path):
    db, req = _setup(tmp_path)
    with db.write() as conn:
        basis = ContinuityStore(db).put(conn, req, "basis", {"retained_ref": "known"})
        conn.execute("UPDATE continuity_objects SET body_json='{}',created_at='2020-01-01T00:00:00Z' WHERE id=?",
                     (basis["id"],))
    result, code = execute(db, req | {"payload": {"dry_run": False}})
    assert code == 5 and result["error"]["code"] == "continuity_corrupt"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM continuity_objects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM requests WHERE request_id=?", (req["request_id"],)).fetchone()[0] == 0
