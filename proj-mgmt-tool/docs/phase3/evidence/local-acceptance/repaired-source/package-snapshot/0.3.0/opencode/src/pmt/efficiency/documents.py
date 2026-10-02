"""Deterministic generated document segments with manual-span preservation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import uuid
from contextlib import closing

from ..errors import PmtError
from ..phase2_common import require_workspace_claim
from ..planning.graph import render_docs
from ..util import fingerprint
from . import graph as graph_store
from .publication import guarded_publish, publication_refs
from .source import pin_source, verify_source_pin
from .storage import Phase3Storage

READ_OPERATIONS = set()
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"prepare_document_segments", "publish_document_segments", "recover_document_segments"}

TEMPLATE_VERSION = "pmt-render-docs-v1"
DEPENDENCY_REGISTRY_VERSION = "pmt-plan-dependencies-v1"
_JOURNAL_KIND = "document_segments"
_MANAGED_BEGIN = "<!-- PMT:DOCUMENT:BEGIN -->"
_MANAGED_END = "<!-- PMT:DOCUMENT:END -->"
_SEGMENT_BEGIN = "<!-- PMT:SEGMENT:{}:BEGIN -->\n"
_SEGMENT_END = "<!-- PMT:SEGMENT:{}:END -->\n"
_SEGMENT_RE = re.compile(r"<!-- PMT:SEGMENT:([0-9a-f-]{36}):BEGIN -->\n(.*?)<!-- PMT:SEGMENT:\1:END -->\n?", re.DOTALL)
_INDEX_ACTOR = "pmt.graph.index"
_INDEX_SESSION = "source-projection"


def _fail(code, message, exit_code=2, details=None, retryable=False):
    raise PmtError(code, message, exit_code, retryable, details)


def _payload(req):
    payload = req.get("payload", {})
    if not isinstance(payload, dict):
        _fail("invalid_payload", "payload must be an object")
    return payload


def _uuid(value, field):
    return graph_store._uuid(value, field)


def _hash(data: bytes):
    return hashlib.sha256(data).hexdigest()


def _actual_document(db, conn, req, payload):
    context = graph_store._scope_context(db, conn, req)
    document_path = graph_store._manifest_relative_path(payload.get("document_path", "docs/pmt-docs/plan.md"))
    # Confirm both source and target ownership before reading either file.
    run = require_workspace_claim(db, conn, req, str(context["workspace"]),
                                  [context["relative_path"], document_path])
    graph_ctx = graph_store._source_graph(db, conn, req)
    if graph_ctx["run"]["id"] != run["id"]:
        _fail("ownership_conflict", "Run changed during document source inspection", 3)
    context.update(graph_ctx)
    context["document_path"] = document_path
    context["run"] = run
    context["document_file"] = Path(os.path.abspath(context["workspace"] / Path(document_path)))
    from ..resources import _reject_links
    _reject_links(context["document_file"])
    try:
        context["document_file"].parent.resolve(strict=True).relative_to(context["workspace"])
    except (OSError, ValueError):
        _fail("document_path_invalid", "Document parent must exist inside the owned workspace", 3)
    if context["document_file"].exists() and not context["document_file"].is_file():
        _fail("document_path_invalid", "Document target must be a regular file", 3)
    try:
        raw = context["document_file"].read_bytes() if context["document_file"].exists() else None
    except OSError as exc:
        raise PmtError("document_read_failed", "Document target could not be read", 4, True) from exc
    if raw is not None and len(raw) > 16 * 1024 * 1024:
        _fail("document_too_large", "Document target exceeds 16 MiB")
    try:
        text = raw.decode("utf-8") if raw is not None else ""
    except UnicodeDecodeError as exc:
        raise PmtError("document_encoding_invalid", "Document must be UTF-8", 3) from exc
    context.update({"document_raw": raw, "document_text": text,
                    "document_sha256": _hash(raw) if raw is not None else None})
    return context


def _segment_id(project_id, document_path, key):
    return str(uuid.uuid5(uuid.UUID(project_id), f"pmt.segment:{TEMPLATE_VERSION}:{document_path}:{key}"))


def _document_id(project_id, document_path):
    return str(uuid.uuid5(uuid.UUID(project_id), f"pmt.document:{document_path}"))


def _render_segments(graph, source_pin, document_path):
    markdown, _, report = render_docs(graph)
    lines = markdown.splitlines(keepends=True)
    try:
        req_start, impl_start, relation_start = (lines.index("## Requirements\n"),
                                                 lines.index("## Implementation\n"),
                                                 lines.index("## Relations\n"))
    except ValueError as exc:
        raise PmtError("render_template_invalid", "Canonical renderer headings changed", 3) from exc
    if not req_start < impl_start < relation_start:
        _fail("render_template_invalid", "Canonical renderer section order is unsupported", 3)
    document_id = _document_id(graph["project_id"], document_path)
    segments = []

    def put(key, content, *, node_ids=(), fields=(), relation_ids=(), ownership="generated"):
        segment_id = _segment_id(graph["project_id"], document_path, key)
        manifest = {"segment_id": segment_id, "document_id": document_id, "document_path": document_path,
                    "node_ids": sorted(set(node_ids)), "field_paths": sorted(set(fields)),
                    "relation_ids": sorted(set(relation_ids)), "template_version": TEMPLATE_VERSION,
                    "output_hash": _hash(content.encode("utf-8")), "ownership": ownership,
                    "source_pin": source_pin.to_dict()}
        segments.append({"segment_id": segment_id, "key": key, "content": content,
                         "manifest": manifest, "output_hash": manifest["output_hash"]})

    put("preamble", "".join(lines[:req_start]))

    def node_fields(node):
        common = ["summary", "premise", "product_stage", "product_scope.applies", "product_scope.reason",
                  "product_scope.criteria", "stop_reason", "delegated_scope"]
        if node["tree_kind"] == "requirement":
            return common + ["criteria"]
        return common + ["framework_assignment", "architecture", "logging", "tests",
                         "function_spec.input", "function_spec.output", "function_spec.constraints",
                         "function_spec.invariants", "function_spec.errors", "function_spec.verification",
                         "choice_set.options", "choice.selected", "choice.reason", "choice.scope"]

    def section(start, stop, key):
        put(key + ":heading", "".join(lines[start:start + 2]))
        node_starts = [index for index in range(start + 2, stop) if lines[index].startswith("### ")]
        if start + 2 < stop and (not node_starts or node_starts[0] != start + 2):
            spacer_end = node_starts[0] if node_starts else stop
            put(key + ":spacing", "".join(lines[start + 2:spacer_end]))
        for offset, begin in enumerate(node_starts):
            end = node_starts[offset + 1] if offset + 1 < len(node_starts) else stop
            content = "".join(lines[begin:end])
            match = re.search(r"(?m)^ID: `([0-9a-f-]{36})`$", content)
            if not match:
                _fail("render_template_invalid", "Canonical node section has no stable UUID", 3)
            node_id = _uuid(match.group(1), "rendered_node_id")
            node = next((value for value in graph["nodes"] if value["id"] == node_id), None)
            if node is None:
                _fail("render_template_invalid", "Rendered node UUID is absent from canonical graph", 3)
            put("node:" + node_id, content, node_ids=[node_id], fields=node_fields(node))

    section(req_start, impl_start, "requirements")
    section(impl_start, relation_start, "implementation")
    put("relations:heading", "".join(lines[relation_start:relation_start + 2]))
    relation_lines = [index for index in range(relation_start + 2, len(lines)) if lines[index].startswith("- ")]
    if len(relation_lines) != len(graph["relations"]):
        _fail("render_template_invalid", "Canonical relation projection changed", 3)
    for relation, line_index in zip(graph["relations"], relation_lines):
        put("relation:" + relation["id"], lines[line_index], relation_ids=[relation["id"]])
    trailer = relation_lines[-1] + 1 if relation_lines else relation_start + 2
    if trailer < len(lines):
        put("relations:trailer", "".join(lines[trailer:]))
    if "".join(segment["content"] for segment in segments) != markdown:
        _fail("render_template_invalid", "Segment contents differ from full render_docs output", 5)
    return markdown, segments, report


def _segment_markers(segment_id, content):
    return _SEGMENT_BEGIN.format(segment_id) + content + _SEGMENT_END.format(segment_id)


def _managed_block(segments):
    return _MANAGED_BEGIN + "\n" + "".join(_segment_markers(segment["segment_id"], segment["content"])
                                         for segment in segments) + _MANAGED_END + "\n"


def _parse_document(text):
    # Managed Markdown uses canonical LF, but Windows editors/checkouts can
    # present equivalent CRLF. Raw target bytes remain protected by hash CAS.
    text = text.replace("\r\n", "\n")
    begins, ends = text.count(_MANAGED_BEGIN), text.count(_MANAGED_END)
    if begins == 0 and ends == 0:
        return {"has_block": False, "prefix": text, "suffix": "", "segments": {}}
    if begins != 1 or ends != 1:
        _fail("document_managed_block_invalid", "Managed block markers are missing or duplicated", 3)
    start, end_marker = text.index(_MANAGED_BEGIN), text.index(_MANAGED_END)
    if end_marker < start:
        _fail("document_managed_block_invalid", "Managed block markers are reversed", 3)
    end = end_marker + len(_MANAGED_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    body_start = start + len(_MANAGED_BEGIN)
    if text[body_start:body_start + 1] == "\n":
        body_start += 1
    body, segments, cursor = text[body_start:end_marker], {}, 0
    for match in _SEGMENT_RE.finditer(body):
        if body[cursor:match.start()] != "":
            _fail("document_managed_block_invalid", "Unowned content appears inside the generated block", 3)
        segment_id = _uuid(match.group(1), "segment_id")
        if segment_id in segments:
            _fail("document_managed_block_invalid", "Segment ID is duplicated in the document", 3)
        segments[segment_id] = match.group(2)
        cursor = match.end()
    if cursor != len(body):
        _fail("document_managed_block_invalid", "Generated segment markers are incomplete", 3)
    return {"has_block": True, "prefix": text[:start], "suffix": text[end:], "segments": segments}


def _compose_document(prefix, suffix, segments):
    if prefix and not prefix.endswith("\n"):
        prefix += "\n\n"
    elif prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + _managed_block(segments) + suffix, prefix


def _request_fingerprint(req):
    ignored = {"request_id", "correlation_id", "received_at", "received_at_utc", "retry_count", "attempt"}
    return fingerprint({key: value for key, value in req.items() if key not in ignored})


def _request_id(value, field):
    return _uuid(value, field)


def _stage_ref(workspace, target, render_id):
    stage = target.parent / f".{target.name}.pmt-document-{render_id}.stage"
    try:
        stage.relative_to(workspace)
    except ValueError:
        _fail("document_path_invalid", "Document stage escapes the owned workspace", 3)
    return stage


def _manifest_refs(db, scope_id, segments):
    with closing(db.connect()) as conn:
        result = []
        for segment in segments:
            row = conn.execute("SELECT revision FROM phase3_objects WHERE kind='segment_manifest' AND id=? AND scope_id=?",
                               (segment["segment_id"], scope_id)).fetchone()
            result.append({"segment_id": segment["segment_id"], "expected_manifest_revision": row[0] if row else 0})
        coverage = conn.execute("SELECT revision FROM phase3_objects WHERE kind='segment_coverage' AND id=? AND scope_id=?",
                                (scope_id, scope_id)).fetchone()
        return result, coverage[0] if coverage else 0


def _manual_segments(context, parsed, segments):
    """Represent preserved spans by hash and stable IDs; never journal their text."""
    additions = []
    for key, text in (("manual-prefix", parsed["prefix"]), ("manual-suffix", parsed["suffix"])):
        if not text:
            continue
        sid = _segment_id(context["scope_id"], context["document_path"], key)
        digest = _hash(text.encode("utf-8"))
        additions.append({"segment_id": sid, "key": key, "content": text,
                          "output_hash": digest,
                          "manifest": {"segment_id": sid,
                                       "document_id": _document_id(context["scope_id"], context["document_path"]),
                                       "document_path": context["document_path"], "node_ids": [], "field_paths": [],
                                       "relation_ids": [], "template_version": TEMPLATE_VERSION,
                                       "output_hash": digest, "ownership": "manual",
                                       "source_pin": context["source_pin"].to_dict()}})
    return segments + additions


def _checkpoint(db, req, intent_id, context, phase, details):
    storage = Phase3Storage(db)
    with closing(db.connect()) as conn:
        intent = storage.get_intent(intent_id, context["scope_id"], req["actor"], req["session_id"], conn=conn)
    if intent is None:
        _fail("document_intent_missing", "Document publication journal is unavailable", 3)
    stage = intent["body"].get("stage")
    if phase in {"durability_unknown", "durability_unsupported"}:
        target = "review_required" if phase == "durability_unknown" else "unsupported"
    elif phase == "conflict":
        target = "conflict"
    elif phase in {"candidate_published", "target_detached", "original_preserved", "candidate_staged"}:
        target = phase
    else:
        target = stage
    if target != stage:
        storage.update_intent(intent_id, stage, target, context["scope_id"], req["actor"], req["session_id"],
                              {"phase": phase, "publication": {key: details.get(key) for key in
                               ("recovery_ref", "target_ref", "candidate_ref", "expected_hash",
                                "original_hash", "candidate_hash", "phase", "durability_warning")
                               if details.get(key) is not None}})


def _operation_request(req, operation, request_id, payload):
    return {"protocol_version": req.get("protocol_version", 1), "operation": operation,
            "request_id": request_id, "actor": req["actor"], "session_id": req["session_id"],
            "scope_id": req.get("scope_id"), "payload": payload}


def _register_manifests(db, req, context, intent):
    from ..phase3 import execute as execute_phase3
    receipts = []
    refs = []
    pin = intent["source_pin"]
    for segment in intent["segments"]:
        expected_revision = segment["expected_manifest_revision"]
        synthetic = _operation_request(req, "register_segment_manifest",
            str(uuid.uuid5(uuid.UUID(intent["render_id"]), "manifest:" + segment["segment_id"])),
            {"repository_id": req["payload"]["repository_id"],
             "workspace": req["payload"]["workspace"],
             "relative_graph_path": req["payload"]["relative_graph_path"],
             "run_id": req["payload"]["run_id"], "document_path": intent["document_path"],
             "expected_source": pin, "expected_manifest_revision": expected_revision,
             "manifest": segment["manifest"]})
        envelope, code = execute_phase3(db, synthetic)
        if code or not envelope.get("ok"):
            error = envelope.get("error") or {}
            _fail(error.get("code", "segment_manifest_register_failed"),
                  "Document segment manifest could not be registered", code or 3,
                  error.get("details"), error.get("retryable", False))
        manifest_hash = envelope["result"]["manifest_hash"]
        receipts.append({"segment_id": segment["segment_id"], "manifest_hash": manifest_hash,
                         "revision": envelope["result"]["manifest_revision"]})
        refs.append({"segment_id": segment["segment_id"], "manifest_hash": manifest_hash})
    certificate = intent["certificate"]
    certificate["manifest_refs"] = refs
    certificate["manifest_set_hash"] = graph_store.manifest_set_fingerprint(refs, certificate["document_paths"], pin)
    synthetic = _operation_request(req, "register_segment_manifest",
        str(uuid.uuid5(uuid.UUID(intent["render_id"]), "coverage")),
        {"repository_id": req["payload"]["repository_id"], "workspace": req["payload"]["workspace"],
         "relative_graph_path": req["payload"]["relative_graph_path"], "run_id": req["payload"]["run_id"],
         "document_path": intent["document_path"], "expected_source": pin,
         "expected_coverage_revision": intent["expected_coverage_revision"],
         "coverage_certificate": certificate})
    envelope, code = execute_phase3(db, synthetic)
    if code or not envelope.get("ok"):
        error = envelope.get("error") or {}
        _fail(error.get("code", "coverage_register_failed"), "Document baseline coverage could not be registered",
              code or 3, error.get("details"), error.get("retryable", False))
    return receipts, envelope["result"]


def execute_file(db, req):
    op = req.get("operation")
    if op == "prepare_document_segments":
        return _prepare(db, req)
    if op == "publish_document_segments":
        return _publish(db, req)
    if op == "recover_document_segments":
        return _recover(db, req)
    _fail("operation_unavailable", "Document operation is unavailable")


def _prepare(db, req):
    payload = _payload(req)
    with closing(db.connect()) as conn:
        context = _actual_document(db, conn, req, payload)
    expected = pin_source(payload.get("expected_source"))
    verify_source_pin(expected, context["source_pin"])
    if payload.get("template_version", TEMPLATE_VERSION) != TEMPLATE_VERSION:
        _fail("document_template_unsupported", "Requested document template is unsupported", 3)
    parsed = _parse_document(context["document_text"])
    markdown, segments, report = _render_segments(context["graph"], expected, context["document_path"])
    prior_manifests = []
    impact = None
    if parsed["has_block"]:
        impact = payload.get("impact_set")
        apply_receipt = payload.get("apply_receipt")
        if not isinstance(impact, dict) or not isinstance(apply_receipt, dict):
            _fail("document_partial_evidence_required", "Partial rendering requires its F3 impact and F1 apply receipt", 3)
        if impact.get("complete") is not True or impact.get("unknown") != []:
            _fail("document_impact_incomplete", "Partial rendering requires complete impact coverage", 3)
        receipt = apply_receipt.get("result") if isinstance(apply_receipt.get("result"), dict) else apply_receipt
        before = pin_source(receipt.get("before_source_pin"))
        applied = pin_source(receipt.get("source_pin"))
        impact_before = pin_source(impact.get("before_source_pin"))
        impact_pin = pin_source(impact.get("source_pin"))
        verify_source_pin(before, impact_before)
        verify_source_pin(before, impact_pin)
        verify_source_pin(applied, expected)
        expected_new = impact.get("expected_new_source")
        if (not isinstance(expected_new, dict) or expected_new.get("graph_hash") != applied.graph_hash or
                expected_new.get("graph_revision") != applied.graph_revision or
                expected_new.get("graph_schema") != applied.graph_schema or
                impact.get("change_id") != receipt.get("change_id") or
                impact.get("change_set_hash") != receipt.get("change_set_hash")):
            _fail("document_impact_apply_mismatch", "F3 impact is not bound to this exact F1 apply receipt", 3)
        change_set = payload.get("change_set")
        preview = payload.get("change_preview")
        if (not isinstance(change_set, dict) or change_set.get("change_id") != impact.get("change_id") or
                not isinstance(preview, dict) or preview.get("change_id") != impact.get("change_id") or
                preview.get("change_set_hash") != impact.get("change_set_hash") or
                preview.get("expected_new_source") != expected_new):
            _fail("document_impact_apply_mismatch", "Original F1 change and validated preview must match the F3 impact", 3)
        with closing(db.connect()) as conn:
            prior = graph_store._load_prior_segment_manifests(db, conn, req, apply_receipt)
        if prior.get("prior_coverage", {}).get("segments") != "complete":
            _fail("document_prior_baseline_unknown", "Prior document coverage is not complete", 3,
                  {"reason_code": prior.get("prior_coverage", {}).get("reason")})
        prior_manifests = prior["prior_manifests"]
        if not prior_manifests or any(item["manifest"].get("document_path") != context["document_path"]
                                      for item in prior_manifests):
            _fail("document_prior_manifest_scope", "Prior complete manifests include unsupported document paths", 3)
        prior_by_id = {item["segment_id"]: item for item in prior_manifests}
        expected_ids = {item["segment_id"] for item in prior_manifests}
        if set(parsed["segments"]) != (expected_ids - {
                item["segment_id"] for item in prior_manifests if item["manifest"].get("ownership") == "manual"}):
            _fail("document_managed_block_conflict", "Managed segment IDs do not match the registered prior baseline", 3)
        for segment_id, old_text in parsed["segments"].items():
            if _hash(old_text.encode("utf-8")) != prior_by_id[segment_id]["manifest"].get("output_hash"):
                _fail("document_generated_edit_conflict", "A generated segment was edited outside PMT", 3,
                      {"segment_id": segment_id})
        # Manual spans are taken from the current claimed target and preserved.
        # The target hash CAS rejects edits made after this preparation.
        impact_ids = {item.get("segment_id") for item in impact.get("documents", []) if isinstance(item, dict)}
        if any(not isinstance(item, str) for item in impact_ids):
            _fail("document_impact_invalid", "Impact document references are invalid", 3)
        changed_ids = set()
        global_version_ids = set()
        for segment in segments:
            old = prior_by_id.get(segment["segment_id"])
            if old is None or old["manifest"].get("output_hash") != segment["output_hash"]:
                changed_ids.add(segment["segment_id"])
                if segment["key"] == "preamble" and before.graph_revision != applied.graph_revision:
                    global_version_ids.add(segment["segment_id"])
        if not changed_ids <= (impact_ids | global_version_ids):
            _fail("document_impact_underreported", "A changed segment is outside the validated impact set", 3,
                  {"segment_ids": sorted(changed_ids - impact_ids - global_version_ids)})
        if set(prior_by_id) != {item["segment_id"] for item in segments} | {
                item["segment_id"] for item in prior_manifests if item["manifest"].get("ownership") == "manual"}:
            _fail("document_segment_set_changed", "Partial rendering cannot add or remove unclassified segments", 3)
    segments = _manual_segments(context, parsed, segments)
    # Build the bytes with the preserved prefix and suffix; manual spans are manifest-only.
    candidate_text, normalized_prefix = _compose_document(parsed["prefix"], parsed["suffix"],
        [item for item in segments if item["manifest"]["ownership"] == "generated"])
    for item in segments:
        if item["key"] == "manual-prefix":
            item["content"] = normalized_prefix
            item["output_hash"] = _hash(normalized_prefix.encode("utf-8"))
            item["manifest"]["output_hash"] = item["output_hash"]
    candidate = candidate_text.encode("utf-8")
    render_id = req["request_id"]
    stage = _stage_ref(context["workspace"], context["document_file"], render_id)
    try:
        with open(stage, "xb") as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        from ..resources import _reject_links
        _reject_links(stage)
        if not stage.is_file():
            _fail("document_stage_conflict", "Prepared document stage is not a regular file", 3)
        if _hash(stage.read_bytes()) != _hash(candidate):
            _fail("document_stage_conflict", "Prepared document stage has different bytes", 3)
    except OSError as exc:
        raise PmtError("document_stage_failed", "Prepared document could not be staged", 4, True) from exc
    all_segments = segments
    manifest_revs, coverage_rev = _manifest_refs(db, context["scope_id"], all_segments)
    rev_map = {item["segment_id"]: item["expected_manifest_revision"] for item in manifest_revs}
    for segment in all_segments:
        segment["expected_manifest_revision"] = rev_map[segment["segment_id"]]
    field_paths = {}
    expected_nodes = sorted(node["id"] for node in context["graph"]["nodes"])
    for segment in all_segments:
        for node_id in segment["manifest"]["node_ids"]:
            field_paths.setdefault(node_id, set()).update(segment["manifest"]["field_paths"])
    certificate = {"source_pin": expected.to_dict(), "document_paths": [context["document_path"]],
        "manifest_refs": [], "manifest_set_hash": "0" * 64,
        "expected_node_ids": expected_nodes,
        "expected_relation_ids": sorted(rel["id"] for rel in context["graph"]["relations"]),
        "expected_field_paths": {node_id: sorted(paths) for node_id, paths in field_paths.items()},
        "dependency_registry_version": DEPENDENCY_REGISTRY_VERSION,
        "template_versions": [TEMPLATE_VERSION], "production_receipt_ref": "document-render:" + render_id,
        "source_table_version": "graph-schema-1"}
    prior_hashes = {item["segment_id"]: item["manifest"].get("output_hash") for item in prior_manifests}
    segment_changes = [{"segment_id": item["segment_id"], "old_hash": prior_hashes.get(item["segment_id"]),
                        "new_hash": item["output_hash"]}
                       for item in all_segments if prior_hashes.get(item["segment_id"]) != item["output_hash"]]
    journal_segments = [{"segment_id": item["segment_id"], "manifest": item["manifest"],
                         "expected_manifest_revision": item["expected_manifest_revision"]} for item in all_segments]
    intent_body = {"stage": "prepared", "render_id": render_id, "request_fingerprint": _request_fingerprint(req),
        "document_path": context["document_path"], "source_pin": expected.to_dict(),
        "document_hash": context["document_sha256"], "candidate_hash": _hash(candidate),
        "stage_ref": str(stage.relative_to(context["workspace"])).replace(os.sep, "/"),
        "publication_refs": publication_refs(context["document_file"], render_id, context["workspace"]),
        "segments": journal_segments, "certificate": certificate,
        "prior_baseline": ({"source_pin": prior.get("before_source_pin"),
            "manifest_refs": prior.get("prior_manifest_refs", []),
            "manifests": [{"segment_id": item["segment_id"], "revision": item["revision"],
                           "manifest_hash": item["manifest_hash"], "manifest": item["manifest"]}
                          for item in prior_manifests],
            "coverage": prior.get("prior_coverage"), "apply_journal_id": prior.get("journal_id"),
            "recovery_ref": prior.get("recovery_ref")} if prior_manifests else None),
        "expected_coverage_revision": coverage_rev,
        "segment_changes": segment_changes,
        "created_segment_count": len(all_segments), "render_hash": _hash(markdown.encode("utf-8")),
        "graph_hash": report["sha256"], "replayed": False}
    with closing(db.connect()) as conn:
        current = _actual_document(db, conn, req, payload)
    verify_source_pin(expected, current["source_pin"])
    if current["document_sha256"] != intent_body["document_hash"]:
        _fail("document_conflict", "Document changed while its candidate was prepared", 3)
    def commit(conn, request):
        graph_store._scope_context(db, conn, request)
        require_workspace_claim(db, conn, request, str(context["workspace"]),
                                [context["relative_path"], context["document_path"]])
        result = Phase3Storage(db).append_intent(_JOURNAL_KIND, render_id, context["scope_id"], req["actor"],
            req["session_id"], intent_body, event_id=None, conn=conn)
        return {"render_id": render_id, "journal_id": result["id"], "stage": "prepared",
                "source_pin": expected.to_dict(), "document_path": context["document_path"],
                "candidate_hash": intent_body["candidate_hash"], "segment_count": len(all_segments),
                "changed_segment_ids": [item["segment_id"] for item in segment_changes],
                "segment_changes": segment_changes,
                "replayed": result["replayed"]}
    envelope, code = db.run_request(req, commit)
    if code:
        with closing(db.connect()) as conn:
            stored = Phase3Storage(db).get_intent(render_id, context["scope_id"],
                req["actor"], req["session_id"], conn=conn)
        if stored is None and stage.exists() and _hash(stage.read_bytes()) == intent_body["candidate_hash"]:
            stage.unlink()
    if code == 0:
        db.diagnostics.emit("planning.document_segments_staged", request_id=req.get("request_id"),
            scope_id=context["scope_id"], record_id=render_id, source_hash=expected.source_hash,
            graph_hash=expected.graph_hash, graph_revision=expected.graph_revision,
            template_version=TEMPLATE_VERSION, manifest_hash=fingerprint([item["manifest"] for item in journal_segments]),
            count=len(journal_segments), outcome="replayed" if envelope.get("result", {}).get("replayed") else "success")
    return envelope, code


def _load_intent(db, req, intent_id):
    payload = _payload(req)
    with closing(db.connect()) as conn:
        context = _actual_document(db, conn, req, payload)
        intent = Phase3Storage(db).get_intent(intent_id, context["scope_id"], req["actor"], req["session_id"], conn=conn)
    if intent is None or intent["kind"] != _JOURNAL_KIND:
        _fail("document_intent_not_found", "Document render journal does not exist", 3)
    if intent["body"].get("document_path") != context["document_path"]:
        _fail("document_intent_scope_mismatch", "Document journal targets a different path", 3)
    return context, intent


def _publish(db, req):
    payload = _payload(req)
    intent_id = _request_id(payload.get("journal_id"), "journal_id")
    context, intent = _load_intent(db, req, intent_id)
    body = intent["body"]
    if body.get("stage") == "completed":
        outcome = intent.get("outcome") or {}
        def replay_completed(conn, request):
            graph_store._scope_context(db, conn, request)
            require_workspace_claim(db, conn, request, str(context["workspace"]),
                                    [context["relative_path"], context["document_path"]])
            return outcome
        return db.run_request(req, replay_completed)
    if body.get("stage") not in {"prepared", "candidate_staged", "original_preserved", "target_detached",
                                  "candidate_published", "intent"}:
        _fail("document_journal_conflict", "Document journal is not prepared for publication", 3,
              {"journal_id": intent_id, "stage": body.get("stage")})
    expected = pin_source(body["source_pin"])
    verify_source_pin(expected, context["source_pin"])
    if body.get("stage") == "prepared" and context["document_sha256"] != body["document_hash"]:
        _fail("document_conflict", "Document target changed after preparation", 3)
    recoverable_missing_target = body.get("stage") in {"intent", "candidate_staged", "original_preserved", "target_detached"}
    if (body.get("stage") != "prepared" and context["document_sha256"] not in
            {body["document_hash"], body["candidate_hash"]} and
            not (context["document_sha256"] is None and recoverable_missing_target)):
        _fail("document_conflict", "Document target matches neither journaled version", 3)
    stage = context["workspace"] / Path(body["stage_ref"])
    from ..resources import _reject_links
    _reject_links(stage)
    if not stage.is_file() or _hash(stage.read_bytes()) != body["candidate_hash"]:
        _fail("document_stage_conflict", "Document candidate stage is missing or changed", 3)
    storage = Phase3Storage(db)
    def on_checkpoint(phase, details):
        _checkpoint(db, req, intent_id, context, phase, details)
        if phase in {"candidate_staged", "original_preserved"}:
            # Recheck outside SQLite immediately before the helper can detach/create the target.
            with closing(db.connect()) as conn:
                checkpoint_source = graph_store._source_graph(db, conn, req)
            verify_source_pin(expected, checkpoint_source["source_pin"])
    try:
        result = guarded_publish(context["document_file"], stage, body["document_hash"], body["render_id"],
                                 context["workspace"], on_checkpoint)
    except PmtError as error:
        db.diagnostics.emit("planning.document_publish_conflict" if error.code == "publication_conflict" else
                            "planning.document_publish_reconcile_required", request_id=req.get("request_id"),
            scope_id=context["scope_id"], record_id=body["render_id"], source_hash=expected.source_hash,
            graph_hash=expected.graph_hash, graph_revision=expected.graph_revision,
            template_version=TEMPLATE_VERSION, manifest_hash=body.get("candidate_hash"),
            reason_code=error.code, outcome="conflict")
        raise
    if result.get("status") == "conflict":
        _fail("publication_conflict", "Document publication retained conflicting versions", 3,
              {key: result.get(key) for key in ("phase", "recovery_ref", "original_hash", "candidate_hash")})
    # A second pass detects edits through a still-open handle before the metadata commit.
    result = guarded_publish(context["document_file"], stage, body["document_hash"], body["render_id"],
                             context["workspace"], on_checkpoint)
    with closing(db.connect()) as conn:
        fresh = _actual_document(db, conn, req, payload)
    try:
        verify_source_pin(expected, fresh["source_pin"])
    except PmtError:
        _checkpoint(db, req, intent_id, context, "conflict", {
            "phase": "source_changed_after_publication", "candidate_hash": body["candidate_hash"],
            "recovery_ref": result.get("recovery_ref"), "target_ref": result.get("target_ref")})
        raise
    if fresh["document_sha256"] != body["candidate_hash"]:
        _fail("document_conflict", "Published document changed before metadata registration", 3)
    # Rebuild the index against the pinned source before registering manifests.
    index_req = _operation_request(req, "rebuild_graph_index",
        str(uuid.uuid5(uuid.UUID(body["render_id"]), "rebuild-index")),
        {"repository_id": payload["repository_id"], "workspace": payload["workspace"],
         "relative_graph_path": payload["relative_graph_path"], "run_id": payload["run_id"],
         "expected_source": body["source_pin"]})
    from ..phase3 import execute as execute_phase3
    index_result, index_code = execute_phase3(db, index_req)
    if index_code or not index_result.get("ok"):
        _fail("graph_index_rebuild_failed", "Pinned graph index could not be prepared for manifests", index_code or 3)
    receipts, coverage = _register_manifests(db, req, context, body)
    outcome = {"journal_id": intent_id, "render_id": body["render_id"], "stage": "completed",
        "document_path": body["document_path"], "source_pin": body["source_pin"],
        "document_hash": body["candidate_hash"], "recovery_ref": result.get("recovery_ref"),
        "changed_segment_ids": [item["segment_id"] for item in body.get("segment_changes", [])],
        "segment_changes": body.get("segment_changes", []),
        "manifest_count": len(receipts), "manifest_refs": receipts,
        "coverage": coverage.get("coverage"), "publication": {key: result.get(key) for key in
            ("status", "target_ref", "expected_hash", "original_hash", "candidate_hash", "recovery_ref", "phase",
             "durability_warning") if result.get(key) is not None}}
    def finish(conn, request):
        current = storage.get_intent(intent_id, context["scope_id"], req["actor"], req["session_id"], conn=conn)
        stage_name = current["body"].get("stage") if current else None
        if stage_name not in {"candidate_published", "completed"}:
            _fail("document_journal_conflict", "Document journal is not ready to complete", 3)
        if stage_name != "completed":
            storage.update_intent(intent_id, stage_name, "completed", context["scope_id"], req["actor"],
                                  req["session_id"], outcome, conn=conn)
        return outcome
    envelope, code = db.run_request(req, finish)
    if code == 0:
        db.diagnostics.emit("planning.document_segments_published", request_id=req.get("request_id"),
            scope_id=context["scope_id"], record_id=body["render_id"], source_hash=expected.source_hash,
            graph_hash=expected.graph_hash, graph_revision=expected.graph_revision,
            template_version=TEMPLATE_VERSION, manifest_hash=fingerprint(receipts), count=len(receipts),
            outcome="success")
    return envelope, code


def _recover(db, req):
    payload = _payload(req)
    original = payload.get("original_request")
    if not isinstance(original, dict) or original.get("operation") != "publish_document_segments":
        _fail("recovery_request_invalid", "original_request must be the original publish_document_segments request")
    from ..service import normalize_request
    original = normalize_request(original)
    if original.get("actor") != req.get("actor") or original.get("session_id") != req.get("session_id"):
        _fail("ownership_conflict", "Only the original actor and session can recover document publication", 3)
    if original.get("scope_id") != req.get("scope_id"):
        _fail("scope_conflict", "Recovery must use the same project scope", 3)
    orig_payload = _payload(original)
    with closing(db.connect()) as conn:
        context = _actual_document(db, conn, req, payload)
        journal_id = _request_id(orig_payload.get("journal_id"), "original_request.journal_id")
        row = conn.execute("SELECT id FROM phase3_journal WHERE kind=? AND id=? AND scope_id=? AND owner_actor=? AND owner_session=?",
                           (_JOURNAL_KIND, journal_id,
                            context["scope_id"], req["actor"], req["session_id"])).fetchone()
    if not row:
        _fail("document_intent_not_found", "No recoverable document journal was found", 3)
    publish_req = dict(original)
    publish_payload = dict(orig_payload)
    # The recovery call proves the currently claimed run; the original request ID
    # remains the durable publication result key.
    for key in ("run_id", "workspace", "repository_id", "relative_graph_path"):
        publish_payload[key] = payload[key]
    publish_req["payload"] = publish_payload
    return _publish(db, publish_req)


def handle(db, conn, req):
    _fail("operation_unavailable", "Document operations run through the file-operation boundary")
