"""The Host returns only current, scoped decision-event receipt metadata."""
from __future__ import annotations

import json
from contextlib import closing

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.util import canonical_json, new_id
from pmt.host.alignment_receipts import _quiescence_conflict
from test_phase3_hosted_runtime import _host_request


def _decision_request(env, *, record_id, payload, expected_revision=1, session=None, actor=None):
    return {"protocol_version": 1, "operation": "save_decision", "request_id": new_id(),
        "actor": actor or env["actor"], "session_id": session or env["session_a"],
        "scope_id": env["project"], "record_id": record_id, "expected_revision": expected_revision,
        "payload": payload, "source": {"product": "fixture"}}


def test_host_decision_receipt_is_actual_current_scoped_and_bounded(live_host, monkeypatch):
    env = live_host
    marker = "decision-private-sentinel"
    saved, code = env["store_a"].execute(_decision_request(env, record_id=env["item"], payload={
        "decision_kind": "delegate", "decider": "fixture user", "reason": marker,
        "confirmation_source": "explicit fixture choice", "delegation_scope": "method-only"}))
    assert code == 0 and saved["ok"], saved.get("error")
    decision_ref = saved["result"]["decision_id"]

    payload = {"decision_ref": decision_ref, "decision_revision": 1,
        "expected_kind": "delegate", "run_id": env["run"], "expected_run_revision": 3}
    request = _host_request(env, "read_decision_receipt", payload)
    response, code = env["store_a"].execute(request)
    assert code == 0 and response["ok"], response.get("error")
    result = response["result"]
    assert result["decision_ref"] == decision_ref and result["revision"] == 1
    assert result["scope_id"] == env["project"] and result["target_ref"] == env["item"]
    assert result["kind"] == "delegate" and result["state"] == "Current"
    assert result["event_ref"] and len(result["event_ref_hash"]) == 64
    assert marker not in canonical_json(result)
    with closing(env["db"].connect()) as conn:
        event = conn.execute("SELECT id,record_id,scope_id,payload_json FROM events WHERE id=?",
            (result["event_ref"],)).fetchone()
    assert event and event["record_id"] == env["item"] and event["scope_id"] == env["project"]
    assert json.loads(event["payload_json"])["decision_id"] == decision_ref

    stale, code = env["store_a"].execute(_host_request(env, "read_decision_receipt",
        payload | {"decision_revision": 2}))
    assert code == 3 and stale["error"]["code"] == "decision_receipt_unavailable"
    wrong_kind, code = env["store_a"].execute(_host_request(env, "read_decision_receipt",
        payload | {"expected_kind": "select"}))
    assert code == 3 and wrong_kind["error"]["code"] == "decision_receipt_unavailable"

    other_owner = {**env, "actor": "network-client-b", "session_a": env["session_b"]}
    denied, code = env["store_b"].execute(_host_request(other_owner, "read_decision_receipt", payload))
    assert code == 3 and denied["error"]["code"] == "workspace_authority_stale"

    from pmt.http_store import HttpStore
    reader = env["app"].auth.issue_device("network-readonly", [env["project"]], ["read"])
    reader_session, reader_environment = "network-readonly-session", new_id()
    reader_headers = {"authorization": "Bearer " + reader["credential"],
        "x-pmt-device": reader["device_id"], "x-pmt-environment": reader_environment,
        "x-pmt-namespace": env["app"].auth.namespace_id, "x-pmt-session": reader_session}
    env["app"].register_session(reader_headers,
        {"session_id": reader_session, "environment_id": reader_environment})
    monkeypatch.setenv("PMT_DECISION_READ_TOKEN", reader["credential"])
    reader_store = HttpStore(env["base"], "PMT_DECISION_READ_TOKEN", reader["device_id"],
        reader_environment, env["app"].auth.namespace_id, ca_file=str(env["cert"]), timeout=2)
    reader_store.register_session(reader_session)
    reader_request = _host_request(env, "read_decision_receipt", payload)
    reader_request.update(actor="network-readonly", session_id=reader_session)
    no_runtime, code = reader_store.execute(reader_request)
    assert code == 3 and no_runtime["error"]["code"] == "scope_forbidden"

    superseding, code = env["store_a"].execute(_decision_request(env, record_id=env["item"], expected_revision=2, payload={
        "decision_kind": "select", "decider": "fixture user", "reason": "new current choice",
        "confirmation_source": "explicit fixture choice", "option_id": "continue",
        "supersedes": decision_ref}))
    assert code == 0 and superseding["ok"], superseding.get("error")
    revoked, code = env["store_a"].execute(_host_request(env, "read_decision_receipt", payload))
    assert code == 3 and revoked["error"]["code"] == "decision_receipt_unavailable"


def test_host_alignment_quiescence_blocks_affected_step_but_allows_safe_work_item_scope(live_host):
    env = live_host
    from pmt.efficiency.storage import Phase3Storage
    from pmt.util import utc_now
    now = utc_now()
    effects = Phase3Storage(env["db"])
    effect_ids = [new_id(), new_id()]
    with env["db"].write() as conn:
        for effect_id, kind in zip(effect_ids, ("graph_change", "document_render")):
            effects.put_object("host_local_file_effect", effect_id, env["project"], env["actor"],
                env["session_a"], "a" * 64, 0,
                {"effect_kind": kind, "state": "completed", "phase": "completed"},
                state="completed", request_id=new_id(), conn=conn)
    with closing(env["db"].connect()) as conn:
        conn.execute("BEGIN")
        safe = _quiescence_conflict(conn, {"scope_id": env["project"]}, env["run"],
            env["canonical"], [env["work"], env["item"]])
        active_step = _quiescence_conflict(conn, {"scope_id": env["project"]}, env["run"],
            env["canonical"], [env["step"]])
        other_run = _quiescence_conflict(conn, {"scope_id": env["project"]}, new_id(),
            env["canonical"], [])
        conn.rollback()
    assert safe is None
    assert active_step and active_step["kind"] == "affected_step_run"
    assert other_run and other_run["kind"] == "active_workspace_run"
    with env["db"].write() as conn:
        effects.put_object("host_local_file_effect", effect_ids[1], env["project"], env["actor"],
            env["session_a"], "b" * 64, 1,
            {"effect_kind": "document_render", "state": "intent", "phase": "candidate_staged"},
            state="intent", request_id=new_id(), conn=conn)
    with closing(env["db"].connect()) as conn:
        conn.execute("BEGIN")
        pending_file = _quiescence_conflict(conn, {"scope_id": env["project"]}, env["run"],
            env["canonical"], [env["work"], env["item"]])
        conn.rollback()
    assert pending_file and pending_file["kind"] == "document_render"
    with env["db"].write() as conn:
        conn.execute("UPDATE phase3_objects SET state='completed',body_json=?,updated_at=? "
            "WHERE kind='host_local_file_effect' AND id=?",
            (canonical_json({"effect_kind": "document_render", "state": "completed", "phase": "completed"}),
             now, effect_ids[1]))
        pending_id, request_id = new_id(), new_id()
        conn.execute("INSERT INTO continuity_journal(id,request_id,kind,scope_id,owner_actor,owner_session,"
            "basis_hash,body_hash,body_json,state,outcome_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,NULL,?,?, 'unknown',NULL,?,?)",
            (pending_id, request_id, "fixture_effect", env["project"], env["actor"], env["session_a"],
             "c" * 64, "{}", now, now))
    with closing(env["db"].connect()) as conn:
        conn.execute("BEGIN")
        pending_continuity = _quiescence_conflict(conn, {"scope_id": env["project"]}, env["run"],
            env["canonical"], [env["work"], env["item"]])
        conn.rollback()
    assert pending_continuity and pending_continuity["conflict_ref"] == pending_id
