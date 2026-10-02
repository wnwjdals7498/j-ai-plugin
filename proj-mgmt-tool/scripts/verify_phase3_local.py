"""F10 local preflight/baselines and F15 isolated acceptance runner.

F10 fixture scenarios and F15 acceptance checks remain separately labelled.
The F15 runner uses only isolated package/Host fixtures and never selects a
user ConfigRoot, DataRoot, checkout or production endpoint.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
import platform
import re
import sqlite3
import sys
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from pmt.efficiency.measurement import (
    MeasurementError, capture_baseline, compare, fingerprint, time_call,
)
from pmt.store import LocalStore
from pmt.planning.graph import render_docs, validate_graph
from pmt.util import canonical_json, utc_now


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "docs/phase3/evidence/2026-10-02/local-integration/scenario-catalog.json"
EVIDENCE = CATALOG.parent
ACCEPTANCE_EVIDENCE = ROOT / "docs/phase3/evidence/local-acceptance"
TRACEABILITY_SOURCES = [
    "docs/phase3/verification.md", "docs/phase3/contracts.md", "docs/phase3/spec-graph-document.md",
    "docs/phase3/spec-context-execution.md", "docs/phase3/spec-local-measurement.md",
    "docs/phase3/spec-host-integration.md", "docs/phase3/implementation-status.md",
    "docs/phase3/implementation-plan.md", "docs/phase3/plan-graph-document.md",
    "docs/phase3/plan-context-execution.md", "docs/phase3/plan-host-integration.md",
    "docs/phase3/traceability.md", "docs/phase3/evidence/2026-10-02/local-integration/README.md",
    "docs/phase3/evidence/local-acceptance/README.md",
    "pyproject.toml", "scripts/build_plugins.py", "scripts/verify_phase3_local.py",
    "src/pmt/db.py", "src/pmt/phase2_schema.py", "src/pmt/phase3_schema.py", "src/pmt/phase2.py",
    "src/pmt/phase2_common.py", "src/pmt/service.py", "src/pmt/store.py", "src/pmt/http_store.py",
    "src/pmt/efficiency/source.py", "src/pmt/efficiency/storage.py", "src/pmt/efficiency/graph.py",
    "src/pmt/efficiency/documents.py", "src/pmt/efficiency/context.py", "src/pmt/efficiency/reuse.py",
    "src/pmt/efficiency/results.py", "src/pmt/efficiency/control.py", "src/pmt/efficiency/batch.py",
    "src/pmt/efficiency/measurement.py", "src/pmt/efficiency/measurement_ops.py",
    "src/pmt/execution/service.py", "src/pmt/runners/service.py", "src/pmt/runners/supervisor.py",
    "src/pmt/hosted_runtime.py", "src/pmt/hosted_files.py", "src/pmt/pending.py",
    "src/pmt/routing/client_config.py", "src/pmt/migration.py", "src/pmt/storage_config.py", "src/pmt/cli.py",
    "src/pmt/host/application.py", "src/pmt/host/auth.py", "src/pmt/host/control_state.py",
    "src/pmt/host/data.py", "src/pmt/host/resources.py", "src/pmt/host/transfer.py",
    "src/pmt/host/file_effects.py", "src/pmt/host/plans.py", "src/pmt/host/server.py",
    "tests/test_phase3_storage_foundation.py", "tests/test_phase3_protocol.py", "tests/test_phase3_http_store.py",
    "tests/test_phase3_measurement.py", "tests/test_phase3_graph.py", "tests/test_phase3_documents.py",
    "tests/test_phase3_context.py", "tests/test_phase3_reuse.py", "tests/test_phase3_results.py",
    "tests/test_phase3_control.py", "tests/test_phase3_batch.py", "tests/test_phase3_local_integration.py",
    "tests/test_phase3_host_application.py", "tests/test_phase3_host_auth.py",
    "tests/test_phase3_host_control_state.py",
    "tests/test_phase3_host_data.py", "tests/test_phase3_host_resources.py",
    "tests/test_phase3_host_network.py", "tests/test_storage_config.py",
    "tests/test_phase3_migration.py", "tests/test_phase3_transfer.py", "tests/test_phase3_transfer_http.py",
    "tests/test_phase3_pending.py", "tests/test_phase3_pending_http.py",
    "tests/test_phase3_hosted_runtime.py", "tests/test_phase3_hosted_cli.py",
    "tests/test_phase3_hosted_files.py", "tests/test_phase3_host_batch_state.py",
    "tests/test_phase3_hosted_planning.py", "tests/test_phase3_source_metadata.py",
    "tests/test_phase3_acceptance.py", "tests/test_phase3_quality_projection.py",
]
TRACE_ALLOWED = {
    "event_kind", "operation", "request_id", "exit_code", "elapsed_ms",
    "input_bytes", "output_bytes", "source_hash", "artifact_hashes",
    "evidence_refs", "work_id", "item_id", "step_id", "job_id", "run_id",
    "nonce", "context_id", "reuse_id", "result_id", "receipt_id", "status",
    "source_hash", "expected_source_hash", "event_refs", "graph_hash",
    "projection_hash", "manifest_hash", "artifact_id", "batch_id", "change_id",
}
TRACE_FORBIDDEN = {"content", "raw", "prompt", "transcript", "stdout", "stderr",
                   "environment_values", "secret", "token_value", "directive_text"}
STAGE_REQUIREMENTS = {
    "F1-F4 source and documents": {
        "capture_source_pin", "preview_graph_change", "apply_graph_change",
        "calculate_graph_impact", "query_graph", "rebuild_graph_index", "register_segment_manifest",
        "prepare_document_segments",
        "publish_document_segments", "recover_document_segments",
    },
    "F5 context": {"build_task_context", "read_context_detail", "resolve_context_alias",
                    "resume_task_context", "read_task_context"},
    "F6 reuse": {"resolve_reuse", "record_reuse_result", "invalidate_reuse", "read_reuse_decision"},
    "F7 results": {"compact_tool_result", "read_tool_result_detail"},
    "F8 control": {"advance_execution_control", "read_execution_control",
                    "acknowledge_execution_action"},
    "F9 batch": {"prepare_step_batch", "bind_step_batch", "collect_step_batch"},
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_catalog(path: Path = CATALOG) -> dict[str, Any]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or catalog.get("kind") != "phase3-local-scenario-catalog":
        raise ValueError("unsupported F10 scenario catalog")
    cases = catalog.get("cases")
    if not isinstance(cases, list) or len(cases) != 6:
        raise ValueError("F10 catalog must retain the six frozen representative cases")
    ids = [case.get("id") for case in cases]
    if len(set(ids)) != 6 or any(not isinstance(value, str) for value in ids):
        raise ValueError("F10 case IDs must be six unique strings")
    fixture = catalog["common"]["source_fixture"]
    source_path = ROOT / fixture["path"]
    if _sha256(source_path.read_bytes()) != fixture["sha256"]:
        raise ValueError("frozen canonical graph hash changed")
    allowed_status = {"measured_fixture", "capture_legacy_fixture", "not_comparable"}
    if any(case.get("baseline_status") not in allowed_status for case in cases):
        raise ValueError("scenario baseline status is missing or unsupported")
    if any(case.get("baseline_status") == "not_comparable" and not case.get("reason") for case in cases):
        raise ValueError("not_comparable cases must preserve a concrete reason")
    full_case = next(item for item in cases if item["id"] == "new-plan-full-graph-render")
    baseline_path = (path.parent / full_case["baseline_ref"]).resolve()
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_body = {key: value for key, value in baseline.items() if key != "manifest_fingerprint"}
    if (fingerprint(baseline_body) != baseline.get("manifest_fingerprint")
            or baseline.get("condition_fingerprint") != full_case.get("baseline_condition_fingerprint")
            or baseline.get("case_fingerprint") != full_case.get("baseline_case_fingerprint")):
        raise ValueError("preserved full-plan baseline fingerprints changed")
    return catalog


def _operation_capability(operation: str, module_name: str | None) -> dict[str, Any]:
    if module_name is None or importlib.util.find_spec(module_name) is None:
        return {"operation": operation, "state": "module_unavailable"}
    module = importlib.import_module(module_name)
    mode = next((name for name in ("READ_OPERATIONS", "WRITE_OPERATIONS", "FILE_OPERATIONS")
                 if operation in getattr(module, name, set())), None)
    if mode is None:
        return {"operation": operation, "state": "registered_without_runtime_handler"}
    return {"operation": operation, "state": "dispatchable", "mode": mode.removesuffix("_OPERATIONS").lower()}


def preflight() -> dict[str, Any]:
    from pmt.phase3 import MODULES

    by_operation = {op: name for name, operations in MODULES.items() for op in operations}
    groups = {}
    for stage, operations in STAGE_REQUIREMENTS.items():
        checks = [_operation_capability(op, by_operation.get(op)) for op in sorted(operations)]
        groups[stage] = {"state": "ready" if all(x["state"] == "dispatchable" for x in checks)
                         else "pending", "operations": checks}
    runtime = importlib.import_module("pmt.efficiency.local_runtime")
    groups["local runner adapter"] = {"state": "adapter_present" if hasattr(runtime, "LocalExecutionRuntime")
                                      else "pending"}
    integration_ready = all(groups[key]["state"] == "ready" for key in STAGE_REQUIREMENTS)
    return {"schema_version": 1, "kind": "phase3-local-preflight", "scenarios": [
                {"id": case["id"], "baseline_status": case["baseline_status"],
                 "baseline_ref": case.get("baseline_ref"), "reason": case.get("reason")}
                for case in load_catalog()["cases"]],
            "capabilities": groups,
            "integrated_run_state": "ready_for_explicit_root_go" if integration_ready else "pending_runtime_capability",
            "actual_scenario_run_performed": False}


def _safe_ids(value: Any, *, limit: int = 40) -> dict[str, str]:
    """Copy only known identifier/hash leaves; never retain arbitrary response bodies."""
    allowed = {"work_id", "item_id", "step_id", "job_id", "run_id", "nonce",
               "context_id", "reuse_id", "result_id", "receipt_id", "resource_id",
               "artifact_id", "batch_id", "change_id", "journal_id", "handle_id",
               "source_hash", "graph_hash", "projection_hash", "manifest_hash"}
    found: dict[str, str] = {}
    def walk(item: Any, depth: int = 0) -> None:
        if depth > 8 or len(found) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if key in allowed and isinstance(child, str) and child:
                    found.setdefault(key, child[:160])
                elif isinstance(child, (dict, list)):
                    walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item[:50]:
                walk(child, depth + 1)
    walk(value)
    return found


@contextmanager
def measure_localstore_calls(trace: list[dict[str, Any]]):
    """Measure every actual LocalStore protocol request, including nested F8 calls."""
    original = LocalStore.execute
    def measured(store, request):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        operation = request.get("operation") if isinstance(request, dict) else None
        started = time.perf_counter_ns()
        input_size = len(canonical_json(request).encode("utf-8"))
        envelope, code = original(store, request)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        output_size = len(canonical_json(envelope).encode("utf-8"))
        payload = request.get("payload", {}) if isinstance(request, dict) else {}
        expected = payload.get("expected_source") if isinstance(payload, dict) else None
        expected_hash = expected.get("source_hash") if isinstance(expected, dict) else None
        actual_ids = _safe_ids(envelope)
        row = {"event_kind": "call", "operation": operation or "unknown",
               "request_id": request_id or "", "exit_code": code,
               "elapsed_ms": elapsed, "input_bytes": input_size, "output_bytes": output_size,
               "status": "ok" if code == 0 and isinstance(envelope, dict) and envelope.get("ok") else
                        (envelope.get("error") or {}).get("code", "failed") if isinstance(envelope, dict) else "failed",
               "evidence_refs": []}
        if isinstance(expected_hash, str) and len(expected_hash) == 64:
            row["expected_source_hash"] = expected_hash
        for key, value in actual_ids.items():
            if key in TRACE_ALLOWED:
                row[key] = value
        trace.append(row)
        return envelope, code
    LocalStore.execute = measured
    try:
        yield
    finally:
        LocalStore.execute = original


def protocol_request(env: dict[str, Any], operation: str, payload: dict[str, Any], *, request_id: str | None = None):
    scope = env.get("project_id", env.get("scope_id"))
    actor, session = env.get("actor"), env.get("session")
    if not all(isinstance(item, str) and item for item in (scope, actor, session)):
        raise ValueError("scenario environment requires project_id/scope_id, actor, and session")
    value = {"protocol_version": 1, "operation": operation,
             "request_id": request_id or str(uuid.uuid4()), "actor": actor,
             "session_id": session, "scope_id": scope,
             "source": {"product": env.get("product", "cli")},
             "payload": dict(payload)}
    if operation in {"capture_source_pin", "preview_graph_change", "apply_graph_change",
                     "recover_graph_change", "query_graph", "rebuild_graph_index",
                     "calculate_graph_impact", "register_segment_manifest",
                     "prepare_document_segments", "publish_document_segments",
                     "recover_document_segments", "build_task_context", "resume_task_context",
                     "resolve_reuse", "invalidate_reuse"}:
        value["payload"] = {"repository_id": env.get("repo_id", env.get("repository_id")),
                            "workspace": str(env["workspace"]),
                            "relative_graph_path": env.get("graph_path", "docs/pmt-docs/plan.graph.json"),
                            "run_id": env.get("run_id", env.get("run")), **value["payload"]}
    return value


def call_public(env: dict[str, Any], operation: str, *, request_id: str | None = None,
                **payload: Any) -> tuple[dict[str, Any], int]:
    request = protocol_request(env, operation, payload, request_id=request_id)
    return LocalStore(env["db"]).execute(request)


def mark_cost(trace: list[dict[str, Any]], kind: str, label: str, *, request_id: str | None = None,
              source_hash: str | None = None, refs: list[str] | None = None) -> None:
    if kind not in {"retry", "rework", "review"}:
        raise ValueError("cost event must be retry, rework, or review")
    row = {"event_kind": kind, "operation": label,
           "request_id": request_id or str(uuid.uuid4()), "exit_code": 0,
           "elapsed_ms": 0, "input_bytes": 0, "output_bytes": 0,
           "status": "observed", "evidence_refs": refs or []}
    if source_hash:
        row["source_hash"] = source_hash
    trace.append(validate_trace_record(row))


def f9_batch_payloads(*, run_refs: list[dict[str, Any]], workspace: str,
                      repository_id: str, relative_graph_path: str,
                      expected_source: dict[str, Any], context_budget: dict[str, Any],
                      prepare_event_id: str, batch_ref: str | None = None,
                      parent_run_id: str | None = None,
                      expected_run_revision: int | None = None,
                      collect_event_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Build current public F9 requests; physical handle binding is an internal P2/F8 hook."""
    if len(run_refs) < 2 or any(set(item) != {"run_id", "expected_run_revision"} for item in run_refs):
        raise ValueError("F9 prepare requires at least two actual run/revision refs")
    payloads = {"prepare_step_batch": {
        "run_refs": run_refs, "workspace": workspace, "repository_id": repository_id,
        "relative_graph_path": relative_graph_path, "expected_source": expected_source,
        "context_budget": context_budget, "event_id": prepare_event_id}}
    if batch_ref is not None:
        if not parent_run_id or type(expected_run_revision) is not int or not collect_event_id:
            raise ValueError("F9 collect requires current parent run revision and event_id")
        payloads["collect_step_batch"] = {"batch_ref": batch_ref, "parent_run_id": parent_run_id,
                                          "expected_run_revision": expected_run_revision,
                                          "event_id": collect_event_id}
    return payloads


def prepare_f9_batch_public(env: dict[str, Any], *, run_refs: list[dict[str, Any]],
                            expected_source: dict[str, Any],
                            context_budget: dict[str, Any], event_id: str) -> dict[str, Any]:
    payloads = f9_batch_payloads(run_refs=run_refs, workspace=str(env["workspace"]),
        repository_id=env.get("repo_id", env.get("repository_id")),
        relative_graph_path=env.get("graph_path", "docs/pmt-docs/plan.graph.json"),
        expected_source=expected_source, context_budget=context_budget, prepare_event_id=event_id)
    return call_f9_public(env, "prepare_step_batch", payloads["prepare_step_batch"])


def collect_f9_batch_public(env: dict[str, Any], *, batch_ref: str, parent_run_id: str,
                            expected_run_revision: int, event_id: str) -> dict[str, Any]:
    # The service reloads the parent's stopped result and verifies its batch report resource/hash.
    return call_f9_public(env, "collect_step_batch",
        {"batch_ref": batch_ref, "parent_run_id": parent_run_id,
         "expected_run_revision": expected_run_revision, "event_id": event_id})


def run_f9_prepare_public(env: dict[str, Any], *, run_refs: list[dict[str, Any]],
                          source_pin: dict[str, Any], context_budget: dict[str, Any],
                          prepare_event_id: str) -> dict[str, Any]:
    """Prepare an actual group; F8/P2 attaches and binds the physical handle later."""
    expected_fields = {"run_id", "expected_run_revision"}
    if len(run_refs) < 2 or any(set(item) != expected_fields for item in run_refs):
        raise ValueError("F9 needs at least two current P2 run refs")
    prepared = prepare_f9_batch_public(env, run_refs=run_refs, expected_source=source_pin,
                                       context_budget=context_budget, event_id=prepare_event_id)
    if prepared.get("state") != "completed":
        return {"status": prepared.get("state"), "stage": "prepare",
                "reason": prepared.get("error_code", prepared.get("reason"))}
    prep_result = prepared.get("result") or {}
    reference = prep_result.get("batch_ref")
    batch_id = reference.get("id") if isinstance(reference, dict) else reference
    if not isinstance(batch_id, str) or prep_result.get("status") != "prepared":
        return {"status": "unknown", "stage": "prepare",
                "reason": "F9 returned no verified prepared batch ref"}
    return {"status": "prepared", "batch_ref": reference, "batch_id": batch_id,
            "parent_run_ref": prep_result.get("parent_run_ref"),
            "member_run_refs": prep_result.get("member_run_refs"),
            "scope_union_sha256": prep_result.get("scope_union_sha256"),
            "physical_slots": prep_result.get("physical_slots"), "request_id": prepared.get("request_id")}


def run_f9_collect_public(env: dict[str, Any], *, batch_id: str, parent_run_id: str,
                          stopped_parent_revision: int, event_id: str) -> dict[str, Any]:
    """Collect only after an actual parent receipt carries its verified batch report ref/hash."""
    collected = collect_f9_batch_public(env, batch_ref=batch_id, parent_run_id=parent_run_id,
        expected_run_revision=stopped_parent_revision, event_id=event_id)
    result = collected.get("result") or {}
    return {"status": collected.get("state"), "collection_status": result.get("status"),
            "parent_done": result.get("parent_done"), "physical_slots": result.get("physical_slots"),
            "child_count": len(result.get("children", [])) if isinstance(result.get("children"), list) else None,
            "error_code": collected.get("error_code"), "request_id": collected.get("request_id")}


def call_f9_public(env: dict[str, Any], operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run only producer-registered F9 operations; absence stays pending, never simulated."""
    preflight_group = preflight()["capabilities"].get("F9 batch", {})
    if preflight_group.get("state") != "ready":
        return {"state": "pending", "reason": "F9 runtime handler is not registered",
                "operation": operation}
    if operation not in {"prepare_step_batch", "bind_step_batch", "collect_step_batch"}:
        raise ValueError("unsupported F9 public operation")
    envelope, code = call_public(env, operation, **payload)
    if code != 0 or not isinstance(envelope, dict) or not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope, dict) else {}
        return {"state": "failed", "exit_code": code,
                "error_code": (error or {}).get("code", "batch_operation_failed")}
    return {"state": "completed", "request_id": envelope.get("request_id"),
            "result": envelope.get("result")}


def f9_status_for_fixture(env: dict[str, Any]) -> dict[str, Any]:
    capability = preflight()["capabilities"].get("F9 batch", {})
    if capability.get("state") != "ready":
        return {"state": "pending", "reason": "F9 operation handler unavailable"}
    refs = env.get("f9_run_refs")
    if not isinstance(refs, list) or len(refs) < 2:
        return {"state": "not_run",
                "reason": "isolated fixture has no two current same-owner queued/starting child runs"}
    return {"state": "not_run",
            "reason": "F9 binding/collection waits for an actual grouped runner report resource",
            "member_count": len(refs)}


def compare_only_on_identical_conditions(baseline_path: Path, measured: dict[str, Any]) -> dict[str, Any]:
    """A baseline mismatch is an explicit non-comparison, not a cost delta."""
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    try:
        return compare(baseline, measured)
    except MeasurementError as exc:
        return {"kind": "comparison", "comparable": False,
                "reason": str(exc).split(":", 1)[0], "baseline_ref": str(baseline_path)}


def _result(env: dict[str, Any], operation: str, **payload: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    envelope, code = call_public(env, operation, **payload)
    if code != 0 or not isinstance(envelope, dict) or not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope, dict) else {}
        issue = error if isinstance(error, dict) else {}
        raise RuntimeError(f"{operation}:{issue.get('code', 'call_failed')}:{code}")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{operation}:result_unavailable")
    return envelope, result


def run_f1_f4_public(env: dict[str, Any], *, premise: str, partial: bool = True) -> dict[str, Any]:
    """Run full baseline -> one typed delta/impact/apply -> partial document publish."""
    if not isinstance(premise, str) or not premise.strip():
        raise ValueError("scenario premise patch is required")
    baseline_pin_envelope, pin_result = _result(env, "capture_source_pin")
    pin = pin_result["source_pin"]
    _result(env, "rebuild_graph_index", expected_source=pin)
    baseline_prepared, baseline_preparation = _result(env, "prepare_document_segments",
                                                       expected_source=pin)
    baseline_published_envelope, baseline_published = _result(
        env, "publish_document_segments", expected_source=pin,
        journal_id=baseline_preparation["journal_id"])
    if baseline_published.get("coverage", {}).get("segments") != "complete":
        raise RuntimeError("F4 full baseline did not certify complete segment coverage")
    if not partial:
        return {"before_source_pin": pin, "source_pin": pin,
                "baseline_prepare": baseline_preparation, "baseline_publish": baseline_published,
                "change_set": None, "preview": None, "impact": None, "apply_envelope": None,
                "apply_receipt": None, "partial_prepare": None, "partial_publish": None,
                "baseline_request_id": baseline_pin_envelope["request_id"],
                "partial_request_id": None, "incomplete_impact": False}

    change_set = {"change_id": str(uuid.uuid4()), "reason": "Frozen F10 one-field partial-change fixture",
                  "evidence_refs": [], "changes": [{
                      "op": "update", "id": env["node_id"],
                      "fields": {"premise": premise}}]}
    _preview_envelope, preview = _result(env, "preview_graph_change",
                                         expected_source=pin, change_set=change_set)
    _impact_envelope, impact = _result(env, "calculate_graph_impact",
                                       expected_source=pin, change_preview=preview,
                                       change_set=change_set)
    apply_envelope, applied = _result(env, "apply_graph_change",
                                      expected_source=pin, change_set=change_set)
    new_pin = applied["source_pin"]
    partial_prepared_envelope, partial_preparation = _result(env, "prepare_document_segments",
        expected_source=new_pin, impact_set=impact, apply_receipt=apply_envelope,
        change_set=change_set, change_preview=preview)
    partial_published_envelope, partial_published = _result(
        env, "publish_document_segments", expected_source=new_pin,
        journal_id=partial_preparation["journal_id"])
    if partial_published.get("source_pin", {}).get("source_hash") != new_pin.get("source_hash"):
        raise RuntimeError("F4 partial publish receipt does not match the applied source pin")
    return {"before_source_pin": pin, "source_pin": new_pin,
            "baseline_prepare": baseline_preparation, "baseline_publish": baseline_published,
            "change_set": change_set, "preview": preview, "impact": impact,
            "apply_envelope": apply_envelope, "apply_receipt": applied,
            "partial_prepare": partial_preparation, "partial_publish": partial_published,
            "baseline_request_id": baseline_pin_envelope["request_id"],
            "partial_request_id": partial_published_envelope["request_id"],
            "incomplete_impact": impact.get("complete") is not True}


def run_f5_public(env: dict[str, Any], source_pin: dict[str, Any], *,
                  role: str = "lower", small_budget: bool = False,
                  resume_from: str | None = None) -> dict[str, Any]:
    max_bytes = 500 if small_budget else 128_000
    max_lines = 8 if small_budget else 2_000
    payload = {"task_ref": {"task_id": env["item_id"], "step_id": env["step_id"],
                            "run_id": env["run_id"]},
               "expected_source": source_pin, "role": role,
               "repository_id": env["repo_id"], "workspace": str(env["workspace"]),
               "relative_graph_path": env["graph_path"], "run_id": env["run_id"],
               "node_ids": env.get("context_node_ids", [env["node_id"]]),
               "budget": {"max_bytes": max_bytes, "max_lines": max_lines, "unit": "utf8"}}
    if resume_from is not None:
        operation = "resume_task_context"
        payload["previous_context_id"] = resume_from
    else:
        operation = "build_task_context"
    envelope, built = _result(env, operation, **payload)
    ref = built["context_ref"]
    # Verify the exact current projection ref through F8's dedicated public reader.
    _, metadata = _result(env, "read_task_context", context_ref=ref)
    alias = None
    if not small_budget:
        _, alias = _result(env, "resolve_context_alias", context_id=ref["id"],
                           alias="N0001", mapping_version=1)
    details, cursor = [], built.get("detail_cursor")
    for _ in range(100):
        if not cursor:
            break
        _, page = _result(env, "read_context_detail", context_id=ref["id"], cursor=cursor,
                          max_bytes=65_536, max_lines=500)
        details.append({key: page.get(key) for key in ("start_byte", "end_byte", "content_sha256")})
        cursor = page.get("next_cursor")
    if cursor:
        raise RuntimeError("F5 detail cursor exceeded the bounded F10 page limit")
    omitted = bool(metadata.get("incomplete") or metadata.get("mandatory_omissions"))
    return {"context_ref": ref, "projection_hash": ref["projection_hash"],
            "source_hash": ref["source_hash"], "budget": metadata.get("budget"),
            "incomplete": omitted, "mandatory_omissions": metadata.get("mandatory_omissions", []),
            "unknown": [{"kind": item.get("kind"), "reason_code": item.get("reason_code"),
                         "severity": item.get("severity")}
                        for item in metadata.get("unknown", []) if isinstance(item, dict)],
            "section_ids": [item.get("section_id") for item in
                            (metadata.get("projection", {}).get("included") or [])
                            if isinstance(item, dict)],
            "unknown_count": len(metadata.get("unknown", [])), "alias": alias,
            "detail_pages": details, "resume": built.get("resume"),
            "request_id": envelope["request_id"]}


def run_f2_public_context_slice(env: dict[str, Any], source_pin: dict[str, Any],
                                node_ids: list[str]) -> dict[str, Any]:
    from pmt.efficiency.context import graph_query_for_context
    query = graph_query_for_context("lower", node_ids=node_ids, page_size=100)
    _, result = _result(env, "query_graph", expected_source=source_pin, query=query)
    body = result.get("graph_slice") or {}
    items = body.get("items", [])
    index = body.get("index") or {}
    with closing(env["db"].connect()) as conn:
        index_row = conn.execute("SELECT body_json FROM phase3_objects WHERE kind='graph_index' AND id=?",
                                 (env.get("project_id", env.get("scope_id")),)).fetchone()
    index_body = json.loads(index_row["body_json"]) if index_row else {}
    return {"query": query, "index_hash": index.get("hash"),
            "index_revision": index.get("revision"),
            "index_node_count": len(index_body.get("nodes", {})),
            "index_relation_count": len(index_body.get("relations", {})),
            "node_count": sum(item.get("entity") == "node" for item in items),
            "relation_count": sum(item.get("entity") == "relation" for item in items),
            "node_ids": sorted(item.get("value", {}).get("id") for item in items
                               if item.get("entity") == "node"),
            "relation_ids": sorted(item.get("value", {}).get("id") for item in items
                                   if item.get("entity") == "relation"),
            "unknown": [{"kind": item.get("kind"), "reason_code": item.get("reason_code"),
                         "node_id": item.get("node_id"), "node_ids": item.get("node_ids"),
                         "relation_id": item.get("relation_id")}
                        for item in body.get("unknown", []) if isinstance(item, dict)],
            "traversal_complete": body.get("traversal_complete"),
            "source_hash": body.get("source_pin", {}).get("source_hash")}


def _reuse_definition(graph_path: str) -> dict[str, Any]:
    return {"definition_id": "f10.local_compile_fixture", "definition_version": "1",
            "meaning_sha256": hashlib.sha256(b"F10 deterministic local compile fixture").hexdigest(),
            "key_schema_version": 1, "required_dimensions": ["input", "environment", "source"],
            "model_is_subject": False,
            "selectors": {"input": {"version": 1, "kind": "snapshot_inputs"},
                          "environment": {"version": 1, "kind": "environment_id"},
                          "source": {"version": 1, "kind": "workspace_files", "paths": [graph_path]}},
            "applicability": {"field_dimensions": {}, "unknown_reason_dimensions": {}}}


def run_f6_public(env: dict[str, Any], source_pin: dict[str, Any]) -> dict[str, Any]:
    definition = _reuse_definition(env["graph_path"])
    common = {"definition": definition, "run_id": env["run_id"],
              "workspace": str(env["workspace"]), "paths": [env["graph_path"]],
              "target_id": env["item_id"], "command": ["fixture", "verify"],
              "inputs": {"source_hash": source_pin["source_hash"]},
              "repository_id": env["repo_id"], "relative_graph_path": env["graph_path"],
              "expected_source": source_pin}
    event_id = str(uuid.uuid4())
    first_envelope, first = _result(env, "resolve_reuse", **common, event_id=event_id)
    second_envelope, second = _result(env, "resolve_reuse", **common, event_id=str(uuid.uuid4()))
    if first.get("status") != "claimed" or second.get("status") != "active":
        raise RuntimeError("F6 identical-key duplicate did not preserve one active claim")
    _, read = _result(env, "read_reuse_decision", body_ref=first["body_ref"],
                      run_id=env["run_id"], workspace=str(env["workspace"]),
                      paths=[env["graph_path"]])
    return {"first": {key: first.get(key) for key in ("status", "key_sha256", "body_ref", "reuse_ref")},
            "second": {key: second.get(key) for key in ("status", "key_sha256", "reuse_ref")},
            "read_status": read.get("status"), "event_ids": [event_id],
            "decision_ref": first["body_ref"],
            "request_ids": [first_envelope["request_id"], second_envelope["request_id"]]}


def run_f7_public(env: dict[str, Any], *, run_id: str, raw_output: str,
                  reported_status: str = "failed", exit_code: int | None = 1) -> tuple[dict[str, Any], str]:
    request_id = str(uuid.uuid4())
    envelope, compact = _result(env, "compact_tool_result", request_id=request_id,
        task_id=env["item_id"], run_id=run_id, step_id=env["step_id"],
        status=reported_status, exit_code=exit_code, format="text",
        output=raw_output, criteria_claims=[{"criterion_id": "fixture-claim", "outcome": "pass"}])
    pages, contents, cursor = [], [], compact.get("detail_cursor")
    for _ in range(100):
        if not cursor:
            break
        _, page = _result(env, "read_tool_result_detail",
            result_id=compact["result_id"], scope_id=env["project_id"], cursor=cursor,
            max_bytes=128, max_lines=3)
        pages.append({key: page.get(key) for key in ("start_byte", "end_byte", "content_sha256")})
        contents.append(page.get("content", ""))
        cursor = page.get("next_cursor")
    if cursor:
        raise RuntimeError("F7 result cursor exceeded the bounded F10 page limit")
    sanitized = "".join(contents)
    return {"result_id": compact["result_id"], "status": compact.get("status"),
            "reported_status": compact.get("reported_status"), "exit_code": compact.get("exit_code"),
            "criteria_verdict": compact.get("criteria_verdict"),
            "evidence_ref": compact.get("evidence_ref"),
            "detail_pages": pages, "request_id": envelope["request_id"],
            "raw_input_bytes": len(raw_output.encode("utf-8")),
            "sanitized_output_bytes": len(sanitized.encode("utf-8")),
            "sanitized_output_sha256": _sha256(sanitized.encode("utf-8")),
            "failure_line_count": sum("Traceback" in line or "RuntimeError" in line or "fixture.py" in line
                                      for line in sanitized.splitlines()),
            "redaction_verified": ("sk_f10fixtureSecret123456" not in sanitized
                                   and "[REDACTED]" in sanitized)}, sanitized


def _make_native_run_starting(env: dict[str, Any], *, mode: str = "native") -> None:
    """Fixture precondition only; the public F8 operation owns subsequent transitions."""
    from pmt.execution.service import _normalize_scopes
    db = env["db"]
    now = utc_now()
    with db.write() as conn:
        row = conn.execute("SELECT job_id,intent_json,scopes_json FROM execution_runs WHERE id=?",
                           (env["run_id"],)).fetchone()
        if not row:
            raise ValueError("F8 fixture run is missing")
        intent = json.loads(row["intent_json"])
        scopes = _normalize_scopes(json.loads(row["scopes_json"]), str(env["workspace"]))
        intent.update(job_id=row["job_id"], step_id=env["step_id"], workspace=str(env["workspace"]),
                      scopes=scopes, dependencies=[])
        route = {"agent": "cli", "provider": "fixture-local", "model": "local-fixture",
                 "mode": mode, "selection_reason": "F10 deterministic local fixture",
                 "auth_state": "authenticated",
                 "actual_support": "verified_supported", "capability_ref": "f10-fixture-native",
                 "max_concurrency": 1}
        env["route"] = route
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json({"workspace": str(env["workspace"]), "criteria": []}), env["item_id"]))
        conn.execute("UPDATE execution_runs SET state='starting',revision=revision+1,route_json=?,scopes_json=?,intent_json=?,updated_at=? WHERE id=?",
                     (canonical_json(route), canonical_json(scopes), canonical_json(intent), now, env["run_id"]))
        conn.execute("UPDATE execution_jobs SET state='starting',updated_at=? WHERE id=?",
                     (now, row["job_id"]))


def run_f8_native_fixture(env: dict[str, Any], context_ref: dict[str, Any],
                          reuse_ref: dict[str, Any]) -> dict[str, Any]:
    _make_native_run_starting(env)
    first_envelope, first = _result(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context_ref, reuse_body_ref=reuse_ref)
    action = first.get("action", {})
    if action.get("kind") != "main-native-call":
        raise RuntimeError("F8 did not return a native action for the verified fixture run: "
                           + canonical_json({"action": action, "run_state": first.get("run_state"),
                                            "locks_retained": first.get("locks_retained")}))
    handle_ref = {"kind": "native_handle", "id": "f10-native-fixture-handle",
                  "provider_ref": "fixture-provider-reference"}
    ack_envelope, ack = _result(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"],
        outcome="started", handle_ref=handle_ref)
    if ack.get("run_state") != "running":
        raise RuntimeError("F8 fixture handle was not acknowledged by the public API")
    return {"action_kind": action["kind"], "action_nonce": action["action_nonce"],
            "control_ref": ack.get("control_ref"), "handle_ref": handle_ref,
            "run_state": ack.get("run_state"),
            "request_ids": [first_envelope["request_id"], ack_envelope["request_id"]],
            "evidence_tier": "local_fixture; no native/model action was invoked"}


def run_f9_native_batch_fixture(env: dict[str, Any], reuse_ref: dict[str, Any], *,
                                reported_count: int = 2, fail_first: bool = True,
                                trace: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Drive public F9 prepare, F8 action/ACK, structured resource report and F9 collect."""
    if not isinstance(env.get("batch_runs"), list) or len(env["batch_runs"]) != 2:
        raise ValueError("F9 fixture requires two existing queued P2 runs")
    if type(reported_count) is not int or reported_count not in {1, 2}:
        raise ValueError("F9 fixture can report one or both actual children")
    from contextlib import closing
    from pmt.util import canonical_json, new_id

    with closing(env["db"].connect()) as conn:
        rows = conn.execute("SELECT id,revision,intent_json FROM execution_runs WHERE id IN (?,?) ORDER BY created_at,id",
                            tuple(item["run_id"] for item in env["batch_runs"])).fetchall()
    revisions = {row["id"]: row["revision"] for row in rows}
    refs = [{"run_id": item["run_id"], "expected_run_revision": revisions[item["run_id"]]}
            for item in env["batch_runs"]]
    batch_pin = env.get("batch_pin")
    if not isinstance(batch_pin, dict):
        raise ValueError("F9 fixture has no freshly captured SourcePin")
    payload = f9_batch_payloads(run_refs=refs, workspace=str(env["workspace"]),
        repository_id=env["repo_id"], relative_graph_path=env["graph_path"],
        expected_source=batch_pin, context_budget={"max_bytes": 20000, "max_lines": 1000, "unit": "utf8"},
        prepare_event_id=new_id())["prepare_step_batch"]
    prepare_envelope, prepared = _result(env, "prepare_step_batch", **payload)
    batch_ref = prepared.get("batch_ref", {}).get("id")
    if prepared.get("status") != "prepared" or not isinstance(batch_ref, str):
        raise RuntimeError("F9 prepare did not return a prepared current two-run binding")
    parent_id = env["batch_runs"][0]["run_id"]
    with closing(env["db"].connect()) as conn:
        binding = conn.execute("SELECT body_json FROM phase3_objects WHERE kind='batch_binding' AND id=?",
                               (batch_ref,)).fetchone()
    if not binding:
        raise RuntimeError("F9 prepared binding disappeared")
    binding_body = json.loads(binding["body_json"])
    if trace is not None:
        for member in binding_body.get("members", []):
            context_ref = member.get("context_ref") or {}
            context_id = context_ref.get("id")
            if isinstance(context_id, str):
                trace.append(validate_trace_record({"event_kind": "context_generated",
                    "operation": "prepare_step_batch.member_context", "request_id": prepare_envelope["request_id"],
                    "exit_code": 0, "elapsed_ms": 0, "input_bytes": 0, "output_bytes": 0,
                    "status": "projected", "evidence_refs": [], "context_id": context_id}))
    first_context_ref = binding_body["members"][0]["context_ref"]
    _, action = _result(env, "advance_execution_control", run_id=parent_id,
        context_ref=first_context_ref, reuse_body_ref=reuse_ref)
    main_action = action.get("action", {})
    if (main_action.get("kind") != "main-native-call"
            or main_action.get("context_ref") != first_context_ref):
        return {"status": "pending", "stage": "F8 native action",
            "reason_code": (main_action.get("reason") if main_action.get("kind") == "review-needed"
                            else "native_action_missing"),
            "action_kind": main_action.get("kind"), "batch_ref": batch_ref,
            "run_state": action.get("run_state"), "locks_retained": action.get("locks_retained"),
            "collect_performed": False, "quality": "not_evaluated"}
    expected_members = [{"step_id": member["step_id"], "run_id": member["run_id"],
                         "directive_sha256": member["directive_sha256"],
                         "context_ref": member["context_ref"]}
                        for member in binding_body["members"]]
    actual_members = main_action.get("members")
    if (main_action.get("batch_ref") != batch_ref or not isinstance(actual_members, list)
            or len(actual_members) != len(expected_members)
            or any(actual_members[index].get(key) != value for index, member in enumerate(expected_members)
                   for key, value in member.items())):
        return {"status": "pending", "stage": "F8 native action",
            "reason_code": "native_action_group_contract_missing", "action_kind": main_action.get("kind"),
            "batch_ref": batch_ref, "batch_ref_exposed": main_action.get("batch_ref") == batch_ref,
            "member_count_expected": len(expected_members),
            "member_count_exposed": len(actual_members) if isinstance(actual_members, list) else None,
            "context_ref_matched": main_action.get("context_ref") == first_context_ref,
            "run_state": action.get("run_state"), "locks_retained": action.get("locks_retained"),
            "collect_performed": False, "quality": "not_evaluated"}
    handle_ref = {"kind": "native_handle", "id": f"f10-group-{uuid.uuid4()}",
                  "provider_ref": "local-fixture-handle"}
    _, acknowledged = _result(env, "acknowledge_execution_action", run_id=parent_id,
        control_ref=action["control_ref"], action_nonce=main_action["action_nonce"],
        expected_run_revision=main_action["expected_run_revision"], outcome="started", handle_ref=handle_ref)
    if acknowledged.get("run_state") != "running":
        raise RuntimeError("F8 did not acknowledge the actual group handle fixture")
    from pmt.efficiency.batch import binding_for_run
    with closing(env["db"].connect()) as conn:
        binding_after_ack = binding_for_run(conn, parent_id, require_leader=True)
        if (not binding_after_ack or binding_after_ack["body"].get("status") != "running"
                or binding_after_ack["body"].get("handle_ref") != handle_ref["id"]):
            raise RuntimeError("F8 handle ACK did not bind the actual P2 handle into F9")
    action_batch_ref_exposed = main_action.get("batch_ref") == batch_ref
    f7, sanitized = run_f7_public(env, run_id=parent_id,
        raw_output="Local deterministic group fixture report.\n" + canonical_json({"batch_ref": batch_ref,
            "member_count": len(binding_body["members"]), "status": "fixture"}) + "\n")
    f7_ref = f7.get("evidence_ref", {}).get("id")
    if not isinstance(f7_ref, str):
        raise RuntimeError("F7 did not preserve the structured group observation resource")

    root = Path(env.get("root") or Path(env["workspace"]).parent)
    output_dir = root / "f10-batch-report"
    output_dir.mkdir(parents=True, exist_ok=True)
    stop_path = output_dir / f"stop-{uuid.uuid4()}.json"
    stop_path.write_text(canonical_json({"fixture": "actual F8 handle acknowledged and stopped",
                                         "parent_run_id": parent_id}) + "\n", encoding="utf-8")
    _, stop_receipt = _result(env, "register_resource", source_path=str(stop_path),
        allowed_root=str(output_dir), retention="evidence", owner_record_id=env["step_id"])
    if "artifact_id" not in stop_receipt:
        raise RuntimeError("F8 stop fixture receipt was not registered")
    step_reports = []
    for index, member in enumerate(binding_body["members"][:reported_count]):
        outcome = "fail" if fail_first and index == 0 else "pass"
        step_reports.append({"step_id": member["step_id"], "run_id": member["run_id"],
            "directive_sha256": member["directive_sha256"], "context_ref": member["context_ref"],
            "summary": "Deterministic local fixture; no model quality was evaluated.", "choices": [],
            "criteria_results": [{"criterion_id": criterion["id"], "outcome": outcome,
                "reason": "fixture failure" if outcome == "fail" else None,
                "evidence_refs": [f7_ref]} for criterion in member["criteria"]],
            "tests": [], "evidence_refs": [f7_ref], "unresolved_items": []})
    report = {"schema": "pmt-batch-report-v1", "batch_id": batch_ref, "steps": step_reports}
    # The runner report is an already-produced local fixture artifact. The F9
    # collector requires its immutable resource ref to be explicitly owned by
    # the representative parent run; public P2/LocalStore still owns result and
    # collection transitions. This matches the F9 producer integration fixture.
    from pmt.phase2_common import persist_json_resource
    report_started = time.perf_counter_ns()
    report_receipt = persist_json_resource(env["db"],
        {"request_id": new_id(), "actor": env["actor"], "session_id": env["session"],
         "scope_id": env["project_id"], "payload": {}}, report,
        env["project_id"], "batch_runner_report_fixture", parent_id)
    if trace is not None:
        trace.append(validate_trace_record({"event_kind": "internal_call",
            "operation": "fixture_report_resource_publication", "request_id": new_id(),
            "exit_code": 0, "elapsed_ms": (time.perf_counter_ns() - report_started) / 1_000_000,
            "input_bytes": report_receipt["size_bytes"], "output_bytes": 0,
            "status": "published", "evidence_refs": [report_receipt["artifact_id"]]}))
    with closing(env["db"].connect()) as conn:
        parent = conn.execute("SELECT revision,route_json,directive_version FROM execution_runs WHERE id=?",
                              (parent_id,)).fetchone()
    parent_result = {"directive_version": parent["directive_version"],
        "actual_route": json.loads(parent["route_json"]),
        "summary": "Fixture result; no model quality was evaluated.",
        "criteria_results": [{"criterion_id": criterion["id"], "outcome": "not_run",
            "reason": "per-child group report is authoritative", "evidence_refs": []}
            for criterion in env["criteria"]],
        "receipt_ref": stop_receipt["artifact_id"], "evidence_refs": [stop_receipt["artifact_id"], f7_ref],
        "stop_confirmed": True, "stop_evidence_refs": [stop_receipt["artifact_id"]],
        "runner_observation": {"exit_code": 0, "origin": "fixture"},
        "batch_ref": batch_ref, "batch_report_ref": report_receipt["artifact_id"],
        "batch_report_sha256": report_receipt["sha256"]}
    _, submitted = _result(env, "submit_execution_result", run_id=parent_id,
        expected_run_revision=parent["revision"], result=parent_result)
    with closing(env["db"].connect()) as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()[0]
    collect_request_id = new_id()
    collect_payload = f9_batch_payloads(run_refs=refs, workspace=str(env["workspace"]),
        repository_id=env["repo_id"], relative_graph_path=env["graph_path"],
        expected_source=batch_pin, context_budget={"max_bytes": 20000, "max_lines": 1000, "unit": "utf8"},
        prepare_event_id=new_id(), batch_ref=batch_ref, parent_run_id=parent_id,
        expected_run_revision=revision, collect_event_id=new_id())["collect_step_batch"]
    import pmt.service as service_module
    original_execute = service_module.execute
    def measured_internal(db, request):
        if isinstance(request, dict) and request.get("operation") == "compact_tool_result":
            started = time.perf_counter_ns()
            input_bytes = len(canonical_json(request).encode("utf-8"))
            envelope, code = original_execute(db, request)
            output_bytes = len(canonical_json(envelope).encode("utf-8"))
            if trace is not None:
                trace.append(validate_trace_record({"event_kind": "internal_call",
                    "operation": "compact_tool_result", "request_id": request["request_id"],
                    "exit_code": code, "elapsed_ms": (time.perf_counter_ns() - started) / 1_000_000,
                    "input_bytes": input_bytes, "output_bytes": output_bytes,
                    "status": "ok" if code == 0 and envelope.get("ok") else "failed",
                    "evidence_refs": []}))
            return envelope, code
        return original_execute(db, request)
    service_module.execute = measured_internal
    try:
        _, collected = _result(env, "collect_step_batch", request_id=collect_request_id, **collect_payload)
        _, replayed = _result(env, "collect_step_batch", request_id=collect_request_id, **collect_payload)
    finally:
        service_module.execute = original_execute
    if replayed != collected:
        raise RuntimeError("F9 collection replay returned a different saved response")
    expected_state = "review_pending" if reported_count == 2 else "reconciling"
    if collected.get("status") != expected_state or collected.get("parent_done") is not False:
        raise RuntimeError("F9 collection did not preserve the fixture failure/missing-child state: "
            + canonical_json({"status": collected.get("status"), "parent_done": collected.get("parent_done"),
                              "children": [{"state": child.get("state"), "run_id": child.get("run_id")}
                                          for child in collected.get("children", [])]}))
    if trace is not None and (fail_first or reported_count < len(binding_body["members"])):
        mark_cost(trace, "review", "F9 child result requires review or missing-member reconciliation",
                  refs=[report_receipt["artifact_id"]])
    return {"batch_ref": prepared["batch_ref"], "status": collected["status"],
        "reported_child_count": reported_count, "child_states": [child["state"] for child in collected["children"]],
        "report_ref": report_receipt["artifact_id"], "report_sha256": report_receipt["sha256"],
        "f7_ref": f7_ref, "parent_result_state": submitted.get("state"),
        "collect_replayed": True, "quality": "fixture_only_not_model_quality",
        "report_resource_bytes": report_receipt["size_bytes"],
        "f8_action_bound_batch_ref": action_batch_ref_exposed,
        "evidence_tier": "actual LocalStore/P2 run and F9 state transition; local deterministic data"}


def _write_fixture_receipt(env: dict[str, Any], *, exit_code: int, output: str) -> dict[str, Any]:
    root = Path(env.get("root") or Path(env["workspace"]).parent)
    target_dir = root / "f10-fixture-receipts"
    target_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = target_dir / f"receipt-{uuid.uuid4()}.json"
    receipt_path.write_text(json.dumps({"run_id": env["run_id"],
        "receipt": {"state": "failed" if exit_code else "completed", "exit_code": exit_code},
        "output": output}, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    _, result = _result(env, "register_resource", source_path=str(receipt_path),
                        allowed_root=str(target_dir), retention="evidence",
                        owner_record_id=env["step_id"])
    return result


def _submit_fixture_run_result(env: dict[str, Any], f7_artifact_id: str,
                               runner_receipt: dict[str, Any]) -> dict[str, Any]:
    with closing(env["db"].connect()) as conn:
        row = conn.execute("SELECT revision,route_json,directive_version FROM execution_runs WHERE id=?",
                           (env["run_id"],)).fetchone()
    route = json.loads(row["route_json"])
    evidence = sorted({f7_artifact_id, runner_receipt["artifact_id"]})
    criteria = env["criteria"]
    result = {"directive_version": row["directive_version"], "actual_route": route,
              "summary": "F10 deterministic failure-output fixture; no model quality was evaluated.",
              "criteria_results": [{"criterion_id": item["id"], "outcome": "fail",
                                    "evidence_refs": evidence,
                                    "reason": "fixture failure evidence is deliberately non-passing"}
                                   for item in criteria],
              "receipt_ref": runner_receipt["artifact_id"], "evidence_refs": evidence,
              "runner_observation": {"exit_code": 1, "state": "failed",
                                     "origin": "local_fixture_receipt"},
              "stop_confirmed": True,
              "stop_evidence_refs": [runner_receipt["artifact_id"]]}
    _, submitted = _result(env, "submit_execution_result", run_id=env["run_id"],
                           expected_run_revision=row["revision"], result=result)
    return {"run_id": submitted["run_id"], "state": submitted["state"],
            "revision": submitted["revision"], "receipt_ref": runner_receipt["artifact_id"],
            "evidence_refs": evidence, "quality": "fixture_failure; not a model result"}


def _prepare_item_for_reuse(env: dict[str, Any]) -> None:
    """Fixture metadata only: give the P2 Item the actual workspace and criteria selectors."""
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json({"workspace": str(env["workspace"]),
                                      "criteria": [item["id"] for item in env["criteria"]]}),
                      env["item_id"]))


def run_f1_f8_preparation(env: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    """Exercise current public F1-F8 LocalStore operations; F9/overall acceptance stay pending."""
    if scenario_id not in {item["id"] for item in load_catalog()["cases"]}:
        raise ValueError("unknown frozen F10 scenario ID")
    if scenario_id == "resume-existing-plan" and not (isinstance(env.get("resume_env"), dict)
            or isinstance(env.get("resume_context_id"), str)):
        return {"scenario_id": scenario_id, "status": "not_run",
                "reason": "a second current owner/session/run fixture is required for resume"}
    if scenario_id == "parallel-investigation-context" and not (
            isinstance(env.get("parallel_members"), list) and len(env["parallel_members"]) >= 2):
        return {"scenario_id": scenario_id, "status": "not_run",
                "reason": "two independently authorized current run fixtures are required"}
    if scenario_id == "ambiguous-requirement" and env.get("criteria"):
        return {"scenario_id": scenario_id, "status": "not_run",
                "reason": "ambiguity fixture must preserve the unresolved acceptance as missing"}
    trace: list[dict[str, Any]] = []
    with measure_localstore_calls(trace):
        # Document baseline needs a separately declared path claim in this synthetic fixture.
        from pmt.util import new_id, utc_now
        with env["db"].write() as conn:
            exists = conn.execute("SELECT 1 FROM scope_locks WHERE run_id=? AND resource=?",
                                  (env["run_id"], "docs/pmt-docs/plan.md")).fetchone()
            if not exists:
                conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (new_id(), env["run_id"], env["session"], "path", str(env["workspace"]),
                              "docs/pmt-docs/plan.md", utc_now()))
        _prepare_item_for_reuse(env)
        premise = ("F10 ambiguity fixture: acceptance remains unknown"
                   if scenario_id == "ambiguous-requirement" else "F10 frozen one-field requirement update")
        needs_delta = scenario_id in {"partial-plan-change", "resume-existing-plan",
                                      "parallel-investigation-context", "ambiguous-requirement"}
        f1_f4 = run_f1_f4_public(env, premise=premise, partial=needs_delta)
        f2_slice = run_f2_public_context_slice(env, f1_f4["source_pin"],
                                               env.get("context_node_ids", [env["node_id"]]))
        context = run_f5_public(env, f1_f4["source_pin"])
        limited_context = run_f5_public(env, f1_f4["source_pin"], small_budget=True)
        resume_context = None
        if scenario_id == "resume-existing-plan":
            resumed_env = env.get("resume_env", env)
            previous_id = env.get("resume_context_id", context["context_ref"]["id"])
            resume_context = run_f5_public(resumed_env, f1_f4["source_pin"],
                                           resume_from=previous_id)
            env = resumed_env
        parallel_contexts = []
        parallel_reuse = []
        if scenario_id == "parallel-investigation-context":
            for member_env in env["parallel_members"]:
                context_result = run_f5_public(member_env, f1_f4["source_pin"])
                parallel_contexts.append({"run_id": member_env["run_id"],
                                          "context_ref": context_result["context_ref"],
                                          "incomplete": context_result["incomplete"],
                                          "detail_pages": context_result["detail_pages"]})
                parallel_reuse.append(run_f6_public(member_env, f1_f4["source_pin"])["first"]["status"])
        reuse = run_f6_public(env, f1_f4["source_pin"])
        selected_context = (context if scenario_id == "ambiguous-requirement"
                            else resume_context or context)

        if selected_context["incomplete"]:
            response, code = call_public(env, "advance_execution_control",
                run_id=env["run_id"], context_ref=selected_context["context_ref"],
                reuse_body_ref=reuse["decision_ref"])
            if code != 0 or not response.get("ok") or response["result"].get("action", {}).get("kind") != "review-needed":
                raise RuntimeError("F8 did not hold an incomplete or unknown mandatory context for review")
            mark_cost(trace, "review", "context_incomplete_review",
                      source_hash=f1_f4["source_pin"]["source_hash"])
            return {"scenario_id": scenario_id, "status": "partial_pending_f9",
                    "f1_f4": f1_f4, "f2_slice": f2_slice,
                    "f5": selected_context, "f5_small_budget": limited_context,
                    "resume": resume_context, "parallel_contexts": parallel_contexts,
                    "parallel_reuse_statuses": parallel_reuse, "f6": reuse,
                    "f8": {"state": "review_required", "action": response["result"]["action"]},
                    "trace": trace, "observation": observation_from_trace(trace),
                    "evidence_tier": "local fixture; F8 correctly blocked incomplete context",
                    "f9": f9_status_for_fixture(env)}

        if scenario_id == "failed-operation-retry-and-rework":
            # Python is placed behind the fixed codex argv as a deterministic failing
            # local child process. No external CLI or provider is launched.
            _make_native_run_starting(env, mode="cli")
            from unittest.mock import patch
            import sys
            import pmt.runners.service as runner_service
            with patch.object(runner_service.shutil, "which", return_value=sys.executable):
                first_env, first = _result(env, "advance_execution_control",
                    run_id=env["run_id"], context_ref=selected_context["context_ref"],
                    reuse_body_ref=reuse["decision_ref"])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with closing(env["db"].connect()) as conn:
                        state = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?",
                                             (env["run_id"],)).fetchone()
                    if state and state["state"] in {"failed", "blocked", "canceled"}:
                        break
                    call_public(env, "advance_execution_control", run_id=env["run_id"],
                                context_ref=selected_context["context_ref"],
                                reuse_body_ref=reuse["decision_ref"])
                    time.sleep(0.1)
                mark_cost(trace, "retry", "advance_execution_control.retry",
                          source_hash=f1_f4["source_pin"]["source_hash"])
                retry_envelope, retry_code = call_public(env, "advance_execution_control",
                    run_id=env["run_id"], context_ref=selected_context["context_ref"],
                    reuse_body_ref=reuse["decision_ref"], retry=True)
                if retry_code == 0 or (retry_envelope.get("error") or {}).get("code") != "retry_not_allowed":
                    raise RuntimeError("F8 retried or misclassified a local CLI fixture without transient evidence")
                f7, _sanitized = run_f7_public(env, run_id=env["run_id"],
                    raw_output="Traceback (most recent call last):\n  File \"fixture.py\", line 17\n"
                               "RuntimeError: fixture CLI launch failed\napi_key=sk_f10fixtureSecret123456\n")
                return {"scenario_id": scenario_id, "status": "partial_pending_f9",
                        "f1_f4": f1_f4, "f2_slice": f2_slice,
                        "f5": selected_context, "f5_small_budget": limited_context,
                        "resume": resume_context, "parallel_contexts": parallel_contexts,
                        "f6": reuse, "f7": f7, "f8": {"state": "failed_fixture",
                            "retry": "rejected_without_transient_classification",
                            "exit_code": retry_code, "error_code": retry_envelope["error"]["code"]},
                        "trace": trace, "observation": observation_from_trace(trace),
                        "evidence_tier": "local process fixture; no provider/model result",
                        "f9": f9_status_for_fixture(env)}

        if scenario_id == "ambiguous-requirement":
            response, code = call_public(env, "advance_execution_control",
                run_id=env["run_id"], context_ref=limited_context["context_ref"],
                reuse_body_ref=reuse["decision_ref"])
            if code != 0 or not response.get("ok") or not response["result"].get("action", {}).get("kind") == "review-needed":
                raise RuntimeError("F8 did not hold an incomplete mandatory context for review")
            mark_cost(trace, "review", "context_incomplete_review",
                      source_hash=f1_f4["source_pin"]["source_hash"])
            return {"scenario_id": scenario_id, "status": "partial_pending_f9",
                    "f1_f4": f1_f4, "f2_slice": f2_slice,
                    "f5": limited_context, "resume": resume_context,
                    "parallel_contexts": parallel_contexts, "f6": reuse,
                    "f8": {"state": "review_required", "action": response["result"]["action"]},
                    "trace": trace, "observation": observation_from_trace(trace),
                    "evidence_tier": "local fixture; no model quality evaluated",
                    "f9": f9_status_for_fixture(env)}

        f8 = run_f8_native_fixture(env, selected_context["context_ref"], reuse["decision_ref"])
        f7, sanitized = run_f7_public(env, run_id=env["run_id"],
            raw_output="Traceback (most recent call last):\n  File \"fixture.py\", line 17\n"
                       "RuntimeError: fixture failure line\napi_key=sk_f10fixtureSecret123456\n")
        f7_artifact_id = f7.get("evidence_ref", {}).get("id")
        if not isinstance(f7_artifact_id, str):
            raise RuntimeError("F7 compact result has no registered resource evidence")
        receipt = _write_fixture_receipt(env, exit_code=1, output=sanitized)
        submitted = _submit_fixture_run_result(env, f7_artifact_id, receipt)
        _, f8_result = _result(env, "advance_execution_control",
            run_id=env["run_id"], context_ref=selected_context["context_ref"],
            reuse_body_ref=reuse["decision_ref"])
        mark_cost(trace, "review", "execution.review_required",
                  source_hash=f1_f4["source_pin"]["source_hash"],
                  refs=[receipt["artifact_id"]])
        return {"scenario_id": scenario_id, "status": "partial_pending_f9",
                "f1_f4": f1_f4, "f2_slice": f2_slice,
                "f5": selected_context, "f5_small_budget": limited_context,
                "resume": resume_context, "parallel_contexts": parallel_contexts,
                "parallel_reuse_statuses": parallel_reuse, "f6": reuse, "f7": f7, "f8": f8,
                "run_result": submitted, "f8_observation": {
                    "action": f8_result.get("action"), "run_state": f8_result.get("run_state"),
                    "locks_retained": f8_result.get("locks_retained")},
                "trace": trace, "observation": observation_from_trace(trace),
                "evidence_tier": "local integration fixture; native/model quality not evaluated",
                "f9": f9_status_for_fixture(env)}


def run_integrated_suite(env_factory, *, root_go: bool) -> dict[str, Any]:
    """Full F10 execution gate. Requires producer F9 runtime and an explicit root GO."""
    if root_go is not True:
        return {"status": "not_run", "reason": "root_go_required",
                "overall_acceptance": "not_run", "actual_scenario_run_performed": False}
    report = preflight()
    if report["integrated_run_state"] != "ready_for_explicit_root_go":
        return {"status": "pending", "reason": "runtime_capability_missing",
                "capabilities": report["capabilities"],
                "overall_acceptance": "not_run", "actual_scenario_run_performed": False}
    results = []
    for case in load_catalog()["cases"]:
        results.append(run_f1_f8_preparation(env_factory(case), case["id"]))
    return {"status": "partial_pending_f9_contract_fixture",
            "scenarios": results, "overall_acceptance": "not_run",
            "actual_scenario_run_performed": True,
            "reason": "F9 runtime and batch fixture must be attached before overall comparison"}


def validate_trace_record(record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("trace record must be an object")
    keys = set(record)
    forbidden = {key for key in keys if key.casefold() in TRACE_FORBIDDEN}
    if forbidden:
        raise ValueError("trace record contains a raw-content field")
    unknown = keys - TRACE_ALLOWED
    if unknown:
        raise ValueError("trace record contains unsupported metadata fields")
    for name in ("event_kind", "operation", "request_id"):
        if not isinstance(record.get(name), str) or not record[name]:
            raise ValueError(f"trace {name} is required")
    if record["event_kind"] not in {"call", "retry", "rework", "review", "context_generated", "internal_call"}:
        raise ValueError("unsupported trace event kind")
    try:
        if str(uuid.UUID(record["request_id"])) != record["request_id"]:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("trace request_id must be a canonical UUID") from exc
    if type(record.get("exit_code")) is not int or record["exit_code"] < 0:
        raise ValueError("trace exit_code must be a nonnegative integer")
    for field in ("elapsed_ms", "input_bytes", "output_bytes"):
        value = record.get(field)
        if type(value) not in (int, float) or value < 0:
            raise ValueError(f"trace {field} must be a nonnegative observation")
    refs = record.get("evidence_refs", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise ValueError("trace evidence_refs must be short references")
    for field in ("source_hash", "artifact_hashes"):
        value = record.get(field)
        values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        if field in record and (not values or any(not isinstance(item, str) or len(item) != 64
                                                  or any(ch not in "0123456789abcdef" for ch in item)
                                                  for item in values)):
            raise ValueError(f"trace {field} must contain SHA-256 fingerprints")
    return dict(record)


def observation_from_trace(records: list[dict[str, Any]], *, tokens: dict[str, Any] | None = None) -> dict[str, Any]:
    trace = [validate_trace_record(item) for item in records]
    calls = [row for row in trace if row["event_kind"] == "call"]
    internal_calls = [row for row in trace if row["event_kind"] == "internal_call"]
    detail_ops = {"read_context_detail", "read_tool_result_detail", "read_reuse_decision"}
    context_ops = {"build_task_context", "resume_task_context"}
    op_counts: dict[str, int] = {}
    for row in calls:
        op_counts[row["operation"]] = op_counts.get(row["operation"], 0) + 1
    return {"input_bytes": sum(row["input_bytes"] for row in calls),
            "output_bytes": sum(row["output_bytes"] for row in calls),
            "calls": len(calls), "detail_queries": sum(row["operation"] in detail_ops for row in calls),
            "context_generations": (sum(row["operation"] in context_ops for row in calls)
                                    + len({row.get("context_id") for row in trace
                                           if row["event_kind"] == "context_generated"
                                           and isinstance(row.get("context_id"), str)})),
            "internal_calls": len(internal_calls),
            "internal_input_bytes": sum(row["input_bytes"] for row in internal_calls),
            "internal_output_bytes": sum(row["output_bytes"] for row in internal_calls),
            "internal_elapsed_ms": sum(row["elapsed_ms"] for row in internal_calls),
            "internal_calls_by_operation": {name: sum(row["operation"] == name for row in internal_calls)
                                            for name in sorted({row["operation"] for row in internal_calls})},
            "calls_by_operation": dict(sorted(op_counts.items())),
            "retries": sum(row["event_kind"] == "retry" for row in trace),
            "rework": sum(row["event_kind"] == "rework" for row in trace),
            "reviews": sum(row["event_kind"] == "review" for row in trace),
            "elapsed_ms": sum(row["elapsed_ms"] for row in trace),
            "measurement_basis": "sum of canonical UTF-8 protocol request/response bytes and observed local call wall time; no token inference",
            "tokens": tokens or {"status": "unknown", "actual": None, "estimate": None},
            "evidence_tier": "local_integration",
            "quality": {"status": "not_run", "criteria": [], "model_quality": "not_evaluated"},
            "evidence_refs": sorted({ref for row in trace for ref in row.get("evidence_refs", [])})}


def capture_partial_change_baseline(*, output_dir: Path = EVIDENCE, repetitions: int = 5) -> dict[str, Any]:
    if type(repetitions) is not int or repetitions < 1 or repetitions > 20:
        raise ValueError("repetitions must be in 1..20")
    catalog = load_catalog()
    scenario = next(item for item in catalog["cases"] if item["id"] == "partial-plan-change")
    fixture = catalog["common"]["source_fixture"]
    base_bytes = (ROOT / fixture["path"]).read_bytes()
    base_graph = json.loads(base_bytes.decode("utf-8"))
    patch = scenario["input"]["patch"]
    node = next((item for item in base_graph["nodes"] if item.get("id") == patch["node_id"]), None)
    if node is None or patch["op"] != "replace" or patch["field"] not in node:
        raise ValueError("frozen partial-change patch no longer matches canonical graph")
    node[patch["field"]] = patch["value"]
    patched_bytes = (json.dumps(base_graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    patched_hash = _sha256(patched_bytes)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "partial-change-input.graph.json").write_bytes(patched_bytes)
    report = validate_graph(base_graph, base_graph["project_id"])
    markdown, graph_json, render_report = render_docs(base_graph)
    if render_report != report:
        raise RuntimeError("legacy renderer and validator disagreed")
    markdown_bytes, graph_bytes = markdown.encode("utf-8"), graph_json.encode("utf-8")
    (output_dir / "partial-change-rendered.md").write_bytes(markdown_bytes)
    (output_dir / "partial-change-rendered.graph.json").write_bytes(graph_bytes)

    legacy_condition = load_catalog()["common"]
    # The baseline records the actual host, but compares only when the after-run has
    # the same frozen source, environment, model role, policy, and measurement units.
    from pmt.efficiency.measurement import fingerprint as measure_fingerprint
    base_manifest = json.loads((ROOT / "docs/phase3/evidence/2026-10-02/baseline/phase2-planning-full-transfer.json").read_text(encoding="utf-8"))
    old_condition = base_manifest["condition"]
    source_hashes = {name: _sha256((ROOT / name).read_bytes())
                     for name in ("src/pmt/planning/graph.py", "src/pmt/util.py")}
    condition = {
        "goal": scenario["logical_goal"],
        "acceptance": {"definition": "same frozen graph change, full validation, and deterministic final plan artifacts",
                       "criteria": scenario["acceptance"]},
        "source": {"repository_commit": fixture["repository_commit"], "base_fixture_sha256": fixture["sha256"],
                   "patched_graph_sha256": patched_hash, "implementation_file_sha256": source_hashes},
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "sqlite": sqlite3.sqlite_version, "execution": "local process; no external model"},
        "model_role": {"model": "none", "role": "existing phase2 graph validator and document renderer"},
        "policy": {"operation": "validate complete changed graph then render complete Markdown and graph JSON",
                   "retry_policy": "no retry", "model_usage": "none"},
        "definition": {"measurement_schema": "pmt-efficiency-local-v1",
                       "byte_definition": "UTF-8 frozen graph input and rendered Markdown + graph JSON",
                       "timing_definition": "perf_counter_ns elapsed time for validate_graph and render_docs calls"},
    }
    observations = []
    for _ in range(repetitions):
        _, validate_ms = time_call(validate_graph, base_graph, base_graph["project_id"])
        (rendered_md, rendered_json, check), render_ms = time_call(render_docs, base_graph)
        if check != report or rendered_md != markdown or rendered_json != graph_json:
            raise RuntimeError("legacy renderer changed output between repetitions")
        observations.append({"input_bytes": len(patched_bytes),
                             "output_bytes": len(markdown_bytes) + len(graph_bytes),
                             "calls": 2, "detail_queries": 0, "context_generations": 0,
                             "retries": 0, "rework": 0, "reviews": 0,
                             "elapsed_ms": validate_ms + render_ms,
                             "validate_elapsed_ms": validate_ms, "render_elapsed_ms": render_ms,
                             "tokens": {"status": "unknown", "actual": None, "estimate": None},
                             "evidence_tier": "fixture",
                             "quality": {"status": "pass" if report.get("valid") and report.get("complete") else "fail",
                                         "criteria": scenario["acceptance"], "model_quality": "not_evaluated"}})
    case = {"id": scenario["measurement_case_id"], "purpose": scenario["logical_goal"],
            "input": {"base_graph_sha256": fixture["sha256"], "patch": patch,
                      "patched_graph_sha256": patched_hash},
            "output": {"validation_report_sha256": fingerprint(report),
                       "markdown_sha256": _sha256(markdown_bytes), "graph_json_sha256": _sha256(graph_bytes)},
            "observations": observations,
            "evidence_refs": ["partial-change-input.graph.json", "partial-change-rendered.md",
                              "partial-change-rendered.graph.json", "src/pmt/planning/graph.py"]}
    manifest = capture_baseline(case, condition)
    manifest["legacy_runtime_provenance"] = {"original_baseline_environment": old_condition["environment"],
                                              "implementation_source_sha256": source_hashes,
                                              "repetitions": repetitions}
    manifest.pop("manifest_fingerprint", None)
    manifest["manifest_fingerprint"] = measure_fingerprint(manifest)
    target = output_dir / "phase2-partial-change-full-render.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"case_id": manifest["case_id"], "repetitions": repetitions,
            "input_bytes": len(patched_bytes), "output_bytes": len(markdown_bytes) + len(graph_bytes),
            "input_sha256": patched_hash, "markdown_sha256": _sha256(markdown_bytes),
            "graph_json_sha256": _sha256(graph_bytes), "manifest_sha256": manifest["manifest_fingerprint"],
            "evidence_refs": [str(target.relative_to(ROOT)),
                              str((output_dir / "partial-change-input.graph.json").relative_to(ROOT))]}


def _pytest_summary(output: str) -> dict[str, int]:
    found = re.search(r"(?P<counts>(?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|warnings?)(?:, )?)+) in ", output)
    counts: dict[str, int] = {}
    if found:
        for count, label in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|warnings?)", found.group("counts")):
            counts[label] = counts.get(label, 0) + int(count)
    return counts


def run_f15_local_acceptance(*, output_dir: Path = ACCEPTANCE_EVIDENCE,
                             source_stable: bool = False) -> dict[str, Any]:
    """Run package and loopback transfer acceptance in isolated test roots."""
    from pmt import __version__
    from pmt.db import SCHEMA_VERSION

    if __version__ != "0.3.0" or SCHEMA_VERSION != 4:
        return {"schema_version": 1, "kind": "phase3-f15-local-acceptance",
                "status": "blocked", "reason_code": "unexpected_core_or_schema_version",
                "core_version": __version__, "db_schema": SCHEMA_VERSION}
    source_files = list(TRACEABILITY_SOURCES) + ["src/pmt/__init__.py"]
    def source_snapshot() -> dict[str, str]:
        return {relative: _sha256((ROOT / relative).read_bytes())
                for relative in source_files if (ROOT / relative).is_file()}
    source_hashes_at_start = source_snapshot()
    test_paths = [
        ("installed_package_local_and_https", ["tests/test_phase3_acceptance.py"]),
        ("https_backup_transfer_and_isolated_restore", [
            "tests/test_phase3_transfer_http.py::test_loopback_https_import_backup_download_and_isolated_restore"]),
    ]
    hosted_files = ROOT / "tests/test_phase3_hosted_files.py"
    hosted_cli = ROOT / "tests/test_phase3_hosted_cli.py"
    if hosted_files.is_file():
        test_paths.append(("hosted_local_file_effects_and_outbox", [str(hosted_files.relative_to(ROOT))]))
    if hosted_cli.is_file():
        test_paths.append(("installed_hosted_runtime_cli", [str(hosted_cli.relative_to(ROOT))]))
    hosted_plan_tests = ROOT / "tests/test_phase3_hosted_planning.py"
    if hosted_plan_tests.is_file():
        test_paths.append(("hosted_client_plan_metadata_and_local_routing",
                           [str(hosted_plan_tests.relative_to(ROOT))]))
    source_metadata_tests = ROOT / "tests/test_phase3_source_metadata.py"
    if source_metadata_tests.is_file():
        test_paths.append(("host_source_metadata_scope_isolation",
                           [str(source_metadata_tests.relative_to(ROOT))]))
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    ephemeral_root = ROOT / ".pmt-test" / ("f15-" + uuid.uuid4().hex[:12])
    ephemeral_root.mkdir(parents=True, exist_ok=False)
    results = []
    source_changes: list[dict[str, Any]] = []
    for index, (name, paths) in enumerate(test_paths):
        basetemp = ephemeral_root / f"pytest-{index}"
        command = [sys.executable, "-m", "pytest", *paths, "-q", "--tb=short",
                   "--show-capture=no", f"--basetemp={basetemp}"]
        started = time.perf_counter_ns()
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", timeout=1200, check=False,
            env={key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH")
                 if key in os.environ})
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        summary = _pytest_summary(completed.stdout + "\n" + completed.stderr)
        results.append({"test_group": name, "test_paths": paths,
            "exit_code": completed.returncode, "elapsed_ms": round(elapsed, 3),
            "counts": summary, "status": "passed" if completed.returncode == 0 else "failed",
            "evidence_tier": "isolated local tests with real in-process/loopback Host behavior"})
        current_hashes = source_snapshot()
        changed = sorted(relative for relative in set(source_hashes_at_start) | set(current_hashes)
            if source_hashes_at_start.get(relative) != current_hashes.get(relative))
        if changed:
            source_changes.append({"after_test_group": name, "changed_source_paths": changed})
    source_hashes = source_snapshot()
    def distribution_version(name: str) -> str | None:
        try:
            from importlib.metadata import version
            return version(name)
        except Exception:
            return None

    upstream_present = hosted_files.is_file() and hosted_cli.is_file()
    all_groups_passed = bool(results) and all(item["exit_code"] == 0 for item in results)
    required_groups = {"installed_package_local_and_https", "https_backup_transfer_and_isolated_restore",
        "hosted_local_file_effects_and_outbox", "installed_hosted_runtime_cli",
        "hosted_client_plan_metadata_and_local_routing", "host_source_metadata_scope_isolation"}
    present_groups = {item["test_group"] for item in results}
    missing_groups = sorted(required_groups - present_groups)
    f14_group_names = {"hosted_local_file_effects_and_outbox", "installed_hosted_runtime_cli"}
    f14_results = [item for item in results if item["test_group"] in f14_group_names]
    f14_ready = upstream_present and len(f14_results) == len(f14_group_names) \
        and all(item["exit_code"] == 0 for item in f14_results)
    local_status = ("failed" if not all_groups_passed else
                    "inconclusive_source_changed_during_run" if source_changes else
                    "incomplete_required_groups" if missing_groups else
                    "passed_local_tier" if source_stable else "partial_pending_source_stability")
    runner_command = [Path(sys.executable).name, "scripts/verify_phase3_local.py", "--acceptance"]
    if source_stable:
        runner_command.append("--source-stable")
    manifest = {"schema_version": 1, "kind": "phase3-f15-local-acceptance",
        "recorded_on": utc_now(),
        "status": local_status,
        "exit_code": 0 if local_status in {"passed_local_tier", "partial_pending_source_stability"} else 1,
        "overall_acceptance": "partial_pending_native_product_remote_and_full_model_measurement",
        "acceptance_command": runner_command,
        "source_stability_confirmation": "confirmed_by_runner_argument" if source_stable else "not_confirmed",
        "core_version": __version__, "db_schema": SCHEMA_VERSION,
        "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=False).stdout.strip() or None,
        "source_tree": {"dirty": bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, check=False).stdout.strip()),
            "tracked_and_untracked_entry_count": len(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                capture_output=True, text=True, check=False).stdout.splitlines())},
        "source_sha256_at_start": source_hashes_at_start,
        "source_sha256": source_hashes, "source_changes_during_run": source_changes,
        "missing_required_groups": missing_groups, "test_groups": results,
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version, "fastapi": distribution_version("fastapi"),
            "uvicorn": distribution_version("uvicorn"),
            "cryptography": distribution_version("cryptography"),
            "deployment_tier": "loopback HTTPS with isolated Host data/config roots"},
        "package_snapshot": {"immutable_product_package": "not_built_source_still_changing",
            "isolated_wheel_test": "temporary wheel built and installed inside pytest fixture",
            "update_fixture": "synthetic distribution metadata revision 0.3.0.post1; runtime core remains 0.3.0"},
        "f14_hosted_local_runtime_and_outbox": "included_and_passed" if f14_ready else "pending_or_incomplete",
        "native_product_installation": "not_run",
        "remote_host": "not_run",
        "linux_or_public_deployment": "not_run",
        "external_models": "not_called",
        "model_quality": "not_assessed",
        "measurement_cost_and_tokens": {"status": "not_assessed_here",
            "tokens": {"status": "unknown", "actual": None, "estimate": None}}}
    fingerprinted = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(fingerprinted).encode("utf-8")).hexdigest()
    target = output_dir / "f15-local-acceptance.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    try:
        manifest_ref = target.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        manifest_ref = target.name
    return {"status": manifest["status"], "overall_acceptance": manifest["overall_acceptance"],
            "manifest_ref": manifest_ref,
            "manifest_sha256": manifest["manifest_sha256"],
            "exit_code": manifest["exit_code"],
            "test_groups": [{"name": item["test_group"], "status": item["status"],
                             "exit_code": item["exit_code"], "counts": item["counts"]}
                            for item in results]}


def build_final_package_snapshot(*, output_dir: Path = ACCEPTANCE_EVIDENCE) -> dict[str, Any]:
    """Build one immutable three-product snapshot after the caller confirms sources stable."""
    import tomllib
    import build_plugins as builder
    from pmt import __version__
    from pmt.db import SCHEMA_VERSION

    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    target_root = Path(output_dir).resolve() / "package-snapshot"
    final_root = target_root / version
    if final_root.exists():
        products = {}
        generated_metadata_overrides = {
            ".codex-plugin/plugin.json", ".claude-plugin/plugin.json",
        }
        for product in builder.PRODUCTS:
            package = final_root / product
            archive = final_root / f"{product}.zip"
            package_manifest_path = package / "pmt-package.json"
            package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
            expected = package_manifest.get("files")
            actual = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in package.rglob("*") if path.is_file() and path.name != "pmt-package.json"}
            if (package_manifest.get("product") != product
                    or package_manifest.get("plugin_version") != version
                    or package_manifest.get("core_version") != __version__
                    or package_manifest.get("schema_version") != SCHEMA_VERSION
                    or actual != expected):
                raise RuntimeError("Existing package snapshot failed manifest integrity verification")
            stale_sources = []
            for source, relative in builder._source_map(ROOT, product).items():
                target_relative = relative.as_posix()
                if target_relative in generated_metadata_overrides:
                    # The builder rewrites this product metadata with the package version.
                    continue
                if expected.get(target_relative) != hashlib.sha256(source.read_bytes()).hexdigest():
                    stale_sources.append(target_relative)
            if stale_sources:
                raise RuntimeError("Existing package snapshot differs from current source hashes: "
                                   + ",".join(sorted(stale_sources)))
            with zipfile.ZipFile(archive) as zipped:
                archive_names = set(zipped.namelist())
                expected_names = set(expected) | {"pmt-package.json"}
                if archive_names != expected_names:
                    raise RuntimeError("Existing package archive file list differs from its manifest")
                for name, digest in expected.items():
                    if hashlib.sha256(zipped.read(name)).hexdigest() != digest:
                        raise RuntimeError("Existing package archive failed file hash verification")
                if hashlib.sha256(zipped.read("pmt-package.json")).hexdigest() != \
                        hashlib.sha256(package_manifest_path.read_bytes()).hexdigest():
                    raise RuntimeError("Existing package archive manifest differs from its directory")
            products[product] = {"directory": str(package), "zip": str(archive),
                "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "manifest_sha256": hashlib.sha256(package_manifest_path.read_bytes()).hexdigest()}
        build = {"version": version, "core_version": __version__,
                 "schema_version": SCHEMA_VERSION, "output": str(final_root), "products": products}
    else:
        build = builder.build_plugins(target_root, version=version, source_root=ROOT)
    relative_output = Path(build["output"]).resolve().relative_to(ROOT).as_posix()
    products = {name: {"zip_sha256": values["zip_sha256"],
                       "manifest_sha256": values["manifest_sha256"],
                       "file_count": len(json.loads((Path(values["directory"]) / "pmt-package.json").read_text(encoding="utf-8"))["files"])}
                for name, values in build["products"].items()}
    smoke_root = ROOT / ".pmt-test" / ("f15-package-smoke-" + uuid.uuid4().hex[:12])
    smoke_root.mkdir(parents=True, exist_ok=False)
    smoke: dict[str, Any] = {}
    smoke_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH")
                 if key in os.environ}
    for product, values in build["products"].items():
        product_root = Path(values["directory"])
        data_root, config_root = smoke_root / (product + "-data"), smoke_root / (product + "-config")
        request = {"protocol_version": 1, "operation": "setup", "request_id": str(uuid.uuid4()),
            "actor": "main", "session_id": "f15-package-snapshot-" + product,
            "payload": {"product": "cli"}}
        completed = subprocess.run([sys.executable, str(product_root / "scripts" / "pmt.py"),
            "--data-root", str(data_root), "--config-root", str(config_root)],
            input=canonical_json(request), cwd=smoke_root, env=smoke_env,
            capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
        if completed.returncode != 0 or completed.stdout.count("\n") != 1:
            raise RuntimeError(f"Unpacked {product} package CLI smoke failed (exit={completed.returncode})")
        envelope = json.loads(completed.stdout)
        result = envelope.get("result") if isinstance(envelope, dict) else None
        if (not isinstance(result, dict) or envelope.get("ok") is not True
                or result.get("core_version") != build["core_version"]
                or result.get("schema_version") != build["schema_version"]):
            raise RuntimeError(f"Unpacked {product} package failed core/schema protocol verification")
        database = data_root / "pmt.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            stored_schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            db_id = conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()
        if (not stored_schema or int(stored_schema[0]) != build["schema_version"] or not db_id):
            raise RuntimeError(f"Unpacked {product} package failed persisted schema/identity verification")
        smoke[product] = {"status": "passed", "core_version": result["core_version"],
            "schema_version": result["schema_version"], "db_identity_created": True,
            "stdout_json_envelopes": 1, "cwd": "unrelated isolated directory"}
    manifest = {"schema_version": 1, "kind": "phase3-f15-final-package-snapshot",
        "package_version": build["version"], "core_version": build["core_version"],
        "db_schema": build["schema_version"], "output": relative_output,
        "products": products, "unpacked_cli_smoke": smoke,
        "source_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=False).stdout.strip() or None,
        "source_sha256": {relative: _sha256((ROOT / relative).read_bytes()) for relative in (
            "pyproject.toml", "src/pmt/__init__.py", "src/pmt/db.py",
            "src/pmt/phase2_common.py", "src/pmt/resources.py", "src/pmt/hosted_runtime.py",
            "src/pmt/hosted_files.py", "scripts/build_plugins.py")}}
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    target = Path(output_dir).resolve() / "f15-package-snapshot.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {"status": "built_immutable_snapshot", "manifest_ref": target.resolve().relative_to(ROOT).as_posix(),
            "manifest_sha256": manifest["manifest_sha256"], "products": products}


def capture_traceability_sources(*, output_dir: Path = ACCEPTANCE_EVIDENCE) -> dict[str, Any]:
    """Fingerprint currently present specs/modules/tests without running them."""
    files = {relative: _sha256((ROOT / relative).read_bytes()) for relative in TRACEABILITY_SOURCES
             if (ROOT / relative).is_file()}
    absent = [relative for relative in TRACEABILITY_SOURCES if not (ROOT / relative).is_file()]
    git_status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
        capture_output=True, text=True, check=False).stdout.splitlines()
    manifest = {"schema_version": 1, "kind": "phase3-plan-test-source-hashes",
        "recorded_on": utc_now(), "source_commit": subprocess.run(["git", "rev-parse", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip() or None,
        "source_tree": {"dirty": bool(git_status), "entry_count": len(git_status)},
        "source_sha256": files, "source_not_present": absent}
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "traceability-source-hashes.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                      encoding="utf-8")
    try:
        reference = target.relative_to(ROOT).as_posix()
    except ValueError:
        reference = target.name
    return {"source_count": len(files), "absent_count": len(absent), "manifest_ref": reference,
            "manifest_sha256": manifest["manifest_sha256"], "dirty": bool(git_status)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true", help="report local operation readiness without running a scenario")
    parser.add_argument("--capture-partial-legacy", action="store_true",
                        help="replay the frozen changed graph through unchanged Phase-2 validation and full rendering")
    parser.add_argument("--acceptance", action="store_true",
                        help="run isolated F15 package, hosted-client, and HTTP backup/restore tests")
    parser.add_argument("--source-stable", action="store_true",
                        help="confirm upstream sources are stable before local-tier promotion or final snapshotting")
    parser.add_argument("--acceptance-output-dir", type=Path, default=ACCEPTANCE_EVIDENCE,
                        help="F15 status manifest directory; test data stays under .pmt-test")
    parser.add_argument("--build-final-package-snapshot", action="store_true",
                        help="build the immutable 0.3.0 plugin folders/ZIPs after all sources are stable")
    parser.add_argument("--traceability-sources", action="store_true",
                        help="record hashes of the plan, modules, and tests currently present")
    parser.add_argument("--output-dir", type=Path, default=EVIDENCE,
                        help="evidence output directory; never points at a user data root")
    args = parser.parse_args(argv)
    if args.build_final_package_snapshot and not args.source_stable:
        result = {"status": "blocked", "reason_code": "source_stability_confirmation_required"}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 3
    if args.build_final_package_snapshot:
        result = build_final_package_snapshot(output_dir=args.acceptance_output_dir)
        exit_code = 0
    elif args.traceability_sources:
        result = capture_traceability_sources(output_dir=args.acceptance_output_dir)
        exit_code = 0
    elif args.acceptance:
        result = run_f15_local_acceptance(output_dir=args.acceptance_output_dir,
                                          source_stable=args.source_stable)
        exit_code = result.get("exit_code", 1)
    elif args.capture_partial_legacy:
        result = capture_partial_change_baseline(output_dir=args.output_dir)
        exit_code = 0
    else:
        result = preflight()
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
