"""P3 verification recording, current-candidate lookup, and finish validation."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .errors import PmtError
from .util import canonical_json, fingerprint, new_id, utc_now

OUTCOMES = {"pass", "fail", "blocked", "aborted"}
SKIP_DIRS = {".git", ".pmt", ".pmt-test", ".pytest-tmp", ".pytest_cache", "__pycache__",
             ".venv", "venv", "node_modules", "dist", "build"}
MANIFEST_NAMES = {"pyproject.toml", "requirements.txt", "requirements.lock", "poetry.lock",
                  "Pipfile", "Pipfile.lock", "uv.lock", "package.json", "package-lock.json",
                  "pnpm-lock.yaml", "yarn.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum"}


def _object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PmtError("stored_record_invalid", f"Stored {label} is invalid JSON", 5) from exc
    if not isinstance(value, dict):
        raise PmtError("stored_record_invalid", f"Stored {label} must be an object", 5)
    return value


def _array(value: Any, label: str) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PmtError("stored_record_invalid", f"Stored {label} is invalid JSON", 5) from exc
    if not isinstance(value, list):
        raise PmtError("stored_record_invalid", f"Stored {label} must be an array", 5)
    return value


def _record_and_scope(conn, target_id: str):
    record = conn.execute("SELECT * FROM records WHERE id=?", (target_id,)).fetchone()
    if record is not None:
        scope = conn.execute("SELECT * FROM scopes WHERE id=?", (record["scope_id"],)).fetchone()
        body = _object(record["body_json"], "record body")
        return record, scope, body
    scope = conn.execute("SELECT * FROM scopes WHERE id=?", (target_id,)).fetchone()
    if scope is not None:
        return None, scope, _object(scope["body_json"], "scope body")
    raise PmtError("verification_target_not_found", "target_id does not identify a record or scope")


def _verification_scope_id(conn, scope):
    """Keep candidates within the record's directly registered verification scope."""
    return scope["id"]


def _criteria(body: dict[str, Any]) -> dict[str, str]:
    values = body.get("criteria", [])
    if not isinstance(values, list):
        raise PmtError("invalid_criteria", "record body.criteria must be an array")
    result = {}
    for value in values:
        criterion_id = value if isinstance(value, str) else value.get("id") if isinstance(value, dict) else None
        if not isinstance(criterion_id, str) or not criterion_id:
            raise PmtError("invalid_criteria", "criterion entries must be IDs or objects with an id")
        if criterion_id in result:
            raise PmtError("invalid_criteria", "criterion IDs must be unique")
        # P2 canonicalizes an id-only object to its string form, while richer
        # criterion objects retain their full text as part of the criterion hash.
        canonical_value = criterion_id if isinstance(value, dict) and set(value) == {"id"} else value
        result[criterion_id] = fingerprint(canonical_value)
    return result


def _resolve_workspace(db, conn, record, scope, body) -> tuple[Path | None, list[str]]:
    reasons = []
    configured = body.get("workspace")
    base = None
    # The closest scope-path mapping for this profile provides the repository root.
    current = scope
    while current is not None and base is None:
        mapped = conn.execute("SELECT path FROM scope_paths WHERE scope_id=? AND environment_id=?",
                              (current["id"], db.environment_id)).fetchone()
        if mapped:
            base = Path(mapped["path"]).expanduser()
        current = conn.execute("SELECT * FROM scopes WHERE id=?", (current["parent_id"],)).fetchone() if current["parent_id"] else None
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            if base is None:
                reasons.append("workspace_relative_without_scope_path")
                return None, reasons
            candidate = base / candidate
    else:
        candidate = base
    if candidate is None:
        reasons.append("workspace_not_registered")
        return None, reasons
    try:
        candidate = candidate.resolve(strict=True)
    except OSError:
        reasons.append("workspace_missing")
        return None, reasons
    if not candidate.is_dir():
        reasons.append("workspace_not_directory")
        return None, reasons
    return candidate, reasons


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _workspace_files(db, root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    files: list[dict[str, Any]] = []
    reasons: list[str] = []
    excluded_roots = [db.root.resolve(), db.config_root.resolve()]
    try:
        def on_walk_error(_error):
            reasons.append("workspace_enumeration_failed")
        for current, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=on_walk_error):
            current_path = Path(current)
            kept = []
            for name in sorted(dirs):
                path = current_path / name
                if name.casefold() in SKIP_DIRS:
                    continue
                if _is_link(path):
                    reasons.append("workspace_contains_symlink")
                    continue
                resolved = path.resolve()
                if any(_within(resolved, excluded) for excluded in excluded_roots):
                    continue
                kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                path = current_path / name
                try:
                    if _is_link(path):
                        reasons.append("workspace_contains_symlink")
                        continue
                    if name == ".git":
                        continue
                    resolved = path.resolve(strict=True)
                    if any(_within(resolved, excluded) for excluded in excluded_roots):
                        continue
                    before = resolved.stat()
                    digest = hashlib.sha256()
                    size = 0
                    with resolved.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                    after = resolved.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        reasons.append("workspace_changed_during_snapshot")
                    relative = path.relative_to(root).as_posix()
                    files.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
                except OSError:
                    reasons.append("workspace_file_unreadable")
    except OSError:
        reasons.append("workspace_enumeration_failed")
    return files, sorted(set(reasons))


def _manifest_hashes(db) -> tuple[list[dict[str, str]], list[str]]:
    project = Path(__file__).resolve().parents[2]
    records, errors = [], []
    if not project.is_dir():
        return [], ["project_root_unavailable"]
    try:
        for current, dirs, names in os.walk(project, followlinks=False):
            current_path = Path(current)
            safe_dirs = []
            for name in dirs:
                path = current_path / name
                if name.casefold() in SKIP_DIRS:
                    continue
                if _is_link(path):
                    errors.append("dependency_contains_symlink")
                    continue
                safe_dirs.append(name)
            dirs[:] = sorted(safe_dirs)
            for name in sorted(names):
                if name not in MANIFEST_NAMES:
                    continue
                path = current_path / name
                if _is_link(path):
                    errors.append("dependency_contains_symlink")
                    continue
                try:
                    records.append({"path": path.relative_to(project).as_posix(),
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                except OSError:
                    errors.append("dependency_manifest_unreadable")
    except OSError:
        errors.append("dependency_manifest_enumeration_failed")
    return records, errors


def _configuration_hashes(db) -> tuple[list[dict[str, str]], list[str]]:
    records, errors = [], []
    try:
        if db.config_root.exists():
            for current, dirs, names in os.walk(db.config_root, followlinks=False):
                current_path = Path(current)
                safe_dirs = []
                for name in dirs:
                    if _is_link(current_path / name):
                        errors.append("configuration_contains_symlink")
                    else:
                        safe_dirs.append(name)
                dirs[:] = sorted(safe_dirs)
                for name in sorted(names):
                    path = current_path / name
                    if _is_link(path):
                        errors.append("configuration_contains_symlink")
                        continue
                    try:
                        records.append({"path": path.relative_to(db.config_root).as_posix(),
                                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                    except OSError:
                        errors.append("configuration_file_unreadable")
    except OSError:
        errors.append("configuration_enumeration_failed")
    return records, errors


def _runtime_manifest() -> dict[str, Any]:
    packages = sorted((dist.metadata.get("Name") or "").casefold() + "==" + dist.version
                      for dist in importlib.metadata.distributions())
    return {"os": sys.platform, "architecture": platform.machine(), "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version, "packages": packages}


def _snapshot(db, conn, target_id: str, definition_id: str, definition_version: str,
              command: Any, inputs: Any = None, inputs_fingerprint: str | None = None) -> tuple[dict[str, Any], str, list[str], dict[str, str], str]:
    record, scope, body = _record_and_scope(conn, target_id)
    criterion_hashes = _criteria(body) if record and record["kind"] in {"work", "item", "step"} else {}
    trusted_provider = getattr(db, "verification_snapshot_provider", None)
    if trusted_provider is not None:
        host_inputs_fingerprint = (inputs_fingerprint if inputs_fingerprint is not None else
                                   fingerprint(inputs) if inputs is not None else None)
        return trusted_provider.snapshot(conn, target_id, definition_id, definition_version, command,
                                         host_inputs_fingerprint, record, scope, body, criterion_hashes)
    workspace, reasons = _resolve_workspace(db, conn, record, scope, body)
    workspace_files = []
    if workspace is not None:
        workspace_files, file_reasons = _workspace_files(db, workspace)
        reasons.extend(file_reasons)
    manifests, manifest_reasons = _manifest_hashes(db)
    config_hashes, config_reasons = _configuration_hashes(db)
    reasons.extend(manifest_reasons)
    reasons.extend(config_reasons)
    snapshot = {
        "fingerprint_version": 1,
        "definition_id": definition_id,
        "definition_version": definition_version,
        "verification_scope_id": _verification_scope_id(conn, scope),
        # Do not retain the absolute workspace path. A one-way identity still
        # prevents reuse across distinct worktrees with identical file contents.
        "workspace_identity_sha256": (hashlib.sha256(os.path.normcase(str(workspace)).encode("utf-8")).hexdigest()
                                       if workspace is not None else None),
        "environment_id": db.environment_id,
        "command": command,
        "inputs_sha256": inputs_fingerprint if inputs_fingerprint is not None else (fingerprint(inputs) if inputs is not None else None),
        "criteria": criterion_hashes,
        "workspace_files": workspace_files,
        "workspace_known": workspace is not None and not any(r.startswith("workspace_") for r in reasons),
        "runtime": _runtime_manifest(),
        "dependency_manifests": manifests,
        "configuration_hashes": config_hashes,
    }
    reasons = sorted(set(reasons))
    digest = fingerprint(snapshot)
    return snapshot, digest, reasons, criterion_hashes, scope["id"]


def _resource_status(db, conn, artifact_id: str, scope_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT id,scope_id,sha256,state FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row is None:
        return {"valid": False, "reason": "evidence_not_found", "sha256": None}
    if row["state"] != "ready":
        return {"valid": False, "reason": "evidence_not_ready", "sha256": row["sha256"]}
    if row["scope_id"] != scope_id:
        return {"valid": False, "reason": "evidence_scope_mismatch", "sha256": row["sha256"]}
    try:
        # P4 owns path/hash checks. Missing helper must never turn evidence into a pass.
        from .resources import check_artifact
    except ImportError:
        return {"valid": False, "reason": "resource_checker_unavailable", "sha256": row["sha256"]}
    try:
        checked = check_artifact(db, conn, artifact_id)
    except Exception:
        return {"valid": False, "reason": "evidence_check_failed", "sha256": row["sha256"]}
    valid = bool(checked.get("valid")) and checked.get("sha256") == row["sha256"]
    return {"valid": valid, "reason": checked.get("reason") if not valid else None,
            "sha256": checked.get("sha256")}


def _validate_evidence(db, conn, evidence_ids: list[str], scope_id: str) -> tuple[list[str], list[str], dict[str, str]]:
    valid_ids, reasons, hashes = [], [], {}
    for artifact_id in evidence_ids:
        result = _resource_status(db, conn, artifact_id, scope_id)
        if result["valid"]:
            valid_ids.append(artifact_id)
            hashes[artifact_id] = result["sha256"]
        else:
            reasons.append(f"{artifact_id}:{result['reason']}")
    return valid_ids, reasons, hashes


def _load_verification(row) -> dict[str, Any]:
    command = _object(row["command_json"], "verification command")
    return {"verification_id": row["id"], "definition_id": row["definition_id"],
            "definition_version": row["definition_version"], "target_id": row["target_id"],
            "environment_id": row["environment_id"], "input_fingerprint": row["input_fingerprint"],
            "outcome": row["outcome"], "exit_code": row["exit_code"],
            "evidence_ids": _array(row["evidence_json"], "evidence IDs"),
            "criterion_coverage": _array(row["includes_json"], "criterion coverage"),
            "command": command.get("command"), "snapshot": command.get("snapshot"),
            "snapshot_hash": command.get("snapshot_hash"),
            "provided_input_fingerprint_sha256": command.get("provided_input_fingerprint_sha256"),
            "state": row["state"], "completed_at": row["completed_at"]}


def _stored_scope_id(conn, row, item):
    snapshot = item.get("snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("verification_scope_id"), str):
        return snapshot["verification_scope_id"]
    # Older schema-2 rows fingerprinted a target Item. Recover their namespace for
    # stale-failure checks; their old fingerprint still will not match new snapshots.
    try:
        _, scope, _ = _record_and_scope(conn, row["target_id"])
        return _verification_scope_id(conn, scope)
    except PmtError:
        return None


def _failure_after(conn, definition_id: str, definition_version: str, scope_id: str,
                   after_rowid: int) -> bool:
    # SQLite row insertion order is authoritative for post-success failures. Wall
    # clock timestamps may repeat or move backwards, so they cannot invalidate reuse.
    rows = conn.execute("SELECT rowid AS verification_rowid,* FROM verifications "
                        "WHERE definition_id=? AND definition_version=? AND rowid>? "
                        "AND (outcome!='pass' OR state!='valid') ORDER BY rowid",
                        (definition_id, definition_version, after_rowid)).fetchall()
    for row in rows:
        item = _load_verification(row)
        if _stored_scope_id(conn, row, item) == scope_id:
            return True
    return False


def _requested_criteria(body: dict[str, Any], requested: Any, criterion_hashes: dict[str, str]) -> list[str]:
    if requested is None:
        return list(criterion_hashes)
    if not isinstance(requested, list) or any(not isinstance(value, str) for value in requested):
        raise PmtError("invalid_criteria", "criterion_ids must be an array of IDs")
    if len(requested) != len(set(requested)) or any(value not in criterion_hashes for value in requested):
        raise PmtError("invalid_criteria", "criterion_ids must be unique current target criteria")
    return requested


def _lookup(db, conn, payload, request):
    definition_id = payload.get("definition_id")
    definition_version = str(payload.get("definition_version", "1"))
    target_id = payload.get("target_id") or request.get("record_id")
    command = payload.get("command")
    if not all(isinstance(value, str) and value for value in (definition_id, target_id)):
        raise PmtError("verification_fields_required", "definition_id and target_id or record_id are required")
    if not ((isinstance(command, str) and command.strip()) or
            (isinstance(command, list) and command and all(isinstance(part, str) and part for part in command))):
        raise PmtError("verification_command_required", "command must be a nonempty string or argument list")
    inputs = payload.get("inputs", payload.get("input"))
    snapshot, digest, reasons, criterion_hashes, scope_id = _snapshot(db, conn, target_id, definition_id,
                                                                      definition_version, command, inputs)
    if reasons:
        return {"reusable": False, "status": "unknown", "verification_id": None,
                "input_fingerprint": digest, "reasons": reasons}
    _, _, body = _record_and_scope(conn, target_id)
    required = _requested_criteria(body, payload.get("criterion_ids"), criterion_hashes)
    verification_scope_id = snapshot["verification_scope_id"]
    rows = conn.execute("SELECT rowid AS verification_rowid,* FROM verifications "
                        "WHERE definition_id=? AND definition_version=? AND environment_id=? "
                        "ORDER BY rowid DESC",
                        (definition_id, definition_version, db.environment_id)).fetchall()
    rows = [row for row in rows if _stored_scope_id(conn, row, _load_verification(row)) == verification_scope_id]
    matching_passes = [row for row in rows if row["outcome"] == "pass" and row["state"] == "valid" and row["input_fingerprint"] == digest]
    for row in matching_passes:
        item = _load_verification(row)
        if _object(row["command_json"], "verification command").get("before_fingerprint") != row["input_fingerprint"]:
            continue
        coverage = {value.get("id"): value.get("sha256") for value in item["criterion_coverage"] if isinstance(value, dict)}
        if any(coverage.get(criterion_id) != criterion_hashes[criterion_id] for criterion_id in required):
            continue
        if _failure_after(conn, definition_id, definition_version, verification_scope_id, row["verification_rowid"]):
            return {"reusable": False, "status": "stale", "verification_id": item["verification_id"],
                    "input_fingerprint": digest, "reasons": ["later_nonpass_verification"]}
        valid_ids, evidence_reasons, hashes = _validate_evidence(db, conn, item["evidence_ids"], scope_id)
        if evidence_reasons or not valid_ids:
            return {"reusable": False, "status": "stale", "verification_id": item["verification_id"],
                    "input_fingerprint": digest, "reasons": evidence_reasons or ["no_ready_evidence"]}
        return {"reusable": True, "status": "valid", "verification_id": item["verification_id"],
                "definition_id": definition_id, "definition_version": definition_version,
                "target_id": target_id, "input_fingerprint": digest,
                "criterion_ids": required, "evidence_ids": valid_ids, "evidence_hashes": hashes,
                "reasons": []}
    same_definition = rows[0] if rows else None
    reasons = ["no_matching_success"]
    if same_definition and same_definition["input_fingerprint"] != digest:
        reasons = ["current_fingerprint_mismatch"]
    return {"reusable": False, "status": "stale" if same_definition else "not_found",
            "verification_id": same_definition["id"] if same_definition else None,
            "input_fingerprint": digest, "reasons": reasons}


def handle(db, conn, request) -> dict[str, Any]:
    """Handle record_verification and lookup_verification."""
    operation = request.get("operation")
    payload = request.get("payload") or {}
    if operation == "lookup_verification":
        return _lookup(db, conn, payload, request)
    if operation != "record_verification":
        raise PmtError("unsupported_verification_operation", "verification handler only supports record_verification and lookup_verification")
    definition_id = payload.get("definition_id")
    definition_version = payload.get("definition_version", "1")
    target_id = payload.get("target_id") or request.get("record_id")
    command = payload.get("command")
    outcome = payload.get("outcome")
    if not isinstance(definition_id, str) or not definition_id or not isinstance(target_id, str) or not target_id:
        raise PmtError("verification_fields_required", "definition_id and target_id or record_id are required")
    if not isinstance(definition_version, str) or not definition_version:
        raise PmtError("invalid_definition_version", "definition_version must be non-empty text")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise PmtError("invalid_verification_outcome", "outcome must be pass, fail, blocked, or aborted")
    if not ((isinstance(command, str) and command.strip()) or
            (isinstance(command, list) and command and all(isinstance(part, str) and part for part in command))):
        raise PmtError("verification_command_required", "command must be a nonempty string or argument list")
    exit_code = payload.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise PmtError("invalid_exit_code", "exit_code must be an integer or null")
    if outcome == "pass" and exit_code != 0:
        raise PmtError("verification_exit_code_mismatch", "pass requires exit_code 0")
    evidence_ids = payload.get("evidence_ids", [])
    if not isinstance(evidence_ids, list) or any(not isinstance(value, str) or not value for value in evidence_ids):
        raise PmtError("invalid_evidence_ids", "evidence_ids must be an array of artifact IDs")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise PmtError("invalid_evidence_ids", "evidence_ids must be unique")
    inputs = payload.get("inputs", payload.get("input"))
    snapshot, digest, reasons, criterion_hashes, scope_id = _snapshot(db, conn, target_id, definition_id,
                                                                      definition_version, command, inputs)
    _, _, body = _record_and_scope(conn, target_id)
    criterion_ids = _requested_criteria(body, payload.get("criterion_ids"), criterion_hashes)
    if outcome == "pass":
        if reasons:
            raise PmtError("verification_fingerprint_unknown", "pass cannot be recorded with an incomplete current snapshot", 2, False, {"reasons": reasons})
        if not evidence_ids:
            raise PmtError("verification_evidence_required", "pass requires ready evidence artifacts")
        if criterion_hashes and not criterion_ids:
            raise PmtError("verification_criteria_incomplete", "pass must cover at least one current criterion")
    valid_evidence, evidence_reasons, evidence_hashes = _validate_evidence(db, conn, evidence_ids, scope_id)
    if outcome == "pass" and evidence_reasons:
        raise PmtError("verification_evidence_invalid", "pass evidence must be ready, present, hash-valid, and in target scope", 2, False,
                       {"reasons": evidence_reasons})
    before_fingerprint = payload.get("before_fingerprint")
    if before_fingerprint is not None and (not isinstance(before_fingerprint, str) or len(before_fingerprint) != 64
                                           or any(char not in "0123456789abcdef" for char in before_fingerprint)):
        raise PmtError("invalid_before_fingerprint", "before_fingerprint must be a SHA-256 digest returned before execution")
    if outcome == "pass" and before_fingerprint is None:
        raise PmtError("verification_before_snapshot_required", "Capture lookup_verification input_fingerprint before running the check")
    if before_fingerprint is not None and before_fingerprint != digest:
        reasons.append("verification_changed_during_execution")
        if outcome == "pass":
            raise PmtError("verification_changed_during_execution", "Verification inputs changed between the before and after snapshots")
    coverage = [{"id": criterion_id, "sha256": criterion_hashes[criterion_id]} for criterion_id in criterion_ids]
    verification_id = new_id()
    completed = utc_now()
    state = "valid" if not reasons and (outcome != "pass" or not evidence_reasons) else "unknown"
    provided_fingerprint = payload.get("input_fingerprint")
    if provided_fingerprint is not None and (not isinstance(provided_fingerprint, str) or len(provided_fingerprint) > 256):
        raise PmtError("invalid_input_fingerprint", "input_fingerprint must be text when supplied")
    command_data = {"command": command, "inputs_sha256": snapshot["inputs_sha256"], "snapshot": snapshot,
                    "snapshot_hash": digest, "snapshot_reasons": reasons,
                    "before_fingerprint": before_fingerprint,
                    "provided_input_fingerprint_sha256": fingerprint(provided_fingerprint) if provided_fingerprint is not None else None}
    conn.execute("INSERT INTO verifications(id,definition_id,definition_version,target_id,environment_id,input_fingerprint,outcome,exit_code,evidence_json,includes_json,command_json,started_at,completed_at,state) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (verification_id, definition_id, definition_version, target_id, db.environment_id, digest,
                  outcome, exit_code, canonical_json(valid_evidence), canonical_json(coverage),
                  canonical_json(command_data), payload.get("started_at"), completed, state))
    return {"verification_id": verification_id, "definition_id": definition_id,
            "definition_version": definition_version, "target_id": target_id,
            "input_fingerprint": digest, "outcome": outcome, "state": state,
            "criterion_ids": criterion_ids, "evidence_ids": valid_evidence,
            "evidence_hashes": evidence_hashes, "reasons": reasons + evidence_reasons,
            "provided_input_fingerprint_used_as_authority": False}


def verify_completion(db, conn, record_dict_or_Row, verification_ids) -> dict[str, Any]:
    """Validate current successful evidence and criterion-union coverage for P2."""
    if not isinstance(verification_ids, (list, tuple)):
        return {"valid": False, "covered_criteria": [], "evidence_ids": [], "reasons": ["verification_ids_invalid"]}
    record = dict(record_dict_or_Row)
    target_id = record.get("id") or record.get("record_id")
    if not target_id:
        return {"valid": False, "covered_criteria": [], "evidence_ids": [], "reasons": ["record_id_missing"]}
    scope_id = record.get("scope_id")
    body = _object(record.get("body_json", record.get("body", {})), "record body")
    current_scope_row = conn.execute("SELECT * FROM scopes WHERE id=?", (scope_id,)).fetchone() if scope_id else None
    if current_scope_row is None:
        return {"valid": False, "covered_criteria": [], "evidence_ids": [], "reasons": ["record_scope_missing"]}
    verification_scope_id = _verification_scope_id(conn, current_scope_row)
    try:
        criterion_hashes = _criteria(body) if record.get("kind") in {"work", "item", "step"} else {}
    except PmtError as exc:
        return {"valid": False, "covered_criteria": [], "evidence_ids": [], "reasons": [exc.code]}
    covered: dict[str, str] = {}
    evidence_ids, reasons = [], []
    if not verification_ids:
        reasons.append("verification_ids_required")
    for verification_id in verification_ids:
        row = conn.execute("SELECT rowid AS verification_rowid,* FROM verifications WHERE id=?", (verification_id,)).fetchone()
        if row is None:
            reasons.append(f"verification_not_found:{verification_id}")
            continue
        item = _load_verification(row)
        if item["outcome"] != "pass" or item["state"] != "valid":
            reasons.append(f"verification_not_successful:{verification_id}")
            continue
        stored_command = _object(row["command_json"], "verification command")
        if stored_command.get("before_fingerprint") != row["input_fingerprint"]:
            reasons.append(f"verification_before_snapshot_missing:{verification_id}")
            continue
        if row["environment_id"] != db.environment_id:
            reasons.append(f"verification_environment_mismatch:{verification_id}")
            continue
        if _stored_scope_id(conn, row, item) != verification_scope_id:
            reasons.append(f"verification_scope_mismatch:{verification_id}")
            continue
        if _failure_after(conn, item["definition_id"], item["definition_version"],
                          verification_scope_id, row["verification_rowid"]):
            reasons.append(f"later_nonpass_verification:{verification_id}")
            continue
        try:
            snapshot, digest, snapshot_reasons, current_criteria, current_scope = _snapshot(
                db, conn, target_id, item["definition_id"], item["definition_version"],
                item["command"], inputs_fingerprint=item.get("snapshot", {}).get("inputs_sha256"))
        except PmtError as exc:
            reasons.append(f"current_snapshot_failed:{exc.code}")
            continue
        if (snapshot_reasons or digest != item["input_fingerprint"]
                or item.get("snapshot_hash") != item["input_fingerprint"]
                or snapshot.get("verification_scope_id") != verification_scope_id):
            reasons.append(f"verification_stale:{verification_id}")
            continue
        valid_ids, evidence_reasons, _ = _validate_evidence(db, conn, item["evidence_ids"], current_scope)
        if evidence_reasons or not valid_ids:
            reasons.extend(f"verification_evidence_invalid:{verification_id}:{reason}" for reason in (evidence_reasons or ["no_ready_evidence"]))
            continue
        for value in item["criterion_coverage"]:
            if isinstance(value, dict) and value.get("id") in current_criteria and value.get("sha256") == current_criteria[value["id"]]:
                covered[value["id"]] = value["sha256"]
        evidence_ids.extend(valid_ids)
    missing = sorted(set(criterion_hashes) - set(covered))
    if missing:
        reasons.append("criteria_not_covered:" + ",".join(missing))
    return {"valid": not reasons, "covered_criteria": sorted(covered),
            "evidence_ids": sorted(set(evidence_ids)), "reasons": reasons}
