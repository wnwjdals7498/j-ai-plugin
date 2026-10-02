"""Local, provider-independent measurement manifests for PMT efficiency work.

This module deliberately stores fingerprints and measurements, never source text.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 1
_REQUIRED_CONDITION = ("goal", "acceptance", "source", "environment", "model_role", "policy", "definition")
_COST_KEYS = ("calls", "detail_queries", "context_generations", "retries", "rework", "reviews", "elapsed_ms")


class MeasurementError(ValueError):
    """Invalid or incomparable measurement input."""


def _validate_refs(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 512 or "\n" in ref or "\r" in ref for ref in value):
        raise MeasurementError(f"{field} must be an array of short, nonempty references")
    return list(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def time_call(function: Any, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    """Run a local callable and return its value plus measured wall time in ms."""
    started = time.perf_counter_ns()
    value = function(*args, **kwargs)
    return value, (time.perf_counter_ns() - started) / 1_000_000


def _validate_condition(condition: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(condition, Mapping):
        raise MeasurementError("condition must be an object")
    missing = [key for key in _REQUIRED_CONDITION if key not in condition or condition[key] in (None, "", {})]
    if missing:
        raise MeasurementError("condition_definition_missing: " + ",".join(missing))
    return dict(condition)


def _validate_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(observation, Mapping):
        raise MeasurementError("observation must be an object")
    result = dict(observation)
    for field in ("input_bytes", "output_bytes", *_COST_KEYS):
        value = result.get(field)
        valid_type = type(value) is int if field != "elapsed_ms" else type(value) in (int, float)
        if field in result and value is not None and (not valid_type or value < 0):
            raise MeasurementError(f"{field} must be a nonnegative numeric measurement")
    token = result.get("tokens", {"status": "unknown", "actual": None, "estimate": None})
    if not isinstance(token, Mapping) or token.get("status") not in {"actual", "estimate", "unknown"}:
        raise MeasurementError("tokens must explicitly distinguish actual, estimate, or unknown")
    token = dict(token)
    if token["status"] == "unknown" and (token.get("actual") is not None or token.get("estimate") is not None):
        raise MeasurementError("unknown token usage cannot contain a value")
    if token["status"] == "actual" and (type(token.get("actual")) is not int or token.get("estimate") is not None or token["actual"] < 0):
        raise MeasurementError("actual token usage requires a nonnegative actual count")
    if token["status"] == "estimate" and (type(token.get("estimate")) is not int or token.get("actual") is not None or token["estimate"] < 0):
        raise MeasurementError("estimated token usage requires a nonnegative estimate")
    result["tokens"] = token
    result.setdefault("input_bytes", None)
    result.setdefault("output_bytes", None)
    for key in _COST_KEYS:
        result.setdefault(key, None)
    result.setdefault("evidence_tier", "fixture")
    if result["evidence_tier"] not in {"fixture", "local_integration", "product", "model_actual"}:
        raise MeasurementError("unsupported evidence_tier")
    result.setdefault("quality", {"status": "not_run", "criteria": []})
    if not isinstance(result["quality"], Mapping) or result["quality"].get("status") not in {"pass", "fail", "blocked", "not_run"}:
        raise MeasurementError("quality status must be pass, fail, blocked, or not_run")
    if "criteria" in result["quality"]:
        _validate_refs(result["quality"]["criteria"], "quality.criteria")
    return result


def capture_baseline(case: Mapping[str, Any], condition: Mapping[str, Any]) -> dict[str, Any]:
    """Capture one immutable-shaped baseline manifest from explicit observations.

    case: {id, purpose, input, output, observations, criteria}; condition holds
    goal/acceptance/source/environment/model_role/policy/definition fingerprints.
    """
    if not isinstance(case, Mapping):
        raise MeasurementError("case must be an object")
    for key in ("id", "purpose", "input", "output", "observations"):
        if key not in case or case[key] in (None, "", {}):
            raise MeasurementError(f"case_definition_missing: {key}")
    cond = _validate_condition(condition)
    if not isinstance(case["observations"], list):
        raise MeasurementError("case observations must be an array")
    observations = [_validate_observation(item) for item in case["observations"]]
    if not observations:
        raise MeasurementError("at least one observation is required")
    if "criteria" in case:
        _validate_refs(case["criteria"], "case.criteria")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "baseline",
        "case_id": str(case["id"]),
        "purpose": str(case["purpose"]),
        "condition": cond,
        "condition_fingerprint": fingerprint(cond),
        "case_fingerprint": fingerprint({"id": case["id"], "purpose": case["purpose"], "input": case["input"], "output": case["output"]}),
        "semantic_io": {"input_fingerprint": fingerprint(case["input"]), "output_fingerprint": fingerprint(case["output"])},
        "observations": observations,
        "evidence_refs": _validate_refs(case.get("evidence_refs", []), "case.evidence_refs"),
    }
    payload["manifest_fingerprint"] = fingerprint(payload)
    return payload


def compare(baseline: Mapping[str, Any], measured: Mapping[str, Any]) -> dict[str, Any]:
    """Compare a baseline and measured manifest; condition changes are rejected."""
    if not isinstance(baseline, Mapping) or baseline.get("kind") != "baseline" or baseline.get("schema_version") != SCHEMA_VERSION:
        raise MeasurementError("invalid or unsupported baseline manifest")
    baseline_body = {key: value for key, value in baseline.items() if key != "manifest_fingerprint"}
    if fingerprint(baseline_body) != baseline.get("manifest_fingerprint"):
        raise MeasurementError("baseline_manifest_modified: immutable baseline fingerprint does not match")
    if not isinstance(measured, Mapping):
        raise MeasurementError("measured run must be an object")
    cond = _validate_condition(measured.get("condition", {}))
    if fingerprint(cond) != baseline.get("condition_fingerprint"):
        raise MeasurementError("condition_mismatch: comparisons require identical goal, acceptance, source, environment, model/role, policy, and definition")
    if measured.get("case_fingerprint") != baseline.get("case_fingerprint"):
        raise MeasurementError("case_mismatch: semantic input/output definition changed")
    if not isinstance(measured.get("observations"), list):
        raise MeasurementError("measured observations must be an array")
    current = [_validate_observation(item) for item in measured["observations"]]
    if not current:
        raise MeasurementError("at least one measured observation is required")
    before = baseline["observations"]
    keys = ["input_bytes", "output_bytes", *_COST_KEYS]
    totals = {}
    for key in keys:
        left_values = [item.get(key) for item in before]
        right_values = [item.get(key) for item in current]
        left = sum(left_values) if all(x is not None for x in left_values) else None
        right = sum(right_values) if all(x is not None for x in right_values) else None
        totals[key] = {"baseline": left, "measured": right,
                       "delta": right - left if left is not None and right is not None else None}
    baseline_tokens = [item["tokens"] for item in before]
    measured_tokens = [item["tokens"] for item in current]

    def token_summary(values: list[dict[str, Any]]) -> dict[str, Any]:
        statuses = {item["status"] for item in values}
        status = statuses.pop() if len(statuses) == 1 else "mixed"
        return {"status": status,
                "actual": sum(x["actual"] for x in values) if status == "actual" else None,
                "estimate": sum(x["estimate"] for x in values) if status == "estimate" else None}
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "comparison",
        "baseline_fingerprint": baseline.get("manifest_fingerprint"),
        "measured_condition_fingerprint": fingerprint(cond),
        "comparable": True,
        "case_id": baseline["case_id"],
        "costs": totals,
        "token_usage": {"baseline": token_summary(baseline_tokens), "measured": token_summary(measured_tokens)},
        "quality": {"baseline": [x["quality"] for x in before], "measured": [x["quality"] for x in current]},
        "evidence_tiers": {"baseline": sorted({x["evidence_tier"] for x in before}), "measured": sorted({x["evidence_tier"] for x in current})},
        "evidence_refs": _validate_refs(measured.get("evidence_refs", []), "measured.evidence_refs"),
    }
    result["comparison_fingerprint"] = fingerprint(result)
    return result
