"""Client-local Phase 4 source capture with Host-only metadata persistence.

Git and selected workspace files are inspected in this process. The Host sees
the registered run/source references and client-attested hashes, never the
workspace path, inventory paths, or source file contents.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import uuid

from .continuity.contracts import digest, bounded_result
from .errors import PmtError
from .service import response
from .util import canonical_json, new_id

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_INVENTORY_FILES = 512
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_INVENTORY_BYTES = 64 * 1024 * 1024
_MAX_PRIVATE_DETAIL_BYTES = 2 * 1024 * 1024


def _remote_result(store, request):
    envelope, code = store.execute(request)
    if code or not isinstance(envelope, dict) or envelope.get("ok") is not True:
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            raise PmtError(error.get("code", "host_request_failed"),
                error.get("message", "Host request failed"), code or 3,
                error.get("retryable") is True, error.get("details"))
        raise PmtError("host_response_invalid", "Host returned an invalid operation response", 3)
    return envelope.get("result")


def _request(req, operation, payload):
    return {"protocol_version": 1, "request_id": new_id(), "operation": operation,
        "actor": req["actor"], "session_id": req["session_id"],
        "scope_id": req["scope_id"], "source": {"product": "pmt-client"},
        "payload": payload}


def _relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("-"):
        raise PmtError("invalid_inventory_path", "Inventory entries must be relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PmtError("invalid_inventory_path", "Inventory entries must remain inside the mapped checkout")
    return path.as_posix()


def _inventory(workspace: Path, workspace_ref: str, paths: list[str]):
    from .resources import _reject_links

    rows = []
    total_bytes = 0
    for relative in paths:
        target = workspace.joinpath(*PurePosixPath(relative).parts)
        path_ref = digest({"workspace_ref": workspace_ref, "relative_path": relative})
        try:
            _reject_links(workspace)
            _reject_links(target)
            target.resolve(strict=True).relative_to(workspace)
            if not target.is_file() or target.stat().st_size > _MAX_FILE_BYTES:
                raise OSError("inventory_source_unavailable")
            raw = target.read_bytes()
            total_bytes += len(raw)
            if total_bytes > _MAX_INVENTORY_BYTES:
                raise OSError("inventory_too_large")
            rows.append({"relative_path": relative, "path_ref": path_ref,
                         "content_hash": hashlib.sha256(raw).hexdigest(),
                         "status": "verified", "reason_code": None})
        except (OSError, ValueError):
            rows.append({"relative_path": relative, "path_ref": path_ref, "content_hash": None,
                         "status": "unknown", "reason_code": "inventory_source_unavailable"})
    verified = sum(row["status"] == "verified" for row in rows)
    reasons = sorted({row["reason_code"] for row in rows if row["reason_code"]})
    return rows, {"selected_count": len(rows), "verified_count": verified,
                  "unknown_count": len(rows) - verified, "reason_codes": reasons}, digest(rows)


def _write_private_inventory_detail(data_root, profile, request, workspace_ref, rows,
                                    coverage, inventory_hash):
    """Retain relative paths and hashes locally; no inventory detail goes to Host."""
    from .resources import _reject_links

    session_hash = hashlib.sha256(request["session_id"].encode("utf-8")).hexdigest()
    directory = (Path(data_root).expanduser().absolute() / "hosted-continuity-private" /
        profile["namespace_id"] / profile["device_id"] / profile["environment_id"] / session_hash)
    directory.mkdir(parents=True, exist_ok=True)
    _reject_links(directory)
    payload = request.get("payload", {})
    task_selection = payload.get("task_ref")
    task_ref = payload.get("task_id") or (
        task_selection.get("task_id") if isinstance(task_selection, dict) else None) or request.get("record_id")
    body = {"schema": "pmt-client-inventory-v1", "scope_id": request["scope_id"],
        "workspace_ref": workspace_ref, "task_ref": task_ref,
        "capture_ref": digest({"scope_id": request["scope_id"], "workspace_ref": workspace_ref,
                               "task_ref": task_ref, "inventory_hash": inventory_hash}),
        "inventory_hash": inventory_hash,
        "coverage": coverage, "items": rows}
    raw = (canonical_json(body) + "\n").encode("utf-8")
    path = directory / (inventory_hash + ".json")
    _reject_links(path)
    if path.exists():
        if path.read_bytes() != raw:
            raise PmtError("basis_detail_conflict", "A local inventory reference is bound to different detail", 3)
    else:
        fd, temporary = tempfile.mkstemp(prefix=".inventory-", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise PmtError("basis_detail_conflict", "A local inventory reference is bound to different detail", 3)
        except OSError as exc:
            raise PmtError("basis_detail_spool_failed", "Private client inventory detail could not be retained", 4, True) from exc
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return {"client_detail_ref": "client-spool:sha256:" + inventory_hash,
            "client_detail_sha256": hashlib.sha256(raw).hexdigest(),
            "client_detail_bytes": len(raw)}


def read_private_inventory_detail(data_root, profile, session_id, inventory_ref):
    """Read and verify this Host-authenticated session's private inventory spool."""
    from .resources import _reject_links

    prefix = "client-inventory:sha256:"
    if not isinstance(inventory_ref, str) or not inventory_ref.startswith(prefix):
        raise PmtError("basis_detail_ref_invalid", "Client inventory detail reference is unsupported", 2)
    inventory_hash = inventory_ref[len(prefix):]
    if not _HEX64.fullmatch(inventory_hash):
        raise PmtError("basis_detail_ref_invalid", "Client inventory detail reference is invalid", 2)
    session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    directory = (Path(data_root).expanduser().absolute() / "hosted-continuity-private" /
        profile["namespace_id"] / profile["device_id"] / profile["environment_id"] / session_hash)
    path = directory / (inventory_hash + ".json")
    _reject_links(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise PmtError("basis_detail_unavailable", "Private client inventory detail was not retained", 3) from exc
    except OSError as exc:
        raise PmtError("basis_detail_unavailable", "Private client inventory detail could not be read", 4, True) from exc
    if len(raw) > _MAX_PRIVATE_DETAIL_BYTES:
        raise PmtError("basis_detail_corrupt", "Private client inventory detail exceeds its bound", 5)
    from .util import strict_json_loads
    body = strict_json_loads(raw, max_bytes=_MAX_PRIVATE_DETAIL_BYTES)
    if (not isinstance(body, dict) or body.get("schema") != "pmt-client-inventory-v1"
            or body.get("inventory_hash") != inventory_hash
            or not isinstance(body.get("items"), list)
            or digest(body["items"]) != inventory_hash):
        raise PmtError("basis_detail_corrupt", "Private client inventory detail failed its hash check", 5)
    return body


class HostedContinuityClient:
    """Composite FILE adapter: local source read, authenticated Host metadata write."""

    def __init__(self, profile, state_port, data_root, environ=None):
        self.profile = profile
        self.state_port = state_port
        self.data_root = Path(data_root).expanduser().absolute()
        self.environ = os.environ if environ is None else environ

    def execute(self, request):
        if request.get("operation") != "capture_work_basis":
            raise PmtError("hosted_local_runtime_unavailable",
                "Hosted client capture adapter does not support this continuity file operation", 3)
        return self.capture_work_basis(request)

    def capture_work_basis(self, request):
        from .efficiency.source import inspect_graph_source, pin_source, verify_source_pin
        from .storage_config import adapt_host_request, mapping_for_request
        from .workspace import canonical_workspace

        payload = request.get("payload", {})
        required = {"run_id", "repository_id", "workspace", "relative_graph_path"}
        if not required <= set(payload):
            raise PmtError("basis_source_required", "Hosted basis capture needs a mapped active run and graph path", 2)
        mapped = mapping_for_request(self.profile, request)
        if mapped is None:
            raise PmtError("storage_mapping_missing", "Hosted basis capture needs one explicit workspace mapping", 3)
        adapt_host_request(self.profile, request)  # validates the supplied local path against the selected mapping
        if payload["repository_id"] != mapped["repository_id"] or request.get("scope_id") != mapped["project_id"]:
            raise PmtError("storage_mapping_conflict", "Hosted basis scope differs from its workspace mapping", 3)
        workspace = Path(mapped["local_root"]).expanduser().resolve(strict=True)
        relative_graph = _relative_path(payload["relative_graph_path"])
        branch_key = mapped.get("branch")
        if not isinstance(branch_key, str) or not branch_key:
            raise PmtError("storage_mapping_missing", "Hosted basis capture needs an explicit mapped branch", 3)
        canonical_ref = mapped["canonical_workspace"]
        if canonical_workspace(mapped["repository_id"], branch_key) != canonical_ref:
            raise PmtError("storage_mapping_stale", "Workspace mapping does not match its canonical branch", 3)

        facts_result = _remote_result(self.state_port, _request(request, "read_current_facts", {}))
        facts = facts_result.get("facts") if isinstance(facts_result, dict) else None
        active = next((item for item in (facts or {}).get("active_execution", [])
                       if item.get("run_ref") == payload["run_id"]), None)
        if not isinstance(active, dict):
            raise PmtError("ownership_conflict", "Selected Host run is not active in the current project", 3)
        expected_run_revision = payload.get("expected_run_revision", active.get("revision"))
        if type(expected_run_revision) is not int or expected_run_revision != active.get("revision"):
            raise PmtError("execution_revision_conflict", "Selected Host run revision changed", 3)

        preflight_payload = {"project_id": mapped["project_id"], "repository_id": mapped["repository_id"],
            "canonical_workspace": canonical_ref, "relative_graph_path": relative_graph,
            "run_id": payload["run_id"], "expected_run_revision": expected_run_revision,
            "branch_key": branch_key, "mode": "source_capture"}
        _remote_result(self.state_port, _request(request, "authorize_workspace", preflight_payload))

        workspace_ref = canonical_ref
        try:
            graph_path = workspace.joinpath(*PurePosixPath(relative_graph).parts)
            client_before = inspect_graph_source(workspace, graph_path, mapped["repository_id"],
                mapped["project_id"], graph_scope_id=mapped["project_id"])
        except (OSError, ValueError) as exc:
            raise PmtError("basis_source_unavailable", "Mapped graph source could not be inspected", 3) from exc
        pin = client_before["source_pin"]
        pin_branch_key = (pin.selected_ref if pin.selected_ref is not None else
            "detached:" + pin.reviewed_commit if pin.source_kind == "git" else "non-git")
        if canonical_workspace(pin.repository_id, pin_branch_key) != canonical_ref:
            raise PmtError("storage_mapping_stale", "Current checkout branch differs from its explicit Host mapping", 3)

        selected = payload.get("inventory_paths") or [relative_graph]
        if not isinstance(selected, list) or not 1 <= len(selected) <= _MAX_INVENTORY_FILES:
            raise PmtError("basis_inventory_invalid", "Select between 1 and 512 workspace-relative files", 2)
        selected = list(dict.fromkeys(_relative_path(item) for item in selected))
        if relative_graph not in selected:
            selected.append(relative_graph)
        inventory_before, coverage, inventory_hash_before = _inventory(workspace, workspace_ref, selected)

        # Recheck local source and inventory after the first read. A mixed
        # client basis is rejected before any metadata is published.
        client_after = inspect_graph_source(workspace, graph_path, mapped["repository_id"],
            mapped["project_id"], graph_scope_id=mapped["project_id"])
        inventory_after, after_coverage, inventory_hash_after = _inventory(workspace, workspace_ref, selected)
        if (pin.source_hash != client_after["source_pin"].source_hash
                or inventory_hash_before != inventory_hash_after):
            raise PmtError("basis_source_changed", "Client source changed during work-basis capture", 3)
        coverage["unknown_count"] = max(coverage["unknown_count"], after_coverage["unknown_count"])
        coverage["verified_count"] = min(coverage["verified_count"], after_coverage["verified_count"])
        coverage["reason_codes"] = sorted(set(coverage["reason_codes"] + after_coverage["reason_codes"]))
        private_detail = _write_private_inventory_detail(self.data_root, self.profile, request,
            workspace_ref, inventory_before, coverage, inventory_hash_before)

        source_query = _request(request, "read_source_metadata", {
            "project_id": mapped["project_id"], "repository_id": mapped["repository_id"],
            "canonical_workspace": canonical_ref, "relative_graph_path": relative_graph})
        source_snapshot = None
        source_revision = 0
        try:
            source_snapshot = _remote_result(self.state_port, source_query)
            source_revision = source_snapshot["snapshot_ref"]["revision"]
        except PmtError as exc:
            if exc.code not in {"source_snapshot_unavailable", "host_source_unavailable"}:
                raise
        if source_snapshot is None or source_snapshot.get("source_pin", {}).get("source_hash") != pin.source_hash:
            graph_ref = self.state_port.publish_resource({"request_id": new_id(),
                "scope_id": mapped["project_id"], "purpose": "graph_snapshot"},
                client_before["wire"], session_id=request["session_id"])["artifact_ref"]
            publish = _request(request, "publish_source_snapshot", {
                "project_id": mapped["project_id"], "repository_id": mapped["repository_id"],
                "canonical_workspace": canonical_ref, "relative_graph_path": relative_graph,
                "run_id": payload["run_id"], "expected_run_revision": expected_run_revision,
                "expected_source_revision": source_revision, "branch_key": pin_branch_key,
                "source_pin": pin.to_dict(), "graph_resource_ref": graph_ref})
            _remote_result(self.state_port, publish)
            source_snapshot = _remote_result(self.state_port, source_query)
            source_revision = source_snapshot["snapshot_ref"]["revision"]
        verify_source_pin(pin, source_snapshot["source_pin"])

        request_payload = {"project_id": mapped["project_id"], "repository_id": mapped["repository_id"],
            "canonical_workspace": canonical_ref, "relative_graph_path": relative_graph,
            "branch_key": pin_branch_key, "run_id": payload["run_id"],
            "expected_run_revision": expected_run_revision, "expected_source_revision": source_revision,
            "task_id": payload.get("task_id") or request.get("record_id"),
            "source_pin_before": pin.to_dict(), "source_pin_after": client_after["source_pin"].to_dict(),
            "inventory_hash_before": inventory_hash_before, "inventory_hash_after": inventory_hash_after,
            "inventory_coverage": coverage}
        published = _remote_result(self.state_port,
            _request(request, "publish_work_basis", request_payload))
        result = {**published, "source_pin": pin.to_dict(), "inventory_coverage": coverage,
            "source_provenance": "client_attested", "host_git_verified": False,
            "client_detail_available": True, **private_detail,
            "source_snapshot_ref": source_snapshot["snapshot_ref"]}
        return response(request["request_id"], result=bounded_result(request, result)), 0
