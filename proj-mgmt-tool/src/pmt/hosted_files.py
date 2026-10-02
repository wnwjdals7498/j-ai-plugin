"""Client-local graph/document effects backed by Host-owned metadata receipts.

This adapter has no PMT database connection. Host calls contain only opaque IDs,
canonical workspace references, workspace-relative paths, hashes and receipts;
all filesystem reads and guarded publication stay in this client process.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .errors import PmtError
from .hosted_runtime import HostedLocalRuntime, _call, _operation
from .efficiency.source import inspect_graph_source, pin_source, verify_source_pin
from .efficiency.graph import prepare_change_set, _safe_operation_summary
from .efficiency.publication import guarded_publish, publication_refs
from .service import response
from .util import canonical_json, fingerprint
from .workspace import canonical_workspace


_GRAPH_OPS = {"preview_graph_change", "apply_graph_change", "recover_graph_change"}
_DOC_OPS = {"prepare_document_segments", "publish_document_segments", "recover_document_segments"}
_ALL = _GRAPH_OPS | _DOC_OPS


def _same_publication_checkpoint(left, right):
    if left == right:
        return True
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    left, right = dict(left), dict(right)
    if {left.get("status"), right.get("status")} <= {"published", "replayed"}:
        left.pop("status", None); right.pop("status", None)
    return left == right


def _uuid(value, label):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("hosted_file_input_invalid", f"{label} must be a canonical UUID", 2) from exc
    return value


class HostedFiles:
    """Execute the existing graph/document semantics against an authenticated Host."""

    def __init__(self, profile, state_port, spool_root):
        self.profile = dict(profile)
        self.state_port = state_port
        self.spool_root = Path(spool_root)
        if not self.spool_root.is_absolute():
            raise PmtError("hosted_file_spool_invalid", "Hosted file-effect spool root must be absolute", 2)

        def mapping_provider(run, pin):
            from .storage_config import mapping_for_request
            selected = mapping_for_request(self.profile, {"scope_id": pin.get("project_id"),
                "payload": {"repository_id": pin.get("repository_id"), "source_pin": pin}})
            if selected is None or run.get("workspace") != selected["canonical_workspace"]:
                raise PmtError("storage_mapping_conflict", "Host run does not match this client's checkout mapping", 3)
            return selected

        self.runtime = HostedLocalRuntime(state_port, mapping_provider, self.spool_root)

    def execute(self, request):
        op = request.get("operation")
        if op not in _ALL:
            raise PmtError("hosted_file_operation_unsupported", "Hosted client file operation is unsupported", 2)
        try:
            if op == "preview_graph_change":
                result = self.preview_graph_change(request)
            elif op == "apply_graph_change":
                result = self.apply_graph_change(request)
            elif op == "recover_graph_change":
                result = self.recover_graph_change(request)
            elif op == "prepare_document_segments":
                result = self.prepare_document_segments(request)
            elif op == "publish_document_segments":
                result = self.publish_document_segments(request)
            else:
                result = self.recover_document_segments(request)
            return response(request.get("request_id"), result=result), 0
        except PmtError as exc:
            return response(request.get("request_id"), error=exc.as_dict()), exc.exit_code
        except OSError as exc:
            error = PmtError("hosted_file_io_unknown", "Client file operation outcome requires reconciliation", 4, True,
                {"exception_type": type(exc).__name__})
            return response(request.get("request_id"), error=error.as_dict()), error.exit_code

    def _prepare(self, request, *, target_path=None):
        payload = request.get("payload", {})
        run, snapshot, context, f5, pin, resolved = self.runtime._prepare(request)
        if target_path is not None:
            from .efficiency.graph import _manifest_relative_path
            target_path = _manifest_relative_path(target_path)
            if target_path == resolved["relative_graph_path"]:
                raise PmtError("hosted_file_target_invalid", "Document path cannot replace the graph source", 2)
            self._authorize_target(request, run, pin, resolved, target_path)
        return run, snapshot, context, f5, pin, resolved

    def _authorize_target(self, request, run, pin, resolved, target_path):
        # The metadata-only file-effect read rechecks graph and target path locks
        # together before we inspect bytes; authorize_workspace alone binds only
        # the graph pointer path.
        payload = {"run_id": run["id"], "expected_run_revision": run["revision"],
            "repository_id": pin.repository_id, "project_id": pin.project_id,
            "canonical_workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"], "target_relative_path": target_path,
            "branch_key": self.runtime._branch_key(pin), "expected_source": pin.to_dict(),
            "baseline_document_path": target_path}
        _call(self.state_port, _operation(request, "read_local_file_effect", payload,
            suffix="document-authorize:" + run["id"] + ":" + target_path + ":" + str(run["revision"])),
            "read_local_file_effect")

    @staticmethod
    def _pin(resolved):
        return pin_source(resolved["source_pin"])

    def preview_graph_change(self, request):
        run, _snapshot, _context, _f5, pin, resolved = self._prepare(request)
        payload = request.get("payload", {})
        expected = pin_source(payload.get("expected_source"))
        verify_source_pin(expected, pin)
        prepared = prepare_change_set(resolved["graph"], payload.get("change_set"), request["request_id"])
        return {"change_id": prepared["change_id"], "source_pin": pin.to_dict(),
            "expected_new_source": {"graph_schema": prepared["graph"]["schema_version"],
                "graph_revision": prepared["graph"]["graph_version"], "graph_hash": prepared["source_hash"]},
            "temp_id_map": prepared["temp_id_map"], "created_ids": prepared["created_ids"],
            "retired_ids": prepared["retired_ids"], "inherited": prepared["inherited"],
            "operations": _safe_operation_summary(prepared), "change_set_hash": prepared["change_set_hash"],
            "journal_status": "preview_only", "run_id": run["id"]}

    def _host_req(self, original, operation, payload, suffix):
        return _operation(original, operation, payload, suffix=suffix)

    def _begin_effect(self, request, run, pin, resolved, *, kind, effect_id, target, old_hash, new_hash, semantic, summary):
        payload = {"effect_id": effect_id, "effect_kind": kind, "run_id": run["id"],
            "expected_run_revision": run["revision"], "repository_id": pin.repository_id,
            "project_id": pin.project_id, "canonical_workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"], "target_relative_path": target,
            "branch_key": self.runtime._branch_key(pin), "expected_source": pin.to_dict(),
            "expected_target_sha256": old_hash, "candidate_sha256": new_hash,
            "semantic_sha256": semantic, "producer_version": "hosted-files-v1", "summary": summary}
        call = self._host_req(request, "begin_local_file_effect", payload, "hosted-file-begin:" + effect_id)
        return _call(self.state_port, call, "begin_local_file_effect")

    def _checkpoint(self, request, run, pin, resolved, effect_id, effect_revision, phase, state, publication,
                    *, after_pin=None, uploaded=None, manifest_refs=None, coverage_ref=None):
        request_pin = pin_source(after_pin) if after_pin is not None else pin
        payload = {"effect_id": effect_id, "expected_effect_revision": effect_revision,
            "run_id": run["id"], "expected_run_revision": run["revision"],
            "repository_id": pin.repository_id, "project_id": pin.project_id,
            "canonical_workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"],
            "target_relative_path": publication["target_ref"], "branch_key": self.runtime._branch_key(pin),
            "expected_source": request_pin.to_dict(), "phase": phase, "state": state,
            "publication": publication, "after_source_pin": after_pin,
            "uploaded_source_ref": uploaded, "manifest_refs": manifest_refs or [],
            "coverage_ref": coverage_ref, "host_document_verified": False}
        call = self._host_req(request, "complete_local_file_effect", payload,
            "hosted-file-progress:" + effect_id + ":" + phase)
        result = _call(self.state_port, call, "complete_local_file_effect")
        return result["effect_ref"]["revision"]

    def apply_graph_change(self, request):
        from .resources import _reject_links
        payload = request.get("payload", {})
        run, _snapshot, _context, _f5, pin, resolved = self._prepare(request)
        verify_source_pin(pin_source(payload.get("expected_source")), pin)
        prepared = prepare_change_set(resolved["graph"], payload.get("change_set"), request["request_id"])
        target = resolved["relative_graph_path"]
        target_path = resolved["graph_path"]
        effect_id = str(uuid.uuid5(uuid.UUID(request["request_id"]), "hosted-local-graph-effect"))
        old_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()
        candidate_hash = hashlib.sha256(prepared["wire"]).hexdigest()
        self._begin_effect(request, run, pin, resolved, kind="graph_change", effect_id=effect_id,
            target=target, old_hash=old_hash, new_hash=candidate_hash,
            semantic=prepared["change_set_hash"], summary={"operation_count": len(prepared["operations"]),
                "node_count": len(prepared["graph"]["nodes"]), "relation_count": len(prepared["graph"]["relations"]),
                "segment_count": 0, "changed_segment_count": 0, "manual_segment_count": 0})
        stage = target_path.with_name("." + target_path.name + ".pmt-hosted-" + effect_id + ".stage")
        _reject_links(stage)
        if stage.exists():
            if hashlib.sha256(stage.read_bytes()).hexdigest() != candidate_hash:
                raise PmtError("hosted_file_stage_conflict", "Existing candidate stage has different bytes", 3)
        else:
            with stage.open("xb") as stream:
                stream.write(prepared["wire"]); stream.flush(); os.fsync(stream.fileno())
        refs = publication_refs(target_path, effect_id, resolved["workspace"])
        revision = 1
        def checkpoint(phase, meta):
            nonlocal revision
            revision = self._checkpoint(request, run, pin, resolved, effect_id, revision, phase,
                "intent", meta | {"effect_id": effect_id}, after_pin=None)
        receipt = guarded_publish(target_path, stage, old_hash, effect_id, resolved["workspace"], checkpoint)
        if receipt.get("status") != "published":
            self._checkpoint(request, run, pin, resolved, effect_id, revision, "conflict", "conflict", receipt)
            raise PmtError("hosted_file_publish_conflict", "Graph file changed during safe publication", 3,
                details={"phase": receipt.get("phase"), "target_ref": receipt.get("target_ref")})
        _reject_links(target_path)
        inspected = inspect_graph_source(resolved["workspace"], target_path, pin.repository_id, pin.project_id,
            pre_read_validator=None)
        after_pin = inspected["source_pin"]
        if (after_pin.graph_hash != prepared["source_hash"] or after_pin.reviewed_commit != pin.reviewed_commit
                or after_pin.selected_ref != pin.selected_ref):
            raise PmtError("hosted_file_source_conflict", "Published graph differs from the validated candidate", 3)
        resource = self.state_port.publish_resource({"request_id": str(uuid.uuid5(uuid.UUID(request["request_id"]),
                "hosted-graph-snapshot")), "scope_id": pin.project_id, "purpose": "graph_snapshot",
                "sha256": candidate_hash, "size": len(prepared["wire"])}, prepared["wire"],
            session_id=request["session_id"])
        source_read = _call(self.state_port, self._host_req(request, "read_source_snapshot", {
            "repository_id": pin.repository_id, "project_id": pin.project_id,
            "canonical_workspace": resolved["canonical_workspace"], "relative_graph_path": target,
            "run_id": run["id"], "expected_run_revision": run["revision"]}, "hosted-source-read:" + effect_id),
            "read_source_snapshot")
        current_rev = source_read.get("snapshot_revision", 0)
        publish_req = self._host_req(request, "publish_source_snapshot", {
            "project_id": pin.project_id, "repository_id": pin.repository_id,
            "canonical_workspace": resolved["canonical_workspace"], "relative_graph_path": target,
            "run_id": run["id"], "expected_run_revision": run["revision"],
            "expected_source_revision": current_rev, "branch_key": self.runtime._branch_key(after_pin),
            "source_pin": after_pin.to_dict(), "graph_resource_ref": resource["artifact_ref"]},
            "hosted-source-publish:" + effect_id)
        published = _call(self.state_port, publish_req, "publish_source_snapshot")
        rebuild_req = self._host_req(request, "rebuild_graph_index", {
            "repository_id": pin.repository_id, "workspace": resolved["canonical_workspace"],
            "relative_graph_path": target, "run_id": run["id"], "expected_source": after_pin.to_dict()},
            "hosted-index-rebuild:" + effect_id)
        rebuilt = self.state_port.execute(rebuild_req)
        if rebuilt[1] or not rebuilt[0].get("ok"):
            raise PmtError("hosted_graph_index_unknown", "Graph index rebuild needs reconciliation", 4, True)
        final_revision = self._checkpoint(request, run, pin, resolved, effect_id, revision,
            "completed", "completed", receipt, after_pin=after_pin.to_dict(),
            uploaded=resource["artifact_ref"])
        return {"change_id": prepared["change_id"], "change_set_hash": prepared["change_set_hash"],
            "before_source_pin": pin.to_dict(), "source_pin": after_pin.to_dict(),
            "source_snapshot_ref": published.get("graph_resource"), "index": rebuilt[0]["result"].get("index"),
            "effect_ref": {"kind": "host_local_file_effect", "id": effect_id,
                "scope_id": pin.project_id, "revision": final_revision}, "replayed": False}

    def recover_graph_change(self, request):
        return self._recover_file_effect(request, "graph_change")

    def prepare_document_segments(self, request):
        return self._prepare_document(request)

    def publish_document_segments(self, request):
        return self._publish_document(request)

    def recover_document_segments(self, request):
        return self._recover_file_effect(request, "document_render")

    def _prepare_document(self, request):
        from .efficiency.documents import (_compose_document, _manual_segments, _parse_document,
            _render_segments, DEPENDENCY_REGISTRY_VERSION, TEMPLATE_VERSION)
        from .hosted_runtime import _write_spool_json
        from .resources import _reject_links
        from .efficiency.graph import manifest_set_fingerprint
        payload = request.get("payload", {})
        document_path = payload.get("document_path", "docs/pmt-docs/plan.md")
        run, _snapshot, _context, _f5, pin, resolved = self._prepare(request, target_path=document_path)
        path = resolved["workspace"].joinpath(*document_path.split("/"))
        _reject_links(path)
        raw = path.read_bytes() if path.exists() else b""
        if len(raw) > 16 * 1024 * 1024:
            raise PmtError("document_too_large", "Document target exceeds 16 MiB", 2)
        try:
            text = raw.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError as exc:
            raise PmtError("document_encoding_invalid", "Document must be UTF-8", 3) from exc
        parsed = _parse_document(text)
        markdown, segments, report = _render_segments(resolved["graph"], pin, document_path)
        prior_pin = pin
        apply_receipt = payload.get("apply_receipt")
        impact = payload.get("impact_set")
        if parsed["has_block"]:
            before_graph = None
            if isinstance(apply_receipt, dict):
                apply_result = apply_receipt.get("result", apply_receipt)
                prior_pin = pin_source(apply_result.get("before_source_pin"))
            baseline_payload = {"run_id": run["id"], "expected_run_revision": run["revision"],
                "repository_id": pin.repository_id, "project_id": pin.project_id,
                "canonical_workspace": resolved["canonical_workspace"],
                "relative_graph_path": resolved["relative_graph_path"], "target_relative_path": document_path,
                "branch_key": self.runtime._branch_key(pin), "expected_source": pin.to_dict(),
                "baseline_document_path": document_path}
            if prior_pin.source_hash != pin.source_hash:
                apply_result = apply_receipt.get("result", apply_receipt) if isinstance(apply_receipt, dict) else {}
                effect_ref = apply_result.get("effect_ref") if isinstance(apply_result, dict) else None
                if not isinstance(effect_ref, dict) or effect_ref.get("kind") != "host_local_file_effect":
                    raise PmtError("document_f1_receipt_unbound", "Partial rendering needs the exact F1 effect reference", 3)
                baseline_payload["baseline_source_pin"] = prior_pin.to_dict()
                baseline_payload.update(graph_effect_id=effect_ref.get("id"), change_set=payload.get("change_set"),
                    change_preview=payload.get("change_preview"), impact_set=impact)
            baseline_req = self._host_req(request, "read_local_file_effect", baseline_payload,
                "hosted-document-baseline:" + run["id"] + ":" + document_path + ":" + prior_pin.source_hash)
            baseline = _call(self.state_port, baseline_req, "read_local_file_effect")
            if prior_pin.source_hash != pin.source_hash:
                verified_f3 = baseline.get("verified_f3") if isinstance(baseline, dict) else None
                if not isinstance(verified_f3, dict) or verified_f3.get("provenance") != "host_recomputed_core_f3":
                    raise PmtError("document_f3_unverified", "Host did not return a recomputed F3 result", 3)
            prior_coverage = baseline.get("coverage") if isinstance(baseline, dict) else None
            prior_certificate = prior_coverage.get("certificate") if isinstance(prior_coverage, dict) else None
            if not isinstance(prior_certificate, dict) or prior_certificate.get("coverage_status") != "complete":
                raise PmtError("document_prior_baseline_unknown", "Host has no complete matching document baseline", 3)
            prior_manifests = baseline.get("manifest_refs")
            if not isinstance(prior_manifests, list) or not prior_manifests:
                raise PmtError("document_prior_manifest_unknown", "Host document manifest set is incomplete", 3)
            by_id = {item.get("segment_id"): item for item in prior_manifests if isinstance(item, dict)}
            if len(by_id) != len(prior_manifests) or any(item.get("manifest", {}).get("document_path") != document_path
                    for item in prior_manifests):
                raise PmtError("document_prior_manifest_scope", "Host baseline contains another document", 3)
            expected_ids = {key for key, item in by_id.items() if item["manifest"].get("ownership") != "manual"}
            if set(parsed["segments"]) != expected_ids:
                raise PmtError("document_managed_block_conflict", "Document generated segment set differs from Host baseline", 3)
            for segment_id, old_text in parsed["segments"].items():
                if hashlib.sha256(old_text.encode()).hexdigest() != by_id[segment_id]["manifest"].get("output_hash"):
                    raise PmtError("document_generated_edit_conflict", "Generated document content changed outside PMT", 3)
            # Manual spans are user-owned: a changed manual prefix/suffix is
            # preserved and gets a fresh local hash in the new manifest.
            self._document_prior = {"manifests": prior_manifests, "coverage": prior_coverage,
                "coverage_revision": prior_coverage.get("revision", 0)}
            if prior_pin.source_hash != pin.source_hash:
                before_graph = self._verify_and_load_f1_before_graph(request, run, pin, resolved,
                    prior_pin, apply_result, impact, baseline)
                self._verify_f3_document_impact(request, pin, prior_pin, before_graph,
                    resolved["graph"], prior_manifests, segments, impact, apply_result)
        else:
            self._document_prior = {"manifests": [], "coverage": {"segments": "unknown"}, "coverage_revision": 0}
        segments = _manual_segments({"scope_id": pin.project_id, "document_path": document_path,
            "source_pin": pin}, parsed, segments)
        candidate_text, _prefix = _compose_document(parsed["prefix"], parsed["suffix"],
            [item for item in segments if item["manifest"]["ownership"] == "generated"])
        candidate = candidate_text.encode("utf-8")
        render_id = str(uuid.uuid5(uuid.UUID(request["request_id"]), "hosted-document-render"))
        journal_id = str(uuid.uuid5(uuid.UUID(render_id), "hosted-document-journal"))
        stage = path.with_name("." + path.name + ".pmt-hosted-" + render_id + ".stage")
        candidate_hash = hashlib.sha256(candidate).hexdigest()
        if stage.exists():
            _reject_links(stage)
            if hashlib.sha256(stage.read_bytes()).hexdigest() != candidate_hash:
                raise PmtError("document_stage_conflict", "Existing document stage contains different bytes", 3)
        else:
            with stage.open("xb") as stream:
                stream.write(candidate); stream.flush(); os.fsync(stream.fileno())
        manifest_refs = []
        old_by_id = {item["segment_id"]: item for item in self._document_prior.get("manifests", [])}
        for segment in segments:
            prior_item = old_by_id.get(segment["segment_id"])
            manifest_refs.append({"segment_id": segment["segment_id"],
                "expected_manifest_revision": prior_item.get("revision", 0) if prior_item else 0})
        expected_nodes = sorted(node["id"] for node in resolved["graph"]["nodes"])
        field_paths = {}
        for segment in segments:
            for node_id in segment["manifest"]["node_ids"]:
                field_paths.setdefault(node_id, set()).update(segment["manifest"]["field_paths"])
        certificate = {"source_pin": pin.to_dict(), "document_paths": [document_path],
            "manifest_refs": [], "manifest_set_hash": "0" * 64,
            "expected_node_ids": expected_nodes,
            "expected_relation_ids": sorted(rel["id"] for rel in resolved["graph"]["relations"]),
            "expected_field_paths": {node_id: sorted(fields) for node_id, fields in field_paths.items()},
            "dependency_registry_version": DEPENDENCY_REGISTRY_VERSION,
            "template_versions": [TEMPLATE_VERSION], "production_receipt_ref": "document-render:" + render_id,
            "source_table_version": "graph-schema-1"}
        journal = {"schema": "pmt-hosted-document-effect-v1", "journal_id": journal_id,
            "render_id": render_id, "effect_id": str(uuid.uuid5(uuid.UUID(render_id), "hosted-document-effect")),
            "request_id": request["request_id"], "semantic_sha256": fingerprint(request),
            "actor": request["actor"], "session_id": request["session_id"],
            "scope_id": pin.project_id, "run_id": run["id"], "run_revision": run["revision"],
            "source_pin": pin.to_dict(), "canonical_workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"], "document_path": document_path,
            "expected_document_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
            "candidate_sha256": candidate_hash,
            "stage_ref": str(stage.relative_to(resolved["workspace"])).replace(os.sep, "/"),
            "segments": [{"segment_id": item["segment_id"], "manifest": item["manifest"],
                "expected_manifest_revision": next(ref["expected_manifest_revision"] for ref in manifest_refs
                    if ref["segment_id"] == item["segment_id"])} for item in segments],
            "coverage_revision": self._document_prior.get("coverage_revision", 0), "certificate": certificate}
        session_key = hashlib.sha256(request["session_id"].encode("utf-8")).hexdigest()
        spool = self.spool_root / session_key / "documents"
        spool.mkdir(parents=True, exist_ok=True)
        _reject_links(spool)
        _write_spool_json(self.spool_root, spool / (journal_id + ".json"), journal)
        self._last_document_context = (run, pin, resolved)
        return {"journal_id": journal_id, "render_id": render_id, "document_path": document_path,
            "source_pin": pin.to_dict(),
            "candidate_sha256": hashlib.sha256(candidate).hexdigest(), "candidate_bytes": len(candidate),
            "segments": [{"segment_id": item["segment_id"], "key": item["key"],
                "manifest": item["manifest"], "output_hash": item["output_hash"],
                "expected_manifest_revision": next(ref["expected_manifest_revision"] for ref in manifest_refs
                    if ref["segment_id"] == item["segment_id"])} for item in segments],
            "report": report, "effect_state": "preview_only", "run_id": run["id"]}

    def _verify_and_load_f1_before_graph(self, request, run, current_pin, resolved,
                                         prior_pin, apply_result, impact, baseline):
        from .efficiency.graph import prepare_change_set, _safe_operation_summary
        from .util import strict_json_loads
        effect_ref = apply_result.get("effect_ref")
        if (not isinstance(effect_ref, dict) or effect_ref.get("kind") != "host_local_file_effect"
                or effect_ref.get("scope_id") != current_pin.project_id):
            raise PmtError("document_f1_receipt_unbound", "F4 requires the exact F1 Host effect receipt", 3)
        payload = request["payload"]
        read_payload = {"effect_id": effect_ref.get("id"), "run_id": run["id"],
            "expected_run_revision": run["revision"], "repository_id": current_pin.repository_id,
            "project_id": current_pin.project_id, "canonical_workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"],
            "target_relative_path": resolved["relative_graph_path"],
            "branch_key": self.runtime._branch_key(current_pin), "expected_source": current_pin.to_dict()}
        read_req = self._host_req(request, "read_local_file_effect", read_payload,
            "hosted-f1-effect-read:" + str(effect_ref.get("id")))
        effect_read = _call(self.state_port, read_req, "read_local_file_effect")
        effect = effect_read.get("effect")
        if (not isinstance(effect, dict) or effect.get("effect_kind") != "graph_change"
                or effect.get("before_source_pin", {}).get("source_hash") != prior_pin.source_hash
                or effect.get("after_source_pin", {}).get("source_hash") != current_pin.source_hash):
            raise PmtError("document_f1_receipt_mismatch", "F1 effect does not bind the before/current SourcePins", 3)
        change_set = payload.get("change_set")
        preview = payload.get("change_preview")
        if not isinstance(change_set, dict) or not isinstance(preview, dict):
            raise PmtError("document_f3_inputs_missing", "Partial document publish needs its original change set and preview", 3)
        if (effect.get("semantic_sha256") != fingerprint(change_set)
                or apply_result.get("change_set_hash") != fingerprint(change_set)
                or preview.get("change_set_hash") != fingerprint(change_set)
                or impact.get("change_set_hash") != fingerprint(change_set)):
            raise PmtError("document_f1_change_mismatch", "Original change set differs from the immutable F1 effect", 3)
        graph_ref = effect.get("before_graph_resource_ref")
        if (not isinstance(graph_ref, dict) or graph_ref.get("purpose") != "graph_snapshot"
                or graph_ref.get("scope_id") != current_pin.project_id):
            raise PmtError("document_f1_before_graph_unavailable", "F1 intent has no Host-bound before-graph resource", 3)
        resource = self.state_port.read_resource(graph_ref["id"], session_id=request["session_id"],
            expected_sha256=graph_ref["sha256"], scope_id=current_pin.project_id)
        raw = resource.get("content") if isinstance(resource, dict) else None
        if (not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != graph_ref["sha256"]
                or len(raw) != graph_ref.get("size")):
            raise PmtError("document_f1_before_graph_corrupt", "Host before-graph resource failed hash/size verification", 3)
        before_graph = strict_json_loads(raw, max_bytes=8 * 1024 * 1024)
        prepared = prepare_change_set(before_graph, change_set, request["request_id"])
        wire_hash = hashlib.sha256(prepared["wire"]).hexdigest()
        if (effect.get("candidate_sha256") != wire_hash
                or prepared["source_hash"] != current_pin.graph_hash
                or prepared["graph"]["graph_version"] != current_pin.graph_revision
                or canonical_json(_safe_operation_summary(prepared)) != canonical_json(preview.get("operations"))
                or preview.get("change_id") != prepared["change_id"]
                or preview.get("expected_new_source", {}).get("graph_hash") != prepared["source_hash"]
                or apply_result.get("change_id") != prepared["change_id"]
                or apply_result.get("change_set_hash") != prepared["change_set_hash"]):
            raise PmtError("document_f1_candidate_mismatch", "F1 candidate/preview does not match the immutable before graph", 3)
        verify_source_pin(current_pin, apply_result.get("source_pin"))
        verify_source_pin(prior_pin, effect.get("before_source_pin"))
        return before_graph

    @staticmethod
    def _verify_f3_document_impact(request, current_pin, prior_pin, before_graph, current_graph,
                                   prior_manifests, current_segments, impact, apply_result):
        from .efficiency.graph import _field_semantic, _safe_operation_summary, prepare_change_set, _FIELD_RULE_VERSION
        from .efficiency.documents import TEMPLATE_VERSION
        if not isinstance(impact, dict):
            raise PmtError("document_f3_inputs_missing", "F3 impact receipt is required", 3)
        prepared = prepare_change_set(before_graph, request["payload"].get("change_set"), request["request_id"])
        if (impact.get("change_id") != prepared["change_id"]
                or impact.get("change_set_hash") != prepared["change_set_hash"]
                or impact.get("rule_version") != _FIELD_RULE_VERSION
                or impact.get("expected_new_source", {}).get("graph_hash") != prepared["source_hash"]
                or impact.get("expected_new_source", {}).get("graph_revision") != prepared["graph"]["graph_version"]
                or impact.get("expected_new_source", {}).get("graph_schema") != prepared["graph"]["schema_version"]):
            raise PmtError("document_f3_impact_mismatch", "F3 impact does not match the revalidated graph change", 3)
        verify_source_pin(prior_pin, impact.get("before_source_pin"))
        verify_source_pin(prior_pin, impact.get("source_pin"))
        operations = _safe_operation_summary(prepared)
        expected_fields = []
        unknown = []
        for operation in operations:
            if operation.get("op") not in {"create", "update"}:
                unknown.append("unsupported_relation_or_deprecation")
                continue
            fields = list(operation.get("fields") or []) + list(operation.get("clear") or [])
            hashes = operation.get("field_fingerprints") or {}
            for field in sorted(set(fields)):
                semantic = _field_semantic(field)
                if semantic == "unknown":
                    unknown.append("unknown_field_semantics")
                expected_fields.append({"node_id": operation.get("id"), "field": field,
                    "semantic": semantic, "before_hash": hashes.get(field, {}).get("before"),
                    "after_hash": hashes.get(field, {}).get("after"),
                    "change_kind": "clear" if field in (operation.get("clear") or []) else "set"})
        if unknown or impact.get("unknown") != [] or impact.get("complete") is not True:
            raise PmtError("document_f3_impact_incomplete", "F3 impact is unknown or contains unsupported changes", 3,
                {"reason_codes": sorted(set(unknown))})
        if canonical_json(impact.get("field_changes")) != canonical_json(expected_fields):
            raise PmtError("document_f3_field_mismatch", "F3 field changes differ from the revalidated ChangeSet", 3)
        old_by_id = {item["segment_id"]: item["manifest"] for item in prior_manifests}
        changed = {segment["segment_id"] for segment in current_segments
            if segment["manifest"].get("ownership") != "manual"
            and old_by_id.get(segment["segment_id"], {}).get("output_hash") != segment["output_hash"]}
        if prior_pin.graph_revision != current_pin.graph_revision:
            changed -= {segment["segment_id"] for segment in current_segments if segment.get("key") == "preamble"}
        reported = {item.get("segment_id") for item in impact.get("documents", []) if isinstance(item, dict)}
        document_path = request["payload"].get("document_path", "docs/pmt-docs/plan.md")
        expected_segment_ids = {sid for sid, manifest in old_by_id.items()
            if manifest.get("document_path") == document_path}
        if not changed <= reported or not reported <= expected_segment_ids:
            raise PmtError("document_f3_document_scope_mismatch", "F3 document refs do not cover exactly the affected baseline scope", 3,
                {"missing_segment_count": len(changed - reported), "foreign_segment_count": len(reported - expected_segment_ids)})
        if (apply_result.get("change_id") != prepared["change_id"]
                or apply_result.get("change_set_hash") != prepared["change_set_hash"]
                or current_pin.graph_hash != prepared["source_hash"] or current_pin.graph_revision != prepared["graph"]["graph_version"]):
            raise PmtError("document_f3_apply_mismatch", "Current graph differs from F3/F1 applied candidate", 3)

    def _document_journal_path(self, request, journal_id):
        _uuid(journal_id, "journal_id")
        session_key = hashlib.sha256(request["session_id"].encode("utf-8")).hexdigest()
        root = self.spool_root / session_key / "documents"
        from .resources import _reject_links
        if not root.exists():
            raise PmtError("document_journal_not_found", "Prepared local document journal is unavailable", 2)
        _reject_links(root)
        return root, root / (journal_id + ".json")

    def _publish_document(self, request):
        from .hosted_runtime import HostedLocalRuntime, _write_spool_json
        from .resources import _reject_links
        from .efficiency.graph import manifest_set_fingerprint
        payload = request.get("payload", {})
        root, journal_path = self._document_journal_path(request, payload.get("journal_id"))
        journal = HostedLocalRuntime._json_file(journal_path)
        if not isinstance(journal, dict) or journal.get("schema") != "pmt-hosted-document-effect-v1":
            raise PmtError("document_journal_invalid", "Prepared document journal is missing or invalid", 3)
        if (journal.get("scope_id") != request.get("scope_id") or journal.get("run_id") != payload.get("run_id")
                or journal.get("run_revision") != payload.get("expected_run_revision")
                or journal.get("actor") != request.get("actor")
                or journal.get("session_id") != request.get("session_id")):
            raise PmtError("document_journal_owner_conflict", "Prepared document journal belongs to another current run", 3)
        run, _snapshot, _context, _f5, pin, resolved = self._prepare(request, target_path=journal["document_path"])
        if pin.source_hash != journal["source_pin"].get("source_hash"):
            raise PmtError("document_source_stale", "Current SourcePin differs from prepared document", 3)
        if (resolved["canonical_workspace"] != journal["canonical_workspace"]
                or resolved["relative_graph_path"] != journal["relative_graph_path"]):
            raise PmtError("document_mapping_stale", "Current local workspace mapping differs from prepared document", 3)
        stage = resolved["workspace"].joinpath(*journal["stage_ref"].split("/"))
        target = resolved["workspace"].joinpath(*journal["document_path"].split("/"))
        _reject_links(stage); _reject_links(target)
        if not stage.is_file() or hashlib.sha256(stage.read_bytes()).hexdigest() != journal["candidate_sha256"]:
            raise PmtError("document_stage_conflict", "Prepared document candidate is missing or changed", 3)
        old_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        effect_id = journal["effect_id"]
        prepared = self._begin_effect(request, run, pin, resolved, kind="document_render", effect_id=effect_id,
            target=journal["document_path"], old_hash=journal["expected_document_sha256"], new_hash=journal["candidate_sha256"],
            semantic=journal["semantic_sha256"], summary={"operation_count": 0, "node_count": len(resolved["graph"]["nodes"]),
                "relation_count": len(resolved["graph"]["relations"]), "segment_count": len(journal["segments"]),
                "changed_segment_count": len(journal["segments"]),
                "manual_segment_count": sum(item["manifest"].get("ownership") == "manual" for item in journal["segments"])})
        effect_read_req = self._host_req(request, "read_local_file_effect", {
            "effect_id": effect_id, "run_id": run["id"], "expected_run_revision": run["revision"],
            "repository_id": pin.repository_id, "project_id": pin.project_id,
            "canonical_workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"],
            "target_relative_path": journal["document_path"],
            "branch_key": self.runtime._branch_key(pin), "expected_source": pin.to_dict()},
            "hosted-document-effect-read:" + effect_id)
        current_effect = _call(self.state_port, effect_read_req, "read_local_file_effect")
        effect_revision = current_effect["effect_ref"]["revision"]
        if current_effect.get("effect_state") == "completed":
            stored = current_effect["effect"]
            return {"journal_id": journal["journal_id"], "render_id": journal["render_id"],
                "document_path": journal["document_path"], "document_hash": journal["candidate_sha256"],
                "source_pin": pin.to_dict(), "manifest_count": len(stored.get("manifest_refs", [])),
                "manifest_refs": stored.get("manifest_refs", []), "coverage": stored.get("coverage_ref"),
                "effect_ref": current_effect["effect_ref"], "publication": stored.get("publication"),
                "host_document_verified": False, "replayed": True}
        if current_effect.get("effect_state") in {"conflict", "reconcile_required"}:
            raise PmtError("document_effect_reconcile_required", "Host document effect requires explicit reconciliation", 3)
        expected_document_hash = journal["expected_document_sha256"]
        detached_original_verified = False
        prior_publication = current_effect.get("effect", {}).get("publication")
        if old_hash is None and expected_document_hash is not None and isinstance(prior_publication, dict):
            recovery_ref = prior_publication.get("recovery_ref")
            if prior_publication.get("phase") == "target_detached" and isinstance(recovery_ref, str):
                recovery_path = resolved["workspace"].joinpath(*recovery_ref.split("/"))
                try:
                    recovery_path.resolve(strict=True).relative_to(resolved["workspace"])
                    _reject_links(recovery_path)
                    detached_original_verified = (recovery_path.is_file()
                        and hashlib.sha256(recovery_path.read_bytes()).hexdigest() == expected_document_hash)
                except (OSError, ValueError):
                    detached_original_verified = False
        if (old_hash != expected_document_hash and old_hash != journal["candidate_sha256"]
                and not detached_original_verified):
            raise PmtError("document_target_changed", "Document matches neither prepared nor published bytes; preserved", 3)
        if old_hash == journal["candidate_sha256"] and current_effect.get("effect_state") != "intent":
            raise PmtError("document_effect_reconcile_required", "Candidate exists without a matching active Host intent", 3)
        pub_refs = publication_refs(target, effect_id, resolved["workspace"])
        checkpoint_receipts = {item.get("phase"): item.get("publication")
            for item in current_effect.get("effect", {}).get("history", []) if isinstance(item, dict)}
        checkpoint_receipts[current_effect.get("effect", {}).get("phase")] = current_effect.get("effect", {}).get("publication")
        def checkpoint(phase, publication):
            nonlocal effect_revision
            if phase in checkpoint_receipts and _same_publication_checkpoint(checkpoint_receipts[phase], publication):
                return
            effect_revision = self._checkpoint(request, run, pin, resolved, effect_id, effect_revision,
                phase, "intent", publication, after_pin=pin.to_dict())
            checkpoint_receipts[phase] = publication
            journal["phase"] = phase
            journal["publication"] = publication
            _write_spool_json(self.spool_root, journal_path, journal)
        publication = guarded_publish(target, stage, expected_document_hash, effect_id, resolved["workspace"], checkpoint)
        if publication.get("status") != "published":
            self._checkpoint(request, run, pin, resolved, effect_id, effect_revision,
                "conflict", "conflict", publication, after_pin=pin.to_dict())
            raise PmtError("document_publish_conflict", "Safe document publication found changed bytes", 3,
                details={"phase": publication.get("phase"), "recovery_ref": publication.get("recovery_ref")})
        # Re-read both owner/scope and target bytes before registering any metadata.
        self._prepare(request, target_path=journal["document_path"])
        if hashlib.sha256(target.read_bytes()).hexdigest() != journal["candidate_sha256"]:
            raise PmtError("document_publish_conflict", "Document changed before manifest registration", 3)
        rebuild_req = self._host_req(request, "rebuild_graph_index", {
            "repository_id": pin.repository_id, "workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"], "run_id": run["id"],
            "expected_source": pin.to_dict()}, "hosted-document-index:" + effect_id)
        rebuild, rebuild_code = self.state_port.execute(rebuild_req)
        if rebuild_code or not rebuild.get("ok"):
            raise PmtError("document_index_unknown", "Host index rebuild needs reconciliation", 4, True)
        manifest_refs = []
        for item in journal["segments"]:
            manifest = item["manifest"]
            register_req = self._host_req(request, "register_segment_manifest", {
                "repository_id": pin.repository_id, "workspace": resolved["canonical_workspace"],
                "relative_graph_path": resolved["relative_graph_path"], "run_id": run["id"],
                "document_path": journal["document_path"], "expected_source": pin.to_dict(),
                "expected_manifest_revision": item["expected_manifest_revision"], "manifest": manifest},
                "hosted-document-manifest:" + effect_id + ":" + item["segment_id"])
            registered, code = self.state_port.execute(register_req)
            if code or not registered.get("ok"):
                raise PmtError("document_manifest_unknown", "Host manifest registration needs reconciliation", 4, True,
                    {"segment_id": item["segment_id"]})
            value = registered["result"]
            manifest_refs.append({"segment_id": item["segment_id"], "manifest_hash": value["manifest_hash"],
                "revision": value["manifest_revision"]})
        refs_for_certificate = [{"segment_id": item["segment_id"], "manifest_hash": item["manifest_hash"]}
                                for item in manifest_refs]
        certificate = dict(journal["certificate"])
        certificate["manifest_refs"] = refs_for_certificate
        certificate["manifest_set_hash"] = manifest_set_fingerprint(refs_for_certificate,
            certificate["document_paths"], pin)
        coverage_req = self._host_req(request, "register_segment_manifest", {
            "repository_id": pin.repository_id, "workspace": resolved["canonical_workspace"],
            "relative_graph_path": resolved["relative_graph_path"], "run_id": run["id"],
            "document_path": journal["document_path"], "expected_source": pin.to_dict(),
            "expected_coverage_revision": journal["coverage_revision"], "coverage_certificate": certificate},
            "hosted-document-coverage:" + effect_id)
        coverage, code = self.state_port.execute(coverage_req)
        if code or not coverage.get("ok"):
            error = coverage.get("error") if isinstance(coverage, dict) else None
            raise PmtError("document_coverage_unknown", "Host coverage registration needs reconciliation", 4, True,
                {"host_error_code": error.get("code") if isinstance(error, dict) else None})
        coverage_result = coverage["result"]
        receipt_refs = [{"segment_id": item["segment_id"], "manifest_hash": item["manifest_hash"],
            "revision": item["revision"]} for item in manifest_refs]
        publication_result = {key: publication.get(key) for key in
            ("status", "target_ref", "candidate_ref", "manifest_ref", "effect_id", "expected_hash",
             "original_hash", "candidate_hash", "recovery_ref", "phase", "durability_warning")}
        effect_revision = self._checkpoint(request, run, pin, resolved, effect_id, effect_revision,
            "completed", "completed", publication_result, after_pin=pin.to_dict(),
            manifest_refs=receipt_refs, coverage_ref={"revision": coverage_result["coverage_revision"],
                "certificate_hash": coverage_result["coverage"]["certificate_hash"],
                "manifest_set_hash": certificate["manifest_set_hash"]})
        journal["phase"] = "completed"
        _write_spool_json(self.spool_root, journal_path, journal)
        return {"journal_id": journal["journal_id"], "render_id": journal["render_id"],
            "document_path": journal["document_path"], "document_hash": journal["candidate_sha256"],
            "source_pin": pin.to_dict(), "manifest_count": len(manifest_refs),
            "manifest_refs": receipt_refs, "coverage": coverage_result["coverage"],
            "effect_ref": {"kind": "host_local_file_effect", "id": effect_id,
                "scope_id": pin.project_id, "revision": effect_revision},
            "publication": publication_result, "host_document_verified": False}

    def _recover_file_effect(self, request, kind):
        payload = request.get("payload", {})
        original = payload.get("original_request")
        expected_operation = "apply_graph_change" if kind == "graph_change" else "publish_document_segments"
        if not isinstance(original, dict) or original.get("operation") != expected_operation:
            raise PmtError("hosted_file_recovery_invalid", "Recovery requires the exact original file-effect request", 2)
        if (original.get("actor") != request.get("actor") or original.get("session_id") != request.get("session_id")
                or original.get("scope_id") != request.get("scope_id")):
            raise PmtError("hosted_file_recovery_owner_conflict", "Only the original owner and Project may recover this effect", 3)
        if kind == "document_render":
            return self._publish_document(original)
        return self._recover_graph_candidate(original)

    def _recover_graph_candidate(self, original):
        from .resources import _reject_links
        from .efficiency.graph import inspect_graph_source
        payload = original.get("payload", {})
        run_id, context_ref = payload.get("run_id"), payload.get("context_ref")
        if not isinstance(run_id, str) or not isinstance(context_ref, dict):
            raise PmtError("hosted_file_recovery_invalid", "Original graph request lacks its run and F5 context refs", 2)
        run, _snapshot = self.runtime._run(original, run_id)
        expected = pin_source(payload.get("expected_source"))
        mapping = self.runtime.workspace_mapping_provider(run, expected.to_dict())
        if (not isinstance(mapping, dict) or mapping.get("project_id") != original.get("scope_id")
                or mapping.get("repository_id") != expected.repository_id
                or run.get("workspace") != canonical_workspace(expected.repository_id, self.runtime._branch_key(expected))):
            raise PmtError("workspace_mapping_conflict", "Current project/workspace mapping changed; file recovery stopped", 3)
        effect_id = str(uuid.uuid5(uuid.UUID(original["request_id"]), "hosted-local-graph-effect"))
        read_payload = {"effect_id": effect_id, "run_id": run_id, "expected_run_revision": run["revision"],
            "repository_id": expected.repository_id, "project_id": expected.project_id,
            "canonical_workspace": run["workspace"], "relative_graph_path": mapping["relative_graph_path"],
            "target_relative_path": mapping["relative_graph_path"],
            "branch_key": self.runtime._branch_key(expected), "expected_source": expected.to_dict()}
        current_read = _call(self.state_port, self._host_req(original, "read_local_file_effect", read_payload,
            "graph-recovery-read:" + effect_id), "read_local_file_effect")
        body = current_read.get("effect")
        if (not isinstance(body, dict) or body.get("effect_kind") != "graph_change"
                or body.get("owner", {}).get("actor") != original.get("actor")
                or body.get("run_id") != run_id or body.get("target_relative_path") != mapping["relative_graph_path"]):
            raise PmtError("hosted_file_recovery_owner_conflict", "F1 intent does not match the current run and target", 3)
        before_pin = pin_source(body.get("before_source_pin"))
        if before_pin.source_hash != expected.source_hash:
            raise PmtError("hosted_file_recovery_source_conflict", "Original request no longer matches the immutable F1 SourcePin", 3)
        workspace = Path(mapping.get("local_root", mapping.get("local_workspace"))).expanduser().resolve(strict=True)
        target = workspace.joinpath(*mapping["relative_graph_path"].split("/"))
        try:
            target.parent.resolve(strict=True).relative_to(workspace)
        except (OSError, ValueError) as exc:
            raise PmtError("hosted_file_recovery_path_invalid", "Current graph parent escaped its mapped checkout", 3) from exc
        _reject_links(workspace); _reject_links(target.parent); _reject_links(target)
        if target.exists() and not target.is_file():
            raise PmtError("hosted_file_recovery_path_invalid", "Current graph target is not a regular file", 3)
        stage = target.with_name("." + target.name + ".pmt-hosted-" + effect_id + ".stage")
        _reject_links(stage)
        old_hash = body.get("expected_target_sha256")
        candidate_hash = body.get("candidate_sha256")
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        current_source_pin = current_read.get("current_source_pin")
        if actual_hash == old_hash:
            if not isinstance(current_source_pin, dict) or current_source_pin.get("source_hash") != before_pin.source_hash:
                raise PmtError("hosted_file_recovery_source_conflict", "Host source pointer changed while graph target remains original", 3)
            return self.apply_graph_change(original)
        body_publication = body.get("publication") or {}
        detached = actual_hash is None and body_publication.get("phase") in {"original_preserved", "target_detached"}
        if detached:
            recovery_ref = body_publication.get("recovery_ref")
            if not isinstance(recovery_ref, str) or old_hash is None:
                raise PmtError("hosted_file_recovery_conflict", "Detached original has no pinned recovery reference", 3)
            recovery_path = workspace.joinpath(*recovery_ref.split("/"))
            try:
                recovery_path.resolve(strict=True).relative_to(workspace)
                _reject_links(recovery_path)
            except (OSError, ValueError) as exc:
                raise PmtError("hosted_file_recovery_conflict", "Detached original recovery path is unavailable", 3) from exc
            if not recovery_path.is_file() or hashlib.sha256(recovery_path.read_bytes()).hexdigest() != old_hash:
                raise PmtError("hosted_file_recovery_conflict", "Detached original bytes differ from the immutable expected hash", 3)
        elif actual_hash != candidate_hash:
            raise PmtError("hosted_file_recovery_conflict", "Graph bytes match neither the original nor F1 candidate; both remain untouched", 3,
                details={"target_sha256": actual_hash, "candidate_sha256": candidate_hash})
        if not stage.is_file():
            candidate_raw = target.read_bytes() if target.exists() else b""
            candidate_stage_ref = body_publication.get("candidate_stage_ref")
            if (not candidate_raw and detached and isinstance(candidate_stage_ref, str)):
                candidate_stage = workspace.joinpath(*candidate_stage_ref.split("/"))
                try:
                    candidate_stage.resolve(strict=True).relative_to(workspace)
                    _reject_links(candidate_stage)
                    candidate_raw = candidate_stage.read_bytes()
                except (OSError, ValueError):
                    candidate_raw = b""
            if hashlib.sha256(candidate_raw).hexdigest() != candidate_hash:
                raise PmtError("hosted_file_recovery_stage_missing", "Pinned F1 candidate bytes are unavailable for recovery", 3)
            with stage.open("xb") as stream:
                stream.write(candidate_raw); stream.flush(); os.fsync(stream.fileno())
        if hashlib.sha256(stage.read_bytes()).hexdigest() != candidate_hash:
            raise PmtError("hosted_file_recovery_stage_conflict", "F1 candidate stage hash changed", 3)
        if before_pin.source_kind == "git":
            import subprocess
            head = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                capture_output=True, check=False, timeout=10)
            if head.returncode != 0 or head.stdout.decode("ascii", errors="ignore").strip() != before_pin.reviewed_commit:
                raise PmtError("hosted_file_recovery_source_conflict", "Current Git commit changed; candidate remains preserved", 3)
            if before_pin.selected_ref is not None:
                branch = subprocess.run(["git", "-C", str(workspace), "symbolic-ref", "--quiet", "--short", "HEAD"],
                    capture_output=True, check=False, timeout=10)
                if branch.returncode != 0 or branch.stdout.decode("utf-8", errors="ignore").strip() != before_pin.selected_ref:
                    raise PmtError("hosted_file_recovery_source_conflict", "Current Git branch changed; candidate remains preserved", 3)
        raw_candidate = stage.read_bytes()
        uploaded = body.get("uploaded_source_ref")
        current_source_pin = current_read.get("current_source_pin")
        after_pin = pin_source(body.get("after_source_pin")) if body.get("after_source_pin") else None
        current_source_ref = current_read.get("current_source_ref")
        source_already_published = isinstance(current_source_pin, dict) and current_source_pin.get("source_hash") != before_pin.source_hash
        if source_already_published:
            from .planning.graph import validate_graph
            try:
                candidate_graph = json.loads(raw_candidate.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise PmtError("hosted_file_recovery_candidate_invalid", "Pinned F1 candidate stage is invalid JSON", 3) from exc
            candidate_report = validate_graph(candidate_graph, before_pin.project_id, complete=False)
            after_pin = pin_source(current_source_pin)
            current_graph_ref = current_source_ref.get("graph_resource_ref") if isinstance(current_source_ref, dict) else None
            if (actual_hash not in {candidate_hash, None}
                    or after_pin.repository_id != before_pin.repository_id or after_pin.project_id != before_pin.project_id
                    or after_pin.selected_ref != before_pin.selected_ref or after_pin.reviewed_commit != before_pin.reviewed_commit
                    or after_pin.graph_hash != candidate_report.get("sha256")
                    or not isinstance(current_graph_ref, dict) or current_graph_ref.get("sha256") != candidate_hash):
                raise PmtError("hosted_file_recovery_source_conflict", "Host source pointer does not match the pinned candidate bytes", 3)
            uploaded = current_graph_ref
        publication_revision = current_read["effect_ref"]["revision"]
        checkpoint_receipts = {item.get("phase"): item.get("publication")
            for item in body.get("history", []) if isinstance(item, dict)}
        checkpoint_receipts[body.get("phase")] = body.get("publication")
        def checkpoint(phase, receipt):
            nonlocal publication_revision
            if phase in checkpoint_receipts and _same_publication_checkpoint(checkpoint_receipts[phase], receipt):
                return
            publication_revision = self._checkpoint(original, run, before_pin, {
                "canonical_workspace": run["workspace"], "relative_graph_path": mapping["relative_graph_path"],
                "workspace": workspace}, effect_id, publication_revision, phase, "intent", receipt,
                after_pin=after_pin.to_dict() if source_already_published else None,
                uploaded=uploaded if source_already_published else None)
            checkpoint_receipts[phase] = receipt
        publication = guarded_publish(target, stage, old_hash, effect_id, workspace, checkpoint)
        if publication.get("status") not in {"published", "replayed"}:
            raise PmtError("hosted_file_recovery_conflict", "F1 publication manifest reports a conflicting version", 3)
        _reject_links(target)
        inspected = inspect_graph_source(workspace, target, expected.repository_id, expected.project_id,
            graph_scope_id=expected.project_id, pre_read_validator=None)
        after_pin = inspected["source_pin"]
        if (after_pin.selected_ref != before_pin.selected_ref or after_pin.reviewed_commit != before_pin.reviewed_commit
                or after_pin.graph_hash != inspected["report"]["sha256"]):
            raise PmtError("hosted_file_recovery_source_conflict", "Candidate checkout identity differs from F1's pinned branch", 3)
        artifact = uploaded or body.get("uploaded_source_ref")
        if source_already_published:
            artifact = artifact or uploaded
        elif isinstance(current_source_pin, dict) and current_source_pin.get("source_hash") == before_pin.source_hash:
            if artifact is None:
                upload_id = str(uuid.uuid5(uuid.UUID(original["request_id"]), "hosted-graph-snapshot"))
                uploaded = self.state_port.publish_resource({"request_id": upload_id,
                    "scope_id": before_pin.project_id, "purpose": "graph_snapshot",
                    "sha256": candidate_hash, "size": len(raw_candidate)}, raw_candidate,
                    session_id=original["session_id"])
                artifact = uploaded["artifact_ref"]
            source_read = _call(self.state_port, self._host_req(original, "read_source_snapshot", {
                "repository_id": before_pin.repository_id, "project_id": before_pin.project_id,
                "canonical_workspace": run["workspace"], "relative_graph_path": mapping["relative_graph_path"],
                "run_id": run_id, "expected_run_revision": run["revision"]},
                "graph-recovery-source-read:" + effect_id), "read_source_snapshot")
            publish_req = self._host_req(original, "publish_source_snapshot", {
                "project_id": before_pin.project_id, "repository_id": before_pin.repository_id,
                "canonical_workspace": run["workspace"], "relative_graph_path": mapping["relative_graph_path"],
                "run_id": run_id, "expected_run_revision": run["revision"],
                "expected_source_revision": source_read.get("snapshot_revision", 0),
                "branch_key": self.runtime._branch_key(after_pin), "source_pin": after_pin.to_dict(),
                "graph_resource_ref": artifact}, "graph-recovery-source-publish:" + effect_id)
            _call(self.state_port, publish_req, "publish_source_snapshot")
            source_already_published = True
        else:
            raise PmtError("hosted_file_recovery_source_conflict", "Current Host source is neither F1 before nor candidate SourcePin", 3)
        rebuild_req = self._host_req(original, "rebuild_graph_index", {
            "repository_id": after_pin.repository_id, "workspace": run["workspace"],
            "relative_graph_path": mapping["relative_graph_path"], "run_id": run_id,
            "expected_source": after_pin.to_dict()}, "hosted-index-rebuild:" + effect_id)
        rebuilt, rebuild_code = self.state_port.execute(rebuild_req)
        if rebuild_code or not rebuilt.get("ok"):
            raise PmtError("hosted_graph_index_unknown", "F2 index recovery needs reconciliation", 4, True)
        completed = self._checkpoint(original, run, before_pin, {
            "canonical_workspace": run["workspace"], "relative_graph_path": mapping["relative_graph_path"],
            "workspace": workspace}, effect_id, publication_revision, "completed", "completed", publication,
            after_pin=after_pin.to_dict(), uploaded=artifact)
        return {"change_id": (payload.get("change_set") or {}).get("change_id"),
            "before_source_pin": before_pin.to_dict(), "source_pin": after_pin.to_dict(),
            "effect_ref": {"kind": "host_local_file_effect", "id": effect_id,
                "scope_id": before_pin.project_id, "revision": completed},
            "index": rebuilt["result"].get("index"), "publication": publication, "recovered": True}
