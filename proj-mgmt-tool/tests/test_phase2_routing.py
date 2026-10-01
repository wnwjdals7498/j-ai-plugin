import uuid

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.routing import FILE_OPERATIONS, READ_OPERATIONS, WRITE_OPERATIONS, handle, select_route


def _cap(mode, support="supported", *, agent="codex", model="m1", full=False):
    value = {"agent": agent, "provider": "provider-x", "model": model, "mode": mode,
             "support": support, "capabilities": ["code"], "capability_ref": f"cap:{mode}:{model}",
             "max_concurrency": 3, "auth_state": "authenticated"}
    if support != "unknown":
        value.update(evidence_ref="evidence:1", source_ref="authenticated-session-manifest",
                     observed_at="2026-10-01T00:00:00Z")
    if full:
        value["capacity_full"] = True
    return value


def _req(op, payload=None, expected=None, *, request_id=None):
    value = {"protocol_version": 1, "operation": op,
             "request_id": request_id or str(uuid.uuid4()), "actor": "tester", "session_id": "session",
             "payload": payload or {}}
    if expected is not None:
        value["expected_revision"] = expected
    return value


def _run(db, req):
    return db.run_request(req, lambda conn, request: handle(db, conn, request))


def _read(db, req):
    with db.connect() as conn:
        return handle(db, conn, req)


def test_sourced_economy_and_explicit_priority():
    cheap, expensive = _cap("native", model="z-cheap"), _cap("native", model="a-expensive")
    for cap, price in ((cheap, 1), (expensive, 10)):
        cap["price"] = {"input": price, "output": price, "currency": "USD", "unit": "per_million_tokens",
                        "source_kind": "official", "source_ref": "official-provider-pricing", "observed_at": "2026-10-01T00:00:00Z"}
    need = {"role": "lower", "active_agent": "codex", "needs": ["code"]}
    assert select_route({"economy": True}, [expensive, cheap], need)["model"] == "z-cheap"
    assert select_route({"economy": True, "role_preferences": {"lower": {"priority": ["a-expensive", "z-cheap"]}}},
                        [expensive, cheap], need)["model"] == "a-expensive"
    cheap["price"]["source_kind"] = "unverified"
    assert select_route({"economy": True}, [expensive, cheap], need)["model"] == "a-expensive"


def test_p2_route_01_default_auto_selects_only_verified_candidates():
    caps = [_cap("native", "unsupported"), _cap("cli", "unknown", agent="claude"), _cap("cli", agent="claude")]
    route = select_route({"api_allowed": False}, caps,
                         {"role": "worker", "needs": ["code"], "active_agent": "codex"})
    assert route["mode"] == "cli"
    assert route["actual_support"] == "verified_supported"
    assert route["price_status"] == "unknown"
    assert route["selection_reason"] != "cheapest_model"


def test_p2_route_02_explicit_model_or_path_wins_without_silent_substitution():
    caps = [_cap("native", agent="codex"), _cap("cli", agent="claude", model="other")]
    route = select_route({}, caps, {"role": "worker", "needs": ["code"], "active_agent": "codex",
                                    "requested_route": "cli", "requested_model": "missing"})
    assert route["blocked"] is True
    assert route["model"] is None


def test_p2_route_03_unknown_price_never_claimed_as_economy_winner():
    route = select_route({"economy": True}, [_cap("native", "unsupported"), _cap("cli", agent="claude", model="a")],
                         {"role": "worker", "needs": ["code"], "active_agent": "codex"})
    assert route["price_status"] == "unknown"
    assert route["selection_reason"] == "verified_external_fallback_after_native_unavailable"


def test_p2_route_04_confirmed_native_unsupported_allows_external_fallback_only_in_auto():
    caps = [_cap("native", "unsupported"), _cap("cli", agent="claude")]
    requirements = {"role": "worker", "needs": ["code"], "active_agent": "codex", "run_state": "not_started"}
    assert select_route({}, caps, requirements)["mode"] == "cli"
    blocked = select_route({}, caps, {**requirements, "requested_route": "native"})
    assert blocked["blocked"] is True
    api_only = select_route({"api_allowed": True}, [_cap("native", "unsupported"), _cap("api", agent="api")], requirements)
    assert api_only["blocked"] is True


def test_p2_route_05_native_capacity_wait_and_started_unknown_run_never_fallback():
    caps = [_cap("native", full=True, agent="codex"), _cap("cli", agent="claude")]
    req = {"role": "worker", "needs": ["code"], "active_agent": "codex"}
    waiting = select_route({}, caps, req)
    assert waiting["waiting"] is True and waiting["mode"] == "subagent"
    for state in ("started", "unknown"):
        route = select_route({}, [_cap("native", "unsupported"), _cap("cli", agent="claude")],
                             {**req, "run_state": state})
        assert route["blocked"] is True and route["mode"] is None
    forced = select_route({}, [_cap("native", "unsupported"), _cap("cli", agent="claude")],
                           {**req, "run_state": "unknown", "requested_route": "cli"})
    assert forced["blocked"] is True and forced["mode"] is None


def test_p2_route_06_policy_cas_snapshot_and_request_replay(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    first = _req("save_routing_policy", {"policy": {"economy": True, "api_allowed": False}}, 1)
    result, code = _run(db, first)
    assert code == 0 and result["result"]["revision"] == 1
    replay, replay_code = _run(db, first)
    assert replay_code == 0 and replay == result
    conflict, conflict_code = _run(db, _req("save_routing_policy", {"policy": {"api_allowed": True}}, 2))
    assert conflict_code == 3 and conflict["error"]["code"] == "revision_conflict"
    snapshot = _read(db, _req("read_routing_policy"))
    assert snapshot["revision"] == 1
    assert snapshot["body"]["price_status"] == "unknown"


def test_p2_route_07_native_then_permitted_cli_and_direct_api_excluded():
    caps = [_cap('native', 'unsupported'), _cap('cli', agent='claude'), _cap('api', agent='api')]
    need = {'role': 'worker', 'needs': ['code'], 'active_agent': 'codex'}
    route = select_route({}, caps, need)
    assert route['mode'] == 'cli' and route['agent'] == 'claude'
    assert select_route({}, caps, {**need, 'requested_route': 'api'})['blocked']
    assert select_route({}, caps[:1] + caps[2:], need)['blocked']


def test_capability_registration_preserves_unknown_and_never_invents_evidence(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    unknown = _cap("native", "unknown")
    result, code = _run(db, _req("register_capabilities", {"capabilities": [unknown]}, 1))
    assert code == 0 and result["result"]["revision"] == 1
    read = _read(db, _req("inspect_capabilities"))
    assert read["body"]["items"][0]["support"] == "unknown"
    invalid = _cap("native", "supported")
    invalid.pop("evidence_ref")
    result, code = _run(db, _req("register_capabilities", {"capabilities": [invalid]}, 1))
    assert code == 2 and result["error"]["code"] == "invalid_routing_request"


def test_selection_operation_returns_settings_and_capability_revisions(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    _run(db, _req("register_capabilities", {"capabilities": [
        _cap("native", "unsupported"), _cap("cli", agent="claude")]}, 1))
    req = _req("select_execution_route", {"requirements": {
        "role": "worker", "needs": ["code"], "active_agent": "codex"}})
    result, code = _run(db, req)
    assert code == 0
    assert result["result"]["mode"] == "cli"
    assert result["result"]["settings_revision"] == 0
    assert result["result"]["capability_revision"] == 1
    replay, replay_code = _run(db, req)
    assert replay_code == 0 and replay == result
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='routing.selected'").fetchone()[0] == 1


def test_malformed_input_and_secret_fields_are_rejected():
    with pytest.raises(PmtError):
        select_route({}, "not-an-array", {"role": "worker"})
    with pytest.raises(PmtError) as error:
        select_route({"api_key": "never store"}, [], {"role": "worker", "active_agent": "codex"})
    assert error.value.code == "secret_field_forbidden"
    assert READ_OPERATIONS == frozenset({"read_routing_policy", "inspect_capabilities"})
    assert WRITE_OPERATIONS == frozenset({"save_routing_policy", "register_capabilities", "select_execution_route"})
    assert FILE_OPERATIONS == frozenset()
