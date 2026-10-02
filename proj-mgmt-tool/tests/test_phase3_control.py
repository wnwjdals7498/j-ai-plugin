"""F8 local acceptance: Phase-2 owner state plus current F5/F6/F7 references."""
from __future__ import annotations

from contextlib import closing
import json
import copy
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest_plugins = ["test_phase3_context"]

from pmt.efficiency.storage import Phase3Storage
from pmt.errors import PmtError
from pmt.db import Database
from pmt.store import LocalStore
from pmt.service import execute
from pmt.util import canonical_json, new_id, utc_now
from test_phase3_context import _actual_f3_ready, _build_actual, _actual_request
from test_phase3_reuse import _actual_definition


def _route(mode="native", agent="cli"):
    return {"agent": agent, "provider": "fixture-native", "model": "local-fixture",
            "mode": mode, "selection_reason": "fixture capability",
            "actual_support": "verified_supported", "auth_state": "authenticated",
            "capability_ref": "fixture-native-capability", "max_concurrency": 1}


def _control_req(env, operation, **payload):
    return {"protocol_version": 1, "operation": operation, "request_id": new_id(),
            "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
            "source": {"product": "cli"}, "payload": payload}


def _reuse_ref(env, pin):
    definition = _actual_definition()
    definition["selectors"]["source"]["paths"] = [env["graph_path"]]
    payload = {"definition": definition, "run_id": env["run_id"],
        "workspace": str(env["workspace"]), "paths": [env["graph_path"]],
        "target_id": env["item_id"], "command": ["fixture-native", "run"],
        "inputs": {"graph": pin["graph_hash"]}, "event_id": new_id(),
        "repository_id": env["repo_id"], "relative_graph_path": env["graph_path"],
        "expected_source": pin}
    req = _actual_request(env, "resolve_reuse", **payload)
    result, code = execute(env["db"], req)
    assert code == 0 and result["ok"], result.get("error")
    assert result["result"]["status"] == "claimed", result["result"]
    return result["result"]["body_ref"]


def test_f8_owned_request_replay_requires_semantic_fingerprint(tmp_path):
    from pmt.efficiency.control import _read_port_replay
    from pmt.db import semantic_request_fingerprint

    db = Database(tmp_path / "data", tmp_path / "config")
    request = {"protocol_version": 1, "operation": "advance_execution_control",
               "request_id": new_id(), "actor": "fixture-actor", "session_id": "sess-a",
               "scope_id": new_id(), "payload": {"run_id": new_id(),
               "context_ref": {"id": new_id()}, "reuse_body_ref": {"key": "reuse-a"}},
               "source": {"product": "cli"}}
    stored, code = db.run_request(request, lambda _conn, req: {"accepted_run": req["payload"]["run_id"]})
    assert code == 0 and stored["ok"]
    port = LocalStore(db)

    assert _read_port_replay(port, request) == (stored, 0)
    ignored_changes = request | {"correlation_id": "new-correlation", "received_at": "later",
                                 "retry_count": 4, "attempt": 2}
    assert semantic_request_fingerprint(ignored_changes) == semantic_request_fingerprint(request)
    assert _read_port_replay(port, ignored_changes) == (stored, 0)

    semantic_mutations = [
        request | {"payload": request["payload"] | {"run_id": new_id()}},
        request | {"payload": request["payload"] | {"context_ref": {"id": new_id()}}},
        request | {"payload": request["payload"] | {"reuse_body_ref": {"key": "reuse-b"}}},
        request | {"payload": request["payload"] | {"now": "later"}},
        request | {"source": {"product": "different"}},
    ]
    for changed in semantic_mutations:
        with pytest.raises(PmtError) as caught:
            _read_port_replay(port, changed)
        assert caught.value.code == "request_conflict"

    class LegacyLookup:
        def get_request_result(self, request_id, actor=None, session_id=None):
            return stored

    with pytest.raises(PmtError) as caught:
        _read_port_replay(LegacyLookup(), request)
    assert caught.value.code == "request_result_lookup_unavailable"


def _native_control_fixture(env, route=None, node_ids=None):
    pin = _actual_f3_ready(env)
    built, code = _build_actual(env, pin, node_ids=node_ids)
    assert code == 0 and built["ok"], built.get("error")
    context = built["result"]
    assert context["incomplete"] is False
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json({"workspace": str(env["workspace"]), "criteria": []}), env["item_id"]))
    reuse_ref = _reuse_ref(env, pin)
    now = utc_now()
    with env["db"].write() as conn:
        row = conn.execute("SELECT job_id,intent_json,scopes_json FROM execution_runs WHERE id=?",
                           (env["run_id"],)).fetchone()
        intent = json.loads(row["intent_json"])
        from pmt.execution.service import _normalize_scopes
        normalized_scopes = _normalize_scopes(json.loads(row["scopes_json"]), str(env["workspace"]))
        intent.update(job_id=row["job_id"], step_id=env["step_id"], workspace=str(env["workspace"]),
                      scopes=normalized_scopes, dependencies=[])
        conn.execute("UPDATE execution_runs SET state='starting',revision=revision+1,route_json=?,updated_at=? WHERE id=?",
                     (canonical_json(route or _route()), now, env["run_id"]))
        conn.execute("UPDATE execution_runs SET scopes_json=? WHERE id=?",
                     (canonical_json(normalized_scopes), env["run_id"]))
        conn.execute("UPDATE execution_runs SET intent_json=? WHERE id=?", (canonical_json(intent), env["run_id"]))
        conn.execute("UPDATE execution_jobs SET state='starting',updated_at=? WHERE id=?",
                     (now, conn.execute("SELECT job_id FROM execution_runs WHERE id=?",
                                        (env["run_id"],)).fetchone()[0]))
    return context, reuse_ref


def test_f8_full_f4_baseline_apply_partial_and_three_node_context(actual_context_env):
    env = actual_context_env
    graph_path = env["workspace"] / env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    third_id = new_id()
    third = copy.deepcopy(next(node for node in graph["nodes"] if node["tree_kind"] == "requirement"))
    third["id"] = third_id
    third["summary"] = "Third canonical node"
    third["stop_reason"] = "implementation_boundary"
    for node in graph["nodes"]:
        if node["id"] == env["implementation_node_id"]:
            node["stop_reason"] = "file_edit_boundary"
    graph["nodes"].append(third)
    graph["relations"].append({"id": new_id(), "kind": "parent",
                               "from": env["node_id"], "to": third_id})
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    with env["db"].write() as conn:
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (new_id(), env["run_id"], env["session"], "path",
                     str(env["workspace"]), "docs/pmt-docs/plan.md", utc_now()))

    pin = _actual_f3_ready(env)
    baseline, code = execute(env["db"], _actual_request(env, "prepare_document_segments", expected_source=pin))
    assert code == 0 and baseline["ok"], baseline.get("error")
    baseline, code = execute(env["db"], _actual_request(env, "publish_document_segments",
        expected_source=pin, journal_id=baseline["result"]["journal_id"]))
    assert code == 0 and baseline["ok"], baseline.get("error")
    assert baseline["result"]["coverage"]["segments"] == "complete"

    pin = _actual_f3_ready(env)
    change_set = {"change_id": new_id(), "reason": "Update one documented premise",
                  "evidence_refs": [], "changes": [{"op": "update", "id": env["node_id"],
                                                        "fields": {"premise": "Revised after baseline"}}]}
    preview, code = execute(env["db"], _actual_request(env, "preview_graph_change",
        expected_source=pin, change_set=change_set))
    assert code == 0 and preview["ok"], preview.get("error")
    impact, code = execute(env["db"], _actual_request(env, "calculate_graph_impact",
        expected_source=pin, change_preview=preview["result"], change_set=change_set))
    assert code == 0 and impact["ok"] and impact["result"]["complete"] is True, impact.get("error")
    applied, code = execute(env["db"], _actual_request(env, "apply_graph_change",
        expected_source=pin, change_set=change_set))
    assert code == 0 and applied["ok"], applied.get("error")
    current, code = execute(env["db"], _actual_request(env, "capture_source_pin"))
    assert code == 0 and current["ok"]
    partial, code = execute(env["db"], _actual_request(env, "prepare_document_segments",
        expected_source=current["result"]["source_pin"], impact_set=impact["result"],
        apply_receipt=applied["result"], change_set=change_set, change_preview=preview["result"]))
    assert code == 0 and partial["ok"], partial.get("error")
    partial, code = execute(env["db"], _actual_request(env, "publish_document_segments",
        expected_source=current["result"]["source_pin"], journal_id=partial["result"]["journal_id"]))
    assert code == 0 and partial["ok"], partial.get("error")

    node_ids = [env["node_id"], env["implementation_node_id"], third_id]
    context, reuse_ref = _native_control_fixture(env, node_ids=node_ids)
    assert context["incomplete"] is False
    assert not any(item.get("reason_code") == "traversal_limit_or_depth"
                   for item in context["unknown"])
    started, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and started["ok"], started.get("error")
    assert started["result"]["action"]["kind"] == "main-native-call"


def test_f8_native_nonce_context_revalidation_and_f7_receipt_handoff(actual_context_env, monkeypatch):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    import pmt.efficiency.control as control_module
    delivered_ids = []
    def notifier(notice_id, _notice):
        delivered_ids.append(notice_id)
        return len(delivered_ids) >= 2
    base_controller = control_module.ExecutionController
    class RetryNoticeController(base_controller):
        def __init__(self, *args, **kwargs):
            kwargs["notifier"] = notifier
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(control_module, "ExecutionController", RetryNoticeController)
    context_ref = context["context_ref"]
    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context_ref, reuse_body_ref=reuse_ref))
    assert code == 0 and first["ok"], first.get("error")
    action = first["result"]["action"]
    assert action["kind"] == "main-native-call", action
    assert action["action_nonce"] and action["context_ref"] == context_ref
    with closing(env["db"].connect()) as conn:
        control = Phase3Storage(env["db"]).get_object("execution_control", env["run_id"], env["project_id"],
            env["actor"], env["session"], conn=conn)
        assert control["body"]["stage"] == "main_action_pending"
        assert "purpose" not in control["body"] and "projection" not in control["body"]
        saved_run = conn.execute("SELECT state,handle_json FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        assert saved_run["state"] == "starting" and saved_run["handle_json"] is None

    ack, code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"], outcome="started",
        handle_ref={"kind": "native_handle", "id": "native-handle-actual-fixture",
                    "provider_ref": "fixture-provider-reference"}))
    assert code == 0 and ack["ok"], ack.get("error")
    assert ack["result"]["run_state"] == "running"
    with closing(env["db"].connect()) as conn:
        attached_handle = json.loads(conn.execute("SELECT handle_json FROM execution_runs WHERE id=?",
                                                  (env["run_id"],)).fetchone()[0])
    assert attached_handle["id"] == "native-handle-actual-fixture"
    duplicate, dup_code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"], outcome="started",
        handle_ref={"kind": "native_handle", "id": "native-handle-actual-fixture",
                    "provider_ref": "fixture-provider-reference"}))
    assert dup_code == 0 and duplicate["ok"] and duplicate["result"]["replayed"]

    progress, progress_code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context_ref, reuse_body_ref=reuse_ref))
    assert progress_code == 0 and progress["ok"]
    first_notice = progress["result"]["notification"]["notice_id"]
    assert progress["result"]["notification"]["status"] == "pending"
    retry_notice, retry_code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context_ref, reuse_body_ref=reuse_ref))
    assert retry_code == 0 and retry_notice["ok"]
    assert retry_notice["result"]["notification"]["notice_id"] == first_notice
    assert retry_notice["result"]["notification"]["status"] == "delivered"
    assert delivered_ids == [first_notice, first_notice]
    stable_control_revision = retry_notice["result"]["control_ref"]["revision"]
    stable, stable_code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context_ref, reuse_body_ref=reuse_ref))
    assert stable_code == 0 and stable["ok"]
    assert stable["result"]["control_ref"]["revision"] == stable_control_revision
    assert delivered_ids == [first_notice, first_notice]

    from pmt.phase2_common import persist_json_resource
    receipt = persist_json_resource(env["db"], _control_req(env, "seed_native_receipt"),
        {"observed": "actual native fixture result", "run_id": env["run_id"]},
        env["project_id"], "native_result", env["run_id"])
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT revision,route_json FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
    result = {"directive_version": 1, "actual_route": json.loads(run["route_json"]),
        "summary": "Actual native fixture returned; criterion verification remains pending.",
        "criteria_results": [{"criterion_id": item["id"], "outcome": "not_run",
                               "reason": "native result requires main review", "evidence_refs": []}
                              for item in env["criteria"]],
        "receipt_ref": receipt["artifact_id"], "evidence_refs": [receipt["artifact_id"]],
        "stop_confirmed": True, "stop_evidence_refs": [receipt["artifact_id"]],
        "runner_observation": {"exit_code": 0}}
    submitted, submit_code = execute(env["db"], _actual_request(env, "submit_execution_result",
        expected_run_revision=run["revision"], result=result))
    assert submit_code == 0 and submitted["result"]["state"] == "review_pending"
    read_result, read_code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context_ref, reuse_body_ref=reuse_ref))
    assert read_code == 0 and read_result["ok"], read_result.get("error")
    assert read_result["result"]["action"]["kind"] == "read-result", read_result["result"]
    observation = read_result["result"]["action"]["refs"]["tool_observation"]
    assert read_result["result"]["action"]["kind"] == "read-result"
    assert observation["run_id"] == env["run_id"]
    assert observation["evidence_ref"]["kind"] == "artifact"
    assert observation["artifact_sha256"] and observation["criteria_verdict"]["status"] == "not_evaluated"


def test_f8_caller_cannot_claim_native_not_started(actual_context_env):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0
    action = first["result"]["action"]
    result, code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"],
        outcome="not_started", verified_trace_ref="caller-assertion"))
    assert code == 3 and result["error"]["code"] == "native_not_started_unverified"
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        assert run["state"] == "starting" and run["stop_confirmed"] == 0


def test_f8_attach_ack_reconciles_after_control_state_write_failure(actual_context_env, monkeypatch):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and first["ok"]
    action = first["result"]["action"]
    ref = {"kind": "native_handle", "id": "native-crash-window-handle",
           "provider_ref": "fixture-provider-reference"}
    import pmt.efficiency.storage as storage_module
    put = storage_module.Phase3Storage.put_object
    fail_once = {"armed": True}
    def fail_control_commit(self, kind, *args, **kwargs):
        if kind == "execution_control" and fail_once["armed"]:
            fail_once["armed"] = False
            raise PmtError("control_state_write_failed", "Injected control-state commit failure", 4, True)
        return put(self, kind, *args, **kwargs)
    monkeypatch.setattr(storage_module.Phase3Storage, "put_object", fail_control_commit)
    first_ack, first_code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"],
        outcome="started", handle_ref=ref))
    assert first_code == 4 and not first_ack["ok"]
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT state,revision,handle_json FROM execution_runs WHERE id=?",
                           (env["run_id"],)).fetchone()
        control = Phase3Storage(env["db"]).get_object("execution_control", env["run_id"], env["project_id"],
            env["actor"], env["session"], conn=conn)
    assert run["state"] == "running" and json.loads(run["handle_json"])["id"] == ref["id"]
    assert control["body"]["stage"] == "main_action_pending"
    monkeypatch.setattr(storage_module.Phase3Storage, "put_object", put)
    resumed, resumed_code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"],
        outcome="started", handle_ref=ref))
    assert resumed_code == 0 and resumed["ok"] and resumed["result"]["run_state"] == "running"
    wrong_handle, wrong_handle_code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"],
        outcome="started", handle_ref={**ref, "id": "different-handle"}))
    assert wrong_handle_code == 3 and wrong_handle["error"]["code"] == "control_action_conflict"
    attach_id = str(uuid.uuid5(uuid.UUID(action["action_nonce"]), "pmt-f8-attach-native-handle"))
    with closing(env["db"].connect()) as conn:
        control = Phase3Storage(env["db"]).get_object("execution_control", env["run_id"], env["project_id"],
            env["actor"], env["session"], conn=conn)
        attach_count = conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (attach_id,)).fetchone()[0]
        run = conn.execute("SELECT state,revision,handle_json FROM execution_runs WHERE id=?",
                           (env["run_id"],)).fetchone()
    assert control["body"]["stage"] == "running" and attach_count == 1
    assert run["state"] == "running" and json.loads(run["handle_json"])["id"] == ref["id"]


def test_f8_lost_native_action_response_reuses_pending_nonce_without_redispatch(actual_context_env):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and first["ok"]
    action = first["result"]["action"]
    replay, replay_code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert replay_code == 0 and replay["ok"]
    assert replay["result"]["action"]["action_nonce"] == action["action_nonce"]
    with closing(env["db"].connect()) as conn:
        count = conn.execute("SELECT count(*) FROM operation_journal WHERE kind='runner' AND id=?",
            (str(uuid.uuid5(uuid.UUID(env["run_id"]), "pmt-runner-dispatch")),)).fetchone()[0]
        run = conn.execute("SELECT state,handle_json FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
    assert count == 1 and run["state"] == "starting" and run["handle_json"] is None


def test_f8_unknown_native_start_requires_reconcile_and_retains_locks(actual_context_env):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and first["ok"]
    action = first["result"]["action"]
    unknown, code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"], outcome="unknown"))
    assert code == 0 and unknown["ok"]
    assert unknown["result"]["reconcile_required"] is True
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        locks = conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (env["run_id"],)).fetchone()[0]
    assert run["state"] == "reconciling" and run["stop_confirmed"] == 0 and locks == 1


def test_f8_cancel_ack_does_not_release_native_lock_without_stop_confirmation(actual_context_env):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    started, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and started["ok"]
    native_action = started["result"]["action"]
    started_ack, ack_code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=started["result"]["control_ref"],
        action_nonce=native_action["action_nonce"], expected_run_revision=native_action["expected_run_revision"],
        outcome="started",
        handle_ref={"kind": "native_handle", "id": "native-cancel-fixture-handle",
                    "provider_ref": "fixture-provider-reference"}))
    assert ack_code == 0 and started_ack["ok"]
    canceled, code = execute(env["db"], _control_req(env, "advance_execution_control", cancel=True,
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and canceled["ok"], canceled.get("error")
    action = canceled["result"]["action"]
    assert action["kind"] == "main-native-cancel" and action["action_nonce"] and action["handle_ref"].get("id"), action
    ack, code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=canceled["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"],
        outcome="cancel_requested"))
    assert code == 0 and ack["ok"], (code, ack.get("error"))
    with closing(env["db"].connect()) as conn:
        saved_control = Phase3Storage(env["db"]).get_object("execution_control", env["run_id"], env["project_id"],
            env["actor"], env["session"], conn=conn)
        assert saved_control["body"].get("handle_ref", {}).get("id") == "native-cancel-fixture-handle"
    observed, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and observed["ok"]
    assert observed["result"]["run_state"] == "cancel_requested"
    assert observed["result"]["locks_retained"] is True
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        locks = conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (env["run_id"],)).fetchone()[0]
    assert run["state"] == "cancel_requested" and run["stop_confirmed"] == 0 and locks == 1


def test_f8_stale_f5_source_fails_before_native_dispatch(actual_context_env):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    graph_path = env["workspace"] / env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["premise"] = "Changed after the bounded context was pinned"
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    response, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 3 and not response["ok"]
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT state FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        runner = conn.execute("SELECT 1 FROM operation_journal WHERE kind='runner' AND id=?",
                              (str(uuid.uuid5(uuid.UUID(env["run_id"]), "pmt-runner-dispatch")),)).fetchone()
        assert run["state"] == "starting" and runner is None


def test_f8_retry_requires_transient_stop_and_new_context_source_match(actual_context_env, monkeypatch):
    env = actual_context_env
    previous_run_id = env["run_id"]
    context, reuse_ref = _native_control_fixture(env)
    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0
    action = first["result"]["action"]
    attached, code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"], outcome="started",
        handle_ref={"kind": "native_handle", "id": "retry-native-handle",
                    "provider_ref": "fixture-provider-reference"}))
    assert code == 0 and attached["ok"]
    with closing(env["db"].connect()) as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()[0]
    reconciled, code = execute(env["db"], _actual_request(env, "reconcile_execution",
        expected_run_revision=revision, stopped=True, not_started=False,
        actual_state="failed", evidence_refs=[env["evidence_id"]]))
    assert code == 0 and reconciled["result"]["state"] == "failed"
    import pmt.runners.service as runner_service
    monkeypatch.setattr(runner_service, "observe_local_runtime", lambda _db, request: {
        "run_id": request["payload"]["run_id"], "status": "terminal", "run_state": "failed",
        "runner_kind": "native", "receipt_ref": env["evidence_id"], "receipt_sha256": "a" * 64,
        "failure_class": "transient_network", "retryable": True, "stop_confirmed": True})
    retry, code = execute(env["db"], _control_req(env, "advance_execution_control", retry=True,
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and retry["ok"], retry.get("error")
    next_run = retry["result"]["run_id"]
    assert retry["result"]["attempt"] == 2 and retry["result"]["retry_count"] == 1
    replay_retry, replay_code = execute(env["db"], _control_req(env, "advance_execution_control", retry=True,
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert replay_code == 0 and replay_retry["ok"] and replay_retry["result"]["run_id"] == next_run
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT count(*) FROM execution_runs WHERE job_id=(SELECT job_id FROM execution_runs WHERE id=?)",
                            (env["run_id"],)).fetchone()[0] == 2

    # Prepare the new P2 attempt so F5 can bind a current claimed run, then change
    # the authoritative graph. F8 must stop before the new runner dispatch.
    env = {**env, "run_id": next_run}
    queued, code = execute(env["db"], _control_req(env, "advance_execution_control", run_id=next_run))
    assert code == 0 and queued["ok"] and queued["result"]["action"]["reason"] == "verified_context_required", (code, queued.get("error"))
    graph_path = env["workspace"] / env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["premise"] = "Changed before a retry context was authorized"
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    pin = _actual_f3_ready(env)
    rebuilt, code = _build_actual(env, pin)
    assert code == 0 and rebuilt["ok"]
    new_reuse = _reuse_ref(env, pin)
    refused, code = execute(env["db"], _control_req(env, "advance_execution_control", run_id=next_run,
        context_ref=rebuilt["result"]["context_ref"], reuse_body_ref=new_reuse, previous_run_id=previous_run_id))
    assert code == 0 and refused["ok"], refused.get("error")
    assert refused["result"]["action"]["reason"] == "retry_source_changed"
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?", (next_run,)).fetchone()
        runner = conn.execute("SELECT 1 FROM operation_journal WHERE kind='runner' AND id=?",
            (str(uuid.uuid5(uuid.UUID(next_run), "pmt-runner-dispatch")),)).fetchone()
    assert run["state"] == "blocked" and run["stop_confirmed"] == 1 and runner is None


def test_f8_retry_count_two_is_a_hard_cap(actual_context_env):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env)
    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0
    action = first["result"]["action"]
    attached, code = execute(env["db"], _control_req(env, "acknowledge_execution_action",
        run_id=env["run_id"], control_ref=first["result"]["control_ref"],
        action_nonce=action["action_nonce"], expected_run_revision=action["expected_run_revision"],
        outcome="started", handle_ref={"kind": "native_handle", "id": "retry-cap-handle",
                                        "provider_ref": "fixture-provider-reference"}))
    assert code == 0 and attached["ok"]
    with closing(env["db"].connect()) as conn:
        stored = Phase3Storage(env["db"]).get_object("execution_control", env["run_id"], env["project_id"],
            env["actor"], env["session"], conn=conn)
    body = dict(stored["body"], retry_count=2)
    Phase3Storage(env["db"]).put_object("execution_control", env["run_id"], env["project_id"],
        env["actor"], env["session"], stored["source_hash"], stored["revision"], body,
        request_id=new_id())
    with closing(env["db"].connect()) as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()[0]
    ended, code = execute(env["db"], _actual_request(env, "reconcile_execution",
        expected_run_revision=revision, stopped=True, not_started=False,
        actual_state="failed", evidence_refs=[env["evidence_id"]]))
    assert code == 0 and ended["result"]["state"] == "failed"
    result, code = execute(env["db"], _control_req(env, "advance_execution_control", retry=True,
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 3 and result["error"]["code"] == "retry_limit"
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT count(*) FROM execution_runs WHERE step_id=?",
                            (env["step_id"],)).fetchone()[0] == 1


def test_f8_local_cli_dispatch_uses_bounded_prompt_observes_without_per_poll_event(actual_context_env, monkeypatch):
    env = actual_context_env
    context, reuse_ref = _native_control_fixture(env, _route(mode="cli", agent="codex"))
    import pmt.efficiency.control as control_module
    import pmt.runners.service as runner_service
    original_launch = runner_service._launch_helper
    captured = {"prompt": None, "notice_ids": []}
    def launch_fixture(config_path, prompt):
        captured["prompt"] = prompt
        config = json.loads(config_path.read_text(encoding="utf-8"))
        report = {"summary": "fixture report", "choices": [],
                  "criteria_results": [{"criterion_id": item["id"], "outcome": "not_run",
                                         "evidence_refs": []} for item in env["criteria"]],
                  "tests": [], "evidence_refs": [], "unresolved_items": []}
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}}
        config.update(contract_fixture=True, fixture_process=True,
                      fixture_stdout=json.dumps(event) + "\n", fixture_stderr="", fixture_delay=2.0)
        runner_service._write_private_json(config_path, config)
        return original_launch(config_path, prompt)
    monkeypatch.setattr(runner_service.shutil, "which", lambda _name: "fixture-cli")
    monkeypatch.setattr(runner_service, "_launch_helper", launch_fixture)
    base_controller = control_module.ExecutionController
    class FixedController(base_controller):
        def __init__(self, *args, **kwargs):
            kwargs.update(clock=lambda: datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc),
                          notifier=lambda notice_id, _notice: captured["notice_ids"].append(notice_id) or True)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(control_module, "ExecutionController", FixedController)

    first, code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert code == 0 and first["ok"], first.get("error")
    assert first["result"]["action"]["kind"] == "wait"
    next_poll = datetime.fromisoformat(first["result"]["action"]["next_poll_at"].replace("Z", "+00:00"))
    controlled_now = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    assert timedelta(0) < next_poll - controlled_now <= timedelta(seconds=60)
    assert first["result"]["notification"]["path"] == "explicit_query"
    assert "Preserve source and criteria" in captured["prompt"]
    assert "Approved directive:" not in captured["prompt"]
    time.sleep(0.1)
    with closing(env["db"].connect()) as conn:
        control_before = conn.execute("SELECT revision FROM phase3_objects WHERE kind='execution_control' AND id=?",
                                      (env["run_id"],)).fetchone()[0]
        poll_events_before = conn.execute("SELECT count(*) FROM events WHERE event_type='runner.poll_observed'").fetchone()[0]
    same, same_code = execute(env["db"], _control_req(env, "advance_execution_control",
        run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
    assert same_code == 0 and same["ok"]
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT revision FROM phase3_objects WHERE kind='execution_control' AND id=?",
                            (env["run_id"],)).fetchone()[0] == control_before
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='runner.poll_observed'").fetchone()[0] == poll_events_before
    deadline = time.time() + 8
    final = None
    while time.time() < deadline:
        final, final_code = execute(env["db"], _control_req(env, "advance_execution_control",
            run_id=env["run_id"], context_ref=context["context_ref"], reuse_body_ref=reuse_ref))
        assert final_code == 0 and final["ok"], final.get("error")
        if final["result"]["action"]["kind"] == "read-result":
            break
        time.sleep(0.2)
    assert final and final["result"]["action"]["kind"] == "read-result", final
    assert len(captured["notice_ids"]) <= 2
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='runner.poll_observed'").fetchone()[0] == 1
        run = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?", (env["run_id"],)).fetchone()
        assert run["state"] == "review_pending" and run["stop_confirmed"] == 1
