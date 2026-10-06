"""Measure isolated legacy and phase-four local resume paths.

The script records actual protocol requests, Git subprocesses, selected file
reads, F5 builds and detail pages. It does not call a model or external Host.
See docs/phase4/evidence/p4-r6-a-actual.md for the measurement contract.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from pmt.db import Database, SCHEMA_VERSION
from pmt.storage_config import configure_storage
from pmt.store import LocalStore
from pmt.util import canonical_json, fingerprint, new_id
from test_phase4_current_context import _continuity_request, _source_fixture
from test_phase3_reuse import _actual_definition


SCRIPT_VERSION = "p4-r6-a-measure-2"
TARGET_PATH = "src/target.py"
TARGET_INITIAL = b"def convert(value: int) -> int:\n    return value + 1\n"
TARGET_CHANGED = b"def convert(value: int) -> int:\n    return value + 2\n"
F5_BUDGET = {"max_bytes": 600, "max_lines": 50, "unit": "utf8"}
F5_DETAIL_BUDGET = {"max_bytes": 4096, "max_lines": 200}
R4_BUDGET = {"max_bytes": 65536, "max_lines": 400}


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _request(env, operation, payload, *, record_id=None, **fields):
    request = {"protocol_version": 1, "operation": operation, "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "payload": payload}
    if record_id is not None:
        request["record_id"] = record_id
    request.update(fields)
    return request


class Meter:
    def __init__(self, store, source_paths):
        self.store = store
        self.source_paths = {Path(value).resolve() for value in source_paths}
        self.calls = []
        self.manual_git_calls = []
        self.source_file_reads = 0
        self.source_file_read_bytes = 0
        self._original_path_read_bytes = None
        self._original_subprocess_run = None

    def __enter__(self):
        self._original_path_read_bytes = Path.read_bytes
        self._original_subprocess_run = subprocess.run

        def read_bytes(path, *args, **kwargs):
            value = self._original_path_read_bytes(path, *args, **kwargs)
            try:
                if Path(path).resolve() in self.source_paths:
                    self.source_file_reads += 1
                    self.source_file_read_bytes += len(value)
            except OSError:
                pass
            return value

        def run(command, *args, **kwargs):
            try:
                argv = list(command) if not isinstance(command, str) else command.split()
                if argv and Path(argv[0]).name.casefold() in {"git", "git.exe"}:
                    self.manual_git_calls.append({"tool": "git", "argv_count": len(argv)})
            except (TypeError, OSError):
                pass
            return self._original_subprocess_run(command, *args, **kwargs)

        Path.read_bytes = read_bytes
        subprocess.run = run
        return self

    def __exit__(self, _kind, _value, _traceback):
        Path.read_bytes = self._original_path_read_bytes
        subprocess.run = self._original_subprocess_run

    def call(self, request):
        request_wire = canonical_json(request).encode("utf-8")
        started = time.perf_counter()
        envelope, code = self.store.execute(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response_wire = canonical_json(envelope).encode("utf-8")
        self.calls.append({"operation": request["operation"], "exit_code": code,
            "request_bytes": len(request_wire), "response_bytes": len(response_wire),
            "response_lines": response_wire.count(b"\n") + 1, "elapsed_ms": round(elapsed_ms, 3)})
        if code != 0 or envelope.get("ok") is not True:
            raise RuntimeError({"operation": request["operation"], "exit_code": code,
                                "error": envelope.get("error")})
        return envelope.get("result")

    def git_diff(self, workspace, before, after, path):
        started = time.perf_counter()
        result = self._original_subprocess_run(["git", "-C", str(workspace), "diff", "--no-ext-diff",
            "--no-color", "--name-status", "-z", before, after, "--", path],
            capture_output=True, check=True)
        self.manual_git_calls.append({"tool": "git", "purpose": "bounded_name_status_diff",
            "exit_code": result.returncode, "stdout_bytes": len(result.stdout),
            "stdout_sha256": _sha_bytes(result.stdout),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
        return result.stdout

    def read_file(self, path):
        """Read one selected source file in the legacy path under the same meter."""
        return Path(path).read_bytes()

    def report(self, elapsed_ms, before_state, after_state, *, semantic=None):
        by_operation = {}
        for call in self.calls:
            bucket = by_operation.setdefault(call["operation"], {
                "count": 0, "request_bytes": 0, "response_bytes": 0,
                "response_lines": 0, "elapsed_ms": 0.0})
            bucket["count"] += 1
            for key in ("request_bytes", "response_bytes", "response_lines", "elapsed_ms"):
                bucket[key] += call[key]
        total_request = sum(call["request_bytes"] for call in self.calls)
        total_response = sum(call["response_bytes"] for call in self.calls)
        return {"operation_calls": len(self.calls), "calls_by_operation": by_operation,
            "request_bytes": total_request, "response_bytes": total_response,
            "protocol_bytes_total": total_request + total_response,
            "response_lines": sum(call["response_lines"] for call in self.calls),
            "operation_elapsed_ms": round(sum(call["elapsed_ms"] for call in self.calls), 3),
            "wall_elapsed_ms": round(elapsed_ms, 3), "git_calls": len(self.manual_git_calls),
            "git_observations": self.manual_git_calls,
            "instrumented_path_read_bytes_calls": self.source_file_reads,
            "instrumented_path_read_bytes": self.source_file_read_bytes,
            "unobserved_file_io_bytes": "unknown",
            "state_before": before_state, "state_after": after_state,
            "state_unchanged": before_state == after_state, "semantic": semantic or {}}


def _state(env):
    with env["db"].connect() as conn:
        run = conn.execute("SELECT id,step_id,state,revision,owner_session,stop_confirmed FROM execution_runs WHERE id=?",
                           (env["run_id"],)).fetchone()
        work = conn.execute("SELECT id,state,revision FROM records WHERE id=?", (env["work_id"],)).fetchone()
        step = conn.execute("SELECT id,state,revision FROM records WHERE id=?", (env["step_id"],)).fetchone()
        locks = [dict(row) for row in conn.execute(
            "SELECT kind,resource,owner_session FROM scope_locks WHERE run_id=? ORDER BY kind,resource",
            (env["run_id"],))]
        pointers = [dict(row) for row in conn.execute(
            "SELECT pointer_key,object_id,revision FROM continuity_pointers WHERE scope_id=? ORDER BY pointer_key",
            (env["project_id"],))]
        continuity = [dict(row) for row in conn.execute(
            "SELECT kind,COUNT(*) AS count FROM continuity_objects WHERE scope_id=? GROUP BY kind ORDER BY kind",
            (env["project_id"],))]
        phase3_objects = [dict(row) for row in conn.execute(
            "SELECT kind,id,revision,state,source_hash FROM phase3_objects WHERE scope_id=? "
            "ORDER BY kind,id", (env["project_id"],))]
        request_count = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    return {"run": dict(run), "work": dict(work), "step": dict(step),
            "scope_locks": locks, "pointers": pointers, "continuity_objects": continuity,
            "phase3_objects": phase3_objects, "request_count": request_count}


def _clone_env(env, destination):
    """Clone one exact isolated PMT starting state while sharing the frozen checkout."""
    destination = Path(destination)
    data_root, config_root = destination / "data", destination / "config"
    data_root.mkdir(parents=True)
    shutil.copytree(env["db"].config_root, config_root)
    shutil.copytree(env["db"].root, data_root, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("pmt.sqlite3", "pmt.sqlite3-wal", "pmt.sqlite3-shm"))
    with sqlite3.connect(str(env["db"].path)) as source, sqlite3.connect(
            str(data_root / "pmt.sqlite3")) as target:
        source.backup(target)
    db = Database(data_root, config_root)
    return dict(env) | {"db": db, "store": LocalStore(db)}


def _f5_request(env, operation, pin, *, max_bytes=600, cursor=None):
    payload = {"run_id": env["run_id"], "task_ref": {
            "task_id": env["item_id"], "step_id": env["step_id"], "run_id": env["run_id"]},
        "role": "lower", "expected_source": pin,
        "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "node_ids": [env["node_id"]],
        "budget": {"max_bytes": max_bytes, "max_lines": 50, "unit": "utf8"},
        "expected_run_revision": 1}
    record_id = env["work_id"]
    if operation == "read_context_detail":
        payload = {"context_id": env["context_id"], "cursor": cursor,
                   "max_bytes": F5_DETAIL_BUDGET["max_bytes"], "max_lines": F5_DETAIL_BUDGET["max_lines"]}
    return _request(env, operation, payload, record_id=record_id)


def _f5_legacy_path(env, meter, *, rebuild_index):
    selected_file = meter.read_file(env["repo"] / TARGET_PATH)
    context = meter.call(_request(env, "read_context", {"query": "", "limit": 50, "budget": 4500},
                                  record_id=env["work_id"]))
    execution = meter.call(_request(env, "read_execution", {"run_id": env["run_id"]},
                                    record_id=env["work_id"]))
    step = meter.call(_request(env, "read_step", {"step_id": env["step_id"]},
                               record_id=env["step_id"]))
    directive = meter.call(_request(env, "read_step_directive", {
        "step_id": env["step_id"], "run_id": env["run_id"]}, record_id=env["step_id"]))
    pin = meter.call(_request(env, "capture_source_pin", {"run_id": env["run_id"],
        "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"]}, record_id=env["work_id"]))["source_pin"]
    if rebuild_index:
        meter.call(_request(env, "rebuild_graph_index", {"run_id": env["run_id"],
            "repository_id": env["repo_id"], "workspace": str(env["repo"]),
            "relative_graph_path": env["graph_path"], "expected_source": pin}, record_id=env["work_id"]))
    built = meter.call(_f5_request(env, "build_task_context", pin))
    context_id = built["context_ref"]["id"]
    env["context_id"] = context_id
    pages = []
    cursor = built.get("detail_cursor")
    while cursor:
        page = meter.call(_f5_request(env, "read_context_detail", pin, cursor=cursor))
        pages.append(page["content"])
        cursor = page.get("next_cursor")
    section_ids = sorted({item.get("section_id") for item in built.get("included", [])
                          if isinstance(item, dict) and item.get("section_id")})
    private_facts = {"selected_file_sha256": _sha_bytes(selected_file),
        "selected_file_bytes": len(selected_file),
        "context_records": len((context or {}).get("records", [])),
        "decision_count": len((context or {}).get("decisions", [])),
        "run_state": (execution or {}).get("run", {}).get("state"),
        "run_revision": (execution or {}).get("run", {}).get("revision"),
        "step_state": (step or {}).get("state"),
        "directive_goal_sha256": _sha_bytes(canonical_json((directive or {}).get("directive", {}).get("goal")).encode()),
        "section_ids": section_ids, "detail_pages": len(pages),
        "detail_text_sha256": _sha_bytes("".join(pages).encode("utf-8")) if pages else None,
        "context_incomplete": built.get("incomplete") is True,
        "missing_required": built.get("missing_required", []),
        "omitted_required": built.get("omitted_required", [])}
    return {"pin": pin, "context_ref": built["context_ref"],
            "detail_cursor": built.get("detail_cursor"), "semantics": private_facts}


def _legacy_case(env, *, changed):
    before = _state(env)
    selected = [env["graph_path"], TARGET_PATH]
    with Meter(env["store"], [env["repo"] / item for item in selected]) as meter:
        started = time.perf_counter()
        old_head = env["before_head"]
        current_head = subprocess.run(["git", "-C", str(env["repo"]), "rev-parse", "--verify", "HEAD"],
            capture_output=True, check=True, text=True, encoding="utf-8").stdout.strip()
        if changed:
            meter.git_diff(env["repo"], old_head, current_head, TARGET_PATH)
        f5 = _f5_legacy_path(env, meter, rebuild_index=changed)
        elapsed_ms = (time.perf_counter() - started) * 1000
        after = _state(env)
        return {"metrics": meter.report(elapsed_ms, before, after, semantic=f5["semantics"]),
                "pin": f5["pin"], "context_ref": f5["context_ref"],
                "detail_cursor": f5["detail_cursor"]}


def _basis_payload(env, *, record_id=None):
    return {"run_id": env["run_id"], "repository_id": env["repo_id"],
        "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
        "task_id": env["work_id"], "inventory_paths": [env["graph_path"], TARGET_PATH]}


def _new_case(env, prior_basis, *, changed):
    before = _state(env)
    selected = [env["graph_path"], TARGET_PATH]
    with Meter(env["store"], [env["repo"] / item for item in selected]) as meter:
        started = time.perf_counter()
        facts = meter.call(_request(env, "read_current_facts", {}, record_id=env["work_id"]))
        selector = {"repository_id": prior_basis["source"]["repository_id"],
            "branch": prior_basis["source"]["branch"],
            "workspace_ref": prior_basis["source"]["workspace_ref"],
            "task_id": env["work_id"], "purpose": "current", "environment_id": None}
        checkpoint = meter.call(_request(env, "read_checkpoint", {"selector": selector},
                                         record_id=env["work_id"]))
        validation = meter.call(_request(env, "validate_basis", {
            **_basis_payload(env), "basis_ref": env["before_basis_ref"]}, record_id=env["work_id"]))
        change = links = assessment = applicability = None
        if changed:
            after_result = meter.call(_request(env, "capture_work_basis", _basis_payload(env),
                                               record_id=env["work_id"]))
            change = meter.call(_request(env, "collect_changes", {
                "run_id": env["run_id"], "before_basis_ref": env["before_basis_ref"],
                "after_basis_ref": after_result["basis_ref"],
                "paths": [TARGET_PATH], "task_id": env["work_id"],
                "expected_pointer_revision": 0}, record_id=env["work_id"]))
            after_basis_ref = after_result["basis_ref"]
            pin = after_result["source_pin"]
            meter.call(_request(env, "rebuild_graph_index", {"run_id": env["run_id"],
                "repository_id": env["repo_id"], "workspace": str(env["repo"]),
                "relative_graph_path": env["graph_path"], "expected_source": pin}, record_id=env["work_id"]))
            links = meter.call(_request(env, "build_implementation_links", {
                "run_id": env["run_id"], "basis_ref": after_basis_ref,
                "paths": [TARGET_PATH], "task_id": env["work_id"],
                "expected_pointer_revision": 0}, record_id=env["work_id"]))
            assessment = meter.call(_request(env, "assess_alignment", {
                "run_id": env["run_id"], "repository_id": env["repo_id"],
                "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
                "paths": [TARGET_PATH], "basis_ref": after_basis_ref,
                "change_ref": change["change_ref"], "index_ref": links["index_ref"]},
                record_id=env["work_id"]))
            applicability = meter.call(_request(env, "read_applicability", {
                "run_id": env["run_id"], "repository_id": env["repo_id"],
                "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
                "paths": ["."], "basis_ref": after_basis_ref,
                "definition": _actual_definition(), "target_id": env["work_id"],
                "command": ["python", "-m", "py_compile", TARGET_PATH],
                "inputs": {"target": TARGET_PATH}}, record_id=env["work_id"]))
            final_basis = meter.call(_request(env, "capture_work_basis", _basis_payload(env),
                                              record_id=env["work_id"]))
            current_basis_ref = final_basis["basis_ref"]
            pin = final_basis["source_pin"]
        else:
            current_basis_ref = env["before_basis_ref"]
            pin = env["before_pin"]
        overview = meter.call(_request(env, "compose_resume_overview", {
            "selector": selector, "role": "main", "budget": {"max_bytes": 12000, "max_lines": 120}},
            record_id=env["work_id"]))
        resume_payload = {"basis_ref": current_basis_ref, "run_id": env["run_id"],
            "task_ref": {"task_id": env["item_id"], "step_id": env["step_id"], "run_id": env["run_id"]},
            "role": "lower", "expected_source": pin, "repository_id": env["repo_id"],
            "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
            "inventory_paths": selected, "node_ids": [env["node_id"]],
            "budget": {**R4_BUDGET, "unit": "utf8"}, "f5_budget": F5_BUDGET,
            "expected_run_revision": 1}
        if change:
            resume_payload.update({"change_ref": change["change_ref"],
                "assessment_ref": assessment["assessment_ref"],
                "applicability_ref": applicability["applicability_ref"]})
        composed = meter.call(_request(env, "compose_task_resume", resume_payload,
                                       record_id=env["work_id"]))
        change_summary = composed.get("change_evidence", {})
        if not changed and change_summary.get("status") != "no_change_confirmed":
            raise RuntimeError({"scenario": "no_change", "checkpoint_ref": checkpoint.get("checkpoint_ref"),
                "change_evidence": change_summary, "reason": "current checkpoint selector did not resolve unchanged basis"})
        if changed and (change_summary.get("change_ref") != (change or {}).get("change_ref")
                or change_summary.get("assessment_ref") != (assessment or {}).get("assessment_ref")
                or change_summary.get("applicability_ref") != (applicability or {}).get("applicability_ref")):
            raise RuntimeError({"scenario": "related_change", "change_evidence": change_summary,
                "reason": "R4 did not retain the actual R2/R3 refs passed to it"})
        detail_pages, detail_text = [], []
        if composed.get("bundle_ref") and composed.get("detail_available"):
            cursor = None
            while True:
                page = meter.call(_request(env, "read_resume_detail", {
                    "bundle_ref": composed["bundle_ref"], "basis_ref": current_basis_ref,
                    "run_id": env["run_id"], "repository_id": env["repo_id"],
                    "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
                    "inventory_paths": selected, "cursor": cursor,
                    "max_bytes": 4096, "max_lines": 200}, record_id=env["work_id"]))
                nested = page.get("detail") or {}
                detail_pages.append(nested)
                detail_text.append(nested.get("content", ""))
                cursor = nested.get("next_cursor")
                if not cursor:
                    break
        elapsed_ms = (time.perf_counter() - started) * 1000
        after = _state(env)
        projection = overview.get("overview", {})
        sections = [{"section_id": item.get("section_id"),
                     "value_sha256": _sha_bytes(canonical_json(item.get("value")).encode("utf-8"))}
                    for item in (composed.get("context_ref"),) if isinstance(item, dict)]
        semantics = {"goal_visible": "Preserve direction" in canonical_json(projection)
                     or "Preserve direction" in "".join(detail_text),
            "active_execution": facts.get("facts", {}).get("active_execution", []),
            "next_action": composed.get("next_action"),
            "detail_pages": len(detail_pages),
            "detail_text_sha256": _sha_bytes("".join(detail_text).encode("utf-8")) if detail_text else None,
            "overview_complete": overview.get("complete") is True,
            "bundle_complete": composed.get("complete") is True,
            "basis_validation": validation.get("status"),
            "checkpoint_ref": checkpoint.get("checkpoint_ref"),
            "change_ref": change.get("change_ref") if change else None,
            "change_coverage": change.get("coverage") if change else None,
            "assessment_ref": assessment.get("assessment_ref") if assessment else None,
            "assessment_state": assessment.get("state") if assessment else None,
            "applicability_ref": applicability.get("applicability_ref") if applicability else None,
            "applicability_status": applicability.get("status") if applicability else None,
            "resume_change_status": composed.get("change_evidence", {}).get("status"),
            "resume_change_reason_codes": composed.get("change_evidence", {}).get("reason_codes", []),
            "link_index_ref": links.get("index_ref") if links else None,
            "link_coverage": links.get("coverage") if links else None,
            "sections": sections}
        return {"metrics": meter.report(elapsed_ms, before, after, semantic=semantics),
                "basis_ref": current_basis_ref, "context_ref": composed.get("context_ref"),
                "detail_pages": detail_pages}


def _add_changed_source(env):
    path = env["repo"] / TARGET_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TARGET_INITIAL)
    subprocess.run(["git", "-C", str(env["repo"]), "add", TARGET_PATH], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "commit", "-qm", "initial selected target"], check=True)
    with env["db"].write() as conn:
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (new_id(), env["run_id"], env["session"], "path",
                                               str(env["repo"]), TARGET_PATH, "2026-10-06T00:00:00Z"))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (new_id(), env["run_id"], env["session"], "workspace",
                                               str(env["repo"]), ".", "2026-10-06T00:00:00Z"))
    branch = subprocess.run(["git", "-C", str(env["repo"]), "symbolic-ref", "--quiet", "--short", "HEAD"],
        check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    configure_storage(env["db"].config_root, {"mode": "local", "expected_config_sha256": None,
        "workspace_mappings": [{"repository_id": env["repo_id"], "project_id": env["project_id"],
            "branch": branch, "branch_key_sha256": hashlib.sha256(branch.encode("utf-8")).hexdigest(),
            "local_root": str(env["repo"]), "relative_graph_path": env["graph_path"]}]})


def _seed_decision_and_basis(env, store):
    saved = store.execute({"protocol_version": 1, "operation": "save_decision", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "record_id": env["work_id"], "expected_revision": 1,
        "payload": {"decision_kind": "select", "decider": "measurement fixture", "title": "Direction",
            "option_id": "keep-direction", "content": "Preserve direction and current source",
            "reason": "Explicit controlled measurement fixture",
            "confirmation_source": "measurement fixture decision"}})
    if saved[1] != 0:
        raise RuntimeError(saved[0])
    with env["db"].connect() as conn:
        boundary_event = conn.execute("SELECT event_id FROM events WHERE event_type='decision_saved' "
            "AND record_id=? ORDER BY recorded_at DESC LIMIT 1", (env["work_id"],)).fetchone()[0]
    request = _continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "task_id": env["work_id"],
        "inventory_paths": [env["graph_path"], TARGET_PATH]})
    basis_result, code = store.execute(request)
    if code != 0:
        raise RuntimeError(basis_result)
    basis = basis_result["result"]
    checkpoint = store.execute(_continuity_request(env, "create_checkpoint", {
        "basis_ref": basis["basis_ref"], "boundary_event_id": boundary_event,
        "expected_pointer_revision": 0, "purpose": "current"}))
    if checkpoint[1] != 0:
        raise RuntimeError(checkpoint[0])
    return basis["basis_ref"], basis["basis"], boundary_event


def _source_ready(env, store, *, rebuild):
    pin_result = store.execute(_continuity_request(env, "capture_source_pin", {
        "run_id": env["run_id"], "repository_id": env["repo_id"],
        "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"]}))
    if pin_result[1] != 0:
        raise RuntimeError(pin_result[0])
    pin = pin_result[0]["result"]["source_pin"]
    if rebuild:
        result = store.execute(_continuity_request(env, "rebuild_graph_index", {
            "run_id": env["run_id"], "repository_id": env["repo_id"],
            "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
            "expected_source": pin}))
        if result[1] != 0:
            raise RuntimeError(result[0])
    return pin


def _run_report(env, *, output=None, temp_root=None):
    started_at = datetime.now(timezone.utc).isoformat()
    env["before_head"] = subprocess.run(["git", "-C", str(env["repo"]), "rev-parse", "--verify", "HEAD"],
        check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    initial_basis_ref, initial_basis_body, boundary_event = _seed_decision_and_basis(env, env["store"])
    env["before_basis_ref"] = initial_basis_ref
    env["before_basis_body"] = initial_basis_body
    env["before_pin"] = _source_ready(env, env["store"], rebuild=True)

    # Each path receives an exact DB/config copy and the same frozen checkout.
    # Cache writes by one path cannot warm the other path's measured start state.
    no_legacy_env = _clone_env(env, Path(temp_root) / "no-change-legacy")
    no_continuity_env = _clone_env(env, Path(temp_root) / "no-change-continuity")
    for isolated in (no_legacy_env, no_continuity_env):
        isolated.update(before_head=env["before_head"], before_basis_ref=initial_basis_ref,
            before_basis_body=initial_basis_body, before_pin=env["before_pin"])
    no_change_legacy = _legacy_case(no_legacy_env, changed=False)
    no_change_new = _new_case(no_continuity_env, initial_basis_body, changed=False)
    no_change_same_start = (no_change_legacy["metrics"]["state_before"] ==
                            no_change_new["metrics"]["state_before"])

    target = env["repo"] / TARGET_PATH
    target.write_bytes(TARGET_CHANGED)
    subprocess.run(["git", "-C", str(env["repo"]), "add", TARGET_PATH], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "commit", "-qm", "change selected target"], check=True)
    changed_head = subprocess.run(["git", "-C", str(env["repo"]), "rev-parse", "--verify", "HEAD"],
        check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()

    changed_legacy_env = _clone_env(env, Path(temp_root) / "related-change-legacy")
    changed_continuity_env = _clone_env(env, Path(temp_root) / "related-change-continuity")
    for isolated in (changed_legacy_env, changed_continuity_env):
        isolated.update(before_head=env["before_head"], before_basis_ref=initial_basis_ref,
            before_basis_body=initial_basis_body, before_pin=env["before_pin"])
    changed_legacy = _legacy_case(changed_legacy_env, changed=True)
    changed_new = _new_case(changed_continuity_env, initial_basis_body, changed=True)
    changed_same_start = (changed_legacy["metrics"]["state_before"] ==
                          changed_new["metrics"]["state_before"])

    no_change_relevant = {"head": env["before_head"], "branch": initial_basis_body["source"]["branch"],
        "inventory_hash": initial_basis_body["source"]["inventory_hash"],
        "run_id": env["run_id"], "run_revision": _state(env)["run"]["revision"],
        "work_revision": _state(env)["work"]["revision"], "role": "lower",
        "environment_id": env["db"].environment_id,
        "f5_budget": F5_BUDGET, "goal_hash": fingerprint(env["criteria"])}
    changed_relevant = {**no_change_relevant, "head": changed_head,
        "target_sha256": _sha_bytes(target.read_bytes()), "scenario": "related selected Python file change"}
    changed_semantics = changed_new["metrics"]["semantic"]
    changed_comparison_eligible = bool(changed_same_start and changed_semantics.get("change_ref")
        and changed_semantics.get("assessment_ref") and changed_semantics.get("applicability_ref")
        and changed_semantics.get("resume_change_status") == "change_requires_review")
    changed_reason = None if changed_comparison_eligible else (
        "same starting state or actual R2/R3/R4 changed-source refs were not established")

    source_files = ("src/pmt/continuity/current.py", "src/pmt/continuity/context.py",
        "src/pmt/hosted_context.py", "tests/test_phase4_current_context.py",
        "tests/test_phase4_hosted_context.py", "tests/test_phase4_hosted_alignment_actual.py")
    report = {"schema": "p4-r6-a-measurement-v1", "measurement_script": SCRIPT_VERSION,
        "measurement_started_at": started_at,
        "measurement_completed_at": datetime.now(timezone.utc).isoformat(),
        "source_head_before_change": env["before_head"], "source_head_after_change": changed_head,
        "project_id": env["project_id"], "repository_id": env["repo_id"],
        "work_id": env["work_id"], "item_id": env["item_id"], "step_id": env["step_id"],
        "run_id": env["run_id"], "boundary_event_id": boundary_event,
        "conditions": {"storage": "isolated local SQLite cloned from one exact fixture snapshot",
            "source": "same isolated real Git checkout held fixed during each paired re-entry",
            "remote_host": False, "model": None, "model_usage": "unknown_not_invoked",
            "legacy_path": "bounded prior re-entry: context, run, Step and directive queries, selected file hash, SourcePin and existing F5 context/detail",
            "continuity_path": "R1 current facts/checkpoint/basis, R2 change/index, R3 assessment/applicability and R4 overview/bundle/detail as applicable",
            "verification_definition_hash": fingerprint(env["criteria"]),
            "initial_basis_ref": initial_basis_ref,
            "target_path": TARGET_PATH, "graph_path": env["graph_path"],
            "source_selection": [env["graph_path"], TARGET_PATH],
            "source_change_cost": "fixture source commit is common setup before the changed-source pair and excluded from re-entry totals",
            "source_read_instrumentation": "Python Path.read_bytes calls on the selected graph/target paths and Git subprocess calls inside each measured path; Git internal reads and other file APIs are not byte-instrumented",
            "unmeasured_io": "Git internal I/O, open/read_text or OS-level reads outside Path.read_bytes, CPU/memory, model tokens and quality scores are unknown"},
        "scenarios": {
            "no_change": {"input_identity": no_change_relevant,
                "comparison_eligible": bool(no_change_same_start),
                "comparison_ineligible_reason": None if no_change_same_start else "start state diverged",
                "legacy": no_change_legacy["metrics"], "continuity": no_change_new["metrics"]},
            "related_change": {"input_identity": changed_relevant,
                "comparison_eligible": changed_comparison_eligible,
                "comparison_ineligible_reason": changed_reason,
                "legacy": changed_legacy["metrics"], "continuity": changed_new["metrics"]}},
        "comparability": {"no_change_same_goal_acceptance_source_role_environment": bool(no_change_same_start),
            "related_change_same_final_source_role_environment": bool(changed_same_start),
            "pairs_use_exact_database_starting_state": True,
            "cost_saving_rate_claimed": False,
            "quality_assessment": "deterministic required refs/state only; no model invoked"},
        "environment": {"python": sys.version.split()[0], "platform": sys.platform,
            "core_version": __import__("pmt").__version__, "db_schema": SCHEMA_VERSION,
            "graph_schema": 1, "script_sha256": _sha_bytes(Path(__file__).read_bytes()),
            "runtime_source_sha256": {name: _sha_bytes(ROOT.joinpath(name).read_bytes())
                                      for name in source_files},
            "fixture_source_sha256": _sha_bytes(Path(__file__).parents[1].joinpath(
                "tests/test_phase4_current_context.py").read_bytes())}}
    if output is not None:
        output_path = Path(output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                               encoding="utf-8")
        report["output_ref"] = output_path.as_posix()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-root", default=".pmt-test/p4-a-measure")
    parser.add_argument("--output", default="docs/phase4/evidence/p4-r6-a-actual.json")
    args = parser.parse_args(argv)
    base = Path(args.temp_root).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    # Keep the isolated run directory until evidence is inspected; this also
    # avoids Windows cleanup races if a failed test leaves SQLite diagnostics open.
    temporary = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    env = _source_fixture(temporary)
    _add_changed_source(env)
    report = _run_report(env, output=args.output, temp_root=base)
    print(json.dumps({"ok": True, "output": report.get("output_ref"),
                      "no_change_eligible": report["scenarios"]["no_change"]["comparison_eligible"],
                      "related_change_eligible": report["scenarios"]["related_change"]["comparison_eligible"]},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
