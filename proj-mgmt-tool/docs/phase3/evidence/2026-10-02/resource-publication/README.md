# Immutable structured-resource publication repair — 2026-10-02

`persist_json_resource` no longer replaces a content-addressed target. It writes an operation-journal-bound sibling stage, publishes with a no-replace hard link, hashes the final target before registering the artifact, and converges only when a concurrent target contains the same SHA-256 and size. Different bytes are a conflict. Only Windows sharing violations 32/33 get bounded retry (three waits totaling at most 0.5 seconds); unsupported hard links and other I/O failures remain explicit. A failed/unknown publication leaves the exact stage and `resource_publish` journal in `staging`. Stage-cleanup errors do not mask the original error; successful final bytes can still be registered and the cleanup reference remains in the completed journal.

Evidence from the final full regression retained at `.pmt-test/current-full-regression/pytest-output.txt` showed two actual F9 native/CLI failures at `phase2_common.py:90` (`os.replace`, WinError 32), followed by the `finally: tmp.unlink()` WinError 32 masking the original `resource_io_error`. The repair preserves no-overwrite semantics and the same deterministic artifact ID/hash/size/reference transaction.

Current-source targeted validation:

- `.venv\Scripts\python.exe -m pytest tests/test_phase2_resource_publish.py tests/test_phase2_operations.py -q --tb=short --basetemp=.pmt-test/p2-resource-replay-regression` — exit **0**, **18 passed**.
- `.venv\Scripts\python.exe -m pytest "tests/test_phase3_batch.py::test_actual_native_group_handle_and_structured_report_collect_per_child[1]" -q --tb=short --basetemp=.pmt-test/p3-f1-recovery-real` — exit **0**, **1 passed**.
- `.venv\Scripts\python.exe -m pytest tests/test_phase3_batch.py::test_actual_cli_group_runs_one_supervisor_and_collects_child_mapping -q --tb=short --basetemp=.pmt-test/p2-resource-cli-readonly-retry` — exit **0**, **1 passed**.

The concurrency suite verifies deterministic empty-target creation, identical-content convergence under two concurrent publishers, different-content conflict without replacing existing bytes, injected WinError 32 retry, preservation of the original PmtError/stage/journal after retry exhaustion, and exact same-request stage recovery. Both actual F9 native-handle/structured-report and CLI supervisor result cases pass in isolation. A combined rerun passed 19 tests but encountered one intermittent Windows SQLite `attempt to write a readonly database` during the CLI fixture's setup insert; that actual CLI case passed immediately in its isolated rerun. The original WinError 32 failures are resolved.

The earlier full suite output was not modified. Two other Windows SQLite read-only fixture-setup failures in that report were isolated and are outside this file-publication change.
