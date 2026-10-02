from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import subprocess
import uuid
from pathlib import Path

from pmt.efficiency.reuse import (
    KEY_SCHEMA_ID,
    build_reuse_key,
    impact_invalidates,
    invalidate_reuse,
    related_baseline_dimension,
    related_source_dimension,
    record_reuse_result,
    resolve_reuse,
)
from pmt.db import Database
from pmt.service import execute as service_execute
from pmt.store import LocalStore
from pmt.util import canonical_json, fingerprint, new_id, utc_now


def _id():
    return str(uuid.uuid4())


def _sha(char):
    return char * 64


def _definition(*, model_is_subject=False):
    required = ["input", "environment", "source", "baseline", "tool", "dependencies"]
    if model_is_subject:
        required.append("model")
    return {
        "definition_id": "tests.lint",
        "definition_version": "3",
        "meaning_sha256": _sha("a"),
        "key_schema_version": 1,
        "required_dimensions": required,
        "model_is_subject": model_is_subject,
        "applicability": {
            "field_dimensions": {
                "criteria": ["input"],
                "source_refs": ["source"],
                "environment_manifest": ["environment"],
                "method": ["tool", "dependencies"],
                "heading": ["document_coverage"],
            },
            "unknown_reason_dimensions": {
                "segment_manifest_absent": ["document_coverage"],
                "dependency_graph_unreadable": ["dependencies"],
            },
        },
    }


def _conditions(*, model=None, source=None, scope_level="project", project_dimension=None):
    scope_id = _id()
    if project_dimension is None:
        project_dimension = ({"state": "known", "id": scope_id} if scope_level == "project"
                             else {"state": "not_applicable", "reason_code": "repository_wide"})
    return {
        "scope": {"level": scope_level, "id": scope_id, "project_dimension": project_dimension},
        "target": {"kind": "step", "id": _id(), "version": "directive-4"},
        "dimensions": {
            "input": {"state": "known", "sha256": _sha("b")},
            "environment": {"state": "known", "sha256": _sha("c")},
            "source": {"state": "known", "sha256": source or _sha("d")},
            "baseline": {"state": "known", "sha256": _sha("e")},
            "tool": {"tool_id": "pytest", "version": "9.1", "capability_sha256": _sha("f")},
            "dependencies": {"state": "known", "sha256": _sha("1")},
        },
        "model_provenance": model or {"provider": "local", "model": "model-A", "version": "7"},
    }


def _candidate(key_result, *, scope_id=None, status="completed"):
    run_id, receipt_id, artifact_id = _id(), _id(), _id()
    scope_id = scope_id or key_result["key"]["scope"]["id"]
    return {
        "key_schema": KEY_SCHEMA_ID,
        "key": key_result["key"],
        "key_sha256": key_result["key_sha256"],
        "scope_id": scope_id,
        "accessible": True,
        "status": status,
        "run_ref": run_id,
        "origin_ref": {"kind": "run", "id": run_id} if status == "active" else
                      {"kind": "verification", "id": receipt_id},
        "run_state": "running" if status == "active" else None,
        "owner": {"actor": "worker", "session_id": "session-1"},
        "receipt": (None if status == "active" else
                    {"kind": "verification", "receipt_ref": receipt_id,
                     "outcome": "pass", "state": "valid"}),
        "later_failure": False if status == "completed" else None,
        "evidence_refs": ([] if status == "active" else
                          [{"id": artifact_id, "sha256": _sha("2")}]),
    }


def _evidence(candidate):
    ref = candidate["evidence_refs"][0]
    return {ref["id"]: {"state": "valid", "sha256": ref["sha256"],
                        "scope_id": candidate["scope_id"], "accessible": True}}


def test_f6_01_exact_key_and_verified_evidence_return_refs_without_result_body():
    definition = _definition()
    conditions = _conditions()
    key = build_reuse_key(definition, conditions)
    candidate = _candidate(key)
    payload = {"request_id": _id(), "event_id": _id(), "definition": definition,
               "conditions": conditions, "candidates": [candidate], "evidence_facts": _evidence(candidate)}
    result = resolve_reuse(payload)
    assert result["status"] == "reusable" and result["reusable"]
    assert result["decision"]["origin_ref"] == candidate["origin_ref"]
    assert "result_body" not in result and "output" not in result
    stored = record_reuse_result(payload)
    assert stored["status"] == "ready_to_store"
    assert stored["manifest"]["key_sha256"] == key["key_sha256"]
    assert "body" not in stored["manifest"] and "output" not in stored["manifest"]


def test_model_provenance_is_excluded_unless_model_itself_is_the_subject():
    definition = _definition()
    model_a_conditions = _conditions(model={"provider": "codex", "model": "A", "version": "7"})
    model_b_conditions = copy.deepcopy(model_a_conditions)
    model_b_conditions["model_provenance"] = {"provider": "claude", "model": "B", "version": "8"}
    first = build_reuse_key(definition, model_a_conditions)
    second = build_reuse_key(definition, model_b_conditions)
    assert first["key_sha256"] == second["key_sha256"]
    assert first["provenance"]["model"]["model"] != second["provenance"]["model"]["model"]

    subject = _definition(model_is_subject=True)
    cond_a = _conditions()
    cond_a["model_provenance"] = {"provider": "codex", "model": "A", "version": "7"}
    cond_a["dimensions"]["model"] = dict(cond_a["model_provenance"])
    cond_b = copy.deepcopy(cond_a)
    cond_b["model_provenance"] = {"provider": "codex", "model": "B", "version": "7"}
    cond_b["dimensions"]["model"] = dict(cond_b["model_provenance"])
    assert build_reuse_key(subject, cond_a)["key_sha256"] != build_reuse_key(subject, cond_b)["key_sha256"]


def test_unknown_required_dimension_is_not_a_wildcard_and_scope_na_is_explicit():
    definition = _definition()
    conditions = _conditions()
    conditions["dimensions"]["environment"] = {"state": "unknown", "reason_code": "not_captured"}
    unknown = build_reuse_key(definition, conditions)
    assert unknown["status"] == "unknown"
    assert "environment:not_captured" in unknown["unknown_dimensions"]
    assert build_reuse_key(definition, _conditions(scope_level="repository"))["status"] == "ready"
    not_explicit = _conditions(scope_level="repository", project_dimension={"state": "unknown"})
    assert build_reuse_key(definition, not_explicit)["status"] == "unknown"


def test_source_and_scope_changes_produce_distinct_keys():
    definition = _definition()
    base = build_reuse_key(definition, _conditions())
    changed_source = _conditions(source=_sha("3"))
    changed_source["scope"] = copy.deepcopy(base["key"]["scope"])
    changed_source["target"] = copy.deepcopy(base["key"]["target"])
    assert build_reuse_key(definition, changed_source)["key_sha256"] != base["key_sha256"]
    changed_scope = _conditions(scope_level="repository")
    assert build_reuse_key(definition, changed_scope)["key_sha256"] != base["key_sha256"]


def test_source_and_baseline_dimensions_use_only_declared_related_components():
    refs = {"node:a/criteria": _sha("a"), "node:b/summary": _sha("b")}
    source = related_source_dimension(refs, ["node:a/criteria"])
    unrelated = related_source_dimension({**refs, "node:b/summary": _sha("c")}, ["node:a/criteria"])
    relevant = related_source_dimension({**refs, "node:a/criteria": _sha("d")}, ["node:a/criteria"])
    assert source == unrelated
    assert source != relevant
    assert related_baseline_dimension(refs, ["node:a/criteria"])["state"] == "known"
    assert related_source_dimension(refs, ["node:missing/premise"])["state"] == "unknown"


def test_f6_02_active_exact_claim_is_shared_and_independent_events_remain_distinct():
    definition = _definition()
    conditions = _conditions()
    key = build_reuse_key(definition, conditions)
    candidate = _candidate(key, status="active")
    common = {"definition": definition, "conditions": conditions,
              "candidates": [candidate], "evidence_facts": {}}
    event_a, event_b = _id(), _id()
    a = resolve_reuse({"request_id": _id(), "event_id": event_a, **common})
    b = resolve_reuse({"request_id": _id(), "event_id": event_b, **common})
    assert a["status"] == b["status"] == "active"
    assert a["decision"]["original_run_ref"] == b["decision"]["original_run_ref"]
    assert a["event_id"] == event_a and b["event_id"] == event_b


def test_model_claim_later_failure_and_damaged_evidence_are_not_reusable():
    definition = _definition()
    conditions = _conditions()
    key = build_reuse_key(definition, conditions)
    candidate = _candidate(key)
    payload = {"request_id": _id(), "event_id": _id(), "definition": definition,
               "conditions": conditions, "candidates": [candidate], "evidence_facts": _evidence(candidate)}
    claim_only = copy.deepcopy(candidate)
    claim_only["receipt"] = {"kind": "model_claim", "receipt_ref": _id(), "outcome": "pass"}
    assert resolve_reuse({**payload, "candidates": [claim_only]})["status"] == "invalid"
    assert resolve_reuse({**payload, "candidates": [{**candidate, "later_failure": True}]})["status"] == "invalid"
    ref = candidate["evidence_refs"][0]
    broken = {ref["id"]: {"state": "valid", "sha256": _sha("3"),
                           "scope_id": conditions["scope"]["id"], "accessible": True}}
    assert resolve_reuse({**payload, "evidence_facts": broken})["status"] == "invalid"


def test_f6_03_old_key_schema_and_unknown_tool_or_tested_model_do_not_hit():
    definition = _definition()
    conditions = _conditions()
    key = build_reuse_key(definition, conditions)
    old = _candidate(key)
    old["key_schema"] = "pmt-reuse-key-v0"
    payload = {"request_id": _id(), "event_id": _id(), "definition": definition,
               "conditions": conditions, "candidates": [old], "evidence_facts": {}}
    assert resolve_reuse(payload)["status"] == "invalid"
    conditions["dimensions"]["tool"]["capability_sha256"] = None
    assert build_reuse_key(definition, conditions)["status"] == "unknown"
    subject = _definition(model_is_subject=True)
    no_model = _conditions()
    no_model.pop("model_provenance")
    assert build_reuse_key(subject, no_model)["status"] == "unknown"


def test_unrelated_f3_document_coverage_unknown_does_not_invalidate_this_key():
    definition = _definition()
    key = build_reuse_key(definition, _conditions())["key"]
    impact = {"completeness": "partial", "changed_fields": [],
              "unknowns": [{"reason_code": "segment_manifest_absent"}]}
    result = impact_invalidates(definition, impact, key)
    assert result["status"] == "unaffected" and not result["invalidated"]
    impact["completeness"] = "unknown"
    result = impact_invalidates(definition, impact, key)
    assert result["status"] == "unaffected" and not result["invalidated"]
    impact["unknowns"] = [{"reason_code": "dependency_graph_unreadable"}]
    result = impact_invalidates(definition, impact, key)
    assert result["status"] == "unknown" and result["invalidated"]


def test_related_f3_change_invalidates_but_unmapped_change_is_unknown():
    definition = _definition()
    key = build_reuse_key(definition, _conditions())["key"]
    relevant = impact_invalidates(definition, {"completeness": "known",
        "changed_fields": ["criteria"], "unknowns": []}, key)
    assert relevant["status"] == "invalid" and relevant["dimensions"] == ["input"]
    unknown = impact_invalidates(definition, {"completeness": "known",
        "changed_fields": ["new_unmapped_field"], "unknowns": []}, key)
    assert unknown["status"] == "unknown"


def test_invalidation_draft_is_pure_and_does_not_mutate_claim_or_request(tmp_path):
    definition = _definition()
    payload = {"definition": definition, "conditions": _conditions(),
               "impact_set": {"completeness": "known", "changed_fields": ["source_refs"], "unknowns": []}}
    snapshot = copy.deepcopy(payload)
    result = invalidate_reuse(payload)
    assert result["status"] == "invalid" and result["invalidated"] is True
    assert payload == snapshot
    assert not list(tmp_path.iterdir())


def test_undeclared_condition_dimension_is_rejected_instead_of_silently_ignored():
    definition = _definition()
    conditions = _conditions()
    conditions["dimensions"]["unmapped_baseline"] = {"state": "known", "sha256": _sha("5")}
    try:
        build_reuse_key(definition, conditions)
    except Exception as error:
        assert getattr(error, "code", None) == "reuse_dimension_undeclared"
    else:
        raise AssertionError("unmapped conditions must not silently produce a cacheable key")


def _actual_fixture(tmp_path, session="reuse-session"):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    db = Database(tmp_path / "data", tmp_path / "config")
    scope, target, step, job, run = new_id(), new_id(), new_id(), new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (scope, "project", "reuse-project", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (target, "work", scope, "Verified target", "In Progress",
                      canonical_json({"workspace": str(root), "criteria": []}), now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (step, "step", scope, target, "Run step", "In Progress", "{}", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job, step, "running", "{}", now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run, job, step, 1, "running", 1, session, 1, str(root), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run, session, "path", str(root), "src/target.py", now))
    return {"db": db, "root": root, "scope": scope, "target": target,
            "step": step, "run": run, "session": session}


def _actual_definition():
    return {"definition_id": "test.py_compile", "definition_version": "1",
            "meaning_sha256": _sha("9"), "key_schema_version": 1,
            "required_dimensions": ["input", "environment", "source"], "model_is_subject": False,
            "selectors": {"input": {"version": 1, "kind": "snapshot_inputs"},
                          "environment": {"version": 1, "kind": "environment_id"},
                          "source": {"version": 1, "kind": "workspace_files",
                                     "paths": ["src/target.py"]}},
            "applicability": {"field_dimensions": {}, "unknown_reason_dimensions": {}}}


def _actual_request(env, op, *, session=None, event_id=None, verification_id=None, definition=None):
    payload = {"definition": definition or _actual_definition(), "run_id": env["run"],
               "workspace": str(env["root"]), "paths": ["src/target.py"],
               "target_id": env["target"], "command": ["python", "-c", "pass"], "inputs": {"source": "target.py"},
               "event_id": event_id or _id()}
    if verification_id:
        payload["verification_id"] = verification_id
    return {"protocol_version": 1, "operation": op, "request_id": _id(),
            "actor": "reuse-test", "session_id": session or env["session"],
            "scope_id": env["scope"], "payload": payload}


def _local_execute(db, request):
    return LocalStore(db).execute(request)


def _verified_p2_result(env):
    from pmt.verification import _snapshot
    db = env["db"]
    with db.connect() as conn:
        snapshot, digest, reasons, _, _ = _snapshot(db, conn, env["target"], "test.py_compile", "1",
                                                    ["python", "-c", "pass"], {"source": "target.py"})
    assert not reasons
    evidence_id = _id()
    wire = b"compile evidence\n"
    relative = "resources/reuse-evidence.txt"
    artifact = db.root / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(wire)
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,retention_until,created_at) VALUES(?,?,?,?,?,'ready',NULL,?)",
                     (evidence_id, env["scope"], hashlib.sha256(wire).hexdigest(), len(wire), relative, now))
    response, code = service_execute(db, {"protocol_version": 1, "operation": "record_verification",
        "request_id": _id(), "actor": "reuse-test", "session_id": env["session"],
        "scope_id": env["scope"], "record_id": env["target"],
        "payload": {"definition_id": "test.py_compile", "definition_version": "1",
                    "command": ["python", "-c", "pass"], "inputs": {"source": "target.py"},
                    "outcome": "pass", "exit_code": 0, "evidence_ids": [evidence_id],
                    "before_fingerprint": digest}})
    assert code == 0 and response["ok"], response
    return response["result"]["verification_id"]


def test_actual_lookup_claim_then_exact_hit_without_verification_id(tmp_path):
    env = _actual_fixture(tmp_path)
    first, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse"))
    assert code == 0 and first["ok"], first
    assert first["result"]["status"] == "claimed"
    verification_id = _verified_p2_result(env)
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='review_pending',stop_confirmed=1 WHERE id=?", (env["run"],))
    recorded, code = _local_execute(env["db"], _actual_request(env, "record_reuse_result",
                                                                   verification_id=verification_id))
    assert code == 0 and recorded["ok"], recorded
    assert recorded["result"]["status"] == "recorded"
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='succeeded' WHERE id=?", (env["run"],))
        job2 = conn.execute("SELECT job_id FROM execution_runs WHERE id=?", (env["run"],)).fetchone()[0]
        conn.execute("UPDATE execution_jobs SET state='running' WHERE id=?", (job2,))
        run2 = new_id()
        now = utc_now()
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run2, job2, env["step"], 2, "running", 1, "next-session", 1,
                      str(env["root"]), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run2, "next-session", "path", str(env["root"]), "src/target.py", now))
    env["run"] = run2
    hit, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", session="next-session"))
    assert code == 0 and hit["ok"], hit
    assert hit["result"]["status"] == "reusable"
    assert hit["result"]["origin_ref"] == {"kind": "verification", "id": verification_id}
    assert hit["result"]["evidence_refs"] and "body" not in hit["result"]
    read_req = {"protocol_version": 1, "operation": "read_reuse_decision", "request_id": _id(),
                "actor": "another-project-reader", "session_id": "next-session", "scope_id": env["scope"],
                "payload": {"body_ref": hit["result"]["body_ref"], "run_id": run2,
                            "workspace": str(env["root"]), "paths": ["src/target.py"]}}
    fetched, code = _local_execute(env["db"], read_req)
    assert code == 0 and fetched["ok"], fetched
    assert fetched["result"]["manifest"]["key_sha256"] == hit["result"]["key_sha256"]
    assert fetched["result"]["condition_refs"] == hit["result"]["condition_refs"]
    tampered_ref = copy.deepcopy(read_req)
    tampered_ref["request_id"] = _id()
    tampered_ref["payload"]["body_ref"]["manifest_sha256"] = _sha("0")
    rejected, code = _local_execute(env["db"], tampered_ref)
    assert code == 4 and rejected["error"]["code"] == "reuse_decision_corrupt"
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='succeeded' WHERE id=?", (run2,))
        run3 = new_id()
        now = utc_now()
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run3, job2, env["step"], 3, "running", 1, "third-session", 1,
                      str(env["root"]), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run3, "third-session", "path", str(env["root"]), "src/target.py", now))
    (env["db"].root / "resources" / "reuse-evidence.txt").write_bytes(b"damaged evidence")
    env["run"] = run3
    damaged, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", session="third-session"))
    assert code == 0 and damaged["ok"], damaged
    assert damaged["result"]["status"] == "claimed"
    assert damaged["result"]["reason"] == "stored_receipt_no_longer_applicable"


def _parallel_actual_resolve(data_root, config_root, request, barrier, output):
    from pmt.db import Database
    from pmt.service import execute
    database = Database(data_root, config_root)
    barrier.wait(timeout=20)
    result, code = execute(database, request)
    output.put((code, result))


def test_actual_parallel_processes_create_one_claim_and_preserve_both_events(tmp_path):
    env = _actual_fixture(tmp_path)
    second_step, second_job, second_run, session = new_id(), new_id(), new_id(), "reuse-session-2"
    now = utc_now()
    with env["db"].write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (second_step, "step", env["scope"], env["target"], "Parallel step", "In Progress", "{}", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (second_job, second_step, "running", "{}", now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (second_run, second_job, second_step, 1, "running", 1, session, 1,
                      str(env["root"]), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), second_run, session, "path", str(env["root"]), "src/target.py", now))
    request_a = _actual_request(env, "resolve_reuse")
    request_b_env = {**env, "run": second_run}
    request_b = _actual_request(request_b_env, "resolve_reuse", session=session)
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    args = (str(env["db"].root), str(env["db"].config_root))
    processes = [ctx.Process(target=_parallel_actual_resolve,
                             args=(*args, request, barrier, output)) for request in (request_a, request_b)]
    for process in processes:
        process.start()
    results = [output.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert all(code == 0 and response["ok"] for code, response in results), results
    statuses = [response["result"]["status"] for _, response in results]
    assert statuses.count("claimed") == 1 and statuses.count("active") == 1
    active = next(response["result"] for _, response in results if response["result"]["status"] == "active")
    assert active["original_run_ref"] in {env["run"], second_run}
    assert set(active["owner_ref"]) == {"actor", "session_id"}
    with env["db"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM phase3_objects WHERE kind='reuse_claim'").fetchone()[0] == 1
        event_ids = {row[0] for row in conn.execute("SELECT event_id FROM events WHERE event_type IN ('efficiency.reuse_claimed','efficiency.reuse_resolved')")}
    assert event_ids == {request_a["payload"]["event_id"], request_b["payload"]["event_id"]}


def test_actual_selector_values_are_local_facts_and_unknowns_never_claim(tmp_path):
    env = _actual_fixture(tmp_path)
    source_def = _actual_definition()
    first, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", definition=source_def))
    assert code == 0 and first["result"]["status"] == "claimed"
    (env["root"] / "src" / "target.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", definition=source_def))
    assert code == 0 and changed["result"]["status"] == "claimed"
    assert changed["result"]["key_sha256"] != first["result"]["key_sha256"]

    missing_def = copy.deepcopy(source_def)
    (env["root"] / "src" / "target.py").unlink()
    missing, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", definition=missing_def))
    assert code == 0 and missing["result"]["status"] == "unknown"
    assert "source:" in " ".join(missing["result"]["unknown_dimensions"])

    model_def = _actual_definition()
    model_def["model_is_subject"] = True
    model_def["required_dimensions"].append("model")
    model_def["selectors"]["model"] = {"version": 1, "kind": "verification_model"}
    no_model, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", definition=model_def))
    assert code == 0 and no_model["result"]["status"] == "unknown"
    assert "model:" in " ".join(no_model["result"]["unknown_dimensions"])
    with env["db"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM phase3_objects WHERE kind='reuse_claim'").fetchone()[0] == 2


def test_actual_command_and_input_semantics_are_part_of_the_key(tmp_path):
    env = _actual_fixture(tmp_path)
    first, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse"))
    assert code == 0 and first["result"]["status"] == "claimed"
    changed = _actual_request(env, "resolve_reuse")
    changed["payload"]["command"] = ["python", "-c", "assert True"]
    second, code = _local_execute(env["db"], changed)
    assert code == 0 and second["result"]["status"] == "claimed"
    assert second["result"]["key_sha256"] != first["result"]["key_sha256"]


def test_actual_metadata_revision_change_keeps_semantic_target_key(tmp_path):
    env = _actual_fixture(tmp_path)
    first, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse"))
    assert code == 0 and first["result"]["status"] == "claimed"
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET state='Done',revision=revision+1,updated_at=? WHERE id=?",
                     (utc_now(), env["target"]))
    again, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse"))
    assert code == 0 and again["result"]["status"] == "active", again
    assert again["result"]["key_sha256"] == first["result"]["key_sha256"]


def test_actual_runtime_dependency_and_criteria_selectors_can_be_verified(tmp_path):
    env = _actual_fixture(tmp_path)
    definition = _actual_definition()
    definition["required_dimensions"].extend(["tool", "dependencies", "baseline"])
    definition["selectors"].update({
        "tool": {"version": 1, "kind": "runtime_fields", "fields": ["os", "python"]},
        "dependencies": {"version": 1, "kind": "dependency_manifests", "names": ["pyproject.toml"]},
        "baseline": {"version": 1, "kind": "criteria", "ids": ["quality"]},
    })
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json({"workspace": str(env["root"]), "criteria": ["quality"]}), env["target"]))
    claim, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", definition=definition))
    assert code == 0 and claim["result"]["status"] == "claimed", claim
    verification_id = _verified_p2_result(env)
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='review_pending',stop_confirmed=1 WHERE id=?", (env["run"],))
    recorded, code = _local_execute(env["db"], _actual_request(env, "record_reuse_result",
        verification_id=verification_id, definition=definition))
    assert code == 0 and recorded["result"]["status"] == "recorded", recorded
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='succeeded' WHERE id=?", (env["run"],))
        job = conn.execute("SELECT job_id FROM execution_runs WHERE id=?", (env["run"],)).fetchone()[0]
        conn.execute("UPDATE execution_jobs SET state='running' WHERE id=?", (job,))
        run2, now = new_id(), utc_now()
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run2, job, env["step"], 2, "running", 1, "selector-session", 1,
                      str(env["root"]), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run2, "selector-session", "path", str(env["root"]), "src/target.py", now))
    env["run"] = run2
    hit, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse", session="selector-session",
                                                           definition=definition))
    assert code == 0 and hit["result"]["status"] == "reusable", hit
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='succeeded' WHERE id=?", (run2,))
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json({"workspace": str(env["root"]),
                                      "criteria": [{"id": "quality", "text": "changed criterion"}]}),
                      env["target"]))
        run3, now = new_id(), utc_now()
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run3, job, env["step"], 3, "running", 1, "criteria-session", 1,
                      str(env["root"]), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run3, "criteria-session", "path", str(env["root"]), "src/target.py", now))
    env["run"] = run3
    changed, code = _local_execute(env["db"], _actual_request(env, "resolve_reuse",
        session="criteria-session", definition=definition))
    assert code == 0 and changed["result"]["status"] == "claimed", changed
    assert changed["result"]["key_sha256"] != hit["result"]["key_sha256"]


def test_actual_replay_body_mismatch_and_unclaimed_scope_path_are_rejected(tmp_path):
    env = _actual_fixture(tmp_path)
    request = _actual_request(env, "resolve_reuse")
    first, code = _local_execute(env["db"], request)
    assert code == 0 and first["result"]["status"] == "claimed"
    replay, code = _local_execute(env["db"], copy.deepcopy(request))
    assert code == 0 and replay["result"] == first["result"]
    altered = copy.deepcopy(request)
    altered["payload"]["inputs"] = {"source": "different"}
    conflict, code = _local_execute(env["db"], altered)
    assert code == 3 and conflict["error"]["code"] == "request_conflict"
    denied = _actual_request(env, "resolve_reuse")
    denied["payload"]["paths"] = ["src/unclaimed.py"]
    rejected, code = _local_execute(env["db"], denied)
    assert code == 3 and rejected["error"]["code"] == "scope_not_owned"
    with env["db"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM phase3_objects WHERE kind='reuse_claim'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event_id=?",
                            (request["payload"]["event_id"],)).fetchone()[0] == 1


def test_actual_source_pin_and_f3_impact_are_recomputed_before_invalidation(tmp_path):
    env = _actual_fixture(tmp_path)
    repository_id = new_id()
    req_node, impl_node = new_id(), new_id()
    graph_path = "docs/pmt-docs/plan.graph.json"
    now = utc_now()
    with env["db"].write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (repository_id, "repository", "repo", "{}", now, now))
        conn.execute("UPDATE scopes SET parent_id=?,body_json=? WHERE id=?",
                     (repository_id, canonical_json({"repository_id": repository_id,
                                                      "workspace": str(env["root"])}), env["scope"]))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), env["run"], env["session"], "path", str(env["root"]), graph_path, now))
    def node(node_id, tree_kind):
        value = {"id": node_id, "tree_kind": tree_kind, "node_kind": "goal", "summary": "Baseline node",
                 "premise": "Original premise", "product_stage": "prototype",
                 "product_scope": {"applies": False, "reason": "Scope check"},
                 "autonomy": {"authority": "user", "scope": "Preserve intent"}}
        if tree_kind == "requirement":
            value.update(source_refs=["request:source"], criteria=["criterion:one"], evidence_refs=["evidence:one"])
        else:
            value.update(framework_assignment="Python", architecture="Graph source → validator",
                         logging="Hashes only", tests=["isolated fixture"],
                         function_spec={"input": "Graph", "output": "Index", "constraints": "No secrets",
                                        "invariants": "Stable IDs", "errors": "Conflict", "verification": "Git hash"},
                         choice_set={"options": [], "insufficient_reason": "Fixture"},
                         choice={"source": "user", "selected": "SQLite", "reason": "Current",
                                 "scope": "Local"})
        return value
    graph = {"schema_version": 1, "project_id": env["scope"], "graph_version": 1,
             "nodes": [node(req_node, "requirement"), node(impl_node, "implementation")],
             "relations": [{"id": new_id(), "kind": "implements", "from": req_node, "to": impl_node}],
             "provenance": {"request_ref": "request:reuse-test"}}
    graph_file = env["root"] / graph_path
    graph_file.parent.mkdir(parents=True)
    graph_file.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    graph_payload = {"repository_id": repository_id, "workspace": str(env["root"]),
                     "relative_graph_path": graph_path, "run_id": env["run"]}
    capture = {"protocol_version": 1, "operation": "capture_source_pin", "request_id": _id(),
               "actor": "reuse-test", "session_id": env["session"], "scope_id": env["scope"],
               "payload": graph_payload}
    pinned, code = _local_execute(env["db"], capture)
    assert code == 0 and pinned["ok"], pinned
    source_pin = pinned["result"]["source_pin"]
    rebuild = {**capture, "operation": "rebuild_graph_index", "request_id": _id(),
               "payload": {**graph_payload, "expected_source": source_pin}}
    rebuilt, code = _local_execute(env["db"], rebuild)
    assert code == 0 and rebuilt["ok"], rebuilt
    change_set = {"change_id": _id(), "reason": "Change premise for impact test",
                  "changes": [{"op": "update", "id": req_node,
                               "fields": {"premise": "Changed premise"}}]}
    preview_req = {**capture, "operation": "preview_graph_change", "request_id": _id(),
                   "payload": {**graph_payload, "expected_source": source_pin,
                               "change_set": change_set}}
    preview, code = _local_execute(env["db"], preview_req)
    assert code == 0 and preview["ok"], preview
    definition = _actual_definition()
    definition["applicability"] = {"field_dimensions": {"premise": ["source"]},
                                   "unknown_reason_dimensions": {
                                       "segment_manifest_missing": ["document_coverage"],
                                       "baseline_certificate_missing_or_stale": ["document_coverage"]}}
    definition["selectors"]["source"] = {"version": 1, "kind": "source_pin_fields",
                                           "fields": ["graph_schema", "graph_revision", "graph_hash", "dirty_state"]}
    invalidate = _actual_request(env, "invalidate_reuse", definition=definition)
    invalidate["payload"].update({**graph_payload, "expected_source": source_pin,
                                  "change_preview": preview["result"], "change_set": change_set})
    result, code = _local_execute(env["db"], invalidate)
    assert code == 0 and result["ok"], result.get("error")
    assert result["result"]["status"] == "invalid"
    assert result["result"]["dimensions"] == ["source"]
    assert result["result"]["impact_source"]["source_pin"] == source_pin
