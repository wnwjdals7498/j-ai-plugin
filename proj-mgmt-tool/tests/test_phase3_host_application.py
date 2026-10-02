"""Storage-layer Host preparation; actual network acceptance is a separate gate."""
import copy
import uuid
from contextlib import closing

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.application import HostApplication


def request(op, payload=None, **kw):
    return {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": op,
            "actor": "main", "session_id": "device-a-session", "payload": payload or {}, **kw}


def headers(app, session_id, scopes=("*",), permissions=("read", "write", "runtime", "review", "admin")):
    issued = app.auth.issue_device("main", list(scopes), list(permissions))
    value = {"authorization": "Bearer " + issued["credential"], "x-pmt-device": issued["device_id"],
             "x-pmt-environment": str(uuid.uuid4()), "x-pmt-namespace": app.auth.namespace_id,
             "x-pmt-session": session_id}
    app.register_session(value, {"session_id": session_id, "environment_id": value["x-pmt-environment"]})
    return value


def call(app, value, auth):
    result, code = app.execute(value, auth)
    assert code == 0, result
    return result["result"]


def test_claim_ref_replay_owner_revision_and_release_without_token_persistence(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    key = b"isolated storage test key, never a production credential"
    app = HostApplication(db, {"fixture": key}, "fixture")
    auth_a = headers(app, "device-a-session")
    project = call(app, request("create_scope", {"kind": "project", "slug": "host-fixture"}), auth_a)
    item = call(app, request("save_change", {"kind": "item", "title": "owned fixture", "reason": "test"}, scope_id=project["id"]), auth_a)
    claim = request("claim_task", record_id=item["id"], expected_revision=1)
    result = call(app, claim, auth_a)
    assert "claim_token" not in result and result["claim_ref"]["record_id"] == item["id"]
    assert call(app, claim, auth_a) == result
    lookup = app.get_request_result(claim["request_id"], auth_a)
    assert lookup["envelope"]["result"] == result
    changed = copy.deepcopy(claim)
    changed["payload"]["reason"] = "different semantic request"
    reply, code = app.execute(changed, auth_a)
    assert code == 3 and reply["error"]["code"] == "request_conflict"
    auth_b = headers(app, "device-b-session", (project["id"],), ("read", "write"))
    rival = request("claim_task", record_id=item["id"], expected_revision=1, session_id="device-b-session")
    reply, code = app.execute(rival, auth_b)
    assert code == 3 and reply["error"]["code"] in {"claim_conflict", "revision_conflict"}
    with pytest.raises(PmtError, match="another session"):
        app.get_request_result(claim["request_id"], auth_b)
    with closing(db.connect()) as conn:
        lease = dict(conn.execute("SELECT * FROM host_claim_leases WHERE id=?", (claim["request_id"],)).fetchone())
        raw_token = app._token(lease)
        dump = "\n".join(conn.iterdump())
        assert raw_token not in dump and key.decode() not in dump
        assert auth_a["authorization"][7:] not in dump
    release = request("release_claim", {"claim_ref": result["claim_ref"], "status": "Paused", "reason": "fixture", "resume": "continue"},
                      record_id=item["id"], expected_revision=2)
    assert call(app, release, auth_a)["state"] == "Paused"
    stale = request("release_claim", release["payload"], record_id=item["id"], expected_revision=3)
    reply, code = app.execute(stale, auth_a)
    assert code == 3 and reply["error"]["code"] == "claim_conflict"


def test_auth_scope_revocation_replay_and_local_process_allowlist(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    app = HostApplication(db, {"fixture": b"x" * 32}, "fixture")
    bootstrap = headers(app, "device-a-session")
    first = call(app, request("create_scope", {"kind": "project", "slug": "first"}), bootstrap)
    second = call(app, request("create_scope", {"kind": "project", "slug": "second"}), bootstrap)
    restricted = headers(app, "restricted-session", (first["id"],), ("read", "write"))
    hidden = request("read_context", scope_id=second["id"], session_id="restricted-session")
    with pytest.raises(PmtError, match="outside"):
        app.execute(hidden, restricted)
    spoofed = request("read_context", scope_id=first["id"], session_id="restricted-session", actor="other")
    with pytest.raises(PmtError, match="Body identity"):
        app.execute(spoofed, restricted)
    for op in ("dispatch_execution", "poll_execution", "publish_project_docs", "apply_graph_change", "register_resource", "save_routing_policy"):
        with pytest.raises(PmtError, match="local client runtime"):
            app.execute(request(op, scope_id=first["id"], session_id="restricted-session"), restricted)
    saved = request("save_change", {"kind": "item", "title": "restricted", "reason": "test"}, scope_id=first["id"], session_id="restricted-session")
    call(app, saved, restricted)
    app.auth.update_grants(restricted["x-pmt-device"], 1, [second["id"]], ["read", "write"])
    with pytest.raises(PmtError, match="outside"):
        app.get_request_result(saved["request_id"], restricted)
    with pytest.raises(PmtError, match="outside"):
        app.execute(saved, restricted)
    # Revocation also blocks the cached response, before any business handler.
    app.auth.revoke_device(restricted["x-pmt-device"], 2)
    with pytest.raises(PmtError, match="authentication failed"):
        app.get_request_result(saved["request_id"], restricted)
