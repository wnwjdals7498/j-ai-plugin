"""Hosted R2/R3 adapter: local Git/code reads, Host-owned metadata and receipts."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from .continuity import changes as local_changes
from .continuity import alignment as local_alignment
from .continuity import changes_core
from .continuity.contracts import bounded_result, budget, digest, validate_metadata
from .errors import PmtError
from .hosted_continuity import HostedContinuityClient
from .hosted_files import HostedFiles
from .hosted_runtime import _call, _operation
from .resources import _reject_links
from .storage_config import adapt_host_request, mapping_for_request
from .util import utc_now
from .workspace import _scope_has_path

OPERATIONS = {"collect_changes", "build_implementation_links", "read_change_slice",
              "register_observed_change", "assess_alignment", "propose_semantic_resolution",
              "apply_alignment", "read_applicability"}
READ_OPERATIONS = set()
WRITE_OPERATIONS = {"register_observed_change", "propose_semantic_resolution"}
FILE_OPERATIONS = OPERATIONS - READ_OPERATIONS - WRITE_OPERATIONS


def _semantic_hash(request):
    ignored = {"request_id", "correlation_id", "received_at", "received_at_utc", "retry_count", "attempt"}
    return digest({key: value for key, value in request.items() if key not in ignored})


class HostedChangesClient:
    """No local DB fallback; source bytes and private patches remain on this client."""

    def __init__(self, profile, state_port, data_root, environ=None):
        self.profile = dict(profile)
        self.state_port = state_port
        self.data_root = Path(data_root).expanduser().absolute()
        self.environ = os.environ if environ is None else environ
        spool = (self.data_root / "hosted-file-effects" / profile["namespace_id"] / profile["device_id"] /
                 profile["environment_id"] / hashlib.sha256(b"hosted-continuity").hexdigest())
        self.files = HostedFiles(self.profile, state_port, spool)
        self.runtime = self.files.runtime

    def execute(self, request):
        op = request.get("operation")
        if op not in OPERATIONS:
            raise PmtError("hosted_continuity_operation_unsupported", "Unsupported hosted continuity operation", 2)
        try:
            handler = getattr(self, op)
            result = handler(request)
            from .service import response
            return response(request["request_id"], result=bounded_result(request, result)), 0
        except PmtError as exc:
            from .service import response
            return response(request.get("request_id"), error=exc.as_dict()), exc.exit_code
        except OSError as exc:
            from .service import response
            error = PmtError("hosted_source_io_unknown", "Client source needs reconciliation", 4, True,
                             {"exception_type": type(exc).__name__})
            return response(request.get("request_id"), error=error.as_dict()), error.exit_code

    def _host(self, request, operation, payload, suffix):
        subrequest = _operation(request, operation, payload, suffix="p4-b:" + suffix)
        return _call(self.state_port, subrequest, operation)

    def _context(self, request, paths):
        payload = request.get("payload", {})
        mapping_request = dict(request) | {"payload": dict(payload)}
        mapping_request["payload"].setdefault("project_id", request.get("scope_id"))
        mapping = mapping_for_request(self.profile, mapping_request)
        if mapping is None or mapping["project_id"] != request.get("scope_id"):
            raise PmtError("storage_mapping_missing", "No unique local checkout matches this project", 3)
        adapt_host_request(self.profile, mapping_request)
        workspace = Path(mapping["local_root"]).expanduser().resolve(strict=True)
        canonical = mapping["canonical_workspace"]
        branch_key = mapping["branch"] if mapping["branch"] is not None else "non-git"
        graph_rel = mapping["relative_graph_path"]
        facts = self._host(request, "read_current_facts", {}, "facts:" + request["scope_id"])
        run_ref = next((item for item in facts.get("facts", {}).get("active_execution", [])
                        if item.get("run_ref") == payload.get("run_id")), None)
        if not run_ref:
            raise PmtError("ownership_conflict", "The selected run is not active in the Host", 3)
        run_result = self._host(request, "read_execution", {"run_id": run_ref["run_ref"]},
                                "run:" + run_ref["run_ref"])
        run = run_result.get("run")
        if (not isinstance(run, dict) or run.get("revision") != run_ref.get("revision")
                or run.get("workspace") != canonical or run.get("owner_session") != request["session_id"]):
            raise PmtError("workspace_authority_stale", "Host run owner, revision, or workspace changed", 3)
        auth_payload = {"project_id": request["scope_id"], "repository_id": mapping["repository_id"],
            "canonical_workspace": canonical, "relative_graph_path": graph_rel, "branch": mapping["branch"],
            "branch_key": branch_key, "run_id": run["id"], "expected_run_revision": run["revision"],
            "mode": "source_capture"}
        grant = self._host(request, "authorize_workspace", auth_payload,
                           "source-auth:" + run["id"] + ":" + str(run["revision"]))
        expected_owner = {"actor": request["actor"], "session_id": request["session_id"],
            "device_id": getattr(self.state_port, "device_id", None),
            "environment_id": getattr(self.state_port, "environment_id", None)}
        if grant.get("status") != "authorized" or grant.get("owner") != expected_owner:
            raise PmtError("workspace_authority_stale", "Host did not confirm this current device/session owner", 3)
        scopes = grant.get("scope_locks")
        if not isinstance(scopes, list) or not _scope_has_path(scopes, graph_rel, canonical):
            raise PmtError("scope_not_owned", "Host run does not own its graph source", 3)
        for path in sorted(set(paths + [graph_rel])):
            if not _scope_has_path(scopes, path, canonical):
                raise PmtError("scope_not_owned", "Host run does not own every selected source path", 3,
                               {"path_ref": digest(path)})
        graph_path = workspace / Path(graph_rel)
        _reject_links(graph_path)
        inspected = __import__("pmt.efficiency.source", fromlist=["inspect_graph_source"]).inspect_graph_source(
            workspace, graph_path, mapping["repository_id"], request["scope_id"], graph_scope_id=request["scope_id"])
        source = self._host(request, "read_source_metadata", {"project_id": request["scope_id"],
            "repository_id": mapping["repository_id"], "canonical_workspace": canonical,
            "relative_graph_path": graph_rel}, "source:" + inspected["source_pin"].source_hash)
        from .efficiency.source import verify_source_pin
        verify_source_pin(inspected["source_pin"], source.get("source_pin"))
        return {"mapping": mapping, "workspace": workspace, "canonical_workspace": canonical,
            "graph_rel": graph_rel, "graph": inspected["graph"], "pin": inspected["source_pin"],
            "graph_file_hash": inspected["raw_sha256"],
            "run": run, "facts": facts["facts"], "scopes": scopes, "source": source,
            "branch_key": branch_key}

    def _authorize_path(self, request, context, relative):
        path = local_changes.normalize_repo_path(relative)
        return self._host(request, "read_local_file_effect", {
            "run_id": context["run"]["id"], "expected_run_revision": context["run"]["revision"],
            "repository_id": context["mapping"]["repository_id"], "project_id": request["scope_id"],
            "canonical_workspace": context["canonical_workspace"],
            "relative_graph_path": context["graph_rel"], "target_relative_path": path,
            "branch_key": context["branch_key"], "expected_source": context["pin"].to_dict(),
            "baseline_document_path": path},
            "source-path:" + context["run"]["id"] + ":" + digest(path) + ":" + context["pin"].source_hash)

    def _authorize_before_read(self, request, context, path):
        try:
            self._authorize_path(request, context, path)
            return True, None
        except PmtError:
            return False, "path_owner_conflict"

    def _basis(self, request, object_id):
        value = self._host(request, "get_continuity_object", {"object_id": object_id, "kind": "basis"},
                           "basis:" + str(object_id))
        if value.get("scope_id") != request["scope_id"] or value.get("kind") != "basis":
            raise PmtError("basis_scope_mismatch", "Basis ref belongs to another Host project", 3)
        return value

    def _put(self, request, kind, body, *, basis_hash=None, event_id=None, suffix):
        payload = {"kind": kind, "body": body, "visibility": "shared"}
        if basis_hash is not None:
            payload["basis_hash"] = basis_hash
        if event_id is not None:
            payload["event_id"] = event_id
        return self._host(request, "put_continuity_object", payload, suffix)

    def _pointer(self, request, selector, suffix):
        return self._host(request, "read_continuity_pointer", {"selector": selector}, suffix)

    def _advance(self, request, selector, object_id, expected_revision, suffix):
        return self._host(request, "advance_continuity_pointer", {"selector": selector,
            "object_id": object_id, "expected_pointer_revision": expected_revision}, suffix)

    def _event_id(self, request, suffix):
        event = request.get("normalized_event")
        return event.get("event_id") if isinstance(event, dict) else str(uuid.uuid5(
            uuid.UUID(request["request_id"]), suffix))

    def _basis_matches(self, request, context, basis):
        body = basis.get("body", {})
        source = body.get("source", {})
        if (basis.get("kind") != "basis" or basis.get("scope_id") != request["scope_id"] or
                source.get("repository_id") != context["mapping"]["repository_id"] or
                source.get("branch") != context["mapping"]["branch"] or
                source.get("workspace_ref") != context["canonical_workspace"] or
                body.get("contract", {}).get("graph_hash") != context["pin"].graph_hash or
                body.get("contract", {}).get("graph_revision") != context["pin"].graph_revision):
            raise PmtError("basis_source_conflict", "Host basis differs from current local source or mapping", 3)
        return body

    def _read_detail(self, request, detail_ref, change_body):
        if not isinstance(detail_ref, str) or not detail_ref.startswith("client-spool:sha256:"):
            return None
        value = _read_private(_spool_root(self.data_root, self.profile, request), detail_ref.rsplit(":", 1)[-1])
        if value.get("origin_request_ref") != change_body.get("origin_request_ref"):
            raise PmtError("change_detail_mismatch", "Private detail belongs to another source capture", 3)
        return value

    def _inventory_detail(self, request, basis):
        from .hosted_continuity import read_private_inventory_detail
        source = basis["body"].get("source", {})
        detail = read_private_inventory_detail(self.data_root, self.profile, request["session_id"],
                                               source.get("inventory_ref"))
        if detail.get("inventory_hash") != source.get("inventory_hash"):
            raise PmtError("basis_detail_mismatch", "Private inventory differs from the selected Host basis", 3)
        return detail

    def _replay_change(self, request, selector, observed_body):
        semantic_hash = _semantic_hash(request)
        values = self._host(request, "list_continuity_objects", {"kind": "change", "limit": 200},
                            "change-replay-list:" + request["request_id"])
        found = [item for item in values if item.get("body", {}).get("origin_request_ref") == request["request_id"]]
        if not found:
            return None
        if len(found) != 1 or found[0]["body"].get("request_semantic_hash") != semantic_hash:
            raise PmtError("request_conflict", "Original change request is bound to different input", 3)
        prior_body = found[0]["body"]
        for field in ("observed_head", "dirty_fingerprint", "selected_scope_hash", "facts", "coverage"):
            if prior_body.get(field) != observed_body.get(field):
                raise PmtError("basis_source_conflict", "Source changed after the original change request", 3,
                               {"reason_code": "source_changed_since_original_observation"})
        pointer = self._pointer(request, selector, "change-replay-pointer:" + request["request_id"])
        if (pointer.get("object_id") == found[0]["id"] or
                found[0]["body"].get("coverage") != "complete"):
            return {"change_ref": found[0]["id"], "change_hash": found[0]["body_hash"],
                "state": found[0]["body"].get("state"), "coverage": found[0]["body"].get("coverage"),
                "reason_codes": found[0]["body"].get("reason_codes", []),
                "observed_head": found[0]["body"].get("observed_head"),
                "path_count": len(found[0]["body"].get("facts", [])),
                "after_basis_ref": found[0]["body"].get("after_basis_ref"), "pointer": pointer,
                "after_basis_hash": found[0]["body"].get("after_basis_hash"),
                "local_detail_ref": "client-spool:sha256:" + found[0]["body"].get("detail_sha256", ""),
                "provenance": "client_git_observation", "replayed": True}
        raise PmtError("revision_conflict", "Observed change pointer has advanced since this request", 3,
                       details={"current_revision": pointer.get("revision")})

    def _find_replay(self, request, kind):
        semantic = _semantic_hash(request)
        objects = self._host(request, "list_continuity_objects", {"kind": kind, "limit": 200},
                              "replay-objects:" + kind + ":" + request["request_id"])
        found = [item for item in objects if item.get("body", {}).get("origin_request_ref") == request["request_id"]]
        if not found:
            return None
        if len(found) != 1 or found[0]["body"].get("request_semantic_hash") != semantic:
            raise PmtError("request_conflict", "Original hosted continuity request has different input", 3)
        return found[0]

    def collect_changes(self, request):
        payload = request.get("payload", {})
        paths = local_changes._paths(payload)
        ctx = self._context(request, paths)
        before = self._basis(request, payload.get("before_basis_ref"))
        after_ref = payload.get("after_basis_ref") or payload.get("current_basis_ref")
        if not isinstance(after_ref, str):
            raise PmtError("current_basis_required", "Capture a fresh source/work basis before collecting hosted changes", 3)
        after = self._basis(request, after_ref)
        for basis in (before, after):
            source = basis["body"].get("source", {})
            if (source.get("repository_id") != ctx["mapping"]["repository_id"] or
                    source.get("branch") != ctx["mapping"]["branch"] or
                    source.get("workspace_ref") != ctx["canonical_workspace"]):
                raise PmtError("basis_scope_mismatch", "Basis ref differs from the selected canonical branch", 3)
        current_pin = after["body"].get("contract", {})
        if (current_pin.get("graph_hash") != ctx["pin"].graph_hash or
                current_pin.get("graph_revision") != ctx["pin"].graph_revision):
            raise PmtError("basis_source_conflict", "Fresh Host basis does not match the current local graph", 3)
        inventory = self._inventory_detail(request, after)
        inventory_items = {item.get("relative_path"): item for item in inventory.get("items", [])
                           if isinstance(item, dict)}
        for path in paths:
            self._authorize_path(request, ctx, path)
        entries, inventory_reasons = local_changes._source_entries(ctx["workspace"], paths,
            lambda path: self._authorize_before_read(request, ctx, path))
        for item in entries:
            old = inventory_items.get(item["path"])
            if not old or old.get("status") != "verified" or old.get("content_hash") != item["sha256"]:
                inventory_reasons.append("basis_inventory_mismatch")
                break
        if (after["body"].get("source", {}).get("inventory_coverage", {}).get("complete") is not True or
                any(item.get("status") != "verified" for item in inventory.get("items", []))):
            inventory_reasons.append("basis_inventory_incomplete")
        if not {item["path"] for item in entries} <= set(inventory_items):
            inventory_reasons.append("basis_inventory_scope_mismatch")
        def pre_read(path):
            return self._authorize_before_read(request, ctx, path)
        captured = local_changes._capture_git(ctx["workspace"], paths,
            before["body"].get("source", {}).get("observed_head"), pre_read=pre_read)
        if inventory_reasons:
            captured["coverage"] = "incomplete"
            captured["reasons"] = sorted(set(captured.get("reasons", []) + inventory_reasons))
        detail = local_changes._retained_detail(ctx["workspace"], paths,
            before["body"].get("source", {}).get("observed_head"), captured)
        body = local_changes._safe_change_body(request, before["id"], before, captured)
        body.update({"after_basis_ref": after["id"], "after_basis_hash": after["body_hash"],
            "scope": {"project_id": request["scope_id"],
            "repository_id": ctx["mapping"]["repository_id"]},
            "source": {"repository_id": ctx["mapping"]["repository_id"],
                "branch": ctx["mapping"]["branch"], "workspace_ref": ctx["canonical_workspace"],
                "observed_head": captured.get("head")},
            "work": {"task_id": after["body"].get("work", {}).get("task_id")},
            "conditions": {"environment_id": getattr(self.state_port, "environment_id", None)},
            "origin_request_ref": request["request_id"],
            "request_semantic_hash": _semantic_hash(request),
            "event_id": (request.get("normalized_event") or {}).get("event_id") or
                str(uuid.uuid5(uuid.UUID(request["request_id"]), "p4-observed-change-event")),
            "capture_provenance": "client_git_observation"})
        event_id = body["event_id"]
        private = {"schema": "pmt-client-change-detail-v1", "scope_id": request["scope_id"],
            "origin_request_ref": request["request_id"], "facts_hash": digest(body["facts"]),
            "basis_ref": before["id"], **detail}
        detail_hash = digest(private)
        _write_private(_spool_root(self.data_root, self.profile, request), detail_hash, private)
        body["detail_sha256"] = detail_hash
        body["change_hash"] = digest({key: value for key, value in body.items() if key != "change_hash"})
        host_body = dict(body)
        host_body["private_detail_available"] = True
        host_body["change_hash"] = digest({key: value for key, value in host_body.items() if key != "change_hash"})
        selector = {"repository_id": ctx["mapping"]["repository_id"], "branch": ctx["mapping"]["branch"],
            "workspace_ref": ctx["canonical_workspace"], "task_id": after["body"].get("work", {}).get("task_id"),
            "purpose": "change", "environment_id": getattr(self.state_port, "environment_id", None)}
        replay = self._replay_change(request, selector, body)
        if replay:
            return replay
        pointer = self._pointer(request, selector, "change-pointer-read:" + request["scope_id"] + ":" + digest(selector))
        expected = payload.get("expected_pointer_revision")
        if type(expected) is not int or expected != pointer.get("revision"):
            raise PmtError("revision_conflict", "Observed Host change pointer revision is stale", 3,
                           details={"expected_revision": expected, "current_revision": pointer.get("revision")})
        saved = self._put(request, "change", host_body, basis_hash=before["body_hash"], event_id=event_id,
                          suffix="observed-change:" + event_id)
        advanced = None
        if body["coverage"] == "complete":
            advanced = self._advance(request, selector, saved["id"], expected, "change-pointer-advance:" + event_id)
        return {"change_ref": saved["id"], "change_hash": saved["body_hash"],
            "state": body["state"], "coverage": body["coverage"], "reason_codes": body["reason_codes"],
            "observed_head": body["observed_head"], "path_count": len(body["facts"]),
            "local_detail_ref": "client-spool:sha256:" + detail_hash, "after_basis_ref": after["id"],
            "after_basis_hash": after["body_hash"],
            "pointer": advanced or pointer, "provenance": "client_git_observation", "replayed": False}

    def build_implementation_links(self, request):
        payload = request.get("payload", {})
        paths = local_changes._paths(payload)
        context = self._context(request, paths)
        basis = self._basis(request, payload.get("basis_ref"))
        basis_body = self._basis_matches(request, context, basis)
        def allowed(path):
            return self._authorize_before_read(request, context, path)
        entries, unknown = local_changes._source_entries(context["workspace"], paths, allowed)
        if unknown:
            link_index = changes_core.build_link_index(source_entries=entries, graph=context["graph"],
                scope_hash=basis_body["source"]["inventory_hash"], explicit_mappings=[])
            link_index["coverage"]["complete"] = False
            link_index["coverage"]["unknown_count"] += len(unknown)
            link_index["coverage"]["reason_codes"] = unknown
        else:
            link_index = changes_core.build_link_index(source_entries=entries, graph=context["graph"],
                scope_hash=basis_body["source"]["inventory_hash"],
                explicit_mappings=self._verified_mappings(request, payload.get("mappings", []), basis, context))
        body = self._link_body(request, link_index, context, basis, paths)
        prior = self._find_replay(request, "link_index")
        if prior:
            pointer = self._pointer(request, {"repository_id": context["mapping"]["repository_id"],
                "branch": context["mapping"]["branch"], "workspace_ref": context["canonical_workspace"],
                "task_id": basis_body.get("work", {}).get("task_id"), "purpose": "link_index",
                "environment_id": getattr(self.state_port, "environment_id", None)},
                "link-index-replay-pointer:" + request["request_id"])
            return {"index_ref": prior["id"], "index_hash": prior["body_hash"],
                "coverage": prior["body"].get("coverage"), "pointer": pointer, "replayed": True}
        selector = {"repository_id": context["mapping"]["repository_id"],
            "branch": context["mapping"]["branch"], "workspace_ref": context["canonical_workspace"],
            "task_id": basis_body.get("work", {}).get("task_id"), "purpose": "link_index",
            "environment_id": getattr(self.state_port, "environment_id", None)}
        pointer = self._pointer(request, selector, "link-pointer-read:" + digest(selector))
        expected = payload.get("expected_pointer_revision")
        if type(expected) is not int or expected != pointer.get("revision"):
            raise PmtError("revision_conflict", "Host implementation-link pointer revision is stale", 3,
                           details={"expected_revision": expected, "current_revision": pointer.get("revision")})
        saved = self._put(request, "link_index", body, basis_hash=basis["body_hash"],
                          suffix="link-index:" + request["request_id"])
        advanced = None
        if body["coverage"].get("complete") is True:
            advanced = self._advance(request, selector, saved["id"], expected,
                                     "link-pointer-advance:" + request["request_id"])
        return {"index_ref": saved["id"], "index_hash": saved["body_hash"],
            "coverage": body["coverage"], "pointer": advanced or pointer}

    def _verified_mappings(self, request, mappings, basis, context):
        if not isinstance(mappings, list) or len(mappings) > 500:
            raise PmtError("mapping_invalid", "Mappings must be a bounded array")
        facts = context["facts"]
        current_decisions = {item.get("id"): item for item in facts.get("decision_refs", [])
            if isinstance(item, dict) and item.get("kind") == "decision" and item.get("state") == "Current"}
        basis_decisions = {item.get("id"): item for item in basis["body"].get("contract", {}).get("decision_refs", [])
            if isinstance(item, dict) and item.get("kind") == "decision" and item.get("state") == "Current"}
        verified = []
        for item in mappings:
            if not isinstance(item, dict) or set(item) - {"path", "node_id", "decision_ref", "reviewed_by",
                                                          "verified_mapping_ref"}:
                raise PmtError("mapping_invalid", "Mapping fields are unsupported")
            ref = item.get("decision_ref")
            if ref in current_decisions and ref in basis_decisions:
                verified.append({"path": item.get("path"), "node_id": item.get("node_id"),
                                 "decision_ref": ref, "verified_mapping_ref": ref})
        return verified

    def _link_body(self, request, index, context, basis, paths):
        path_ref = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
        source = basis["body"].get("source", {})
        body = {"rule_version": changes_core.RULE_VERSION, "basis_ref": basis["id"],
            "basis_hash": basis["body_hash"], "scope": {"project_id": request["scope_id"],
                "repository_id": context["mapping"]["repository_id"]},
            "source": {"repository_id": context["mapping"]["repository_id"],
                "branch": context["mapping"]["branch"], "workspace_ref": context["canonical_workspace"],
                "observed_head": context["pin"].reviewed_commit},
            "work": {"task_id": basis["body"].get("work", {}).get("task_id")},
            "conditions": {"environment_id": getattr(self.state_port, "environment_id", None)},
            "source_inventory_hash": index["source_inventory_hash"], "graph_hash": context["pin"].graph_hash,
            "index_hash": index["index_hash"],
            "coverage": {key: value for key, value in index["coverage"].items() if key != "unmapped_paths"},
            "entries": [{"path_ref": path_ref(item["path"]), "content_hash": item["content_hash"],
                "status": item["status"], "reason_code": item["reason_code"], "symbols": item["symbols"]}
                for item in index["entries"]],
            "links": [{"path_ref": path_ref(item["path"]), "node_ids": item["node_ids"],
                "link_state": item["link_state"], "evidence": item["evidence"],
                "source_hash": item["source_hash"], "decision_ref": item.get("decision_ref")}
                for item in index["links"]], "scope_hash": digest(sorted(paths)),
            "origin_request_ref": request["request_id"], "request_semantic_hash": _semantic_hash(request),
            "captured_at": utc_now()}
        body["index_hash"] = digest(body)
        return validate_metadata(body)

    def read_change_slice(self, request):
        payload = request.get("payload", {})
        change = self._host(request, "get_continuity_object", {
            "object_id": payload.get("change_ref"), "kind": "change"},
            "read-change:" + str(payload.get("change_ref")))
        body = change.get("body", {})
        result = {"change_ref": change["id"], "change_hash": change["body_hash"],
            "state": body.get("state"), "origin": body.get("origin"),
            "coverage": body.get("coverage"), "reason_codes": body.get("reason_codes", []),
            "facts": body.get("facts", []), "before_basis_ref": body.get("before_basis_ref"),
            "after_basis_ref": body.get("after_basis_ref"), "after_basis_hash": body.get("after_basis_hash"),
            "intent_state": body.get("intent_state", "unknown"),
            "local_detail_ref": ("client-spool:sha256:" + body["detail_sha256"])
                if _hex(body.get("detail_sha256")) else None}
        if payload.get("include_detail") is True:
            paths = local_changes._paths(payload)
            context = self._context(request, paths)
            for path in paths:
                self._authorize_path(request, context, path)
            detail_hash = body.get("detail_sha256")
            if not _hex(detail_hash):
                result["detail"] = {"available": False, "reason_code": "client_detail_unavailable"}
            else:
                detail = _read_private(_spool_root(self.data_root, self.profile, request), detail_hash)
                if (detail.get("schema") != "pmt-client-change-detail-v1" or
                        detail.get("origin_request_ref") != body.get("origin_request_ref") or
                        detail.get("facts_hash") != digest(body.get("facts", []))):
                    raise PmtError("change_detail_mismatch", "Private client detail is not bound to this Host receipt", 3)
                offset = payload.get("detail_offset", 0)
                file_offset = payload.get("file_offset", 0)
                if type(offset) is not int or offset < 0 or type(file_offset) is not int or file_offset < 0:
                    raise PmtError("change_detail_cursor_invalid", "Detail offsets must be nonnegative integers")
                diff = detail.get("diff_base64", "")
                if offset > len(diff) or offset % 4:
                    raise PmtError("change_detail_cursor_invalid", "Diff offset is outside the retained detail")
                chunk_size = max(4, min(8192, (budget(payload)["max_bytes"] // 2) // 4 * 4))
                chunk = diff[offset:offset + chunk_size]
                files = detail.get("files", [])
                page = files[file_offset:file_offset + 20]
                next_diff, next_file = offset + len(chunk), file_offset + len(page)
                result["detail"] = {"available": True, "complete": detail.get("complete"),
                    "reason_code": detail.get("reason_code"), "diff_sha256": detail.get("diff_sha256"),
                    "diff_offset": offset, "diff_base64": chunk,
                    "next_diff_offset": next_diff if next_diff < len(diff) else None,
                    "file_offset": file_offset, "files": page,
                    "file_count": len(files), "next_file_offset": next_file if next_file < len(files) else None}
        return bounded_result(request, result)

    def register_observed_change(self, request):
        payload = request.get("payload", {})
        change = self._host(request, "get_continuity_object", {
            "object_id": payload.get("change_ref"), "kind": "change"},
            "register-change:" + str(payload.get("change_ref")))
        body = change.get("body", {})
        if change.get("scope_id") != request["scope_id"] or body.get("capture_provenance") != "client_git_observation":
            raise PmtError("change_provenance_invalid", "Receipt is not a scoped client observation", 3)
        if payload.get("approved") is True or payload.get("claim_complete") is True:
            raise PmtError("untrusted_change_claim", "Caller flags cannot upgrade observed intent or coverage", 3)
        return {"change_ref": change["id"], "change_hash": change["body_hash"],
            "state": body.get("state"), "coverage": body.get("coverage"),
            "intent_state": body.get("intent_state", "unknown"), "registered": True}

    def assess_alignment(self, request):
        payload = request.get("payload", {})
        paths = local_changes._paths(payload)
        context = self._context(request, paths)
        change = self._host(request, "get_continuity_object", {"object_id": payload.get("change_ref"), "kind": "change"},
                            "assessment-change:" + str(payload.get("change_ref")))
        index = self._host(request, "get_continuity_object", {"object_id": payload.get("index_ref"), "kind": "link_index"},
                           "assessment-index:" + str(payload.get("index_ref")))
        basis = self._basis(request, payload.get("basis_ref"))
        self._basis_matches(request, context, basis)
        change_body = change.get("body", {})
        if (index.get("body", {}).get("basis_ref") != basis.get("id") or
                index.get("body", {}).get("basis_hash") != basis.get("body_hash") or
                change_body.get("after_basis_ref") != basis.get("id") or
                change_body.get("after_basis_hash") != basis.get("body_hash")):
            raise PmtError("assessment_basis_mismatch", "Change/index/basis refs are not coherent", 3)
        affected, unknown, _ = local_alignment._changed_nodes(change, index)
        delegation, delegation_unknown = self._delegations(request, context, affected)
        unknown.extend(delegation_unknown)
        typed = self._typed_impact(request, context, payload)
        if typed is not None and (typed.get("complete") is not True or typed.get("unknown")):
            unknown.append({"reason_code": "typed_graph_impact_incomplete"})
        body = {"state": "incomplete" if unknown else "assessed", "change_ref": change["id"],
            "change_hash": change["body_hash"], "index_ref": index["id"], "index_hash": index["body_hash"],
            "basis_ref": basis["id"], "basis_hash": basis["body_hash"],
            "scope": basis["body"].get("scope"), "source": basis["body"].get("source"),
            "work": basis["body"].get("work"), "conditions": basis["body"].get("conditions"),
            "observed_graph_hash": context["pin"].graph_hash, "graph_revision": context["pin"].graph_revision,
            "observed_graph_file_hash": context["graph_file_hash"],
            "affected_node_refs": affected, "affected_record_refs": self._affected_records(context["graph"], affected),
            "unknown": unknown, "unaffected_scope_hash": digest(sorted(
                set(node["id"] for node in context["graph"]["nodes"]) - set(affected))),
            "delegation_refs": delegation, "eligible_method_node_refs": [item["node_id"] for item in delegation],
            "typed_graph_impact": typed, "typed_graph_preview_used": typed is not None,
            "typed_graph_preview_hash": digest(payload.get("typed_graph_preview")) if typed is not None else None,
            "raw_change_was_typed_graph_delta": False, "semantic_state": "unknown",
            "judgment_required": not bool(delegation) or bool(unknown),
            "required_action": "native_main_review" if delegation and not unknown else
                "decision_event_ref_lookup_required" if delegation else "user_choice_or_more_evidence",
            "origin_request_ref": request["request_id"], "request_semantic_hash": _semantic_hash(request),
            "captured_at": utc_now(), "rule_version": "p4-alignment-host-1"}
        body["assessment_hash"] = digest(body)
        prior = self._find_replay(request, "assessment")
        if prior:
            return {"assessment_ref": prior["id"], "assessment_hash": prior["body_hash"],
                "state": prior["body"].get("state"),
                "affected_node_refs": prior["body"].get("affected_node_refs", []),
                "unknown": prior["body"].get("unknown", []),
                "required_action": prior["body"].get("required_action"),
                "typed_graph_impact": prior["body"].get("typed_graph_impact"), "replayed": True}
        saved = self._put(request, "assessment", body, basis_hash=basis["body_hash"],
                          suffix="assessment:" + request["request_id"])
        return {"assessment_ref": saved["id"], "assessment_hash": saved["body_hash"],
            "state": body["state"], "affected_node_refs": affected, "unknown": unknown,
            "required_action": body["required_action"], "typed_graph_impact": typed}

    def _delegations(self, request, context, affected):
        decisions = {item.get("id"): item for item in context["facts"].get("decision_refs", [])
            if isinstance(item, dict) and item.get("kind") == "decision" and item.get("state") == "Current"}
        summaries = {item.get("decision_ref"): item for item in context["facts"].get("decision_summaries", [])
            if isinstance(item, dict)}
        output, unknown = [], []
        for node_id in affected:
            node = next((item for item in context["graph"]["nodes"] if item.get("id") == node_id), None)
            if (not node or node.get("tree_kind") != "implementation" or
                    node.get("stop_reason") != "user_delegated" or
                    node.get("autonomy", {}).get("authority") != "user"):
                continue
            for decision_ref in node.get("source_refs", []):
                decision, summary = decisions.get(decision_ref), summaries.get(decision_ref, {})
                if (decision and summary.get("delegation_scope") == node.get("delegated_scope")):
                    try:
                        receipt = self._host(request, "read_decision_receipt", {
                            "decision_ref": decision_ref, "decision_revision": decision.get("revision"),
                            "expected_kind": "delegate", "run_id": context["run"]["id"],
                            "expected_run_revision": context["run"]["revision"]},
                            "delegation-decision-receipt:" + decision_ref + ":" + str(decision.get("revision")))
                    except PmtError as exc:
                        if exc.code == "decision_receipt_unavailable":
                            unknown.append({"reason_code": "delegation_decision_receipt_unavailable",
                                "node_ref": node_id, "decision_ref": decision_ref})
                            continue
                        raise
                    affected_records = set(self._affected_records(context["graph"], [node_id]))
                    if (receipt.get("state") != "Current" or receipt.get("kind") != "delegate" or
                            receipt.get("scope_id") != request["scope_id"] or
                            receipt.get("revision") != decision.get("revision") or
                            receipt.get("decision_ref") != decision_ref or
                            receipt.get("event_ref") is None or
                            receipt.get("target_ref") not in affected_records):
                        unknown.append({"reason_code": "delegation_decision_receipt_mismatch",
                            "node_ref": node_id, "decision_ref": decision_ref})
                        continue
                    output.append({"node_id": node_id, "decision_ref": decision_ref,
                        "decision_revision": receipt["revision"],
                        "decision_event_ref": receipt["event_ref"],
                        "decision_event_ref_hash": receipt.get("event_ref_hash"),
                        "decision_target_ref": receipt["target_ref"],
                        "scope_hash": digest(node.get("delegated_scope"))})
        return output, unknown

    @staticmethod
    def _affected_records(graph, node_refs):
        result = set()
        for node in graph["nodes"]:
            if node.get("id") not in node_refs:
                continue
            refs = node.get("work_item_step_refs", {})
            if isinstance(refs, dict):
                result.update(ref for values in refs.values() if isinstance(values, list)
                              for ref in values if isinstance(ref, str))
        return sorted(result)

    def _typed_impact(self, request, context, payload):
        preview, change_set = payload.get("typed_graph_preview"), payload.get("change_set")
        if preview is None and change_set is None:
            return None
        if not isinstance(preview, dict) or not isinstance(change_set, dict):
            raise PmtError("graph_preview_required", "Typed F1 ChangeSet and preview are required", 2)
        from .efficiency.graph import prepare_change_set, _safe_operation_summary
        prepared = prepare_change_set(context["graph"], change_set, request["request_id"])
        if (preview.get("change_id") != prepared["change_id"] or
                preview.get("change_set_hash") != prepared["change_set_hash"] or
                preview.get("operations") != _safe_operation_summary(prepared) or
                preview.get("source_pin") != context["pin"].to_dict() or
                preview.get("expected_new_source", {}).get("graph_hash") != prepared["source_hash"]):
            raise PmtError("graph_preview_conflict", "Typed F1 preview differs from current source", 3)
        return self._host(request, "calculate_graph_impact", {
            "repository_id": context["mapping"]["repository_id"], "workspace": context["canonical_workspace"],
            "relative_graph_path": context["graph_rel"], "run_id": context["run"]["id"],
            "expected_source": context["pin"].to_dict(), "change_preview": preview,
            "change_set": change_set, "rule_version": "graph-field-semantics-1", "max_depth": 4},
            "typed-f3-impact:" + preview["change_id"])

    def propose_semantic_resolution(self, request):
        payload = request.get("payload", {})
        assessment = self._host(request, "get_continuity_object",
            {"object_id": payload.get("assessment_ref"), "kind": "assessment"},
            "resolution-assessment:" + str(payload.get("assessment_ref")))
        kind = payload.get("resolution_kind")
        if kind not in {"method", "premise", "unclear"}:
            raise PmtError("resolution_kind_invalid", "resolution_kind must be method, premise, or unclear", 2)
        prior = self._find_replay(request, "resolution")
        if prior:
            return {"resolution_ref": prior["id"], "resolution_hash": prior["body_hash"],
                "state": prior["body"].get("state"), "decision_ref": prior["body"].get("decision_ref"),
                "required_action": prior["body"].get("required_action"), "replayed": True}
        nodes = assessment.get("body", {}).get("eligible_method_node_refs", [])
        method = kind == "method" and bool(nodes) and not assessment.get("body", {}).get("unknown")
        # Host facts do not currently expose a selected decision kind and event
        # receipt with enough proof to approve premise edits, so keep them pending.
        state = "delegated_method_candidate" if method else "awaiting_user" if kind == "premise" else "needs_review"
        body = {"state": state, "assessment_ref": assessment["id"], "assessment_hash": assessment["body_hash"],
            "resolution_kind": kind, "eligible_method_node_refs": nodes if method else [],
            "decision_ref": None, "decision_event_ref": None, "decision_revision": None,
            "decision_target_ref": None, "model_claim_is_approval": False,
            "required_action": "native_main_review" if method else "user_decision_required",
            "reason_codes": ["current_explicit_delegation_present"] if method else
                ["premise_requires_user_decision"] if kind == "premise" else ["meaning_or_authority_unknown"],
            "origin_request_ref": request["request_id"], "request_semantic_hash": _semantic_hash(request),
            "created_at": utc_now()}
        body["resolution_hash"] = digest(body)
        saved = self._put(request, "resolution", body, basis_hash=assessment["body"].get("basis_hash"),
                          suffix="resolution:" + request["request_id"])
        return {"resolution_ref": saved["id"], "resolution_hash": saved["body_hash"], "state": state,
            "decision_ref": None, "required_action": body["required_action"]}

    def apply_alignment(self, request):
        """Record an applied alignment only after client bytes and Host effects read back."""
        payload = request.get("payload", {})
        document_path = local_changes.normalize_repo_path(payload.get("document_path", "docs/pmt-docs/plan.md"))
        paths = sorted(set(local_changes._paths(payload) + [document_path]))
        context = self._context(request, paths)
        basis = self._basis(request, payload.get("basis_ref"))
        assessment = self._host(request, "get_continuity_object", {
            "object_id": payload.get("assessment_ref"), "kind": "assessment"},
            "apply-assessment:" + str(payload.get("assessment_ref")))
        resolution = self._host(request, "get_continuity_object", {
            "object_id": payload.get("resolution_ref"), "kind": "resolution"},
            "apply-resolution:" + str(payload.get("resolution_ref")))
        if (basis.get("scope_id") != request["scope_id"] or
                assessment.get("scope_id") != request["scope_id"] or
                resolution.get("scope_id") != request["scope_id"]):
            raise PmtError("alignment_reference_stale", "Alignment inputs are outside the current Host project", 3)
        assessed = assessment.get("body", {})
        resolved = resolution.get("body", {})
        if (assessed.get("state") != "assessed" or assessed.get("unknown") or
                assessed.get("basis_ref") != basis.get("id") or
                assessed.get("basis_hash") != basis.get("body_hash") or
                resolved.get("assessment_ref") != assessment.get("id") or
                resolved.get("assessment_hash") != assessment.get("body_hash") or
                resolved.get("state") not in {"delegated_method_candidate", "premise_decision_recorded"}):
            raise PmtError("alignment_resolution_incomplete",
                "A complete assessment and current decision/delegation resolution are required", 3)
        source = basis.get("body", {}).get("source", {})
        basis_graph = basis.get("body", {}).get("contract", {}).get("graph_hash")
        if (source.get("repository_id") != context["mapping"]["repository_id"] or
                source.get("branch") != context["mapping"]["branch"] or
                source.get("workspace_ref") != context["canonical_workspace"] or
                assessed.get("observed_graph_hash") != basis_graph):
            raise PmtError("alignment_basis_source_mismatch",
                "Assessment before graph and basis source are not the same current mapping", 3)

        graph_ref = payload.get("graph_effect_ref")
        document_ref = payload.get("document_effect_ref")
        graph = self._read_file_effect(request, context, graph_ref, context["graph_rel"], "graph_change")
        document = self._read_file_effect(request, context, document_ref, document_path, "document_render")
        before_pin = graph.get("before_source_pin", {})
        after_pin = graph.get("after_source_pin", {})
        publication = document.get("publication") or {}
        if (before_pin.get("graph_hash") != assessed.get("observed_graph_hash") or
                before_pin.get("graph_hash") != basis_graph or
                after_pin.get("source_hash") != context["pin"].source_hash or
                document.get("after_source_pin", {}).get("source_hash") != context["pin"].source_hash or
                graph.get("target_relative_path") != context["graph_rel"] or
                document.get("target_relative_path") != document_path or
                document.get("host_document_verified") is not False or
                publication.get("status") not in {"published", "replayed"} or
                publication.get("candidate_hash") != document.get("candidate_sha256") or
                not document.get("manifest_refs") or not document.get("coverage_ref")):
            raise PmtError("alignment_effect_source_mismatch",
                "Completed F1/F3 effects do not bind the assessed before source and current after source", 3)

        # Host confirms the current SourcePin; the client separately reads actual bytes.
        graph_path = context["workspace"].joinpath(*context["graph_rel"].split("/"))
        target = context["workspace"].joinpath(*document_path.split("/"))
        _reject_links(graph_path); _reject_links(target)
        if (not graph_path.is_file() or hashlib.sha256(graph_path.read_bytes()).hexdigest() !=
                graph.get("candidate_sha256") or not target.is_file() or
                hashlib.sha256(target.read_bytes()).hexdigest() != document.get("candidate_sha256")):
            raise PmtError("alignment_physical_readback_mismatch",
                "Local graph/document bytes differ from completed Host effect candidates", 3)
        current_graph = __import__("pmt.efficiency.source", fromlist=["inspect_graph_source"]).inspect_graph_source(
            context["workspace"], graph_path, context["mapping"]["repository_id"], request["scope_id"],
            graph_scope_id=request["scope_id"])
        if current_graph["source_pin"].source_hash != context["pin"].source_hash:
            raise PmtError("alignment_source_changed", "SourcePin changed during alignment readback", 3)

        step_refs = payload.get("step_effect_refs", [])
        if not isinstance(step_refs, list) or len(step_refs) > 100:
            raise PmtError("step_effect_refs_invalid", "Step directive receipts must be a bounded list", 2)
        pointer_selector = {"repository_id": source.get("repository_id"), "branch": source.get("branch"),
            "workspace_ref": source.get("workspace_ref"), "task_id": basis.get("body", {}).get("work", {}).get("task_id"),
            "purpose": "applied_alignment", "environment_id": getattr(self.state_port, "environment_id", None)}
        expected_revision = payload.get("expected_pointer_revision")
        if type(expected_revision) is not int or expected_revision < 0:
            raise PmtError("invalid_pointer_revision", "Applied alignment pointer revision must be nonnegative", 2)
        typed_payload = {"run_id": context["run"]["id"],
            "expected_run_revision": context["run"]["revision"],
            "assessment_ref": assessment["id"], "assessment_hash": assessment["body_hash"],
            "resolution_ref": resolution["id"], "resolution_hash": resolution["body_hash"],
            "basis_ref": basis["id"], "basis_hash": basis["body_hash"],
            "graph_effect_ref": graph_ref, "document_effect_ref": document_ref,
            "step_effect_refs": step_refs, "expected_pointer_revision": expected_revision}
        typed_request = _operation(request, "apply_alignment_receipt", typed_payload,
            suffix="p4-b:apply-alignment-receipt:" + request["request_id"])
        # Current owner/effect/source checks above precede exact original-request
        # recovery. This lookup must precede pointer CAS so ack-loss replay with
        # the original revision returns the already committed Host result.
        cached = self.state_port.get_request_result(typed_request["request_id"],
            request["actor"], request["session_id"], expected_request=typed_request)
        if cached is not None:
            envelope, code = cached
            if code != 0 or not isinstance(envelope, dict) or envelope.get("ok") is not True:
                error = envelope.get("error") if isinstance(envelope, dict) else None
                if isinstance(error, dict):
                    raise PmtError(error.get("code", "alignment_receipt_failed"),
                        error.get("message", "Host alignment receipt failed"), code or 3,
                        error.get("retryable") is True, error.get("details"))
                raise PmtError("alignment_receipt_failed", "Original Host alignment request did not complete", code or 3)
            result = envelope.get("result") or {}
            result["replayed"] = True
        else:
            pointer = self._pointer(request, pointer_selector,
                "applied-alignment-pointer:" + digest(pointer_selector))
            if expected_revision != pointer.get("revision"):
                raise PmtError("revision_conflict", "Applied alignment pointer is stale", 3,
                    details={"expected_revision": expected_revision, "current_revision": pointer.get("revision")})
            result = _call(self.state_port, typed_request, "apply_alignment_receipt")
        # The Host pointer and receipt are durable by this point. Re-read the local
        # targets under current Host authority; if they moved during the network
        # round trip, surface the already-advanced receipt as reconciliation work.
        try:
            post = self._context(request, paths)
            for path in (post["graph_rel"], document_path):
                self._authorize_path(request, post, path)
            post_graph = post["workspace"].joinpath(*post["graph_rel"].split("/"))
            post_document = post["workspace"].joinpath(*document_path.split("/"))
            _reject_links(post_graph); _reject_links(post_document)
            post_ok = (post["pin"].source_hash == context["pin"].source_hash and
                hashlib.sha256(post_graph.read_bytes()).hexdigest() == graph.get("candidate_sha256") and
                hashlib.sha256(post_document.read_bytes()).hexdigest() == document.get("candidate_sha256"))
        except (PmtError, OSError, ValueError):
            post_ok = False
        if not post_ok:
            return {"state": "reconciliation_required", "alignment_ref": result.get("alignment_ref"),
                "receipt_hash": result.get("receipt_hash"), "pointer": result.get("pointer"),
                "effect_ref": result.get("effect_ref"), "applied_pointer_advanced": True,
                "client_post_readback": False, "reason_codes": ["local_source_changed_after_host_receipt"],
                "host_git_verified": False, "host_document_verified": False}
        return {"alignment_ref": result.get("alignment_ref"), "receipt_hash": result.get("receipt_hash"),
            "pointer": result.get("pointer"), "effect_ref": result.get("effect_ref"),
            "source_pin": result.get("source_pin"), "provenance": result.get("provenance"),
            "host_git_verified": False, "host_document_verified": False,
            "client_graph_readback": True, "client_document_readback": True, "client_post_readback": True,
            "applied_pointer_advanced": result.get("applied_pointer_advanced") is True,
            "replayed": result.get("replayed") is True}

    def _read_file_effect(self, request, context, reference, path, expected_kind):
        if (not isinstance(reference, dict) or reference.get("kind") != "host_local_file_effect" or
                not isinstance(reference.get("id"), str) or type(reference.get("revision")) is not int or
                reference.get("scope_id") != request["scope_id"]):
            raise PmtError("alignment_effect_ref_invalid", "A scoped Host file effect ref is required", 2)
        response = self._host(request, "read_local_file_effect", {
            "effect_id": reference["id"], "run_id": context["run"]["id"],
            "expected_run_revision": context["run"]["revision"],
            "repository_id": context["mapping"]["repository_id"], "project_id": request["scope_id"],
            "canonical_workspace": context["canonical_workspace"],
            "relative_graph_path": context["graph_rel"], "target_relative_path": path,
            "branch_key": context["branch_key"], "expected_source": context["pin"].to_dict(),
            "baseline_document_path": path},
            "alignment-effect-read:" + reference["id"] + ":" + digest(path) + ":" + context["pin"].source_hash)
        effect = response.get("effect")
        if (response.get("effect_state") != "completed" or
                response.get("effect_ref", {}).get("revision") != reference["revision"] or
                not isinstance(effect, dict) or effect.get("effect_kind") != expected_kind or
                effect.get("state") != "completed" or effect.get("phase") != "completed" or
                effect.get("run_id") != context["run"]["id"] or
                effect.get("target_relative_path") != path or effect.get("owner", {}).get("session_id") != request["session_id"]):
            raise PmtError("alignment_effect_unverified", "Host did not return the exact completed current-owner effect", 3)
        return effect

    def read_applicability(self, request):
        payload = request.get("payload", {})
        selected_paths = payload.get("paths")
        if selected_paths == ["."]:
            # The F6/P2 snapshot reads the full current workspace. `.` is a
            # dedicated whole-workspace selector here, not a repository path;
            # _context revalidates it against the current run's actual root claim.
            paths = ["."]
        else:
            if isinstance(selected_paths, list) and "." in selected_paths:
                raise PmtError("workspace_scope_required",
                    "Select the whole workspace alone for Hosted F6 applicability", 3)
            paths = local_changes._paths(payload)
        context = self._context(request, paths)
        if "." not in paths:
            raise PmtError("workspace_scope_required", "Hosted P2 snapshot requires a current whole-workspace claim", 3)
        basis = self._basis(request, payload.get("basis_ref"))
        prior = self._find_replay(request, "applicability")
        if prior:
            return {"applicability_ref": prior["id"], "applicability_hash": prior["body_hash"],
                "status": prior["body"].get("status"), "reason_codes": prior["body"].get("reason_codes", []),
                "verification_ref": prior["body"].get("verification_ref"),
                "condition_hash": prior["body"].get("condition_hash"), "replayed": True}
        allowed = {"definition", "target_id", "command", "inputs", "verification_id", "event_id", "reason",
                   "run_id", "paths", "repository_id", "relative_graph_path", "workspace"}
        reuse_payload = {key: payload[key] for key in allowed if key in payload}
        reuse_payload.update({"run_id": context["run"]["id"], "workspace": context["canonical_workspace"],
            "repository_id": context["mapping"]["repository_id"], "relative_graph_path": context["graph_rel"]})
        result = self._host(request, "resolve_reuse", reuse_payload,
                            "applicability-reuse:" + request["request_id"])
        state = "applicable" if result.get("status") == "reusable" else (
            "unknown" if result.get("status") in {"unknown", "active"} else "not_applicable")
        body = {"status": state, "reason_codes": [] if state == "applicable" else
                [result.get("reason", "reuse_not_applicable")],
            "verification_ref": result.get("receipt_ref"), "definition_ref": result.get("decision_ref"),
            "condition_hash": result.get("key_sha256"), "evidence_refs": result.get("evidence_refs", []),
            "basis_ref": basis["id"], "basis_hash": basis["body_hash"],
            "source": {"repository_id": context["mapping"]["repository_id"],
                "branch": context["mapping"]["branch"], "workspace_ref": context["canonical_workspace"],
                "observed_head": context["pin"].reviewed_commit},
            "scope": {"project_id": request["scope_id"], "repository_id": context["mapping"]["repository_id"]},
            "work": {"task_id": basis["body"].get("work", {}).get("task_id")},
            "conditions": {"environment_id": getattr(self.state_port, "environment_id", None)},
            "selector_version": "pmt-reuse-key-v1", "captured_at": utc_now()}
        body["applicability_hash"] = digest(body)
        saved = self._put(request, "applicability", body, basis_hash=basis["body_hash"],
                          suffix="applicability:" + request["request_id"])
        return {"applicability_ref": saved["id"], "applicability_hash": saved["body_hash"],
            "status": state, "reason_codes": body["reason_codes"], "verification_ref": body["verification_ref"],
            "condition_hash": body["condition_hash"]}


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PRIVATE_CHANGE_BYTES = 512 * 1024


def _spool_root(data_root, profile, request):
    session_hash = hashlib.sha256(request["session_id"].encode("utf-8")).hexdigest()
    root = (Path(data_root).expanduser().absolute() / "hosted-continuity-private" /
        profile["namespace_id"] / profile["device_id"] / profile["environment_id"] /
        session_hash / "changes")
    root.mkdir(parents=True, exist_ok=True)
    _reject_links(root)
    return root


def _write_private(root, content_hash, body):
    if not _hex(content_hash) or digest(body) != content_hash:
        raise PmtError("change_detail_hash_invalid", "Private change detail failed its content hash", 5)
    from .util import canonical_json
    raw = (canonical_json(body) + "\n").encode("utf-8")
    if len(raw) > _MAX_PRIVATE_CHANGE_BYTES:
        raise PmtError("change_detail_too_large", "Private change detail exceeds its local bound", 4)
    target = Path(root) / (content_hash + ".json")
    _reject_links(target)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(raw).hexdigest():
            raise PmtError("change_detail_conflict", "A retained local detail ref has different bytes", 3)
        return
    fd, temporary = tempfile.mkstemp(prefix=".change-", suffix=".tmp", dir=str(root))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        _reject_links(target)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(raw).hexdigest():
                raise PmtError("change_detail_conflict", "A concurrent local detail differs", 3)
    except OSError as exc:
        raise PmtError("change_detail_spool_failed", "Private source detail could not be safely retained", 4, True,
            {"exception_type": type(exc).__name__}) from exc
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _read_private(root, content_hash):
    if not _hex(content_hash):
        raise PmtError("change_detail_ref_invalid", "Private change detail ref is invalid", 2)
    path = Path(root) / (content_hash + ".json")
    _reject_links(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PmtError("change_detail_unavailable", "Private local detail is unavailable", 3) from exc
    if len(raw) > _MAX_PRIVATE_CHANGE_BYTES:
        raise PmtError("change_detail_corrupt", "Private local detail exceeds its retained bound", 5)
    from .util import strict_json_loads
    value = strict_json_loads(raw, max_bytes=_MAX_PRIVATE_CHANGE_BYTES)
    if not isinstance(value, dict) or digest(value) != content_hash:
        raise PmtError("change_detail_corrupt", "Private local detail failed its content hash", 5)
    return value


def _hex(value):
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None
