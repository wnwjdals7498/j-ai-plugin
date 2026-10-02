from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from pathlib import Path
import uuid

import pytest

from pmt.errors import PmtError
from pmt.pending import PendingOutbox, preflight_new_shared_write
from pmt.efficiency.source import SourcePin
from pmt.util import canonical_json, fingerprint, new_id
from pmt.workspace import canonical_workspace


def _identity():
    return {"namespace_id": new_id(), "actor": "fixture-actor", "device_id": new_id(),
            "environment_id": new_id(), "session_id": "fixture-session"}


def _request(identity, *, request_id=None, operation="submit_execution_result", result=None):
    return {"protocol_version": 1, "operation": operation, "request_id": request_id or new_id(),
        "actor": identity["actor"], "session_id": identity["session_id"], "scope_id": new_id(),
        "payload": {"run_id": new_id(), "expected_revision": 3,
            "result": result or {"directive_version": 1, "actual_route": {"model": "fixture"},
                "criteria_results": [{"criterion_id": "C1", "outcome": "not_run", "reason": "fixture only"}]}}}


def _pin(scope_id, revision=1):
    return SourcePin(str(uuid.uuid5(uuid.UUID(scope_id), "pending-test-repository")), scope_id, "main", "c" * 40, 1, revision,
        hashlib.sha256(f"graph-{revision}".encode()).hexdigest(), "clean").to_dict()


def _receipt(identity, request, *, revision=3, source_hash=None, stop_confirmed=True):
    pin = _pin(request["scope_id"])
    source_hash = source_hash or pin["source_hash"]
    result_bytes = canonical_json(request["payload"]["result"]).encode()
    actual_route = request["payload"]["result"]["actual_route"]
    raw_receipt = canonical_json({"run_id": request["payload"]["run_id"], "runner_kind": "fixture",
        "state": "completed", "exit_code": 0, "started_at": "2026-10-02T00:00:00Z",
        "completed_at": "2026-10-02T00:00:01Z", "error_code": None}).encode()
    receipt_sha = hashlib.sha256(raw_receipt).hexdigest()
    physical = {"run_id": request["payload"]["run_id"], "runner_kind": "fixture", "state": "completed",
        "exit_code": 0, "started_at": "2026-10-02T00:00:00Z", "completed_at": "2026-10-02T00:00:01Z",
        "error_code": None, "stop_confirmed": stop_confirmed}
    value = {"schema": "pmt-hosted-runner-receipt-v1", "run_id": request["payload"]["run_id"],
        "step_id": new_id(), "scope_id": request["scope_id"],
        "canonical_workspace": canonical_workspace(pin["repository_id"], pin["selected_ref"]),
        "attached_run_revision": revision,
        "owner": {"namespace_id": identity["namespace_id"], "actor": identity["actor"], "session_id": identity["session_id"],
            "device_id": identity["device_id"], "environment_id": identity["environment_id"]},
        "source_hash": source_hash, "context_ref": "f5-context:fixture", "runner_kind": "fixture",
        "prompt_sha256": "b" * 64, "route_sha256": fingerprint(actual_route), "receipt": physical,
        "receipt_sha256": receipt_sha, "output_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "output_sha256_basis": "exact local supervisor output.json bytes", "output_available": bool(result_bytes),
        "report_valid": False, "model_report_sha256": None, "provenance": "local_runner_supervisor", "fixture": True}
    manifest_raw = canonical_json(value).encode()
    parts = {"manifest_bytes": manifest_raw, "receipt_bytes": raw_receipt, "output_bytes": result_bytes}
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    ref = "local-runner-receipt:" + request["payload"]["run_id"] + ":" + manifest_sha
    return ref, manifest_sha, parts


def _outbox(tmp_path, identity):
    return PendingOutbox(tmp_path, **identity)


def _facts(identity, request, *, source_pin=None, revision=3, state="running"):
    source_pin = source_pin or _pin(request["scope_id"])
    source_ref = {"schema": "pmt-source-pin-ref-v1", "ref": "source-pin:fixture", "pin": source_pin}
    auth = {"schema": "pmt-host-auth-facts-v1", "ref": "host-auth:fixture", **identity,
        "scope_id": request["scope_id"], "scopes": [request["scope_id"]], "permissions": ["read", "write", "runtime"]}
    auth["sha256"] = fingerprint(auth)
    run_id = request.get("payload", {}).get("run_id")
    owner = {"actor": identity["actor"], "device_id": identity["device_id"],
        "session_id": identity["session_id"]} if run_id else None
    run_ref = None
    if run_id:
        run_ref = {"schema": "pmt-host-run-read-v1", "ref": "run-read:" + run_id,
            "run_id": run_id, "scope_id": request["scope_id"], "revision": revision, "state": state, "owner": owner}
        run_ref["actual_route"] = request["payload"]["result"]["actual_route"]
        pin = _pin(request["scope_id"])
        run_ref["workspace"] = canonical_workspace(pin["repository_id"], pin["selected_ref"])
        run_ref["sha256"] = fingerprint(run_ref)
    return {"schema": "pmt-pending-current-facts-v1", **identity, "scope_id": request["scope_id"],
        "source_fingerprint": source_pin["source_hash"], "source_ref": source_ref,
        "authorization_ref": auth, "run_ref": run_ref, "run_id": run_id,
        "run_revision": revision if run_id else 0, "run_state": state if run_id else None, "run_owner": owner}


class _ReplyLostStore:
    def __init__(self):
        self.saved = None
        self.executed = []

    def get_request_result(self, request_id, actor, session_id, *, expected_request=None):
        if self.saved is None:
            return None
        assert expected_request["request_id"] == request_id
        return self.saved

    def execute(self, request):
        self.executed.append(json.loads(canonical_json(request)))
        self.saved = ({"protocol_version": 1, "request_id": request["request_id"], "ok": True,
            "result": {"run_id": request["payload"]["run_id"], "state": "review_pending", "revision": 4},
            "error": None, "warnings": []}, 0)
        raise PmtError("remote_unavailable", "fixture lost response", 3, True, {"effect": "unknown"})


def _enqueue(box, identity, request, *, source_hash=None, revision=3, stop_confirmed=True):
    source_hash = source_hash or _pin(request["scope_id"])["source_hash"]
    ref, digest, raw = _receipt(identity, request, revision=revision, source_hash=source_hash,
                                stop_confirmed=stop_confirmed)
    return box.enqueue_result(request, base_run_revision=revision, source_fingerprint=source_hash,
        runtime_receipt_ref=ref, runtime_receipt_sha256=digest,
        receipt_reader=lambda actual_ref: raw if actual_ref == ref else None)


def _parallel_pending_resource_publish(data_root, identity, upload_request_id, facts, barrier, results, calls_path):
    class Store:
        def publish_resource(self, metadata, content, *, session_id):
            with sqlite3.connect(calls_path, timeout=10) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY)")
                conn.execute("INSERT INTO calls DEFAULT VALUES")
            return {"artifact_ref": {"id": "fixture-artifact", "scope_id": metadata["scope_id"],
                    "purpose": metadata["purpose"], "sha256": metadata["sha256"], "size": len(content)},
                "receipt_ref": {"request_id": metadata["request_id"]}}

    try:
        box = PendingOutbox(data_root, **identity)
        result = box.publish_staged_resource(upload_request_id, Store(),
            lambda _immutable: (barrier.wait(timeout=10), facts)[1])
        results.put(result["state"])
    except PmtError as error:
        results.put(error.code)


def test_only_eligible_result_request_is_durable_and_same_id_body_is_immutable(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity)
    ref, digest, raw = _receipt(identity, request)
    args = {"base_run_revision": 3, "source_fingerprint": _pin(request["scope_id"])["source_hash"],
        "runtime_receipt_ref": ref, "runtime_receipt_sha256": digest,
        "receipt_reader": lambda actual_ref: raw if actual_ref == ref else None}
    pending = box.enqueue_result(request, **args)
    assert pending.state == "pending"
    assert box.list_pending() == (pending,)
    assert box.enqueue_result(request, **args).body_sha256 == pending.body_sha256

    changed = _request(identity, request_id=request["request_id"], result={"different": True, "actual_route": {"model": "fixture"}})
    ref, digest, raw = _receipt(identity, changed)
    with pytest.raises(PmtError) as caught:
        box.enqueue_result(changed, base_run_revision=3, source_fingerprint=_pin(changed["scope_id"])["source_hash"],
            runtime_receipt_ref=ref, runtime_receipt_sha256=digest, receipt_reader=lambda _ref: raw)
    assert caught.value.code == "pending_request_conflict"
    assert caught.value.exit_code == 3


def test_pending_missing_resource_uses_cli_exit_code_and_resource_listing_is_metadata_only(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    with pytest.raises(PmtError) as caught:
        box.publish_staged_resource(new_id(), object(), lambda _item: {})
    assert caught.value.code == "pending_resource_not_found"
    assert caught.value.exit_code == 2
    payload = b"finished fixture evidence"
    staged = box.stage_resource(upload_request_id=new_id(), scope_id=new_id(), purpose="evidence",
        source_fingerprint="a" * 64, output_ref="local-fixture-output", output_reader=lambda _ref: payload)
    listed = box.list_resources()
    assert listed == (staged,)
    assert listed[0].sha256 == hashlib.sha256(payload).hexdigest()
    assert listed[0].size == len(payload) and listed[0].state == "staged"
    assert not hasattr(listed[0], "bytes") and not hasattr(listed[0], "template")


@pytest.mark.parametrize("operation", ["create_scope", "claim_task", "release_claim", "finish_task",
    "review_execution", "request_execution_cancel", "save_change"])
def test_shared_writes_claims_reviews_and_cancellation_cannot_be_queued(tmp_path, operation):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity, operation=operation)
    with pytest.raises(PmtError) as caught:
        _enqueue(box, identity, request)
    assert caught.value.code == "pending_operation_forbidden"


def test_new_shared_write_requires_current_compatible_host_and_never_queues(tmp_path):
    identity = _identity()
    class Store:
        namespace_id = identity["namespace_id"]
        device_id = identity["device_id"]
        def check_compatibility(self):
            return {"compatible": True, "namespace_id": self.namespace_id, "device_id": self.device_id,
                "scopes": [identity["namespace_id"]], "permissions": ["write", "runtime"],
                "core_version": "0.3.0", "db_schema": 4, "graph_schema": 1}
    assert preflight_new_shared_write(Store(), "claim_task", scope_id=identity["namespace_id"])["state"] == "connected"

    class OfflineStore:
        def check_compatibility(self):
            raise PmtError("remote_unavailable", "fixture disconnected", 3, True)
    with pytest.raises(PmtError) as caught:
        preflight_new_shared_write(OfflineStore(), "save_change", scope_id=identity["namespace_id"])
    assert caught.value.code == "hosted_write_blocked"
    with pytest.raises(PmtError) as caught:
        preflight_new_shared_write(Store(), "record_verification", scope_id=identity["namespace_id"])
    assert caught.value.code == "pending_result_hook_required"


def test_receipt_must_be_read_as_real_hashed_terminal_receipt(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity)
    ref, digest, raw = _receipt(identity, request, stop_confirmed=False)
    incomplete = box.enqueue_result(request, base_run_revision=3, source_fingerprint=_pin(request["scope_id"])["source_hash"],
        runtime_receipt_ref=ref, runtime_receipt_sha256=digest, receipt_reader=lambda _ref: raw)
    assert incomplete.state == "pending_incomplete"
    assert box.list_pending()[0].state == "pending_incomplete"

    recoverable = _request(identity)
    ref, digest, parts = _receipt(identity, recoverable)
    first = box.enqueue_result(recoverable, base_run_revision=3,
        source_fingerprint=_pin(recoverable["scope_id"])["source_hash"], runtime_receipt_ref=ref,
        runtime_receipt_sha256=digest, receipt_reader=lambda _ref: (_ for _ in ()).throw(OSError("offline reader")))
    assert first.state == "pending_incomplete"
    recovered = box.enqueue_result(recoverable, base_run_revision=3,
        source_fingerprint=_pin(recoverable["scope_id"])["source_hash"], runtime_receipt_ref=ref,
        runtime_receipt_sha256=digest, receipt_reader=lambda _ref: parts)
    assert recovered.state == "pending"
    assert next(item for item in box.list_pending() if item.request_id == recoverable["request_id"]).state == "pending"

    request = _request(identity)
    ref, digest, raw = _receipt(identity, request)
    incomplete = box.enqueue_result(request, base_run_revision=3, source_fingerprint=_pin(request["scope_id"])["source_hash"],
        runtime_receipt_ref=ref, runtime_receipt_sha256="c" * 64,
        receipt_reader=lambda _ref: raw)
    assert incomplete.state == "pending_incomplete"


def test_sensitive_route_is_not_saved_and_route_change_blocks_replay(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity, result={"directive_version": 1, "actual_route": {"model": "fixture",
        "env": {"API_TOKEN": "do-not-persist"}}, "criteria_results": []})
    pending = _enqueue(box, identity, request)
    assert pending.state == "pending_incomplete"
    with sqlite3.connect(box.path) as conn:
        row = conn.execute("SELECT immutable_json FROM pending_results WHERE request_id=?", (pending.request_id,)).fetchone()
    assert "do-not-persist" not in row[0] and "API_TOKEN" not in row[0]

    safe = _request(identity)
    pending = _enqueue(box, identity, safe)
    store = _ReplyLostStore()
    facts = _facts(identity, safe)
    facts["run_ref"]["actual_route"] = {"model": "changed-route"}
    facts["run_ref"].pop("sha256")
    facts["run_ref"]["sha256"] = fingerprint(facts["run_ref"])
    result = box.reconcile(pending.request_id, store, lambda _item: facts)
    assert result.state == "conflict"
    assert result.reason_code == "pending_request_reconstruction_conflict"
    assert not store.executed


def test_reconcile_queries_then_exactly_reuses_original_request_after_lost_reply(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity)
    pending = _enqueue(box, identity, request)
    store = _ReplyLostStore()
    facts = _facts(identity, request)
    first = box.reconcile(pending.request_id, store, lambda _item: facts)
    assert first.state == "unknown"
    assert len(store.executed) == 1
    expected = json.loads(canonical_json(request))
    expected["expected_revision"] = 3
    expected["payload"].pop("expected_revision")
    expected["context_refs"] = []
    expected["source"] = {}
    assert store.executed[0] == expected

    applied_facts = _facts(identity, request, revision=4, state="review_pending")
    second = box.reconcile(pending.request_id, store, lambda _item: applied_facts)
    assert second.state == "already_applied"
    assert len(store.executed) == 1
    assert box.list_pending()[0].state == "applied"


def test_found_same_body_receipt_is_already_applied_after_later_source_change(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity)
    pending = _enqueue(box, identity, request)
    store = _ReplyLostStore()
    assert box.reconcile(pending.request_id, store, lambda _item: _facts(identity, request)).state == "unknown"
    changed_pin = _pin(request["scope_id"], revision=2)
    changed = box.reconcile(pending.request_id, store,
        lambda _item: _facts(identity, request, source_pin=changed_pin, revision=4, state="review_pending"))
    assert changed.state == "already_applied"
    assert "source_fingerprint" in changed.changed_dimensions
    assert changed.reason_code == "applied_current_conditions_changed"
    assert len(store.executed) == 1


def test_current_source_revision_or_owner_change_preserves_pending_without_replay(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity)
    pending = _enqueue(box, identity, request)
    store = _ReplyLostStore()
    changed_facts = _facts(identity, request, source_pin=_pin(request["scope_id"], revision=2), revision=4, state="review_pending")
    result = box.reconcile(pending.request_id, store, lambda _item: changed_facts)
    assert result.state == "stale"
    assert "source_fingerprint" in result.changed_dimensions
    assert not store.executed
    assert box.list_pending()[0].state == "stale"


def test_caller_boolean_cannot_substitute_for_current_host_source_auth_and_run_reads(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity)
    pending = _enqueue(box, identity, request)
    store = _ReplyLostStore()
    result = box.reconcile(pending.request_id, store, lambda _item: {"authorized": True, "source_current": True})
    assert result.state == "unknown"
    assert result.reason_code == "pending_current_facts_invalid"
    assert not store.executed


def test_pending_checksum_detects_tamper_and_new_environment_cannot_read(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    request = _request(identity)
    pending = _enqueue(box, identity, request)
    other_identity = dict(identity) | {"device_id": new_id(), "environment_id": new_id(),
        "session_id": "different-environment-session"}
    other = _outbox(tmp_path, other_identity)
    with pytest.raises(PmtError) as caught:
        other.reconcile(pending.request_id, _ReplyLostStore(), lambda _item: {})
    assert caught.value.code == "pending_owner_mismatch"

    with sqlite3.connect(box.path) as conn:
        conn.execute("UPDATE pending_results SET immutable_json=replace(immutable_json,'fixture-actor','tampered') WHERE request_id=?",
                     (pending.request_id,))
    with pytest.raises(PmtError) as caught:
        box.list_pending()
    assert caught.value.code == "pending_corrupt"


def test_same_namespace_different_device_and_environment_cannot_list_or_publish_pending(tmp_path):
    owner = _identity()
    box_a = _outbox(tmp_path, owner)
    scope_id = new_id()
    upload_id = new_id()
    source_hash = _pin(scope_id)["source_hash"]
    box_a.stage_resource(upload_request_id=upload_id, scope_id=scope_id, purpose="result",
        source_fingerprint=source_hash, output_ref="owned-output", output_reader=lambda _ref: b"private bytes")
    other = dict(owner) | {"device_id": new_id(), "environment_id": new_id(), "session_id": "other-session"}
    box_b = _outbox(tmp_path, other)
    assert box_b.list_pending() == ()
    with pytest.raises(PmtError) as caught:
        box_b.publish_staged_resource(upload_id, object(), lambda _item: {})
    assert caught.value.code == "pending_owner_mismatch"


def test_staged_result_resource_uses_one_upload_id_and_hash_across_unknown_response(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    scope_id, upload_request_id = new_id(), new_id()
    source_hash = _pin(scope_id)["source_hash"]
    content = b"already-created sanitized result resource"
    staged = box.stage_resource(upload_request_id=upload_request_id, scope_id=scope_id, purpose="result",
        source_fingerprint=source_hash, output_ref="runtime-output-ref", output_reader=lambda _ref: content)
    assert staged.sha256 == hashlib.sha256(content).hexdigest()
    facts = _facts(identity, {"scope_id": scope_id, "payload": {}}, source_pin=_pin(scope_id), revision=0, state=None)

    class Store:
        def __init__(self):
            self.calls = []
            self.result = None

        def publish_resource(self, metadata, raw, *, session_id):
            self.calls.append((dict(metadata), raw, session_id))
            if self.result is None:
                self.result = {"artifact_ref": {"id": new_id(), "scope_id": scope_id, "purpose": "result",
                    "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)},
                    "receipt_ref": {"request_id": metadata["request_id"]}}
                raise PmtError("remote_unavailable", "fixture upload response lost", 3, True)
            return self.result

    store = Store()
    first = box.publish_staged_resource(upload_request_id, store, lambda _item: facts)
    assert first.state == "unknown"
    second = box.publish_staged_resource(upload_request_id, store, lambda _item: facts)
    assert second["state"] == "published"
    assert len(store.calls) == 2
    assert store.calls[0] == store.calls[1]
    assert store.calls[0][0]["request_id"] == upload_request_id
    assert store.calls[0][0]["sha256"] == staged.sha256


def test_two_processes_cas_one_physical_pending_resource_upload(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    scope_id, upload_request_id = new_id(), new_id()
    source_pin = _pin(scope_id)
    facts = _facts(identity, {"scope_id": scope_id, "payload": {}}, source_pin=source_pin, revision=0, state=None)
    content = b"one already-created upload"
    staged = box.stage_resource(upload_request_id=upload_request_id, scope_id=scope_id, purpose="result",
        source_fingerprint=facts["source_fingerprint"], output_ref="existing-result", output_reader=lambda _ref: content)
    calls_path = str(tmp_path / "upload-calls.sqlite3")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [context.Process(target=_parallel_pending_resource_publish,
        args=(str(tmp_path), identity, upload_request_id, facts, barrier, results, calls_path)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
    assert all(worker.exitcode == 0 for worker in workers)
    outcomes = sorted(results.get(timeout=2) for _ in workers)
    assert outcomes == ["pending_state_conflict", "published"]
    with sqlite3.connect(calls_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1
    assert box._resource_receipt_by_id(upload_request_id)["state"] == "published"


def test_missing_local_bytes_are_pending_incomplete_not_fabricated(tmp_path):
    identity = _identity()
    box = _outbox(tmp_path, identity)
    ref = box.stage_resource(upload_request_id=new_id(), scope_id=new_id(), purpose="evidence",
        source_fingerprint=_pin(new_id())["source_hash"], output_ref="missing-ref", output_reader=lambda _ref: None)
    assert ref.state == "pending_incomplete"
    assert ref.sha256 == ""
