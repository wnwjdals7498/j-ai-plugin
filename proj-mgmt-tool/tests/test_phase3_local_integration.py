from __future__ import annotations

import json
import platform
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

pytest_plugins = ["test_phase3_context"]

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_phase3_local as f10
from test_phase3_context import actual_context_env
from pmt.util import canonical_json

_SCENARIO_EVIDENCE_ROOT = None


@pytest.fixture(autouse=True)
def isolate_scenario_evidence(tmp_path, monkeypatch):
    # A regression run must never overwrite the historical Phase 3 evidence.
    monkeypatch.setattr(sys.modules[__name__], "_SCENARIO_EVIDENCE_ROOT", tmp_path / "scenario-evidence")


def _write_f10_scenario_evidence(scenario_id, *, result, trace=None, extra=None, variant=None):
    catalog = f10.load_catalog()
    case = next(item for item in catalog["cases"] if item["id"] == scenario_id)
    observation = result.get("observation") or {}
    f1_f4 = result.get("f1_f4") or {}
    source = f1_f4.get("source_pin") or {}
    record = {"schema_version": 1, "kind": "phase3-local-scenario-observation",
        "scenario_id": scenario_id, "variant": variant, "baseline_status": case["baseline_status"],
        "baseline_ref": case.get("baseline_ref"), "reason": case.get("reason"),
        "evidence_tier": result.get("evidence_tier", "actual LocalStore fixture; model quality not evaluated"),
        "run_status": result.get("status"), "python": sys.version.split()[0],
        "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
        "source_fixture_sha256": catalog["common"]["source_fixture"]["sha256"],
        "source_pin": {key: source.get(key) for key in ("source_hash", "graph_hash", "graph_revision", "graph_schema")},
        "document_publication": {
            "full_coverage": (f1_f4.get("baseline_publish") or {}).get("coverage", {}).get("segments"),
            "partial_coverage": (f1_f4.get("partial_publish") or {}).get("coverage", {}).get("segments"),
            "partial_artifacts": (f1_f4.get("partial_publish") or {}).get("artifacts", [])},
        "observation": {key: observation.get(key) for key in (
            "input_bytes", "output_bytes", "internal_input_bytes", "internal_output_bytes",
            "elapsed_ms", "internal_elapsed_ms", "calls", "internal_calls", "calls_by_operation",
            "internal_calls_by_operation", "context_generations", "detail_queries", "retries",
            "rework", "reviews", "tokens", "quality", "evidence_tier")},
        "f5": {key: (result.get("f5") or {}).get(key) for key in (
            "incomplete", "mandatory_omissions", "unknown_count", "section_ids", "budget")},
        "f6": {key: (result.get("f6") or {}).get(key) for key in ("first", "second", "read_status")},
        "f8": {key: (result.get("f8") or {}).get(key) for key in ("state", "action_kind", "retry", "error_code")},
        "f9": result.get("f9") or result.get("batch") or {},
        "trace": [f10.validate_trace_record(row) for row in (trace or [])],
        "extra": extra or {}, "comparison": {"status": "not_comparable",
            "reason": (case.get("reason") if case["baseline_status"] == "not_comparable" else
                "Actual fixture binds a different project SourcePin/runtime route and includes F1-F9/control work; the preserved legacy renderer baseline does not measure that same end-to-end condition. No efficiency improvement is inferred."),
            "tokens": "unknown; UTF-8 bytes are not token counts"}}
    if _SCENARIO_EVIDENCE_ROOT is None:
        raise RuntimeError("Scenario evidence requires an isolated test output root")
    destination = _SCENARIO_EVIDENCE_ROOT
    destination.mkdir(parents=True, exist_ok=True)
    filename = scenario_id + (f"-{variant}" if variant else "") + ".json"
    (destination / filename).write_text(canonical_json(record) + "\n", encoding="utf-8")


def _use_frozen_plan_fixture(env):
    env = {**env, "root": env["workspace"].parent}
    frozen = json.loads((Path(__file__).resolve().parents[1] /
        "docs/phase3/evidence/2026-10-02/baseline/canonical-planning-graph.json").read_text(encoding="utf-8"))
    frozen["project_id"] = env["project_id"]
    graph_path = env["workspace"] / env["graph_path"]
    graph_path.write_text(canonical_json(frozen) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(env["workspace"]), "add", env["graph_path"]], check=True)
    subprocess.run(["git", "-C", str(env["workspace"]), "commit", "-qm", "F10 frozen graph fixture"], check=True)
    env["node_id"] = "00000000-0000-4000-8000-000000000002"
    env["implementation_node_id"] = "00000000-0000-4000-8000-000000000004"
    env["context_node_ids"] = [node["id"] for node in frozen["nodes"]]
    return env


def _use_shallow_complete_graph(env):
    frozen = json.loads((Path(__file__).resolve().parents[1] /
        "docs/phase3/evidence/2026-10-02/baseline/canonical-planning-graph.json").read_text(encoding="utf-8"))
    nodes = {item["id"]: item for item in frozen["nodes"]}
    requirement = nodes["00000000-0000-4000-8000-000000000002"]
    requirement["stop_reason"] = "implementation_boundary"
    implementation = nodes["00000000-0000-4000-8000-000000000004"]
    frozen.update(project_id=env["project_id"], graph_version=1,
                  nodes=[requirement, implementation],
                  relations=[{"id": "00000000-0000-4000-8000-000000000023",
                              "kind": "implements", "from": requirement["id"],
                              "to": implementation["id"]}])
    graph_path = env["workspace"] / env["graph_path"]
    graph_path.write_text(canonical_json(frozen) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(env["workspace"]), "add", env["graph_path"]], check=True)
    subprocess.run(["git", "-C", str(env["workspace"]), "commit", "-qm", "F10 shallow CLI fixture"], check=True)
    env["node_id"] = requirement["id"]
    env["implementation_node_id"] = implementation["id"]
    env["context_node_ids"] = [requirement["id"], implementation["id"]]
    return env


def test_f10_catalog_freezes_six_scenarios_and_not_comparable_reasons():
    catalog = f10.load_catalog()
    cases = {item["id"]: item for item in catalog["cases"]}
    assert set(cases) == {
        "new-plan-full-graph-render", "partial-plan-change", "resume-existing-plan",
        "parallel-investigation-context", "ambiguous-requirement",
        "failed-operation-retry-and-rework",
    }
    assert cases["new-plan-full-graph-render"]["baseline_status"] == "measured_fixture"
    assert cases["partial-plan-change"]["baseline_status"] == "capture_legacy_fixture"
    for name in ("resume-existing-plan", "parallel-investigation-context",
                 "ambiguous-requirement", "failed-operation-retry-and-rework"):
        assert cases[name]["baseline_status"] == "not_comparable"
        assert cases[name]["reason"]


def test_f10_preflight_does_not_call_missing_runtime_capabilities_a_pass():
    report = f10.preflight()
    assert report["actual_scenario_run_performed"] is False
    assert report["integrated_run_state"] in {"pending_runtime_capability", "ready_for_explicit_root_go"}
    for stage in ("F8 control", "F9 batch"):
        assert report["capabilities"][stage]["state"] in {"pending", "ready"}
    for scenario in report["scenarios"]:
        assert "id" in scenario and "baseline_status" in scenario


def test_f10_f9_payload_contains_only_current_state_refs_and_event_ids():
    refs = [{"run_id": "00000000-0000-4000-8000-000000000301", "expected_run_revision": 3},
            {"run_id": "00000000-0000-4000-8000-000000000304", "expected_run_revision": 1}]
    payloads = f10.f9_batch_payloads(run_refs=refs, workspace="D:/fixture/repo",
        repository_id="00000000-0000-4000-8000-000000000309",
        relative_graph_path="docs/pmt-docs/plan.graph.json",
        expected_source={"source_hash": "a" * 64},
        context_budget={"max_bytes": 32768, "max_lines": 500, "unit": "utf8"},
        prepare_event_id="00000000-0000-4000-8000-000000000306")
    assert set(payloads) == {"prepare_step_batch"}
    assert set(payloads["prepare_step_batch"]) == {"run_refs", "workspace", "repository_id",
        "relative_graph_path", "expected_source", "context_budget", "event_id"}
    assert "status" not in json.dumps(payloads)
    payloads = f10.f9_batch_payloads(run_refs=refs, workspace="D:/fixture/repo",
        repository_id="00000000-0000-4000-8000-000000000309",
        relative_graph_path="docs/pmt-docs/plan.graph.json",
        expected_source={"source_hash": "a" * 64},
        context_budget={"max_bytes": 32768, "max_lines": 500, "unit": "utf8"},
        prepare_event_id="00000000-0000-4000-8000-000000000306",
        batch_ref="00000000-0000-4000-8000-000000000307",
        parent_run_id=refs[0]["run_id"], expected_run_revision=4,
        collect_event_id="00000000-0000-4000-8000-000000000310")
    assert set(payloads) == {"prepare_step_batch", "collect_step_batch"}
    collect = payloads["collect_step_batch"]
    assert set(collect) == {"batch_ref", "parent_run_id", "expected_run_revision", "event_id"}
    assert "handle_ref" not in json.dumps(payloads) and "criteria_results" not in collect


def test_f10_suite_is_not_run_without_root_go_or_f9_runtime():
    called = []
    no_go = f10.run_integrated_suite(lambda case: called.append(case) or {}, root_go=False)
    assert no_go["status"] == "not_run" and no_go["overall_acceptance"] == "not_run"
    if f10.preflight()["capabilities"]["F9 batch"]["state"] != "ready":
        gated = f10.run_integrated_suite(lambda case: called.append(case) or {}, root_go=True)
        assert gated["status"] == "pending" and gated["actual_scenario_run_performed"] is False
        assert gated["overall_acceptance"] == "not_run"
    assert called == []


def test_f10_trace_aggregates_actual_call_bytes_without_inventing_tokens():
    records = [
        {"event_kind": "call", "operation": "build_task_context", "request_id": "00000000-0000-4000-8000-000000000101",
         "exit_code": 0, "elapsed_ms": 4.5, "input_bytes": 120, "output_bytes": 900,
         "source_hash": "a" * 64, "context_id": "00000000-0000-4000-8000-000000000102",
         "evidence_refs": ["context:fixture"]},
        {"event_kind": "call", "operation": "read_context_detail", "request_id": "00000000-0000-4000-8000-000000000103",
         "exit_code": 0, "elapsed_ms": 1.5, "input_bytes": 80, "output_bytes": 250,
         "source_hash": "a" * 64, "context_id": "00000000-0000-4000-8000-000000000102",
         "evidence_refs": ["context:fixture"]},
        {"event_kind": "retry", "operation": "retry_authorized", "request_id": "00000000-0000-4000-8000-000000000104",
         "exit_code": 0, "elapsed_ms": 0.1, "input_bytes": 0, "output_bytes": 0},
        {"event_kind": "rework", "operation": "rework_recorded", "request_id": "00000000-0000-4000-8000-000000000105",
         "exit_code": 0, "elapsed_ms": 0.2, "input_bytes": 0, "output_bytes": 0},
        {"event_kind": "review", "operation": "review_recorded", "request_id": "00000000-0000-4000-8000-000000000106",
         "exit_code": 0, "elapsed_ms": 0.3, "input_bytes": 0, "output_bytes": 0},
    ]
    observation = f10.observation_from_trace(records)
    assert (observation["input_bytes"], observation["output_bytes"]) == (200, 1150)
    assert (observation["calls"], observation["detail_queries"], observation["context_generations"]) == (2, 1, 1)
    assert (observation["retries"], observation["rework"], observation["reviews"]) == (1, 1, 1)
    assert observation["elapsed_ms"] == pytest.approx(6.6)
    assert observation["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
    assert observation["evidence_tier"] == "local_integration"
    assert observation["quality"]["model_quality"] == "not_evaluated"


@pytest.mark.parametrize("field", ["content", "raw", "prompt", "transcript", "stdout", "token_value"])
def test_f10_trace_rejects_raw_or_secret_content_fields(field):
    record = {"event_kind": "call", "operation": "fixture", "request_id": "00000000-0000-4000-8000-000000000201",
              "exit_code": 0, "elapsed_ms": 0.0, "input_bytes": 0, "output_bytes": 0, field: "sensitive"}
    with pytest.raises(ValueError):
        f10.validate_trace_record(record)


def test_f10_localstore_meter_counts_canonical_protocol_envelopes(tmp_path):
    from pmt.db import Database
    from pmt.store import LocalStore
    from pmt.util import canonical_json, new_id
    db = Database(tmp_path / "data", tmp_path / "config")
    req = {"protocol_version": 1, "operation": "setup", "request_id": new_id(),
           "actor": "f10-fixture", "session_id": "f10-local", "payload": {"product": "cli"}}
    trace = []
    with f10.measure_localstore_calls(trace):
        response, code = LocalStore(db).execute(req)
    observation = f10.observation_from_trace(trace)
    assert code == 0 and response["ok"]
    assert observation["calls"] == 1
    assert observation["input_bytes"] == len(canonical_json(req).encode("utf-8"))
    assert observation["output_bytes"] == len(canonical_json(response).encode("utf-8"))
    assert observation["tokens"] == {"status": "unknown", "actual": None, "estimate": None}


def test_f10_partial_change_baseline_replays_unchanged_legacy_renderer(tmp_path):
    # Tests write only under pytest's isolated basetemp, never to evidence or user data.
    result = f10.capture_partial_change_baseline(output_dir=tmp_path, repetitions=2)
    manifest_path = tmp_path / "phase2-partial-change-full-render.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["case_id"] == "phase2-partial-plan-full-render"
    assert len(manifest["observations"]) == 2
    assert all(row["evidence_tier"] == "fixture" for row in manifest["observations"])
    assert all(row["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
               for row in manifest["observations"])
    assert manifest["condition"]["source"]["patched_graph_sha256"] == result["input_sha256"]
    assert manifest["observations"][0]["output_bytes"] == result["output_bytes"]
    assert (tmp_path / "partial-change-input.graph.json").is_file()


def test_f10_prepared_f1_f8_public_localstore_flow_remains_pending_f9(actual_context_env):
    env = _use_frozen_plan_fixture(actual_context_env)
    result = f10.run_f1_f8_preparation(env, "new-plan-full-graph-render")
    assert result["status"] == "partial_pending_f9"
    assert result["f1_f4"]["baseline_publish"]["coverage"]["segments"] == "complete"
    assert result["f1_f4"]["apply_receipt"] is None
    assert result["f1_f4"]["source_pin"]["source_hash"] == result["f5"]["source_hash"]
    assert isinstance(result["f5"]["incomplete"], bool)
    assert result["f5_small_budget"]["incomplete"] is True
    assert result["f6"]["first"]["status"] == "claimed"
    assert result["f6"]["second"]["status"] == "active"
    if result["f5"]["incomplete"]:
        assert result["f8"]["state"] == "review_required"
        assert result["f8"]["action"]["kind"] == "review-needed"
        assert result["f8"].get("action", {}).get("run_id") == actual_context_env["run_id"]
        assert "run_result" not in result
    else:
        assert result["f8"]["action_kind"] == "main-native-call"
        assert result["run_result"]["state"] == "review_pending"
        assert result["f8_observation"]["action"]["kind"] == "read-result"
    assert result["f9"]["state"] in {"pending", "not_run"}
    observation = result["observation"]
    assert observation["calls"] > 10
    assert observation["input_bytes"] > 0 and observation["output_bytes"] > 0
    assert observation["context_generations"] >= 2 and observation["detail_queries"] > 0
    if result["f5"]["incomplete"]:
        assert result["f5"]["mandatory_omissions"] or result["f5"]["unknown"]
    assert observation["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
    assert observation["quality"]["model_quality"] == "not_evaluated"
    assert all(not ({"content", "raw", "prompt", "transcript"} & set(row)) for row in result["trace"])
    _write_f10_scenario_evidence("new-plan-full-graph-render", result=result, trace=result["trace"])


def test_f10_prepared_partial_change_requires_f3_f4_evidence_and_holds_unknown_context(actual_context_env, tmp_path):
    env = _use_frozen_plan_fixture(actual_context_env)
    result = f10.run_f1_f8_preparation(env, "partial-plan-change")
    assert result["status"] == "partial_pending_f9"
    assert result["f1_f4"]["baseline_publish"]["coverage"]["segments"] == "complete"
    assert result["f1_f4"]["preview"]["change_id"] == result["f1_f4"]["apply_receipt"]["change_id"]
    assert result["f1_f4"]["impact"]["source_pin"]["source_hash"] == result["f1_f4"]["before_source_pin"]["source_hash"]
    assert result["f1_f4"]["partial_publish"]["source_pin"]["source_hash"] == result["f1_f4"]["source_pin"]["source_hash"]
    assert result["f2_slice"]["index_node_count"] == result["f2_slice"]["node_count"] == 3
    assert result["f2_slice"]["index_relation_count"] == result["f2_slice"]["relation_count"] == 2
    assert result["f2_slice"]["source_hash"] == result["f1_f4"]["source_pin"]["source_hash"]
    assert result["f2_slice"]["traversal_complete"] is True
    assert not any(item.get("reason_code") == "traversal_limit_or_depth"
                   for item in result["f2_slice"]["unknown"])
    if result["f5"]["incomplete"]:
        assert result["f5"]["mandatory_omissions"] or result["f5"]["unknown"]
        assert result["f8"]["state"] == "review_required"
        assert result["f8"]["action"]["kind"] == "review-needed"
    else:
        assert result["f8"]["action_kind"] == "main-native-call"
        assert result["run_result"]["state"] == "review_pending"
    assert result["observation"]["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
    assert result["f9"]["state"] in {"pending", "not_run"}
    (tmp_path / "f10-f2-f5-diagnostic.json").write_text(json.dumps({
        "frozen_graph_sha256": f10.load_catalog()["common"]["source_fixture"]["sha256"],
        "source_pin": {key: result["f1_f4"]["source_pin"].get(key)
                       for key in ("source_hash", "graph_hash", "graph_revision", "graph_schema")},
        "f2_public_slice": result["f2_slice"],
        "f5": {"incomplete": result["f5"]["incomplete"],
               "mandatory_omissions": result["f5"]["mandatory_omissions"],
               "unknown": result["f5"]["unknown"],
               "section_ids": result["f5"]["section_ids"], "budget": result["f5"]["budget"]},
        "f8_action": result["f8"].get("action") or result["f8"].get("action_kind")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    _write_f10_scenario_evidence("partial-plan-change", result=result, trace=result["trace"],
        extra={"f2_traversal_complete": result["f2_slice"]["traversal_complete"],
               "f2_node_count": result["f2_slice"]["node_count"],
               "f2_relation_count": result["f2_slice"]["relation_count"]})


def test_f10_local_cli_failure_retry_is_blocked_without_transient_receipt(actual_context_env):
    env = _use_shallow_complete_graph(actual_context_env)
    result = f10.run_f1_f8_preparation(env, "failed-operation-retry-and-rework")
    assert result["status"] == "partial_pending_f9"
    assert result["f5"]["incomplete"] is False
    assert result["f8"]["state"] == "failed_fixture"
    assert result["f8"]["retry"] == "rejected_without_transient_classification"
    assert result["f8"]["error_code"] == "retry_not_allowed"
    assert result["observation"]["retries"] == 1
    assert result["observation"]["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
    assert result["f9"]["state"] in {"pending", "not_run"}
    _write_f10_scenario_evidence("failed-operation-retry-and-rework", result=result, trace=result["trace"])


def test_f10_actual_new_session_resume_rebuilds_context_from_current_owner(actual_context_env):
    env = _use_frozen_plan_fixture(actual_context_env)
    import test_phase3_context as f5_fixture
    pin = f5_fixture._actual_f3_ready(env)
    old_context, code = f5_fixture._build_actual(env, pin)
    assert code == 0 and old_context["ok"]
    previous_context_id = old_context["result"]["context_ref"]["id"]
    new_session = "f10-resume-session"
    from pmt.util import new_id, utc_now, canonical_json
    new_run, new_job = new_id(), new_id()
    now = utc_now()
    with env["db"].write() as conn:
        old = conn.execute("SELECT * FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        conn.execute("UPDATE execution_runs SET state='failed',revision=revision+1,updated_at=? WHERE id=?",
                     (now, env["run_id"]))
        conn.execute("UPDATE execution_jobs SET state='failed',updated_at=? WHERE id=?", (now, old["job_id"]))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (env["run_id"],))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,'running','{}',?,?)",
                     (new_job, env["step_id"], now, now))
        intent = json.loads(old["intent_json"])
        intent.update(run_id=new_run, job_id=new_job)
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,"
            "workspace,scopes_json,route_json,intent_json,created_at,updated_at) "
            "VALUES(?,?,?,2,'running',1,?,1,?,?,?,?,?,?)",
            (new_run, new_job, env["step_id"], new_session, old["workspace"], old["scopes_json"],
             old["route_json"], canonical_json(intent), now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
            (new_id(), new_run, new_session, "path", str(env["workspace"]), env["graph_path"], now))
    env.update(session=new_session, run_id=new_run, job_id=new_job, resume_context_id=previous_context_id)
    result = f10.run_f1_f8_preparation(env, "resume-existing-plan")
    assert result["scenario_id"] == "resume-existing-plan"
    assert result["status"] == "partial_pending_f9"
    assert result["resume"]["context_ref"]["id"] != previous_context_id
    assert result["resume"]["resume"]["status"] == "unknown"
    assert result["resume"]["resume"]["old_aliases_reactivated"] is False
    assert result["resume"]["source_hash"] == result["f1_f4"]["source_pin"]["source_hash"]
    assert result["observation"]["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
    assert result["observation"]["quality"]["model_quality"] == "not_evaluated"
    _write_f10_scenario_evidence("resume-existing-plan", result=result, trace=result["trace"],
        extra={"old_context_id": previous_context_id,
               "resumed_context_id": result["resume"]["context_ref"]["id"],
               "resume_status": result["resume"]["resume"]["status"],
               "old_aliases_reactivated": result["resume"]["resume"]["old_aliases_reactivated"]})


def test_f10_actual_missing_acceptance_stays_unknown_and_blocks_f8(actual_context_env):
    env = _use_frozen_plan_fixture(actual_context_env)
    env["criteria"] = []
    with env["db"].write() as conn:
        conn.execute("UPDATE step_specs SET criteria_json='[]' WHERE step_id=?", (env["step_id"],))
        row = conn.execute("SELECT intent_json FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        intent = json.loads(row["intent_json"])
        intent["criteria"] = []
        conn.execute("UPDATE execution_runs SET intent_json=? WHERE id=?",
                     (json.dumps(intent, sort_keys=True), env["run_id"]))
    result = f10.run_f1_f8_preparation(env, "ambiguous-requirement")
    assert result["status"] == "partial_pending_f9"
    omissions = result["f5"]["mandatory_omissions"]
    assert any(item.get("section_id") == "criteria" and item.get("reason_code") == "criteria_missing"
               for item in omissions)
    assert result["f5"]["incomplete"] is True
    assert result["f8"]["state"] == "review_required"
    assert result["f8"]["action"]["kind"] == "review-needed"
    assert result["observation"]["reviews"] == 1
    assert result["observation"]["quality"]["model_quality"] == "not_evaluated"
    _write_f10_scenario_evidence("ambiguous-requirement", result=result, trace=result["trace"],
        extra={"missing_acceptance_preserved": True})


def test_f10_actual_public_two_child_batch_report_collect_and_replay(actual_context_env):
    """Real LocalStore state changes, deterministic child report; never model-quality evidence."""
    env = _use_frozen_plan_fixture(actual_context_env)
    trace = []
    with f10.measure_localstore_calls(trace):
        from pmt.util import new_id, utc_now
        with env["db"].write() as conn:
            if not conn.execute("SELECT 1 FROM scope_locks WHERE run_id=? AND resource=?",
                    (env["run_id"], "docs/pmt-docs/plan.md")).fetchone():
                conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (new_id(), env["run_id"], env["session"], "path",
                    str(env["workspace"]), "docs/pmt-docs/plan.md", utc_now()))
        frozen_partial = next(item for item in f10.load_catalog()["cases"]
                              if item["id"] == "partial-plan-change")
        f1_f4 = f10.run_f1_f4_public(env,
            premise=frozen_partial["input"]["patch"]["value"], partial=True)
        context = f10.run_f5_public(env, f1_f4["source_pin"])
        assert context["incomplete"] is False, {
            "mandatory_omissions": context["mandatory_omissions"], "unknown": context["unknown"]}
        f10._prepare_item_for_reuse(env)
        reuse = f10.run_f6_public(env, f1_f4["source_pin"])
        # Use the producer's real queued P2 fixture builder to create the second same-owner child.
        import test_phase3_batch as f9_fixtures
        batch_env = f9_fixtures._queue_two_actual_runs(env)
        batch_env["product"] = "codex"
        from pmt.util import canonical_json
        native_route = {**batch_env["batch_route"], "mode": "native", "adapter_kind": "native",
            "model": "local-native-fixture", "actual_support": "verified_supported"}
        with batch_env["db"].write() as conn:
            for ref in batch_env["batch_runs"]:
                row = conn.execute("SELECT intent_json FROM execution_runs WHERE id=?", (ref["run_id"],)).fetchone()
                intent = json.loads(row["intent_json"])
                intent["route"] = native_route
                conn.execute("UPDATE execution_runs SET route_json=?,intent_json=? WHERE id=?",
                    (canonical_json(native_route), canonical_json(intent), ref["run_id"]))
        f9 = f10.run_f9_native_batch_fixture(batch_env, reuse["decision_ref"],
            reported_count=2, fail_first=True, trace=trace)
    obs = f10.observation_from_trace(trace)
    assert f1_f4["partial_publish"]["coverage"]["segments"] == "complete"
    assert obs["input_bytes"] > 0 and obs["output_bytes"] > 0
    assert obs["context_generations"] >= 2
    assert obs["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
    assert obs["quality"]["model_quality"] == "not_evaluated"
    assert all(not ({"content", "raw", "prompt", "transcript"} & set(row)) for row in trace)
    if f9["status"] == "pending":
        assert f9["reason_code"] == "native_action_group_contract_missing"
        assert f9["collect_performed"] is False
        assert {"prepare_step_batch", "advance_execution_control"}.issubset(obs["calls_by_operation"])
        assert "acknowledge_execution_action" not in obs["calls_by_operation"]
        return
    assert f9["status"] == "review_pending" and f9["collect_replayed"] is True
    assert f9["quality"] == "fixture_only_not_model_quality"
    assert f9["reported_child_count"] == 2
    required_public_ops = {"prepare_step_batch", "advance_execution_control",
        "acknowledge_execution_action", "submit_execution_result", "collect_step_batch"}
    assert required_public_ops.issubset(obs["calls_by_operation"]), sorted(obs["calls_by_operation"])
    assert obs["internal_calls_by_operation"].get("compact_tool_result") == 2
    assert obs["internal_input_bytes"] > f9["report_resource_bytes"]
    _write_f10_scenario_evidence("parallel-investigation-context", result={
        "scenario_id": "parallel-investigation-context", "status": "partial_pending_f9",
        "evidence_tier": f9["evidence_tier"], "f1_f4": f1_f4, "f5": context, "f6": reuse,
        "f9": f9, "observation": obs}, trace=trace,
        extra={"member_count": f9["reported_child_count"], "physical_slots": 1,
               "report_status": f9["status"], "report_ref": f9["report_ref"],
               "report_sha256": f9["report_sha256"], "collect_replayed": f9["collect_replayed"]})


def test_f10_actual_public_group_missing_child_remains_reconciling(actual_context_env):
    env = actual_context_env
    import test_phase3_batch as f9_fixtures
    from pmt.util import canonical_json
    import test_phase3_context as f5_fixture
    batch_pin = f5_fixture._actual_f3_ready(env)
    f10._prepare_item_for_reuse(env)
    reuse = f10.run_f6_public(env, batch_pin)
    batch_env = f9_fixtures._queue_two_actual_runs(env)
    batch_env["product"] = "codex"
    native_route = {**batch_env["batch_route"], "mode": "native", "adapter_kind": "native",
        "model": "local-native-fixture", "actual_support": "verified_supported"}
    with batch_env["db"].write() as conn:
        for ref in batch_env["batch_runs"]:
            row = conn.execute("SELECT intent_json FROM execution_runs WHERE id=?", (ref["run_id"],)).fetchone()
            intent = json.loads(row["intent_json"]); intent["route"] = native_route
            conn.execute("UPDATE execution_runs SET route_json=?,intent_json=? WHERE id=?",
                (canonical_json(native_route), canonical_json(intent), ref["run_id"]))
    trace = []
    with f10.measure_localstore_calls(trace):
        f9 = f10.run_f9_native_batch_fixture(batch_env, reuse["decision_ref"],
            reported_count=1, fail_first=False, trace=trace)
    obs = f10.observation_from_trace(trace)
    assert f9["status"] == "reconciling" and f9["reported_child_count"] == 1
    assert f9["child_states"].count("reconciling") == 1
    assert f9["collect_replayed"] is True
    assert obs["calls_by_operation"]["collect_step_batch"] == 2
    assert obs["internal_calls_by_operation"]["compact_tool_result"] == 1
    assert obs["tokens"] == {"status": "unknown", "actual": None, "estimate": None}
    assert obs["quality"]["model_quality"] == "not_evaluated"
    _write_f10_scenario_evidence("parallel-investigation-context", result={
        "status": "partial_pending_f9", "evidence_tier": f9["evidence_tier"],
        "f1_f4": {}, "f5": {}, "f6": reuse, "f9": f9, "observation": obs}, trace=trace,
        extra={"reported_child_count": 1, "missing_child_state": "reconciling",
               "report_ref": f9["report_ref"], "report_sha256": f9["report_sha256"],
               "collect_replayed": True}, variant="missing-child")


def test_f10_actual_public_group_cancel_before_dispatch_cancels_children(actual_context_env):
    env = actual_context_env
    import test_phase3_batch as f9_fixtures
    from pmt.util import new_id
    batch_env = f9_fixtures._queue_two_actual_runs(env)
    batch_env["product"] = "codex"
    trace = []
    with f10.measure_localstore_calls(trace):
        refs = [{"run_id": item["run_id"], "expected_run_revision": item["expected_run_revision"]}
                for item in batch_env["batch_runs"]]
        prepared_envelope, prepared = f10._result(batch_env, "prepare_step_batch",
            run_refs=refs, workspace=str(batch_env["workspace"]), repository_id=batch_env["repo_id"],
            relative_graph_path=batch_env["graph_path"], expected_source=batch_env["batch_pin"],
            context_budget={"max_bytes": 20000, "max_lines": 1000, "unit": "utf8"}, event_id=new_id())
        assert prepared["status"] == "prepared"
        for member in prepared.get("member_run_refs", []):
            context_ref = member.get("context_ref") or {}
            if isinstance(context_ref.get("id"), str):
                trace.append(f10.validate_trace_record({"event_kind": "context_generated",
                    "operation": "prepare_step_batch.member_context", "request_id": prepared_envelope["request_id"],
                    "exit_code": 0, "elapsed_ms": 0, "input_bytes": 0, "output_bytes": 0,
                    "status": "projected", "evidence_refs": [], "context_id": context_ref["id"]}))
        parent_id = batch_env["batch_runs"][0]["run_id"]
        with closing(batch_env["db"].connect()) as conn:
            revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()[0]
        _, canceled = f10._result(batch_env, "request_execution_cancel", run_id=parent_id,
            expected_run_revision=revision, event_id=new_id())
        assert canceled["state"] == "canceled"
        assert canceled["batch"]["status"] == "canceled"
    with closing(batch_env["db"].connect()) as conn:
        states = [conn.execute("SELECT state FROM execution_runs WHERE id=?", (item["run_id"],)).fetchone()[0]
                  for item in batch_env["batch_runs"]]
        remaining_locks = conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (parent_id,)).fetchone()[0]
    obs = f10.observation_from_trace(trace)
    assert states == ["canceled", "canceled"] and remaining_locks == 0
    assert obs["calls_by_operation"]["prepare_step_batch"] == 1
    assert obs["calls_by_operation"]["request_execution_cancel"] == 1
    assert obs["context_generations"] == 2
    _write_f10_scenario_evidence("parallel-investigation-context", result={
        "status": "cancelled_before_dispatch", "evidence_tier": "actual LocalStore group cancellation fixture",
        "f1_f4": {}, "f5": {}, "f9": {"batch_ref": prepared["batch_ref"], "status": "canceled"},
        "observation": obs}, trace=trace,
        extra={"member_run_ids": [item["run_id"] for item in batch_env["batch_runs"]],
               "child_states": states, "scope_locks_remaining": remaining_locks}, variant="cancel-before-dispatch")
