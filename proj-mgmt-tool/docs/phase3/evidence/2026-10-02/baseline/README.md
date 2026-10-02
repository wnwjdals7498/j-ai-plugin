# PMT3 pre-optimization baseline

`phase2-planning-full-transfer.json` is the reproducible baseline for full graph and document delivery. It uses the existing `tests/test_phase2_planning.py` graph fixture, normalized to fixed UUIDs in `canonical-planning-graph.json`, then invokes the unchanged phase2 `validate_graph` and `render_docs` functions. The checked execution used commit `4f632da7065bd025d1b5053ef8d43cfe9774709f`, Python 3.13.13, graph schema 1, 3 nodes and 2 relations.

The recorded run observed 3,956 UTF-8 input bytes, 1,644 rendered Markdown bytes and 2,743 full graph JSON bytes (4,387 output bytes total). It separately measured validator and renderer wall time with `perf_counter_ns`; timings are single-run local observations and will vary. The input graph, both rendered outputs, exact output hashes, implementation file hashes, complete comparison condition, and manifest fingerprint are preserved. Tokens are unknown. This is a deterministic fixture-quality result; it says nothing about native model quality.

Re-run from the repository root with:

```powershell
$env:PYTHONPATH = 'src'
.venv/Scripts/python.exe docs/phase3/evidence/2026-10-02/baseline/run_baseline.py
```

The script refreshes the observed manifest and rendered outputs from the preserved input. Before comparing a future run, use the condition in the refreshed manifest; a change in source hashes, runtime, operation, policy, or definition makes it a different condition.

`representative-case-definitions.json` fixes six reusable scenario meanings: new full plan, partial change, resume, investigation context, ambiguous requirement, and failure/retry/rework. Only the full-plan case has an observed baseline in the existing phase2 graph/render path. The other five remain `not_run` with reasons; they are not assigned invented costs or a pass result. The earlier `native-planning.json` preserves historical actual-model quality evidence separately. Its original prompt bytes, graph/document bytes, elapsed time, and rework receipts were not retained, so it cannot serve as a measured full-transfer baseline.

F0-S4 measurement API: `pmt.efficiency.measurement.capture_baseline(case, condition)` and `compare(baseline, measured)`. They require complete matching goal, acceptance, source, environment, model/role, policy, and definition values; preserve actual, estimated, and unknown tokens separately; and count calls, detail reads, context creation, retries, rework, reviews, bytes, and elapsed time. A changed or missing condition is rejected. Fixture and actual-model quality evidence remain distinct. F10-01 full integration has not been run.

F0 checks in `tests/test_phase3_measurement.py` cover condition mismatch, case mismatch, extra costs, unknown/estimated token distinction, fixture/model quality tiers, and modified baseline rejection. Command: `.venv/Scripts/python.exe -m pytest tests/test_phase3_measurement.py -q --basetemp .pmt-test/phase3-measurement` (exit 0, 5 passed). The test command does not call an external model.
