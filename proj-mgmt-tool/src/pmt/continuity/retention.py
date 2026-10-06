"""Conservative project metadata cleanup; current references and effects survive."""
import json
import re
from datetime import datetime, timedelta, timezone

from ..errors import PmtError
from .contracts import authorize, digest

READ_OPERATIONS = set()
WRITE_OPERATIONS = {"prune_continuity"}
FILE_OPERATIONS = set()
_IDS = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_CLOSED = {"Done", "Canceled", "Cancelled", "Rejected", "Discarded", "Obsolete"}


def _body(value, expected_hash=None):
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise PmtError("continuity_corrupt", "Cleanup reference metadata is corrupt", 5) from exc
    if not isinstance(result, dict) or (expected_hash is not None and digest(result) != expected_hash):
        raise PmtError("continuity_corrupt", "Cleanup reference metadata failed integrity validation", 5)
    return result


def _refs(body):
    # The previous checkpoint is optional audit history, rather than a current
    # execution/evidence dependency that pins an unlimited parent chain.
    if isinstance(body, dict):
        body = {key: value for key, value in body.items() if key not in {
            "parent_checkpoint_ref", "parent_checkpoint", "previous_checkpoint_ref"}}
    return set(_IDS.findall(json.dumps(body, ensure_ascii=False)))


def _old(value, cutoff):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) < cutoff
    except (ValueError, TypeError, AttributeError):
        return False


def handle(db, conn, req):
    authorize(db, conn, req)
    payload = req.get("payload", {})
    if set(payload) - {"dry_run"} or type(payload.get("dry_run", True)) is not bool:
        raise PmtError("invalid_retention_policy", "Only an explicit dry_run flag is accepted")
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    scope = req["scope_id"]
    objects = {row["id"]: dict(row) for row in conn.execute(
        "SELECT * FROM continuity_objects WHERE scope_id=?", (scope,))}
    bodies = {object_id: _body(row["body_json"], row["body_hash"])
              for object_id, row in objects.items()}
    pending_effects = conn.execute("SELECT body_json,outcome_json,basis_hash FROM continuity_journal "
                                   "WHERE scope_id=? AND state!='completed'", (scope,)).fetchall()
    protected, retired_pointers = set(), []
    for pointer in conn.execute("SELECT * FROM continuity_pointers WHERE scope_id=?", (scope,)):
        selector = _body(pointer["selector_json"])
        task = conn.execute("SELECT state,updated_at FROM records WHERE id=?", (selector.get("task_id"),)).fetchone()
        if not pending_effects and task and task["state"] in _CLOSED and _old(task["updated_at"], cutoff):
            retired_pointers.append(pointer["pointer_key"])
        else:
            protected.add(pointer["object_id"])
    for row in pending_effects:
        protected |= _refs(_body(row["body_json"]))
        if row["outcome_json"]:
            protected |= _refs(_body(row["outcome_json"]))
        if row["basis_hash"]:
            protected |= {object_id for object_id, value in objects.items()
                          if row["basis_hash"] in {value["body_hash"], value["basis_hash"]}}
    scope_ids = [row[0] for row in conn.execute(
        "WITH RECURSIVE selected(id) AS (SELECT ? UNION ALL SELECT scopes.id FROM scopes JOIN selected ON scopes.parent_id=selected.id) SELECT id FROM selected", (scope,))]
    placeholders = ",".join("?" for _ in scope_ids)
    for row in conn.execute(f"SELECT id,state,body_json FROM records WHERE scope_id IN ({placeholders})", scope_ids):
        if row["state"] not in _CLOSED:
            protected |= _refs(_body(row["body_json"]))
    # Private rows belonging to other actors and fresh metadata also act as
    # reference roots, so cleanup cannot leave their current dependencies missing.
    for object_id, row in objects.items():
        if not _old(row["created_at"], cutoff) or (row["visibility"] == "private" and row["owner_actor"] != req["actor"]):
            protected.add(object_id)
        run_refs = _refs(bodies[object_id])
        if any(conn.execute("SELECT 1 FROM execution_runs WHERE id=? AND state IN ('starting','running','review_pending','reconciling','cancel_requested')", (value,)).fetchone() for value in run_refs):
            protected.add(object_id)
        if any(conn.execute("SELECT 1 FROM execution_jobs WHERE id=? AND state IN ('queued','starting','running','waiting')", (value,)).fetchone() for value in run_refs):
            protected.add(object_id)
    queue = list(protected)
    while queue:
        object_id = queue.pop()
        if object_id not in objects:
            continue
        for ref in _refs(bodies[object_id]) & objects.keys():
            if ref not in protected:
                protected.add(ref)
                queue.append(ref)
    candidates = sorted(object_id for object_id, row in objects.items()
                        if object_id not in protected and _old(row["created_at"], cutoff))
    if not payload.get("dry_run", True):
        for key in retired_pointers:
            conn.execute("DELETE FROM continuity_pointers WHERE scope_id=? AND pointer_key=?", (scope, key))
        for object_id in candidates:
            conn.execute("DELETE FROM continuity_events WHERE scope_id=? AND object_id=?", (scope, object_id))
            conn.execute("DELETE FROM artifact_refs WHERE owner_type='continuity' AND owner_id=?", (object_id,))
            conn.execute("DELETE FROM continuity_objects WHERE scope_id=? AND id=?", (scope, object_id))
        db.diagnostics.emit("continuity.metadata_pruned", request_id=req["request_id"], scope_id=scope,
                            count=len(candidates), outcome="candidate", transaction_outcome="pending")
    return {"dry_run": payload.get("dry_run", True), "candidate_count": len(candidates),
            "removed_count": 0 if payload.get("dry_run", True) else len(candidates),
            "retired_pointer_count": len(retired_pointers), "protected_count": len(protected & objects.keys()),
            "retention_days": 90, "source_files_changed": False}
