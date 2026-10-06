"""Immutable project metadata, private projections, CAS pointers and effect journals."""
from __future__ import annotations

import json
import re
import uuid

from ..errors import PmtError
from ..phase2_common import identifier
from ..util import canonical_json, utc_now
from .contracts import CONTRACT_VERSION, KINDS, PRIVATE_KINDS, authorize, digest, validate_metadata


class ContinuityStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        try:
            result["body"] = json.loads(result.pop("body_json"))
        except (TypeError, ValueError) as exc:
            raise PmtError("continuity_corrupt", "Stored continuity object is corrupt", 5) from exc
        if digest(result["body"]) != result["body_hash"]:
            raise PmtError("continuity_corrupt", "Stored continuity body hash changed", 5)
        if result["contract_version"] != CONTRACT_VERSION:
            raise PmtError("continuity_version_unsupported", "Continuity contract version is unsupported", 2)
        return result

    @staticmethod
    def _protect_refs(conn, req, body, owner_type, owner_id, purpose):
        for artifact_id in set(re.findall(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", canonical_json(body))):
            artifact = conn.execute("SELECT scope_id,state FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if artifact:
                if artifact["scope_id"] != req["scope_id"] or artifact["state"] != "ready":
                    raise PmtError("resource_scope_mismatch", "Referenced evidence is not ready in this scope", 3)
                conn.execute("INSERT OR IGNORE INTO artifact_refs VALUES(?,?,?,?,?)",
                             (artifact_id, owner_type, owner_id, purpose, utc_now()))

    def put(self, conn, req, kind, body, *, object_id=None, visibility="shared", basis_hash=None, event_id=None):
        authorize(self.db, conn, req)
        if kind not in KINDS or visibility not in {"shared", "private"}:
            raise PmtError("invalid_continuity_kind", "Unsupported continuity kind or visibility")
        if kind in PRIVATE_KINDS and visibility != "private":
            raise PmtError("private_metadata_forbidden", "Task projections and details must remain private", 3)
        validate_metadata(body)
        if basis_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(basis_hash)):
            raise PmtError("invalid_basis_hash", "Basis hash must be a SHA256")
        body_hash = digest(body)
        if event_id is not None:
            identifier(event_id, "event_id")
            old = conn.execute("SELECT * FROM continuity_events WHERE scope_id=? AND kind=? AND event_id=?",
                               (req["scope_id"], kind, event_id)).fetchone()
            if old:
                if old["body_hash"] != body_hash:
                    raise PmtError("event_conflict", "Event is already bound to different metadata", 3)
                saved = self.get(conn, req, old["object_id"], kind=kind)
                if saved["basis_hash"] != basis_hash or saved["visibility"] != visibility:
                    raise PmtError("event_conflict", "Event is already bound to a different basis or visibility", 3)
                return saved
        if object_id is None:
            identity = {"scope": req["scope_id"], "kind": kind, "body_hash": body_hash,
                        "visibility": visibility, "event_id": event_id,
                        "basis_hash": basis_hash, "contract_version": CONTRACT_VERSION}
            if visibility == "private":
                identity.update(actor=req["actor"], session=req["session_id"])
            object_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "pmt:continuity:" + digest(identity)))
        identifier(object_id, "object_id")
        old = conn.execute("SELECT * FROM continuity_objects WHERE id=?", (object_id,)).fetchone()
        if old:
            same = (old["scope_id"] == req["scope_id"] and old["kind"] == kind and
                    old["body_hash"] == body_hash and old["visibility"] == visibility and
                    old["basis_hash"] == basis_hash)
            if not same:
                raise PmtError("immutable_conflict", "Object ID is already bound to different metadata", 3)
            result = self.get(conn, req, object_id, kind=kind)
        else:
            now = utc_now()
            conn.execute("INSERT INTO continuity_objects VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
                         (object_id, kind, req["scope_id"], CONTRACT_VERSION, body_hash, basis_hash,
                          visibility, req["actor"], req["session_id"], canonical_json(body), now))
            result = self.get(conn, req, object_id, kind=kind)
            # A live immutable record protects all actual resources it references.
            self._protect_refs(conn, req, body, "continuity", object_id, kind)
        if event_id is not None:
            conn.execute("INSERT INTO continuity_events VALUES(?,?,?,?,?,?)",
                         (req["scope_id"], kind, event_id, body_hash, object_id, utc_now()))
        self.db.diagnostics.emit("continuity.object_prepared", request_id=req["request_id"],
                                 scope_id=req["scope_id"], object_kind=kind, object_id=object_id,
                                 source_hash=body_hash, basis_hash=basis_hash, outcome="candidate")
        return result

    def get(self, conn, req, object_id, *, kind=None):
        authorize(self.db, conn, req)
        identifier(object_id, "object_id")
        row = conn.execute("SELECT * FROM continuity_objects WHERE id=? AND scope_id=?",
                           (object_id, req["scope_id"])).fetchone()
        if row is None or (kind is not None and row["kind"] != kind):
            raise PmtError("continuity_not_found", "Continuity object is unavailable in this project", 3)
        if row["visibility"] == "private" and (row["owner_actor"], row["owner_session"]) != (req["actor"], req["session_id"]):
            raise PmtError("ownership_conflict", "Private projection belongs to another actor or session", 3)
        return self._decode(row)

    def list(self, conn, req, kind, *, limit=100):
        authorize(self.db, conn, req)
        if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 200:
            raise PmtError("invalid_continuity_query", "Unsupported kind or query limit")
        rows = conn.execute("SELECT * FROM continuity_objects WHERE scope_id=? AND kind=? AND "
                            "(visibility='shared' OR (owner_actor=? AND owner_session=?)) "
                            "ORDER BY created_at DESC,id LIMIT ?",
                            (req["scope_id"], kind, req["actor"], req["session_id"], limit)).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _selector(selector):
        fields = {"repository_id", "branch", "workspace_ref", "task_id", "purpose", "environment_id"}
        if not isinstance(selector, dict) or set(selector) - fields or not selector.get("purpose"):
            raise PmtError("invalid_pointer_selector", "An explicit bounded pointer selector is required")
        validate_metadata(selector)
        for value in selector.values():
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 300):
                raise PmtError("invalid_pointer_selector", "Pointer dimensions must be bounded strings")
        normalized = {key: value for key, value in selector.items() if value is not None}
        return normalized, digest(normalized)

    def read_pointer(self, conn, req, selector):
        authorize(self.db, conn, req)
        selector, key = self._selector(selector)
        row = conn.execute("SELECT object_id,revision,selector_json FROM continuity_pointers "
                           "WHERE scope_id=? AND pointer_key=?", (req["scope_id"], key)).fetchone()
        if row:
            try:
                stored_selector, stored_key = self._selector(json.loads(row["selector_json"]))
            except (TypeError, ValueError, PmtError) as exc:
                raise PmtError("continuity_corrupt", "Stored pointer selector is corrupt", 5) from exc
            if stored_selector != selector or stored_key != key:
                raise PmtError("continuity_corrupt", "Pointer selector hash does not match", 5)
        return {"pointer_key": key, "object_id": row["object_id"] if row else None,
                "revision": row["revision"] if row else 0, "selector": selector}

    def advance_pointer(self, conn, req, selector, object_id, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise PmtError("invalid_pointer_revision", "Expected pointer revision must be nonnegative")
        obj = self.get(conn, req, object_id)
        current = self.read_pointer(conn, req, selector)
        selector = current["selector"]
        if current["revision"] != expected_revision:
            raise PmtError("revision_conflict", "Continuity pointer changed", 3, details={
                "expected_revision": expected_revision, "current_revision": current["revision"]})
        if obj["visibility"] != "shared":
            raise PmtError("private_metadata_forbidden", "Shared pointers cannot reference private projections", 3)
        task_id = selector.get("task_id")
        if task_id:
            authorize(self.db, conn, req | {"record_id": task_id})
        revision = expected_revision + 1
        if expected_revision == 0:
            conn.execute("INSERT INTO continuity_pointers VALUES(?,?,?,?,?,?)",
                         (req["scope_id"], current["pointer_key"], canonical_json(selector), object_id, revision, utc_now()))
        else:
            conn.execute("UPDATE continuity_pointers SET object_id=?,revision=?,updated_at=? "
                         "WHERE scope_id=? AND pointer_key=? AND revision=?",
                         (object_id, revision, utc_now(), req["scope_id"], current["pointer_key"], expected_revision))
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise PmtError("revision_conflict", "Continuity pointer changed", 3)
        self.db.diagnostics.emit("continuity.pointer_prepared", request_id=req["request_id"],
                                 scope_id=req["scope_id"], pointer_key=current["pointer_key"],
                                 object_id=object_id, old_revision=expected_revision, new_revision=revision,
                                 outcome="candidate")
        return current | {"object_id": object_id, "revision": revision, "previous_revision": expected_revision}

    @staticmethod
    def _journal(row):
        if row is None:
            return None
        result = dict(row)
        try:
            result["body"] = json.loads(result.pop("body_json"))
            outcome = result.pop("outcome_json")
            result["outcome"] = json.loads(outcome) if outcome else None
        except (TypeError, ValueError) as exc:
            raise PmtError("continuity_corrupt", "Stored effect journal is corrupt", 5) from exc
        if not isinstance(result["body"], dict) or (result["outcome"] is not None and not isinstance(result["outcome"], dict)):
            raise PmtError("continuity_corrupt", "Stored effect journal has an invalid shape", 5)
        if digest(result["body"]) != result["body_hash"]:
            raise PmtError("continuity_corrupt", "Effect journal hash changed", 5)
        return result

    def begin_effect(self, conn, req, kind, body, *, basis_hash=None):
        authorize(self.db, conn, req)
        validate_metadata(body)
        if not isinstance(kind, str) or not 1 <= len(kind) <= 80:
            raise PmtError("invalid_effect_kind", "Effect kind is required")
        effect_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "continuity:" + kind))
        row = conn.execute("SELECT * FROM continuity_journal WHERE id=?", (effect_id,)).fetchone()
        if row:
            result = self.get_effect(conn, req, effect_id)
            if result["kind"] != kind or result["body_hash"] != digest(body) or result["basis_hash"] != basis_hash:
                raise PmtError("request_conflict", "Original effect is bound to different metadata", 3)
            return result
        now = utc_now()
        conn.execute("INSERT INTO continuity_journal VALUES(?,?,?,?,?,?,?,?,?,'prepared',NULL,?,?)",
                     (effect_id, req["request_id"], kind, req["scope_id"], req["actor"], req["session_id"],
                      basis_hash, digest(body), canonical_json(body), now, now))
        self._protect_refs(conn, req, body, "continuity_effect", effect_id, kind)
        return self.get_effect(conn, req, effect_id)

    def get_effect(self, conn, req, effect_id):
        authorize(self.db, conn, req)
        identifier(effect_id, "effect_id")
        row = conn.execute("SELECT * FROM continuity_journal WHERE id=? AND scope_id=? AND owner_actor=? "
                           "AND owner_session=?", (effect_id, req["scope_id"], req["actor"], req["session_id"])).fetchone()
        if not row:
            raise PmtError("effect_not_found", "Effect journal is unavailable to this owner", 3)
        return self._journal(row)

    def update_effect(self, conn, req, effect_id, state, outcome=None):
        prior = self.get_effect(conn, req, effect_id)
        if state not in {"prepared", "applying", "partial", "unknown", "conflict", "completed"}:
            raise PmtError("invalid_effect_state", "Unsupported effect state")
        if outcome is not None:
            validate_metadata(outcome)
            self._protect_refs(conn, req, outcome, "continuity_effect", effect_id, prior["kind"])
        if prior["state"] == "completed":
            if state == "completed" and prior["outcome"] == outcome:
                return prior
            raise PmtError("effect_completed", "Completed effects cannot be re-applied", 3)
        conn.execute("UPDATE continuity_journal SET state=?,outcome_json=?,updated_at=? WHERE id=?",
                     (state, canonical_json(outcome) if outcome is not None else None, utc_now(), effect_id))
        self.db.diagnostics.emit("continuity.effect_prepared", request_id=req["request_id"],
                                 scope_id=req["scope_id"], effect_id=effect_id,
                                 basis_hash=prior["basis_hash"], outcome=state, transaction_outcome="pending")
        return self.get_effect(conn, req, effect_id)
