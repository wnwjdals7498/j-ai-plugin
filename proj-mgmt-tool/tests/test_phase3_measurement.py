from __future__ import annotations

import pytest

from pmt.efficiency.measurement import MeasurementError, capture_baseline, compare


def condition():
    return {"goal": "g1", "acceptance": "a1", "source": "s1", "environment": "e1",
            "model_role": "m1", "policy": "p1", "definition": "d1"}


def case():
    return {"id": "case-1", "purpose": "representative", "input": {"request_ref": "sha256:x"},
            "output": {"criterion": "linked-plan"},
            "observations": [{"input_bytes": 120, "output_bytes": 40, "calls": 1, "detail_queries": 2,
                              "context_generations": 1, "retries": 1, "rework": 3, "reviews": 1,
                              "elapsed_ms": 900, "tokens": {"status": "unknown", "actual": None, "estimate": None},
                              "evidence_tier": "model_actual",
                              "quality": {"status": "pass", "criteria": ["linked-plan"]}}]}


def test_compare_preserves_unknown_tokens_fixtures_and_total_rework():
    baseline = capture_baseline(case(), condition())
    current = {"condition": condition(), "case_fingerprint": baseline["case_fingerprint"],
               "observations": [{**case()["observations"][0], "evidence_tier": "fixture", "rework": 5}],
               "evidence_refs": ["run:current"]}
    result = compare(baseline, current)
    assert result["comparable"] is True
    assert result["token_usage"] == {"baseline": {"status": "unknown", "actual": None, "estimate": None},
                                     "measured": {"status": "unknown", "actual": None, "estimate": None}}
    assert result["costs"]["rework"] == {"baseline": 3, "measured": 5, "delta": 2}
    assert result["evidence_tiers"] == {"baseline": ["model_actual"], "measured": ["fixture"]}
    assert result["quality"]["baseline"][0]["status"] == "pass"


def test_condition_or_case_change_cannot_be_used_for_favorable_comparison():
    baseline = capture_baseline(case(), condition())
    changed = condition()
    changed["model_role"] = "cheaper-model"
    with pytest.raises(MeasurementError, match="condition_mismatch"):
        compare(baseline, {"condition": changed, "case_fingerprint": baseline["case_fingerprint"],
                           "observations": case()["observations"]})
    with pytest.raises(MeasurementError, match="case_mismatch"):
        compare(baseline, {"condition": condition(), "case_fingerprint": "changed",
                           "observations": case()["observations"]})


def test_missing_condition_fields_and_ambiguous_token_values_are_rejected():
    with pytest.raises(MeasurementError, match="condition_definition_missing"):
        capture_baseline(case(), {"goal": "g"})
    broken = case()
    broken["observations"][0]["tokens"] = {"status": "unknown", "actual": 4, "estimate": None}
    with pytest.raises(MeasurementError, match="unknown token usage"):
        capture_baseline(broken, condition())


def test_changed_baseline_is_rejected_as_non_immutable():
    baseline = capture_baseline(case(), condition())
    baseline["observations"][0]["rework"] = 0
    with pytest.raises(MeasurementError, match="baseline_manifest_modified"):
        compare(baseline, {"condition": condition(), "case_fingerprint": baseline["case_fingerprint"],
                           "observations": case()["observations"]})


def test_estimate_is_not_merged_with_unknown_or_actual_tokens():
    baseline = capture_baseline(case(), condition())
    estimated = {"condition": condition(), "case_fingerprint": baseline["case_fingerprint"],
                 "observations": [{**case()["observations"][0],
                                   "tokens": {"status": "estimate", "actual": None, "estimate": 25}}]}
    result = compare(baseline, estimated)
    assert result["token_usage"]["baseline"]["status"] == "unknown"
    assert result["token_usage"]["measured"] == {"status": "estimate", "actual": None, "estimate": 25}
