"""Policy-bounded tool result resource storage and byte-accurate detail reads."""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from ..errors import PmtError
from ..service import response
from ..util import canonical_json
from .storage import Phase3Storage

READ_OPERATIONS = {"read_tool_result_detail"}
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"compact_tool_result"}

_MAX_INLINE = 900 * 1024  # Leave room below protocol-v1's 1 MiB envelope limit.
_MAX_DETAIL = 64 * 1024
_REDACTIONS = (
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|credential)\b\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?im)(\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*\s*=\s*)([^\r\n]+)"),
    re.compile(r"(?im)(\b(?:HOME|PATH|PWD|USER|USERNAME|TEMP|TMP|SYSTEMROOT|HOSTNAME|HTTP_PROXY|HTTPS_PROXY|NO_PROXY)\s*=\s*)([^\r\n]+)"),
    re.compile(r"(?is)(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END [A-Z ]*PRIVATE KEY-----)"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})\b"),
)
_SENSITIVE_KEYS = re.compile(r"(?i)(secret|token|authorization|password|credential|environment|conversation|transcript|prompt|api.?key|private.?key)")
_ENV_ASSIGNMENT = re.compile(r"(?m)^([A-Z][A-Z0-9_]{1,63}\s*=\s*)[^\r\n]*$")
_TRANSCRIPT_LINE = re.compile(r"(?im)^(system|developer|assistant|user|tool)(?:\[[^\]\r\n]+\])?:\s*[^\r\n]*$")
_COMPACT_FIELDS = {"task_id", "run_id", "step_id", "status", "exit_code", "format", "output",
                   "source_artifact_id", "criteria_claims", "result_id", "scope_id"}
_DETAIL_FIELDS = {"result_id", "scope_id", "cursor", "max_bytes", "max_lines"}


def _payload(req):
    payload = req.get("payload", {})
    if not isinstance(payload, dict):
        raise PmtError("input_invalid", "payload must be an object")
    return payload


def _required_text(value, field, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PmtError("input_invalid", f"{field} must be nonempty text")
    return value


def _redact(text):
    count = 0
    for pattern in _REDACTIONS:
        if pattern.groups:
            text, replaced = pattern.subn(lambda match: match.group(1) + "[REDACTED]", text)
        else:
            text, replaced = pattern.subn("[REDACTED]", text)
        count += replaced
    if len(list(_ENV_ASSIGNMENT.finditer(text))) >= 3:
        text, replaced = _ENV_ASSIGNMENT.subn(lambda match: match.group(1) + "[REDACTED_ENV]", text)
        count += replaced
    transcript_lines = list(_TRANSCRIPT_LINE.finditer(text))
    if len(transcript_lines) >= 2:
        text, replaced = _TRANSCRIPT_LINE.subn(lambda match: match.group(1) + ": [REDACTED_CONVERSATION]", text)
        count += replaced
    return text, count


def _redact_json(value):
    """Recursively replace sensitive JSON fields without retaining their values."""
    count = 0
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            if _SENSITIVE_KEYS.search(key):
                result[key] = "[REDACTED]"
                count += 1
            else:
                result[key], nested = _redact_json(child)
                count += nested
        return result, count
    if isinstance(value, list):
        result = []
        for child in value:
            item, nested = _redact_json(child)
            result.append(item)
            count += nested
        return result, count
    if isinstance(value, str):
        value, count = _redact(value)
    return value, count


def _validate_binding(db, conn, *, scope_id, actor, session, task_id, run_id, step_id):
    from ..phase2_common import project_scope_id, validate_scope
    from ..resources import check_artifact

    validate_scope(None, conn, scope_id)
    step = conn.execute("SELECT id,scope_id,parent_id,kind FROM records WHERE id=?", (step_id,)).fetchone()
    run = conn.execute("SELECT r.id,r.step_id,r.owner_session,r.state,r.result_json,j.step_id AS job_step "
                       "FROM execution_runs r JOIN execution_jobs j ON j.id=r.job_id WHERE r.id=?", (run_id,)).fetchone()
    task = conn.execute("SELECT id,scope_id,kind FROM records WHERE id=?", (task_id,)).fetchone()
    if (not step or step["kind"] != "step" or not run or run["step_id"] != step_id
            or run["job_step"] != step_id or run["owner_session"] != session or not task):
        raise PmtError("result_binding_invalid", "Result must match an existing Step and its owned execution run", 3)
    if run["state"] in {"queued", "starting"}:
        raise PmtError("result_run_not_started", "A tool result cannot be attached before the run starts", 3)
    if project_scope_id(conn, step["scope_id"]) != scope_id or project_scope_id(conn, task["scope_id"]) != scope_id:
        raise PmtError("result_scope_mismatch", "Run, Step, task, and requested scope do not match", 3)
    if task_id != step_id:
        current = step
        seen = set()
        belongs = False
        while current and current["id"] not in seen:
            seen.add(current["id"])
            if current["id"] == task_id:
                belongs = True
                break
            parent = current["parent_id"]
            current = conn.execute("SELECT id,parent_id,kind FROM records WHERE id=?", (parent,)).fetchone() if parent else None
        if not belongs:
            raise PmtError("result_binding_invalid", "task_id must be the Step or one of its ancestor records", 3)
    receipt_observation = None
    receipt_candidate_invalid = False
    if run["result_json"]:
        try:
            persisted = json.loads(run["result_json"])
        except (ValueError, TypeError):
            persisted = None
        observation = persisted.get("runner_observation") if isinstance(persisted, dict) else None
        receipt_ref = persisted.get("receipt_ref") if isinstance(persisted, dict) else None
        if isinstance(observation, dict) and type(observation.get("exit_code")) is int:
            receipt_candidate_invalid = True
            try:
                canonical = isinstance(receipt_ref, str) and str(uuid.UUID(receipt_ref)) == receipt_ref
            except (ValueError, TypeError, AttributeError):
                canonical = False
            receipt_row = conn.execute("SELECT scope_id,relative_path,size_bytes FROM artifacts WHERE id=?",
                                       (receipt_ref,)).fetchone() if canonical else None
            artifact_check = check_artifact(db, conn, receipt_ref) if receipt_row else {"valid": False}
            if (receipt_row and isinstance(receipt_row["scope_id"], str)
                    and project_scope_id(conn, receipt_row["scope_id"]) == scope_id
                    and artifact_check.get("valid") and receipt_row["size_bytes"] <= 8 * 1024 * 1024):
                try:
                    from ..resources import _artifact_path
                    output_receipt = json.loads(_artifact_path(db, receipt_row["relative_path"]).read_bytes().decode("utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    output_receipt = None
                runner_receipt = output_receipt.get("receipt") if isinstance(output_receipt, dict) else None
                if (isinstance(output_receipt, dict) and output_receipt.get("run_id") == run_id
                        and isinstance(runner_receipt, dict)
                        and type(runner_receipt.get("exit_code")) is int
                        and runner_receipt["exit_code"] == observation["exit_code"]):
                    receipt_observation = {"exit_code": observation["exit_code"], "receipt_ref": receipt_ref}
                    receipt_candidate_invalid = False
    return {"state": run["state"], "step_id": step_id, "task_id": task_id,
            "scope_id": scope_id, "runner_receipt": receipt_observation,
            "runner_receipt_invalid": receipt_candidate_invalid}


def _normalize_observation(reported_status, reported_exit, runner_receipt, receipt_invalid=False):
    """Keep producer claims separate from receipt-backed or internally consistent status."""
    source = "runner_receipt" if runner_receipt else ("unverified_runner_receipt" if receipt_invalid else "producer_observation")
    if receipt_invalid:
        return {"status": "unknown", "exit_code": None, "source": source,
                "reason": "runner_receipt_missing_or_invalid"}
    actual_exit = runner_receipt["exit_code"] if runner_receipt else reported_exit
    if runner_receipt and reported_exit is not None and reported_exit != actual_exit:
        return {"status": "unknown", "exit_code": actual_exit, "source": source,
                "reason": "producer_exit_conflicts_with_runner_receipt"}
    if runner_receipt:
        normalized = "succeeded" if actual_exit == 0 else "failed"
        if reported_status in {"succeeded", "failed"} and reported_status != normalized:
            return {"status": "unknown", "exit_code": actual_exit, "source": source,
                    "reason": "producer_status_conflicts_with_runner_receipt"}
        return {"status": normalized if reported_status in {"succeeded", "failed"} else reported_status,
                "exit_code": actual_exit, "source": source, "reason": None}
    if reported_status == "succeeded" and reported_exit != 0:
        reason = "success_claim_has_nonzero_or_unknown_exit"
        return {"status": "unknown", "exit_code": reported_exit, "source": source, "reason": reason}
    if reported_status == "failed" and (reported_exit is None or reported_exit == 0):
        reason = "failure_claim_has_zero_or_unknown_exit"
        return {"status": "unknown", "exit_code": reported_exit, "source": source, "reason": reason}
    return {"status": reported_status, "exit_code": reported_exit, "source": source, "reason": None}


def _read_input(db, p, scope_id, run_id, step_id):
    """Accept bounded inline text or an already registered resource in this scope."""
    if "source_artifact_id" in p:
        from ..resources import _artifact_path, check_artifact

        artifact_id = _required_text(p.get("source_artifact_id"), "source_artifact_id")
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT scope_id,size_bytes,relative_path,sha256 FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            check = check_artifact(db, conn, artifact_id)
            owner_ref = conn.execute("SELECT 1 FROM artifact_refs WHERE artifact_id=? AND owner_id IN (?,?) LIMIT 1",
                                     (artifact_id, run_id, step_id)).fetchone()
        if not row or row["scope_id"] != scope_id or not check["valid"] or not owner_ref:
            raise PmtError("source_artifact_unavailable", "Source artifact is unavailable in this scope", 3)
        if row["size_bytes"] > 32 * 1024 * 1024:
            raise PmtError("result_too_large", "Source artifact exceeds the 32 MiB processing limit")
        raw = _artifact_path(db, row["relative_path"]).read_bytes()
        if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise PmtError("source_artifact_corrupt", "Source artifact changed while being read", 4)
    else:
        output = p.get("output", "")
        if isinstance(output, str):
            raw = output.encode("utf-8")
        elif isinstance(output, bytes):
            raw = output
        else:
            raise PmtError("input_invalid", "output must be text or an existing resource reference")
        if len(raw) > _MAX_INLINE:
            raise PmtError("result_too_large", "Inline output exceeds the safe protocol-v1 input limit")
    return raw


def _response(req, result=None, error=None):
    return response(req.get("request_id"), result=result, error=error)


def _register_managed_resource(db, req, *, scope_id, result_id, request_id, stored):
    from ..resources import _artifact_path, _reject_links, check_artifact, execute as resource_execute

    staging = db.root / "resources" / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    _reject_links(staging)
    source = staging / ("tool-result-" + str(uuid.uuid5(uuid.UUID(request_id), "staging")) + ".txt")
    try:
        from ..resources import _hash
        if source.exists():
            _reject_links(source)
            if _hash(source) != (hashlib.sha256(stored).hexdigest(), len(stored)):
                raise PmtError("request_conflict", "Staged result differs from the original request", 3)
        else:
            try:
                fd = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if _hash(source) != (hashlib.sha256(stored).hexdigest(), len(stored)):
                    raise PmtError("request_conflict", "Staged result differs from the original request", 3)
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(stored)
                    stream.flush()
                    os.fsync(stream.fileno())
        resource_request_id = str(uuid.uuid5(uuid.UUID(request_id), "tool-result-resource"))
        registration = {
            "protocol_version": 1, "operation": "register_resource",
            "request_id": resource_request_id, "actor": req["actor"],
            "session_id": req["session_id"], "scope_id": scope_id,
            "payload": {"source_path": str(source), "allowed_root": str(staging), "retention": "evidence"},
        }
        envelope, code = resource_execute(db, registration)
        if code != 0 or not envelope.get("ok"):
            issue = envelope.get("error") or {}
            raise PmtError(issue.get("code", "result_resource_failed"),
                           "Tool output resource publication failed", code or 4,
                           bool(issue.get("retryable", False)))
        receipt = envelope["result"]
        if receipt["size_bytes"] != len(stored) or receipt["sha256"] != hashlib.sha256(stored).hexdigest():
            raise PmtError("result_hash_mismatch", "Published resource does not match sanitized output", 4)
        with closing(db.connect()) as conn:
            check = check_artifact(db, conn, receipt["artifact_id"])
            row = conn.execute("SELECT relative_path FROM artifacts WHERE id=? AND scope_id=?",
                               (receipt["artifact_id"], scope_id)).fetchone()
        if not check["valid"] or not row:
            raise PmtError("result_resource_invalid", "Published output resource failed integrity verification", 4)
        # A second file read after publication ensures the reference points at the saved bytes.
        published = _artifact_path(db, row["relative_path"]).read_bytes()
        if hashlib.sha256(published).hexdigest() != receipt["sha256"]:
            raise PmtError("result_hash_mismatch", "Published output resource changed after registration", 4)
        with db.write() as conn:
            conn.execute("INSERT OR IGNORE INTO artifact_refs(artifact_id,owner_type,owner_id,purpose,created_at) VALUES(?,?,?,?,?)",
                         (receipt["artifact_id"], "phase3_tool_result", result_id, "evidence", __import__("pmt.util", fromlist=["utc_now"]).utc_now()))
        return receipt
    except Exception:
        # Keep deterministic staging content so the same request can resume safely.
        raise


def _staging_file(db, request_id):
    rid = str(uuid.uuid5(uuid.UUID(request_id), "staging"))
    return db.root / "resources" / ".staging" / ("tool-result-" + rid + ".txt")


def _verify_resource(db, scope_id, resource_id, expected_hash):
    from ..resources import _artifact_path, check_artifact

    with closing(db.connect()) as conn:
        row = conn.execute("SELECT scope_id,relative_path,size_bytes,sha256 FROM artifacts WHERE id=?",
                           (resource_id,)).fetchone()
        check = check_artifact(db, conn, resource_id)
    if not row or row["scope_id"] != scope_id or not check["valid"] or row["sha256"] != expected_hash:
        raise PmtError("result_hash_mismatch", "Published output resource is missing or invalid", 4)
    raw = _artifact_path(db, row["relative_path"]).read_bytes()
    if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise PmtError("result_hash_mismatch", "Published output resource failed replay integrity verification", 4)


def execute_file(db, req):
    """Redact, publish as a managed resource, then persist only its manifest."""
    p = _payload(req)
    try:
        unknown = set(p) - _COMPACT_FIELDS
        if unknown:
            raise PmtError("unknown_fields", "Unsupported result fields", details={"fields": sorted(unknown)})
        scope_id = _required_text(req.get("scope_id") or p.get("scope_id"), "scope_id")
        actor = _required_text(req.get("actor"), "actor")
        session = _required_text(req.get("session_id"), "session_id")
        task_id = _required_text(p.get("task_id"), "task_id")
        run_id = _required_text(p.get("run_id"), "run_id")
        step_id = _required_text(p.get("step_id"), "step_id")
        request_id = _required_text(req.get("request_id"), "request_id")
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PmtError("invalid_request_id", "request_id must be a canonical UUID")
        with closing(db.connect()) as conn:
            binding = _validate_binding(db, conn, scope_id=scope_id, actor=actor, session=session,
                                        task_id=task_id, run_id=run_id, step_id=step_id)
        status = p.get("status")
        if not isinstance(status, str) or status not in {"succeeded", "failed", "partial", "unknown", "cancelled"}:
            raise PmtError("input_invalid", "status must describe the observed tool outcome")
        exit_code = p.get("exit_code")
        if exit_code is not None and type(exit_code) is not int:
            raise PmtError("input_invalid", "exit_code must be an integer or unknown")
        observation = _normalize_observation(status, exit_code, binding.get("runner_receipt"),
                                             binding.get("runner_receipt_invalid", False))
        fmt = p.get("format", "text")
        if not isinstance(fmt, str) or fmt not in {"text", "json", "binary"}:
            raise PmtError("format_unsupported", "Only text, json, and binary result formats are recognized")
        criteria = p.get("criteria_claims", [])
        if not isinstance(criteria, list) or len(criteria) > 100:
            raise PmtError("input_invalid", "criteria_claims must be a bounded array")
        if "source_artifact_id" not in p and "output" not in p:
            raise PmtError("input_invalid", "output or source_artifact_id is required")
        result_id = p.get("result_id") or str(uuid.uuid5(uuid.NAMESPACE_URL, "pmt-tool-result:" + request_id))
        try:
            if str(uuid.UUID(result_id)) != result_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PmtError("input_invalid", "result_id must be a canonical UUID")

        if fmt == "binary":
            return _response(req, {"result_id": result_id, "reported_status": status,
                                   "reported_exit_code": exit_code, "status": observation["status"],
                                   "exit_code": observation["exit_code"], "status_source": observation["source"],
                                   "status_reason": observation["reason"],
                                   "format": fmt, "capability": "unsupported", "preservation": "not_preserved",
                                   "criteria_verdict": {"status": "not_evaluated", "source": "tool_output_not_authoritative"}}), 0
        if "source_artifact_id" in p and "output" in p:
            raise PmtError("input_invalid", "Provide output or source_artifact_id, not both")
        source = _read_input(db, p, scope_id, run_id, step_id)
        try:
            text = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return _response(req, {"result_id": result_id, "reported_status": status,
                                   "reported_exit_code": exit_code, "status": observation["status"],
                                   "exit_code": observation["exit_code"], "status_source": observation["source"],
                                   "status_reason": observation["reason"],
                                   "format": fmt, "capability": "unsupported_encoding", "preservation": "not_preserved",
                                   "source_sha256": hashlib.sha256(source).hexdigest(),
                                   "criteria_verdict": {"status": "not_evaluated", "source": "tool_output_not_authoritative"}}), 0
        if fmt == "json":
            try:
                decoded_json = json.loads(text)
            except (ValueError, json.JSONDecodeError):
                return _response(req, {"result_id": result_id,
                                       "reported_status": status, "reported_exit_code": exit_code,
                                       "status": observation["status"], "exit_code": observation["exit_code"],
                                       "status_source": observation["source"], "status_reason": observation["reason"],
                                       "format": fmt, "capability": "unsupported_json",
                                       "preservation": "not_preserved",
                                       "source_sha256": hashlib.sha256(source).hexdigest(),
                                       "source_size_bytes": len(source),
                                       "criteria_verdict": {"status": "not_evaluated", "source": "tool_output_not_authoritative"}}), 0
            safe_json, redactions = _redact_json(decoded_json)
            stored = canonical_json(safe_json).encode("utf-8")
        else:
            sanitized_text, redactions = _redact(text)
            stored = sanitized_text.encode("utf-8")
        source_hash, stored_hash = hashlib.sha256(source).hexdigest(), hashlib.sha256(stored).hexdigest()
        request_fingerprint = hashlib.sha256(canonical_json({
            "scope_id": scope_id, "task_id": task_id, "run_id": run_id, "step_id": step_id,
            "source_sha256": source_hash, "format": fmt, "status": status, "exit_code": exit_code,
            "criteria_claim_count": len(criteria),
            "criteria_claims_sha256": hashlib.sha256(canonical_json(criteria).encode("utf-8")).hexdigest(),
            "source_artifact_id": p.get("source_artifact_id"),
        }).encode("utf-8")).hexdigest()
        storage = Phase3Storage(db)
        with closing(db.connect()) as conn:
            prior_row = conn.execute("SELECT id,scope_id,owner_actor,owner_session FROM phase3_journal WHERE kind=? AND request_id=?",
                                     ("tool_result.compact", request_id)).fetchone()
        if prior_row:
            if tuple(prior_row[1:]) != (scope_id, actor, session):
                raise PmtError("ownership_conflict", "Result request belongs to another scope or owner", 3)
            intent = storage.get_intent(prior_row["id"], scope_id, actor, session) or {}
            body, outcome = intent.get("body", {}), intent.get("outcome") or {}
            if body.get("request_fingerprint") != request_fingerprint or body.get("result_id") != result_id:
                raise PmtError("request_conflict", "Request ID was reused for different output or metadata", 3)
            stage = body.get("stage")
            if stage == "completed":
                summary = outcome.get("summary")
                evidence = (summary or {}).get("evidence_ref") or {}
                if not evidence.get("id"):
                    raise PmtError("result_receipt_invalid", "Completed result has no evidence reference", 4)
                _verify_resource(db, scope_id, evidence["id"], evidence.get("sha256"))
                return _response(req, summary), 0
            if stage == "failed":
                storage.update_intent(intent["id"], "failed", "persisting", scope_id, actor, session,
                                      {"result_id": result_id, "request_fingerprint": request_fingerprint})
                stage = "persisting"
        else:
            try:
                intent = storage.append_intent("tool_result.compact", request_id, scope_id, actor, session,
                                               {"stage": "received", "result_id": result_id, "task_id": task_id,
                                                "run_id": run_id, "step_id": step_id,
                                                "source_sha256": source_hash, "request_fingerprint": request_fingerprint})
                stage = "received"
            except PmtError as exc:
                # A concurrent identical request may have advanced its journal after our lookup.
                if exc.code != "request_conflict":
                    raise
                with closing(db.connect()) as conn:
                    raced = conn.execute("SELECT id,scope_id,owner_actor,owner_session FROM phase3_journal WHERE kind=? AND request_id=?",
                                         ("tool_result.compact", request_id)).fetchone()
                if not raced or tuple(raced[1:]) != (scope_id, actor, session):
                    raise
                intent = storage.get_intent(raced["id"], scope_id, actor, session) or {}
                body = intent.get("body", {})
                if body.get("request_fingerprint") != request_fingerprint or body.get("result_id") != result_id:
                    raise PmtError("request_conflict", "Request ID was reused for different output or metadata", 3)
                if body.get("stage") == "completed":
                    return _response(req, (intent.get("outcome") or {}).get("summary")), 0
                raise PmtError("execution_unknown", "The same result request is already being reconciled", 4, True)

        if stage == "received":
            storage.update_intent(intent["id"], "received", "persisting", scope_id, actor, session,
                                  {"result_id": result_id, "request_fingerprint": request_fingerprint})
            stage = "persisting"
        try:
            resource = _register_managed_resource(db, req, scope_id=scope_id, result_id=result_id,
                                                  request_id=request_id, stored=stored)
            if stage == "persisting":
                storage.update_intent(intent["id"], "persisting", "resource_published", scope_id, actor, session,
                                      {"resource_id": resource["artifact_id"], "resource_sha256": stored_hash})
            lines = _line_count(stored)
            metadata = {
                "schema_version": 1, "storage_kind": "registered_resource",
                "resource_id": resource["artifact_id"], "run_id": run_id,
                "step_id": step_id, "task_id": task_id,
                "reported_status": status, "reported_exit_code": exit_code,
                "status": observation["status"], "exit_code": observation["exit_code"],
                "status_source": observation["source"], "status_reason": observation["reason"],
                "runner_receipt_ref": (binding.get("runner_receipt") or {}).get("receipt_ref"),
                "format": fmt,
                "source_sha256": source_hash, "artifact_sha256": stored_hash,
                "source_size_bytes": len(source), "size_bytes": len(stored),
                "line_count": lines, "redactions": redactions,
                "criteria_claim_count": len(criteria),
                "criteria_claims_sha256": hashlib.sha256(canonical_json(criteria).encode("utf-8")).hexdigest(),
                "observed_by": "request_producer",
                "run_state_at_observation": binding["state"],
            }
            saved = storage.put_object("tool_result", result_id, scope_id, actor, session,
                                       stored_hash, 0, metadata, state="ready",
                                       request_id=request_id)
            evidence_ref = {"kind": "artifact", "id": resource["artifact_id"],
                            "scope_id": scope_id, "sha256": stored_hash,
                            "source_revision": saved["revision"]}
            summary = {
                "result_id": result_id, "task_id": task_id, "run_id": run_id, "step_id": step_id,
                "reported_status": status, "reported_exit_code": exit_code,
                "status": observation["status"], "exit_code": observation["exit_code"],
                "status_source": observation["source"], "status_reason": observation["reason"],
                "runner_receipt_ref": (binding.get("runner_receipt") or {}).get("receipt_ref"),
                "format": fmt,
                "observed_by": "request_producer",
                "run_state_at_observation": binding["state"],
                "capability": "supported", "evidence_ref": evidence_ref,
                "source_sha256": source_hash, "artifact_sha256": stored_hash,
                "source_size_bytes": len(source), "size_bytes": len(stored), "line_count": lines,
                "redactions": redactions, "preservation": "complete_redacted",
                "criteria_claim_count": len(criteria),
                "criteria_claims_sha256": hashlib.sha256(canonical_json(criteria).encode("utf-8")).hexdigest(),
                "criteria_verdict": {"status": "not_evaluated", "source": "tool_output_not_authoritative"},
                "summary": f"Tool {observation['status']}; exit={observation['exit_code'] if observation['exit_code'] is not None else 'unknown'}; {lines} lines, {len(stored)} stored bytes.",
            }
            try:
                _staging_file(db, request_id).unlink(missing_ok=True)
            except OSError:
                # The published, verified resource remains authoritative; only sanitized staging bytes remain.
                summary["staging_cleanup"] = "pending"
            storage.update_intent(intent["id"], "resource_published", "completed", scope_id, actor, session,
                                  {"summary": summary, "evidence_ref": evidence_ref})
            return _response(req, summary), 0
        except Exception as exc:
            try:
                current = storage.get_intent(intent["id"], scope_id, actor, session) or {}
                current_stage = current.get("body", {}).get("stage", "persisting")
                storage.update_intent(intent["id"], current_stage, "failed", scope_id, actor, session,
                                      {"error_code": getattr(exc, "code", "result_persist_failed")})
            except Exception:
                pass
            if isinstance(exc, PmtError):
                raise
            raise PmtError("result_persist_failed", "Tool output could not be safely persisted", 4, True) from exc
    except PmtError as exc:
        return _response(req, error=exc.as_dict()), exc.exit_code
    except OSError as exc:
        error = PmtError("result_io_error", "Tool result file operation failed", 4, True,
                         {"errno": getattr(exc, "errno", None)})
        return _response(req, error=error.as_dict()), error.exit_code
    except (TypeError, ValueError) as exc:
        error = PmtError("input_invalid", "Tool result input could not be processed safely")
        return _response(req, error=error.as_dict()), error.exit_code
    except Exception:
        error = PmtError("result_internal_error", "Tool result operation failed unexpectedly", 5)
        return _response(req, error=error.as_dict()), error.exit_code


def _decode_cursor(value, result_id, scope_id, digest):
    if value is None:
        return 0
    if not isinstance(value, str) or len(value) > 2048:
        raise PmtError("cursor_invalid", "Cursor is invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise PmtError("cursor_invalid", "Cursor is invalid") from exc
    if (not isinstance(obj, dict) or set(obj) != {"result_id", "scope_id", "artifact_sha256", "offset"}
            or obj["result_id"] != result_id or obj["scope_id"] != scope_id
            or obj["artifact_sha256"] != digest or type(obj["offset"]) is not int or obj["offset"] < 0):
        raise PmtError("cursor_invalid", "Cursor does not match this stored result")
    return obj["offset"]


def _encode_cursor(result_id, scope_id, digest, offset):
    raw = canonical_json({"result_id": result_id, "scope_id": scope_id,
                          "artifact_sha256": digest, "offset": offset}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _line_breaks(data):
    count = 0
    index = 0
    while index < len(data):
        if data[index] == 13:
            count += 1
            index += 2 if index + 1 < len(data) and data[index + 1] == 10 else 1
        elif data[index] == 10:
            count += 1
            index += 1
        else:
            index += 1
    return count


def _line_count(data):
    return _line_breaks(data) + (1 if data and not data.endswith((b"\r", b"\n")) else 0)


def _limit_lines(data, start, end, maximum):
    lines = 1
    index = start
    while index < end:
        if data[index] == 13:
            if lines == maximum:
                return index + (2 if index + 1 < end and data[index + 1] == 10 else 1)
            lines += 1
            index += 2 if index + 1 < end and data[index + 1] == 10 else 1
        elif data[index] == 10:
            if lines == maximum:
                return index + 1
            lines += 1
            index += 1
        else:
            index += 1
    return end


def _detail(db, conn, req):
    from ..resources import _artifact_path, check_artifact

    p = _payload(req)
    unknown = set(p) - _DETAIL_FIELDS
    if unknown:
        raise PmtError("unknown_fields", "Unsupported detail fields", details={"fields": sorted(unknown)})
    result_id = _required_text(p.get("result_id"), "result_id")
    scope_id = _required_text(req.get("scope_id") or p.get("scope_id"), "scope_id")
    actor = _required_text(req.get("actor"), "actor")
    session = _required_text(req.get("session_id"), "session_id")
    max_bytes = p.get("max_bytes", 16 * 1024)
    max_lines = p.get("max_lines", 200)
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_DETAIL:
        raise PmtError("input_invalid", "max_bytes must be between 1 and 65536")
    if type(max_lines) is not int or not 1 <= max_lines <= 500:
        raise PmtError("input_invalid", "max_lines must be between 1 and 500")
    storage = Phase3Storage(db)
    item = storage.get_object("tool_result", result_id, scope_id, actor, session, conn=conn)
    if not item or item["state"] != "ready":
        raise PmtError("result_not_found", "Stored result is unavailable to this owner", 3)
    metadata = item["body"]
    _validate_binding(db, conn, scope_id=scope_id, actor=actor, session=session,
                      task_id=metadata.get("task_id"), run_id=metadata.get("run_id"),
                      step_id=metadata.get("step_id"))
    if metadata.get("storage_kind") != "registered_resource":
        raise PmtError("result_format_unsupported", "Result is not stored as a readable text resource")
    rid = metadata.get("resource_id")
    row = conn.execute("SELECT scope_id,relative_path,size_bytes,sha256,state FROM artifacts WHERE id=?", (rid,)).fetchone()
    if not row or row["scope_id"] != scope_id or row["state"] != "ready":
        raise PmtError("result_resource_unavailable", "Stored result resource is unavailable", 3)
    checked = check_artifact(db, conn, rid)
    if not checked["valid"] or checked["sha256"] != metadata.get("artifact_sha256"):
        raise PmtError("result_hash_mismatch", "Stored result failed integrity verification", 4)
    raw = _artifact_path(db, row["relative_path"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != metadata.get("artifact_sha256") or len(raw) != metadata.get("size_bytes"):
        raise PmtError("result_hash_mismatch", "Stored result failed integrity verification", 4)
    start = _decode_cursor(p.get("cursor"), result_id, scope_id, digest)
    if start > len(raw):
        raise PmtError("cursor_invalid", "Cursor is outside the stored result")
    try:
        raw[:start].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PmtError("cursor_invalid", "Cursor splits a UTF-8 character") from exc
    if start and start < len(raw) and raw[start - 1:start + 1] == b"\r\n":
        raise PmtError("cursor_invalid", "Cursor splits a CRLF line ending")
    end = min(len(raw), start + max_bytes)
    while end > start:
        try:
            raw[start:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    if end == start and start < len(raw):
        raise PmtError("cursor_invalid", "Byte limit is too small for the next UTF-8 character")
    if end > start and end < len(raw) and raw[end - 1:end + 1] == b"\r\n":
        if end - start < max_bytes:
            end += 1
        else:
            end -= 1
    if end == start and start < len(raw):
        raise PmtError("cursor_invalid", "Byte limit is too small for a complete line ending")
    end = _limit_lines(raw, start, end, max_lines)
    content = raw[start:end]
    if len(content) > max_bytes or _line_count(content) > max_lines:
        raise PmtError("detail_boundary_error", "Detail exceeds requested line or byte bounds", 5)
    next_cursor = _encode_cursor(result_id, scope_id, digest, end) if end < len(raw) else None
    return {"result_id": result_id, "evidence_ref": {"kind": "artifact", "id": rid,
            "scope_id": scope_id, "source_hash": digest, "source_revision": item["revision"]},
            "artifact_sha256": digest, "start_byte": start, "end_byte": end,
            "start_line": _line_breaks(raw[:start]) + 1,
            "end_line": (_line_breaks(raw[:end]) if raw[:end].endswith((b"\r", b"\n"))
                         else _line_breaks(raw[:end]) + (1 if end else 0)),
            "content": content.decode("utf-8"),
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "next_cursor": next_cursor, "available_end_byte": len(raw),
            "discarded_ranges": []}


def handle(db, conn, req):
    try:
        return _detail(db, conn, req)
    except PmtError:
        raise
    except OSError as exc:
        raise PmtError("result_io_error", "Stored result file could not be read", 4, True,
                       {"errno": getattr(exc, "errno", None)}) from exc


def compact_tool_result(db, req):
    """Named adapter entry point for direct Python callers."""
    return execute_file(db, req)


def read_tool_result_detail(db, conn, req):
    """Named adapter entry point; caller connection remains caller-owned."""
    return _detail(db, conn, req)
