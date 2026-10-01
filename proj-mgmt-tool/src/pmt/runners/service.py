"""Product runner dispatch, durable receipts, and execution reconciliation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from ..errors import PmtError
from ..execution import handle as execution_handle
from ..phase2_common import event, persist_json_resource, replay, validate_scope
from ..service import response
from ..steps import handle as steps_handle
from ..util import canonical_json, utc_now

READ_OPERATIONS = frozenset()
WRITE_OPERATIONS = frozenset()
FILE_OPERATIONS = frozenset({"dispatch_execution", "poll_execution", "cancel_runner"})
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_SENSITIVE_TEXT = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*|\bsk-(?:ant-)?[A-Za-z0-9_-]{12,}|\b(?:api[_-]?key|token|password)\s*[:=]\s*[^\s,;]+")
_TERMINAL_HELPER = {"completed", "failed", "canceled", "unknown"}
_MODES = {"native", "subagent", "cli"}


def _fail(code, message, exit_code=2, retryable=False, details=None):
    raise PmtError(code, message, exit_code, retryable, details)


def _json(text, label, expected=dict):
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        _fail("stored_record_invalid", f"Stored {label} is invalid", 5)
    if not isinstance(value, expected):
        _fail("stored_record_invalid", f"Stored {label} has an invalid shape", 5)
    return value


def _run(conn, req, run_id):
    from ..execution.service import _get_run, _json as exec_json, _owned
    run = _get_run(conn, run_id)
    _owned(req, run)
    run["intent"] = exec_json(run["intent_json"], "run intent", dict)
    run["route"] = exec_json(run["route_json"], "route", dict)
    if run["state"] not in {"queued", "starting", "running", "review_pending", "cancel_requested",
                              "reconciling", "succeeded", "failed", "blocked", "canceled"}:
        _fail("invalid_transition", "Runner run state is unsupported", 3)
    if run["state"] in {"starting", "running", "review_pending", "cancel_requested", "reconciling"} and not conn.execute("SELECT 1 FROM scope_locks WHERE run_id=?", (run_id,)).fetchone():
        _fail("scope_not_owned", "Runner requires the run's confirmed scope locks", 3)
    step = conn.execute("SELECT * FROM step_specs WHERE step_id=?", (run["step_id"],)).fetchone()
    if not step or step["directive_version"] != run["directive_version"]:
        _fail("directive_version_conflict", "Run does not own the current Step directive", 3)
    validate_scope(None, conn, conn.execute("SELECT scope_id FROM records WHERE id=?", (run["step_id"],)).fetchone()[0])
    return run, dict(step)


def _safe_spool(db, run_id):
    from ..resources import _reject_links
    root = Path(db.root).resolve()
    spool = root / "runner-spool" / run_id
    if not spool.resolve().is_relative_to(root):
        _fail("runner_path_invalid", "Runner spool must remain inside the PMT data root")
    spool.mkdir(parents=True, exist_ok=True)
    _reject_links(spool)
    return spool


def _read_directive(db, req, run):
    request = {**req, "operation": "read_step_directive", "record_id": run["step_id"],
               "payload": {"step_id": run["step_id"], "run_id": run["id"]}}
    with db.connect() as conn:
        return steps_handle(db, conn, request)["directive"]


def _prompt(intent, directive):
    required = {"summary", "choices", "criteria_results", "tests", "evidence_refs", "unresolved_items"}
    criteria = intent.get("criteria")
    return ("Complete the approved PMT Step within its workspace and scope. Follow the directive below. "
            "Return one JSON object with fields " + ", ".join(sorted(required)) + ". choices must list findings "
            "with a reason and evidence reference when available. "
            "criteria_results must contain one item for each criteria ID, using outcome pass/fail/blocked/not_run. "
            "Include evidence references and the exact tests and exit codes actually observed. Never claim a test "
            "or evidence that was not observed.\n\nPMT execution reference: "
            + canonical_json({"job_id": intent["job_id"], "run_id": intent["run_id"],
                              "step_id": intent["step_id"], "parent_run_id": intent.get("parent_run_id"),
                              "role": intent["role"], "criteria": criteria,
                              "workspace": intent["workspace"], "scopes": intent["scopes"]})
            + "\n\nApproved directive:\n" + canonical_json(directive))


def _journal_id(run_id):
    return str(uuid.uuid5(uuid.UUID(run_id), "pmt-runner-dispatch"))


def _journal(db, run_id):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM operation_journal WHERE id=?", (_journal_id(run_id),)).fetchone()
    if not row:
        return None
    return {**dict(row), "body": _json(row["body_json"], "runner journal")}


def _store_journal(db, req, run_id, body, state):
    now = utc_now()
    with db.write() as conn:
        existed = conn.execute("SELECT 1 FROM operation_journal WHERE id=?", (_journal_id(run_id),)).fetchone()
        conn.execute("INSERT INTO operation_journal(id,kind,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET state=excluded.state,body_json=excluded.body_json,updated_at=excluded.updated_at",
                     (_journal_id(run_id), "runner", state, canonical_json(body), now, now))
        if not existed:
            event(conn, {**req, "payload": {}}, "runner.intent_dispatched", payload={
                "run_id": run_id, "runner_kind": body.get("runner_kind"), "outcome": state,
                "handle_ref": body.get("handle_ref")})


def _base_environment():
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
               "TEMP", "TMP", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return env


def _write_private_json(path, body):
    from ..resources import _reject_links
    _reject_links(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_json(body))
        stream.flush()
        os.fsync(stream.fileno())
    _reject_links(path.parent)
    os.replace(tmp, path)


def _helper_alive(state, spool):
    try:
        state_path = spool / "state.json"
        current = json.loads(state_path.read_text(encoding="utf-8"))
        heartbeat = float(current.get("heartbeat_epoch", 0))
        import time
        return time.time() - heartbeat <= 5.0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _launch_helper(cfg_path, prompt):
    flags = 0
    startupinfo = None
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    try:
        process = subprocess.Popen([sys.executable, "-m", "pmt.runners.supervisor", str(cfg_path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   cwd=str(Path(__file__).resolve().parents[3]), env=_base_environment(),
                                   close_fds=True, creationflags=flags, startupinfo=startupinfo,
                                   start_new_session=(os.name != "nt"))
    except OSError as exc:
        raise PmtError("runner_process_start_failed", "Runner supervisor could not be started", 3) from exc
    try:
        encoded = prompt.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            _fail("runner_prompt_too_large", "Runner prompt exceeds 1 MiB")
        process.stdin.write(encoded)
        process.stdin.close()
    except Exception:
        try:
            process.kill()
        except OSError:
            pass
        raise PmtError("runner_handoff_unknown", "Runner supervisor started but prompt handoff was not confirmed", 4, True)
    return process.pid


def _command(route):
    product = route.get("agent")
    model = route.get("model")
    if not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model) or model.startswith("-"):
        _fail("invalid_model", "Selected model name is invalid for a fixed CLI argument")
    if product == "codex":
        return "codex", ["exec", "--json", "--sandbox", "workspace-write", "-m", model, "-"]
    if product == "claude":
        return "claude", ["-p", "--output-format", "json", "--model", model]
    _fail("runner_unsupported", "Only Codex and Claude CLI adapters are available", 3)


def _request_identity(req, suffix):
    return str(uuid.uuid5(uuid.UUID(req["request_id"]), suffix))


def _attach_started(db, req, run, handle_body, journal_body):
    def finish(conn, request):
        result = execution_handle(db, conn, {**request, "operation": "attach_execution_handle",
            "payload": {"run_id": run["id"], "expected_run_revision": run["revision"], "handle": handle_body}})
        now = utc_now()
        conn.execute("UPDATE operation_journal SET state='active',body_json=?,updated_at=? WHERE id=?",
                     (canonical_json(journal_body), now, _journal_id(run["id"])))
        return {"run_id": run["id"], "job_id": run["job_id"], "state": result["state"],
                "revision": result["revision"], "handle_ref": handle_body["id"], "runner_kind": journal_body["runner_kind"]}
    return finish


def _read_state(spool):
    try:
        return json.loads((spool / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _read_receipt(spool):
    try:
        return json.loads((spool / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _new_main_action(db, req, run, spool):
    route = run["route"]
    if route.get("mode") not in {"native", "subagent"}:
        _fail("invalid_route", "Main-action native dispatch requires a native selected route", 3)
    if route.get("actual_support") != "verified_supported" or route.get("agent") != req.get("source", {}).get("product", route.get("agent")):
        _fail("native_support_unconfirmed", "Native dispatch needs this product session's verified support", 3)
    action = {"kind": "invoke_native_subagent", "parent_run_id": req.get("payload", {}).get("parent_run_id"),
              "job_id": run["job_id"], "run_id": run["id"], "step_id": run["step_id"],
              "agent": route["agent"], "provider": route["provider"], "model": route["model"],
              "directive_ref": run["intent"]["directive_ref"],
              "instruction": "Read the approved directive with read_step_directive using this run_id, then invoke "
                             "the native subagent. Return its real handle and result through the stated operations.",
              "return_contract": {"native_handle": "attach_execution_handle after actual invocation",
                                  "result_operation": "submit_execution_result after actual completion",
                                  "preserve_parent_child_run_ids": True}}
    body = {"runner_kind": "native", "state_ref": str(spool / "state.json"), "main_action_pending": True,
            "capability_ref": route.get("capability_ref"), "model": route["model"],
            "dispatch_request_id": req["request_id"]}
    _store_journal(db, req, run["id"], body, "main_action_pending")
    return {"run_id": run["id"], "job_id": run["job_id"], "state": "starting",
            "main_action": action, "main_action_pending": True}


def _request_committed(db, request_id):
    if not isinstance(request_id, str):
        return False
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM requests WHERE request_id=?", (request_id,)).fetchone() is not None


def _resume_existing_external(db, req, run, body, spool):
    state = _read_state(spool)
    receipt = _read_receipt(spool)
    handle_body = body.get("handle")
    if receipt:
        status = receipt.get("state") if receipt.get("state") in _TERMINAL_HELPER else "unknown"
        if not handle_body:
            supervisor_pid = (state or {}).get("supervisor_pid") or receipt.get("supervisor_pid")
            handle_body = {"id": str(uuid.uuid5(uuid.UUID(run["id"]), "runner-handle")),
                           "runner_kind": body.get("runner_kind"),
                           "state_ref": body.get("state_ref"), "receipt_ref": body.get("receipt_ref")}
            if supervisor_pid:
                handle_body["supervisor_pid"] = supervisor_pid
            body["handle"] = handle_body
            if supervisor_pid:
                body["supervisor_pid"] = supervisor_pid
            _store_journal(db, req, run["id"], body, "completed")
        if handle_body and run["state"] == "starting":
            attach_req = {**req, "request_id": _request_identity(req, "recover-handle:" + handle_body["id"])}
            attached, code = db.run_request(attach_req, _attach_started(db, attach_req, run, handle_body, body))
            if code != 0:
                return attached, code
            if status == "unknown":
                _mark_reconciling(db, req, run, f"prior-receipt-unknown:{run['id']}")
                status = "reconciling"
            return db.run_request(req, lambda conn, request: {**attached["result"], "state": status,
                "receipt_ref": body.get("receipt_ref"), "already_dispatched": True})
        if status == "unknown":
            _mark_reconciling(db, req, run, f"prior-receipt-unknown:{run['id']}")
            return db.run_request(req, lambda conn, request: {"run_id": run["id"], "state": "reconciling",
                "blocked": True, "reason": "prior_dispatch_outcome_unknown_no_fallback"})
        return db.run_request(req, lambda conn, request: {"run_id": run["id"], "state": status,
            "receipt_ref": body.get("receipt_ref"), "already_dispatched": True})
    if state and _helper_alive(state, spool):
        if not handle_body and state.get("supervisor_pid"):
            handle_body = {"id": str(uuid.uuid5(uuid.UUID(run["id"]), "runner-handle")),
                           "runner_kind": body.get("runner_kind"), "supervisor_pid": state["supervisor_pid"],
                           "state_ref": body.get("state_ref"), "receipt_ref": body.get("receipt_ref")}
            body["handle"] = handle_body
            body["supervisor_pid"] = state["supervisor_pid"]
            _store_journal(db, req, run["id"], body, "active")
        if handle_body and run["state"] == "starting":
            attach_req = {**req, "request_id": _request_identity(req, "recover-handle:" + handle_body["id"])}
            attached, code = db.run_request(attach_req, _attach_started(db, attach_req, run, handle_body, body))
            if code != 0:
                return attached, code
            return db.run_request(req, lambda conn, request: attached["result"])
        return db.run_request(req, lambda conn, request: {"run_id": run["id"], "state": "running",
            "handle_ref": handle_body.get("id") if handle_body else None, "already_dispatched": True})
    if time.time() - body.get("dispatched_epoch", 0) < 10:
        return db.run_request(req, lambda conn, request: {"run_id": run["id"], "state": "running",
            "startup_pending": True, "already_dispatched": True})
    _mark_reconciling(db, req, run, f"prior-dispatch-unknown:{run['id']}")
    return db.run_request(req, lambda conn, request: {"run_id": run["id"], "state": "reconciling",
        "blocked": True, "reason": "prior_dispatch_outcome_unknown_no_fallback"})


def _dispatch(db, req):
    p = req.get("payload", {})
    run_id = p.get("run_id")
    if not isinstance(run_id, str):
        _fail("invalid_payload", "run_id is required")
    with db.connect() as conn:
        run, _step = _run(conn, req, run_id)
    if run["state"] != "starting":
        _fail("invalid_transition", "dispatch_execution requires a starting run", 3)
    route = run["route"]
    mode = route.get("mode")
    spool = _safe_spool(db, run_id)
    old = _journal(db, run_id)
    if old:
        body = old["body"]
        if body.get("runner_kind") == "native" or body.get("main_action_pending") is True:
            original_id = body.get("dispatch_request_id")
            if original_id and not _request_committed(db, original_id):
                _read_directive(db, req, run)
                result = _new_main_action(db, req, run, spool)
                return db.run_request(req, lambda conn, request: result)
            return db.run_request(req, lambda conn, request: {"run_id": run_id, "job_id": run["job_id"],
                "state": "starting", "awaiting_original_main_action": True,
                "original_dispatch_request_id": original_id, "main_action_must_not_be_reissued": True})
        return _resume_existing_external(db, req, run, body, spool)
    try:
        if mode not in _MODES:
            _fail("runner_unsupported", "Only native subagents and Codex/Claude CLI routes are supported", 3)
        if mode in {"native", "subagent"}:
            _read_directive(db, req, run)
            spool = _safe_spool(db, run_id)
            result = _new_main_action(db, req, run, spool)
            def save_native(conn, request):
                return result
            return db.run_request(req, save_native)
        if route.get("adapter_kind") == "sdk" or route.get("agent") == "opencode":
            _fail("runner_unsupported", "SDK and OpenCode routes are not supported", 3)
        if route.get("agent") not in {"codex", "claude"}:
            _fail("runner_unsupported", "Only Codex and Claude CLI adapters are available", 3)
        if route.get("auth_state") != "authenticated":
            _fail("cli_auth_unavailable", "The selected CLI lacks a verified authenticated capability", 3)
        runner_kind, command = _command(route)
        executable = shutil.which(runner_kind)
        if not executable:
            _fail("cli_not_installed", "Selected CLI executable is not installed", 3)
        adapter = runner_kind
    except PmtError as exc:
        _confirm_not_started(db, req, run, [f"runner-preflight:{run_id}"],
                             blocked=True, reason_code=exc.code)
        raise

    directive = _read_directive(db, req, run)
    prompt = _prompt(run["intent"], directive)
    config = {"run_id": run_id, "runner_kind": runner_kind, "adapter": adapter,
              "executable": executable,
              "command": command, "workspace": run["workspace"], "spool_root": str(spool), "model": route["model"],
              "provider": route["provider"], "agent": route["agent"],
              "state_path": str(spool / "state.json"), "receipt_path": str(spool / "receipt.json"),
              "stdout_path": str(spool / "stdout.raw"), "stderr_path": str(spool / "stderr.raw"),
              "output_path": str(spool / "output.json"),
              "criteria_ids": [x if isinstance(x, str) else x.get("id") for x in run["intent"].get("criteria", [])],
              "control_path": str(spool / "control.json")}
    _write_private_json(spool / "config.json", config)
    body = {"runner_kind": runner_kind, "adapter": adapter, "model": route["model"],
            "state_ref": str(spool / "state.json"), "receipt_ref": str(spool / "receipt.json"),
            "spool_ref": f"runner-spool/{run_id}", "dispatched_epoch": time.time(),
            "dispatched_at": utc_now()}
    _store_journal(db, req, run_id, body, "dispatching")
    try:
        supervisor_pid = _launch_helper(spool / "config.json", prompt)
    except PmtError as exc:
        if exc.code == "runner_process_start_failed":
            evidence = [f"runner-preflight:{run_id}"]
            _confirm_not_started(db, req, run, evidence, reason_code=exc.code)
        else:
            _mark_reconciling(db, req, run, f"runner-handoff-unknown:{run_id}")
        raise
    handle_body = {"id": str(uuid.uuid4()), "runner_kind": runner_kind,
                   "supervisor_pid": supervisor_pid, "state_ref": body["state_ref"],
                   "receipt_ref": body["receipt_ref"]}
    body["handle"] = handle_body
    body["supervisor_pid"] = supervisor_pid
    _store_journal(db, req, run_id, body, "active")
    return db.run_request(req, _attach_started(db, req, run, handle_body, body))


def _confirm_not_started(db, req, run, evidence, *, blocked=False, reason_code="runner_preflight_failed"):
    proof_ref = str(uuid.uuid5(uuid.UUID(req["request_id"]), "runner-preflight-proof:" + run["id"]))
    route = run["route"]
    proof = {"run_id": run["id"], "outcome": "blocked" if blocked else "failed",
             "reason_code": reason_code, "mode": route.get("mode"), "agent": route.get("agent"),
             "provider": route.get("provider"), "model": route.get("model"),
             "observed_at": utc_now(), "evidence_refs": [x for x in evidence if isinstance(x, str)]}
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT OR IGNORE INTO operation_journal(id,kind,state,body_json,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?)", (proof_ref, "runner_preflight", "not_started", canonical_json(proof), now, now))
        event(conn, {**req, "payload": {}}, "runner.preflight_observed", record_id=run["step_id"],
              payload={"run_id": run["id"], "outcome": proof["outcome"], "reason_code": reason_code,
                       "evidence_ref": proof_ref})
    def stop(conn, request):
        current, _ = _run(conn, request, run["id"])
        return execution_handle(db, conn, {**request, "operation": "reconcile_execution",
            "payload": {"run_id": run["id"], "expected_run_revision": current["revision"],
                        "stopped": False, "not_started": True, "actual_state": "blocked" if blocked else None,
                        "evidence_refs": [proof_ref]}})
    internal = {**req, "request_id": _request_identity(req, "runner-preflight-stop"),
                "operation": "reconcile_execution", "payload": {"run_id": run["id"]}}
    return db.run_request(internal, stop)


def _mark_reconciling(db, req, run, evidence_ref):
    internal = {**req, "request_id": _request_identity(req, "runner-reconcile:" + evidence_ref),
                "operation": "reconcile_execution", "payload": {"run_id": run["id"]}}
    def reconcile(conn, request):
        current, _ = _run(conn, request, run["id"])
        return execution_handle(db, conn, {**request, "operation": "reconcile_execution",
            "payload": {"run_id": run["id"], "expected_run_revision": current["revision"],
                        "stopped": False, "not_started": False, "evidence_refs": [evidence_ref]}})
    return db.run_request(internal, reconcile)


def _result_for(run, receipt, raw_text, receipt_ref):
    intent = run["intent"]
    criteria = intent.get("criteria", [])
    criterion_ids = [x if isinstance(x, str) else x.get("id") for x in criteria]
    reports = [{"criterion_id": item, "outcome": "not_run", "evidence_refs": [],
                "reason": "runner output is a model report and requires separate verification"}
               for item in criterion_ids]
    parsed = raw_text if isinstance(raw_text, dict) else None
    if parsed is not None and not _valid_report(parsed, criterion_ids):
        parsed = None
    summary = (f"Runner exited with code {receipt.get('exit_code')}; structured report "
               + ("was parsed" if parsed is not None else "could not be parsed")
               + "; criterion verification remains pending.")
    return {"directive_version": run["directive_version"], "actual_route": run["route"],
            "summary": summary, "criteria_results": reports,
            "receipt_ref": receipt_ref, "evidence_refs": [receipt_ref],
            "stop_confirmed": receipt.get("state") in {"completed", "failed", "canceled"},
            "stop_evidence_refs": [receipt_ref],
            "runner_observation": {"exit_code": receipt.get("exit_code"),
                                   "completed_at": receipt.get("completed_at"),
                                   "fixture": receipt.get("contract_fixture", False)},
            "model_report_valid": parsed is not None,
            "report_ref": receipt.get("report_ref")}


def _extract_text(raw, agent):
    try:
        if agent == "claude":
            value = json.loads(raw)
            return value.get("result", "") if isinstance(value, dict) else ""
        lines = []
        for line in raw.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("type") == "item.completed":
                value = item.get("item", {})
                if isinstance(value, dict) and value.get("type") == "agent_message" and isinstance(value.get("text"), str):
                    lines.append(value["text"])
            elif isinstance(item, dict) and item.get("type") == "response.output_text.done" and isinstance(item.get("text"), str):
                lines.append(item["text"])
        if lines:
            return lines[-1]
        # Accept a final structured response, never an entire unrecognized event trace.
        value = json.loads(raw)
        return raw if isinstance(value, dict) and isinstance(value.get("summary"), str) else ""
    except (ValueError, TypeError):
        return ""


def _scrub_sensitive(value):
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", value)
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in {"api_key", "token", "password", "authorization", "secret"}:
                clean[key_text] = "[REDACTED]"
            else:
                clean[key_text] = _scrub_sensitive(item)
        return clean
    return value


def _parse_report(text, criterion_ids):
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        report = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(report, dict) or not isinstance(report.get("summary"), str):
        return None
    if not isinstance(report.get("criteria_results"), list):
        return None
    ids = [x.get("criterion_id") for x in report["criteria_results"] if isinstance(x, dict)]
    if len(ids) != len(set(ids)) or set(ids) != set(criterion_ids):
        return None
    if any(not isinstance(item, dict) or item.get("outcome") not in {"pass", "fail", "blocked", "not_run"}
           or not isinstance(item.get("evidence_refs", []), list)
           for item in report["criteria_results"]):
        return None
    if any(not isinstance(report.get(key), list) for key in ("choices", "tests", "evidence_refs", "unresolved_items")):
        return None
    return _scrub_sensitive({key: report.get(key) for key in
        ("summary", "choices", "criteria_results", "tests", "evidence_refs", "unresolved_items")})


def _valid_report(report, criterion_ids):
    if not isinstance(report, dict) or not isinstance(report.get("summary"), str):
        return False
    rows = report.get("criteria_results")
    if not isinstance(rows, list):
        return False
    ids = [row.get("criterion_id") for row in rows if isinstance(row, dict)]
    if len(ids) != len(set(ids)) or set(ids) != set(criterion_ids):
        return False
    if any(not isinstance(row, dict) or row.get("outcome") not in {"pass", "fail", "blocked", "not_run"}
           or not isinstance(row.get("evidence_refs", []), list) for row in rows):
        return False
    return all(isinstance(report.get(key), list)
               for key in ("choices", "tests", "evidence_refs", "unresolved_items"))


def _load_json_artifact(db, artifact_id):
    from ..phase2_common import load_json_resource
    with db.connect() as conn:
        return load_json_resource(db, conn, artifact_id)


def _poll_reply(db, req, run_id, result, outcome):
    def record(conn, request):
        run, _ = _run(conn, request, run_id)
        event(conn, {**request, "payload": {}}, "runner.poll_observed", record_id=run["step_id"],
              payload={"run_id": run_id, "outcome": outcome,
                       "receipt_ref": result.get("receipt_ref")})
        return result
    return db.run_request(req, record)


def _cancel_reply(db, req, run_id, result, outcome):
    def record(conn, request):
        step = conn.execute("SELECT step_id FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        event(conn, {**request, "payload": {}}, "runner.cancel_observed",
              record_id=step[0] if step else None,
              payload={"run_id": run_id, "outcome": outcome,
                       "awaiting_stop_confirmation": result.get("awaiting_stop_confirmation", False)})
        return result
    return db.run_request(req, record)


def _poll(db, req):
    p = req.get("payload", {})
    run_id = p.get("run_id")
    if not isinstance(run_id, str):
        _fail("invalid_payload", "run_id is required")
    with db.connect() as conn:
        run, _step = _run(conn, req, run_id)
    journal = _journal(db, run_id)
    if not journal:
        _fail("runner_not_dispatched", "No durable runner dispatch exists", 3)
    if run["state"] in {"succeeded", "failed", "blocked", "canceled"}:
        result = {"run_id": run_id, "state": run["state"], "already_terminal": True,
                  "receipt_ref": journal["body"].get("receipt_ref")}
        return _poll_reply(db, req, run_id, result, "terminal")
    if journal["body"].get("runner_kind") == "native":
        if run["state"] == "starting":
            result = {"run_id": run_id, "state": "starting", "awaiting_main_action": True,
                      "main_action_pending": True}
        elif run["state"] == "running":
            result = {"run_id": run_id, "state": "running", "awaiting_native_result": True}
        elif run["state"] == "review_pending":
            result = {"run_id": run_id, "state": "review_pending", "result_persisted": True}
        elif run["state"] == "cancel_requested":
            native_handle = json.loads(run["handle_json"]) if run.get("handle_json") else None
            result = {"run_id": run_id, "state": "cancel_requested", "awaiting_native_cancellation": True,
                      "main_action": {"kind": "cancel_native_subagent", "native_handle": native_handle}}
        else:
            result = {"run_id": run_id, "state": run["state"], "scope_locks_retained": True}
        return _poll_reply(db, req, run_id, result, "native_waiting")
    spool = _safe_spool(db, run_id)
    receipt = _read_receipt(spool)
    if receipt is None:
        state = _read_state(spool)
        if state and _helper_alive(state, spool):
            result = {"run_id": run_id, "state": "running", "runner_kind": journal["body"].get("runner_kind"),
                      "last_observed_at": state.get("heartbeat_at")}
            return _poll_reply(db, req, run_id, result, "running")
        if time.time() - journal["body"].get("dispatched_epoch", 0) < 10:
            result = {"run_id": run_id, "state": "running", "startup_pending": True,
                      "runner_kind": journal["body"].get("runner_kind")}
            return _poll_reply(db, req, run_id, result, "startup_pending")
        for path in (spool / "stdout.raw", spool / "stderr.raw"):
            path.unlink(missing_ok=True)
        _mark_reconciling(db, req, run, f"runner-receipt-missing:{run_id}")
        result = {"run_id": run_id, "state": "reconciling", "scope_locks_retained": True,
                  "reason": "supervisor_receipt_missing_outcome_unknown"}
        return _poll_reply(db, req, run_id, result, "reconciling")

    body = journal["body"]
    parsed_report = None
    if body.get("receipt_ref") and body.get("receipt_persisted"):
        receipt_ref = body["receipt_ref"]
        if body.get("report_ref"):
            parsed_report = _load_json_artifact(db, body["report_ref"])
    else:
        try:
            safe_output = json.loads((spool / "output.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            safe_output = {"final_message": "", "model_report": None, "report_valid": False,
                           "stderr_note": {"present": False, "byte_count": 0, "classification": "none"}}
        final_message = _scrub_sensitive(safe_output.get("final_message", ""))
        parsed_report = safe_output.get("model_report")
        criterion_ids = [x if isinstance(x, str) else x.get("id") for x in run["intent"].get("criteria", [])]
        if not _valid_report(parsed_report, criterion_ids):
            parsed_report = None
        report_ref = None
        if parsed_report is not None:
            report_resource = persist_json_resource(db, req, parsed_report, run["intent"]["scope_id"],
                                                    "runner_model_report", run_id)
            report_ref = report_resource["artifact_id"]
        stderr_note = safe_output.get("stderr_note", {"present": False, "byte_count": 0, "classification": "none"})
        receipt["report_ref"] = report_ref
        output_value = {"run_id": run_id, "runner_kind": body.get("runner_kind"),
                        "final_message": _scrub_sensitive(final_message[:16000]),
                        "stderr_note": stderr_note, "receipt": receipt,
                        "report_valid": parsed_report is not None}
        if len(canonical_json(output_value).encode("utf-8")) > 8 * 1024 * 1024:
            _fail("runner_output_too_large", "Runner output exceeds internal artifact limit", 3)
        ref = persist_json_resource(db, req, output_value, run["intent"]["scope_id"], "runner_output", run_id)
        receipt_ref = ref["artifact_id"]
        body.update(receipt_ref=receipt_ref, report_ref=report_ref, receipt_persisted=True,
                    output_sha256=ref["sha256"])
        with db.write() as conn:
            conn.execute("UPDATE operation_journal SET state=?,body_json=?,updated_at=? WHERE id=?",
                         (receipt.get("state", "completed"), canonical_json(body), utc_now(), _journal_id(run_id)))
        for path in (spool / "stdout.raw", spool / "stderr.raw", spool / "output.json"):
            path.unlink(missing_ok=True)

    if receipt.get("state") == "unknown":
        _mark_reconciling(db, req, run, f"runner-outcome-unknown:{run_id}")
        result = {"run_id": run_id, "state": "reconciling", "receipt_ref": receipt_ref,
                  "scope_locks_retained": True}
        return _poll_reply(db, req, run_id, result, "reconciling")

    if receipt.get("state") == "canceled" or run["state"] == "cancel_requested":
        if run["state"] != "cancel_requested":
            cancel_req = {**req, "request_id": _request_identity(req, "cancel-mark:" + receipt_ref),
                          "operation": "request_execution_cancel", "payload": {"run_id": run_id}}
            def request_cancel(conn, request):
                current, _ = _run(conn, request, run_id)
                return execution_handle(db, conn, {**request, "operation": "request_execution_cancel",
                    "payload": {"run_id": run_id, "expected_run_revision": current["revision"]}})
            db.run_request(cancel_req, request_cancel)
        result = _result_for(run, receipt, parsed_report, receipt_ref)
        submit_req = {**req, "request_id": _request_identity(req, "cancel-late-result:" + receipt_ref),
                      "operation": "submit_execution_result", "payload": {"run_id": run_id}}
        def save_late_result(conn, request):
            current, _ = _run(conn, request, run_id)
            return execution_handle(db, conn, {**request, "operation": "submit_execution_result",
                "payload": {"run_id": run_id, "expected_run_revision": current["revision"], "result": result}})
        db.run_request(submit_req, save_late_result)
        reconcile_req = {**req, "request_id": _request_identity(req, "cancel-confirmed:" + receipt_ref),
                         "operation": "reconcile_execution", "payload": {"run_id": run_id}}
        def reconcile(conn, request):
            current, _ = _run(conn, request, run_id)
            return execution_handle(db, conn, {**request, "operation": "reconcile_execution",
                "payload": {"run_id": run_id, "expected_run_revision": current["revision"],
                            "stopped": True, "not_started": False, "actual_state": "canceled",
                            "evidence_refs": [receipt_ref]}})
        reconciled, code = db.run_request(reconcile_req, reconcile)
        if code != 0:
            return response(req.get("request_id"), error=reconciled.get("error")), code
        return _poll_reply(db, req, run_id, reconciled["result"], "canceled")

    if (receipt.get("exit_code") != 0 or receipt.get("state") == "failed") and run["state"] != "cancel_requested":
        internal = {**req, "request_id": _request_identity(req, "failure-reconcile:" + receipt_ref),
                    "operation": "reconcile_execution", "payload": {"run_id": run_id}}
        def fail_run(conn, request):
            current, _ = _run(conn, request, run_id)
            return execution_handle(db, conn, {**request, "operation": "reconcile_execution",
                "payload": {"run_id": run_id, "expected_run_revision": current["revision"],
                            "stopped": True, "not_started": False, "actual_state": "failed",
                            "evidence_refs": [receipt_ref]}})
        failed, code = db.run_request(internal, fail_run)
        if code != 0:
            return response(req.get("request_id"), error=failed.get("error")), code
        return _poll_reply(db, req, run_id, failed["result"], "failed")

    stdout_path = spool / "stdout.raw"
    receipt["report_ref"] = body.get("report_ref")
    result = _result_for(run, receipt, parsed_report, receipt_ref)
    internal = {**req, "request_id": _request_identity(req, "submit-result:" + receipt_ref),
                "operation": "submit_execution_result", "payload": {"run_id": run_id}}
    def submit(conn, request):
        current, _ = _run(conn, request, run_id)
        return execution_handle(db, conn, {**request, "operation": "submit_execution_result",
            "payload": {"run_id": run_id, "expected_run_revision": current["revision"], "result": result}})
    submitted, code = db.run_request(internal, submit)
    if code != 0 or not submitted.get("ok"):
        return response(req.get("request_id"), error=submitted.get("error")), code
    result = {**submitted["result"], "receipt_ref": receipt_ref, "report_ref": body.get("report_ref")}
    return _poll_reply(db, req, run_id, result, "result_persisted")


def _cancel(db, req):
    p = req.get("payload", {})
    run_id = p.get("run_id")
    if not isinstance(run_id, str):
        _fail("invalid_payload", "run_id is required")
    with db.connect() as conn:
        run, _step = _run(conn, req, run_id)
    journal = _journal(db, run_id)
    if not journal:
        _fail("runner_not_dispatched", "No runner dispatch exists", 3)
    body = journal["body"]
    spool = _safe_spool(db, run_id)
    receipt = _read_receipt(spool)
    if receipt and receipt.get("state") in {"completed", "failed", "canceled"}:
        result = {"run_id": run_id, "state": receipt.get("state"), "already_stopped": True}
        return db.run_request(req, lambda conn, request: result)
    if run["state"] not in {"cancel_requested", "canceled", "failed", "blocked", "succeeded"}:
        cancel_req = {**req, "request_id": _request_identity(req, "q5-cancel"),
                      "operation": "request_execution_cancel", "payload": {"run_id": run_id}}
        def mark_cancel(conn, request):
            current, _ = _run(conn, request, run_id)
            return execution_handle(db, conn, {**request, "operation": "request_execution_cancel",
                "payload": {"run_id": run_id, "expected_run_revision": current["revision"]}})
        db.run_request(cancel_req, mark_cancel)
    if body.get("runner_kind") == "native":
        handle = run.get("handle_json")
        handle_body = json.loads(handle) if handle else None
        result = {"run_id": run_id, "state": "cancel_requested", "awaiting_main_action": True,
                  "main_action": {"kind": "cancel_native_subagent", "run_id": run_id,
                                  "native_handle": handle_body}}
        return _cancel_reply(db, req, run_id, result, "native_main_action_required")
    _write_private_json(spool / "control.json", {"cancel_requested": True, "requested_at": utc_now()})
    result = {"run_id": run_id, "state": "cancel_requested",
              "awaiting_stop_confirmation": True, "scope_locks_retained": True}
    return _cancel_reply(db, req, run_id, result, "cancel_requested")


def execute_file(db, req):
    request_id = req.get("request_id") if isinstance(req, dict) else None
    operation = req.get("operation") if isinstance(req, dict) else None
    try:
        prior = replay(db, req)
        if prior:
            return prior
        if operation == "dispatch_execution":
            return _dispatch(db, req)
        if operation == "poll_execution":
            return _poll(db, req)
        if operation == "cancel_runner":
            return _cancel(db, req)
        _fail("operation_unsupported", "Unsupported runner operation")
    except PmtError as exc:
        return response(request_id, error=exc.as_dict()), exc.exit_code
    except OSError:
        exc = PmtError("runner_io_error", "Runner I/O failed", 4, True)
        return response(request_id, error=exc.as_dict()), exc.exit_code
    except Exception:
        exc = PmtError("runner_internal_error", "Runner operation failed unexpectedly", 5)
        return response(request_id, error=exc.as_dict()), exc.exit_code
