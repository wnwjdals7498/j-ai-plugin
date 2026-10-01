"""Transactional lifecycle operations for scopes, records, events and claims."""
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
import uuid

from .errors import PmtError
from .util import canonical_json, new_id, utc_now

SCOPE_KINDS = {"environment", "repository", "project", "classification"}
RECORD_KINDS = {"work", "item", "fact", "principle", "backlog"}
LIVE_STATES = {"Planned", "In Progress", "Paused", "Blocked"}

def _payload(request):
    value = request.get("payload", {})
    if not isinstance(value, dict):
        raise PmtError("invalid_payload", "payload must be an object")
    return value

def _field(request, key, aliases=(), required=False):
    payload = _payload(request)
    top = request.get(key)
    nested = next((payload[a] for a in (key, *aliases) if a in payload), None)
    if top is not None and nested is not None and top != nested:
        raise PmtError("input_conflict", f"{key} conflicts with its payload alias")
    value = top if top is not None else nested
    if required and value is None:
        raise PmtError("missing_field", f"{key} is required")
    return value

def _uuid(value, field):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PmtError("invalid_identifier", f"{field} must be a canonical UUID")
    return value

def _json_object(value, field):
    if not isinstance(value, dict):
        raise PmtError("invalid_field", f"{field} must be an object")
    canonical_json(value)
    return value

def _canonical_remote(value):
    if not isinstance(value, str) or not value.strip():
        raise PmtError("invalid_repository_remote", "repository remote must be a non-empty URL")
    remote = value.strip()
    scp = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", remote)
    if scp and "://" not in remote:
        host, path = scp.groups()
        scheme = "ssh"
    else:
        parsed = urlsplit(remote)
        if not parsed.scheme or not parsed.hostname or parsed.query or parsed.fragment:
            raise PmtError("invalid_repository_remote", "repository remote must be an absolute URL without query or fragment")
        if parsed.scheme.lower() not in {"ssh", "http", "https", "git"}:
            raise PmtError("invalid_repository_remote", "repository remote uses an unsupported scheme")
        try:
            host = parsed.hostname.lower()
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError as exc:
            raise PmtError("invalid_repository_remote", "repository remote has an invalid port") from exc
        host += port
        path = parsed.path
        scheme = parsed.scheme.lower()
    path = "/" + path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return f"{scheme}://{host.lower()}{path}"

def _event(conn, request, *, event_id=None, event_type, record_id=None, scope_id=None,
           reason=None, old_revision=None, new_revision=None, payload=None, occurred_at=None):
    event_id = _uuid(event_id or new_id(), "event_id")
    now = utc_now()
    conn.execute("""INSERT INTO events(id,event_id,record_id,scope_id,actor,event_type,reason,
        old_revision,new_revision,payload_json,occurred_at,recorded_at,correlation_id,causation_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (new_id(), event_id, record_id, scope_id, request.get("actor"), event_type, reason,
         old_revision, new_revision, canonical_json(payload or {}), occurred_at, now,
         request.get("correlation_id"), request.get("causation_id")))
    return conn.execute("SELECT id FROM events WHERE event_id=?", (event_id,)).fetchone()[0]

def _record(conn, request, record_id=None):
    record_id = _uuid(record_id or _field(request, "record_id", required=True), "record_id")
    row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise PmtError("record_not_found", "Record does not exist")
    return row

def _expected(request, row):
    expected = _field(request, "expected_revision", required=True)
    if not isinstance(expected, int) or isinstance(expected, bool) or expected != row["revision"]:
        raise PmtError("revision_conflict", "Record revision does not match", 3, False,
                       {"current_revision": row["revision"]})

def _record_result(row):
    body = json.loads(row["body_json"])
    return {"record_id": row["id"], "id": row["id"], "kind": row["kind"],
            "title": row["title"], "status": row["state"], "state": row["state"],
            "revision": row["revision"], "scope_id": row["scope_id"], "parent_id": row["parent_id"], "body": body}

def create_scope(db, conn, request):
    payload = _payload(request)
    kind = payload.get("kind")
    slug = payload.get("slug")
    parent_id = _field(request, "parent_id")
    if kind not in SCOPE_KINDS or not isinstance(slug, str) or not slug.strip():
        raise PmtError("invalid_scope", "A supported kind and non-empty slug are required")
    parent = None
    if parent_id is not None:
        parent_id = _uuid(parent_id, "parent_id")
        parent = conn.execute("SELECT id,kind FROM scopes WHERE id=?", (parent_id,)).fetchone()
        if parent is None:
            raise PmtError("scope_not_found", "Parent scope does not exist")
    allowed = {"environment": set(), "repository": {"environment"},
               "project": {"repository"}, "classification": {"project"}}
    if kind == "project" and parent is None:
        pass  # a standalone project is a supported repository root
    elif (parent is None and kind != "environment") or (parent is not None and parent["kind"] not in allowed[kind]):
        raise PmtError("invalid_scope_parent", "Parent scope has an invalid kind")
    body = _json_object(payload.get("body", {}), "body")
    body = dict(body)
    if kind == "repository":
        candidates = [body[key] for key in ("remote", "url") if key in body]
        if len(candidates) > 1 and _canonical_remote(candidates[0]) != _canonical_remote(candidates[1]):
            raise PmtError("repository_remote_conflict", "remote and url fields refer to different endpoints")
        if candidates:
            body["remote"] = _canonical_remote(candidates[0])
            body.pop("url", None)
        canonical_json(body)
    scope_id = _uuid(request.get("scope_id") or new_id(), "scope_id")
    now = utc_now()
    conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                 (scope_id, kind, parent_id, slug.strip(), canonical_json(body), now, now))
    if kind == "environment":
        conn.execute("INSERT INTO scope_paths(scope_id,environment_id,path) VALUES(?,?,?)",
                     (scope_id, scope_id, str(body.get("path", "")))) if body.get("path") else None
    _event(conn, request, event_type="scope_created", scope_id=scope_id, payload={"kind": kind, "slug": slug.strip()})
    return {"scope_id": scope_id, "id": scope_id, "kind": kind, "slug": slug.strip(), "parent_id": parent_id, "body": body}

def save_change(db, conn, request):
    payload = _payload(request)
    record_id = _field(request, "record_id")
    now = utc_now()
    if record_id is None:
        kind, title = payload.get("kind"), payload.get("title")
        scope_id = _field(request, "scope_id", required=True)
        scope_id = _uuid(scope_id, "scope_id")
        if kind not in RECORD_KINDS or not isinstance(title, str) or not title.strip():
            raise PmtError("invalid_record", "New record requires a supported kind and title")
        if not conn.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone():
            raise PmtError("scope_not_found", "Scope does not exist")
        parent_id = payload.get("parent_id")
        if parent_id is not None:
            parent_id = _uuid(parent_id, "parent_id")
            if not conn.execute("SELECT 1 FROM records WHERE id=? AND scope_id=?", (parent_id, scope_id)).fetchone():
                raise PmtError("record_parent_invalid", "Parent record must exist in the same scope")
        body = _normalize_body(kind, payload.get("body", {}))
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise PmtError("missing_reason", "A reason is required")
        record_id = _uuid(new_id(), "record_id")
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,'Planned',?,1,?,?)",
                     (record_id, kind, scope_id, parent_id, title.strip(), canonical_json(body), now, now))
        _event(conn, request, event_type="record_created", record_id=record_id, scope_id=scope_id,
               reason=reason.strip(), old_revision=0, new_revision=1, payload={"kind": kind})
    else:
        row = _record(conn, request, record_id)
        _expected(request, row)
        if row["state"] in {"Done", "Canceled", "Superseded"}:
            raise PmtError("record_closed", "Closed records cannot be changed")
        changes = {}
        if "status" in payload:
            status = payload["status"]
            if row["kind"] not in {"work", "item"}:
                raise PmtError("invalid_transition", "Only work and item records have task transitions")
            if status == "Planned" and row["state"] == "Blocked":
                changes["state"] = status
            elif status == "Canceled" and row["state"] in LIVE_STATES:
                owned = conn.execute("SELECT 1 FROM claims WHERE record_id=?", (row["id"],)).fetchone()
                if owned:
                    _owned_claim(conn, request, row)
                    if payload.get("stopped") is not True:
                        raise PmtError("stop_confirmation_required", "Canceling an owned task requires explicit stop confirmation")
                active_children = conn.execute(
                    "WITH RECURSIVE tree(id) AS (SELECT id FROM records WHERE parent_id=? UNION ALL "
                    "SELECT r.id FROM records r JOIN tree t ON r.parent_id=t.id) "
                    "SELECT c.record_id FROM claims c JOIN tree t ON t.id=c.record_id LIMIT 1", (row["id"],)
                ).fetchone()
                if active_children:
                    raise PmtError("active_children", "Stop owned child tasks before canceling their parent", 3)
                unfinished_children = conn.execute(
                    "SELECT 1 FROM records WHERE parent_id=? AND kind IN ('work','item') AND state NOT IN ('Done','Canceled') LIMIT 1",
                    (row["id"],)
                ).fetchone()
                if unfinished_children:
                    raise PmtError("unfinished_children", "Cancel child tasks explicitly before canceling their parent")
                changes["state"] = status
            else:
                raise PmtError("invalid_transition", "Use claim, release or finish for this transition")
        if "title" in payload:
            if not isinstance(payload["title"], str) or not payload["title"].strip():
                raise PmtError("invalid_title", "title must be non-empty")
            changes["title"] = payload["title"].strip()
        body = json.loads(row["body_json"])
        if "body" in payload:
            body = _normalize_body(row["kind"], payload["body"])
            changes["body_json"] = canonical_json(body)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise PmtError("missing_reason", "A reason is required")
        if not changes:
            raise PmtError("empty_change", "At least one record field must change")
        new_revision = row["revision"] + 1
        assignments = ",".join(f"{key}=?" for key in changes)
        conn.execute(f"UPDATE records SET {assignments},revision=?,updated_at=? WHERE id=? AND revision=?",
                     (*changes.values(), new_revision, now, row["id"], row["revision"]))
        if changes.get("state") == "Canceled":
            conn.execute("DELETE FROM claims WHERE record_id=?", (row["id"],))
        _event(conn, request, event_type="record_changed", record_id=row["id"], scope_id=row["scope_id"],
               reason=reason.strip(), old_revision=row["revision"], new_revision=new_revision,
               payload={"changed_fields": sorted(changes), "status": changes.get("state")})
    return _record_result(conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())

def _normalize_body(kind, body):
    if not isinstance(body, dict):
        raise PmtError("invalid_field", "body must be an object")
    body = dict(body)
    if kind == "item":
        criteria = body.get("criteria", [])
        if not isinstance(criteria, list):
            raise PmtError("invalid_criteria", "Item criteria must be an array")
        normalized = []
        criterion_ids = []
        for criterion in criteria:
            cid = criterion if isinstance(criterion, str) else criterion.get("id") if isinstance(criterion, dict) else None
            if not isinstance(cid, str) or not cid:
                raise PmtError("invalid_criteria", "Each criterion needs an id")
            normalized.append(dict(criterion) if isinstance(criterion, dict) and set(criterion) != {"id"} else cid)
            criterion_ids.append(cid)
        if len(set(criterion_ids)) != len(criterion_ids):
            raise PmtError("invalid_criteria", "Criterion IDs must be unique")
        body["criteria"] = normalized
        blocked = body.get("blocked_by", [])
        if not isinstance(blocked, list):
            raise PmtError("invalid_body", "blocked_by must be an array of record IDs")
        body["blocked_by"] = [_uuid(item, "blocked_by") for item in blocked]
        if "workspace" in body:
            workspace = body["workspace"]
            if not isinstance(workspace, (str, os.PathLike)):
                raise PmtError("invalid_body", "workspace must be a path string")
            body["workspace"] = str(workspace)
    canonical_json(body)
    return body

def save_decision(db, conn, request):
    payload = _payload(request)
    target = _record(conn, request)
    _expected(request, target)
    decision_kind = payload.get("decision_kind")
    if decision_kind not in {"select", "custom", "delegate"}:
        raise PmtError("invalid_decision", "decision_kind must be select, custom or delegate")
    reason = payload.get("reason")
    source = payload.get("confirmation_source") or payload.get("source")
    if not isinstance(reason, str) or not reason.strip() or not source:
        raise PmtError("decision_confirmation_required", "Decision reason and explicit confirmation source are required")
    if decision_kind == "select" and not payload.get("option_id"):
        raise PmtError("invalid_decision", "Selected decisions require option_id")
    if decision_kind == "custom" and not payload.get("content"):
        raise PmtError("invalid_decision", "Custom decisions require content")
    if decision_kind == "delegate" and not payload.get("delegation_scope"):
        raise PmtError("invalid_decision", "Delegated decisions require delegation_scope")
    supersedes = payload.get("supersedes")
    decision_id, now = new_id(), utc_now()
    if supersedes:
        supersedes = _uuid(supersedes, "supersedes")
        old = conn.execute("SELECT id,parent_id,body_json,revision,state FROM records WHERE id=? AND kind='decision'", (supersedes,)).fetchone()
        if old is None or old["parent_id"] != target["id"]:
            raise PmtError("invalid_supersedes", "Superseded decision must belong to this target")
        if old["state"] != "Current":
            raise PmtError("invalid_supersedes", "Only the current decision can be superseded")
        another_current = conn.execute(
            "SELECT 1 FROM records WHERE parent_id=? AND kind='decision' AND state='Current' AND id!=? LIMIT 1",
            (target["id"], supersedes)).fetchone()
        if another_current:
            raise PmtError("current_decision_conflict", "Target already has another current decision")
        old_body = json.loads(old["body_json"])
        old_body["superseded_by"] = decision_id
        updated = conn.execute("UPDATE records SET body_json=?,state='Superseded',revision=revision+1,updated_at=? "
                               "WHERE id=? AND state='Current'",
                     (canonical_json(old_body), utc_now(), supersedes))
        if updated.rowcount != 1:
            raise PmtError("invalid_supersedes", "Superseded decision is no longer current")
    elif conn.execute("SELECT 1 FROM records WHERE parent_id=? AND kind='decision' AND state='Current' LIMIT 1", (target["id"],)).fetchone():
        raise PmtError("supersedes_required", "A current decision exists and must be explicitly superseded")
    if not isinstance(payload.get("decider"), str) or not payload["decider"].strip():
        raise PmtError("invalid_decision", "decider is required")
    body = {key: payload[key] for key in ("decision_kind", "decider", "option_id", "content", "delegation_scope", "reason", "evidence_refs", "context_refs", "confirmation_source") if key in payload}
    body["supersedes"] = supersedes
    conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,'decision',?,?,?,'Current',?,1,?,?)",
                 (decision_id, target["scope_id"], target["id"], payload.get("title") or "Decision", canonical_json(body), now, now))
    next_revision = target["revision"] + 1
    conn.execute("UPDATE records SET revision=?,updated_at=? WHERE id=? AND revision=?", (next_revision, now, target["id"], target["revision"]))
    _event(conn, request, event_type="decision_saved", record_id=target["id"], scope_id=target["scope_id"],
           reason=reason.strip(), old_revision=target["revision"], new_revision=next_revision,
           payload={"decision_id": decision_id, "supersedes": supersedes})
    result = _record_result(conn.execute("SELECT * FROM records WHERE id=?", (target["id"],)).fetchone())
    result["decision_id"] = decision_id
    return result

def record_event(db, conn, request):
    normalized = request.get("normalized_event")
    if not isinstance(normalized, dict):
        raise PmtError("invalid_event", "normalized_event must be an object")
    event_id = _uuid(normalized.get("event_id"), "normalized_event.event_id")
    event_type = normalized.get("type")
    source = normalized.get("source", request.get("source", {}))
    meta = normalized.get("source_metadata", normalized.get("meta", {}))
    if not isinstance(event_type, str) or not event_type:
        raise PmtError("invalid_event", "normalized event type is required")
    if not isinstance(source, dict) or not isinstance(meta, dict):
        raise PmtError("invalid_event", "normalized event source and meta must be objects")
    source = dict(source)
    if "version" in source:
        source.setdefault("product_version", source["version"])
    if "installation_id" in source:
        source.setdefault("instance_id", source["installation_id"])
    meta = dict(meta)
    for source_key, canonical_key in (("native_event", "native_event_name"), ("tool", "tool_name")):
        if source_key in meta:
            meta.setdefault(canonical_key, meta[source_key])
    for key in ("turn_id", "source_event_id"):
        if key in normalized:
            meta.setdefault(key, normalized[key])
    source = {key: value for key, value in source.items()
              if key in {"product", "product_version", "adapter_version", "instance_id"}
              and isinstance(value, str) and len(value) <= 256}
    meta = {key: value for key, value in meta.items()
            if key in {"native_event_name", "turn_id", "source_event_id", "tool_name", "status", "source_session_id", "dedup_scope", "agent_id", "agent_type"}
            and isinstance(value, str) and len(value) <= 256}
    existing = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    if existing:
        original = json.loads(existing["payload_json"])
        return {"event_id": event_id, "id": existing["id"], "duplicate": True,
                "event_type": existing["event_type"], "source": original.get("source", {}),
                "meta": original.get("meta", {}), "occurred_at": existing["occurred_at"],
                "record_id": existing["record_id"], "scope_id": existing["scope_id"]}
    record_id = request.get("record_id")
    scope_id = request.get("scope_id")
    if record_id:
        row = _record(conn, request, record_id)
        scope_id = row["scope_id"]
    if scope_id:
        scope_id = _uuid(scope_id, "scope_id")
        if not conn.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone():
            raise PmtError("scope_not_found", "Event scope does not exist")
    inserted_id = _event(conn, request, event_id=event_id, event_type=event_type,
                         record_id=record_id, scope_id=scope_id,
                         occurred_at=normalized.get("occurred_at"),
                         payload={"source": source, "meta": meta})
    return {"event_id": event_id, "id": inserted_id, "duplicate": False, "event_type": event_type,
            "source": source, "meta": meta, "occurred_at": normalized.get("occurred_at"),
            "record_id": record_id, "scope_id": scope_id}

def _claim_record(conn, request):
    row = _record(conn, request)
    _expected(request, row)
    if row["kind"] not in {"item", "work"}:
        raise PmtError("invalid_claim_target", "Only work and item records can be claimed")
    if not request.get("session_id"):
        raise PmtError("missing_session", "session_id is required")
    return row

def claim_task(db, conn, request):
    row = _claim_record(conn, request)
    if row["state"] not in {"Planned", "Paused"}:
        raise PmtError("claim_conflict", "Record is not available for claim", 3)
    if conn.execute("SELECT 1 FROM claims WHERE record_id=?", (row["id"],)).fetchone():
        raise PmtError("claim_conflict", "Record already has an owner", 3)
    body = json.loads(row["body_json"])
    for dependency in body.get("blocked_by", []):
        dep = conn.execute("SELECT state FROM records WHERE id=?", (dependency,)).fetchone()
        if dep is None or dep["state"] not in {"Done", "Canceled"}:
            raise PmtError("task_blocked", "Task has an unresolved dependency", 3,
                           details={"dependency_id": dependency})
    token = new_id()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now, revision = utc_now(), row["revision"] + 1
    conn.execute("UPDATE records SET state='In Progress',revision=?,updated_at=? WHERE id=? AND revision=?",
                 (revision, now, row["id"], row["revision"]))
    event_id = _event(conn, request, event_type="claim_acquired", record_id=row["id"], scope_id=row["scope_id"],
                      old_revision=row["revision"], new_revision=revision)
    conn.execute("INSERT INTO claims(record_id,owner_session,token_hash,claimed_at,heartbeat_at,claim_event_id) VALUES(?,?,?,?,?,?)",
                 (row["id"], request["session_id"], token_hash, now, now, event_id))
    result = _record_result(conn.execute("SELECT * FROM records WHERE id=?", (row["id"],)).fetchone())
    result.update(claim_token=token, owner_session=request["session_id"])
    return result

def _owned_claim(conn, request, row):
    claim = conn.execute("SELECT * FROM claims WHERE record_id=?", (row["id"],)).fetchone()
    token = _field(request, "claim_token", required=True)
    if claim is None or claim["owner_session"] != request.get("session_id") or not isinstance(token, str) or hashlib.sha256(token.encode("utf-8")).hexdigest() != claim["token_hash"]:
        raise PmtError("claim_conflict", "Current claim owner or token does not match", 3)
    return claim

def release_claim(db, conn, request):
    row = _claim_record(conn, request)
    claim = _owned_claim(conn, request, row)
    payload = _payload(request)
    state, reason = payload.get("status"), payload.get("reason")
    if state not in {"Paused", "Blocked"} or not isinstance(reason, str) or not reason.strip():
        raise PmtError("invalid_release", "Release requires Paused or Blocked status and a reason")
    if state == "Paused" and not (payload.get("resume") or payload.get("next")):
        raise PmtError("resume_info_required", "Paused release requires resume information")
    if state == "Blocked" and not (payload.get("next_action") or payload.get("next")):
        raise PmtError("next_action_required", "Blocked release requires a next action")
    revision, now = row["revision"] + 1, utc_now()
    body = json.loads(row["body_json"])
    body["next"] = payload.get("next_action") or payload.get("next") or payload.get("resume")
    conn.execute("UPDATE records SET state=?,body_json=?,revision=?,updated_at=? WHERE id=? AND revision=?",
                 (state, canonical_json(body), revision, now, row["id"], row["revision"]))
    conn.execute("DELETE FROM claims WHERE record_id=?", (row["id"],))
    _event(conn, request, event_type="claim_released", record_id=row["id"], scope_id=row["scope_id"],
           reason=reason.strip(), old_revision=row["revision"], new_revision=revision,
           payload={"status": state, "owner_session": claim["owner_session"]})
    return _record_result(conn.execute("SELECT * FROM records WHERE id=?", (row["id"],)).fetchone())

def recover_claim(db, conn, request):
    row = _claim_record(conn, request)
    claim = conn.execute("SELECT * FROM claims WHERE record_id=?", (row["id"],)).fetchone()
    payload = _payload(request)
    proof = payload.get("terminated_or_isolated")
    reason = payload.get("reason")
    if claim is None or not proof or not isinstance(reason, str) or not reason.strip():
        raise PmtError("recovery_evidence_required", "Recovery requires an existing claim, termination/isolation evidence and reason")
    if request.get("actor") != "main":
        raise PmtError("recovery_owner_required", "Only the main actor may explicitly recover a claim")
    token = new_id()
    revision, now = row["revision"] + 1, utc_now()
    conn.execute("UPDATE records SET revision=?,updated_at=? WHERE id=? AND revision=?", (revision, now, row["id"], row["revision"]))
    event_id = _event(conn, request, event_type="claim_recovered", record_id=row["id"], scope_id=row["scope_id"],
                      reason=reason.strip(), old_revision=row["revision"], new_revision=revision,
                      payload={"previous_owner": claim["owner_session"], "evidence": proof})
    conn.execute("UPDATE claims SET owner_session=?,token_hash=?,claimed_at=?,heartbeat_at=?,claim_event_id=? WHERE record_id=?",
                 (request["session_id"], hashlib.sha256(token.encode()).hexdigest(), now, now, event_id, row["id"]))
    result = _record_result(conn.execute("SELECT * FROM records WHERE id=?", (row["id"],)).fetchone())
    result.update(claim_token=token, owner_session=request["session_id"])
    return result

def finish_task(db, conn, request):
    row = _claim_record(conn, request)
    claim = _owned_claim(conn, request, row)
    payload = _payload(request)
    unfinished = conn.execute("SELECT id FROM records WHERE parent_id=? AND kind IN ('work','item') AND state NOT IN ('Done','Canceled')",
                              (row["id"],)).fetchall()
    if unfinished:
        raise PmtError("unfinished_children", "Task has unfinished child tasks", 2,
                       details={"child_ids": [child["id"] for child in unfinished]})
    result_text = payload.get("result")
    if not isinstance(result_text, str) or not result_text.strip():
        raise PmtError("finish_result_required", "A non-empty result is required")
    verification_ids = payload.get("verification_ids", [])
    if not isinstance(verification_ids, list):
        raise PmtError("invalid_verifications", "verification_ids must be an array")
    if not verification_ids:
        raise PmtError("completion_evidence_unavailable", "Finishing requires current passing verification and evidence checks", 2)
    for verification_id in verification_ids:
        _uuid(verification_id, "verification_id")
        if conn.execute("SELECT 1 FROM verifications WHERE id=?", (verification_id,)).fetchone() is None:
            raise PmtError("completion_evidence_unavailable", "Referenced verification does not exist", 2)
    # Validators are owned by P3/P4. Missing validators fail closed until those modules are integrated.
    try:
        from .verification import verify_completion
        from .resources import check_artifact
    except ImportError as exc:
        raise PmtError("completion_evidence_unavailable", "Verification and artifact validators are not integrated", 2) from exc
    verified = verify_completion(db, conn, row, verification_ids)
    criteria = json.loads(row["body_json"]).get("criteria", [])
    required = {criterion if isinstance(criterion, str) else criterion["id"] for criterion in criteria}
    if row["kind"] == "item" and not required:
        raise PmtError("completion_criteria_unmet", "Items require explicit non-empty completion criteria")
    if not verified or not verified.get("valid") or not required.issubset(set(verified.get("covered_criteria", []))):
        raise PmtError("completion_criteria_unmet", "Required completion criteria or evidence are not satisfied", 2)
    artifacts = []
    for artifact_id in verified.get("evidence_ids", []):
        checked = check_artifact(db, conn, artifact_id)
        if not checked or not checked.get("valid"):
            raise PmtError("completion_evidence_unavailable", "Completion evidence is missing or invalid", 2,
                           details={"artifact_id": artifact_id, "reason": (checked or {}).get("reason")})
        artifacts.append(artifact_id)
    if required and not artifacts:
        raise PmtError("completion_evidence_unavailable", "Every completed Item requires ready evidence", 2)
    revision, now = row["revision"] + 1, utc_now()
    body = json.loads(row["body_json"])
    body["result"] = result_text.strip()
    body["verification_ids"] = verification_ids
    conn.execute("UPDATE records SET state='Done',body_json=?,revision=?,updated_at=? WHERE id=? AND revision=?",
                 (canonical_json(body), revision, now, row["id"], row["revision"]))
    conn.execute("DELETE FROM claims WHERE record_id=?", (row["id"],))
    _event(conn, request, event_type="task_finished", record_id=row["id"], scope_id=row["scope_id"],
           reason=result_text.strip(), old_revision=row["revision"], new_revision=revision,
           payload={"verification_ids": verification_ids, "artifact_ids": artifacts, "covered_criteria": sorted(required),
                    "owner_session": claim["owner_session"]})
    return _record_result(conn.execute("SELECT * FROM records WHERE id=?", (row["id"],)).fetchone())


def handle(db, conn, request):
    handlers = {"create_scope": create_scope, "save_change": save_change,
                "save_decision": save_decision, "record_event": record_event,
                "claim_task": claim_task, "release_claim": release_claim,
                "recover_claim": recover_claim, "finish_task": finish_task}
    try:
        handler = handlers[request["operation"]]
    except KeyError:
        raise PmtError("operation_unsupported", "Unsupported lifecycle operation")
    return handler(db, conn, request)
