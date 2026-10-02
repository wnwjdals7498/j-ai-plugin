"""Host resource storage tests at the local SQL/filesystem fixture tier."""
import hashlib
import json
import uuid
from contextlib import closing

import pytest

pytest_plugins = ["test_phase3_context"]

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.application import HostApplication
from pmt.host.resources import HostResourceStore
from pmt.util import new_id, utc_now


def _req(operation, payload=None, **kw):
    return {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": operation,
            "actor": "main", "session_id": kw.pop("session_id", "resource-session"),
            "payload": payload or {}, **kw}


def _headers(app, session, scopes=("*",), permissions=("read", "write", "runtime", "review", "admin"), actor="main"):
    issued = app.auth.issue_device(actor, list(scopes), list(permissions))
    headers = {"authorization": "Bearer " + issued["credential"], "x-pmt-device": issued["device_id"],
        "x-pmt-environment": str(uuid.uuid4()), "x-pmt-namespace": app.auth.namespace_id,
        "x-pmt-session": session}
    app.register_session(headers, {"session_id": session, "environment_id": headers["x-pmt-environment"]})
    return headers


def _fixture(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    app = HostApplication(db, {"fixture": b"H" * 32}, "fixture")
    headers = _headers(app, "resource-session")
    scope_req = _req("create_scope", {"kind": "project", "slug": "resource-fixture"})
    scope_reply, code = app.execute(scope_req, headers)
    assert code == 0, scope_reply
    scope_id = scope_reply["result"]["id"]
    # Replace the wildcard grant with an explicit project grant for boundary tests.
    app.auth.update_grants(headers["x-pmt-device"], 1, [scope_id], ["read", "write", "runtime", "review"])
    store = HostResourceStore(db, app.auth)
    return db, app, store, headers, scope_id


def test_host_resource_publish_read_replay_and_hash_verification(tmp_path):
    db, app, store, headers, scope_id = _fixture(tmp_path)
    raw = b"fixture result\nline two\n"
    req = {"request_id": str(uuid.uuid4()), "scope_id": scope_id, "purpose": "result", "content": raw,
           "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    published = store.publish(req, headers)
    assert published["artifact_ref"]["size"] == len(raw)
    assert published["artifact_ref"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.publish(req, headers) == published
    assert store.read({"resource_id": published["artifact_ref"]["id"]}, headers)["content"] == raw
    duplicate = store.publish({**req, "request_id": str(uuid.uuid4())}, headers)
    assert duplicate["artifact_ref"]["id"] == published["artifact_ref"]["id"]
    assert len(list((db.root / "resources" / "objects").iterdir())) == 1
    changed = dict(req, content=b"other")
    with pytest.raises(PmtError, match="does not match bytes"):
        store.publish(changed, headers)
    changed = dict(req, purpose="evidence")
    with pytest.raises(PmtError, match="another resource body"):
        store.publish(changed, headers)
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT relative_path FROM artifacts WHERE id=?", (published["artifact_ref"]["id"],)).fetchone()
    (db.root / row[0]).write_bytes(b"tampered")
    with pytest.raises(PmtError, match="integrity verification"):
        store.read({"resource_id": published["artifact_ref"]["id"]}, headers)
    with pytest.raises(PmtError, match="integrity verification"):
        store.publish(req, headers)


def test_host_resource_scope_revocation_and_forged_private_ref_are_denied(tmp_path):
    db, app, store, headers, scope_id = _fixture(tmp_path)
    admin = _headers(app, "admin-session")
    second_req = _req("create_scope", {"kind": "project", "slug": "other-resource-fixture"},
                      session_id="admin-session")
    second, code = app.execute(second_req, admin)
    assert code == 0, second
    other = _headers(app, "other-session", scopes=(second["result"]["id"],), permissions=("read", "write", "runtime"))
    raw = b"private-looking content"
    publish_request = {"request_id": str(uuid.uuid4()), "scope_id": scope_id,
        "purpose": "evidence", "content": raw}
    published = store.publish(publish_request, headers)
    ref = published["artifact_ref"]["id"]
    with pytest.raises(PmtError, match="outside"):
        store.read({"resource_id": ref, "run_id": str(uuid.uuid4())}, other)
    with pytest.raises(PmtError, match="Step directives use"):
        store.publish({"request_id": str(uuid.uuid4()), "scope_id": scope_id,
            "purpose": "step_directive", "content": raw}, headers)
    with pytest.raises(PmtError, match="outside"):
        store.read({"resource_id": ref}, other)
    app.auth.revoke_device(other["x-pmt-device"], 1)
    with pytest.raises(PmtError, match="authentication failed"):
        store.read({"resource_id": ref}, other)
    app.auth.revoke_device(headers["x-pmt-device"], 2)
    with pytest.raises(PmtError, match="authentication failed"):
        store.publish(publish_request, headers)


def test_host_resource_recovery_finishes_verified_staging_file(tmp_path):
    db, app, store, headers, scope_id = _fixture(tmp_path)
    raw = b"recoverable staged bytes"
    digest = hashlib.sha256(raw).hexdigest()
    request_id, artifact_id = str(uuid.uuid4()), new_id()
    stage_rel = f"resources/.staging/host-{request_id}.part"
    stage = db.root / stage_rel
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.write_bytes(raw)
    now = utc_now()
    with db.write() as conn:
        principal = store._principal(conn, headers)
        conn.execute("INSERT INTO host_resource_journal VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, principal.actor, principal.session_id, principal.device_id, scope_id, "result",
             None, None, None, digest, len(raw), stage_rel, artifact_id, "request-fingerprint",
             "staged", now, now))
    other = _headers(app, "other-resource-owner", scopes=(scope_id,), permissions=("read", "write"))
    with pytest.raises(PmtError, match="another current owner"):
        store.recover(request_id, other)
    response = store.recover(request_id, headers)
    assert response["artifact_ref"]["id"] == artifact_id
    assert store.read({"resource_id": artifact_id}, headers)["content"] == raw
    assert store.recover(request_id, headers)["receipt_ref"]["state"] == "published"


def test_host_resource_recovery_after_object_publish_before_db_commit(tmp_path, monkeypatch):
    db, app, store, headers, scope_id = _fixture(tmp_path)
    raw = b"published file; transaction interrupted"
    request = {"request_id": str(uuid.uuid4()), "scope_id": scope_id, "purpose": "result", "content": raw}
    original = store._principal
    calls = 0

    def fail_at_final_commit(conn, auth_headers):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PmtError("fixture_interruption", "injected after file publication")
        return original(conn, auth_headers)

    monkeypatch.setattr(store, "_principal", fail_at_final_commit)
    with pytest.raises(PmtError, match="injected"):
        store.publish(request, headers)
    monkeypatch.setattr(store, "_principal", original)
    recovered = store.recover(request["request_id"], headers)
    assert recovered["artifact_ref"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.read({"resource_id": recovered["artifact_ref"]["id"]}, headers)["content"] == raw


def test_host_resource_bounded_retry_for_sharing_violation_only(tmp_path, monkeypatch):
    import pmt.host.resources as host_resource_module
    db, app, store, headers, scope_id = _fixture(tmp_path)
    raw = b"transient sharing violation fixture"
    original_link = host_resource_module.os.link
    attempts = 0

    def fail_twice_then_link(source, target):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            error = PermissionError(32, "fixture sharing violation")
            error.winerror = 32
            raise error
        return original_link(source, target)

    monkeypatch.setattr(host_resource_module.os, "link", fail_twice_then_link)
    request = {"request_id": str(uuid.uuid4()), "scope_id": scope_id, "purpose": "result", "content": raw}
    result = store.publish(request, headers)
    assert attempts == 3
    assert store.read({"resource_id": result["artifact_ref"]["id"]}, headers)["content"] == raw


def test_host_resource_persistent_sharing_lock_preserves_journal_and_recovers_same_request(tmp_path, monkeypatch):
    import pmt.host.resources as host_resource_module
    db, app, store, headers, scope_id = _fixture(tmp_path)
    raw = b"persistent sharing violation fixture"
    request = {"request_id": str(uuid.uuid4()), "scope_id": scope_id, "purpose": "result", "content": raw}
    original_link = host_resource_module.os.link
    attempts = 0

    def always_block(source, target):
        nonlocal attempts
        attempts += 1
        error = PermissionError(32, "fixture persistent sharing violation")
        error.winerror = 32
        raise error

    monkeypatch.setattr(host_resource_module.os, "link", always_block)
    with pytest.raises(PmtError, match="sharing lock") as caught:
        store.publish(request, headers)
    assert caught.value.code == "resource_io_unknown" and caught.value.retryable is True
    assert attempts == 4
    with closing(db.connect()) as conn:
        journal = conn.execute("SELECT state,sha256,size_bytes FROM host_resource_journal WHERE request_id=?",
                               (request["request_id"],)).fetchone()
    assert journal["state"] == "staged"
    assert (journal["sha256"], journal["size_bytes"]) == (hashlib.sha256(raw).hexdigest(), len(raw))
    monkeypatch.setattr(host_resource_module.os, "link", original_link)
    recovered = store.recover(request["request_id"], headers)
    assert recovered["artifact_ref"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.read({"resource_id": recovered["artifact_ref"]["id"]}, headers)["content"] == raw


def test_host_resource_does_not_retry_non_sharing_file_errors(tmp_path, monkeypatch):
    import pmt.host.resources as host_resource_module
    _, _, store, headers, scope_id = _fixture(tmp_path)
    raw = b"non-sharing failure fixture"
    original_link = host_resource_module.os.link
    attempts = 0

    def denied(source, target):
        nonlocal attempts
        attempts += 1
        error = PermissionError(5, "fixture access denied")
        error.winerror = 5
        raise error

    monkeypatch.setattr(host_resource_module.os, "link", denied)
    request = {"request_id": str(uuid.uuid4()), "scope_id": scope_id, "purpose": "result", "content": raw}
    with pytest.raises(PmtError, match="publication failed") as caught:
        store.publish(request, headers)
    assert caught.value.code == "resource_io_error" and caught.value.retryable is True
    assert attempts == 1
    monkeypatch.setattr(host_resource_module.os, "link", original_link)
    assert store.recover(request["request_id"], headers)["artifact_ref"]["sha256"] == hashlib.sha256(raw).hexdigest()


def test_host_resource_size_and_hash_claim_are_strict(tmp_path):
    db, app, store, headers, scope_id = _fixture(tmp_path)
    raw = b"x" * (store.max_bytes + 1)
    with pytest.raises(PmtError, match="upload limit"):
        store.publish({"request_id": str(uuid.uuid4()), "scope_id": scope_id,
            "purpose": "evidence", "content": raw}, headers)
    with pytest.raises(PmtError, match="does not match bytes"):
        store.publish({"request_id": str(uuid.uuid4()), "scope_id": scope_id,
            "purpose": "evidence", "content": b"actual", "sha256": "0" * 64}, headers)
    with pytest.raises(PmtError, match="purpose is unsupported"):
        store.publish({"request_id": str(uuid.uuid4()), "scope_id": scope_id,
            "purpose": "context_projection", "content": b"private context"}, headers)


def test_host_resource_requires_registered_session_and_environment_for_all_calls(tmp_path):
    _, _, store, headers, scope_id = _fixture(tmp_path)
    body = b"resource"
    request = {"request_id": str(uuid.uuid4()), "scope_id": scope_id,
               "purpose": "evidence", "content": body}
    sessionless = dict(headers); sessionless.pop("x-pmt-session")
    environmentless = dict(headers); environmentless.pop("x-pmt-environment")
    for auth in (sessionless, environmentless):
        with pytest.raises(PmtError, match="session and environment"):
            store.publish(request, auth)
        with pytest.raises(PmtError, match="session and environment"):
            store.read({"resource_id": str(uuid.uuid4())}, auth)
        with pytest.raises(PmtError, match="session and environment"):
            store.recover(str(uuid.uuid4()), auth)


def test_private_step_directive_read_uses_current_p2_and_f5_authority(actual_context_env):
    env = actual_context_env
    db = env["db"]
    from pmt.host.resources import HostResourceStore
    from pmt.store import LocalStore
    import test_phase3_context as f5_fixture
    import test_phase3_local_integration as f10_fixture

    pin = f5_fixture._actual_f3_ready(env)
    built, code = f5_fixture._build_actual(env, pin)
    assert code == 0 and built["ok"]
    context_ref = built["result"]["context_ref"]
    f10_fixture.f10._prepare_item_for_reuse(env)
    reuse = f10_fixture.f10.run_f6_public(env, pin)
    f10_fixture.f10.run_f8_native_fixture(env, context_ref, reuse["decision_ref"])
    public_directive, code = LocalStore(db).execute(f5_fixture._actual_request(env,
        "read_step_directive", step_id=env["step_id"]))
    assert code == 0 and public_directive["ok"]

    app = HostApplication(db, {"fixture": b"R" * 32}, "fixture")
    headers = _headers(app, env["session"], scopes=(env["project_id"],), actor=env["actor"])
    store = HostResourceStore(db, app.auth)
    directive_raw = json.dumps(public_directive["result"]["directive"], ensure_ascii=False,
                               sort_keys=True, separators=(",", ":")).encode("utf-8")
    # The existing Core step directive artifact is private even if identical
    # bytes are offered under a public-purpose upload request.
    with pytest.raises(PmtError, match="private Step directive"):
        store.publish({"request_id": str(uuid.uuid4()), "scope_id": env["project_id"],
            "purpose": "evidence", "content": directive_raw}, headers)
    private = store.read({"resource_id": env["directive_id"], "run_id": env["run_id"]}, headers)
    assert private["purpose"] == "step_directive" and private["content"]
    assert json.loads(private["content"].decode("utf-8")) == public_directive["result"]["directive"]

    other = _headers(app, "second-session", scopes=(env["project_id"],),
                     permissions=("read", "runtime"), actor=env["actor"])
    with pytest.raises(PmtError):
        store.read({"resource_id": env["directive_id"], "run_id": env["run_id"]}, other)
    with db.write() as conn:
        conn.execute("UPDATE phase3_objects SET source_hash=? WHERE kind='task_context' AND id=?",
            ("f" * 64, context_ref["id"]))
    with pytest.raises(PmtError, match="source-bound F5"):
        store.read({"resource_id": env["directive_id"], "run_id": env["run_id"]}, headers)
    with db.write() as conn:
        conn.execute("UPDATE phase3_objects SET source_hash=? WHERE kind='task_context' AND id=?",
            (context_ref["source_hash"], context_ref["id"]))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (env["run_id"],))
    with pytest.raises(PmtError, match="scope"):
        store.read({"resource_id": env["directive_id"], "run_id": env["run_id"]}, headers)


def test_private_f9_child_directive_requires_explicit_active_batch_scope_binding(actual_context_env):
    env = actual_context_env
    import test_phase3_batch as f9_fixture
    from pmt.store import LocalStore

    batch_env = f9_fixture._queue_two_actual_runs(env)
    batch_env["product"] = "codex"
    request = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": str(uuid.uuid4()),
        "actor": batch_env["actor"], "session_id": batch_env["session"], "scope_id": batch_env["project_id"],
        "source": {"product": "codex"}, "payload": {"run_refs": batch_env["batch_runs"],
        "workspace": str(batch_env["workspace"]), "repository_id": batch_env["repo_id"],
        "relative_graph_path": batch_env["graph_path"], "expected_source": batch_env["batch_pin"],
        "context_budget": {"max_bytes": 20_000, "max_lines": 1_000, "unit": "utf8"},
        "event_id": str(uuid.uuid4())}}
    prepared, code = LocalStore(batch_env["db"]).execute(request)
    assert code == 0 and prepared["ok"], prepared.get("error")
    batch_ref = prepared["result"]["batch_ref"]["id"]
    child_run = batch_env["batch_child_run_id"]
    child_step = batch_env["batch_step_ids"][1]
    with closing(batch_env["db"].connect()) as conn:
        directive_id = conn.execute("SELECT directive_id FROM step_specs WHERE step_id=?", (child_step,)).fetchone()[0]
    app = HostApplication(batch_env["db"], {"fixture": b"F" * 32}, "fixture")
    headers = _headers(app, batch_env["session"], scopes=(batch_env["project_id"],), actor=batch_env["actor"])
    store = HostResourceStore(batch_env["db"], app.auth)
    read_back = store.read({"resource_id": directive_id, "run_id": child_run}, headers)
    assert read_back["purpose"] == "step_directive" and read_back["content"]
    with closing(batch_env["db"].connect()) as conn:
        row = conn.execute("SELECT body_json FROM phase3_objects WHERE kind='batch_binding' AND id=?",
                           (batch_ref,)).fetchone()
        assert row and json.loads(row[0])["status"] == "prepared"
    with batch_env["db"].write() as conn:
        conn.execute("DELETE FROM phase3_objects WHERE kind='batch_binding' AND id=?", (batch_ref,))
    with pytest.raises(PmtError, match="scope"):
        store.read({"resource_id": directive_id, "run_id": child_run}, headers)
