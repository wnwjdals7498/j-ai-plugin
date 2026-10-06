"""Host storage-only continuity boundary tests on isolated SQLite fixtures."""
from __future__ import annotations

from contextlib import closing

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.application import HostApplication
from pmt.host.data import HostDataExtension
from pmt.host.resources import HostResourceStore
from pmt.continuity.current import _snapshot, basis_body
from pmt.util import new_id


def _request(operation, actor, session, scope, payload, *, request_id=None):
    request = {"protocol_version": 1, "request_id": request_id or new_id(),
        "operation": operation, "actor": actor, "session_id": session,
        "payload": payload}
    if scope is not None:
        request["scope_id"] = scope
    return request


def _host(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    app = HostApplication(db, {"fixture": b"phase4 isolated Host test key material"}, "fixture")
    resources = HostResourceStore(db, app.auth)
    extension = HostDataExtension(db, app.auth, resources, authorizer=app.authorize)
    app.extension = extension
    issued = app.auth.issue_device("continuity-user", ["*"], ["read", "write"])
    session = "continuity-session"
    headers = {"authorization": "Bearer " + issued["credential"],
        "x-pmt-device": issued["device_id"], "x-pmt-environment": new_id(),
        "x-pmt-namespace": app.auth.namespace_id, "x-pmt-session": session}
    app.register_session(headers, {"session_id": session, "environment_id": headers["x-pmt-environment"]})
    created, code = app.execute(_request("create_scope", "continuity-user", session, None,
        {"kind": "project", "slug": "continuity-fixture"}), headers)
    assert code == 0 and created["ok"], created
    return db, app, headers, created["result"]["id"], issued


def test_host_continuity_shared_storage_replay_pointer_cas_and_allowlist(tmp_path):
    _db, app, headers, project, issued = _host(tmp_path)
    actor, session = "continuity-user", headers["x-pmt-session"]
    create = _request("put_continuity_object", actor, session, project,
        {"kind": "basis", "body": {"capture": "fixture", "coherence": "unknown"},
         "visibility": "shared", "event_id": new_id()})
    first, code = app.execute(create, headers)
    assert code == 0 and first["ok"], first
    replay, code = app.execute(create, headers)
    assert code == 0 and replay["result"] == first["result"]

    selector = {"purpose": "basis"}
    initial, code = app.execute(_request("read_continuity_pointer", actor, session, project,
        {"selector": selector}), headers)
    assert code == 0 and initial["result"]["revision"] == 0
    advance, code = app.execute(_request("advance_continuity_pointer", actor, session, project,
        {"selector": selector, "object_id": first["result"]["id"], "expected_pointer_revision": 0}), headers)
    assert code == 0 and advance["result"]["revision"] == 1
    stale, code = app.execute(_request("advance_continuity_pointer", actor, session, project,
        {"selector": selector, "object_id": first["result"]["id"], "expected_pointer_revision": 0}), headers)
    assert code == 3 and stale["error"]["code"] == "revision_conflict"
    mismatched, code = app.execute(_request("advance_continuity_pointer", actor, session, project,
        {"selector": {"purpose": "basis", "branch": "main"},
         "object_id": first["result"]["id"], "expected_pointer_revision": 0}), headers)
    assert code == 3 and mismatched["error"]["code"] == "continuity_pointer_mismatch"

    with pytest.raises(PmtError) as private:
        app.execute(_request("put_continuity_object", actor, session, project,
            {"kind": "bundle", "body": {"summary": "synthetic private fixture"}, "visibility": "private"}), headers)
    assert private.value.code == "private_metadata_forbidden"
    with pytest.raises(PmtError) as checkpoint_object:
        app.execute(_request("put_continuity_object", actor, session, project,
            {"kind": "checkpoint", "body": {"boundary_kind": "caller_claim"}}), headers)
    assert checkpoint_object.value.code == "private_metadata_forbidden"
    with pytest.raises(PmtError) as alignment_object:
        app.execute(_request("put_continuity_object", actor, session, project,
            {"kind": "alignment", "body": {"state": "applied"}}), headers)
    assert alignment_object.value.code == "private_metadata_forbidden"
    forbidden_prompt, code = app.execute(_request("put_continuity_object", actor, session, project,
        {"kind": "basis", "body": {"prompt": "synthetic-prompt-sentinel"}}), headers)
    assert code == 2 and forbidden_prompt["error"]["code"] == "private_metadata_forbidden"
    forged_basis_publish = {"project_id": project, "complete": True,
        "manifest": {"coherence": "coherent"}}
    with pytest.raises(PmtError) as forged:
        app.execute(_request("publish_work_basis", actor, session, project, forged_basis_publish), headers)
    assert forged.value.code == "host_input_invalid"
    with closing(_db.connect()) as conn:
        conn.execute("BEGIN")
        current_snapshot = _snapshot(conn, project)
    valid_basis = basis_body(
        scope={"project_id": project, "repository_id": new_id()},
        source={"repository_id": new_id(), "branch": "main", "workspace_ref": "fixture-workspace",
            "observed_head": "a" * 40, "analyzed_ref": None, "applied_ref": None,
            "dirty_state": "clean", "dirty_fingerprint": None, "inventory_ref": "fixture-inventory",
            "inventory_hash": "b" * 64, "inventory_coverage": {"selected_count": 0,
                "verified_count": 0, "unknown_count": 0, "complete": True, "reason_codes": []}},
        contract={"graph_schema": 1, "graph_revision": 1, "graph_hash": "c" * 64,
            "requirement_refs": [], "decision_refs": []},
        work={"capture_ref": current_snapshot["snapshot_hash"], "task_id": None, "records": [],
            "run_refs": [], "claim_refs": [], "pending_refs": []},
        conditions={"environment_id": headers["x-pmt-environment"], "selected": ["fixture"], "unknown": []},
        manifest={"components": [{"component": "synthetic", "complete": True}],
            "coherence": "coherent", "captured_at": "2026-10-06T00:00:00Z"})
    basis, code = app.execute(_request("put_continuity_object", actor, session, project,
        {"kind": "basis", "body": valid_basis}), headers)
    assert code == 0 and basis["ok"]
    unconfirmed, code = app.execute(_request("create_checkpoint", actor, session, project,
        {"basis_ref": basis["result"]["id"], "boundary_event_id": new_id(),
         "boundary_kind": "caller_claim", "purpose": "checkpoint",
         "expected_pointer_revision": 0}), headers)
    assert code == 3 and unconfirmed["error"]["code"] == "checkpoint_boundary_not_found"
    alignment_pointer, code = app.execute(_request("advance_continuity_pointer", actor, session, project,
        {"selector": {"purpose": "applied_alignment"},
         "object_id": basis["result"]["id"], "expected_pointer_revision": 0}), headers)
    assert code == 3 and alignment_pointer["error"]["code"] == "continuity_boundary_required"
    with pytest.raises(PmtError) as missing_receipt:
        app.execute(_request("apply_alignment_receipt", actor, session, project, {}), headers)
    assert missing_receipt.value.code == "host_alignment_receipt_invalid"

    readonly = app.auth.issue_device(actor, [project], ["read"])
    readonly_headers = {**headers, "authorization": "Bearer " + readonly["credential"],
        "x-pmt-device": readonly["device_id"], "x-pmt-session": "continuity-reader",
        "x-pmt-environment": new_id()}
    app.register_session(readonly_headers, {"session_id": "continuity-reader",
        "environment_id": readonly_headers["x-pmt-environment"]})
    with pytest.raises(PmtError) as denied_write:
        app.execute(_request("put_continuity_object", actor, "continuity-reader", project,
            {"kind": "basis", "body": {"capture": "unauthorized"}}), readonly_headers)
    assert denied_write.value.code == "scope_forbidden"


def test_host_continuity_foreign_scope_and_private_effect_are_not_visible(tmp_path):
    db, app, headers, project, _issued = _host(tmp_path)
    actor, session = "continuity-user", headers["x-pmt-session"]
    foreign, code = app.execute(_request("create_scope", actor, session, None,
        {"kind": "project", "slug": "foreign"}), headers)
    assert code == 0
    foreign_id = foreign["result"]["id"]
    req = _request("put_continuity_object", actor, session, project,
        {"kind": "basis", "body": {"capture": "fixture"}, "visibility": "shared"})
    saved, code = app.execute(req, headers)
    assert code == 0 and saved["ok"]
    with pytest.raises(PmtError) as outside:
        app.execute(_request("get_continuity_object", actor, session, foreign_id,
            {"object_id": saved["result"]["id"]}), headers)
    assert outside.value.code in {"continuity_not_found", "scope_forbidden"}

    # Body fields cannot choose a different owner; the server binds the journal
    # to the authenticated request identity and never transfers that ownership.
    effect, code = app.execute(_request("begin_continuity_effect", actor, session, project,
        {"kind": "fixture", "body": {"source": "synthetic"}}), headers)
    assert code == 0 and effect["ok"]
    with pytest.raises(PmtError) as unverified_complete:
        app.execute(_request("update_continuity_effect", actor, session, project,
            {"effect_id": effect["result"]["id"], "state": "completed",
             "outcome": {"actual": True}}), headers)
    assert unverified_complete.value.code == "continuity_boundary_required"
    foreign_session = "other-session"
    other = app.auth.issue_device(actor, [project], ["read", "write"])
    other_headers = {**headers, "authorization": "Bearer " + other["credential"],
        "x-pmt-device": other["device_id"], "x-pmt-session": foreign_session,
        "x-pmt-environment": new_id()}
    app.register_session(other_headers, {"session_id": foreign_session,
        "environment_id": other_headers["x-pmt-environment"]})
    with pytest.raises(PmtError) as hidden:
        app.execute(_request("get_continuity_effect", actor, foreign_session, project,
            {"effect_id": effect["result"]["id"]}), other_headers)
    assert hidden.value.code == "effect_not_found"
