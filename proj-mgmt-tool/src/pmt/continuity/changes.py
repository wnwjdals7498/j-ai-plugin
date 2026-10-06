"""Phase-four source-bound change collection and implementation links."""
from __future__ import annotations

import hashlib
import os
import json
import base64
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any

from ..errors import PmtError
from ..planning.graph import validate_graph
from ..reconciliation import service as reconcile
from ..resources import _reject_links
from ..util import fingerprint, new_id, strict_json_loads, utc_now
from .changes_core import RULE_VERSION, build_link_index, parse_name_status_z, sha256_bytes, normalize_repo_path
from .contracts import authorize, bounded_result, budget, work_access
from .storage import ContinuityStore

READ_OPERATIONS = set()
WRITE_OPERATIONS = {"register_observed_change"}
FILE_OPERATIONS = {"collect_changes", "build_implementation_links", "read_change_slice"}
_GIT_SHA = __import__("re").compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _payload(req):
    value = req.get("payload", {})
    if not isinstance(value, dict):
        raise PmtError("invalid_payload", "payload must be an object")
    return value


def _paths(payload, *, default=()):
    values = payload.get("paths", list(default))
    if not isinstance(values, list) or not values or len(values) > 256:
        raise PmtError("source_paths_required", "An explicit bounded path scope is required")
    result = sorted({normalize_repo_path(value) for value in values}, key=str.casefold)
    return result


def _context(db, req, paths):
    payload = _payload(req)
    with closing(db.connect()) as conn:
        run_id = payload.get("run_id")
        row = conn.execute("SELECT workspace FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise PmtError("run_not_found", "Current run does not exist", 3)
        workspace = Path(row["workspace"]).resolve()
        run = work_access(db, conn, req, paths, str(workspace))
        if run["workspace"] != row["workspace"]:
            raise PmtError("ownership_conflict", "Run workspace changed during authorization", 3)
        scope = conn.execute("SELECT kind FROM scopes WHERE id=?", (req.get("scope_id"),)).fetchone()
        if not scope or scope["kind"] != "project":
            raise PmtError("project_scope_required", "Change operations require explicit project scope")
        if reconcile._step_belongs_to_project(conn, run["step_id"], req["scope_id"]) is None:
            raise PmtError("scope_mismatch", "Current run does not belong to the requested project", 3)
        return workspace, run


def _replay(db, req):
    """Return an exact committed request only after the caller rechecks live authority."""
    ignored = {"request_id", "correlation_id", "received_at", "received_at_utc", "retry_count", "attempt"}
    semantic = {key: value for key, value in req.items() if key not in ignored}
    requested_hash = fingerprint(semantic)
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT request_fingerprint,response_json,exit_code,actor,session_id FROM requests WHERE request_id=?",
                           (req["request_id"],)).fetchone()
    if not row:
        return None
    if row[3] != req.get("actor") or row[4] != req.get("session_id"):
        raise PmtError("request_owner_mismatch", "Request result belongs to another actor or session", 3)
    if row[0] != requested_hash:
        raise PmtError("request_conflict", "Request ID was already used for different input", 3)
    return json.loads(row[1]), row[2]


def _run_git(workspace: Path, *args: str, allow_failure=False):
    root, _ = reconcile._git_context(workspace)
    if root is None:
        return None
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = str(root.parent)
    command = ["git", "-c", f"safe.directory={root}", "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", "-C", str(root), *args]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, shell=False, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PmtError("git_inspection_failed", "Git source could not be inspected", 4, True) from exc
    if result.returncode and not allow_failure:
        raise PmtError("git_inspection_failed", "Git source could not be inspected", 4, True,
                       {"git_exit_code": result.returncode})
    return result


def _verify_source_mapping(db, req, workspace: Path, basis):
    """Bind this run's physical checkout to the configured canonical repo/branch mapping."""
    source = basis["body"].get("source", {})
    project_id = req.get("scope_id")
    repository_id = source.get("repository_id")
    root, _ = reconcile._git_context(workspace)
    if root is None:
        branch, head, source_kind = None, None, "non_git"
    else:
        head = _git_text(_run_git(workspace, "rev-parse", "--verify", "HEAD"), "HEAD").strip()
        branch_result = _run_git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
        branch = _git_text(branch_result, "branch").strip() if not branch_result.returncode else None
        source_kind = "git"
    if not isinstance(repository_id, str) or not repository_id:
        raise PmtError("source_mapping_unknown", "BasisVector lacks a canonical repository identity", 3)
    from ..storage_config import _read_profile, mapping_for_request
    profile, _ = _read_profile(db.config_root)
    if not isinstance(profile, dict):
        raise PmtError("source_mapping_unknown", "No configured canonical workspace mapping exists", 3)
    mapping = mapping_for_request(profile, {"scope_id": project_id, "payload": {
        "repository_id": repository_id, "project_id": project_id, "branch": branch,
        "source_pin": {"repository_id": repository_id, "project_id": project_id,
                       "selected_ref": branch, "reviewed_commit": head, "source_kind": source_kind}}})
    if mapping is None:
        raise PmtError("source_mapping_unknown", "No configured canonical workspace mapping exists", 3)
    mapped_graph = (Path(mapping["local_root"]).resolve() / Path(mapping["relative_graph_path"])).resolve()
    current_graph = (workspace / Path(mapping["relative_graph_path"])).resolve()
    if mapped_graph != current_graph:
        raise PmtError("source_mapping_conflict", "Current run workspace differs from its configured canonical mapping", 3)
    branch_key = branch if branch is not None else ("detached:" + head if head else "non-git")
    expected_workspace_ref = f"pmt://{repository_id}/{hashlib.sha256(branch_key.encode('utf-8')).hexdigest()}"
    if source.get("workspace_ref") != expected_workspace_ref or source.get("branch") != branch:
        raise PmtError("source_basis_conflict", "BasisVector repository, branch, or workspace ref is stale", 3)
    if root is not None and source.get("observed_head") not in {None, head}:
        # A later HEAD is a legitimate observed change; the caller's prior basis remains the baseline.
        # The configured mapping still binds repository and branch, while divergence is assessed below.
        return {"repository_id": repository_id, "branch": branch, "workspace_ref": expected_workspace_ref,
                "head": head, "root": root, "source_kind": source_kind, "head_changed": True,
                "relative_graph_path": mapping["relative_graph_path"]}
    return {"repository_id": repository_id, "branch": branch, "workspace_ref": expected_workspace_ref,
            "head": head, "root": root, "source_kind": source_kind, "head_changed": False,
            "relative_graph_path": mapping["relative_graph_path"]}


def _selection_args(root: Path, workspace: Path, paths: list[str]) -> list[str]:
    project_relative = workspace.resolve().relative_to(root.resolve()).as_posix()
    prefix = "" if project_relative == "." else project_relative.rstrip("/") + "/"
    return [prefix + path for path in paths]


def _verified_mappings(db, req, mappings):
    if not isinstance(mappings, list) or len(mappings) > 500:
        raise PmtError("mapping_invalid", "Mappings must be a bounded array")
    from ..phase2_common import project_scope_id
    vetted = []
    with closing(db.connect()) as conn:
        for value in mappings:
            if not isinstance(value, dict) or set(value) - {
                    "path", "node_id", "decision_ref", "reviewed_by", "verified_mapping_ref"}:
                raise PmtError("mapping_invalid", "Mapping fields are unsupported")
            decision_ref = value.get("decision_ref")
            row = conn.execute("SELECT id,scope_id,state,body_json,revision FROM records WHERE id=? AND kind='decision'",
                               (decision_ref,)).fetchone()
            if not row or row["state"] != "Current" or project_scope_id(conn, row["scope_id"]) != req["scope_id"]:
                continue
            decision_body = strict_json_loads(row["body_json"] or "{}")
            event = conn.execute("SELECT id,actor FROM events WHERE event_type='decision_saved' AND scope_id=? "
                                 "AND json_extract(payload_json,'$.decision_id')=? ORDER BY recorded_at DESC LIMIT 1",
                                 (row["scope_id"], row["id"])).fetchone()
            if not event or not event["actor"]:
                continue
            vetted.append({"path": value.get("path"), "node_id": value.get("node_id"),
                           "decision_ref": row["id"], "verified_mapping_ref": row["id"]})
    return vetted


def _git_text(result, label):
    if result is None:
        return None
    try:
        return result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PmtError("git_output_invalid", f"Git {label} is not valid UTF-8") from exc


def _capture_git(workspace: Path, paths: list[str], base_ref: str | None, pre_read=None):
    root, _ = reconcile._git_context(workspace)
    if root is None:
        return {"kind": "non_git", "head": None, "branch": None, "status": None,
                "changes": [], "files": {}, "coverage": "unknown", "reasons": ["non_git_source"]}
    head_result = _run_git(workspace, "rev-parse", "--verify", "HEAD")
    head = _git_text(head_result, "HEAD").strip()
    if not _GIT_SHA.fullmatch(head):
        raise PmtError("git_head_invalid", "Git returned an invalid HEAD")
    branch_result = _run_git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    branch = _git_text(branch_result, "branch").strip() if branch_result and not branch_result.returncode else None
    selected = _selection_args(root, workspace, paths)
    committed = []
    if base_ref:
        if not _GIT_SHA.fullmatch(base_ref):
            raise PmtError("change_basis_invalid", "before basis must resolve to a verified commit SHA")
        ancestor = _run_git(workspace, "merge-base", "--is-ancestor", base_ref, head, allow_failure=True)
        if ancestor is None or ancestor.returncode != 0:
            return {"kind": "git", "head": head, "branch": branch, "status": None,
                    "changes": [], "files": {}, "coverage": "incomplete", "reasons": ["history_diverged"]}
        result = _run_git(workspace, "diff", "--name-status", "-z", "--find-renames", base_ref, head,
                          "--", *selected)
        committed = parse_name_status_z(result.stdout)
    worktree_result = _run_git(workspace, "diff", "--name-status", "-z", "--find-renames", "HEAD", "--", *selected)
    worktree = parse_name_status_z(worktree_result.stdout)
    status_result = _run_git(workspace, "status", "--porcelain=v1", "-z", "--no-renames",
                             "--untracked-files=all", "--", *selected)
    status = status_result.stdout
    status_paths = []
    chunks = status.split(b"\0")
    for item in chunks:
        if len(item) >= 4:
            try:
                status_paths.append(normalize_repo_path(item[3:].decode("utf-8", errors="strict")))
            except (UnicodeDecodeError, PmtError):
                return {"kind": "git", "head": head, "branch": branch, "status": sha256_bytes(status),
                        "changes": [], "files": {}, "coverage": "incomplete", "reasons": ["path_metadata_invalid"]}
    # Git reports repository-root-relative names even when the mapped PMT
    # workspace is a nested checkout. Normalize both diff and status paths to the
    # same selected workspace-relative namespace before matching inventory refs.
    project_relative = workspace.resolve().relative_to(root.resolve()).as_posix()
    prefix = "" if project_relative == "." else project_relative.rstrip("/") + "/"
    local_status_paths = []
    reasons = []
    for path in status_paths:
        if prefix and not path.startswith(prefix):
            reasons.append("git_path_outside_workspace")
            continue
        local_status_paths.append(path[len(prefix):] if prefix else path)
    changes = {}
    for item in committed + worktree:
        raw_path, raw_before = item["path"], item.get("before_path")
        if prefix and not raw_path.startswith(prefix):
            reasons.append("git_path_outside_workspace")
            continue
        path = raw_path[len(prefix):] if prefix else raw_path
        try:
            path = normalize_repo_path(path)
        except PmtError:
            reasons.append("git_path_metadata_invalid")
            continue
        before_path = None
        if raw_before:
            if prefix and not raw_before.startswith(prefix):
                # A rename crossing the mapped workspace cannot be attributed
                # from this run's claim; keep only an incomplete target fact.
                reasons.append("rename_crosses_workspace")
            else:
                before_path = raw_before[len(prefix):] if prefix else raw_before
                try:
                    before_path = normalize_repo_path(before_path)
                except PmtError:
                    before_path = None
                    reasons.append("rename_path_metadata_invalid")
            if before_path and not any(before_path == selected_path or
                    before_path.startswith(selected_path.rstrip("/") + "/") for selected_path in paths):
                before_path = None
                reasons.append("rename_crosses_selected_scope")
        localized = dict(item) | {"path": path, "before_path": before_path}
        if path not in changes or item.get("kind") == "renamed":
            changes[path] = localized
    for path in local_status_paths:
        changes.setdefault(path, {"kind": "working_tree", "path": path})
    files = {}
    for path in sorted(set(changes) | set(paths), key=str.casefold):
        # Only inspect paths covered by this run's explicit selected scope.
        if not any(path == selected_path or path.startswith(selected_path.rstrip("/") + "/")
                   for selected_path in paths):
            continue
        if pre_read is not None:
            allowed, _reason = pre_read(path)
            if not allowed:
                files[path] = {"sha256": None, "size": None, "content": None,
                               "reason": "path_owner_conflict"}
                continue
        candidate = workspace / Path(path)
        _reject_links(candidate)
        try:
            candidate.resolve().relative_to(workspace.resolve())
        except ValueError as exc:
            raise PmtError("git_path_outside_workspace", "Git path escaped the current workspace", 4) from exc
        if candidate.is_file():
            content = candidate.read_bytes()
            files[path] = {"sha256": sha256_bytes(content), "size": len(content), "content": content}
        elif candidate.exists():
            files[path] = {"sha256": None, "size": None, "content": None, "reason": "not_regular_file"}
        else:
            files[path] = {"sha256": None, "size": None, "content": None, "reason": "deleted_or_missing"}
    # A second independent capture catches HEAD, status and selected bytes changing mid-read.
    head_after = _git_text(_run_git(workspace, "rev-parse", "--verify", "HEAD"), "HEAD").strip()
    status_after = _run_git(workspace, "status", "--porcelain=v1", "-z", "--no-renames",
                             "--untracked-files=all", "--", *selected).stdout
    owner_conflict = any(value.get("reason") == "path_owner_conflict" for value in files.values())
    unstable = head != head_after or status != status_after
    return {"kind": "git", "head": head, "branch": branch, "status": sha256_bytes(status),
            "changes": list(changes.values()), "files": files,
            "coverage": "incomplete" if unstable or owner_conflict or reasons else "complete",
            "reasons": sorted(set(reasons + (["source_changed_during_capture"] if unstable else []) +
                       (["path_owner_conflict"] if owner_conflict else [])))}


def _safe_change_body(req, basis_ref, basis, capture):
    payload = _payload(req)
    path_to_ref = lambda path: hashlib.sha256(path.encode("utf-8")).hexdigest()
    facts = []
    for item in capture["changes"]:
        path = item["path"]
        file = capture["files"].get(path, {})
        before_path = item.get("before_path")
        facts.append({"change_kind": item["kind"], "path_ref": path_to_ref(path),
                      "before_path_ref": path_to_ref(before_path) if before_path else None,
                      "content_hash": file.get("sha256"), "content_size": file.get("size")})
    facts.sort(key=lambda item: (item["path_ref"], item["change_kind"]))
    body = {"origin": "git" if capture["kind"] == "git" else "non_git",
            "scope": {"project_id": req["scope_id"],
                      "repository_id": basis["body"].get("source", {}).get("repository_id")},
            "source": {"repository_id": basis["body"].get("source", {}).get("repository_id"),
                       "branch": capture.get("branch"),
                       "workspace_ref": basis["body"].get("source", {}).get("workspace_ref"),
                       "observed_head": capture.get("head")},
            "work": {"task_id": payload.get("task_id") or req.get("record_id") or
                     basis["body"].get("work", {}).get("task_id")},
            "conditions": {"environment_id": basis["body"].get("conditions", {}).get("environment_id")},
            "state": "incomplete" if capture["coverage"] != "complete" else ("complete" if facts else "no_change"),
            "before_basis_ref": basis_ref, "before_basis_hash": basis.get("body_hash"),
            "observed_head": capture["head"], "branch": capture["branch"],
            "selected_scope_hash": fingerprint(sorted(payload["paths"])),
            "dirty_fingerprint": fingerprint({"status_hash": capture["status"],
                                               "files": [(item["path_ref"], item["content_hash"]) for item in facts]}),
            "facts": facts, "coverage": capture["coverage"], "reason_codes": capture["reasons"],
            "intent_state": "unknown", "intent_evidence_refs": [], "captured_at": utc_now(),
            "rule_version": "p4-change-1"}
    body["change_hash"] = fingerprint(body)
    return body


def _retained_detail(workspace, paths, base_ref, capture):
    """Capture a bounded raw Git patch for the already authorized local owner."""
    if capture["kind"] != "git" or capture["coverage"] != "complete" or not capture["changes"]:
        return {"complete": False, "reason_code": "detail_requires_complete_git_capture", "diff_base64": ""}
    root, _ = reconcile._git_context(workspace)
    selected = _selection_args(root, workspace, paths)
    pieces = []
    if base_ref and base_ref != capture["head"]:
        pieces.append(_run_git(workspace, "diff", "--no-ext-diff", "--binary", "--no-color",
                               base_ref, capture["head"], "--", *selected).stdout)
    pieces.append(_run_git(workspace, "diff", "--no-ext-diff", "--binary", "--no-color",
                           "HEAD", "--", *selected).stdout)
    raw = b"\n".join(piece for piece in pieces if piece)
    limit = 256 * 1024
    complete = len(raw) <= limit
    raw = raw[:limit]
    return {"complete": complete, "reason_code": None if complete else "raw_diff_truncated",
            "diff_sha256": sha256_bytes(raw), "diff_bytes": len(raw),
            "diff_base64": base64.b64encode(raw).decode("ascii"),
            "files": [{"path": item["path"], "before_path": item.get("before_path"),
                       "change_kind": item["kind"],
                       "after_sha256": capture["files"].get(item["path"], {}).get("sha256"),
                       "after_size": capture["files"].get(item["path"], {}).get("size")}
                      for item in capture["changes"]]}


def _source_entries(workspace, paths, pre_read):
    """Enumerate only an explicitly claimed source scope and inspect regular files."""
    import os
    from .changes_core import sha256_bytes
    entries, unknown = [], []
    seen = set()
    for selected in paths:
        root = workspace / Path(selected)
        _reject_links(root)
        if not root.exists():
            unknown.append("selected_path_missing")
            continue
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = []
            for current, directories, files in os.walk(root, topdown=True, followlinks=False):
                base = Path(current)
                kept = []
                for name in directories:
                    child = base / name
                    _reject_links(child)
                    if child.is_symlink():
                        unknown.append("symbolic_path_unavailable")
                    else:
                        kept.append(name)
                directories[:] = kept
                for name in files:
                    candidates.append(base / name)
        else:
            unknown.append("selected_path_not_regular")
            continue
        for candidate in sorted(candidates, key=lambda value: value.as_posix().casefold()):
            _reject_links(candidate)
            if candidate.is_symlink() or not candidate.is_file():
                unknown.append("source_file_not_regular")
                continue
            relative = candidate.relative_to(workspace).as_posix()
            relative = normalize_repo_path(relative)
            if relative in seen:
                continue
            seen.add(relative)
            allowed, _reason = pre_read(relative)
            if not allowed:
                unknown.append("path_owner_conflict")
                continue
            content = candidate.read_bytes()
            entries.append({"path": relative, "content": content, "sha256": sha256_bytes(content)})
            if len(entries) > 2000:
                raise PmtError("source_scope_too_large", "Selected source scope exceeds 2000 files")
    return entries, sorted(set(unknown))


def _finish_effect(db, req, effect_id, state, outcome):
    with db.write() as conn:
        store = ContinuityStore(db)
        return store.update_effect(conn, req, effect_id, state, outcome)


def _file_effect_start(db, req, kind, body, basis_hash):
    with db.write() as conn:
        return ContinuityStore(db).begin_effect(conn, req, kind, body, basis_hash=basis_hash)


def _basis(db, req, basis_ref):
    if not isinstance(basis_ref, str) or not basis_ref:
        raise PmtError("basis_required", "A prior BasisVector ref is required")
    with closing(db.connect()) as conn:
        value = ContinuityStore(db).get(conn, req, basis_ref, kind="basis")
    return value


def execute_file(db, req):
    operation = req.get("operation")
    if operation == "collect_changes":
        return _collect_changes(db, req)
    if operation == "build_implementation_links":
        return _build_implementation_links(db, req)
    if operation == "read_change_slice":
        from ..service import response
        with closing(db.connect()) as conn:
            result = _read_change_slice(db, conn, req)
        return response(req["request_id"], result=result), 0
    raise PmtError("operation_unsupported", "Unsupported source continuity operation")


def _collect_changes(db, req):
    operation = req.get("operation")
    payload = _payload(req)
    paths = _paths(payload)
    workspace, run = _context(db, req, paths)
    basis = _basis(db, req, payload.get("before_basis_ref"))
    after_basis_ref = payload.get("after_basis_ref") or payload.get("current_basis_ref")
    if not isinstance(after_basis_ref, str):
        raise PmtError("current_basis_required", "A fresh after BasisVector ref is required", 3)
    after_basis = _basis(db, req, after_basis_ref)
    mapping = _verify_source_mapping(db, req, workspace, basis)
    after_source = after_basis["body"].get("source", {})
    if (after_source.get("repository_id") != mapping["repository_id"] or
            after_source.get("branch") != mapping["branch"] or
            after_source.get("workspace_ref") != mapping["workspace_ref"] or
            (mapping.get("head") is not None and after_source.get("observed_head") != mapping["head"])):
        raise PmtError("basis_source_conflict", "After BasisVector does not match the current Git head and workspace", 3)
    with closing(db.connect()) as conn:
        inventory = ContinuityStore(db).get(conn, req, after_source.get("inventory_ref"), kind="detail")
    inventory_items = {item.get("relative_path"): item for item in inventory["body"].get("items", [])
                       if isinstance(item, dict)}
    replay = _replay(db, req)
    if replay is not None:
        return replay
    baseline = basis["body"].get("source", {}).get("observed_head")
    if basis["body"].get("scope", {}).get("project_id", req["scope_id"]) != req["scope_id"]:
        raise PmtError("basis_scope_mismatch", "BasisVector belongs to another project", 3)
    selector = {"repository_id": mapping["repository_id"],
                "branch": mapping["branch"],
                "workspace_ref": mapping["workspace_ref"],
                "task_id": payload.get("task_id"), "purpose": "observed_change",
                "environment_id": db.environment_id}
    store = ContinuityStore(db)
    with closing(db.connect()) as conn:
        pointer = store.read_pointer(conn, req, selector)
    expected = payload.get("expected_pointer_revision")
    if type(expected) is not int or expected != pointer["revision"]:
        raise PmtError("revision_conflict", "Observed change pointer revision is stale", 3,
                       details={"expected_revision": expected, "current_revision": pointer["revision"]})
    journal = _file_effect_start(db, req, "collect_changes", {"basis_ref": payload["before_basis_ref"],
        "scope_hash": fingerprint(paths), "operation": operation}, basis["body_hash"])
    try:
        def pre_read(path):
            with closing(db.connect()) as conn:
                return reconcile._read_scope_status(conn, workspace, run["id"], path)
        capture = _capture_git(workspace, paths, baseline, pre_read=pre_read)
        # Recheck current source after content reads. No pointer moves for an incomplete capture.
        if capture["kind"] == "git" and capture["coverage"] == "complete":
            again = _capture_git(workspace, paths, baseline, pre_read=pre_read)
            if (capture["head"], capture["status"], {k: v.get("sha256") for k, v in capture["files"].items()}) != (
                    again["head"], again["status"], {k: v.get("sha256") for k, v in again["files"].items()}):
                capture["coverage"] = "incomplete"
                capture["reasons"] = ["source_changed_during_capture"]
        body = _safe_change_body(req, payload["before_basis_ref"], basis, capture)
        inventory_mismatch = False
        for item in capture["changes"]:
            path = item["path"]
            sha = capture["files"].get(path, {}).get("sha256")
            if item["kind"] == "deleted" and path not in inventory_items and sha is None:
                continue
            found = inventory_items.get(path)
            if not found or found.get("status") != "verified" or found.get("content_hash") != sha:
                inventory_mismatch = True
                break
        if inventory_mismatch or after_source.get("inventory_coverage", {}).get("complete") is not True:
            body["coverage"] = "incomplete"
            body["state"] = "incomplete"
            body["reason_codes"] = sorted(set(body["reason_codes"] + ["after_basis_inventory_mismatch"]))
        body["after_basis_ref"] = after_basis["id"]
        body["after_basis_hash"] = after_basis["body_hash"]
        body["scope"] = {"project_id": req["scope_id"], "repository_id": mapping["repository_id"]}
        body["source"] = {"repository_id": mapping["repository_id"], "branch": mapping["branch"],
            "workspace_ref": mapping["workspace_ref"], "observed_head": capture["head"]}
        body["work"] = {"task_id": payload.get("task_id") or req.get("record_id") or
            after_basis["body"].get("work", {}).get("task_id")}
        body["conditions"] = {"environment_id": db.environment_id}
        detail_body = _retained_detail(workspace, paths, baseline, capture)
        detail_body.update({"before_basis_ref": basis["id"], "facts_hash": fingerprint(body["facts"]),
                            "scope_hash": fingerprint(paths), "origin": body["origin"],
                            "captured_at": body["captured_at"]})
        with db.write() as conn:
            local_detail = store.put(conn, req, "detail", detail_body,
                                     visibility="private", basis_hash=basis["body_hash"])
        body["local_detail_ref"] = local_detail["id"]
        body["selected_path_refs"] = [hashlib.sha256(path.encode()).hexdigest() for path in paths]
        change_hash = fingerprint(body)
        body["change_hash"] = change_hash
        result_holder = {}
        def commit(conn, request):
            authorize(db, conn, request)
            for path in paths:
                work_access(db, conn, request, [path], str(workspace))
            saved = store.put(conn, request, "change", body, visibility="shared", basis_hash=basis["body_hash"])
            result_holder["saved"] = saved
            pointer_result = None
            if body["coverage"] == "complete":
                pointer_result = store.advance_pointer(conn, request, selector, saved["id"], expected)
            effect = store.get_effect(conn, request, journal["id"])
            store.update_effect(conn, request, journal["id"], "completed" if pointer_result is not None else "partial",
                {"object_ref": saved["id"], "change_hash": body["change_hash"], "coverage": body["coverage"],
                 "pointer_revision": pointer_result["revision"] if pointer_result else pointer["revision"]})
            return {"change_ref": saved["id"], "change_hash": body["change_hash"], "state": body["state"],
                    "coverage": body["coverage"], "reason_codes": body["reason_codes"],
                    "observed_head": body["observed_head"], "path_count": len(body["facts"]),
                    "after_basis_ref": body.get("after_basis_ref"),
                    "after_basis_hash": body.get("after_basis_hash"),
                    "local_detail_ref": body.get("local_detail_ref"),
                    "pointer": pointer_result or pointer, "effect_ref": effect["id"],
                    "replayed": saved.get("created_at") is not None}
        envelope, code = db.run_request(req, commit, authorize=lambda conn, request: authorize(db, conn, request))
        if code == 0:
            return envelope, code
        with closing(db.connect()) as conn:
            current = store.get_effect(conn, req, journal["id"])
        if current["state"] != "completed":
            _finish_effect(db, req, journal["id"], "unknown", {"reason_code": "request_commit_failed"})
        return envelope, code
    except Exception:
        try:
            with closing(db.connect()) as conn:
                current = store.get_effect(conn, req, journal["id"])
            if current["state"] != "completed":
                _finish_effect(db, req, journal["id"], "unknown", {"reason_code": "source_capture_failed"})
        except PmtError:
            pass
        raise


def _build_implementation_links(db, req):
    payload = _payload(req)
    paths = _paths(payload)
    workspace, run = _context(db, req, paths)
    basis = _basis(db, req, payload.get("basis_ref"))
    mapping = _verify_source_mapping(db, req, workspace, basis)
    graph_path = mapping["relative_graph_path"]
    access_paths = sorted(set(paths + [graph_path]), key=str.casefold)
    with closing(db.connect()) as conn:
        work_access(db, conn, req, [graph_path], str(workspace))
        for path in access_paths:
            allowed, _reason = reconcile._read_scope_status(conn, workspace, run["id"], path)
            if not allowed:
                raise PmtError("path_owner_conflict", "Another active run owns a selected source path", 3)
    replay = _replay(db, req)
    if replay is not None:
        return replay
    graph_file = workspace / Path(graph_path)
    _reject_links(graph_file)
    if not graph_file.is_file():
        raise PmtError("project_graph_missing", "Current project graph is unavailable", 4)
    graph_raw = graph_file.read_bytes()
    from ..util import strict_json_loads
    graph = strict_json_loads(graph_raw, max_bytes=8 * 1024 * 1024)
    validate_graph(graph, req["scope_id"], complete=False)
    def pre_read(path):
        with closing(db.connect()) as conn:
            return reconcile._read_scope_status(conn, workspace, run["id"], path)
    entries, source_unknown = _source_entries(workspace, paths, pre_read)
    before = {entry["path"]: sha256_bytes(entry["content"]) for entry in entries}
    explicit = _verified_mappings(db, req, payload.get("mappings", []))
    index = build_link_index(source_entries=entries, graph=graph,
        scope_hash=basis["body"].get("source", {}).get("inventory_hash") or basis["body_hash"],
        explicit_mappings=explicit)
    if source_unknown:
        index["coverage"]["complete"] = False
        index["coverage"]["unknown_count"] += len(source_unknown)
        index["coverage"]["reason_codes"] = source_unknown
    after_graph = sha256_bytes(graph_file.read_bytes())
    after = {path: sha256_bytes((workspace / Path(path)).read_bytes()) for path in before
             if (workspace / Path(path)).is_file()}
    if after_graph != sha256_bytes(graph_raw) or before != after:
        index["coverage"]["complete"] = False
        index["coverage"]["reason_codes"] = ["source_changed_during_index"]
        index["index_hash"] = fingerprint({k: v for k, v in index.items() if k != "index_hash"})
    # Revalidate the same claimed paths immediately before saving the source-bound index.
    with closing(db.connect()) as conn:
        for path in access_paths:
            work_access(db, conn, req, [path], str(workspace))
    selector = {"repository_id": mapping["repository_id"],
                "branch": mapping["branch"],
                "workspace_ref": mapping["workspace_ref"],
                "task_id": payload.get("task_id"), "purpose": "implementation_links",
                "environment_id": db.environment_id}
    with closing(db.connect()) as conn:
        pointer = ContinuityStore(db).read_pointer(conn, req, selector)
    expected = payload.get("expected_pointer_revision")
    if type(expected) is not int or expected != pointer["revision"]:
        raise PmtError("revision_conflict", "Implementation index pointer revision is stale", 3)
    body = {"rule_version": RULE_VERSION, "basis_ref": basis["id"], "basis_hash": basis["body_hash"],
            "scope": {"project_id": req["scope_id"],
                      "repository_id": mapping["repository_id"]},
            "source": {"repository_id": mapping["repository_id"], "branch": mapping["branch"],
                       "workspace_ref": mapping["workspace_ref"],
                       "observed_head": _git_text(_run_git(workspace, "rev-parse", "--verify", "HEAD"), "HEAD").strip()
                           if mapping.get("root") is not None else None},
            "work": {"task_id": payload.get("task_id") or req.get("record_id") or
                     basis["body"].get("work", {}).get("task_id")},
            "conditions": {"environment_id": db.environment_id},
            "source_inventory_hash": index["source_inventory_hash"], "graph_hash": sha256_bytes(graph_raw),
            "index_hash": index["index_hash"], "coverage": index["coverage"],
            "entries": [{"path_ref": hashlib.sha256(item["path"].encode()).hexdigest(),
                         "content_hash": item["content_hash"], "status": item["status"],
                         "reason_code": item["reason_code"], "symbols": item["symbols"]}
                        for item in index["entries"]],
            "links": [{"path_ref": hashlib.sha256(item["path"].encode()).hexdigest(),
                       "node_ids": item["node_ids"], "link_state": item["link_state"],
                       "evidence": item["evidence"], "source_hash": item["source_hash"],
                       "decision_ref": item.get("decision_ref")} for item in index["links"]],
            "captured_at": utc_now()}
    if "unmapped_paths" in body["coverage"]:
        # File names remain local; the shared index keeps count and an opaque inventory reference.
        body["coverage"].pop("unmapped_paths", None)
    body["coverage"]["scope_hash"] = fingerprint(paths)
    journal = _file_effect_start(db, req, "build_implementation_links", {"basis_ref": basis["id"],
        "scope_hash": fingerprint(paths), "graph_hash": body["graph_hash"]}, basis["body_hash"])
    store = ContinuityStore(db)
    def commit(conn, request):
        authorize(db, conn, request)
        saved = store.put(conn, request, "link_index", body, basis_hash=basis["body_hash"])
        pointer_result = None
        if body["coverage"].get("complete") is True:
            pointer_result = store.advance_pointer(conn, request, selector, saved["id"], expected)
        store.update_effect(conn, request, journal["id"],
            "completed" if pointer_result else "partial",
            {"object_ref": saved["id"], "index_hash": body["index_hash"],
             "coverage": body["coverage"], "pointer_revision": pointer_result["revision"] if pointer_result else pointer["revision"]})
        return {"index_ref": saved["id"], "index_hash": body["index_hash"],
                "coverage": body["coverage"], "pointer": pointer_result or pointer,
                "effect_ref": journal["id"]}
    return db.run_request(req, commit, authorize=lambda conn, request: authorize(db, conn, request))


def handle(db, conn, req):
    operation = req.get("operation")
    payload = _payload(req)
    store = ContinuityStore(db)
    if operation == "register_observed_change":
        ref = payload.get("change_ref")
        change = store.get(conn, req, ref, kind="change")
        if change["owner_session"] != req["session_id"]:
            raise PmtError("ownership_conflict", "Observed change was captured by another session", 3)
        if payload.get("claim_complete") is True or payload.get("approved") is True:
            # A caller assertion never changes the source-bound receipt or intent level.
            raise PmtError("untrusted_change_claim", "Caller flags cannot upgrade observed change authority", 3)
        return {"change_ref": change["id"], "change_hash": change["body_hash"],
                "state": change["body"].get("state"), "coverage": change["body"].get("coverage"),
                "registered": True, "intent_state": change["body"].get("intent_state", "unknown")}
    raise PmtError("operation_unsupported", "Unsupported change operation")


def _read_change_slice(db, conn, req):
        payload = _payload(req)
        store = ContinuityStore(db)
        change_ref = payload.get("change_ref")
        change = store.get(conn, req, change_ref, kind="change")
        body = change["body"]
        result = {"change_ref": change["id"], "change_hash": change["body_hash"],
                  "state": body["state"], "origin": body["origin"], "coverage": body["coverage"],
                  "facts": body["facts"], "reason_codes": body["reason_codes"],
                  "before_basis_ref": body["before_basis_ref"], "intent_state": body["intent_state"],
                  "after_basis_ref": body.get("after_basis_ref"),
                  "after_basis_hash": body.get("after_basis_hash"),
                  "intent_evidence_refs": body["intent_evidence_refs"],
                  "local_detail_ref": body.get("local_detail_ref")}
        if payload.get("include_detail") is True:
            detail_ref = body.get("local_detail_ref")
            if not isinstance(detail_ref, str):
                result["detail"] = {"available": False, "reason_code": "detail_unavailable"}
            else:
                paths = _paths(payload)
                workspace, _run = _context(db, req, paths)
                detail = store.get(conn, req, detail_ref, kind="detail")
                if detail["body"].get("facts_hash") != fingerprint(body["facts"]):
                    raise PmtError("change_detail_mismatch", "Private detail is not bound to this change receipt", 3)
                local_files = detail["body"].get("files", [])
                if not all(isinstance(item, dict) and isinstance(item.get("path"), str) and
                           any(item["path"] == selected or item["path"].startswith(selected.rstrip("/") + "/")
                               for selected in paths) for item in local_files):
                    raise PmtError("change_detail_scope_mismatch", "Current claim does not cover every detailed path", 3)
                with closing(db.connect()) as authority_conn:
                    for path in local_files:
                        work_access(db, authority_conn, req, [path["path"]], str(workspace))
                        allowed, _reason = reconcile._read_scope_status(authority_conn, workspace,
                                                                         _payload(req)["run_id"], path["path"])
                        if not allowed:
                            raise PmtError("path_owner_conflict", "Another active run owns a detailed path", 3)
                payload_offset = payload.get("detail_offset", 0)
                file_offset = payload.get("file_offset", 0)
                if type(payload_offset) is not int or payload_offset < 0 or type(file_offset) is not int or file_offset < 0:
                    raise PmtError("change_detail_cursor_invalid", "Detail offsets must be nonnegative integers")
                diff = detail["body"].get("diff_base64", "")
                if payload_offset > len(diff) or payload_offset % 4:
                    raise PmtError("change_detail_cursor_invalid", "Diff offset is outside the retained detail")
                byte_budget = budget(payload)["max_bytes"]
                chunk_size = max(4, min(8192, (byte_budget // 2) // 4 * 4))
                diff_chunk = diff[payload_offset:payload_offset + chunk_size]
                page_size = 20
                page = local_files[file_offset:file_offset + page_size]
                next_diff = payload_offset + len(diff_chunk)
                next_file = file_offset + len(page)
                result["detail"] = {"available": True, "complete": detail["body"].get("complete"),
                    "reason_code": detail["body"].get("reason_code"), "diff_sha256": detail["body"].get("diff_sha256"),
                    "diff_bytes": detail["body"].get("diff_bytes"), "diff_offset": payload_offset,
                    "diff_base64": diff_chunk, "next_diff_offset": next_diff if next_diff < len(diff) else None,
                    "file_offset": file_offset, "files": page,
                    "next_file_offset": next_file if next_file < len(local_files) else None,
                    "file_count": len(local_files)}
        return bounded_result(req, result)
