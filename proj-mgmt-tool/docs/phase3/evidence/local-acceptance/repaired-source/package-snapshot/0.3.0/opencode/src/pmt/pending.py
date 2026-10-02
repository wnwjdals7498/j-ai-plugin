"""Durable local preservation and conservative replay for completed Host results.

The outbox is not a local authority for claims or shared state. It accepts only
completed result operations and replays their exact request after an owned Host
read confirms current identity, scope, source, and run revision.
"""
from __future__ import annotations

from contextlib import closing
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid

from .db import semantic_request_fingerprint
from .errors import PmtError
from .service import normalize_request
from .util import canonical_json, fingerprint, utc_now
from .efficiency.source import pin_source

SCHEMA_VERSION = 1
ALLOWED_RESULT_OPERATIONS = frozenset({"submit_execution_result", "record_verification", "record_reuse_result"})
RESOURCE_PURPOSES = frozenset({"evidence", "result", "verification_snapshot"})
MAX_PENDING_JSON = 2 * 1024 * 1024
MAX_STAGED_RESOURCE = 8 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = frozenset({"authorization", "bearer", "credential", "password", "secret", "api_key",
    "claim_token", "token", "env", "environment", "argv", "cwd", "local_root",
    "local_workspace", "private_key"})
_FORBIDDEN_KEY_MARKERS = ("authorization", "credential", "password", "secret", "token", "api_key", "private_key")
_FACT_SCHEMA = "pmt-pending-current-facts-v1"
_RECEIPT_SCHEMA = "pmt-hosted-runner-receipt-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS outbox_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_results(
 request_id TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL, body_sha256 TEXT NOT NULL,
 immutable_json TEXT NOT NULL, immutable_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','checking','unknown','applied','conflict','stale','pending_incomplete')),
 attempts INTEGER NOT NULL DEFAULT 0, reason_code TEXT, receipt_json TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pending_results_state_idx ON pending_results(state, created_at);
CREATE TABLE IF NOT EXISTS pending_resources(
 upload_request_id TEXT PRIMARY KEY, immutable_json TEXT NOT NULL, immutable_sha256 TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('staged','checking','unknown','published','conflict','stale','pending_incomplete')),
 artifact_ref_json TEXT, attempts INTEGER NOT NULL DEFAULT 0, reason_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pending_resources_state_idx ON pending_resources(state, created_at);
"""


def _fail(code: str, message: str, exit_code: int = 2, details=None, retryable: bool = False):
    if type(exit_code) is not int or exit_code not in range(6):
        raise ValueError("pending exit_code must be in 0..5")
    raise PmtError(code, message, exit_code, retryable, details)


def _uuid(value, label):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("pending_input_invalid", f"{label} must be a canonical UUID") from exc
    return value


def _sha(value, label):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        _fail("pending_input_invalid", f"{label} must be a lowercase SHA-256 digest")
    return value


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _guard(value, depth=0):
    if depth > 32:
        _fail("pending_content_unsafe", "Pending content is too deeply nested")
    if isinstance(value, dict):
        for key, item in value.items():
            if (not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS
                    or any(marker in key.casefold() for marker in _FORBIDDEN_KEY_MARKERS)):
                _fail("pending_content_unsafe", "Pending content contains a credential, command, environment, or local path field")
            _guard(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 10000:
            _fail("pending_content_too_large", "Pending array exceeds its bound")
        for item in value:
            _guard(item, depth + 1)
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_PENDING_JSON:
            _fail("pending_content_too_large", "Pending text exceeds its bound")
        if re.search(r"(?:^[A-Za-z]:[\\/]|^\\\\|(?i:bearer)\s+|\bsk-[A-Za-z0-9_-]{8,})", value):
            _fail("pending_content_unsafe", "Pending content contains a credential or physical local path")
    elif value is not None and type(value) not in {int, bool, float}:
        _fail("pending_content_invalid", "Pending content must contain JSON values only")


def _canonical(value):
    return canonical_json(value)


def _identity(value):
    required = {"namespace_id", "actor", "device_id", "environment_id", "session_id"}
    if not isinstance(value, dict) or set(value) != required:
        _fail("pending_identity_invalid", "Pending owner identity is incomplete")
    return {"namespace_id": _uuid(value["namespace_id"], "namespace_id"),
            "actor": _nonempty(value["actor"], "actor"), "device_id": _uuid(value["device_id"], "device_id"),
            "environment_id": _uuid(value["environment_id"], "environment_id"),
            "session_id": _nonempty(value["session_id"], "session_id")}


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        _fail("pending_input_invalid", f"{label} must be bounded nonempty text")
    return value


def _safe_root(path):
    path = Path(path).expanduser().absolute()
    path.mkdir(parents=True, exist_ok=True)
    current = path
    while current != current.parent:
        if current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)()):
            _fail("pending_path_invalid", "Pending storage root cannot contain a link or junction", 5)
        current = current.parent
    return path.resolve(strict=True)


def preflight_new_shared_write(store, operation: str, *, scope_id, permission="write") -> dict:
    """Require an authenticated, compatible Host before any new shared write.

    This is not an offline queue. Final result operations with a verified local
    runtime receipt use ``PendingOutbox`` after transport uncertainty instead.
    """
    _nonempty(operation, "operation")
    _uuid(scope_id, "scope_id")
    if permission not in {"write", "runtime", "review", "admin"}:
        _fail("pending_input_invalid", "Unsupported preflight permission")
    if operation in ALLOWED_RESULT_OPERATIONS:
        _fail("pending_result_hook_required", "Completed results must use the verified result path", 3)
    check = getattr(store, "check_compatibility", None)
    if not callable(check):
        _fail("hosted_write_blocked", "Shared write requires the configured authenticated Host StorePort", 3)
    try:
        result = check()
    except Exception as exc:
        code = getattr(exc, "code", None) or "host_unavailable"
        raise PmtError("hosted_write_blocked", "Host is unavailable; a new shared write was not queued", 3,
                       True, {"reason_code": code}) from exc
    if (not isinstance(result, dict) or result.get("compatible") is not True
            or result.get("namespace_id") != getattr(store, "namespace_id", None)
            or result.get("device_id") != getattr(store, "device_id", None)
            or not isinstance(result.get("scopes"), list)
            or (scope_id not in result["scopes"] and "*" not in result["scopes"])
            or not isinstance(result.get("permissions"), list)
            or permission not in result["permissions"]):
        _fail("hosted_write_blocked", "Host identity or compatibility is not verified; shared write was not queued", 3)
    return {"state": "connected", "namespace_id": result["namespace_id"],
            "device_id": result["device_id"], "scope_id": scope_id, "permission": permission,
            "core_version": result.get("core_version"),
            "db_schema": result.get("db_schema"), "graph_schema": result.get("graph_schema")}


@dataclass(frozen=True)
class PendingRef:
    request_id: str
    request_fingerprint: str
    body_sha256: str
    state: str


@dataclass(frozen=True)
class PendingResourceRef:
    upload_request_id: str
    sha256: str
    size: int
    scope_id: str
    state: str


@dataclass(frozen=True)
class PendingReceipt:
    state: str
    request_id: str
    request_fingerprint: str
    changed_dimensions: tuple[str, ...] = ()
    reason_code: str | None = None
    server_result_ref: str | None = None


class PendingOutbox:
    """A per-namespace local spool of immutable completed-result requests."""

    def __init__(self, data_root, *, namespace_id, actor, device_id, environment_id, session_id):
        self.identity = _identity({"namespace_id": namespace_id, "actor": actor, "device_id": device_id,
                                   "environment_id": environment_id, "session_id": session_id})
        self.root = _safe_root(Path(data_root) / "host-pending")
        self.resource_root = self.root / "resources"
        self.resource_root.mkdir(exist_ok=True)
        if self.resource_root.is_symlink() or bool(getattr(self.resource_root, "is_junction", lambda: False)()):
            _fail("pending_path_invalid", "Pending resource directory cannot be a link", 5)
        self.path = self.root / "outbox.sqlite3"
        if self.path.is_symlink() or bool(getattr(self.path, "is_junction", lambda: False)()):
            _fail("pending_path_invalid", "Pending database cannot be a link", 5)
        with closing(self._connect()) as conn:
            conn.executescript(_DDL)
            prior = conn.execute("SELECT value FROM outbox_meta WHERE key='schema_version'").fetchone()
            if prior and prior[0] != str(SCHEMA_VERSION):
                _fail("pending_schema_unsupported", "Pending outbox schema version is unsupported", 3)
            conn.execute("INSERT OR IGNORE INTO outbox_meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            # A process crash while reconciling is an unknown effect, never a lease to replay blindly.
            conn.execute("UPDATE pending_results SET state='unknown',reason_code='previous_reconcile_interrupted',updated_at=? WHERE state='checking'",
                         (utc_now(),))
            conn.execute("UPDATE pending_resources SET state='unknown',reason_code='previous_upload_interrupted',updated_at=? WHERE state='checking'",
                         (utc_now(),))

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _request(self, request):
        req = normalize_request(request)
        if req["operation"] not in ALLOWED_RESULT_OPERATIONS:
            _fail("pending_operation_forbidden", "Only completed result operations may enter the Host outbox", 3)
        if (req["actor"], req["session_id"]) != (self.identity["actor"], self.identity["session_id"]):
            _fail("pending_owner_mismatch", "Request owner differs from the pending namespace owner", 3)
        if req.get("scope_id") is None:
            _fail("pending_scope_required", "Pending result requires its exact scope ID", 3)
        _uuid(req["scope_id"], "scope_id")
        encoded = _canonical(req).encode("utf-8")
        if len(encoded) > MAX_PENDING_JSON:
            _fail("pending_content_too_large", "Pending request exceeds 2 MiB")
        req_fingerprint = semantic_request_fingerprint(req)
        template = copy.deepcopy(req)
        private_fields = []
        route_safe = True
        if req["operation"] == "submit_execution_result":
            result = req["payload"].get("result")
            run_id = _uuid(req["payload"].get("run_id"), "run_id")
            route = result.get("actual_route") if isinstance(result, dict) else None
            if not isinstance(route, dict):
                _fail("pending_content_invalid", "Completed execution result must include its pinned actual_route", 3)
            try:
                _guard(route)
            except PmtError as exc:
                if exc.code != "pending_content_unsafe":
                    raise
                route_safe = False
            template["payload"]["result"]["actual_route"] = {"$pending_actual_route_ref": run_id}
            private_fields = ["payload.result.actual_route"]
        _guard(template)
        command = req.get("payload", {}).get("command") if req["operation"] == "record_verification" else None
        if command is not None:
            parts = command if isinstance(command, list) else [command]
            if (not parts or not all(isinstance(part, str) and part and len(part) <= 4096 for part in parts)
                    or any(re.search(r"(?:^/|^[A-Za-z]:[\\/]|^\\\\|(?i:bearer)\s+|\bsk-[A-Za-z0-9_-]{8,}|(?i)(?:token|password|secret|api[_-]?key)\s*=)", part)
                           for part in parts)):
                _fail("pending_content_unsafe", "Verification command contains a secret or physical local path")
        return req, encoded, req_fingerprint, template, private_fields, route_safe

    @staticmethod
    def _facts(value):
        required = {"schema", "namespace_id", "actor", "device_id", "environment_id", "session_id", "scope_id",
                    "source_fingerprint", "source_ref", "authorization_ref", "run_ref", "run_id", "run_revision",
                    "run_state", "run_owner"}
        if not isinstance(value, dict) or set(value) != required or value.get("schema") != _FACT_SCHEMA:
            _fail("pending_current_facts_invalid", "Current-state reader must return the versioned verified fact set", 3)
        ident = _identity({key: value[key] for key in ("namespace_id", "actor", "device_id", "environment_id", "session_id")})
        _uuid(value["scope_id"], "scope_id")
        _sha(value["source_fingerprint"], "source_fingerprint")
        source_ref = value["source_ref"]
        if (not isinstance(source_ref, dict) or set(source_ref) != {"schema", "ref", "pin"}
                or source_ref.get("schema") != "pmt-source-pin-ref-v1"):
            _fail("pending_current_facts_invalid", "Current SourcePin reference is incomplete", 3)
        _nonempty(source_ref["ref"], "source_ref.ref")
        try:
            pin = pin_source(source_ref["pin"])
        except PmtError as exc:
            raise PmtError("pending_current_facts_invalid", "Current SourcePin is invalid", 3) from exc
        if pin.source_hash != value["source_fingerprint"] or pin.project_id != value["scope_id"]:
            _fail("pending_current_facts_invalid", "Current SourcePin hash or project differs from selected scope", 3)
        auth = value["authorization_ref"]
        auth_fields = {"schema", "ref", "namespace_id", "actor", "device_id", "environment_id", "session_id",
                       "scope_id", "scopes", "permissions", "sha256"}
        if not isinstance(auth, dict) or set(auth) != auth_fields or auth.get("schema") != "pmt-host-auth-facts-v1":
            _fail("pending_current_facts_invalid", "Current authorization reference is incomplete", 3)
        _nonempty(auth["ref"], "authorization_ref.ref")
        _sha(auth["sha256"], "authorization_ref.sha256")
        if fingerprint({key: auth[key] for key in auth_fields - {"sha256"}}) != auth["sha256"]:
            _fail("pending_current_facts_invalid", "Current authorization reference hash is invalid", 3)
        if any(auth[key] != ident[key] for key in ("namespace_id", "actor", "device_id", "environment_id", "session_id")) \
                or auth["scope_id"] != value["scope_id"]:
            _fail("pending_current_facts_invalid", "Current authorization reference identity or scope changed", 3)
        if (not isinstance(auth["scopes"], list) or any(not isinstance(item, str) for item in auth["scopes"])
                or value["scope_id"] not in auth["scopes"] and "*" not in auth["scopes"]
                or not isinstance(auth["permissions"], list) or any(not isinstance(item, str) for item in auth["permissions"])
                or "write" not in auth["permissions"]
                or (value["run_id"] is not None and "runtime" not in auth["permissions"])):
            _fail("pending_current_facts_invalid", "Current authorization lacks the selected scope or write permission", 3)
        if value["run_id"] is not None:
            _uuid(value["run_id"], "run_id")
        if type(value["run_revision"]) is not int or value["run_revision"] < 0:
            _fail("pending_current_facts_invalid", "Current run revision must be a nonnegative integer", 3)
        if value["run_state"] is not None and value["run_state"] not in {"queued", "starting", "running", "reconciling",
                "cancel_requested", "review_pending", "succeeded", "failed", "blocked", "canceled"}:
            _fail("pending_current_facts_invalid", "Current run state is unsupported", 3)
        owner = value["run_owner"]
        if owner is not None:
            required_owner = {"actor", "device_id", "session_id"}
            if not isinstance(owner, dict) or set(owner) != required_owner:
                _fail("pending_current_facts_invalid", "Current run owner facts are incomplete", 3)
            _nonempty(owner["actor"], "run_owner.actor")
            _uuid(owner["device_id"], "run_owner.device_id")
            _nonempty(owner["session_id"], "run_owner.session_id")
        run_ref = value["run_ref"]
        if run_ref is None:
            if value["run_id"] is not None or owner is not None or value["run_state"] is not None:
                _fail("pending_current_facts_invalid", "A run read reference is required when a run is selected", 3)
        else:
            run_fields = {"schema", "ref", "run_id", "scope_id", "revision", "state", "owner", "workspace", "actual_route", "sha256"}
            if not isinstance(run_ref, dict) or set(run_ref) != run_fields or run_ref.get("schema") != "pmt-host-run-read-v1":
                _fail("pending_current_facts_invalid", "Current run read reference is incomplete", 3)
            if not isinstance(run_ref["actual_route"], dict):
                _fail("pending_current_facts_invalid", "Current run read lacks its bound actual route", 3)
            _guard(run_ref["actual_route"])
            _nonempty(run_ref["workspace"], "run_ref.workspace")
            _nonempty(run_ref["ref"], "run_ref.ref")
            _sha(run_ref["sha256"], "run_ref.sha256")
            if fingerprint({key: run_ref[key] for key in run_fields - {"sha256"}}) != run_ref["sha256"]:
                _fail("pending_current_facts_invalid", "Current run read reference hash is invalid", 3)
            if (run_ref["run_id"] != value["run_id"] or run_ref["scope_id"] != value["scope_id"]
                    or run_ref["revision"] != value["run_revision"] or run_ref["state"] != value["run_state"]
                    or run_ref["owner"] != owner):
                _fail("pending_current_facts_invalid", "Current run read facts disagree with their bound reference", 3)
        return {**value, **ident}

    def _immutable_row(self, row, expected_id, *, resource=False):
        try:
            immutable = json.loads(row["immutable_json"])
        except (ValueError, TypeError) as exc:
            raise PmtError("pending_corrupt", "Pending record JSON is corrupt", 5) from exc
        if not isinstance(immutable, dict) or _digest(_canonical(immutable).encode()) != row["immutable_sha256"]:
            _fail("pending_corrupt", "Pending record checksum does not match", 5)
        key = "upload_request_id" if resource else "request_id"
        if immutable.get(key) != expected_id:
            _fail("pending_corrupt", "Pending record identity does not match its index", 5)
        if not resource and (row["request_fingerprint"] != immutable.get("request_fingerprint")
                or row["body_sha256"] != immutable.get("body_sha256")):
            _fail("pending_corrupt", "Pending record index does not match its immutable body", 5)
        return immutable

    def enqueue_result(self, request, *, base_run_revision, source_fingerprint,
                       runtime_receipt_ref, runtime_receipt_sha256, receipt_reader, run_id=None):
        """Persist a final result only when a local immutable runtime receipt verifies it."""
        req, body, req_fingerprint, template, private_fields, route_safe = self._request(request)
        if type(base_run_revision) is not int or base_run_revision < 1:
            _fail("pending_input_invalid", "base_run_revision must be a positive integer")
        source_fingerprint = _sha(source_fingerprint, "source_fingerprint")
        runtime_receipt_ref = _nonempty(runtime_receipt_ref, "runtime_receipt_ref")
        receipt_sha = _sha(runtime_receipt_sha256, "runtime_receipt_sha256")
        if not callable(receipt_reader):
            _fail("pending_receipt_unavailable", "An owner runtime receipt reader is required", 3)
        run_id = _uuid(run_id or req["payload"].get("run_id"), "run_id")
        try:
            receipt_parts = receipt_reader(runtime_receipt_ref)
        except Exception as exc:
            immutable = self._result_immutable(req, body, req_fingerprint, base_run_revision,
                source_fingerprint, runtime_receipt_ref, receipt_sha, run_id, "pending_incomplete", "runtime_receipt_unavailable",
                template=template, private_fields=private_fields)
            self._insert_pending(immutable, "pending_incomplete", "runtime_receipt_unavailable")
            return PendingRef(req["request_id"], req_fingerprint, _digest(body), "pending_incomplete")
        if (not isinstance(receipt_parts, dict) or set(receipt_parts) != {"manifest_bytes", "receipt_bytes", "output_bytes"}
                or any(not isinstance(receipt_parts[key], bytes) for key in ("manifest_bytes", "receipt_bytes", "output_bytes"))
                or any(len(receipt_parts[key]) > MAX_PENDING_JSON for key in ("manifest_bytes", "receipt_bytes", "output_bytes"))
                or _digest(receipt_parts["manifest_bytes"]) != receipt_sha):
            immutable = self._result_immutable(req, body, req_fingerprint, base_run_revision,
                source_fingerprint, runtime_receipt_ref, receipt_sha, run_id, "pending_incomplete", "runtime_receipt_hash_unavailable",
                template=template, private_fields=private_fields)
            self._insert_pending(immutable, "pending_incomplete", "runtime_receipt_hash_unavailable")
            return PendingRef(req["request_id"], req_fingerprint, _digest(body), "pending_incomplete")
        try:
            receipt = self._validate_runtime_receipt(receipt_parts, runtime_receipt_ref, req,
                                                     base_run_revision, source_fingerprint, run_id)
        except PmtError as exc:
            if exc.code != "pending_incomplete":
                raise
            immutable = self._result_immutable(req, body, req_fingerprint, base_run_revision,
                source_fingerprint, runtime_receipt_ref, receipt_sha, run_id, "pending_incomplete", exc.code,
                template=template, private_fields=private_fields)
            self._insert_pending(immutable, "pending_incomplete", exc.code)
            return PendingRef(req["request_id"], req_fingerprint, _digest(body), "pending_incomplete")
        initial_state = "pending" if route_safe else "pending_incomplete"
        initial_reason = None if route_safe else "actual_route_contains_private_fields"
        immutable = self._result_immutable(req, body, req_fingerprint, base_run_revision,
            source_fingerprint, runtime_receipt_ref, receipt_sha, run_id, initial_state, initial_reason,
            template=template, private_fields=private_fields, workspace=receipt["canonical_workspace"])
        immutable["runtime_receipt_id"] = runtime_receipt_ref
        immutable["runtime_receipt_fixture"] = receipt["fixture"]
        immutable["route_sha256"] = receipt["route_sha256"]
        self._insert_pending(immutable, initial_state, initial_reason)
        return PendingRef(req["request_id"], req_fingerprint, _digest(body), initial_state)

    def _validate_runtime_receipt(self, parts, receipt_ref, req, base_revision, source_hash, run_id):
        manifest_raw, receipt_raw, output_raw = (parts["manifest_bytes"], parts["receipt_bytes"], parts["output_bytes"])
        try:
            receipt = json.loads(manifest_raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise PmtError("pending_receipt_invalid", "Runtime receipt manifest must be UTF-8 JSON", 3) from exc
        required = {"schema", "run_id", "step_id", "scope_id", "canonical_workspace", "attached_run_revision",
                    "owner", "source_hash", "context_ref", "runner_kind", "prompt_sha256", "route_sha256",
                    "receipt", "receipt_sha256", "output_sha256",
                    "output_sha256_basis", "output_available", "report_valid", "model_report_sha256", "provenance", "fixture"}
        if not isinstance(receipt, dict) or set(receipt) != required or receipt.get("schema") != _RECEIPT_SCHEMA:
            _fail("pending_receipt_incomplete", "Runtime receipt schema or required fields are incomplete", 3)
        _uuid(receipt["run_id"], "receipt.run_id")
        _uuid(receipt["step_id"], "receipt.step_id")
        if receipt["scope_id"] != req.get("scope_id"):
            _fail("pending_receipt_mismatch", "Runtime receipt scope differs from the result request", 3)
        _nonempty(receipt["canonical_workspace"], "receipt.canonical_workspace")
        if run_id is not None and receipt["run_id"] != run_id:
            _fail("pending_receipt_mismatch", "Runtime receipt refers to a different run", 3)
        if type(receipt["attached_run_revision"]) is not int or receipt["attached_run_revision"] != base_revision:
            _fail("pending_receipt_mismatch", "Runtime receipt run revision differs from pending base revision", 3)
        _sha(receipt["source_hash"], "receipt.source_hash")
        if receipt["source_hash"] != source_hash:
            _fail("pending_receipt_mismatch", "Runtime receipt source differs from pending source", 3)
        owner = receipt["owner"]
        if (not isinstance(owner, dict) or set(owner) != {"namespace_id", "actor", "session_id", "device_id", "environment_id"}
                or owner["namespace_id"] != self.identity["namespace_id"]
                or owner["actor"] != self.identity["actor"] or owner["session_id"] != self.identity["session_id"]
                or owner["device_id"] != self.identity["device_id"] or owner["environment_id"] != self.identity["environment_id"]):
            _fail("pending_receipt_mismatch", "Runtime receipt does not match the current owner and environment", 3)
        physical = receipt["receipt"]
        physical_fields = {"run_id", "runner_kind", "state", "exit_code", "started_at", "completed_at", "error_code", "stop_confirmed"}
        if (not isinstance(physical, dict) or set(physical) != physical_fields
                or physical.get("run_id") != receipt["run_id"]
                or physical.get("state") not in {"completed", "failed", "canceled"}
                or type(physical.get("stop_confirmed")) is not bool or physical["stop_confirmed"] is not True
                or (physical.get("exit_code") is not None and type(physical["exit_code"]) is not int)):
            _fail("pending_incomplete", "Physical completion is not confirmed by a terminal runtime receipt", 3)
        _sha(receipt["receipt_sha256"], "receipt.receipt_sha256")
        _sha(receipt["output_sha256"], "receipt.output_sha256")
        _sha(receipt["prompt_sha256"], "receipt.prompt_sha256")
        _sha(receipt["route_sha256"], "receipt.route_sha256")
        submitted_route = req.get("payload", {}).get("result", {}).get("actual_route")
        if isinstance(submitted_route, dict) and fingerprint(submitted_route) != receipt["route_sha256"]:
            _fail("pending_receipt_mismatch", "Runtime receipt route hash differs from the submitted result", 3)
        if (_digest(receipt_raw) != receipt["receipt_sha256"] or _digest(output_raw) != receipt["output_sha256"]
                or canonical_json(receipt).encode("utf-8") != manifest_raw):
            _fail("pending_receipt_mismatch", "Managed receipt bytes or canonical manifest hash do not match", 3)
        try:
            raw_receipt = json.loads(receipt_raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise PmtError("pending_receipt_invalid", "Managed runner receipt is not UTF-8 JSON", 3) from exc
        if (not isinstance(raw_receipt, dict) or raw_receipt.get("run_id") != receipt["run_id"]
                or raw_receipt.get("state") != physical["state"] or raw_receipt.get("exit_code") != physical["exit_code"]):
            _fail("pending_receipt_mismatch", "Manifest terminal state differs from managed supervisor bytes", 3)
        if (receipt["output_sha256_basis"] != "exact local supervisor output.json bytes"
                or type(receipt["output_available"]) is not bool or type(receipt["report_valid"]) is not bool
                or receipt["provenance"] != "local_runner_supervisor" or type(receipt["fixture"]) is not bool
                or (receipt["model_report_sha256"] is not None and not _HEX64.fullmatch(receipt["model_report_sha256"]))):
            _fail("pending_receipt_invalid", "Runtime receipt provenance or output basis is invalid", 3)
        expected_ref = "local-runner-receipt:" + receipt["run_id"] + ":" + _digest(manifest_raw)
        if receipt_ref != expected_ref:
            _fail("pending_receipt_mismatch", "Runtime receipt reference does not bind its run and raw receipt hash", 3)
        if receipt["output_available"] is not bool(output_raw):
            _fail("pending_receipt_mismatch", "Runtime receipt output availability disagrees with its managed bytes", 3)
        # Receipt authentication comes from the actual immutable resource bytes and expected hash,
        # not from report_valid or any model-authored success claim.
        return receipt

    def _result_immutable(self, req, body, req_fingerprint, base_revision, source_hash,
                          receipt_ref, receipt_sha, run_id, state, reason, *, template=None, private_fields=(), workspace=None):
        template = template if template is not None else req
        template_raw = _canonical(template).encode("utf-8")
        return {"schema": SCHEMA_VERSION, "identity": self.identity, "request_id": req["request_id"],
            "request_fingerprint": req_fingerprint, "body_sha256": _digest(body),
            "request_json": template_raw.decode("utf-8"), "template_sha256": _digest(template_raw),
            "private_fields": list(private_fields), "operation": req["operation"], "scope_id": req["scope_id"],
            "run_id": run_id, "workspace": workspace, "base_run_revision": base_revision,
            "source_fingerprint": source_hash,
            "runtime_receipt_ref": receipt_ref, "runtime_receipt_sha256": receipt_sha,
            "initial_state": state, "initial_reason": reason}

    def _insert_pending(self, immutable, state, reason):
        raw = _canonical(immutable)
        checksum = _digest(raw.encode("utf-8"))
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM pending_results WHERE request_id=?", (immutable["request_id"],)).fetchone()
                if row:
                    existing = self._immutable_row(row, immutable["request_id"])
                    if existing != immutable:
                        if row["state"] == "pending_incomplete" and state == "pending":
                            mutable = {"initial_state", "initial_reason", "runtime_receipt_id", "runtime_receipt_fixture",
                                "workspace", "route_sha256"}
                            prior_core = {key: value for key, value in existing.items() if key not in mutable}
                            next_core = {key: value for key, value in immutable.items() if key not in mutable}
                            if prior_core != next_core:
                                _fail("pending_request_conflict", "Original request ID already stores a different body or source", 3)
                            updated = _canonical(immutable)
                            changed = conn.execute("UPDATE pending_results SET request_fingerprint=?,body_sha256=?,immutable_json=?,immutable_sha256=?,state='pending',reason_code=NULL,receipt_json=NULL,updated_at=? WHERE request_id=? AND state='pending_incomplete'",
                                (immutable["request_fingerprint"], immutable["body_sha256"], updated,
                                 _digest(updated.encode()), now, immutable["request_id"]))
                            if changed.rowcount != 1:
                                _fail("pending_state_conflict", "Pending incomplete recovery lost its compare-and-set", 3)
                        else:
                            _fail("pending_request_conflict", "Original request ID already stores a different body or source", 3)
                    conn.commit()
                    return
                conn.execute("INSERT INTO pending_results(request_id,request_fingerprint,body_sha256,immutable_json,immutable_sha256,state,reason_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (immutable["request_id"], immutable["request_fingerprint"], immutable["body_sha256"], raw,
                     checksum, state, reason, now, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def list_pending(self):
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM pending_results ORDER BY created_at,request_id").fetchall()
            result = []
            for row in rows:
                item = self._immutable_row(row, row["request_id"])
                if item.get("identity") != self.identity:
                    continue
                result.append(PendingRef(item["request_id"], item["request_fingerprint"], item["body_sha256"], row["state"]))
            return tuple(result)

    def list_resources(self):
        """List owned staged-resource metadata without exposing templates or bytes."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM pending_resources ORDER BY created_at,upload_request_id").fetchall()
            result = []
            for row in rows:
                immutable = self._immutable_row(row, row["upload_request_id"], resource=True)
                if immutable.get("identity") != self.identity:
                    continue
                digest = immutable.get("sha256")
                size = immutable.get("size")
                if immutable.get("initial_state") == "pending_incomplete":
                    digest, size = "", 0
                elif not isinstance(digest, str) or not _HEX64.fullmatch(digest) or type(size) is not int or size < 0:
                    _fail("pending_corrupt", "Pending resource metadata is invalid", 5)
                result.append(PendingResourceRef(row["upload_request_id"], digest, size,
                    immutable["scope_id"], row["state"]))
            return tuple(result)

    def _load(self, request_id):
        request_id = _uuid(request_id, "request_id")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM pending_results WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                _fail("pending_not_found", "Pending request is unavailable", 2)
            immutable = self._immutable_row(row, request_id)
            req = json.loads(immutable["request_json"])
            template_raw = _canonical(req).encode("utf-8")
            private_fields = immutable.get("private_fields", [])
            if _digest(template_raw) != immutable.get("template_sha256"):
                _fail("pending_corrupt", "Pending request template checksum changed", 5)
            if private_fields == ["payload.result.actual_route"]:
                marker = req.get("payload", {}).get("result", {}).get("actual_route")
                if marker != {"$pending_actual_route_ref": immutable.get("run_id")}:
                    _fail("pending_corrupt", "Pending actual-route reference changed", 5)
            elif private_fields:
                _fail("pending_corrupt", "Pending request has an unsupported private-field reference", 5)
            else:
                body = template_raw
                if _digest(body) != immutable["body_sha256"] or semantic_request_fingerprint(req) != immutable["request_fingerprint"]:
                    _fail("pending_corrupt", "Pending body or semantic request fingerprint changed", 5)
            if immutable["identity"] != self.identity:
                _fail("pending_owner_mismatch", "Pending record belongs to another local namespace owner", 3)
            return dict(row), immutable, req

    @staticmethod
    def _rehydrate_request(template, immutable, facts):
        request = copy.deepcopy(template)
        if immutable.get("private_fields") == ["payload.result.actual_route"]:
            if not isinstance(facts.get("run_ref"), dict) or facts["run_ref"].get("run_id") != immutable["run_id"]:
                _fail("pending_current_facts_invalid", "Cannot resolve the pinned actual route from the current run", 3)
            route = copy.deepcopy(facts["run_ref"]["actual_route"])
            _guard(route)
            request["payload"]["result"]["actual_route"] = route
        body = _canonical(request).encode("utf-8")
        if (_digest(body) != immutable["body_sha256"]
                or semantic_request_fingerprint(request) != immutable["request_fingerprint"]):
            _fail("pending_request_reconstruction_conflict", "Current Host route cannot reproduce the original immutable request", 3)
        return request

    @staticmethod
    def _same_owner_scope_facts(facts, immutable, *, require_active_revision):
        changed = []
        expected = immutable["identity"]
        for key in ("namespace_id", "actor", "device_id", "environment_id", "session_id", "scope_id"):
            if facts[key] != (expected.get(key) if key != "scope_id" else immutable["scope_id"]):
                changed.append(key)
        if facts["source_fingerprint"] != immutable["source_fingerprint"]:
            changed.append("source_fingerprint")
        if facts["run_id"] != immutable["run_id"]:
            changed.append("run_id")
        if immutable.get("workspace") is not None and (facts["run_ref"] is None
                or facts["run_ref"].get("workspace") != immutable["workspace"]):
            changed.append("workspace")
        if immutable.get("route_sha256") is not None and (facts["run_ref"] is None
                or fingerprint(facts["run_ref"].get("actual_route")) != immutable["route_sha256"]):
            changed.append("route_sha256")
        if require_active_revision and facts["run_revision"] != immutable["base_run_revision"]:
            changed.append("run_revision")
        owner = facts["run_owner"]
        if immutable["run_id"] is not None and (owner is None or owner.get("actor") != expected["actor"]
                or owner.get("device_id") != expected["device_id"] or owner.get("session_id") != expected["session_id"]):
            changed.append("run_owner")
        if require_active_revision and facts["run_state"] not in {"starting", "running", "reconciling", "cancel_requested"}:
            changed.append("run_state")
        return tuple(dict.fromkeys(changed))

    def reconcile(self, request_id, store, read_current_facts):
        """Lookup original outcome, then revalidate and replay the exact body at most once."""
        row, immutable, req = self._load(request_id)
        if row["state"] in {"applied", "conflict", "stale", "pending_incomplete"}:
            return PendingReceipt(row["state"], request_id, immutable["request_fingerprint"],
                reason_code=row["reason_code"])
        if not callable(getattr(store, "get_request_result", None)) or not callable(getattr(store, "execute", None)):
            _fail("pending_store_invalid", "Reconciliation requires an owned StorePort", 3)
        if not callable(read_current_facts):
            _fail("pending_current_facts_unavailable", "A current Host/source/owner fact reader is required", 3)
        self._set_state(request_id, expected={"pending", "unknown"}, state="checking", reason=None, attempts_delta=1)
        try:
            facts = self._facts(read_current_facts(immutable))
        except Exception as exc:
            return self._uncertain(request_id, immutable, exc)
        try:
            req = self._rehydrate_request(req, immutable, facts)
        except PmtError as exc:
            status = "pending_incomplete" if exc.code == "pending_content_unsafe" else "conflict"
            self._set_state(request_id, expected={"checking"}, state=status, reason=exc.code)
            return PendingReceipt(status, request_id, immutable["request_fingerprint"],
                                  ("actual_route",), exc.code)
        try:
            prior = store.get_request_result(request_id, immutable["identity"]["actor"],
                immutable["identity"]["session_id"], expected_request=req)
        except Exception as exc:
            return self._uncertain(request_id, immutable, exc)
        if prior is not None and (not isinstance(prior, tuple) or len(prior) != 2
                or not isinstance(prior[0], dict) or type(prior[1]) is not int
                or prior[0].get("request_id") != request_id):
            self._set_state(request_id, expected={"checking"}, state="unknown", reason="prior_receipt_invalid")
            return PendingReceipt("unknown", request_id, immutable["request_fingerprint"],
                                  reason_code="prior_receipt_invalid")
        changed = self._same_owner_scope_facts(facts, immutable, require_active_revision=prior is None)
        if changed:
            identity_dimensions = {"namespace_id", "actor", "device_id", "environment_id", "session_id", "scope_id", "run_id"}
            if (prior is not None and prior[0].get("ok") is True and prior[1] == 0
                    and not identity_dimensions.intersection(changed)):
                receipt = {"kind": "already_applied", "request_id": request_id,
                    "request_fingerprint": immutable["request_fingerprint"], "body_sha256": immutable["body_sha256"],
                    "changed_dimensions": list(changed), "result_ref": _bounded_result_ref(prior[0].get("result"))}
                self._set_state(request_id, expected={"checking"}, state="applied",
                    reason="applied_current_conditions_changed", receipt=receipt)
                return PendingReceipt("already_applied", request_id, immutable["request_fingerprint"], changed,
                    "applied_current_conditions_changed", receipt["result_ref"])
            status = "stale" if any(x in changed for x in ("source_fingerprint", "run_revision", "run_state")) else "conflict"
            self._set_state(request_id, expected={"checking"}, state=status, reason="current_facts_changed",
                            receipt={"changed_dimensions": list(changed)})
            return PendingReceipt(status, request_id, immutable["request_fingerprint"], changed,
                                  "current_facts_changed")
        if prior is not None:
            envelope, exit_code = prior
            if envelope.get("ok") is True and exit_code == 0:
                receipt = {"kind": "already_applied", "request_id": request_id,
                           "request_fingerprint": immutable["request_fingerprint"],
                           "body_sha256": immutable["body_sha256"],
                           "result_ref": _bounded_result_ref(envelope.get("result"))}
                self._set_state(request_id, expected={"checking"}, state="applied", reason=None, receipt=receipt)
                return PendingReceipt("already_applied", request_id, immutable["request_fingerprint"],
                                      server_result_ref=receipt["result_ref"])
            self._set_state(request_id, expected={"checking"}, state="conflict", reason="prior_request_failed",
                            receipt={"exit_code": exit_code})
            return PendingReceipt("conflict", request_id, immutable["request_fingerprint"], reason_code="prior_request_failed")
        # The exact original ID and normalized body are used. No replacement ID or altered body is possible here.
        try:
            reply = store.execute(req)
        except Exception as exc:
            return self._uncertain(request_id, immutable, exc)
        if (not isinstance(reply, tuple) or len(reply) != 2 or not isinstance(reply[0], dict)
                or type(reply[1]) is not int or reply[0].get("request_id") != request_id):
            self._set_state(request_id, expected={"checking"}, state="unknown", reason="replay_response_invalid")
            return PendingReceipt("unknown", request_id, immutable["request_fingerprint"], reason_code="replay_response_invalid")
        envelope, exit_code = reply
        if envelope.get("ok") is True and exit_code == 0:
            ref = _bounded_result_ref(envelope.get("result"))
            self._set_state(request_id, expected={"checking"}, state="applied", reason=None,
                receipt={"kind": "submitted", "request_id": request_id,
                         "request_fingerprint": immutable["request_fingerprint"], "body_sha256": immutable["body_sha256"],
                         "result_ref": ref})
            return PendingReceipt("applied", request_id, immutable["request_fingerprint"], server_result_ref=ref)
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        code = error.get("code")
        if error.get("retryable") is True:
            self._set_state(request_id, expected={"checking"}, state="unknown", reason=code or "retryable_result_response",
                            receipt={"exit_code": exit_code, "error_code": code})
            return PendingReceipt("unknown", request_id, immutable["request_fingerprint"], reason_code=code or "retryable_result_response")
        status = "stale" if code and any(term in code for term in ("stale", "revision", "source", "owner")) else "conflict"
        self._set_state(request_id, expected={"checking"}, state=status, reason=code or "result_rejected",
                        receipt={"exit_code": exit_code, "error_code": code})
        return PendingReceipt(status, request_id, immutable["request_fingerprint"], reason_code=code or "result_rejected")

    def _uncertain(self, request_id, immutable, exc):
        code = getattr(exc, "code", None)
        if code in {"request_conflict", "request_owner_mismatch", "scope_forbidden", "unauthenticated",
                    "credential_unavailable", "ownership_conflict", "source_conflict"}:
            state = "conflict"
            reason = code
        else:
            state = "unknown"
            reason = code or "transport_or_read_unknown"
        self._set_state(request_id, expected={"checking"}, state=state, reason=reason)
        return PendingReceipt(state, request_id, immutable["request_fingerprint"], reason_code=reason)

    def _set_state(self, request_id, *, expected, state, reason, receipt=None, attempts_delta=0):
        now = utc_now()
        encoded_receipt = _canonical(receipt) if receipt is not None else None
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT state FROM pending_results WHERE request_id=?", (request_id,)).fetchone()
                if not row or row["state"] not in expected:
                    _fail("pending_state_conflict", "Pending result changed during compare-and-set", 3)
                conn.execute("UPDATE pending_results SET state=?,reason_code=?,receipt_json=?,attempts=attempts+?,updated_at=? WHERE request_id=? AND state=?",
                    (state, reason, encoded_receipt, attempts_delta, now, request_id, row["state"]))
                if conn.total_changes != 1:
                    _fail("pending_state_conflict", "Pending result compare-and-set failed", 3)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def stage_resource(self, *, upload_request_id, scope_id, purpose, source_fingerprint,
                       output_ref, output_reader):
        """Copy bytes for an already-created local result resource; never creates shared metadata offline."""
        upload_request_id = _uuid(upload_request_id, "upload_request_id")
        scope_id = _uuid(scope_id, "scope_id")
        if purpose not in RESOURCE_PURPOSES:
            _fail("pending_resource_purpose_invalid", "Only result/evidence snapshots may be staged", 3)
        source_fingerprint = _sha(source_fingerprint, "source_fingerprint")
        output_ref = _nonempty(output_ref, "output_ref")
        if not callable(output_reader):
            _fail("pending_resource_incomplete", "A local result reader is required", 3)
        try:
            content = output_reader(output_ref)
        except Exception:
            return self._stage_incomplete_resource(upload_request_id, scope_id, purpose, source_fingerprint,
                                                   output_ref, "local_result_unavailable")
        if not isinstance(content, bytes) or not content or len(content) > MAX_STAGED_RESOURCE:
            return self._stage_incomplete_resource(upload_request_id, scope_id, purpose, source_fingerprint,
                                                   output_ref, "local_result_bytes_invalid")
        digest, size = _digest(content), len(content)
        path = self.resource_root / digest
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            _fail("pending_path_invalid", "Staged resource target cannot be a link", 5)
        if path.exists():
            if _file_hash(path) != (digest, size):
                _fail("pending_resource_conflict", "Content-addressed pending file contains different bytes", 5)
        else:
            fd, tmp_name = tempfile.mkstemp(prefix=".pending-", dir=self.resource_root)
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    if _file_hash(path) != (digest, size):
                        _fail("pending_resource_conflict", "Concurrent staged resource has different bytes", 5)
            finally:
                tmp.unlink(missing_ok=True)
        immutable = {"schema": SCHEMA_VERSION, "identity": self.identity, "upload_request_id": upload_request_id,
            "scope_id": scope_id, "purpose": purpose, "source_fingerprint": source_fingerprint,
            "output_ref": output_ref, "sha256": digest, "size": size, "relative_path": "resources/" + digest,
            "initial_state": "staged"}
        self._insert_resource(immutable, "staged", None)
        return PendingResourceRef(upload_request_id, digest, size, scope_id, "staged")

    def _stage_incomplete_resource(self, request_id, scope_id, purpose, source_hash, output_ref, reason):
        immutable = {"schema": SCHEMA_VERSION, "identity": self.identity, "upload_request_id": request_id,
            "scope_id": scope_id, "purpose": purpose, "source_fingerprint": source_hash,
            "output_ref": output_ref, "sha256": None, "size": None, "relative_path": None,
            "initial_state": "pending_incomplete", "initial_reason": reason}
        self._insert_resource(immutable, "pending_incomplete", reason)
        return PendingResourceRef(request_id, "", 0, scope_id, "pending_incomplete")

    def _insert_resource(self, immutable, state, reason):
        raw = _canonical(immutable)
        checksum = _digest(raw.encode())
        now = utc_now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute("SELECT * FROM pending_resources WHERE upload_request_id=?",
                                     (immutable["upload_request_id"],)).fetchone()
                if prior:
                    if self._immutable_row(prior, immutable["upload_request_id"], resource=True) != immutable:
                        _fail("pending_resource_conflict", "Upload request ID already has different bytes or conditions", 3)
                else:
                    conn.execute("INSERT INTO pending_resources(upload_request_id,immutable_json,immutable_sha256,state,reason_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (immutable["upload_request_id"], raw, checksum, state, reason, now, now))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def publish_staged_resource(self, upload_request_id, store, read_current_facts):
        upload_request_id = _uuid(upload_request_id, "upload_request_id")
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM pending_resources WHERE upload_request_id=?", (upload_request_id,)).fetchone()
        if not row:
            _fail("pending_resource_not_found", "Pending resource is unavailable", 2)
        immutable = self._immutable_row(row, upload_request_id, resource=True)
        if immutable.get("identity") != self.identity:
            _fail("pending_owner_mismatch", "Pending resource belongs to another local namespace device or session", 3)
        if row["state"] in {"published", "conflict", "stale", "pending_incomplete"}:
            return self._resource_receipt(row, immutable)
        try:
            facts = self._facts(read_current_facts(immutable))
        except Exception as exc:
            reason = getattr(exc, "code", None) or "current_facts_unavailable"
            if row["state"] in {"staged", "unknown"}:
                self._set_resource_state(upload_request_id, {row["state"]}, "unknown", reason)
            return PendingResourceRef(upload_request_id, immutable.get("sha256") or "",
                immutable.get("size") or 0, immutable["scope_id"], "unknown")
        changed = [key for key, expected in (("namespace_id", self.identity["namespace_id"]),
            ("actor", self.identity["actor"]), ("device_id", self.identity["device_id"]),
            ("environment_id", self.identity["environment_id"]), ("session_id", self.identity["session_id"]),
            ("scope_id", immutable["scope_id"]), ("source_fingerprint", immutable["source_fingerprint"]))
            if facts[key] != expected]
        if changed:
            self._set_resource_state(upload_request_id, {row["state"]}, "stale", "current_facts_changed")
            return PendingResourceRef(upload_request_id, immutable.get("sha256") or "", immutable.get("size") or 0,
                                      immutable["scope_id"], "stale")
        if immutable.get("initial_state") == "pending_incomplete":
            return PendingResourceRef(upload_request_id, "", 0, immutable["scope_id"], "pending_incomplete")
        path = self.root / immutable["relative_path"]
        if not path.resolve(strict=False).is_relative_to(self.root) or path.is_symlink():
            _fail("pending_resource_corrupt", "Pending resource path escaped its owned root", 5)
        content = path.read_bytes()
        if (len(content), _digest(content)) != (immutable["size"], immutable["sha256"]):
            _fail("pending_resource_corrupt", "Pending resource hash or size changed", 5)
        self._set_resource_state(upload_request_id, {row["state"]}, "checking", None, attempts_delta=1)
        try:
            result = store.publish_resource({"request_id": upload_request_id, "scope_id": immutable["scope_id"],
                "purpose": immutable["purpose"], "sha256": immutable["sha256"], "size": immutable["size"]},
                content, session_id=self.identity["session_id"])
        except Exception as exc:
            reason = getattr(exc, "code", None) or "resource_upload_unknown"
            status = "conflict" if reason in {"scope_forbidden", "unauthenticated", "credential_unavailable",
                "resource_request_conflict", "resource_hash_mismatch"} else "unknown"
            self._set_resource_state(upload_request_id, {"checking"}, status, reason)
            return PendingResourceRef(upload_request_id, immutable["sha256"], immutable["size"], immutable["scope_id"], status)
        artifact = result.get("artifact_ref") if isinstance(result, dict) else None
        receipt = result.get("receipt_ref") if isinstance(result, dict) else None
        if (not isinstance(artifact, dict) or artifact.get("scope_id") != immutable["scope_id"]
                or artifact.get("purpose") != immutable["purpose"] or artifact.get("sha256") != immutable["sha256"]
                or artifact.get("size") != immutable["size"] or not isinstance(artifact.get("id"), str)
                or not isinstance(receipt, dict) or receipt.get("request_id") != upload_request_id):
            self._set_resource_state(upload_request_id, {"checking"}, "unknown", "resource_receipt_invalid")
            return PendingResourceRef(upload_request_id, immutable["sha256"], immutable["size"], immutable["scope_id"], "unknown")
        self._set_resource_state(upload_request_id, {"checking"}, "published", None,
                                 artifact_ref=artifact, receipt_ref=receipt)
        return self._resource_receipt_by_id(upload_request_id)

    def _resource_receipt(self, row, immutable):
        ref = json.loads(row["artifact_ref_json"]) if row["artifact_ref_json"] else {}
        return {"upload_request_id": immutable["upload_request_id"], "state": row["state"],
                "sha256": immutable.get("sha256"), "size": immutable.get("size"),
                "scope_id": immutable["scope_id"], "artifact_ref": ref.get("artifact_ref"),
                "receipt_ref": ref.get("receipt_ref"), "reason_code": row["reason_code"]}

    def _resource_receipt_by_id(self, request_id):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM pending_resources WHERE upload_request_id=?", (request_id,)).fetchone()
            immutable = self._immutable_row(row, request_id, resource=True)
            return self._resource_receipt(row, immutable)

    def _set_resource_state(self, request_id, expected, state, reason, *, artifact_ref=None, receipt_ref=None, attempts_delta=0):
        now = utc_now()
        value = _canonical({"artifact_ref": artifact_ref, "receipt_ref": receipt_ref}) if artifact_ref is not None else None
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT state FROM pending_resources WHERE upload_request_id=?", (request_id,)).fetchone()
                if not row or row["state"] not in expected:
                    _fail("pending_state_conflict", "Pending resource changed during compare-and-set", 3)
                conn.execute("UPDATE pending_resources SET state=?,reason_code=?,artifact_ref_json=?,attempts=attempts+?,updated_at=? WHERE upload_request_id=? AND state=?",
                    (state, reason, value, attempts_delta, now, request_id, row["state"]))
                if conn.total_changes != 1:
                    _fail("pending_state_conflict", "Pending resource compare-and-set failed", 3)
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def _bounded_result_ref(result):
    if not isinstance(result, dict):
        return None
    for key in ("run_id", "verification_id", "reuse_ref", "claim_ref", "artifact_id", "result_ref"):
        value = result.get(key)
        if isinstance(value, str) and len(value) <= 200:
            return value
        if isinstance(value, dict) and isinstance(value.get("id"), str) and len(value["id"]) <= 200:
            return value["id"]
    return None


def _file_hash(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
