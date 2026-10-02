"""Public measurement operations preserve the existing protocol and ownership."""
import copy
import uuid

from pmt.db import Database
from pmt.service import execute


def request(op, payload=None, **kw):
    return {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": op,
            "actor": "main", "session_id": "main", "payload": payload or {}, **kw}


def sample():
    case = {"id": "fixture", "purpose": "measure the same acceptance definition",
            "input": "semantic input", "output": "semantic result",
            "observations": [{"input_bytes": 100, "output_bytes": 50, "calls": 1,
                              "quality": {"status": "pass", "criteria": ["fixture-only"]}}]}
    condition = {k: k + "-v1" for k in
                 ("goal", "acceptance", "source", "environment", "model_role", "policy", "definition")}
    return case, condition


def test_measurement_protocol_replay_and_owner(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    scope, code = execute(db, request("create_scope", {"kind": "project", "slug": "p3"}))
    assert code == 0
    scope_id = scope["result"]["scope_id"]
    case, condition = sample()
    req = request("capture_measurement", {"case": case, "condition": condition}, scope_id=scope_id)
    result, code = execute(db, req)
    assert code == 0 and result["ok"]
    assert execute(db, req) == (result, code)
    modified = copy.deepcopy(req)
    modified["payload"]["case"]["observations"][0]["input_bytes"] = 10
    conflict, code = execute(db, modified)
    assert code == 3 and conflict["error"]["code"] == "request_conflict"
    manifest = result["result"]["manifest"]
    measured = {"condition": condition, "case_fingerprint": manifest["case_fingerprint"],
                "observations": case["observations"]}
    p = {"baseline_id": result["result"]["measurement_id"], "measured": measured}
    comparison, code = execute(db, request("compare_measurements", p, scope_id=scope_id))
    assert code == 0 and comparison["result"]["manifest"]["token_usage"]["measured"]["status"] == "unknown"
    denied, code = execute(db, request("compare_measurements", p, scope_id=scope_id, session_id="other"))
    assert code == 3 and denied["error"]["code"] == "measurement_not_found"


def test_measurement_actual_cli_is_one_envelope(cli, create_project, request_factory, parse_cli_response):
    scope_id = create_project()
    case, condition = sample()
    run = cli.call(request_factory("capture_measurement", {"case": case, "condition": condition}, scope_id=scope_id))
    assert run.returncode == 0, run.stderr
    result = parse_cli_response(run.stdout)
    assert result["ok"] and result["result"]["manifest"]["kind"] == "baseline"
