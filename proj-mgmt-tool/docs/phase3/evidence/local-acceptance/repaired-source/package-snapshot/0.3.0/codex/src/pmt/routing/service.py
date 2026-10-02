"""Routing policy and verified-capability snapshots.

This module never discovers model catalogs or infers support from product
versions. Callers supply capability evidence from an authenticated session or
another approved adapter.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
import math

from ..errors import PmtError
from ..util import canonical_json, utc_now

READ_OPERATIONS = frozenset({"read_routing_policy", "inspect_capabilities"})
WRITE_OPERATIONS = frozenset({"save_routing_policy", "register_capabilities", "select_execution_route"})
FILE_OPERATIONS = frozenset()

_POLICY_ID = "routing_policy"
_CAPABILITIES_ID = "routing_capabilities"
_MODES = {"native", "cli"}
_SUPPORT = {"supported", "unsupported", "unknown"}
_SECRET_KEYS = {"token", "api_key", "apikey", "secret", "password", "credential", "authorization"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


def _invalid(message, code="invalid_routing_request"):
    raise PmtError(code, message)


def _contains_secret_key(value):
    if isinstance(value, dict):
        return any(str(key).lower() in _SECRET_KEYS or _contains_secret_key(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _nonempty(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _invalid(f"{name} must be a nonempty string")
    return value.strip()


def _default_policy():
    return {"mode": "auto", "economy": True, "role_preferences": {},
            "api_allowed": False, "price_status": "unknown"}


def _verified(cap):
    """A support claim is usable only with supplied evidence and observation."""
    evidence = cap.get("evidence_ref")
    observed = cap.get("observed_at")
    source = cap.get("source_ref")
    try:
        parsed = datetime.fromisoformat(observed.replace("Z", "+00:00")) if isinstance(observed, str) else None
    except ValueError:
        parsed = None
    return (isinstance(evidence, str) and bool(evidence.strip())
            and isinstance(source, str) and bool(source.strip())
            and parsed is not None and parsed.tzinfo is not None)


def _supports_needs(cap, needs):
    available = cap.get("capabilities", [])
    return isinstance(available, list) and set(needs).issubset(set(x for x in available if isinstance(x, str)))


def _rank(candidate, preferred, requirements, economy):
    """User ordering first, then comparable, explicitly sourced price estimates."""
    preferences = preferred if isinstance(preferred, dict) else {}
    model = preferences.get("model")
    order = preferences.get("priority", [])
    order = order if isinstance(order, list) else []
    if model:
        order = [model, *[entry for entry in order if entry != model]]
    priority = order.index(candidate["model"]) if candidate["model"] in order else len(order)
    identity = sum(1 for field in ("agent", "provider") if preferences.get(field)
                   and preferences[field] != candidate.get(field))
    price = candidate.get("price", {})
    cost = float("inf")
    if (economy and isinstance(price, dict) and price.get("source_kind") in {"official", "account"}
            and price.get("source_ref") and price.get("observed_at") and price.get("currency") == "USD"
            and price.get("unit") == "per_million_tokens"):
        input_rate, output_rate = price.get("input"), price.get("output")
        if all(type(rate) in (int, float) and math.isfinite(rate) and rate >= 0 for rate in (input_rate, output_rate)):
            budget = requirements.get("token_budget", {"input": 1_000_000, "output": 1_000_000})
            if (isinstance(budget, dict) and all(type(budget.get(k)) is int and budget[k] >= 0 for k in ("input", "output"))):
                cost = (input_rate * budget["input"] + output_rate * budget["output"]) / 1_000_000
    candidate_priority = candidate.get("priority", 999)
    candidate_priority = candidate_priority if type(candidate_priority) is int else 999
    return (priority, identity, cost, candidate_priority, candidate["model"], candidate["provider"], candidate["capability_ref"])


def _route(candidate, reason, *, waiting=False, blocked=False):
    selected_mode = candidate.get("mode") if candidate else None
    if selected_mode == "native":
        selected_mode = "subagent"
    elif selected_mode == "sdk":
        selected_mode = "cli"
    return {
        "agent": candidate.get("agent") if candidate else None,
        "provider": candidate.get("provider") if candidate else None,
        "model": candidate.get("model") if candidate else None,
        "mode": selected_mode,
        "authorization_state": candidate.get("authorization_state") if candidate else None,
        "max_concurrency": candidate.get("max_concurrency", 1) if candidate else None,
        "selection_reason": reason,
        "selection_reason_code": reason,
        "capability_ref": candidate.get("capability_ref") if candidate else None,
        "actual_support": "verified_supported" if candidate else "unknown",
        "waiting": bool(waiting),
        "blocked": bool(blocked),
        "price_status": "unknown",
        "auth_state": candidate.get("auth_state", "unknown") if candidate else "unknown",
        "adapter_kind": "sdk" if candidate and candidate.get("mode") == "sdk" else "cli" if selected_mode == "cli" else selected_mode,
    }


def select_route(policy, capabilities, requirements):
    """Select a route from supplied facts; no external discovery or API calls."""
    if not isinstance(policy, dict) or not isinstance(capabilities, list) or not isinstance(requirements, dict):
        _invalid("policy, capabilities, and requirements must be objects/array")
    if _contains_secret_key(policy) or _contains_secret_key(capabilities) or _contains_secret_key(requirements):
        _invalid("routing data must not contain credentials", "secret_field_forbidden")
    if requirements.get("requested_route") in {"api", "sdk"}:
        return _route(None, "direct_api_or_sdk_disabled_by_project_scope", blocked=True)
    role = _nonempty(requirements.get("role"), "role")
    needs = requirements.get("needs", [])
    if not isinstance(needs, list) or any(not isinstance(n, str) or not n for n in needs):
        _invalid("needs must be an array of nonempty strings")
    explicit_mode = requirements.get("requested_route")
    explicit_model = requirements.get("requested_model")
    explicit_agent = requirements.get("requested_agent")
    explicit_provider = requirements.get("requested_provider")
    if explicit_mode == "subagent":
        explicit_mode = "native"
    if explicit_mode is not None and explicit_mode not in (_MODES | {"subagent"}):
        _invalid("requested_route is unsupported")
    for value, name in ((explicit_model, "requested_model"), (explicit_agent, "requested_agent"),
                        (explicit_provider, "requested_provider")):
        if value is not None:
            _nonempty(value, name)

    candidates = []
    for item in capabilities:
        if not isinstance(item, dict):
            continue
        if item.get("mode") not in _MODES or item.get("support") not in _SUPPORT:
            continue
        for key in ("agent", "provider", "model", "capability_ref"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                break
        else:
            if _supports_needs(item, needs) and item.get("support") == "supported" and _verified(item):
                if type(item.get("max_concurrency")) is int and item["max_concurrency"] > 0:
                    candidates.append(item)

    session_agent = requirements.get("active_agent")
    run_state = requirements.get("run_state", "not_started")
    if run_state not in {"not_started", "started", "unknown"}:
        _invalid("run_state must be not_started, started, or unknown")
    if run_state in {"started", "unknown"}:
        return _route(None, "existing_run_started_or_unknown_no_fallback", blocked=True)
    candidates = [c for c in candidates if c["mode"] == "native" or c["agent"] in {"codex", "claude"}]
    requested = [c for c in candidates
                 if (explicit_mode is None or c["mode"] == explicit_mode
                     or (explicit_mode == "cli" and c["mode"] == "sdk"))
                 and (explicit_model is None or c["model"] == explicit_model)
                 and (explicit_agent is None or c["agent"] == explicit_agent)
                 and (explicit_provider is None or c["provider"] == explicit_provider)]

    def explicit_block(reason):
        return _route(None, reason, blocked=True)

    if explicit_mode or explicit_model or explicit_agent or explicit_provider:
        if not requested:
            return explicit_block("explicit_selection_unavailable_or_unverified")
        preferred = policy.get("role_preferences", {}).get(role, {})
        requested.sort(key=lambda c: _rank(c, preferred, requirements, policy.get("economy", True)))
        chosen = requested[0]
        if chosen["mode"] == "native" and chosen["agent"] != session_agent:
            return explicit_block("native_route_must_match_active_agent")
        if chosen["mode"] in {"cli", "sdk"} and chosen.get("auth_state") != "authenticated":
            return explicit_block("cli_sdk_not_authenticated")
        if chosen["mode"] == "native" and chosen.get("capacity_full") is True:
            return _route(chosen, "native_capacity_full_wait", waiting=True)
        return _route(chosen, "user_explicit_selection")

    if not isinstance(session_agent, str) or not session_agent.strip():
        return _route(None, "active_session_agent_unknown", blocked=True)
    native_observations = [c for c in capabilities if isinstance(c, dict)
                           and c.get("mode") == "native" and c.get("agent") == session_agent
                           and _supports_needs(c, needs)
                           and (not explicit_model or c.get("model") == explicit_model)]
    same_agent_native = [c for c in candidates if c["mode"] == "native" and c["agent"] == session_agent]
    if same_agent_native:
        preferred = policy.get("role_preferences", {}).get(role, {}) if isinstance(policy.get("role_preferences", {}), dict) else {}
        preferred_model = preferred.get("model") if isinstance(preferred, dict) else None
        same_agent_native.sort(key=lambda c: _rank(c, preferred, requirements, policy.get("economy", True)))
        chosen = same_agent_native[0]
        if chosen.get("capacity_full") is True:
            return _route(chosen, "native_capacity_full_wait", waiting=True)
        return _route(chosen, "same_agent_native_verified_support")
    if not native_observations or any(c.get("support") != "unsupported" or not _verified(c)
                                      for c in native_observations):
        return _route(None, "native_capability_unknown_no_fallback", blocked=True)

    # Native has first priority; external routes are considered only after the
    # caller establishes that this request has not started.
    preferred = policy.get("role_preferences", {}).get(role, {}) if isinstance(policy.get("role_preferences", {}), dict) else {}
    preferred_model = preferred.get("model") if isinstance(preferred, dict) else None
    external = [c for c in candidates if c["mode"] == "cli"]
    external.sort(key=lambda c: _rank(c, preferred, requirements, policy.get("economy", True)))
    for candidate in external:
        if candidate["mode"] in {"cli", "sdk"} and candidate.get("auth_state") != "authenticated":
            continue
        return _route(candidate, "verified_external_fallback_after_native_unavailable")
    return _route(None, "no_verified_permitted_candidate", blocked=True)


def _snapshot(conn, key, default):
    row = conn.execute("SELECT revision,body_json,updated_at FROM routing_settings WHERE id=?", (key,)).fetchone()
    return ({"revision": int(row[0]), "body": json.loads(row[1]), "updated_at": row[2]} if row else
            {"revision": 0, "body": default, "updated_at": None})


def _payload(req):
    payload = req.get("payload", {})
    if not isinstance(payload, dict):
        _invalid("payload must be an object")
    return payload


def handle(db, conn, req):
    """Handler for routing operation requests inside the caller's transaction."""
    del db
    operation = req.get("operation")
    payload = _payload(req)
    if operation == "read_routing_policy":
        saved = _snapshot(conn, _POLICY_ID, _default_policy())
        return {"configured": saved["revision"] > 0, **saved}
    if operation == "inspect_capabilities":
        saved = _snapshot(conn, _CAPABILITIES_ID, {"items": []})
        return {"configured": saved["revision"] > 0, **saved}
    if operation == "select_execution_route":
        policy = _snapshot(conn, _POLICY_ID, _default_policy())
        capability_snapshot = _snapshot(conn, _CAPABILITIES_ID, {"items": []})
        requirements = payload.get("requirements")
        if not isinstance(requirements, dict):
            _invalid("requirements must be an object")
        route = select_route(policy["body"], capability_snapshot["body"]["items"], requirements)
        result = {**route, "settings_revision": policy["revision"],
                  "capability_revision": capability_snapshot["revision"],
                  "requested_route": requirements.get("requested_route"),
                  "requested_model": requirements.get("requested_model"),
                  "requested_agent": requirements.get("requested_agent"),
                  "requested_provider": requirements.get("requested_provider")}
        from ..phase2_common import event, validate_scope
        scope_id = req.get("scope_id")
        if scope_id is not None:
            validate_scope(None, conn, scope_id)
        event_type = "routing.waiting" if route["waiting"] else "routing.blocked" if route["blocked"] else "routing.selected"
        event(conn, {**req, "payload": {}}, event_type, scope_id=scope_id, payload={
            "agent": route["agent"], "provider": route["provider"], "model": route["model"],
            "mode": route["mode"], "max_concurrency": route["max_concurrency"],
            "selection_reason_code": route["selection_reason_code"],
            "capability_ref": route["capability_ref"], "actual_support": route["actual_support"],
            "settings_revision": policy["revision"], "capability_revision": capability_snapshot["revision"],
            "requested_route": requirements.get("requested_route"),
            "requested_model": requirements.get("requested_model"),
            "requested_agent": requirements.get("requested_agent"),
            "requested_provider": requirements.get("requested_provider"),
            "waiting": route["waiting"], "blocked": route["blocked"]})
        return result
    if operation not in WRITE_OPERATIONS:
        raise PmtError("operation_unsupported", "Unsupported routing operation")

    key = _POLICY_ID if operation == "save_routing_policy" else _CAPABILITIES_ID
    old = conn.execute("SELECT revision FROM routing_settings WHERE id=?", (key,)).fetchone()
    current = int(old[0]) if old else 0
    expected = req.get("expected_revision")
    # Initial creation may omit expected_revision or pass 1; updates must use
    # the exact current snapshot revision.
    valid_expected = ((current == 0 and expected in (None, 1)) or
                      (current > 0 and type(expected) is int and expected == current))
    if not valid_expected:
        raise PmtError("revision_conflict", "Routing settings revision does not match", 3,
                       details={"expected_revision": expected, "current_revision": current})

    if operation == "save_routing_policy":
        body = payload.get("policy")
        if not isinstance(body, dict) or _contains_secret_key(body):
            _invalid("policy must be an object without credential fields")
        unknown_keys = set(body) - {"mode", "economy", "role_preferences", "api_allowed", "price_status"}
        if unknown_keys:
            _invalid("policy contains unsupported fields")
        body = {**_default_policy(), **body}
        if body.get("api_allowed") is True:
            _invalid("Direct model API is outside the current implementation scope", "direct_api_disabled_by_project_scope")
        if (body.get("mode") != "auto" or type(body.get("economy")) is not bool
                or type(body.get("api_allowed")) is not bool
                or not isinstance(body.get("role_preferences", {}), dict)):
            _invalid("policy mode/economy/api_allowed values are invalid")
        if body.get("price_status", "unknown") != "unknown":
            # Prices may only be compared when an official/account source and
            # observation time are supplied. This module deliberately stores
            # unknown unless a separate verified price adapter is added.
            body["price_status"] = "unknown"
        for role_name, preferences in body["role_preferences"].items():
            _nonempty(role_name, "role")
            if not isinstance(preferences, dict) or set(preferences) - {"model", "agent", "provider", "priority"}:
                _invalid("Role preferences have unsupported fields")
            for field in ("model", "agent", "provider"):
                if field in preferences:
                    _nonempty(preferences[field], field)
            if "priority" in preferences and (not isinstance(preferences["priority"], list) or
                    any(not isinstance(model, str) or not model.strip() for model in preferences["priority"])):
                _invalid("Role priority must be an ordered list of model names")
    else:
        body = payload.get("capabilities")
        if not isinstance(body, list) or _contains_secret_key(body):
            _invalid("capabilities must be an array without credential fields")
        for cap in body:
            if not isinstance(cap, dict):
                _invalid("each capability entry must be an object")
            if cap.get("mode") not in _MODES or cap.get("support") not in _SUPPORT:
                _invalid("capability mode or support state is invalid")
            if "auth_state" in cap and cap["auth_state"] not in {"authenticated", "unauthenticated", "unknown"}:
                _invalid("auth_state is unsupported")
            for name in ("agent", "provider", "model", "capability_ref"):
                _nonempty(cap.get(name), name)
            if cap.get("support") in {"supported", "unsupported"} and not _verified(cap):
                _invalid("confirmed capability requires supplied evidence_ref, source_ref, and observed_at")
            if cap.get("support") in {"supported", "unsupported"} and (
                    type(cap.get("max_concurrency")) is not int or cap["max_concurrency"] < 1):
                _invalid("confirmed capability requires a positive max_concurrency")
        body = {"items": body}

    revision = current + 1
    updated = utc_now()
    encoded = canonical_json(body)
    conn.execute("INSERT INTO routing_settings(id,revision,body_json,updated_at) VALUES(?,?,?,?) "
                 "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body_json=excluded.body_json,updated_at=excluded.updated_at",
                 (key, revision, encoded, updated))
    from ..phase2_common import event, validate_scope
    scope_id = req.get("scope_id")
    if scope_id is not None:
        validate_scope(None, conn, scope_id)
    event(conn, {**req, "payload": {}}, "routing.policy_saved" if operation == "save_routing_policy" else "routing.capabilities_registered",
          scope_id=scope_id, payload={"revision": revision, "settings_id": key})
    return {"revision": revision, "settings_id": key, "updated_at": updated,
            "capability_count": len(body["items"]) if operation == "register_capabilities" else None}
