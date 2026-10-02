from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from pmt.errors import PmtError
from pmt.phase2_common import persist_json_resource
from pmt.resources import _publish_content_addressed_bytes
import pmt.resources as resources
from test_phase2_steps import req, setup


def _sharing_violation():
    error = PermissionError(13, "fixture file sharing lock")
    error.winerror = 32
    return error


def test_immutable_file_publish_creates_verifies_and_converges_on_same_bytes(tmp_path):
    content = b"immutable-content"
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "objects" / "content.json"
    target.parent.mkdir()
    first_stage = target.with_name(".request-1.stage")
    first = _publish_content_addressed_bytes(target, first_stage, content, digest)
    assert first["created"] is True and first["cleanup_pending"] is False
    assert target.read_bytes() == content and not first_stage.exists()

    second_stage = target.with_name(".request-2.stage")
    second = _publish_content_addressed_bytes(target, second_stage, content, digest)
    assert second["created"] is False and second["sha256"] == digest
    assert not second_stage.exists() and target.read_bytes() == content


def test_immutable_file_publish_rejects_different_existing_bytes_without_replacing(tmp_path):
    content = b"expected"
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "objects" / "content.json"
    target.parent.mkdir()
    original = b"user-or-corrupt-content"
    target.write_bytes(original)
    stage = target.with_name(".request.stage")
    with pytest.raises(PmtError) as caught:
        _publish_content_addressed_bytes(target, stage, content, digest)
    assert caught.value.code == "resource_publish_conflict"
    assert target.read_bytes() == original


def test_immutable_file_publish_retries_only_winerror_32_and_preserves_final_hash(tmp_path, monkeypatch):
    content = b"retry-after-sharing-lock"
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "objects" / "content.json"
    target.parent.mkdir()
    stage = target.with_name(".request.stage")
    original_link, original_sleep = os.link, resources.time.sleep
    attempts, sleeps = {"count": 0}, []

    def fail_twice(source, destination, *args, **kwargs):
        if Path(destination) == target and attempts["count"] < 2:
            attempts["count"] += 1
            raise _sharing_violation()
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(resources.os, "link", fail_twice)
    monkeypatch.setattr(resources.time, "sleep", lambda duration: sleeps.append(duration))
    result = _publish_content_addressed_bytes(target, stage, content, digest)
    monkeypatch.setattr(resources.time, "sleep", original_sleep)
    assert result["created"] is True and attempts["count"] == 2 and len(sleeps) == 2
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest


def test_same_content_concurrent_publishers_converge_on_one_immutable_object(tmp_path, monkeypatch):
    db, scope, _, _ = setup(tmp_path)
    value = {"evidence": "same immutable body"}
    # Use the production canonical JSON digest to identify the common target.
    from pmt.util import canonical_json
    wire = canonical_json(value).encode()
    digest = hashlib.sha256(wire).hexdigest()
    target = db.root / "resources" / "objects" / digest[:2] / (digest + ".json")
    original_link = os.link
    barrier = threading.Barrier(2)

    def race_link(source, destination, *args, **kwargs):
        if Path(destination) == target:
            barrier.wait(timeout=5)
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(resources.os, "link", race_link)
    requests = [req("publish_fixture", {"n": index}) for index in (1, 2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda request: persist_json_resource(db, request, value, scope, "fixture"), requests))
    assert results[0]["artifact_id"] == results[1]["artifact_id"]
    assert target.read_bytes() == wire
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM artifacts WHERE sha256=?", (digest,)).fetchone()[0] == 1
        journals = conn.execute("SELECT state,body_json FROM operation_journal WHERE kind='resource_publish'").fetchall()
        assert len(journals) == 2 and all(row["state"] == "completed" for row in journals)
        assert all(json.loads(row["body_json"]).get("stage_relative_path") for row in journals)


def test_exhausted_winerror_32_keeps_original_error_stage_and_operation_journal(tmp_path, monkeypatch):
    db, scope, _, _ = setup(tmp_path)
    request = req("publish_fixture", {"body": "retryable"})
    value = {"fixture": "preserve unknown publication"}
    wire = __import__("pmt.util", fromlist=["canonical_json"]).canonical_json(value).encode()
    digest = hashlib.sha256(wire).hexdigest()
    target = db.root / "resources" / "objects" / digest[:2] / (digest + ".json")
    original_link = os.link
    unlink_calls = []

    def locked_link(source, destination, *args, **kwargs):
        if Path(destination) == target:
            raise _sharing_violation()
        return original_link(source, destination, *args, **kwargs)

    original_unlink = Path.unlink
    def forbidden_cleanup(path, *args, **kwargs):
        if path.name.endswith(".stage"):
            unlink_calls.append(str(path))
            raise _sharing_violation()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(resources.os, "link", locked_link)
    monkeypatch.setattr(resources.time, "sleep", lambda _duration: None)
    monkeypatch.setattr(Path, "unlink", forbidden_cleanup)
    with pytest.raises(PmtError) as caught:
        persist_json_resource(db, request, value, scope, "fixture")
    assert caught.value.code == "resource_io_unknown" and caught.value.retryable is True
    assert not unlink_calls, "failed publication must preserve its stage without masking the original error"
    with db.connect() as conn:
        row = conn.execute("SELECT state,body_json FROM operation_journal WHERE kind='resource_publish'").fetchone()
        assert row["state"] == "staging"
        journal = json.loads(row["body_json"])
    stage = db.root / journal["stage_relative_path"]
    assert stage.is_file() and stage.read_bytes() == wire
    assert not target.exists()
    monkeypatch.undo()
    recovered = persist_json_resource(db, request, value, scope, "fixture")
    assert recovered["sha256"] == digest and target.read_bytes() == wire
    assert not stage.exists()
    with db.connect() as conn:
        row = conn.execute("SELECT state,body_json FROM operation_journal WHERE kind='resource_publish'").fetchone()
        assert row["state"] == "completed"
        assert json.loads(row["body_json"])["publication"]["sha256"] == digest
