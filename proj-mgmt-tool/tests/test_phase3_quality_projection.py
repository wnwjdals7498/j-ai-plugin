"""Same-source full/bounded payload evidence; no provider token estimate."""
import json
import os
from pathlib import Path
import subprocess
import time
from contextlib import closing

pytest_plugins = ["test_phase3_context"]

from pmt.util import canonical_json, new_id, fingerprint
from test_phase3_context import _graph_node, _actual_f3_ready, _build_actual


def test_same_source_projection_preserves_handoff_and_measures_both_payloads(actual_context_env):
    env = actual_context_env
    graph_path = env["workspace"] / env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    for index in range(80):
        other_step = new_id()
        requirement = _graph_node(new_id(), "requirement", other_step)
        implementation = _graph_node(new_id(), "implementation", other_step)
        requirement["summary"] = f"Unrelated requirement {index}"
        implementation["summary"] = f"Unrelated implementation {index}"
        graph["nodes"].extend([requirement, implementation])
        graph["relations"].append({"id": new_id(), "kind": "implements",
                                  "from": requirement["id"], "to": implementation["id"]})
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(env["workspace"]), "add", env["graph_path"]], check=True)
    subprocess.run(["git", "-C", str(env["workspace"]), "commit", "-qm", "fixed quality fixture"], check=True)
    pin = _actual_f3_ready(env)
    started = time.perf_counter_ns()
    bounded, code = _build_actual(env, pin, max_bytes=24000, max_lines=2000)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    assert code == 0 and bounded["ok"] and not bounded["result"]["incomplete"], bounded
    from pmt.phase2_common import load_json_resource
    with closing(env["db"].connect()) as conn:
        directive = load_json_resource(env["db"], conn, env["directive_id"])
    full = {"source_pin": pin, "graph": graph, "directive": directive,
            "criteria": env["criteria"], "task": {"step_id": env["step_id"],
            "item_id": env["item_id"], "run_id": env["run_id"]}}
    full_wire = canonical_json(full).encode("utf-8")
    bounded_wire = canonical_json(bounded["result"]).encode("utf-8")
    assert len(bounded_wire) < len(full_wire)
    text = bounded_wire.decode("utf-8")
    required = ["Build a verified context", "Preserve source and criteria",
                "Do not broaden access", "claims and authentication", "source-current",
                "bounded-context", "safe IDs only", "read", "project"]
    assert all(value in text for value in required)
    assert "Unrelated requirement" not in text
    location = os.environ.get("PMT_QUALITY_EVIDENCE_ROOT")
    if location:
        root = Path(location).absolute()
        root.mkdir(parents=True, exist_ok=True)
        (root / "full.json").write_bytes(full_wire)
        (root / "bounded.json").write_bytes(bounded_wire)
        report = {"tier": "local_integration", "target_source_hash": pin["source_hash"],
            "condition_sha256": fingerprint({"source": pin, "role": "lower", "acceptance": required}),
            "full_payload_bytes": len(full_wire), "bounded_payload_bytes": len(bounded_wire),
            "bounded_build_elapsed_ms": elapsed, "bounded_incomplete": False,
            "criteria_preservation": "pass", "provider_tokens": None,
            "model_quality": "not_run", "total_cost_comparison": "not_run",
            "scope": "payload comparison only; setup/index/detail/retry/model costs excluded"}
        (root / "comparison.json").write_text(canonical_json(report) + "\n", encoding="utf-8")
