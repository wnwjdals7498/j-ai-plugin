"""Re-run the preserved phase2 full-graph validation/render baseline locally."""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from pathlib import Path

from pmt.efficiency.measurement import capture_baseline, fingerprint, time_call
from pmt.planning.graph import render_docs, validate_graph


def main() -> None:
    output_dir = Path(__file__).resolve().parent
    root = output_dir.parents[4]
    source_path = output_dir / "canonical-planning-graph.json"
    source_bytes = source_path.read_bytes()
    graph = json.loads(source_bytes.decode("utf-8"))
    report, validate_ms = time_call(validate_graph, graph, graph["project_id"])
    (rendered_markdown, rendered_json, render_report), render_ms = time_call(render_docs, graph)
    if render_report != report:
        raise RuntimeError("validation report changed between validation and rendering")
    markdown_bytes = rendered_markdown.encode("utf-8")
    graph_bytes = rendered_json.encode("utf-8")
    (output_dir / "rendered-plan.md").write_bytes(markdown_bytes)
    (output_dir / "rendered-plan.graph.json").write_bytes(graph_bytes)

    files = ("src/pmt/planning/graph.py", "src/pmt/util.py", "tests/test_phase2_planning.py")
    source_hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    fixture_hash = hashlib.sha256(source_bytes).hexdigest()
    condition = {
        "goal": "Validate and render the complete canonical planning graph as the unoptimized full-transfer reference.",
        "acceptance": {
            "definition": "existing phase2 validator complete graph + deterministic markdown and canonical graph JSON",
            "criterion": "valid complete graph, rendered output hashes and byte lengths recorded",
        },
        "source": {"repository_commit": "4f632da7065bd025d1b5053ef8d43cfe9774709f", "graph_schema": 1,
                   "fixture_sha256": fixture_hash, "implementation_file_sha256": source_hashes},
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "sqlite": sqlite3.sqlite_version, "execution": "local process; no external model; single-run timing observation"},
        "model_role": {"model": "none", "role": "existing phase2 graph validator and document renderer"},
        "policy": {"operation": "validate complete graph then render full plan markdown and full graph JSON", "retry_policy": "no retry"},
        "definition": {"measurement_schema": "pmt-efficiency-local-v1",
                       "byte_definition": "UTF-8 source graph bytes and UTF-8 rendered Markdown + JSON bytes",
                       "timing_definition": "perf_counter_ns elapsed time per validator call and render_docs call; render_docs itself validates once"},
    }
    case = {
        "id": "phase2-canonical-plan-full-graph-render",
        "purpose": "Reproducible full graph/document transfer baseline using the existing test graph fixture with stable UUIDs. Quality tier is fixture only.",
        "input": {"artifact": "canonical-planning-graph.json", "sha256": fixture_hash},
        "output": {"validator_report": report, "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
                   "graph_json_sha256": hashlib.sha256(graph_bytes).hexdigest()},
        "observations": [{
            "input_bytes": len(source_bytes), "output_bytes": len(markdown_bytes) + len(graph_bytes), "calls": 2,
            "detail_queries": 0, "context_generations": 0, "retries": 0, "rework": 0, "reviews": 0,
            "elapsed_ms": validate_ms + render_ms, "validate_elapsed_ms": validate_ms, "render_elapsed_ms": render_ms,
            "tokens": {"status": "unknown", "actual": None, "estimate": None}, "evidence_tier": "fixture",
            "quality": {"status": "pass" if report["valid"] and report["complete"] else "fail",
                        "criteria": ["existing phase2 validator accepted complete graph",
                                     "existing phase2 renderer produced deterministic UTF-8 Markdown and graph JSON"],
                        "model_quality": "not_evaluated"},
        }],
        "evidence_refs": ["docs/phase3/evidence/2026-10-02/baseline/canonical-planning-graph.json",
                          "docs/phase3/evidence/2026-10-02/baseline/rendered-plan.md",
                          "docs/phase3/evidence/2026-10-02/baseline/rendered-plan.graph.json",
                          "tests/test_phase2_planning.py", "src/pmt/planning/graph.py"],
    }
    manifest = capture_baseline(case, condition)
    manifest["observations"][0].update(validate_elapsed_ms=validate_ms, render_elapsed_ms=render_ms)
    manifest.pop("manifest_fingerprint")
    manifest["manifest_fingerprint"] = fingerprint(manifest)
    target = output_dir / "phase2-planning-full-transfer.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"input_bytes": len(source_bytes), "markdown_bytes": len(markdown_bytes),
                      "graph_json_bytes": len(graph_bytes), "output_bytes": len(markdown_bytes) + len(graph_bytes),
                      "validate_elapsed_ms": validate_ms, "render_elapsed_ms": render_ms,
                      "input_sha256": fixture_hash, "manifest_sha256": manifest["manifest_fingerprint"]}))


if __name__ == "__main__":
    main()
