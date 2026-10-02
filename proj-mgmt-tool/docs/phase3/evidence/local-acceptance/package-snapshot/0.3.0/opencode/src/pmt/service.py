"""Protocol boundary shared by the CLI and future remote storage adapter."""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from importlib import import_module

from . import __version__
from .db import Database
from .errors import PmtError
from .util import canonical_json
from .phase2 import OPERATIONS as PHASE2_OPERATIONS
from .phase3 import OPERATIONS as PHASE3_OPERATIONS
from .host.host_contract import HOST_DATA_OPERATIONS

ALLOWED = {"protocol_version", "operation", "request_id", "actor", "session_id",
           "scope_id", "record_id", "expected_revision", "payload", "context_refs",
           "source", "normalized_event", "correlation_id", "causation_id"}
OPERATIONS = {"setup", "create_scope", "read_context", "save_change", "save_decision",
              "record_event", "claim_task", "release_claim", "recover_claim",
              "finish_task", "register_resource", "record_verification",
              "lookup_verification", "backup", "restore", "diagnose", "get_request_result"}
LIFECYCLE = {"create_scope", "save_change", "save_decision", "record_event", "claim_task",
             "release_claim", "recover_claim", "finish_task"}
READ = {"read_context", "lookup_verification", "get_request_result", "diagnose"}
OPERATIONS |= PHASE2_OPERATIONS
OPERATIONS |= PHASE3_OPERATIONS
OPERATIONS |= HOST_DATA_OPERATIONS
OPERATIONS.add("write_execution_control")
OPERATIONS.add("read_step_batch")


def response(request_id, *, result=None, error=None, warnings=None):
    return {"protocol_version": 1, "request_id": request_id, "ok": error is None,
            "result": result, "error": error, "warnings": warnings or []}


def normalize_request(value):
    if not isinstance(value, dict):
        raise PmtError("invalid_request", "Request must be a JSON object")
    unknown = set(value) - ALLOWED
    if unknown:
        raise PmtError("unknown_fields", "Unsupported request fields", details={"fields": sorted(unknown)})
    req = dict(value)
    if type(req.get("protocol_version")) is not int or req["protocol_version"] != 1:
        raise PmtError("protocol_version_unsupported", "Only protocol_version=1 is supported")
    if req.get("operation") not in OPERATIONS:
        raise PmtError("operation_unsupported", "Unsupported operation")
    try:
        if str(uuid.UUID(req["request_id"])) != req["request_id"]:
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise PmtError("invalid_request_id", "request_id must be a canonical UUID")
    for field in ("actor", "session_id"):
        if not isinstance(req.get(field), str) or not req[field].strip() or len(req[field]) > 200:
            raise PmtError("invalid_identity", f"{field} must be a nonempty string")
    payload = req.setdefault("payload", {})
    if not isinstance(payload, dict):
        raise PmtError("invalid_payload", "payload must be an object")
    req["payload"] = dict(payload)
    # Early convenience payloads use these aliases; the canonical wire form is top-level.
    for field in ("record_id", "scope_id", "expected_revision"):
        if field in req["payload"]:
            if field in req and req[field] != req["payload"][field]:
                raise PmtError("conflicting_fields", f"Conflicting {field} values")
            req[field] = req["payload"].pop(field)
    for field in ("record_id", "scope_id"):
        if field in req:
            try:
                if str(uuid.UUID(req[field])) != req[field]:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise PmtError("invalid_id", f"{field} must be a canonical UUID")
    if "expected_revision" in req and (type(req["expected_revision"]) is not int or req["expected_revision"] < 1):
        raise PmtError("invalid_revision", "expected_revision must be a positive integer")
    req.setdefault("context_refs", [])
    req.setdefault("source", {})
    if not isinstance(req["context_refs"], list) or not isinstance(req["source"], dict):
        raise PmtError("invalid_context", "context_refs must be an array and source an object")
    if "normalized_event" in req and not isinstance(req["normalized_event"], dict):
        raise PmtError("invalid_event", "normalized_event must be an object")
    if len(canonical_json(req).encode("utf-8")) > 1024 * 1024:
        raise PmtError("request_too_large", "Request exceeds 1MiB")
    return req


def _setup(db, conn, req):
    from .util import new_id
    product = req["payload"].get("product", "cli")
    if product not in {"cli", "codex", "claude", "opencode"}:
        raise PmtError("product_unsupported", "Unsupported product")
    key = f"installation:{product}"
    installation_id = db._meta_value(conn, key) or new_id()
    db._put_meta(conn, key, installation_id)
    return {"db_id": db._meta_value(conn, "db_id"), "environment_id": db.environment_id,
            "installation_id": installation_id, "product": product, "core_version": __version__,
            "schema_version": int(db._meta_value(conn, "schema_version")),
            "data_root": str(db.root), "config_root": str(db.config_root),
            "runtime": {"python": os.sys.version.split()[0], "sqlite": sqlite3.sqlite_version},
            "storage_ready": True}


def normalize_lookup_request(value):
    """Lookup ignores declared delivery metadata, without loosening new writes."""
    if not isinstance(value, dict):
        raise PmtError("invalid_request", "Expected lookup request must be an object")
    ignored_delivery = {"received_at", "received_at_utc", "retry_count", "attempt"}
    return normalize_request({key: item for key, item in value.items() if key not in ignored_delivery})


def execute(db: Database, value):
    request_id = value.get("request_id") if isinstance(value, dict) else None
    try:
        req = normalize_request(value)
        op = req["operation"]
        if op == "setup":
            return db.run_request(req, lambda conn, request: _setup(db, conn, request))
        if op == "get_request_result":
            target = req["payload"].get("request_id")
            try:
                found = db.get_request_result(target, actor=req["actor"], session_id=req["session_id"])
            except TypeError:
                raise PmtError("request_lookup_unavailable", "Ownership-aware request lookup is unavailable", 5)
            return response(request_id, result={"found": found is not None, "response": found[0] if found else None,
                                                "exit_code": found[1] if found else None}), 0
        if op in PHASE2_OPERATIONS:
            return import_module("pmt.phase2").execute(db, req)
        if op in PHASE3_OPERATIONS:
            return import_module("pmt.phase3").execute(db, req)
        if op in HOST_DATA_OPERATIONS - PHASE2_OPERATIONS - PHASE3_OPERATIONS - {"record_verification", "lookup_verification"}:
            raise PmtError("host_connection_required", "This storage transfer operation requires a configured Host", 3)
        if op in {"write_execution_control", "read_step_batch"}:
            raise PmtError("host_connection_required", "This control storage operation requires a configured Host", 3)
        if op in LIFECYCLE:
            handler = import_module("pmt.lifecycle").handle
            return db.run_request(req, lambda conn, request: handler(db, conn, request))
        if op in {"read_context", "record_verification", "lookup_verification"}:
            module = import_module("pmt.queries" if op == "read_context" else "pmt.verification")
            if op in READ:
                with closing(db.connect()) as conn:
                    result = module.handle(db, conn, req)
                return response(request_id, result=result), 0
            return db.run_request(req, lambda conn, request: module.handle(db, conn, request))
        module = import_module("pmt.resources")
        if op in {"register_resource", "backup", "restore"}:
            return module.execute(db, req)
        with closing(db.connect()) as conn:
            result = module.handle(db, conn, req)
        return response(request_id, result=result), 0
    except PmtError as error:
        return response(request_id, error=error.as_dict()), error.exit_code
    except sqlite3.OperationalError as error:
        issue = db._sqlite_error(error)
        return response(request_id, error=issue.as_dict()), issue.exit_code
