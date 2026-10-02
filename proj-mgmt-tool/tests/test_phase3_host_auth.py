"""Storage-level Host auth preparation; no HTTP/HTTPS acceptance claim."""
import uuid
from contextlib import closing

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.auth import AuthRegistry
from pmt.service import execute


def request(op, payload=None, **kw):
    return {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": op,
            "actor": "main", "session_id": "auth-fixture", "payload": payload or {}, **kw}


def scope(db, slug, parent_id=None, kind="project"):
    payload = {"kind": kind, "slug": slug}
    if parent_id:
        payload["parent_id"] = parent_id
    result, code = execute(db, request("create_scope", payload))
    assert code == 0, result
    return result["result"]["id"]


def test_device_session_environment_grants_and_hash_only_storage(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    registry = AuthRegistry(db)
    project = scope(db, "allowed")
    child = scope(db, "child", parent_id=project, kind="classification")
    other = scope(db, "denied")
    device = registry.issue_device("main", [project], ["read", "runtime"])
    environment = str(uuid.uuid4())
    registry.register_session(device["credential"], device["device_id"], registry.namespace_id, "native-session-1", environment)
    principal = registry.principal(device["credential"], device["device_id"], registry.namespace_id,
                                   session_id="native-session-1", environment_id=environment)
    with closing(db.connect()) as conn:
        assert registry.authorize_scope(conn, principal, child) == child
        with pytest.raises(PmtError, match="outside"):
            registry.authorize_scope(conn, principal, other)
        assert device["credential"] not in "\n".join(conn.iterdump())
    with pytest.raises(PmtError, match="permission"):
        principal.require("review")
    other_device = registry.issue_device("worker", [other], ["read"])
    with pytest.raises(PmtError, match="another device"):
        registry.register_session(other_device["credential"], other_device["device_id"], registry.namespace_id,
                                  "different-session", environment)
    with pytest.raises(PmtError, match="registered"):
        registry.principal(other_device["credential"], other_device["device_id"], registry.namespace_id,
                           session_id="native-session-1", environment_id=environment)


def test_rotation_revocation_and_reduced_grants_take_effect_on_fresh_checks(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    registry = AuthRegistry(db)
    project = scope(db, "allowed")
    device = registry.issue_device("main", [project], ["read", "write"])
    rotated = registry.rotate_device(device["device_id"], 1)
    with pytest.raises(PmtError, match="authentication failed"):
        registry.principal(device["credential"], device["device_id"], registry.namespace_id)
    registry.update_grants(device["device_id"], 2, [project], ["read"])
    principal = registry.principal(rotated["credential"], device["device_id"], registry.namespace_id)
    assert principal.revision == 3
    with pytest.raises(PmtError, match="permission"):
        principal.require("write")
    with pytest.raises(PmtError, match="changed"):
        registry.revoke_device(device["device_id"], 2)
    assert registry.revoke_device(device["device_id"], 3)["state"] == "revoked"
    with pytest.raises(PmtError, match="authentication failed"):
        registry.principal(rotated["credential"], device["device_id"], registry.namespace_id)
    assert AuthRegistry(db).namespace_id == registry.namespace_id


def test_replay_rechecks_device_on_the_same_transaction_before_cache_access(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    registry = AuthRegistry(db)
    project = scope(db, "allowed")
    device = registry.issue_device("main", [project], ["read", "write"])
    value = request("record_event", scope_id=project)
    checks = []
    calls = []
    def authorize(conn, req):
        principal = registry.authenticate(conn, device["credential"], device["device_id"], registry.namespace_id)
        principal.require("write")
        registry.authorize_scope(conn, principal, req["scope_id"])
        checks.append(principal.revision)
    def handler(conn, req):
        calls.append(req["request_id"])
        return {"recorded": True}
    first = db.run_request(value, handler, authorize=authorize)
    assert first[1] == 0 and db.run_request(value, handler, authorize=authorize) == first
    assert checks == [1, 1] and calls == [value["request_id"]]
    registry.revoke_device(device["device_id"], 1)
    denied, code = db.run_request(value, handler, authorize=authorize)
    assert code == 3 and denied["error"]["code"] == "unauthenticated"
    assert calls == [value["request_id"]]
