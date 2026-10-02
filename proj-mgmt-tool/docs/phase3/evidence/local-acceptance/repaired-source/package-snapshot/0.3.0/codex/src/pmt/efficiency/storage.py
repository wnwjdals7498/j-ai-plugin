"""Atomic storage primitives for phase-three derived state and receipts."""
import json

from ..errors import PmtError
from ..util import canonical_json, fingerprint, new_id, utc_now


class Phase3Storage:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _owner(actor, session):
        if not isinstance(actor, str) or not actor or not isinstance(session, str) or not session:
            raise PmtError("ownership_required", "actor and session are required")

    @staticmethod
    def _authorized(row, scope_id, actor, session):
        return row and row["scope_id"] == scope_id and row["owner_actor"] == actor and row["owner_session"] == session

    def put_object(self, kind, object_id, scope_id, owner_actor, owner_session, source_hash,
                   expected_revision, body, state="active", request_id=None, event_id=None, conn=None):
        self._owner(owner_actor, owner_session)
        if not all(isinstance(x, str) and x for x in (kind, object_id, scope_id, source_hash, state)):
            raise PmtError("input_invalid", "object identity, scope, source hash and state are required")
        if type(expected_revision) is not int or expected_revision < 0:
            raise PmtError("input_invalid", "expected_revision must be a nonnegative integer")
        encoded = canonical_json(body)
        request_id = request_id or new_id()
        wrote = {"value": False}

        def write(tx, _request):
            wrote["value"] = True
            row = tx.execute("SELECT * FROM phase3_objects WHERE kind=? AND id=?", (kind, object_id)).fetchone()
            if row:
                if not self._authorized(row, scope_id, owner_actor, owner_session):
                    raise PmtError("ownership_conflict", "Object belongs to another scope or owner", 3)
                if row["revision"] != expected_revision:
                    raise PmtError("revision_conflict", "Object revision changed", 3, False,
                                   {"expected_revision": expected_revision, "current_revision": row["revision"]})
                revision = expected_revision + 1
                tx.execute("UPDATE phase3_objects SET source_hash=?,revision=?,body_json=?,state=?,updated_at=? WHERE kind=? AND id=? AND revision=?",
                           (source_hash, revision, encoded, state, utc_now(), kind, object_id, expected_revision))
                if tx.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PmtError("revision_conflict", "Object revision changed", 3)
            else:
                if expected_revision != 0:
                    raise PmtError("revision_conflict", "Object does not exist at expected revision", 3, False,
                                   {"expected_revision": expected_revision, "current_revision": 0})
                revision = 1
                now = utc_now()
                tx.execute("INSERT INTO phase3_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (kind, object_id, scope_id, owner_actor, owner_session, source_hash, revision,
                            encoded, state, now, now))
            if event_id:
                tx.execute("INSERT INTO phase3_journal(id,request_id,event_id,kind,scope_id,owner_actor,owner_session,body_json,outcome_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (new_id(), request_id, event_id, "object." + kind, scope_id, owner_actor,
                            owner_session, canonical_json({"id": object_id, "revision": revision, "source_hash": source_hash}),
                            canonical_json({"revision": revision}), utc_now(), utc_now()))
            return {"id": object_id, "kind": kind, "revision": revision, "source_hash": source_hash,
                    "replayed": False}

        if conn is not None:
            return write(conn, {})
        request = {"request_id": request_id, "actor": owner_actor, "session_id": owner_session,
                   "operation": "phase3.put_object", "scope_id": scope_id,
                   "source": {"product": "local_store", "source_hash": source_hash},
                   "payload": {"kind": kind, "id": object_id, "expected_revision": expected_revision,
                               "source_hash": source_hash, "body_hash": fingerprint(body),
                               "state": state, "event_id": event_id}}
        envelope, exit_code = self.db.run_request(request, write)
        if exit_code:
            error = envelope.get("error") or {}
            raise PmtError(error.get("code", "storage_error"), error.get("message", "Storage operation failed"),
                           exit_code, error.get("retryable", False), error.get("details"))
        result = envelope["result"]
        result["replayed"] = not wrote["value"]
        return result

    def get_object(self, kind, object_id, scope_id, owner_actor, owner_session, conn=None):
        self._owner(owner_actor, owner_session)
        connection = conn or self.db.connect()
        try:
            row = connection.execute("SELECT * FROM phase3_objects WHERE kind=? AND id=?", (kind, object_id)).fetchone()
            if not self._authorized(row, scope_id, owner_actor, owner_session):
                return None
            return {"kind": row["kind"], "id": row["id"], "scope_id": row["scope_id"],
                    "owner_actor": row["owner_actor"], "owner_session": row["owner_session"],
                    "source_hash": row["source_hash"], "revision": row["revision"],
                    "body": json.loads(row["body_json"]), "state": row["state"],
                    "created_at": row["created_at"], "updated_at": row["updated_at"]}
        finally:
            if conn is None:
                connection.close()

    def list_objects(self, kind, scope_id, owner_actor, owner_session, limit=100, conn=None):
        self._owner(owner_actor, owner_session)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise PmtError("input_invalid", "limit must be between 1 and 500")
        connection = conn or self.db.connect()
        try:
            rows = connection.execute("SELECT * FROM phase3_objects WHERE kind=? AND scope_id=? AND owner_actor=? AND owner_session=? ORDER BY id LIMIT ?",
                                      (kind, scope_id, owner_actor, owner_session, limit)).fetchall()
            return [{"kind": r["kind"], "id": r["id"], "scope_id": r["scope_id"],
                     "owner_actor": r["owner_actor"], "owner_session": r["owner_session"],
                     "source_hash": r["source_hash"], "revision": r["revision"],
                     "body": json.loads(r["body_json"]), "state": r["state"]} for r in rows]
        finally:
            if conn is None:
                connection.close()

    def append_intent(self, kind, request_id, scope_id, owner_actor, owner_session, body, event_id=None, conn=None):
        return self._journal(kind, request_id, scope_id, owner_actor, owner_session, body, event_id, conn)

    def update_intent(self, intent_id, expected_stage, stage, scope_id, owner_actor, owner_session,
                      outcome=None, conn=None):
        def update(tx):
            row = tx.execute("SELECT body_json FROM phase3_journal WHERE id=? AND scope_id=? AND owner_actor=? AND owner_session=?",
                             (intent_id, scope_id, owner_actor, owner_session)).fetchone()
            if not row:
                raise PmtError("intent_not_found", "Intent does not exist", 3)
            body = json.loads(row[0])
            if body.get("stage") != expected_stage:
                raise PmtError("intent_conflict", "Intent stage changed", 3, False,
                               {"expected_stage": expected_stage, "current_stage": body.get("stage")})
            body["stage"] = stage
            body["outcome"] = outcome
            tx.execute("UPDATE phase3_journal SET body_json=?,outcome_json=?,completed_at=? WHERE id=?",
                       (canonical_json(body), canonical_json(outcome) if outcome is not None else None,
                        utc_now() if stage in {"completed", "failed", "cancelled"} else None, intent_id))
            return {"id": intent_id, "stage": stage}
        return self._with_conn(update, conn)

    def get_intent(self, intent_id, scope_id, owner_actor, owner_session, conn=None):
        connection = conn or self.db.connect()
        try:
            row = connection.execute("SELECT * FROM phase3_journal WHERE id=? AND scope_id=? AND owner_actor=? AND owner_session=?",
                                     (intent_id, scope_id, owner_actor, owner_session)).fetchone()
            return ({"id": row["id"], "request_id": row["request_id"], "kind": row["kind"],
                     "body": json.loads(row["body_json"]),
                     "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None} if row else None)
        finally:
            if conn is None:
                connection.close()

    def _journal(self, kind, request_id, scope_id, actor, session, body, event_id, conn):
        self._owner(actor, session)
        encoded = canonical_json(body)
        intent_id = new_id()
        def insert(tx):
            old = tx.execute("SELECT id,body_json FROM phase3_journal WHERE kind=? AND request_id=?", (kind, request_id)).fetchone()
            if old:
                identity = tx.execute("SELECT scope_id,owner_actor,owner_session FROM phase3_journal WHERE id=?", (old[0],)).fetchone()
                if tuple(identity) != (scope_id, actor, session):
                    raise PmtError("ownership_conflict", "Intent belongs to another scope or owner", 3)
                if old[1] != encoded:
                    raise PmtError("request_conflict", "Intent request id was reused", 3)
                return {"id": old[0], "request_id": request_id,
                        "stage": body.get("stage") if isinstance(body, dict) else None, "replayed": True}
            tx.execute("INSERT INTO phase3_journal(id,request_id,event_id,kind,scope_id,owner_actor,owner_session,body_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (intent_id, request_id, event_id, kind, scope_id, actor, session, encoded, utc_now()))
            return {"id": intent_id, "request_id": request_id,
                    "stage": body.get("stage") if isinstance(body, dict) else None, "replayed": False}
        return self._with_conn(insert, conn)

    def enqueue_outbox(self, kind, request_id, scope_id, owner_actor, owner_session, body, conn=None):
        self._owner(owner_actor, owner_session)
        encoded, item_id = canonical_json(body), new_id()
        def insert(tx):
            old = tx.execute("SELECT id,body_json,scope_id,owner_actor,owner_session FROM phase3_outbox WHERE kind=? AND request_id=?", (kind, request_id)).fetchone()
            if old:
                if tuple(old[2:]) != (scope_id, owner_actor, owner_session):
                    raise PmtError("ownership_conflict", "Outbox item belongs to another scope or owner", 3)
                if old[1] != encoded:
                    raise PmtError("request_conflict", "Outbox request id was reused", 3)
                return {"id": old[0], "replayed": True}
            now = utc_now()
            tx.execute("INSERT INTO phase3_outbox(id,kind,request_id,scope_id,owner_actor,owner_session,body_json,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (item_id, kind, request_id, scope_id, owner_actor, owner_session, encoded, "pending", now, now))
            return {"id": item_id, "replayed": False}
        return self._with_conn(insert, conn)

    def claim_outbox(self, owner, scope_id, owner_actor, owner_session, limit=20, kind=None, conn=None):
        self._owner(owner_actor, owner_session)
        if not owner or type(limit) is not int or not 1 <= limit <= 100:
            raise PmtError("input_invalid", "owner and limit 1..100 are required")
        def claim(tx):
            query = "SELECT id FROM phase3_outbox WHERE state='pending' AND scope_id=? AND owner_actor=? AND owner_session=?"
            args = [scope_id, owner_actor, owner_session]
            if kind is not None:
                query += " AND kind=?"
                args.append(kind)
            query += " ORDER BY created_at LIMIT ?"
            args.append(limit)
            rows = tx.execute(query, args).fetchall()
            ids = [row[0] for row in rows]
            for item_id in ids:
                tx.execute("UPDATE phase3_outbox SET state='leased',owner=?,attempts=attempts+1,updated_at=? WHERE id=? AND state='pending'",
                           (owner, utc_now(), item_id))
            return [dict(tx.execute("SELECT * FROM phase3_outbox WHERE id=?", (item_id,)).fetchone()) for item_id in ids]
        return self._with_conn(claim, conn)

    def reclaim_outbox(self, item_id, owner, conn=None):
        def reclaim(tx):
            cur = tx.execute("UPDATE phase3_outbox SET state='pending',owner=NULL,updated_at=? WHERE id=? AND state='leased' AND owner=?",
                             (utc_now(), item_id, owner))
            if cur.rowcount != 1:
                raise PmtError("outbox_owner_conflict", "Only the current lease owner can reclaim", 3)
            return {"id": item_id, "state": "pending"}
        return self._with_conn(reclaim, conn)

    def complete_outbox(self, item_id, owner, result, conn=None):
        encoded = canonical_json(result)
        def complete(tx):
            cur = tx.execute("UPDATE phase3_outbox SET state='done',result_json=?,updated_at=? WHERE id=? AND state='leased' AND owner=?",
                             (encoded, utc_now(), item_id, owner))
            if cur.rowcount != 1:
                raise PmtError("outbox_owner_conflict", "Only the current lease owner can complete", 3)
            return {"id": item_id, "state": "done"}
        return self._with_conn(complete, conn)

    def _with_conn(self, fn, conn):
        if conn is not None:
            return fn(conn)
        with self.db.write() as tx:
            return fn(tx)
