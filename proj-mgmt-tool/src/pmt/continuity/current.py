"""Current facts and checkpoint operations for phase four.

The service is a projection over existing PMT records, runs and verification
receipts. It does not own those states or infer authority from its own refs.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import uuid

from ..errors import PmtError
from ..util import canonical_json
from .contracts import authorize, digest, work_access
from .storage import ContinuityStore

READ_OPERATIONS = {"read_current_facts", "read_checkpoint"}
WRITE_OPERATIONS = {"create_checkpoint", "link_session"}
FILE_OPERATIONS = {"capture_work_basis", "validate_basis"}

_VERIFICATION_OUTCOMES = {"pass", "fail", "blocked", "aborted"}


def classify_implementation(*, planned: bool, implementation_refs=(), verification=None) -> str:
    """Return a conservative implementation level from actual source refs.

    Verification is accepted only as a persisted row projection. Free-form
    model or caller claims cannot upgrade a feature to verified.
    """
    refs = tuple(ref for ref in implementation_refs if isinstance(ref, str) and ref)
    if not refs:
        return "planned" if planned else "unknown"
    if not isinstance(verification, Mapping):
        return "implemented"
    outcome = verification.get("outcome")
    if (verification.get("state") == "valid"
            and outcome == "pass"
            and verification.get("definition_ref")
            and verification.get("evidence_refs")
            and verification.get("basis_ref")):
        return "verified"
    if outcome in _VERIFICATION_OUTCOMES:
        return "implemented"
    return "implemented"


def basis_body(*, scope: Mapping[str, Any], source: Mapping[str, Any],
               contract: Mapping[str, Any], work: Mapping[str, Any],
               conditions: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Build the agreed immutable BasisVector body without inventing coherence."""
    source_fields = {
        "repository_id", "branch", "workspace_ref", "observed_head",
        "analyzed_ref", "applied_ref", "dirty_state", "dirty_fingerprint",
        "inventory_ref", "inventory_hash", "inventory_coverage",
    }
    if set(source) != source_fields:
        raise ValueError("source basis fields do not match the phase-four contract")
    coverage = source["inventory_coverage"]
    if not isinstance(coverage, Mapping) or set(coverage) != {
            "selected_count", "verified_count", "unknown_count", "complete", "reason_codes"}:
        raise ValueError("inventory coverage must include counts, completeness and reasons")
    components = manifest.get("components") if isinstance(manifest, Mapping) else None
    if not isinstance(components, list):
        raise ValueError("capture manifest components must be a list")
    complete_components = all(
        isinstance(item, Mapping) and item.get("complete") is True for item in components
    )
    coherent = manifest.get("coherence") == "coherent" and complete_components
    return {
        "version": 1,
        "scope": dict(scope),
        "source": dict(source),
        "contract": dict(contract),
        "work": dict(work),
        "conditions": dict(conditions),
        "manifest": {
            **dict(manifest),
            "coherence": "coherent" if coherent else manifest.get("coherence", "incomplete"),
        },
        "complete": coherent and coverage.get("complete") is True,
    }


def validate_basis_components(expected: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Compare selected basis components and report uncertainty explicitly."""
    if not isinstance(expected, Mapping) or not isinstance(current, Mapping):
        raise ValueError("basis values must be objects")
    changed, unknown = [], []
    for component in ("scope", "source", "contract", "work", "conditions"):
        old, new = expected.get(component), current.get(component)
        if old is None or new is None:
            unknown.append(component)
        elif component == "source" and isinstance(old, Mapping) and isinstance(new, Mapping):
            old_source = {key: value for key, value in old.items() if key != "inventory_ref"}
            new_source = {key: value for key, value in new.items() if key != "inventory_ref"}
            if old_source != new_source:
                changed.append(component)
        elif old != new:
            changed.append(component)
    if changed:
        status = "changed"
    elif unknown:
        status = "unknown"
    elif expected.get("complete") is not True or current.get("complete") is not True:
        status = "unknown"
        unknown.append("completeness")
    else:
        status = "unchanged"
    return {"status": status, "changed_components": changed,
            "unknown_components": unknown}


def _payload(req):
    payload = req.get("payload", {})
    if not isinstance(payload, dict):
        raise PmtError("invalid_payload", "Continuity payload must be an object")
    return payload


def _scope_id(req):
    value = req.get("scope_id")
    if not isinstance(value, str) or not value:
        raise PmtError("scope_required", "An explicit project scope is required")
    return value


def _rows(conn, scope_id):
    return conn.execute("WITH RECURSIVE project_scopes(id) AS (SELECT id FROM scopes WHERE id=? "
                        "UNION ALL SELECT s.id FROM scopes s JOIN project_scopes p ON s.parent_id=p.id) "
                        "SELECT r.id,r.scope_id,r.kind,r.parent_id,r.title,r.state,r.body_json,r.revision,r.updated_at "
                        "FROM records r WHERE r.scope_id IN (SELECT id FROM project_scopes) ORDER BY r.kind,r.id",
                        (scope_id,)).fetchall()


def _snapshot(conn, scope_id, *, exclude_effect_id=None):
    records = _rows(conn, scope_id)
    scope_ids = [row[0] for row in conn.execute(
        "WITH RECURSIVE project_scopes(id) AS (SELECT id FROM scopes WHERE id=? "
        "UNION ALL SELECT s.id FROM scopes s JOIN project_scopes p ON s.parent_id=p.id) "
        "SELECT id FROM project_scopes ORDER BY id", (scope_id,)).fetchall()]
    placeholders = ",".join("?" for _ in scope_ids)
    runs = conn.execute("SELECT id,step_id,state,revision,stop_confirmed,result_json,updated_at FROM execution_runs "
                         f"WHERE step_id IN (SELECT id FROM records WHERE scope_id IN ({placeholders})) ORDER BY id",
                         tuple(scope_ids)).fetchall()
    claims = conn.execute("SELECT record_id FROM claims WHERE record_id IN "
                          f"(SELECT id FROM records WHERE scope_id IN ({placeholders})) ORDER BY record_id",
                          tuple(scope_ids)).fetchall()
    pending, pending_observed = [], True
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase3_outbox'").fetchone():
        pending.extend(conn.execute("SELECT id,kind,state,updated_at FROM phase3_outbox WHERE scope_id=? "
                                     "AND state IN ('pending','leased') ORDER BY id", (scope_id,)).fetchall())
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuity_journal'").fetchone():
        pending.extend(conn.execute("SELECT id,kind,state,updated_at FROM continuity_journal WHERE scope_id=? "
                                     "AND state IN ('prepared','applying','partial','unknown','conflict') "
                                     "AND (? IS NULL OR id<>?) ORDER BY id",
                                     (scope_id, exclude_effect_id, exclude_effect_id)).fetchall())
    record_views = []
    decisions, direction_refs, status, attention = [], [], [], []
    for row in records:
        body = json.loads(row["body_json"] or "{}")
        ref = {"id": row["id"], "scope_id": row["scope_id"], "kind": row["kind"],
               "title": row["title"][:240], "state": row["state"],
               "revision": row["revision"], "updated_at": row["updated_at"]}
        record_views.append(ref)
        if row["kind"] in {"decision", "choice", "premise"}:
            decisions.append(ref)
        if row["kind"] in {"project", "requirement", "direction"}:
            direction_refs.append(ref)
        if row["state"] in {"In Progress", "Blocked", "Paused", "Review"}:
            status.append(ref)
        if row["state"] in {"Blocked", "Paused", "Review"}:
            attention.append(ref)
    active = [{"run_ref": row["id"], "task_ref": row["step_id"], "state": row["state"],
               "revision": row["revision"], "stop_confirmed": bool(row["stop_confirmed"]),
               "updated_at": row["updated_at"]}
              for row in runs if row["state"] in {
                  "queued", "starting", "running", "review_pending", "reconciling", "cancel_requested"}]
    occupied = [{"task_ref": row["record_id"], "state": "occupied"} for row in claims]
    pending_refs = [{"pending_ref": row["id"], "kind": row["kind"], "state": row["state"],
                     "updated_at": row["updated_at"]} for row in pending]
    revision_set = {row["id"]: row["revision"] for row in records}
    revision_set.update({"run:" + row["id"]: row["revision"] for row in runs})
    revision_set.update({"pending:" + row["id"]: row["state"] for row in pending})
    step_ids = [row["id"] for row in records if row["kind"] == "step"]
    step_specs = []
    if step_ids:
        marks = ",".join("?" for _ in step_ids)
        for row in conn.execute(f"SELECT step_id,directive_id,directive_version,requirements_version,plan_version "
                                f"FROM step_specs WHERE step_id IN ({marks}) ORDER BY step_id", tuple(step_ids)):
            view = dict(row)
            step_specs.append(view)
            revision_set["step_spec:" + row["step_id"]] = digest(view)
    verification_refs = []
    if records:
        record_placeholders = ",".join("?" for _ in records)
        target_ids = [row["id"] for row in records]
        for row in conn.execute("SELECT id,target_id,definition_id,definition_version,environment_id,"
                                "input_fingerprint,outcome,state,completed_at,evidence_json FROM verifications "
                                f"WHERE target_id IN ({record_placeholders}) ORDER BY completed_at,id", tuple(target_ids)):
            try:
                evidence_refs = json.loads(row["evidence_json"] or "[]")
            except (TypeError, ValueError):
                evidence_refs = []
            if not isinstance(evidence_refs, list):
                evidence_refs = []
            verification_refs.append({"verification_ref": row["id"], "target_ref": row["target_id"],
                                      "definition_ref": row["definition_id"],
                                      "definition_version": row["definition_version"],
                                      "environment_id": row["environment_id"],
                                      "input_fingerprint": row["input_fingerprint"],
                                      "evidence_refs": [item for item in evidence_refs if isinstance(item, str)],
                                      "outcome": row["outcome"], "state": row["state"],
                                      "completed_at": row["completed_at"]})
            revision_set.update({"verification:" + item["verification_ref"]: digest(item)
                                 for item in verification_refs})
    return {"records": record_views, "decisions": decisions, "direction_refs": direction_refs,
            "current_status": status, "attention": attention, "active_execution": active,
            "claim_refs": occupied, "pending_refs": pending_refs,
            "pending_observed": pending_observed,
            "step_specs": step_specs, "verification_refs": verification_refs,
            "revision_set": revision_set,
            "snapshot_hash": digest({"records": record_views, "active": active,
                                     "claims": occupied, "pending": pending_refs,
                                     "step_specs": step_specs,
                                     "verification_refs": verification_refs})}


def _selected_basis(db, conn, req):
    """Resolve an actual retained BasisVector from a ref or exact current checkpoint pointer."""
    payload = _payload(req)
    store = ContinuityStore(db)
    basis_ref = payload.get("basis_ref")
    if isinstance(basis_ref, str):
        return store.get(conn, req, basis_ref, kind="basis")
    selector = payload.get("selector")
    if isinstance(selector, dict) and selector.get("purpose"):
        pointer = store.read_pointer(conn, req, selector)
        checkpoint = (store.get(conn, req, pointer["object_id"], kind="checkpoint")
                      if pointer.get("object_id") else None)
        if checkpoint and isinstance(checkpoint["body"].get("basis_ref"), str):
            return store.get(conn, req, checkpoint["body"]["basis_ref"], kind="basis")
        return None
    task_id = payload.get("task_id") or req.get("record_id")
    rows = conn.execute("SELECT selector_json,object_id,revision FROM continuity_pointers WHERE scope_id=?",
                        (req["scope_id"],)).fetchall()
    candidates = []
    for row in rows:
        try:
            candidate_selector = json.loads(row["selector_json"])
        except (TypeError, ValueError):
            continue
        if candidate_selector.get("purpose") != "current":
            continue
        if task_id is not None and candidate_selector.get("task_id") != task_id:
            continue
        for dimension in ("repository_id", "branch", "workspace_ref"):
            expected = payload.get(dimension)
            if expected is not None and candidate_selector.get(dimension) != expected:
                break
        else:
            candidates.append((row["revision"], row["object_id"]))
    if len(candidates) != 1:
        return None
    checkpoint = store.get(conn, req, candidates[0][1], kind="checkpoint")
    if not isinstance(checkpoint["body"].get("basis_ref"), str):
        return None
    return store.get(conn, req, checkpoint["body"]["basis_ref"], kind="basis")


def _implementation_facts(db, conn, req, basis_obj, verification_refs, has_plan):
    """Join only persisted, same-basis receipts; metadata reads never revalidate source bytes."""
    if basis_obj is None:
        return {"level": "planned" if has_plan else "unknown", "basis_ref": None,
                "basis_hash": None, "source_currentness": "unknown_without_selected_basis",
                "implementation_refs": [], "change_refs": [], "assessment_refs": [],
                "verification_at_basis": [], "current_applicability": "unknown_requires_read_applicability",
                "unknowns": ["current_basis_not_selected"]}
    store, basis = ContinuityStore(db), basis_obj["body"]
    basis_ref, basis_hash = basis_obj["id"], basis_obj["body_hash"]
    indexes = [item for item in store.list(conn, req, "link_index", limit=200)
               if item["body"].get("basis_ref") == basis_ref
               and item["body"].get("basis_hash") == basis_hash]
    changes = [item for item in store.list(conn, req, "change", limit=200)
               if item["body"].get("after_basis_ref") == basis_ref
               and item["body"].get("after_basis_hash") == basis_hash]
    changes = [item for item in changes if item["body"].get("coverage") == "complete"
               and item["body"].get("state") in {"complete", "no_change"}]
    index_refs = [{"ref": item["id"], "hash": item["body_hash"],
        "coverage": item["body"].get("coverage", {}),
        "entry_count": len(item["body"].get("entries", [])),
        "link_count": len(item["body"].get("links", []))} for item in indexes]
    change_refs = [{"ref": item["id"], "hash": item["body_hash"],
        "state": item["body"].get("state"), "coverage": item["body"].get("coverage"),
        "intent_state": item["body"].get("intent_state", "unknown"),
        "reason_codes": item["body"].get("reason_codes", [])} for item in changes]
    assessments = [item for item in store.list(conn, req, "assessment", limit=200)
        if item["body"].get("basis_ref") == basis_ref
        and item["body"].get("basis_hash") == basis_hash
        and any(item["body"].get("change_ref") == change["id"]
                and item["body"].get("change_hash") == change["body_hash"] for change in changes)]
    assessment_refs = [{"ref": item["id"], "hash": item["body_hash"],
        "state": item["body"].get("state"), "unknown_count": len(item["body"].get("unknown", [])),
        "semantic_state": item["body"].get("semantic_state", "unknown"),
        "required_action": item["body"].get("required_action")} for item in assessments]
    basis_conditions = basis.get("conditions", {})
    selected = basis_conditions.get("selected", []) if isinstance(basis_conditions, Mapping) else []
    verification_by_ref = {item.get("verification_ref"): item for item in verification_refs
                           if isinstance(item, Mapping)}
    verification_at_basis = []
    for condition in selected if isinstance(selected, list) else []:
        if not isinstance(condition, Mapping):
            continue
        receipt = verification_by_ref.get(condition.get("verification_ref"))
        if not receipt:
            continue
        matches = all(receipt.get(key) == condition.get(key) for key in (
            "definition_ref", "definition_version", "environment_id", "outcome", "state"))
        evidence_refs = receipt.get("evidence_refs", [])
        ready_evidence = bool(evidence_refs) and all(isinstance(ref, str) and conn.execute(
            "SELECT 1 FROM artifacts WHERE id=? AND scope_id=? AND state='ready'", (ref, req["scope_id"])
            ).fetchone() for ref in evidence_refs)
        if (matches and receipt.get("state") == "valid" and receipt.get("outcome") == "pass"
                and ready_evidence):
            verification_at_basis.append({"verification_ref": receipt["verification_ref"],
                "definition_ref": receipt["definition_ref"],
                "definition_version": receipt["definition_version"],
                "environment_id": receipt["environment_id"], "outcome": "pass",
                "evidence_refs": evidence_refs[:5], "basis_ref": basis_ref,
                "basis_hash": basis_hash})
    observed = bool(indexes)
    level = "verification_at_basis" if verification_at_basis else (
        "implementation_observed_at_basis" if observed else "planned" if has_plan else "unknown")
    newer_failures = []
    captured_at = basis.get("manifest", {}).get("captured_at") if isinstance(basis.get("manifest"), Mapping) else None
    for receipt in verification_refs:
        if (not isinstance(receipt, Mapping) or receipt.get("state") != "valid"
                or receipt.get("outcome") != "fail" or not captured_at):
            continue
        if receipt.get("completed_at") and receipt["completed_at"] > captured_at:
            newer_failures.append(receipt["verification_ref"])
    return {"level": level, "basis_ref": basis_ref, "basis_hash": basis_hash,
        "source_currentness": "not_revalidated_by_metadata_read",
        "implementation_refs": index_refs[:5], "change_refs": change_refs[:5],
        "assessment_refs": assessment_refs[:5], "verification_at_basis": verification_at_basis[:5],
        "reference_counts": {"implementation": len(index_refs), "changes": len(change_refs),
            "assessments": len(assessment_refs), "verification_at_basis": len(verification_at_basis)},
        "verification_after_basis_fail_refs": newer_failures,
        "current_applicability": "unknown_requires_read_applicability",
        "unknowns": (["basis_source_not_revalidated"] if basis.get("complete") is not True else []) +
                    (["no_same_basis_implementation_index"] if not indexes else [])}


def _facts(conn, req, db=None):
    scope_id = _scope_id(req)
    snap = _snapshot(conn, scope_id)
    from ..util import utc_now
    project = conn.execute("SELECT body_json FROM scopes WHERE id=?", (scope_id,)).fetchone()
    scope_body = json.loads(project["body_json"] or "{}") if project else {}
    summary_fields = ("summary", "goal", "premise", "non_goal", "autonomy", "delegation_scope",
                      "purpose", "constraints")
    direction = {key: str(scope_body[key])[:1000] for key in summary_fields
                 if isinstance(scope_body.get(key), str) and scope_body[key].strip()}
    work_items = []
    for row in snap["records"]:
        if row["kind"] not in {"work", "item", "step", "requirement"}:
            continue
        item = {"record_ref": row["id"], "kind": row["kind"], "title": row["title"][:240],
                "state": row["state"], "revision": row["revision"]}
        if row["kind"] in {"work", "item", "requirement"}:
            body = json.loads(conn.execute("SELECT body_json FROM records WHERE id=?", (row["id"],)).fetchone()[0] or "{}")
            for key in ("summary", "goal", "premise", "non_goal", "delegation_scope", "autonomy"):
                if isinstance(body.get(key), str) and body[key].strip():
                    item[key] = body[key][:800]
        work_items.append(item)
    decision_summaries = []
    for decision in snap["decisions"]:
        row = conn.execute("SELECT body_json FROM records WHERE id=?", (decision["id"],)).fetchone()
        body = json.loads(row["body_json"] or "{}") if row else {}
        summary = {key: body[key][:800] for key in (
            "content", "reason", "option_id", "delegation_scope", "watch", "next")
                   if isinstance(body.get(key), str) and body[key].strip()}
        if summary:
            decision_summaries.append({"decision_ref": decision["id"],
                                       "revision": decision["revision"], **summary})
    facts = {"scope_id": scope_id, "observed_at": utc_now(), "scope_selected": True,
             "project_ref": scope_id, "direction": direction,
             "work_items": work_items,
             "direction_refs": snap["direction_refs"], "decision_refs": snap["decisions"],
             "decision_summaries": decision_summaries,
             "current_status": snap["current_status"], "active_execution": snap["active_execution"],
             "verification_refs": snap["verification_refs"],
             "claim_refs": snap["claim_refs"], "pending_refs": snap["pending_refs"],
             "unknowns": ([] if snap["pending_observed"] else ["pending_snapshot_unavailable"])
                         + ["local_pending_spool_not_checked"],
             "complete": False,
             "revision_hash": snap["snapshot_hash"]}
    if db is not None:
        selected_basis = _selected_basis(db, conn, req)
        facts["implementation"] = _implementation_facts(db, conn, req, selected_basis,
            snap["verification_refs"], bool(work_items or direction))
    return facts


def _metadata_basis(db, conn, req, payload):
    """Capture a source-free planning basis from one actual PMT DB snapshot."""
    scope_id = _scope_id(req)
    snapshot = _snapshot(conn, scope_id)
    project = conn.execute("SELECT body_json FROM scopes WHERE id=?", (scope_id,)).fetchone()
    project_body = json.loads(project["body_json"] or "{}") if project else {}
    facts = _facts(conn, req, db)
    repository_id = project_body.get("repository_id")
    if not isinstance(repository_id, str):
        repository_id = None
    return basis_body(
        scope={"project_id": scope_id, "repository_id": repository_id,
               "direction": facts.get("direction", {}),
               "decision_summaries": facts.get("decision_summaries", [])},
        source={"repository_id": repository_id, "branch": None, "workspace_ref": None,
                "observed_head": None, "analyzed_ref": None, "applied_ref": None,
                "dirty_state": "unknown", "dirty_fingerprint": None,
                "inventory_ref": None, "inventory_hash": None,
                "inventory_coverage": {"selected_count": 0, "verified_count": 0,
                                       "unknown_count": 0, "complete": False,
                                       "reason_codes": ["source_not_selected"]}},
        contract={"graph_schema": None, "graph_revision": None, "graph_hash": None,
                  "requirement_refs": [], "decision_refs": snapshot["decisions"]},
        work={"capture_ref": snapshot["snapshot_hash"],
              "task_id": payload.get("task_id") or req.get("record_id"),
              "records": [{"id": key, "revision": value} for key, value in snapshot["revision_set"].items()
                          if not key.startswith(("run:", "pending:", "verification:", "step_spec:"))],
              "run_refs": snapshot["active_execution"], "claim_refs": snapshot["claim_refs"],
              "pending_refs": snapshot["pending_refs"]},
        conditions={"environment_id": db.environment_id, "selected": [],
                    "unknown": ["source_conditions_not_selected"]},
        manifest={"components": [
            {"name": "source", "captured_at": __import__("pmt.util", fromlist=["utc_now"]).utc_now(),
             "authority": "not_selected", "version": None, "complete": False},
            {"name": "work", "captured_at": __import__("pmt.util", fromlist=["utc_now"]).utc_now(),
             "authority": "sqlite_read_snapshot", "version": snapshot["snapshot_hash"], "complete": True}],
            "coherence": "incomplete", "reasons": ["source_not_selected"],
            "coherence_checks": {"work_snapshot_complete": True, "source_selected": False}})


def _planning_metadata_basis(basis):
    """Accept only an explicit source-free projection with a complete DB capture."""
    if not isinstance(basis, Mapping) or basis.get("complete") is not False:
        return False
    source, manifest, work = basis.get("source"), basis.get("manifest"), basis.get("work")
    if not all(isinstance(item, Mapping) for item in (source, manifest, work)):
        return False
    reasons = manifest.get("reasons")
    components = manifest.get("components")
    return (
        manifest.get("coherence") == "incomplete"
        and reasons == ["source_not_selected"]
        and isinstance(components, list)
        and any(item.get("name") == "work" and item.get("complete") is True for item in components if isinstance(item, Mapping))
        and source.get("branch") is None and source.get("workspace_ref") is None
        and source.get("observed_head") is None
        and source.get("inventory_coverage", {}).get("reason_codes") == ["source_not_selected"]
        and isinstance(work.get("capture_ref"), str)
    )


def checkpoint_boundary(db, conn, req, basis, *, allow_historical=False):
    """Resolve a checkpoint gate from persisted source records, never caller flags.

    Host adapters may call this helper inside their authenticated write
    transaction before invoking the same `handle` implementation.
    """
    purpose = _payload(req).get("purpose", "current")
    coherent_source_basis = (isinstance(basis, Mapping) and basis.get("complete") is True
                             and basis.get("manifest", {}).get("coherence") == "coherent")
    planning_basis = purpose in {"planning", "direction"} and _planning_metadata_basis(basis)
    if (not isinstance(basis, Mapping) or basis.get("scope", {}).get("project_id") != req.get("scope_id")
            or not (coherent_source_basis or planning_basis)):
        raise PmtError("basis_incomplete", "A complete coherent basis is required for a checkpoint", 3)
    event_id = _payload(req).get("boundary_event_id")
    if not isinstance(event_id, str):
        raise PmtError("checkpoint_boundary_required", "An actual confirmed boundary event is required")
    event = conn.execute("SELECT event_id,record_id,scope_id,event_type,new_revision,recorded_at,payload_json "
                         "FROM events "
                         "WHERE event_id=? AND scope_id=?", (event_id, req["scope_id"])).fetchone()
    # Events on a project classification are valid only when the record's
    # actual scope descends from the explicitly selected project.
    if event is None:
        event = conn.execute("SELECT event_id,record_id,scope_id,event_type,new_revision,recorded_at,payload_json "
                             "FROM events WHERE event_id=?", (event_id,)).fetchone()
    if not event:
        raise PmtError("checkpoint_boundary_not_found", "Boundary event is not present in the project", 3)
    from ..phase2_common import project_scope_id
    if project_scope_id(conn, event["scope_id"]) != req["scope_id"]:
        raise PmtError("checkpoint_boundary_scope_mismatch", "Boundary event is outside the selected project", 3)
    eligible = {"decision_saved", "record_changed", "task_finished", "scope_lock.acquired",
                "runner.handle_recorded", "runner.receipt_persisted", "execution.reviewed",
                "planning.result_reviewed", "planning.project_docs_published",
                "planning.graph_change_published"}
    if event["event_type"] not in eligible:
        raise PmtError("checkpoint_boundary_not_confirmed", "Event type does not confirm a checkpoint boundary", 3)
    if event["record_id"]:
        record = conn.execute("SELECT revision,scope_id FROM records WHERE id=?",
                              (event["record_id"],)).fetchone()
        if (not record or project_scope_id(conn, record["scope_id"]) != req["scope_id"]
                or (not allow_historical and event["new_revision"] is not None
                    and record["revision"] != event["new_revision"])):
            raise PmtError("checkpoint_boundary_stale", "Boundary event does not match the current record revision", 3)
    payload = json.loads(event["payload_json"] or "{}")
    run_id = payload.get("run_id")
    if event["event_type"] in {"scope_lock.acquired", "runner.handle_recorded", "runner.receipt_persisted",
                               "execution.reviewed", "planning.result_reviewed"}:
        run = conn.execute("SELECT id,step_id,state,revision,result_json,handle_json FROM execution_runs WHERE id=?",
                           (run_id,)).fetchone()
        if not run or (event["record_id"] and event["record_id"] != run["step_id"]):
            raise PmtError("checkpoint_boundary_stale", "Boundary event does not match an actual execution run", 3)
        if not allow_historical and event["event_type"] == "runner.handle_recorded" and not run["handle_json"]:
            raise PmtError("checkpoint_boundary_stale", "Runner handle was not retained", 3)
        if not allow_historical and event["event_type"] in {"runner.receipt_persisted", "execution.reviewed", "planning.result_reviewed"} \
                and not run["result_json"]:
            raise PmtError("checkpoint_boundary_stale", "Execution result is not retained", 3)
    if event["event_type"] == "planning.project_docs_published":
        plan = conn.execute("SELECT state,sha256 FROM plans WHERE id=? AND scope_id=?",
                            (payload.get("plan_id"), req["scope_id"])).fetchone()
        if not allow_historical and (not plan or plan["state"] != "published" or plan["sha256"] != payload.get("sha256")):
            raise PmtError("checkpoint_boundary_stale", "Published plan receipt no longer matches project state", 3)
    if event["event_type"] == "planning.graph_change_published":
        contract = basis.get("contract", {})
        if not allow_historical and (contract.get("graph_revision") != payload.get("graph_revision")
                or contract.get("graph_hash") != payload.get("graph_hash")):
            raise PmtError("checkpoint_boundary_stale", "Basis does not contain the graph published by this event", 3)
    return {"event_id": event["event_id"], "event_type": event["event_type"],
            "record_id": event["record_id"], "record_revision": event["new_revision"],
            "recorded_at": event["recorded_at"]}


def _selector(payload, basis):
    source = basis["source"]
    work = basis["work"]
    purpose = payload.get("purpose", "current")
    return {"repository_id": source.get("repository_id"), "branch": source.get("branch"),
            "workspace_ref": source.get("workspace_ref"),
            "task_id": None if purpose in {"planning", "direction"} else work.get("task_id"),
            "purpose": purpose,
            "environment_id": (basis.get("conditions", {}).get("environment_id")
                               if purpose in {"observation", "evidence"} else None)}


def _strict_relative(value, field):
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("-"):
        raise PmtError("invalid_inventory_path", f"{field} must be a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PmtError("invalid_inventory_path", f"{field} must stay inside the claimed workspace")
    return path.as_posix()


def _capture(db, req):
    from ..efficiency import graph
    from ..resources import _reject_links
    from ..efficiency.source import _run_git
    from ..util import utc_now

    payload, scope_id = _payload(req), _scope_id(req)
    repo_id, graph_path = payload.get("repository_id"), payload.get("relative_graph_path")
    if not isinstance(repo_id, str) or not isinstance(payload.get("workspace"), str):
        raise PmtError("basis_source_required", "Repository mapping and claimed workspace are required")
    graph_path = _strict_relative(graph_path, "relative_graph_path")
    selected = payload.get("inventory_paths", [graph_path])
    if not isinstance(selected, list) or not 1 <= len(selected) <= 512:
        raise PmtError("basis_inventory_invalid", "Select between 1 and 512 inventory paths")
    selected = list(dict.fromkeys(_strict_relative(item, "inventory_paths item") for item in selected))
    if graph_path not in selected:
        selected.append(graph_path)

    req = {**req, "scope_id": scope_id}
    workspace = payload["workspace"]
    own_effect = (str(uuid.uuid5(uuid.UUID(req["request_id"]), "continuity:basis_capture"))
                  if req.get("operation") == "capture_work_basis" else None)
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        auth = authorize(db, conn, req)
        run = work_access(db, conn, req, paths=selected, workspace=workspace)
        before = _snapshot(conn, scope_id, exclude_effect_id=own_effect)
    if os.path.normcase(os.path.realpath(run["workspace"])) != os.path.normcase(os.path.realpath(workspace)):
        raise PmtError("ownership_conflict", "Run workspace does not match the selected source")

    source_req = {**req, "payload": {**payload, "run_id": payload.get("run_id"),
                                      "repository_id": repo_id, "workspace": workspace,
                                      "relative_graph_path": graph_path}}
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        source = graph._source_graph(db, conn, source_req)
    root = Path(workspace).resolve(strict=True)
    branch = source["source_pin"].selected_ref
    branch_key = branch if branch is not None else (
        "detached:" + source["source_pin"].reviewed_commit if source["is_git"]
        else "non-git")
    from ..workspace import canonical_workspace
    workspace_ref = canonical_workspace(repo_id, branch_key)
    inventory_rows, inventory_items, reasons = [], [], []
    total_bytes = 0
    for relative in selected:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        try:
            _reject_links(root)
            _reject_links(candidate)
            candidate.resolve(strict=True).relative_to(root)
            if not candidate.is_file():
                raise OSError("not_regular_file")
            if candidate.stat().st_size > 8 * 1024 * 1024:
                raise OSError("file_too_large")
            raw = candidate.read_bytes()
            total_bytes += len(raw)
            if total_bytes > 64 * 1024 * 1024:
                raise OSError("inventory_too_large")
            sha = hashlib.sha256(raw).hexdigest()
            status = "verified"
            reason = None
        except (OSError, ValueError):
            sha, status, reason = None, "unknown", "inventory_source_unavailable"
            reasons.append(reason)
        inventory_rows.append({"path_ref": digest({"workspace_ref": workspace_ref,
                                                   "relative_path": relative}),
                               "content_hash": sha, "status": status, "reason_code": reason})
        inventory_items.append({"relative_path": relative, "content_hash": sha,
                                "status": status, "reason_code": reason})

    head = None
    if source["is_git"]:
        head = source["source_pin"].reviewed_commit
        statuses = []
        for relative in selected:
            status_result = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", relative, check=False)
            statuses.append(os.fsdecode(status_result.stdout))
        dirty_state = "dirty" if any(item.strip() for item in statuses) else source["source_pin"].dirty_state
        dirty_fingerprint = digest({"selected": list(zip(selected, statuses))}) if dirty_state == "dirty" else None
    else:
        dirty_state, dirty_fingerprint = "unknown", None
    inventory_hash = digest(inventory_rows)
    coverage = {"selected_count": len(selected),
                "verified_count": sum(item["status"] == "verified" for item in inventory_rows),
                "unknown_count": sum(item["status"] != "verified" for item in inventory_rows),
                "complete": not reasons and all(item["status"] == "verified" for item in inventory_rows),
                "reason_codes": sorted(set(reasons))}
    inventory_detail = {"version": 1, "workspace_ref": workspace_ref,
                        "inventory_hash": inventory_hash, "items": inventory_items}
    inventory_body_hash = digest(inventory_detail)
    inventory_identity = {"scope": scope_id, "kind": "detail", "body_hash": inventory_body_hash,
                          "visibility": "private", "event_id": None, "basis_hash": None,
                          "contract_version": "phase4-1",
                          "actor": req["actor"], "session": req["session_id"]}
    inventory_ref = str(uuid.uuid5(uuid.NAMESPACE_URL, "pmt:continuity:" + digest(inventory_identity)))
    baseline = None
    # Repeat each selected source observation and the actual owner claim after
    # file hashing. Git, the files and SQLite remain separate snapshots.
    final_items = []
    for relative in selected:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        path_ref = digest({"workspace_ref": workspace_ref, "relative_path": relative})
        try:
            _reject_links(candidate)
            candidate.resolve(strict=True).relative_to(root)
            if not candidate.is_file() or candidate.stat().st_size > 8 * 1024 * 1024:
                raise OSError("inventory_source_unavailable")
            raw = candidate.read_bytes()
            final_items.append({"path_ref": path_ref, "content_hash": hashlib.sha256(raw).hexdigest(),
                                "status": "verified", "reason_code": None})
        except (OSError, ValueError):
            final_items.append({"path_ref": path_ref, "content_hash": None,
                                "status": "unknown", "reason_code": "inventory_source_unavailable"})
    final_inventory_hash = digest(final_items)
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        authorize(db, conn, req)
        work_access(db, conn, req, paths=selected, workspace=workspace)
        final_source = graph._source_graph(db, conn, source_req)
        row = conn.execute("SELECT reviewed_commit,selected_ref,fingerprint,revision FROM project_baselines "
                           "WHERE scope_id=?", (scope_id,)).fetchone()
        if row:
            baseline = dict(row)
        after = _snapshot(conn, scope_id, exclude_effect_id=own_effect)
    coherence_reasons = []
    if before["snapshot_hash"] != after["snapshot_hash"]:
        coherence_reasons.append("work_state_changed_during_capture")
    source_stable = source["source_pin"].source_hash == final_source["source_pin"].source_hash
    inventory_stable = inventory_hash == final_inventory_hash
    if not source_stable:
        coherence_reasons.append("source_changed_during_capture")
    if not inventory_stable:
        coherence_reasons.append("inventory_changed_during_capture")
    if not coverage["complete"]:
        coherence_reasons.extend(coverage["reason_codes"] or ["inventory_incomplete"])
    components = [
        {"name": "source", "captured_at": utc_now(), "authority": "current_work_access",
         "version": head or "non_git", "complete": coverage["complete"] and source_stable and inventory_stable},
        {"name": "work", "captured_at": utc_now(), "authority": "sqlite_read_snapshot",
         "version": before["snapshot_hash"], "complete": before["snapshot_hash"] == after["snapshot_hash"]},
    ]
    coherent = not coherence_reasons
    contract = {"graph_schema": source["source_pin"].graph_schema,
                "graph_revision": source["source_pin"].graph_revision,
                "graph_hash": source["source_pin"].graph_hash,
                "requirement_refs": [], "decision_refs": before["decisions"]}
    basis_work = {"capture_ref": before["snapshot_hash"],
                  "task_id": payload.get("task_id") or req.get("record_id"),
                  "records": [{"id": key, "revision": value} for key, value in before["revision_set"].items()
                              if not key.startswith(("run:", "pending:"))],
                  "run_refs": before["active_execution"], "claim_refs": before["claim_refs"],
                  "pending_refs": before["pending_refs"]}
    requested_conditions = payload.get("condition_refs", [])
    if not isinstance(requested_conditions, list) or len(requested_conditions) > 200 or any(
            not isinstance(item, str) or not item for item in requested_conditions):
        raise PmtError("basis_conditions_invalid", "condition_refs must be a bounded list of references")
    selected_conditions, unknown_conditions = [], []
    from ..phase2_common import project_scope_id
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        authorize(db, conn, req)
        for condition_ref in requested_conditions:
            row = conn.execute("SELECT id,definition_id,definition_version,environment_id,outcome,state,target_id "
                               "FROM verifications WHERE id=?", (condition_ref,)).fetchone()
            target = conn.execute("SELECT scope_id FROM records WHERE id=?", (row["target_id"],)).fetchone() if row else None
            if row and target and project_scope_id(conn, target["scope_id"]) == scope_id:
                selected_conditions.append({"verification_ref": row["id"], "definition_ref": row["definition_id"],
                                            "definition_version": row["definition_version"],
                                            "environment_id": row["environment_id"],
                                            "outcome": row["outcome"], "state": row["state"]})
            else:
                unknown_conditions.append("condition_reference_unavailable")
    if not requested_conditions:
        unknown_conditions.append("conditions_not_selected")
    conditions = {"environment_id": db.environment_id, "selected": selected_conditions,
                  "unknown": sorted(set(unknown_conditions))}
    basis = basis_body(
        scope={"project_id": scope_id, "repository_id": repo_id},
        source={"repository_id": repo_id, "branch": branch,
                "workspace_ref": workspace_ref, "observed_head": head,
                "analyzed_ref": baseline["reviewed_commit"] if baseline else None,
                "applied_ref": None,
                "dirty_state": dirty_state, "dirty_fingerprint": dirty_fingerprint,
                "inventory_ref": inventory_ref, "inventory_hash": inventory_hash,
                "inventory_coverage": coverage},
        contract=contract, work=basis_work, conditions=conditions,
        manifest={"components": components, "coherence": "coherent" if coherent else "incomplete",
                  "reasons": sorted(set(coherence_reasons)), "captured_at": utc_now(),
                  "coherence_checks": {"work_before_after_equal": before["snapshot_hash"] == after["snapshot_hash"],
                                       "source_capture_complete": coverage["complete"],
                                       "source_before_after_equal": source_stable,
                                       "inventory_before_after_equal": inventory_stable}})
    return basis, {"run": run, "source_pin": source["source_pin"].to_dict(),
                   "inventory_manifest_hash": inventory_hash, "snapshot_before": before,
                   "snapshot_after": after, "baseline_revision": baseline["revision"] if baseline else None,
                   "inventory_detail": inventory_detail}


def handle(db, conn, req):
    operation, payload = req.get("operation"), _payload(req)
    authorize(db, conn, req)
    store = ContinuityStore(db)
    if operation == "read_current_facts":
        facts = _facts(conn, req, db)
        return {"facts_ref": "facts:sha256:" + digest(facts), "facts": facts,
                "complete": facts["complete"],
                "metadata_only": True, "private_directive_included": False}
    if operation == "read_checkpoint":
        if payload.get("object_ref"):
            value = store.get(conn, req, payload["object_ref"], kind="checkpoint")
            return {"checkpoint": value["body"], "checkpoint_ref": value["id"], "current_pointer": False}
        selector = payload.get("selector")
        pointer = store.read_pointer(conn, req, selector)
        if not pointer["object_id"]:
            return {"checkpoint": None, "pointer": pointer, "complete": True}
        value = store.get(conn, req, pointer["object_id"], kind="checkpoint")
        return {"checkpoint": value["body"], "checkpoint_ref": value["id"],
                "pointer": pointer, "current_pointer": True}
    if operation == "validate_basis":
        raise PmtError("basis_validation_requires_source_capture", "Validate basis is available through the source capture boundary")
    if operation == "create_checkpoint":
        basis_ref = payload.get("basis_ref")
        if basis_ref:
            basis_obj = store.get(conn, req, basis_ref, kind="basis")
            basis = basis_obj["body"]
        elif payload.get("purpose", "current") in {"planning", "direction"}:
            basis = _metadata_basis(db, conn, req, payload)
            basis_obj = store.put(conn, req, "basis", basis, basis_hash=digest(basis))
            basis_ref = basis_obj["id"]
        else:
            raise PmtError("basis_required", "A captured basis is required for this checkpoint purpose", 3)
        existed = conn.execute("SELECT object_id FROM continuity_events WHERE scope_id=? AND kind='checkpoint' AND event_id=?",
                               (req["scope_id"], payload.get("boundary_event_id"))).fetchone()
        if not existed and basis.get("work", {}).get("capture_ref") != _snapshot(conn, req["scope_id"])["snapshot_hash"]:
            raise PmtError("checkpoint_basis_stale", "Work revisions changed after basis capture", 3)
        boundary = checkpoint_boundary(db, conn, req, basis, allow_historical=bool(existed))
        event_id = boundary["event_id"]
        selector = _selector(payload, basis)
        pointer = store.read_pointer(conn, req, selector)
        expected = payload.get("expected_pointer_revision")
        if type(expected) is not int or expected < 0:
            raise PmtError("invalid_pointer_revision", "expected_pointer_revision must be nonnegative")
        evidence_refs = payload.get("evidence_refs", [])
        if not isinstance(evidence_refs, list) or len(evidence_refs) > 500 or any(
                not isinstance(ref, str) or not ref for ref in evidence_refs):
            raise PmtError("checkpoint_evidence_invalid", "Evidence refs must be a bounded list of references")
        from ..phase2_common import project_scope_id
        for ref in evidence_refs:
            artifact = conn.execute("SELECT scope_id,state FROM artifacts WHERE id=?", (ref,)).fetchone()
            verification = conn.execute("SELECT v.state,v.outcome,r.scope_id FROM verifications v "
                                         "JOIN records r ON r.id=v.target_id WHERE v.id=?", (ref,)).fetchone()
            continuity = conn.execute("SELECT kind FROM continuity_objects WHERE id=? AND scope_id=?",
                                      (ref, req["scope_id"])).fetchone()
            if not ((artifact and artifact["state"] == "ready"
                     and project_scope_id(conn, artifact["scope_id"]) == req["scope_id"])
                    or (verification and verification["state"] == "valid" and verification["outcome"] == "pass"
                        and project_scope_id(conn, verification["scope_id"]) == req["scope_id"])
                    or continuity and continuity["kind"] in {"assessment", "applicability", "alignment"}):
                raise PmtError("checkpoint_evidence_unavailable", "Evidence reference is not retained in this project", 3)
        unresolved = list(dict.fromkeys(
            list(basis.get("manifest", {}).get("reasons", []))
            + list(basis.get("conditions", {}).get("unknown", []))))
        if basis.get("work", {}).get("run_refs"):
            unresolved.append("existing_execution_requires_current_observation")
        if basis.get("work", {}).get("pending_refs"):
            unresolved.append("pending_effect_requires_reconciliation")
        checkpoint = {"version": 1, "basis_ref": basis_ref, "basis_hash": basis_obj["body_hash"],
                      "basis_complete": basis.get("complete") is True,
                      "source_currentness": "captured" if basis.get("complete") is True else "unknown",
                      "boundary_event_ref": event_id, "boundary_kind": boundary["event_type"],
                      "scope": basis["scope"], "source": basis["source"],
                      "contract": basis["contract"], "work": basis["work"],
                      "facts_ref": basis.get("work", {}).get("capture_ref"),
                      "conditions": basis.get("conditions", {}),
                      "decision_refs": basis["contract"].get("decision_refs", []),
                      "active_execution": basis.get("work", {}).get("run_refs", []),
                      "claim_refs": basis.get("work", {}).get("claim_refs", []),
                      "pending_refs": basis.get("work", {}).get("pending_refs", []),
                      "evidence_refs": sorted(set(evidence_refs)),
                      "unresolved": sorted(set(unresolved)),
                      "next_conditions": ["revalidate_current_authority_before_detail",
                                          "revalidate_basis_before_action"],
                      "pointer_selector": selector, "captured_at": boundary["recorded_at"]}
        checkpoint["parent_checkpoint_ref"] = pointer["object_id"]
        checkpoint["facts_ref"] = basis.get("work", {}).get("capture_ref")
        if existed:
            old = store.get(conn, req, existed["object_id"], kind="checkpoint")
            old_body = old["body"]
            checkpoint["parent_checkpoint_ref"] = old_body.get("parent_checkpoint_ref")
            stable_fields = ("boundary_event_ref", "boundary_kind", "scope", "source", "contract", "work", "conditions",
                             "parent_checkpoint_ref",
                             "basis_complete", "source_currentness",
                             "facts_ref",
                             "decision_refs", "active_execution", "claim_refs", "pending_refs",
                             "evidence_refs", "unresolved", "next_conditions", "pointer_selector", "captured_at")
            stable = all(old_body.get(field) == checkpoint.get(field)
                         for field in stable_fields if field != "source")
            old_source = {key: value for key, value in old_body.get("source", {}).items()
                          if key != "inventory_ref"}
            new_source = {key: value for key, value in checkpoint.get("source", {}).items()
                          if key != "inventory_ref"}
            if stable and old_source == new_source:
                value = old
            else:
                raise PmtError("event_conflict", "Boundary event is already bound to different checkpoint meaning", 3)
        else:
            value = store.put(conn, req, "checkpoint", checkpoint,
                              basis_hash=basis_obj["body_hash"], event_id=event_id)
        current = store.read_pointer(conn, req, selector)
        if current["object_id"] == value["id"] or (existed and current["object_id"] != value["id"]):
            pointer_receipt = current | {"replayed": True}
        else:
            pointer_receipt = store.advance_pointer(conn, req, selector, value["id"], expected)
            pointer_receipt["replayed"] = False
        return {"checkpoint_ref": value["id"], "basis_ref": basis_ref,
                "pointer": pointer_receipt, "owner_changed": False,
                "execution_state_changed": False}
    if operation == "link_session":
        checkpoint_ref = payload.get("checkpoint_ref")
        checkpoint = store.get(conn, req, checkpoint_ref, kind="checkpoint")
        session_identity = {"actor_hash": digest(req["actor"]), "session_hash": digest(req["session_id"])}
        body = {"version": 1, "project_ref": req["scope_id"], "checkpoint_ref": checkpoint["id"],
                "basis_ref": checkpoint["body"].get("basis_ref"), "session": session_identity,
                "owner_transfer": False, "execution_grant": False}
        event_id = payload.get("event_id")
        result = store.put(conn, req, "session_link", body, event_id=event_id)
        return {"session_link_ref": result["id"], "checkpoint_ref": checkpoint["id"],
                "owner_transfer": False, "execution_grant": False}
    raise PmtError("operation_unsupported", "Unsupported current facts operation")


def execute_file(db, req):
    from ..service import response
    operation, payload = req.get("operation"), _payload(req)
    effect_id = None
    try:
        def authorize_work(conn, request):
            authorize(db, conn, request)
            if request.get("operation") in {"capture_work_basis", "validate_basis"}:
                payload_now = _payload(request)
                paths_now = payload_now.get("inventory_paths") or [payload_now.get("relative_graph_path", ".")]
                paths_now = [_strict_relative(item, "inventory_paths item") for item in paths_now]
                graph_path_now = _strict_relative(payload_now.get("relative_graph_path"), "relative_graph_path")
                if graph_path_now not in paths_now:
                    paths_now.append(graph_path_now)
                work_access(db, conn, request, paths=paths_now,
                            workspace=payload_now.get("workspace"))

        if operation == "capture_work_basis":
            effect_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "continuity:basis_capture"))
            with db.write() as conn:
                authorize_work(conn, req)
                previous = db.get_request_result(req["request_id"], req.get("actor"),
                                                 req.get("session_id"), expected_request=req)
                if previous:
                    return previous
                store = ContinuityStore(db)
                existing = conn.execute("SELECT id FROM continuity_journal WHERE id=?", (effect_id,)).fetchone()
                if existing:
                    effect = store.get_effect(conn, req, effect_id)
                    if effect["state"] == "completed":
                        raise PmtError("effect_response_missing", "Completed basis effect has no original request receipt", 5)
                    expected_body = {"repository_id": payload.get("repository_id"),
                                     "run_id": payload.get("run_id"),
                                     "graph_path_ref": digest(payload.get("relative_graph_path")),
                                     "inventory_selection_hash": digest(payload.get("inventory_paths", []))}
                    if effect["body"] != expected_body:
                        raise PmtError("request_conflict", "Original basis effect is bound to another source selection", 3)
                else:
                    store.begin_effect(conn, req, "basis_capture",
                                       {"repository_id": payload.get("repository_id"),
                                        "run_id": payload.get("run_id"),
                                        "graph_path_ref": digest(payload.get("relative_graph_path")),
                                        "inventory_selection_hash": digest(payload.get("inventory_paths", []))})
                store.update_effect(conn, req, effect_id, "applying")
        basis, evidence = _capture(db, req)
        if operation == "validate_basis":
            def commit_validation(conn, request):
                authorize(db, conn, request)
                basis_ref = payload.get("basis_ref")
                stored = ContinuityStore(db).get(conn, request, basis_ref, kind="basis")
                comparison = validate_basis_components(stored["body"], basis)
                return {"basis_ref": basis_ref, "current_basis_hash": digest(basis),
                          **comparison, "capture_complete": basis.get("complete") is True,
                          "reasons": basis.get("manifest", {}).get("reasons", [])}
            return db.run_request(req, commit_validation,
                                  authorize=authorize_work)

        def commit_capture(conn, request):
            authorize(db, conn, request)
            store = ContinuityStore(db)
            inventory = store.put(conn, request, "detail", evidence["inventory_detail"], visibility="private")
            if inventory["id"] != basis["source"]["inventory_ref"]:
                raise PmtError("inventory_reference_mismatch", "Retained inventory ref does not match the captured basis", 5,
                                details={"retained_ref": inventory["id"],
                                         "basis_ref": basis["source"]["inventory_ref"]})
            stable_id = str(uuid.uuid5(uuid.UUID(request["request_id"]), "continuity:basis"))
            stored = store.put(conn, request, "basis", basis, object_id=stable_id, basis_hash=digest(basis))
            store.update_effect(conn, request, effect_id, "completed", {"basis_ref": stored["id"],
                                "basis_hash": stored["body_hash"], "complete": basis["complete"],
                                "inventory_ref": inventory["id"]})
            return {"basis_ref": stored["id"], "basis_hash": stored["body_hash"],
                    "basis": basis, "source_pin": evidence["source_pin"],
                    "complete": basis["complete"],
                    "inventory_coverage": basis["source"]["inventory_coverage"]}

        result, code = db.run_request(req, commit_capture,
                                      authorize=authorize_work)
        if code != 0 or not result.get("ok"):
            try:
                with db.write() as conn:
                    authorize(db, conn, req)
                    store = ContinuityStore(db)
                    row = conn.execute("SELECT id FROM continuity_journal WHERE id=?", (effect_id,)).fetchone()
                    if row:
                        prior_effect = store.get_effect(conn, req, effect_id)
                        if prior_effect["state"] != "completed":
                            store.update_effect(conn, req, effect_id, "unknown",
                                                {"reason_code": (result.get("error") or {}).get("code", "request_failed")})
            except (PmtError, OSError):
                db.diagnostics.emit("continuity.basis_effect_recovery_required", request_id=req.get("request_id"),
                                    scope_id=req.get("scope_id"), outcome="unknown")
            return result, code
        db.diagnostics.emit("continuity.basis_captured", request_id=req["request_id"],
                            scope_id=req.get("scope_id"), basis_ref=result["result"]["basis_ref"],
                            basis_hash=result["result"]["basis_hash"], complete=basis["complete"],
                            inventory_hash=basis["source"]["inventory_hash"],
                            selected_count=basis["source"]["inventory_coverage"]["selected_count"],
                            unknown_count=basis["source"]["inventory_coverage"]["unknown_count"])
        return result, code
    except PmtError as exc:
        if effect_id is not None:
            try:
                with db.write() as conn:
                    authorize(db, conn, req)
                    store = ContinuityStore(db)
                    row = conn.execute("SELECT id FROM continuity_journal WHERE id=?", (effect_id,)).fetchone()
                    if row:
                        effect = store.get_effect(conn, req, effect_id)
                        if effect["state"] != "completed":
                            store.update_effect(conn, req, effect_id, "unknown",
                                                {"reason_code": exc.code})
            except (PmtError, OSError):
                db.diagnostics.emit("continuity.basis_effect_recovery_required", request_id=req.get("request_id"),
                                    scope_id=req.get("scope_id"), outcome="unknown")
        return response(req.get("request_id"), error=exc.as_dict()), exc.exit_code
    except (OSError, ValueError, TypeError, KeyError) as exc:
        error = PmtError("basis_capture_failed", "Current source basis could not be captured safely", 4, True)
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code
    except sqlite3.Error as exc:
        error = db._sqlite_error(exc)
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code
