"""A trusted adapter can cache claim locators without retaining bearer tokens."""
import copy
import hashlib
import hmac
import uuid
from contextlib import closing

from pmt.db import Database
from pmt.lifecycle import claim_task, release_claim
from pmt.service import execute


def request(operation, payload=None, **kw):
    return {"protocol_version": 1, "operation": operation, "request_id": str(uuid.uuid4()),
            "actor": "main", "session_id": "claim-adapter-fixture", "payload": payload or {}, **kw}


def item(db):
    reply, code = execute(db, request("create_scope", {"kind": "project", "slug": "claim-boundary"}))
    assert code == 0
    scope_id = reply["result"]["id"]
    reply, code = execute(db, request("save_change", {"kind": "item", "title": "fixture", "reason": "test"},
                                     scope_id=scope_id))
    assert code == 0
    return reply["result"]


def test_internal_factory_and_sanitized_cache_replay(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    record = item(db)
    req = request("claim_task", record_id=record["id"], expected_revision=1)
    key = b"isolated non-production test fixture key"
    def factory(row, value):
        message = ":".join((row["scope_id"], row["id"], value["request_id"], value["session_id"]))
        return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()
    raw_token = factory(record, req)
    calls = []
    def handle(conn, value):
        calls.append(value["request_id"])
        result = claim_task(db, conn, value, token_factory=factory)
        assert result.pop("claim_token") == raw_token
        return result | {"claim_ref": {"id": value["request_id"], "record_id": record["id"]}}
    reply, code = db.run_request(req, handle)
    assert code == 0 and db.run_request(req, handle) == (reply, code)
    assert calls == [req["request_id"]]
    with closing(db.connect()) as conn:
        cached = conn.execute("SELECT response_json FROM requests WHERE request_id=?", (req["request_id"],)).fetchone()[0]
        assert raw_token not in cached and '"claim_token"' not in cached
        assert conn.execute("SELECT token_hash FROM claims WHERE record_id=?", (record["id"],)).fetchone()[0] == hashlib.sha256(raw_token.encode()).hexdigest()
    release = request("release_claim", {"status": "Paused", "reason": "fixture", "resume": "continue"},
                      record_id=record["id"], expected_revision=reply["result"]["revision"])
    def release_handler(conn, value):
        internal = copy.deepcopy(value)
        internal["payload"]["claim_token"] = raw_token
        return release_claim(db, conn, internal)
    result, code = db.run_request(release, release_handler)
    assert code == 0 and result["result"]["state"] == "Paused"


def test_invalid_internal_factory_rolls_back_and_wire_cannot_select_it(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    record = item(db)
    req = request("claim_task", record_id=record["id"], expected_revision=1)
    reply, code = db.run_request(req, lambda conn, value: claim_task(db, conn, value, token_factory=lambda *_: None))
    assert code == 5 and reply["error"]["code"] == "claim_factory_invalid"
    with closing(db.connect()) as conn:
        assert conn.execute("SELECT state,revision FROM records WHERE id=?", (record["id"],)).fetchone()[0] == "Planned"
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
    req = request("claim_task", {"token_factory": "caller-chosen"}, record_id=record["id"], expected_revision=1)
    reply, code = execute(db, req)
    assert code == 0 and reply["result"]["claim_token"] != "caller-chosen"
