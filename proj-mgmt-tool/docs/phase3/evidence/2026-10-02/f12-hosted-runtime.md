# F12 hosted local runtime evidence

Baseline: `4f632da7065bd025d1b5053ef8d43cfe9774709f` (working tree; no commit created).

The hosted runtime now consumes the Host F9 batch binding before returning one native action. It rechecks the parent and every child run, F5 ref, directive digest, criteria digest, source pin, and scope-union fingerprint. The integration fixture exercised nonce replay, rejection of a second handle for the same nonce, one parent handle, F9 report collection, `not_run` child criteria, review-pending state, and retained scope locks. Its short-lived Python process and handle were test fixtures; no model or external native tool was invoked.

F14 stores the completed local supervisor manifest in the profile-scoped pending outbox before upload and stores the exact deterministic result request before submit. Offline capture reads only the attached managed spool, verifies the terminal receipt and byte hashes, stages the receipt resource, and leaves Host run state unchanged. For a pending request ID, reconciliation first checks current owner-scoped request history and reads `read_source_metadata` with the exact Project/repository/workspace/graph mapping. This read validates the current immutable Host snapshot and explicitly grants no execution or checkout access. If the exact response is absent, reconciliation checks current SourcePin, workspace mapping, and run revision before replay.

`inspect_graph_source` now derives clean Git pins from validated canonical graph content when Git reports a clean checkout, so LF and CRLF worktrees with the same commit have the same SourcePin. A dirty status still records the raw working-file hash and Git status; missing or invalid baseline content remains unknown.

Verification on isolated temporary databases and worktrees:

- `python -m pytest tests/test_phase3_hosted_runtime.py tests/test_phase3_host_batch_state.py -q --tb=short --basetemp=.pmt-test/f12-runtime-batch-final` — exit 0, 8 passed, 69.28 s.
- `python -m pytest tests/test_phase3_workspace.py -q --tb=short --basetemp=.pmt-test/f12-source-workspace-final` — exit 0, 8 passed, 1 symlink-privilege skip, 19.88 s.
- `python -m pytest tests/test_phase3_hosted_cli.py::test_cli_preserves_offline_terminal_receipt_then_reconciles_once -q --tb=short --basetemp=.pmt-test/f12-cli-pending-fix` — exit 0, 1 passed, 17.69 s.
- `python -m pytest tests/test_phase3_control.py::test_f8_owned_request_replay_requires_semantic_fingerprint -q --tb=short --basetemp=.pmt-test/f12-control-replay-isolated` — exit 0, 1 passed.
- The combined control/pending/CLI run produced 38 passed, 1 Windows read-only database setup error, 1 deselected. The failing replay test passed in its own fresh fixture; `test_f8_cancel_ack_does_not_release_native_lock_without_stop_confirmation` also passed in isolation.

After the history-read boundary change, the hosted receipt replay test passed with the run moved to `succeeded`, its P2 lock released, and the local checkout unavailable. A broader current-target run exposed a Host F9 source-pointer regression (`relative_graph_path` missing from batch authorization); Root repaired the shared callers. The final targeted rerun passed: hosted group action/ACK/replay/collection, offline pending reconciliation, Host F9 prepare/read, and the two-Project source-metadata test — 4 passed in 47.92 s. The offline CLI reconciliation plus F8 semantic replay tests also passed — 2 passed in 17.89 s.

Source hashes are generated from randomized isolated fixtures. The tests assert the captured SourcePin matches the current F5/source reference and that the two clean LF/CRLF checkouts have identical SourcePins; temporary values are not written into this evidence file.
