import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.execution import handle
from pmt.phase2_common import event
from pmt.util import canonical_json, utc_now


ROUTE = {"agent": "codex", "provider": "openai", "model": "test-model", "mode": "native",
         "max_concurrency": 3, "selection_reason": "verified fixture",
         "capability_ref": "fixture-cap", "actual_support": "verified_supported"}


def _req(operation, payload, session="session"):
    return {"request_id": str(uuid.uuid4()), "operation": operation, "actor": "test",
            "session_id": session, "payload": payload}


def _write(db, operation, payload, session="session"):
    req = _req(operation, payload, session)
    return db.run_request(req, lambda conn, request: handle(db, conn, request))


def _seed(db, tmp_path, *, scopes=None, n=2):
    ws = (tmp_path / "workspace").resolve()
    ws.mkdir(parents=True, exist_ok=True)
    scope_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4())
    now = utc_now()
    steps = [str(uuid.uuid4()) for _ in range(n)]
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)", (scope_id, "project", "p", now, now))
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) VALUES(?,?,?,0,?,'ready',?)", (artifact_id, scope_id, "hash", "directive", now))
        for i, step in enumerate(steps):
            body = {"criteria": []}
            conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (step, "step", scope_id, f"step{i}", "Planned", canonical_json(body), now, now))
            declared = scopes[i] if scopes is not None else [{"kind": "path", "workspace": str(ws), "resource": "src/module"}]
            conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) VALUES(?,?,1,'req-1','plan-1','worker','build',?,?,?,?,?,?)",
                         (step, artifact_id, str(ws), canonical_json(declared), "[]", "[]", now, now))
    return steps, str(ws), scope_id


def _enqueue(db, step):
    response, code = _write(db, "enqueue_execution", {"step_id": step, "route": ROUTE})
    assert code == 0, response
    return response["result"]


def _prepare(db, run_id, revision=1, session="session"):
    return _write(db, "prepare_execution", {"run_id": run_id, "expected_run_revision": revision}, session)


def test_overlapping_lock_waits_but_disjoint_paths_can_run(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    ws = str((tmp_path / "workspace").resolve())
    scopes = [[{"kind": "path", "workspace": ws, "resource": "src"}],
              [{"kind": "path", "workspace": ws, "resource": "src/module/file.py"}],
              [{"kind": "path", "workspace": ws, "resource": "docs"}]]
    steps, _, _ = _seed(db, tmp_path, scopes=scopes, n=3)
    queued = [_enqueue(db, step) for step in steps]
    first, code = _prepare(db, queued[0]["run_id"])
    assert code == 0 and first["result"]["state"] == "starting"
    overlap, code = _prepare(db, queued[1]["run_id"])
    assert code == 0 and overlap["result"]["waiting_reason"] == "scope_conflict"
    independent, code = _prepare(db, queued[2]["run_id"])
    assert code == 0 and independent["result"]["state"] == "starting"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks").fetchone()[0] == 2


def test_multiscope_conflict_never_partially_acquires(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    ws = str((tmp_path / "workspace").resolve())
    steps, _, _ = _seed(db, tmp_path, scopes=[
        [{"kind": "path", "workspace": ws, "resource": "free"}],
        [{"kind": "path", "workspace": ws, "resource": "busy"}],
        [{"kind": "path", "workspace": ws, "resource": "free"}, {"kind": "path", "workspace": ws, "resource": "busy/sub"}],
    ], n=3)
    q = [_enqueue(db, step) for step in steps]
    assert _prepare(db, q[1]["run_id"])[0]["result"]["state"] == "starting"
    result, code = _prepare(db, q[2]["run_id"])
    assert code == 0 and result["result"]["waiting_reason"] == "scope_conflict"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (q[2]["run_id"],)).fetchone()[0] == 0


def test_result_before_observation_is_durable_and_observation_cannot_regress(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    steps, _, _ = _seed(db, tmp_path, n=1)
    q = _enqueue(db, steps[0]); started, _ = _prepare(db, q["run_id"])
    actual_route = {**ROUTE, "mode": "subagent", "adapter_kind": "native"}
    result = {"directive_version": 1, "actual_route": actual_route, "criteria_results": [], "receipt_ref": "receipt-1",
              "stop_confirmed": True, "stop_evidence_refs": [str(uuid.uuid4())]}
    saved, code = _write(db, "submit_execution_result", {"run_id": q["run_id"], "expected_run_revision": 2, "result": result})
    assert code == 0 and saved["result"]["state"] == "review_pending"
    observation, code = _write(db, "observe_execution", {"run_id": q["run_id"], "expected_run_revision": 3, "observation": {"state": "running"}})
    assert code == 0 and observation["result"]["state"] == "review_pending"
    replay, code = _write(db, "submit_execution_result", {"run_id": q["run_id"], "expected_run_revision": 3, "result": result})
    assert code == 0 and replay["result"]["replayed"] is True
    changed, code = _write(db, "submit_execution_result", {"run_id": q["run_id"], "expected_run_revision": 3,
        "result": {**result, "receipt_ref": "other"}})
    assert code == 3 and changed["error"]["code"] == "execution_record_conflict"


def test_cancel_requires_reconcile_evidence_before_releasing_locks(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    steps, _, _ = _seed(db, tmp_path, n=1)
    q = _enqueue(db, steps[0]); _prepare(db, q["run_id"])
    cancel, code = _write(db, "request_execution_cancel", {"run_id": q["run_id"], "expected_run_revision": 2})
    assert code == 0 and cancel["result"]["state"] == "cancel_requested"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (q["run_id"],)).fetchone()[0] == 1
        actual_route = json.loads(conn.execute("SELECT route_json FROM execution_runs WHERE id=?", (q["run_id"],)).fetchone()[0])
    late, code = _write(db, "submit_execution_result", {"run_id": q["run_id"], "expected_run_revision": 3,
        "result": {"directive_version": 1, "actual_route": actual_route, "criteria_results": [], "receipt_ref": "late"}})
    assert code == 0 and late["result"]["state"] == "cancel_requested"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (q["run_id"],)).fetchone()[0] == 1
    unknown, code = _write(db, "reconcile_execution", {"run_id": q["run_id"], "expected_run_revision": 4,
        "stopped": False, "not_started": False, "evidence_refs": ["status-ref"]})
    assert code == 0 and unknown["result"]["scope_locks_retained"]
    canceled, code = _write(db, "reconcile_execution", {"run_id": q["run_id"], "expected_run_revision": 5,
        "stopped": True, "not_started": False, "actual_state": "canceled", "evidence_refs": ["exit-ref"]})
    assert code == 0 and canceled["result"]["state"] == "canceled"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (q["run_id"],)).fetchone()[0] == 0


def test_scope_extension_conflict_keeps_original_locks_without_new_subset(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    ws = str((tmp_path / "workspace").resolve())
    steps, _, _ = _seed(db, tmp_path, scopes=[
        [{"kind": "path", "workspace": ws, "resource": "mine"}],
        [{"kind": "path", "workspace": ws, "resource": "occupied"}],
    ])
    first, other = _enqueue(db, steps[0]), _enqueue(db, steps[1])
    _prepare(db, first["run_id"]); _prepare(db, other["run_id"])
    res, code = _write(db, "extend_execution_scopes", {"run_id": first["run_id"], "expected_run_revision": 2,
        "scopes": [{"kind": "path", "workspace": ws, "resource": "new"},
                   {"kind": "path", "workspace": ws, "resource": "occupied/child"}]})
    assert code == 0 and res["result"]["extended"] is False
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (first["run_id"],)).fetchone()[0] == 1


def test_two_independent_processes_compete_for_overlapping_scopes(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    steps, _, _ = _seed(db, tmp_path, n=2)
    queued = [_enqueue(db, step) for step in steps]
    checkout = Path(__file__).resolve().parents[1]
    barrier = tmp_path / "process-barrier"
    barrier.mkdir()
    code = ("import json,sys,time\nfrom pathlib import Path\nfrom pmt.db import Database\nfrom pmt.execution import handle\n"
            "d=Database(sys.argv[1],sys.argv[2])\nr=json.loads(sys.argv[3])\nb=Path(sys.argv[4])\nkey=r['payload']['run_id']\n"
            "(b/(key+'.ready')).write_text('ready')\ndeadline=time.monotonic()+20\n"
            "while not (b/'go').exists() and time.monotonic()<deadline:\n time.sleep(.01)\n"
            "x,c=d.run_request(r,lambda conn,req:handle(d,conn,req))\n"
            "print(json.dumps({'code':c,'result':x.get('result'),'error':x.get('error')}))")
    procs = []
    for item in queued:
        req = _req("prepare_execution", {"run_id": item["run_id"], "expected_run_revision": 1})
        env = {**os.environ, "PYTHONPATH": str(checkout / "src")}
        procs.append(subprocess.Popen([sys.executable, "-c", code, str(db.root), str(db.config_root), json.dumps(req), str(barrier)],
                                      cwd=checkout, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    import time
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not all((barrier / (item["run_id"] + ".ready")).exists() for item in queued):
        time.sleep(0.01)
    assert all((barrier / (item["run_id"] + ".ready")).exists() for item in queued)
    (barrier / "go").write_text("go")
    results = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sum(r["result"]["state"] == "starting" for r in results) == 1
    assert sum(r["result"].get("waiting_reason") == "scope_conflict" for r in results) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks").fetchone()[0] == 1


def test_retry_requires_confirmed_end_and_stops_after_two_retries(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    steps, _, _ = _seed(db, tmp_path, n=1)
    current = _enqueue(db, steps[0])
    for attempt in (1, 2, 3):
        started, code = _prepare(db, current["run_id"], revision=1)
        assert code == 0 and started["result"]["state"] == "starting"
        ended, code = _write(db, "reconcile_execution", {"run_id": current["run_id"],
            "expected_run_revision": 2, "stopped": False, "not_started": True,
            "evidence_refs": [f"not-started-{attempt}"]})
        assert code == 0 and ended["result"]["state"] == "failed"
        if attempt < 3:
            retry, code = _write(db, "retry_execution", {"run_id": current["run_id"],
                "expected_run_revision": 3, "reason": "transient"})
            assert code == 0 and retry["result"]["attempt"] == attempt + 1
            current = retry["result"]
        else:
            rejected, code = _write(db, "retry_execution", {"run_id": current["run_id"],
                "expected_run_revision": 3, "reason": "transient"})
            assert code == 3 and rejected["error"]["code"] == "retry_not_allowed"


def test_unknown_or_unsupported_route_blocks_before_scope_acquisition(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    steps, _, _ = _seed(db, tmp_path, n=1)
    route = {**ROUTE, "actual_support": "unknown"}
    queued, code = _write(db, "enqueue_execution", {"step_id": steps[0], "route": route})
    assert code == 0
    prepared, code = _prepare(db, queued["result"]["run_id"])
    assert code == 0 and prepared["result"]["state"] == "blocked"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scope_locks").fetchone()[0] == 0


def test_dot_path_covers_entire_workspace_and_route_modes_normalize(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    ws = str((tmp_path / "workspace").resolve())
    steps, _, _ = _seed(db, tmp_path, scopes=[
        [{"kind": "path", "workspace": ws, "resource": "."}],
        [{"kind": "path", "workspace": ws, "resource": "file.py"}],
    ])
    q1 = _enqueue(db, steps[0])
    q2 = _enqueue(db, steps[1])
    prepared, code = _prepare(db, q1["run_id"])
    assert code == 0 and prepared["result"]["intent"]["route"]["mode"] == "subagent"
    assert prepared["result"]["intent"]["route"]["adapter_kind"] == "native"
    conflict, code = _prepare(db, q2["run_id"])
    assert code == 0 and conflict["result"]["waiting_reason"] == "scope_conflict"


def test_global_execution_cap_applies_across_different_routes(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    ws = str((tmp_path / "workspace").resolve())
    scopes = [[{"kind": "path", "workspace": ws, "resource": f"dir{i}"}] for i in range(4)]
    steps, _, _ = _seed(db, tmp_path, scopes=scopes, n=4)
    queued = []
    for i, step in enumerate(steps):
        route = {**ROUTE, "agent": f"agent-{i}", "model": f"model-{i}", "max_concurrency": 8}
        response, code = _write(db, "enqueue_execution", {"step_id": step, "route": route})
        assert code == 0
        queued.append(response["result"])
    for item in queued[:3]:
        result, code = _prepare(db, item["run_id"])
        assert code == 0 and result["result"]["state"] == "starting"
    result, code = _prepare(db, queued[3]["run_id"])
    assert code == 0 and result["result"]["waiting_reason"] == "route_capacity"


def test_stale_step_result_is_preserved_but_cannot_complete(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    steps, _, _ = _seed(db, tmp_path, n=1)
    q = _enqueue(db, steps[0]); _prepare(db, q["run_id"])
    with db.write() as conn:
        body = json.loads(conn.execute("SELECT body_json FROM records WHERE id=?", (steps[0],)).fetchone()[0])
        body["invalidated"] = True
        conn.execute("UPDATE records SET body_json=?,revision=revision+1 WHERE id=?", (canonical_json(body), steps[0]))
    result = {"directive_version": 1, "actual_route": {**ROUTE, "mode": "subagent", "adapter_kind": "native"},
              "criteria_results": [], "receipt_ref": "late-receipt", "stop_confirmed": True,
              "stop_evidence_refs": ["exit-receipt"]}
    stored, code = _write(db, "submit_execution_result", {"run_id": q["run_id"],
        "expected_run_revision": 2, "result": result})
    assert code == 0 and stored["result"]["state"] == "review_pending"
    with db.connect() as conn:
        saved = conn.execute("SELECT result_json,intent_json FROM execution_runs WHERE id=?", (q["run_id"],)).fetchone()
        assert json.loads(saved[0]) == result
        assert json.loads(saved[1])["result_assessment"]["current_step_matches"] is False
        assert conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (q["run_id"],)).fetchone()[0] == 1
    req = _req("review_execution", {"run_id": q["run_id"], "expected_run_revision": 3,
        "directive_version": 1, "accepted": True, "verification_ids": [str(uuid.uuid4())],
        "integration_confirmed": True, "evidence_refs": ["integration-ref"]})
    req["actor"] = "main"
    response, code = db.run_request(req, lambda conn, request: handle(db, conn, request))
    assert code == 3 and response["error"]["code"] == "step_invalidated"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM execution_runs WHERE id=?", (q["run_id"],)).fetchone()[0] == "review_pending"


def test_observation_rejects_arbitrary_text_fields(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    steps, _, _ = _seed(db, tmp_path, n=1)
    q = _enqueue(db, steps[0]); _prepare(db, q["run_id"])
    rejected, code = _write(db, "observe_execution", {"run_id": q["run_id"],
        "expected_run_revision": 2, "observation": {"state": "running", "transcript": "private payload"}})
    assert code == 2 and rejected["error"]["code"] == "invalid_observation"
    accepted, code = _write(db, "observe_execution", {"run_id": q["run_id"],
        "expected_run_revision": 2, "observation": {"state": "running", "stage": "compile", "artifact_refs": []}})
    assert code == 0 and accepted["result"]["state"] == "starting"
