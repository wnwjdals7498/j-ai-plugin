"""Control metadata and original response cache share current Host authority."""
import copy

import pytest

from pmt.db import semantic_request_fingerprint
from pmt.errors import PmtError
from pmt.util import new_id
from test_phase3_host_data import host_data_env, _publish_source


def request(env, op, payload):
    return {"protocol_version": 1, "operation": op, "request_id": new_id(), "actor": env["actor"],
            "session_id": env["session"], "scope_id": env["project"], "payload": payload,
            "context_refs": [], "source": {}}


def _native_action(env, context_ref, nonce):
    return {"kind": "main-native-call", "run_id": env["run"], "expected_run_revision": 3,
        "action_nonce": nonce, "context_ref": context_ref, "prompt_sha256": "b" * 64,
        "capability_ref": "fixture-capability", "directive_ref": "fixture-directive-ref",
        "agent": "fixture", "provider": "fixture", "model": "fixture-model",
        "instruction": ("Use only the current bounded F5 context. Invoke exactly one native subagent call. "
            "Do not read or request a larger private directive. Return its real opaque handle and final result "
            "through the supplied operations; do not report a test or evidence that was not observed."),
        "return_contract": {"native_handle": "attach_execution_handle after actual invocation",
            "result_operation": "submit_execution_result after actual completion"}}


def _context(env):
    return {"kind": "task_context", "id": new_id(), "scope_id": env["project"],
        "source_hash": env["pin"].source_hash, "version": 1, "projection_hash": "a" * 64}


def _persist_pending(env, body):
    write = request(env, "write_execution_control", {"run_id": env["run"],
        "source_hash": env["pin"].source_hash, "expected_control_revision": 0,
        "body": body, "event_name": "control.state_observed"})
    result, code = env["app"].execute(write, env["headers"])
    assert code == 0, result
    return result["result"]["control_ref"]


def test_host_control_cas_cache_original_without_revision_bump_and_semantic_conflict(host_data_env):
    env = host_data_env
    app = env["app"]
    app.extension = env["extension"]
    _publish_source(env)
    nonce, context_ref = new_id(), _context(env)
    action = _native_action(env, context_ref, nonce)
    body = {"stage": "main_action_pending", "source_hash": env["pin"].source_hash,
        "context_ref": context_ref, "action_nonce": nonce, "run_id": env["run"], "prompt_sha256": "b" * 64,
        "action": {key: value for key, value in action.items() if key not in {"instruction", "return_contract"}}}
    original = request(env, "advance_execution_control", {"run_id": env["run"],
        "context_ref": context_ref, "reuse_body_ref": {"id": new_id()}})
    body["action_response_request_id"] = original["request_id"]
    ref = _persist_pending(env, body)
    response_result = {"control_ref": ref, "run_id": env["run"], "run_state": "running",
        "locks_retained": True, "action": action}
    complete = request(env, "write_execution_control", {"run_id": env["run"],
        "source_hash": env["pin"].source_hash, "expected_control_revision": 1,
        "body": body, "original_request": original,
        "original_response": {"result": response_result, "exit_code": 0}})
    final, code = app.execute(complete, env["headers"])
    assert code == 0 and final["result"]["revision"] == 1, final
    assert app.execute(complete, env["headers"]) == (final, code)
    read = request(env, "read_execution_control", {"run_id": env["run"], "include_pending_action": True})
    current, code = app.execute(read, env["headers"])
    assert code == 0 and current["result"]["pending_action"] == action, current
    from pmt.db import semantic_request_fingerprint
    cached = app.get_request_result(original["request_id"], env["headers"], semantic_request_fingerprint(original))
    assert cached["envelope"]["result"] == response_result
    changed = copy.deepcopy(original)
    changed["payload"]["cancel"] = True
    with pytest.raises(PmtError, match="different request"):
        app.get_request_result(original["request_id"], env["headers"], semantic_request_fingerprint(changed))


def test_host_control_original_response_rejects_forged_nonce_source_schema_and_revision(host_data_env):
    env = host_data_env
    app = env["app"]
    app.extension = env["extension"]
    _publish_source(env)
    nonce, context_ref = new_id(), _context(env)
    action = _native_action(env, context_ref, nonce)
    body = {"stage": "main_action_pending", "source_hash": env["pin"].source_hash,
        "context_ref": context_ref, "action_nonce": nonce, "run_id": env["run"], "prompt_sha256": "b" * 64,
        "action": {key: value for key, value in action.items() if key not in {"instruction", "return_contract"}}}
    original = request(env, "advance_execution_control", {"run_id": env["run"],
        "context_ref": context_ref, "reuse_body_ref": {"id": new_id()}})
    body["action_response_request_id"] = original["request_id"]
    ref = _persist_pending(env, body)
    valid_result = {"control_ref": ref, "run_id": env["run"], "run_state": "running",
        "locks_retained": True, "action": action}

    cases = []
    wrong_nonce = copy.deepcopy(valid_result)
    wrong_nonce["action"]["action_nonce"] = new_id()
    cases.append((original, wrong_nonce))
    wrong_source = copy.deepcopy(original)
    wrong_source["source"] = {"source_hash": "c" * 64}
    cases.append((wrong_source, valid_result))
    wrong_schema = copy.deepcopy(valid_result)
    wrong_schema["action"]["argv"] = ["runner"]
    cases.append((original, wrong_schema))
    wrong_ref = copy.deepcopy(valid_result)
    wrong_ref["control_ref"]["revision"] += 1
    cases.append((original, wrong_ref))
    for original_request, response_result in cases:
        complete = request(env, "write_execution_control", {"run_id": env["run"],
            "source_hash": env["pin"].source_hash, "expected_control_revision": 1, "body": body,
            "original_request": original_request,
            "original_response": {"result": response_result, "exit_code": 0}})
        rejected, code = app.execute(complete, env["headers"])
        assert code == 3 and rejected["error"]["code"] == "control_response_conflict", rejected
        assert app.get_request_result(original_request["request_id"], env["headers"])["envelope"] is None


def test_host_control_ack_response_binds_original_nonce_and_semantic_request(host_data_env):
    env = host_data_env
    app = env["app"]
    app.extension = env["extension"]
    _publish_source(env)
    nonce, context_ref = new_id(), _context(env)
    first_body = {"stage": "main_action_pending", "source_hash": env["pin"].source_hash,
        "context_ref": context_ref, "action_nonce": nonce, "run_id": env["run"], "prompt_sha256": "b" * 64,
        "action": {"kind": "main-native-call", "run_id": env["run"], "expected_run_revision": 3,
            "action_nonce": nonce, "context_ref": context_ref, "prompt_sha256": "b" * 64}}
    first_ref = _persist_pending(env, first_body)
    from pmt.db import semantic_request_fingerprint
    original = request(env, "acknowledge_execution_action", {"run_id": env["run"],
        "control_ref": first_ref, "action_nonce": nonce, "outcome": "started",
        "expected_run_revision": 3,
        "handle_ref": {"kind": "native_handle", "id": "handle-1", "provider_ref": "provider-1"}})
    handle_ref = original["payload"]["handle_ref"]
    ack = {"nonce": nonce, "outcome": "started", "handle_ref": handle_ref,
        "run_state": "running", "request_fingerprint": semantic_request_fingerprint(original)}
    second_body = {"stage": "running", "source_hash": env["pin"].source_hash,
        "context_ref": context_ref, "action_nonce": nonce, "run_id": env["run"],
        "prompt_sha256": "b" * 64,
        "action": first_body["action"], "action_ack": ack, "action_acks": {nonce: ack}}
    second = request(env, "write_execution_control", {"run_id": env["run"],
        "source_hash": env["pin"].source_hash, "expected_control_revision": 1, "body": second_body})
    persisted, code = app.execute(second, env["headers"])
    assert code == 0, persisted
    current_ref = persisted["result"]["control_ref"]
    result = {"control_ref": current_ref, "run_id": env["run"], "acknowledged": True,
        "reconcile_required": False, "run_state": "running", "handle_ref": handle_ref,
        "locks_retained": True,
        "action": {"kind": "wait", "run_id": env["run"], "reason": "native_handle_attached",
            "next_poll_at": "2026-10-02T12:00:00Z", "locks_retained": True}}
    complete = request(env, "write_execution_control", {"run_id": env["run"],
        "source_hash": env["pin"].source_hash, "expected_control_revision": 2, "body": second_body,
        "original_request": original, "original_response": {"result": result, "exit_code": 0}})
    cached, code = app.execute(complete, env["headers"])
    assert code == 0 and cached["result"]["original_response"]["result"] == result, cached


def test_host_control_group_action_cache_checks_prompt_and_current_member_hash_bindings(host_data_env):
    env = host_data_env
    app = env["app"]
    app.extension = env["extension"]
    _publish_source(env)
    nonce, context_ref = new_id(), _context(env)
    group_prompt = "bounded group context for the current F5 source"
    group_sha = __import__("hashlib").sha256(group_prompt.encode("utf-8")).hexdigest()
    member = {"step_id": env["step"], "run_id": env["run"], "role": "worker",
        "directive_version": "1", "directive_ref": "directive-ref", "directive_sha256": "d" * 64,
        "context_ref": context_ref, "criteria": [{"id": "criterion", "sha256": "e" * 64}]}
    member_two = member | {"step_id": new_id(), "run_id": new_id(), "directive_sha256": "c" * 64,
        "context_ref": context_ref | {"id": new_id()}}
    action = {"kind": "main-native-call", "run_id": env["run"], "expected_run_revision": 3,
        "action_nonce": nonce, "context_ref": context_ref, "prompt_sha256": group_sha,
        "capability_ref": "fixture-capability", "directive_ref": "directive-ref",
        "agent": "fixture", "provider": "fixture", "model": "fixture-model",
        "instruction": ("Use only the current bounded F5 context. Invoke exactly one native subagent call. "
            "Do not read or request a larger private directive. Return its real opaque handle and final result "
            "through the supplied operations; do not report a test or evidence that was not observed."),
        "return_contract": {"native_handle": "attach_execution_handle after actual invocation",
            "result_operation": "submit_execution_result after actual completion"},
        "batch_ref": {"kind": "batch_binding", "id": new_id(), "scope_id": env["project"]},
        "batch_report_schema": "pmt-batch-report-v1", "members": [member, member_two],
        "group_context_refs": [context_ref, member_two["context_ref"]], "group_prompt": group_prompt,
        "group_prompt_sha256": group_sha, "source_hash": env["pin"].source_hash,
        "scope_union_sha256": "f" * 64, "physical_slots": 1,
        "group_prompt_accounting": {"group_prompt_bytes": len(group_prompt.encode("utf-8"))}}
    body_action = {key: value for key, value in action.items()
        if key not in {"instruction", "return_contract", "group_prompt", "members"}}
    body_action["member_refs"] = [{key: item[key] for key in ("step_id", "run_id", "role",
        "directive_version", "directive_ref", "directive_sha256", "context_ref", "criteria")}
        for item in (member, member_two)]
    body = {"stage": "main_action_pending", "source_hash": env["pin"].source_hash,
        "scope_id": env["project"],
        "context_ref": context_ref, "action_nonce": nonce, "run_id": env["run"],
        "prompt_sha256": group_sha, "action": body_action}
    original = request(env, "advance_execution_control", {"run_id": env["run"],
        "context_ref": context_ref, "reuse_body_ref": {"id": new_id()}})
    body["action_response_request_id"] = original["request_id"]
    ref = _persist_pending(env, body)
    result = {"control_ref": ref, "run_id": env["run"], "run_state": "running",
        "locks_retained": True, "action": action}
    bad = copy.deepcopy(result)
    bad["action"]["group_prompt"] += " changed"
    for response_result in (bad, result):
        complete = request(env, "write_execution_control", {"run_id": env["run"],
            "source_hash": env["pin"].source_hash, "expected_control_revision": 1, "body": body,
            "original_request": original,
            "original_response": {"result": response_result, "exit_code": 0}})
        cached, code = app.execute(complete, env["headers"])
        if response_result is bad:
            assert code == 3 and cached["error"]["code"] == "control_response_conflict", cached
        else:
            assert code == 0 and cached["result"]["original_response"]["result"] == result, cached
