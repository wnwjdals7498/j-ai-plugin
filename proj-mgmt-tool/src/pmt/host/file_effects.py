"""Host-side metadata receipts for client-local graph and document file effects.

This module never reads or writes a client workspace. The authenticated Host
checks its current run, scope lock, SourcePin, and registered metadata only.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import PurePosixPath

from ..efficiency.source import pin_source, verify_source_pin
from ..efficiency.storage import Phase3Storage
from ..errors import PmtError
from ..phase2_common import require_workspace_claim
from ..planning.graph import validate_graph
from ..util import canonical_json, fingerprint, utc_now

READ_OPERATIONS = frozenset({"read_local_file_effect"})
WRITE_OPERATIONS = frozenset({"begin_local_file_effect", "complete_local_file_effect"})
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS
_KIND = "host_local_file_effect"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = frozenset({"intent", "candidate_staged", "original_preserved", "target_detached",
    "candidate_published", "source_published", "manifests_registered", "durability_unknown",
    "durability_unsupported", "reconcile_required", "conflict", "completed"})
_STATES = frozenset({"intent", "reconcile_required", "conflict", "completed"})
_SUMMARY_KEYS = frozenset({"operation_count", "node_count", "relation_count", "segment_count",
    "changed_segment_count", "manual_segment_count"})
_PUBLICATION_KEYS = frozenset({"status", "target_ref", "candidate_ref", "candidate_stage_ref", "manifest_ref",
    "effect_id", "expected_hash", "original_hash", "candidate_hash", "recovery_ref", "phase", "durability_warning"})


def _fail(code, message, exit_code=3, details=None, retryable=False):
    raise PmtError(code, message, exit_code, retryable, details)


def _uuid(value, name):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise PmtError("host_file_effect_invalid", f"{name} must be a canonical UUID") from exc
    return value


def _hash(value, name, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        _fail("host_file_effect_invalid", f"{name} must be a lowercase SHA-256 digest")
    return value


def _relative(value, name):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value or ":" in value:
        _fail("host_file_effect_invalid", f"{name} must be workspace-relative POSIX syntax")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        _fail("host_file_effect_invalid", f"{name} must remain inside the canonical workspace")
    return path.as_posix()


def _pin(value, name="expected_source"):
    try:
        return pin_source(value)
    except PmtError as exc:
        raise PmtError("host_file_effect_invalid", f"{name} is invalid", 3) from exc


def _request_identity(extension, req, principal):
    if (req.get("actor") != principal.actor or req.get("session_id") != principal.session_id
            or not principal.device_id or not principal.environment_id):
        _fail("unauthenticated", "Current Host request owner is not authenticated", 3)
    return {"namespace_id": extension.auth.namespace_id, "actor": principal.actor,
        "device_id": principal.device_id, "environment_id": principal.environment_id,
        "session_id": principal.session_id}


def _common(extension, conn, req, principal, *, source_capture=False):
    payload = req.get("payload")
    if not isinstance(payload, dict):
        _fail("host_file_effect_invalid", "payload must be an object", 2)
    identity = _request_identity(extension, req, principal)
    project_id = _uuid(payload.get("project_id") or req.get("scope_id"), "project_id")
    repository_id = _uuid(payload.get("repository_id"), "repository_id")
    if req.get("scope_id") != project_id:
        _fail("scope_forbidden", "File effect must use the selected Project scope", 3)
    run_id = _uuid(payload.get("run_id"), "run_id")
    revision = payload.get("expected_run_revision")
    if type(revision) is not int or revision < 1:
        _fail("execution_revision_conflict", "A current positive run revision is required", 3)
    workspace = payload.get("canonical_workspace")
    relative_graph_path = _relative(payload.get("relative_graph_path"), "relative_graph_path")
    target = _relative(payload.get("target_relative_path"), "target_relative_path")
    branch_key = payload.get("branch_key")
    if not isinstance(branch_key, str) or not branch_key or len(branch_key) > 1024:
        _fail("workspace_mapping_invalid", "File effect requires the selected branch key", 3)
    binding = extension._workspace_binding(conn, req, principal,
        mode="source_capture" if source_capture else "execute", allow_source_capture=source_capture)
    if (binding["project_id"] != project_id or binding["repository_id"] != repository_id
            or binding["canonical_workspace"] != workspace or binding["relative_graph_path"] != relative_graph_path
            or binding["run"]["id"] != run_id or binding["run"]["revision"] != revision):
        _fail("workspace_authority_stale", "Current Host run, project, or canonical workspace changed", 3)
    if binding["run"].get("owner_session") != principal.session_id:
        _fail("ownership_conflict", "File effect requires the current run owner", 3)
    require_workspace_claim(extension._db_for(conn, req), conn, req, workspace,
                            [relative_graph_path, target])
    source = extension._current_source_pointer(conn, binding, required=not source_capture)
    current_pin = _pin(source["body"].get("source_pin")) if source else None
    expected = _pin(payload.get("expected_source"))
    if (expected.repository_id != repository_id or expected.project_id != project_id
            or expected.source_kind not in {"git", "non_git"}):
        _fail("source_pin_invalid", "File effect requires a known current SourcePin", 3)
    if not source_capture and current_pin is not None:
        verify_source_pin(expected, current_pin)
    if workspace != binding["canonical_workspace"]:
        _fail("workspace_mapping_invalid", "Canonical workspace URI does not match this run", 3)
    return {"identity": identity, "project_id": project_id, "repository_id": repository_id,
        "run_id": run_id, "run_revision": revision, "workspace": workspace,
        "relative_graph_path": relative_graph_path, "target_relative_path": target,
        "expected_source": expected.to_dict(), "current_source": current_pin.to_dict() if current_pin else None,
        "binding": binding}


def _summary(value):
    if not isinstance(value, dict) or set(value) - _SUMMARY_KEYS:
        _fail("host_file_effect_invalid", "Effect summary contains unsupported fields")
    result = {}
    for key, item in value.items():
        if type(item) is not int or item < 0 or item > 100000:
            _fail("host_file_effect_invalid", f"{key} must be a bounded nonnegative integer")
        result[key] = item
    return result


def _intent(payload, context, before_graph_resource_ref=None, before_source_pointer=None):
    if payload.get("effect_kind") not in {"graph_change", "document_render"}:
        _fail("host_file_effect_invalid", "effect_kind must be graph_change or document_render")
    if not isinstance(payload.get("producer_version"), str) or not payload["producer_version"].strip() or len(payload["producer_version"]) > 100:
        _fail("host_file_effect_invalid", "producer_version must be bounded text")
    return {"schema_version": 1, "effect_id": _uuid(payload.get("effect_id"), "effect_id"),
        "effect_kind": payload["effect_kind"], "owner": context["identity"],
        "project_id": context["project_id"], "repository_id": context["repository_id"],
        "canonical_workspace": context["workspace"], "relative_graph_path": context["relative_graph_path"],
        "target_relative_path": context["target_relative_path"], "run_id": context["run_id"],
        "run_revision_at_begin": context["run_revision"], "before_source_pin": context["expected_source"],
        "before_graph_resource_ref": before_graph_resource_ref,
        "before_source_pointer": before_source_pointer,
        "expected_target_sha256": _hash(payload.get("expected_target_sha256"), "expected_target_sha256", nullable=True),
        "candidate_sha256": _hash(payload.get("candidate_sha256"), "candidate_sha256"),
        "semantic_sha256": _hash(payload.get("semantic_sha256"), "semantic_sha256"),
        "producer_version": payload["producer_version"], "summary": _summary(payload.get("summary", {})),
        "phase": "intent", "state": "intent", "publication": None,
        "after_source_pin": None, "uploaded_source_ref": None, "manifest_refs": [],
        "coverage_ref": None, "host_document_verified": False, "history": []}


def _same_intent(old, new):
    mutable = {"phase", "state", "publication", "after_source_pin", "uploaded_source_ref",
               "manifest_refs", "coverage_ref", "host_document_verified", "history"}
    return {key: value for key, value in old.items() if key not in mutable} == {
        key: value for key, value in new.items() if key not in mutable}


def _safe_publication(value, body):
    if not isinstance(value, dict) or set(value) - _PUBLICATION_KEYS:
        _fail("host_file_effect_receipt_invalid", "Publication receipt has unsupported fields", 2)
    result = dict(value)
    if result.get("effect_id") != body["effect_id"]:
        _fail("host_file_effect_receipt_invalid", "Publication receipt effect ID differs from its intent", 3)
    if result.get("target_ref") != body["target_relative_path"]:
        _fail("host_file_effect_receipt_invalid", "Publication target ref differs from the immutable intent", 3)
    for key in ("candidate_ref", "candidate_stage_ref", "manifest_ref", "recovery_ref"):
        if result.get(key) is not None:
            result[key] = _relative(result[key], "publication." + key)
    for key in ("expected_hash", "original_hash", "candidate_hash"):
        if key in result:
            result[key] = _hash(result[key], "publication." + key, nullable=True)
    if result.get("expected_hash") != body["expected_target_sha256"] or result.get("candidate_hash") != body["candidate_sha256"]:
        _fail("host_file_effect_receipt_invalid", "Publication receipt hash differs from the immutable intent", 3)
    if result.get("status") not in {"published", "replayed", "conflict"}:
        _fail("host_file_effect_receipt_invalid", "Publication status is unsupported", 2)
    if result.get("durability_warning") is not None and result["durability_warning"] != "directory_fsync_unsupported":
        _fail("host_file_effect_receipt_invalid", "Durability warning is unsupported", 2)
    return result


def _current_graph(extension, conn, context, headers):
    binding = context["binding"]
    pointer = extension._current_source_pointer(conn, binding, required=True)
    return extension._source_from_pointer(conn, binding, pointer, headers), pointer


def _verified_f3_impact(extension, db, conn, req, principal, headers, context, body, payload,
                        manifests, coverage):
    """Recompute F3 against Host-pinned before graph and actual old manifests."""
    from ..efficiency import graph as graph_ops
    required = {"graph_effect_id", "change_set", "change_preview", "impact_set"}
    if not required <= set(payload):
        _fail("host_file_effect_f3_invalid", "F3 verification inputs are incomplete", 2)
    effect_id = _uuid(payload["graph_effect_id"], "graph_effect_id")
    effect = Phase3Storage(db).get_object(_KIND, effect_id, context["project_id"],
        principal.actor, principal.session_id, conn=conn)
    if not effect or effect["state"] != "completed":
        _fail("host_file_effect_f3_unavailable", "F1 effect is not a completed Host-owned graph change", 3)
    effect_body = effect["body"]
    prior_pin = pin_source(effect_body.get("before_source_pin"))
    current_pin = pin_source(context["current_source"])
    if (effect_body.get("effect_kind") != "graph_change"
            or effect_body.get("after_source_pin", {}).get("source_hash") != current_pin.source_hash
            or effect_body.get("run_id") != context["run_id"]
            or effect_body.get("canonical_workspace") != context["workspace"]
            or effect_body.get("target_relative_path") != context["relative_graph_path"]
            or effect_body.get("owner") != context["identity"]
            or effect_body.get("semantic_sha256") != fingerprint(payload["change_set"])):
        _fail("host_file_effect_f3_mismatch", "F1 effect does not match current owner, source, or ChangeSet", 3)
    pointer_ref = effect_body.get("before_source_pointer")
    before_ref = effect_body.get("before_graph_resource_ref")
    if (not isinstance(pointer_ref, dict) or pointer_ref.get("source_hash") != prior_pin.source_hash
            or not isinstance(before_ref, dict) or before_ref != pointer_ref.get("graph_resource_ref")
            or before_ref.get("purpose") != "graph_snapshot" or before_ref.get("scope_id") != context["project_id"]):
        _fail("host_file_effect_f3_source_missing", "Immutable F1 before graph/source pointer is unavailable", 3)
    before_resource = extension._read_resource(before_ref, context["project_id"], headers)
    raw = before_resource.get("content")
    if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != before_ref.get("sha256") or len(raw) != before_ref.get("size"):
        _fail("host_file_effect_f3_source_corrupt", "F1 before graph resource failed Host hash verification", 3)
    try:
        before_graph = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PmtError("host_file_effect_f3_source_invalid", "F1 before graph resource is not valid JSON", 3) from exc
    report = validate_graph(before_graph, context["project_id"], complete=False)
    if (report.get("sha256") != prior_pin.graph_hash or report.get("graph_version") != prior_pin.graph_revision
            or report.get("schema_version") != prior_pin.graph_schema):
        _fail("host_file_effect_f3_source_mismatch", "F1 before graph bytes differ from its SourcePin", 3)
    current_graph, current_pointer = _current_graph(extension, conn, context, headers)
    uploaded = effect_body.get("uploaded_source_ref")
    if (not isinstance(uploaded, dict) or current_pointer["body"].get("graph_resource") != uploaded
            or effect_body.get("candidate_sha256") != uploaded.get("sha256")):
        _fail("host_file_effect_f3_candidate_mismatch", "F1 candidate is not the current registered Host graph", 3)
    if (effect_body.get("publication", {}).get("candidate_hash") != effect_body.get("candidate_sha256")
            or effect_body.get("publication", {}).get("status") not in {"published", "replayed"}):
        _fail("host_file_effect_f3_publication_mismatch", "F1 publication receipt is not a successful candidate publish", 3)

    prior_manifests = {key: item for key, item in manifests.items()
        if item.get("source_hash") == prior_pin.source_hash
        and item.get("manifest", {}).get("source_pin", {}).get("source_hash") == prior_pin.source_hash}
    before_coverage = graph_ops._stored_coverage(conn, context["project_id"], prior_pin,
        graph_ops._index_body(before_graph, prior_pin, prior_manifests, False), prior_manifests)
    before_index = graph_ops._index_body(before_graph, prior_pin, prior_manifests, False, before_coverage)
    impact_request = dict(req)
    impact_request["operation"] = "calculate_graph_impact"
    impact_request["payload"] = {"expected_source": prior_pin.to_dict(),
        "change_set": payload["change_set"], "change_preview": payload["change_preview"],
        "rule_version": graph_ops._FIELD_RULE_VERSION, "max_depth": 4}
    verified = graph_ops._calculate_impact(db, conn, impact_request,
        {"scope_id": context["project_id"], "source_pin": prior_pin, "graph": before_graph},
        index_override=({"revision": pointer_ref["revision"]}, before_index))
    submitted = payload["impact_set"]
    if not isinstance(submitted, dict):
        _fail("host_file_effect_f3_invalid", "impact_set must be an object", 2)
    semantic_keys = ("change_id", "change_set_hash", "source_pin", "before_source_pin", "expected_new_source",
        "rule_version", "field_changes", "known", "documents", "steps", "verifications", "unknown", "complete")
    if any(canonical_json(submitted.get(key)) != canonical_json(verified.get(key)) for key in semantic_keys):
        _fail("host_file_effect_f3_mismatch", "Submitted impact differs from Host-recomputed F3 result", 3)
    return {"impact": verified, "before_graph_resource_ref": before_ref,
        "before_source_pointer": pointer_ref, "provenance": "host_recomputed_core_f3"}


def authorize(extension, conn, req, principal):
    op = req.get("operation")
    if op not in OPERATIONS:
        _fail("host_operation_forbidden", "Local file effect operation is not registered", 3)
    principal.require("runtime")
    payload = req.get("payload")
    if not isinstance(payload, dict):
        _fail("host_file_effect_invalid", "payload must be an object", 2)
    source_capture = op == "read_local_file_effect"
    return _common(extension, conn, req, principal, source_capture=source_capture)


def execute_file(db_view, req):
    extension = getattr(db_view, "_extension", None)
    principal = getattr(db_view, "_principal", None)
    headers = getattr(db_view, "_headers", None)
    db = getattr(db_view, "_db", None)
    if extension is None or principal is None or headers is None or db is None:
        _fail("host_file_effect_unavailable", "Authenticated Host metadata view is unavailable", 5)
    from ..service import response
    try:
        operation = req.get("operation")
        payload = req.get("payload", {})
        if operation == "begin_local_file_effect":
            required = {"effect_id", "effect_kind", "run_id", "expected_run_revision", "repository_id", "project_id",
                "canonical_workspace", "relative_graph_path", "target_relative_path", "branch_key", "expected_source",
                "expected_target_sha256", "candidate_sha256", "semantic_sha256", "producer_version", "summary"}
            if not isinstance(payload, dict) or set(payload) != required:
                _fail("host_file_effect_invalid", "Begin effect fields are incomplete or unsupported", 2)
            holder = {}
            def begin(conn, request):
                context = _common(extension, conn, request, principal)
                pointer = extension._current_source_pointer(conn, context["binding"], required=True)
                before_graph_resource_ref = pointer["body"].get("graph_resource")
                if (not isinstance(before_graph_resource_ref, dict)
                        or before_graph_resource_ref.get("purpose") != "graph_snapshot"
                        or before_graph_resource_ref.get("scope_id") != context["project_id"]):
                    _fail("host_file_effect_source_unavailable", "Current graph resource is unavailable for safe recovery", 3)
                before_source_pointer = {"id": pointer["id"], "revision": pointer["revision"],
                    "source_hash": pointer["source_hash"], "graph_resource_ref": before_graph_resource_ref}
                intent = _intent(payload, context, before_graph_resource_ref, before_source_pointer)
                existing = Phase3Storage(db).get_object(_KIND, intent["effect_id"], context["project_id"],
                    principal.actor, principal.session_id, conn=conn)
                if existing:
                    if not _same_intent(existing["body"], intent):
                        _fail("host_file_effect_conflict", "Effect ID is already bound to different source or bytes", 3)
                    holder["result"] = {"effect_ref": {"kind": _KIND, "id": existing["id"],
                        "scope_id": context["project_id"], "revision": existing["revision"],
                        "source_hash": existing["source_hash"]}, "effect": existing["body"],
                        "effect_state": existing["state"], "replayed": True,
                        "host_document_verified": False}
                    return holder["result"]
                receipt = Phase3Storage(db).put_object(_KIND, intent["effect_id"], context["project_id"],
                    principal.actor, principal.session_id, context["expected_source"]["source_hash"], 0, intent,
                    state="intent", request_id=request["request_id"],
                    event_id=str(uuid.uuid5(uuid.UUID(request["request_id"]), "host-file-effect-begun")), conn=conn)
                holder["result"] = {"effect_ref": {"kind": _KIND, "id": intent["effect_id"],
                    "scope_id": context["project_id"], "revision": receipt["revision"],
                    "source_hash": context["expected_source"]["source_hash"]}, "effect": intent,
                    "effect_state": "intent", "replayed": False, "host_document_verified": False}
                return holder["result"]
            envelope, code = db_view.run_request(req, begin)
            return envelope, code
        if operation == "complete_local_file_effect":
            required = {"effect_id", "expected_effect_revision", "run_id", "expected_run_revision", "repository_id",
                "project_id", "canonical_workspace", "relative_graph_path", "target_relative_path", "branch_key",
                "expected_source", "phase", "state", "publication", "after_source_pin", "uploaded_source_ref",
                "manifest_refs", "coverage_ref", "host_document_verified"}
            if not isinstance(payload, dict) or set(payload) != required:
                _fail("host_file_effect_invalid", "Complete effect fields are incomplete or unsupported", 2)
            holder = {}
            def complete(conn, request):
                context = _common(extension, conn, request, principal)
                effect_id = _uuid(payload["effect_id"], "effect_id")
                storage = Phase3Storage(db)
                old = storage.get_object(_KIND, effect_id, context["project_id"], principal.actor,
                                         principal.session_id, conn=conn)
                if not old:
                    _fail("host_file_effect_not_found", "Local file effect intent is unavailable", 2)
                body = old["body"]
                if (body.get("owner") != context["identity"] or body.get("run_id") != context["run_id"]
                        or body.get("canonical_workspace") != context["workspace"]
                        or body.get("target_relative_path") != context["target_relative_path"]
                        or body.get("relative_graph_path") != context["relative_graph_path"]):
                    _fail("host_file_effect_owner_conflict", "Effect owner, run or target changed", 3)
                expected_rev = payload["expected_effect_revision"]
                if type(expected_rev) is not int or expected_rev != old["revision"]:
                    _fail("revision_conflict", "File effect revision changed", 3,
                          {"expected_revision": expected_rev, "current_revision": old["revision"]})
                if payload["phase"] not in _PHASES or payload["state"] not in _STATES:
                    _fail("host_file_effect_invalid", "Effect phase or state is unsupported", 2)
                if type(payload["host_document_verified"]) is not bool or payload["host_document_verified"] is not False:
                    _fail("host_file_effect_invalid", "Host cannot claim to have verified client document bytes", 3)
                publication = _safe_publication(payload["publication"], body) if payload["publication"] is not None else None
                after_pin = pin_source(payload["after_source_pin"]).to_dict() if payload["after_source_pin"] is not None else None
                if old["state"] in {"completed", "conflict"}:
                    same_terminal = (old["state"] == payload["state"] and body.get("phase") == payload["phase"]
                        and body.get("publication") == publication and body.get("after_source_pin") == after_pin
                        and body.get("uploaded_source_ref") == payload["uploaded_source_ref"]
                        and body.get("manifest_refs") == payload["manifest_refs"]
                        and body.get("coverage_ref") == payload["coverage_ref"]
                        and payload["host_document_verified"] is False)
                    if same_terminal:
                        holder["result"] = {"effect_ref": {"kind": _KIND, "id": effect_id,
                            "scope_id": context["project_id"], "revision": old["revision"],
                            "source_hash": old["source_hash"]}, "effect_state": old["state"],
                            "receipt": body.get("history", [])[-1] if body.get("history") else None,
                            "replayed": True, "host_document_verified": False}
                        return holder["result"]
                    _fail("host_file_effect_conflict", "A terminal file effect cannot be changed", 3)
                source = context["current_source"]
                if after_pin:
                    if not source:
                        _fail("host_file_effect_source_unavailable", "Published effect requires the current registered SourcePin", 3)
                    verify_source_pin(after_pin, source)
                else:
                    verify_source_pin(context["expected_source"], source)
                if payload["phase"] in {"source_published", "manifests_registered", "completed"} and not after_pin:
                    _fail("host_file_effect_source_unavailable", "Final effect requires the current registered SourcePin", 3)
                manifest_refs = payload["manifest_refs"]
                if not isinstance(manifest_refs, list) or len(manifest_refs) > 500:
                    _fail("host_file_effect_receipt_invalid", "manifest_refs must be a bounded array", 2)
                normalized_refs = []
                if body["effect_kind"] == "document_render" and payload["phase"] in {"manifests_registered", "completed"}:
                    for item in manifest_refs:
                        if not isinstance(item, dict) or set(item) != {"segment_id", "manifest_hash", "revision"}:
                            _fail("host_file_effect_receipt_invalid", "Document manifest ref shape is invalid", 2)
                        segment_id = _uuid(item["segment_id"], "manifest.segment_id")
                        manifest_hash = _hash(item["manifest_hash"], "manifest.manifest_hash")
                        revision = item["revision"]
                        if type(revision) is not int or revision < 1:
                            _fail("host_file_effect_receipt_invalid", "Manifest revision is invalid", 2)
                        row = conn.execute("SELECT revision,source_hash,body_json FROM phase3_objects WHERE kind='segment_manifest' AND id=? AND scope_id=?",
                            (segment_id, context["project_id"])).fetchone()
                        manifest = json.loads(row["body_json"]) if row else None
                        if (not row or row["revision"] != revision or row["source_hash"] != after_pin["source_hash"]
                                or not isinstance(manifest, dict) or manifest.get("document_path") != body["target_relative_path"]
                                or fingerprint(manifest) != manifest_hash):
                            _fail("host_file_effect_manifest_mismatch", "Registered document manifest does not match the effect receipt", 3)
                        normalized_refs.append({"segment_id": segment_id, "manifest_hash": manifest_hash, "revision": revision})
                    coverage = payload["coverage_ref"]
                    if not isinstance(coverage, dict) or set(coverage) != {"revision", "certificate_hash", "manifest_set_hash"}:
                        _fail("host_file_effect_receipt_invalid", "Document coverage receipt is incomplete", 2)
                    coverage_row = conn.execute("SELECT revision,source_hash,body_json FROM phase3_objects WHERE kind='segment_coverage' AND id=? AND scope_id=?",
                        (context["project_id"], context["project_id"])).fetchone()
                    certificate = json.loads(coverage_row["body_json"]) if coverage_row else None
                    if (not coverage_row or coverage_row["revision"] != coverage["revision"]
                            or coverage_row["source_hash"] != after_pin["source_hash"]
                            or not isinstance(certificate, dict)
                            or certificate.get("coverage_certificate_hash") != coverage["certificate_hash"]
                            or certificate.get("manifest_set_hash") != coverage["manifest_set_hash"]):
                        _fail("host_file_effect_manifest_mismatch", "Registered document coverage receipt does not match", 3)
                elif manifest_refs or payload["coverage_ref"] is not None:
                    _fail("host_file_effect_receipt_invalid", "Only document effects may include manifest receipts", 2)
                uploaded = payload["uploaded_source_ref"]
                graph_verified = False
                if body["effect_kind"] == "graph_change" and payload["phase"] in {"source_published", "completed"}:
                    if not isinstance(uploaded, dict) or set(uploaded) != {"id", "sha256", "size", "scope_id", "purpose"}:
                        _fail("host_file_effect_receipt_invalid", "Graph effect requires the published graph resource ref", 2)
                    pointer = extension._current_source_pointer(conn, context["binding"], required=True)
                    if pointer["body"].get("graph_resource") != uploaded:
                        _fail("host_file_effect_resource_mismatch", "Graph resource is not the current registered SourcePin source", 3)
                    graph_resource = extension._read_resource(uploaded, context["project_id"], getattr(db_view, "_headers", {}))
                    if graph_resource.get("purpose") != "graph_snapshot" or not isinstance(graph_resource.get("content"), bytes):
                        _fail("host_file_effect_resource_mismatch", "Graph snapshot resource could not be verified", 3)
                    if hashlib.sha256(graph_resource["content"]).hexdigest() != body["candidate_sha256"]:
                        _fail("host_file_effect_resource_mismatch", "Published graph bytes differ from the prepared candidate", 3)
                    graph_verified = True
                elif uploaded is not None:
                    _fail("host_file_effect_receipt_invalid", "Only graph effects may include an uploaded source ref", 2)
                if payload["state"] == "completed":
                    if body["effect_kind"] == "graph_change" and not graph_verified:
                        _fail("host_file_effect_incomplete", "Graph effect cannot complete before source publication", 3)
                    if body["effect_kind"] == "document_render" and payload["phase"] != "completed":
                        _fail("host_file_effect_incomplete", "Document effect must complete after manifest registration", 3)
                history = list(body.get("history", []))
                receipt = {"phase": payload["phase"], "state": payload["state"],
                    "publication": publication, "after_source_pin": after_pin,
                    "manifest_refs": normalized_refs, "uploaded_source_ref": uploaded,
                    "host_graph_bytes_verified": graph_verified, "host_document_verified": False,
                    "recorded_at": utc_now()}
                if history and history[-1]["phase"] == payload["phase"] and history[-1] == receipt:
                    holder["result"] = {"effect_ref": {"kind": _KIND, "id": effect_id,
                        "scope_id": context["project_id"], "revision": old["revision"],
                        "source_hash": after_pin["source_hash"] if after_pin else old["source_hash"]},
                        "effect_state": body["state"], "receipt": receipt, "replayed": True,
                        "host_document_verified": False}
                    return holder["result"]
                updated = dict(body) | {"phase": payload["phase"], "state": payload["state"],
                    "publication": publication, "after_source_pin": after_pin,
                    "uploaded_source_ref": uploaded, "manifest_refs": normalized_refs,
                    "coverage_ref": payload["coverage_ref"], "host_document_verified": False,
                    "history": (history + [receipt])[-20:]}
                object_source_hash = after_pin["source_hash"] if after_pin else body["before_source_pin"]["source_hash"]
                stored = storage.put_object(_KIND, effect_id, context["project_id"], principal.actor,
                    principal.session_id, object_source_hash, old["revision"], updated, state=payload["state"],
                    request_id=request["request_id"],
                    event_id=str(uuid.uuid5(uuid.UUID(request["request_id"]), "host-file-effect:" + payload["phase"])),
                    conn=conn)
                holder["result"] = {"effect_ref": {"kind": _KIND, "id": effect_id,
                    "scope_id": context["project_id"], "revision": stored["revision"],
                    "source_hash": object_source_hash}, "effect_state": payload["state"],
                    "receipt": receipt, "replayed": False, "host_document_verified": False}
                return holder["result"]
            envelope, code = db_view.run_request(req, complete)
            return envelope, code
        _fail("host_operation_unavailable", "Unknown local file effect operation", 2)
    except PmtError as exc:
        return response(req.get("request_id"), error=exc.as_dict()), exc.exit_code


def handle(extension, db, conn, req, principal, headers):
    if req.get("operation") != "read_local_file_effect":
        _fail("host_operation_unavailable", "Only local file effect reads use the read handler", 2)
    payload = req.get("payload", {})
    context = _common(extension, conn, req, principal, source_capture=True)
    storage = Phase3Storage(db)
    effect_id = payload.get("effect_id")
    if effect_id is not None:
        effect_id = _uuid(effect_id, "effect_id")
        current = storage.get_object(_KIND, effect_id, context["project_id"], principal.actor,
                                     principal.session_id, conn=conn)
        if not current:
            _fail("host_file_effect_not_found", "Local file effect intent is unavailable", 2)
        body = current["body"]
        if (body.get("owner") != context["identity"] or body.get("run_id") != context["run_id"]
                or body.get("target_relative_path") != context["target_relative_path"]
                or body.get("canonical_workspace") != context["workspace"]):
            _fail("host_file_effect_owner_conflict", "Effect intent belongs to another current run or target", 3)
        pointer = extension._current_source_pointer(conn, context["binding"], required=False)
        source_ref = ({"id": pointer["id"], "revision": pointer["revision"],
            "source_hash": pointer["source_hash"], "graph_resource_ref": pointer["body"].get("graph_resource")}
            if pointer else None)
        return {"effect_ref": {"kind": _KIND, "id": effect_id, "scope_id": context["project_id"],
                    "revision": current["revision"], "source_hash": current["source_hash"]},
                "effect": body, "effect_state": current["state"],
                "current_source_pin": context["current_source"], "current_source_ref": source_ref,
                "host_document_verified": False}
    allowed_baseline = {"run_id", "expected_run_revision", "repository_id", "project_id", "canonical_workspace",
            "relative_graph_path", "target_relative_path", "branch_key", "expected_source", "baseline_document_path"}
    f3_fields = {"graph_effect_id", "change_set", "change_preview", "impact_set"}
    payload_fields = frozenset(payload)
    allowed_shapes = {frozenset(allowed_baseline), frozenset(allowed_baseline | {"baseline_source_pin"}),
        frozenset(allowed_baseline | f3_fields), frozenset(allowed_baseline | f3_fields | {"baseline_source_pin"})}
    if payload_fields not in allowed_shapes:
        _fail("host_file_effect_invalid", "Read effect or baseline fields are incomplete", 2)
    document_path = _relative(payload["baseline_document_path"], "baseline_document_path")
    if document_path != context["target_relative_path"]:
        _fail("host_file_effect_invalid", "Baseline document path differs from the authorized target", 3)
    current_pin = pin_source(context["current_source"])
    expected = pin_source(payload.get("baseline_source_pin", payload["expected_source"]))
    if (expected.project_id != current_pin.project_id or expected.repository_id != current_pin.repository_id
            or expected.selected_ref != current_pin.selected_ref or expected.source_kind != current_pin.source_kind):
        _fail("host_file_effect_source_conflict", "Prior document baseline belongs to another branch or project", 3)
    rows = conn.execute("SELECT id,revision,source_hash,body_json FROM phase3_objects WHERE kind='segment_manifest' AND scope_id=? AND state='ready' ORDER BY id",
                        (context["project_id"],)).fetchall()
    manifests = []
    for row in rows:
        try:
            manifest = json.loads(row["body_json"])
        except (TypeError, ValueError) as exc:
            raise PmtError("host_file_effect_manifest_corrupt", "Stored document manifest is invalid", 5) from exc
        if (row["source_hash"] == expected.source_hash and manifest.get("source_pin", {}).get("source_hash") == expected.source_hash
                and manifest.get("document_path") == document_path):
            manifests.append({"segment_id": row["id"], "revision": row["revision"],
                "manifest_hash": fingerprint(manifest), "source_hash": row["source_hash"], "manifest": manifest})
    coverage_row = conn.execute("SELECT revision,source_hash,body_json FROM phase3_objects WHERE kind='segment_coverage' AND id=? AND scope_id=?",
                                (context["project_id"], context["project_id"])).fetchone()
    coverage = None
    if coverage_row and coverage_row["source_hash"] == expected.source_hash:
        try:
            certificate = json.loads(coverage_row["body_json"])
        except (TypeError, ValueError) as exc:
            raise PmtError("host_file_effect_manifest_corrupt", "Stored document coverage is invalid", 5) from exc
        if document_path in certificate.get("document_paths", []):
            coverage = {"revision": coverage_row["revision"], "source_hash": coverage_row["source_hash"],
                "certificate_hash": certificate.get("coverage_certificate_hash"),
                "manifest_set_hash": certificate.get("manifest_set_hash"), "certificate": certificate}
    result = {"current_source_pin": current_pin.to_dict(), "document_path": document_path,
        "manifest_refs": manifests, "coverage": coverage, "host_document_verified": False,
        "provenance": "host_registry_metadata_only"}
    if payload_fields & f3_fields:
        if not f3_fields <= payload_fields:
            _fail("host_file_effect_f3_invalid", "F3 comparison requires the effect, ChangeSet, preview, and submitted impact", 2)
        manifest_map = {item["segment_id"]: {"revision": item["revision"],
            "source_hash": item["source_hash"], "manifest": item["manifest"]}
            for item in manifests}
        try:
            result["verified_f3"] = _verified_f3_impact(extension, db, conn, req, principal, headers,
                context, None, payload, manifest_map, coverage)
        except AttributeError as exc:
            raise PmtError("host_file_effect_f3_internal", "Host could not recompute the pinned F3 result", 5,
                details={"reason_code": "internal_attribute_error"}) from exc
    return result
