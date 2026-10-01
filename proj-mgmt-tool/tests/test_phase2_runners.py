import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

from pmt.db import Database
from pmt.execution import handle as execution_handle
from pmt.phase2 import execute
from pmt.phase2_common import load_json_resource, persist_json_resource
from pmt.runners.supervisor import _atomic_json
from pmt.util import canonical_json, utc_now


def _request(operation, payload, *, source=None, request_id=None):
    return {"protocol_version": 1, "request_id": request_id or str(uuid.uuid4()), "operation": operation,
            "actor": "main", "session_id": "session", "payload": payload,
            "source": source or {"product": "codex"}}


def _write(db, operation, payload):
    req = _request(operation, payload)
    return db.run_request(req, lambda conn, request: execution_handle(db, conn, request))


def _seed(db, tmp_path, route):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    scope_id, step_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (scope_id, "project", "runners", now, now))
    directive = {"purpose": "Q6 local contract", "goal": "Return a structured report", "method": "Follow the scope",
                 "non_goal": [], "inputs": [], "outputs": [], "tests": ["local supervisor contract"],
                 "logging": ["safe references only"], "context_refs": [],
                 "change_scope": {"add": [], "modify": ["src"], "delete": [], "forbidden": ["outside workspace"]}}
    ref = persist_json_resource(db, _request("seed", {}), directive, scope_id, "step_directive", step_id)
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,created_at,updated_at) "
                     "VALUES(?, 'step', ?, 'Runner test', 'Planned', '{}', ?, ?)", (step_id, scope_id, now, now))
        scopes = [{"kind": "path", "workspace": str(workspace), "resource": "src"}]
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,"
                     "role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
                     "VALUES(?,?,1,'req-1','plan-1','worker','prototype',?,?,?,?,?,?)",
                     (step_id, ref["artifact_id"], str(workspace), canonical_json(scopes), '["C1"]', '[]', now, now))
    queued, code = _write(db, "enqueue_execution", {"step_id": step_id, "route": route})
    assert code == 0, queued
    run_id = queued["result"]["run_id"]
    prepared, code = _write(db, "prepare_execution", {"run_id": run_id, "expected_run_revision": 1})
    assert code == 0 and prepared["result"]["state"] == "starting", prepared
    return run_id, step_id, scope_id


def _call(db, req):
    return execute(db, req)


def _attach(db, run_id, handle):
    with db.connect() as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (run_id,)).fetchone()[0]
    return _write(db, "attach_execution_handle", {"run_id": run_id, "expected_run_revision": revision,
                                                    "handle": handle})


def _route(mode="cli", agent="codex", **extra):
    return {"agent": agent, "provider": "openai" if agent == "codex" else "anthropic",
            "model": "local-fixture-model", "mode": mode, "max_concurrency": 2,
            "selection_reason": "verified local fixture capability", "actual_support": "verified_supported",
            "auth_state": "authenticated", "capability_ref": "fixture-capability", **extra}


def test_native_main_action_attach_and_poll_preserve_real_ids(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    route = _route(mode="native", agent="codex", model="gpt-6-luna")
    run_id, _step_id, scope_id = _seed(db, tmp_path, route)
    dispatch_req = _request("dispatch_execution", {"run_id": run_id})
    dispatched, code = _call(db, dispatch_req)
    assert code == 0 and dispatched["ok"]
    assert _call(db, dispatch_req) == (dispatched, 0)
    duplicate_dispatch, code = _call(db, _request("dispatch_execution", {"run_id": run_id}))
    assert code == 0 and duplicate_dispatch["result"]["awaiting_original_main_action"]
    assert duplicate_dispatch["result"]["main_action_must_not_be_reissued"]
    assert "main_action" not in duplicate_dispatch["result"]
    action = dispatched["result"]["main_action"]
    assert action["kind"] == "invoke_native_subagent"
    assert action["run_id"] == run_id and action["model"] == "gpt-6-luna"
    assert action["directive_ref"]["artifact_id"]
    assert "read_step_directive" in action["instruction"]
    with db.connect() as conn:
        run = conn.execute("SELECT state,handle_json FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        saved = conn.execute("SELECT response_json FROM requests WHERE request_id=?", (dispatch_req["request_id"],)).fetchone()[0]
    assert run["state"] == "starting" and run["handle_json"] is None
    assert "Follow the scope" not in saved

    native_handle = {"id": "native-handle-actual-fixture", "native_subagent_id": "child-run-actual-fixture"}
    attached, code = _attach(db, run_id, native_handle)
    assert code == 0 and attached["result"]["state"] == "running"
    polled, code = _call(db, _request("poll_execution", {"run_id": run_id}))
    assert code == 0 and polled["result"]["awaiting_native_result"]

    evidence = persist_json_resource(db, _request("seed-evidence", {}), {"local": True}, scope_id,
                                     "local_native_receipt", run_id)
    with db.connect() as conn:
        stored = conn.execute("SELECT route_json,revision FROM execution_runs WHERE id=?", (run_id,)).fetchone()
    result = {"directive_version": 1, "actual_route": json.loads(stored["route_json"]),
              "summary": "Native child returned; criterion verification remains pending.",
              "criteria_results": [{"criterion_id": "C1", "outcome": "not_run", "reason": "local fixture only",
                                    "evidence_refs": []}],
              "receipt_ref": evidence["artifact_id"], "evidence_refs": [evidence["artifact_id"]],
              "stop_confirmed": True, "stop_evidence_refs": [evidence["artifact_id"]]}
    submitted, code = _write(db, "submit_execution_result", {"run_id": run_id,
        "expected_run_revision": stored["revision"], "result": result})
    assert code == 0 and submitted["result"]["state"] == "review_pending"
    polled, code = _call(db, _request("poll_execution", {"run_id": run_id}))
    assert code == 0 and polled["result"]["result_persisted"]


def _local_supervisor(db, run_id, stdout_text, stderr_text, delay):
    spool = db.root / "runner-spool" / run_id
    spool.mkdir(parents=True, exist_ok=True)
    config = {"run_id": run_id, "runner_kind": "codex", "adapter": "codex", "agent": "codex",
              "provider": "openai", "model": "local-fixture-model", "workspace": str(spool),
              "spool_root": str(spool), "state_path": str(spool / "state.json"),
              "receipt_path": str(spool / "receipt.json"), "stdout_path": str(spool / "stdout.raw"),
              "stderr_path": str(spool / "stderr.raw"), "output_path": str(spool / "output.json"),
              "control_path": str(spool / "control.json"), "criteria_ids": ["C1"],
              "contract_fixture": True, "fixture_process": True,
              "fixture_stdout": stdout_text, "fixture_stderr": stderr_text, "fixture_delay": delay}
    config_path = spool / "config.json"
    _atomic_json(config_path, config)
    src = Path(__file__).resolve().parents[1] / "src"
    env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "USERPROFILE": os.environ.get("USERPROFILE", ""), "APPDATA": os.environ.get("APPDATA", ""),
           "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""), "TEMP": os.environ.get("TEMP", ""),
           "TMP": os.environ.get("TMP", ""), "PYTHONPATH": str(src)}
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    helper = subprocess.Popen([sys.executable, "-m", "pmt.runners.supervisor", str(config_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             cwd=str(src.parent), env=env, creationflags=flags,
                             start_new_session=(os.name != "nt"))
    helper.stdin.write(b"local supervisor fixture input")
    helper.stdin.close()
    return helper, spool


def _journal_local_runner(db, run_id, helper_pid, spool):
    now = utc_now()
    handle = {"id": f"local-supervisor-{helper_pid}", "runner_kind": "codex",
              "supervisor_pid": helper_pid, "state_ref": str(spool / "state.json"),
              "receipt_ref": str(spool / "receipt.json")}
    body = {"runner_kind": "codex", "adapter": "codex", "model": "local-fixture-model",
            "state_ref": handle["state_ref"], "receipt_ref": handle["receipt_ref"],
            "spool_ref": f"runner-spool/{run_id}", "handle": handle,
            "supervisor_pid": helper_pid, "dispatched_epoch": time.time() - 30,
            "dispatched_at": now}
    with db.write() as conn:
        conn.execute("INSERT INTO operation_journal(id,kind,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (str(uuid.uuid5(uuid.UUID(run_id), "pmt-runner-dispatch")), "runner", "completed",
                      canonical_json(body), now, now))
    attached, code = _attach(db, run_id, handle)
    assert code == 0 and attached["result"]["state"] == "running"


def test_local_supervisor_receipt_restart_duplicate_poll_and_cancel_tree(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    report = {"summary": "Local fixture report", "choices": [{"name": "local", "reason": "fixture"}],
              "criteria_results": [{"criterion_id": "C1", "outcome": "pass", "evidence_refs": ["self-report"]}],
              "tests": [{"name": "fixture", "exit_code": 0}], "evidence_refs": ["self-report"],
              "unresolved_items": []}
    codex_event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}}
    normal_run, _step_id, _scope_id = _seed(db, tmp_path / "normal", _route())
    helper, spool = _local_supervisor(db, normal_run, json.dumps(codex_event) + "\n", "controlled local stderr", 0.1)
    _journal_local_runner(db, normal_run, helper.pid, spool)
    normal_poll_request = _request("poll_execution", {"run_id": normal_run})
    end = time.time() + 10
    while helper.poll() is None and time.time() < end:
        time.sleep(0.05)
    assert helper.wait(timeout=2) == 0
    first, code = _call(db, normal_poll_request)
    assert code == 0 and first["result"]["state"] == "review_pending", first
    replay, code = _call(db, normal_poll_request)
    assert code == 0 and replay == first
    with db.connect() as conn:
        saved = conn.execute("SELECT result_json,stop_confirmed FROM execution_runs WHERE id=?", (normal_run,)).fetchone()
        body = json.loads(conn.execute("SELECT body_json FROM operation_journal WHERE id=?",
            (str(uuid.uuid5(uuid.UUID(normal_run), "pmt-runner-dispatch")),)).fetchone()[0])
        report_value = load_json_resource(db, conn, body["report_ref"])
        receipt_value = load_json_resource(db, conn, first["result"]["receipt_ref"])
    result = json.loads(saved["result_json"])
    assert saved["stop_confirmed"] == 1 and result["criteria_results"][0]["outcome"] == "not_run"
    assert result["runner_observation"]["fixture"] is True
    assert report_value["choices"][0]["name"] == "local"
    assert receipt_value["stderr_note"]["byte_count"] == len("controlled local stderr")
    assert not (spool / "stdout.raw").exists() and not (spool / "stderr.raw").exists()
    assert not (spool / "output.json").exists()

    cancel_run, _step_id, _scope_id = _seed(db, tmp_path / "cancel", _route())
    cancel_helper, cancel_spool = _local_supervisor(db, cancel_run, "", "controlled local stderr", 30)
    _journal_local_runner(db, cancel_run, cancel_helper.pid, cancel_spool)
    state_end = time.time() + 5
    child_pid = None
    while time.time() < state_end:
        try:
            child_pid = json.loads((cancel_spool / "state.json").read_text(encoding="utf-8")).get("child_pid")
        except (OSError, ValueError):
            pass
        if child_pid:
            break
        time.sleep(0.05)
    assert child_pid
    cancel_request = _request("cancel_runner", {"run_id": cancel_run})
    canceled, code = _call(db, cancel_request)
    assert code == 0 and canceled["result"]["awaiting_stop_confirmation"]
    assert cancel_helper.wait(timeout=10) == 0
    receipt = json.loads((cancel_spool / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["state"] == "canceled" and receipt["contract_fixture"] is True
    canceled_result, code = _call(db, _request("poll_execution", {"run_id": cancel_run}))
    assert code == 0 and canceled_result["result"]["state"] == "canceled"
    with db.connect() as conn:
        run = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?", (cancel_run,)).fetchone()
        assert conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (cancel_run,)).fetchone()[0] == 0
    assert run["state"] == "canceled" and run["stop_confirmed"] == 1


@pytest.mark.parametrize("route", [
    _route(mode="api", agent="openai"),
    _route(mode="cli", agent="opencode"),
    _route(mode="cli", agent="codex", adapter_kind="sdk"),
])
def test_api_sdk_and_opencode_routes_block_with_not_started_proof(tmp_path, monkeypatch, route):
    from pmt.runners import service as runner_service

    db = Database(tmp_path / str(uuid.uuid4()), tmp_path / str(uuid.uuid4()))
    run_id, _step_id, _scope_id = _seed(db, tmp_path / str(uuid.uuid4()), route)
    monkeypatch.setattr(runner_service.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("runner must not start"))
    result, code = _call(db, _request("dispatch_execution", {"run_id": run_id}))
    assert code == 3 and result["error"]["code"] == "runner_unsupported"
    with db.connect() as conn:
        run = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        proof = conn.execute("SELECT kind,state,body_json FROM operation_journal WHERE kind='runner_preflight' "
                             "AND json_extract(body_json,'$.run_id')=?", (run_id,)).fetchone()
        locks = conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (run_id,)).fetchone()[0]
    assert run["state"] == "blocked" and run["stop_confirmed"] == 1 and locks == 0
    assert proof and proof["state"] == "not_started"
    assert json.loads(proof["body_json"])["outcome"] == "blocked"


def test_codex_and_claude_commands_are_fixed_and_no_auth_status_command_exists():
    from pmt.runners import service as runner_service

    assert runner_service._command(_route(agent="codex")) == (
        "codex", ["exec", "--json", "--sandbox", "workspace-write", "-m", "local-fixture-model", "-"])
    assert runner_service._command(_route(agent="claude")) == (
        "claude", ["-p", "--output-format", "json", "--model", "local-fixture-model"])
    assert not hasattr(runner_service, "_preflight_cli")


def test_existing_uncertain_dispatch_is_recovered_before_cli_preflight(tmp_path, monkeypatch):
    from pmt.runners import service as runner_service

    db = Database(tmp_path / "data", tmp_path / "config")
    route = _route(auth_state="unknown")
    run_id, _step_id, _scope_id = _seed(db, tmp_path, route)
    spool = db.root / "runner-spool" / run_id
    spool.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    body = {"runner_kind": "codex", "adapter": "codex", "model": route["model"],
            "state_ref": str(spool / "state.json"), "receipt_ref": str(spool / "receipt.json"),
            "spool_ref": f"runner-spool/{run_id}", "dispatched_epoch": time.time() - 30,
            "dispatched_at": now, "dispatch_request_id": str(uuid.uuid4())}
    with db.write() as conn:
        conn.execute("INSERT INTO operation_journal(id,kind,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (str(uuid.uuid5(uuid.UUID(run_id), "pmt-runner-dispatch")), "runner", "dispatching",
                      canonical_json(body), now, now))
    monkeypatch.setattr(runner_service.shutil, "which", lambda *_args: pytest.fail("new CLI preflight must not run"))
    monkeypatch.setattr(runner_service.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not relaunch"))
    result, code = _call(db, _request("dispatch_execution", {"run_id": run_id}))
    assert code == 0 and result["result"]["state"] == "reconciling"
    with db.connect() as conn:
        run = conn.execute("SELECT state FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        lock_count = conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (run_id,)).fetchone()[0]
        preflights = conn.execute("SELECT count(*) FROM operation_journal WHERE kind='runner_preflight' "
                                  "AND json_extract(body_json,'$.run_id')=?", (run_id,)).fetchone()[0]
    assert run["state"] == "reconciling" and lock_count > 0 and preflights == 0


def test_only_final_agent_message_survives_event_stream():
    from pmt.runners.service import _extract_text
    events = [
        {"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "intermediate message"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final structured report"}},
    ]
    assert _extract_text("\n".join(json.dumps(event) for event in events), "codex") == "final structured report"
    assert _extract_text(json.dumps(events[0]), "codex") == ""


def test_concurrent_state_publication_uses_unique_staging_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    path = tmp_path / "state.json"
    barrier = threading.Barrier(4)
    def write_state(index):
        barrier.wait(timeout=5)
        for iteration in range(10):
            _atomic_json(path, {"writer": index, "iteration": iteration})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_state, range(4)))
    assert json.loads(path.read_text(encoding="utf-8"))["iteration"] == 9
    assert not list(tmp_path.glob(".pmt-state-*.tmp"))
