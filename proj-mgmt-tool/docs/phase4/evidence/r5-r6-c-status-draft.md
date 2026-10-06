# P4-R5/R6 C evidence and status draft

2026-10-06 02:41 UTC. Source HEAD `ac7adedc1e7d2464e0023e70a01515c8d70cb878`; current worktree is dirty and shared Phase 4 work is still being integrated. Core is 0.4.0 / DB schema 5 / graph schema 1. This is C's tiered evidence draft, not final Phase 4 acceptance.

## Implemented C boundary

Host exposes eight explicit continuity storage ports plus five pure metadata service operations. Every request uses the registered Host principal, current session, namespace, explicit project scope and current read/write permission. Generic Host object writes cannot publish `checkpoint` or `alignment`; generic pointer writes accept only same-kind shared metadata and known non-service purposes, with repository/branch/workspace/task/environment dimensions checked against the immutable object. Generic effect updates cannot mark effects `completed`. Private `bundle`/`detail` objects are rejected on generic Host wire operations. `create_checkpoint`, `read_checkpoint`, `read_current_facts`, `link_session`, and `compose_resume_overview` call their actual Phase 4 service handlers inside the Host application transaction.

Hosted `capture_work_basis` now uses a client composite: it checks the current Host run and workspace grant before local file access, inspects Git and selected file hashes on the mapped client checkout, compares before/after observations, validates or updates the existing client-attested F3 graph snapshot, and sends only canonical refs, SourcePin and inventory hashes/counts to the dedicated `publish_work_basis` operation. Host rechecks run owner/revision, scope, current source pointer and SQL work snapshot, then constructs the stored BasisVector with `source_provenance=client_attested` and `host_git_verified=false`. Relative paths and per-file hashes remain in a session-scoped client spool; no local SQLite primary is created in Hosted mode.

SessionStart now requests bounded `compose_resume_overview` metadata with an explicit `PMT_SCOPE_ID`. A unique configured workspace mapping supplies a selector; absent or ambiguous mappings do not select a branch implicitly. Hosted Hook reads map only the allowlisted native SessionStart source to the stored installation principal and still require current Host authentication and explicit scope. Stop, idle, SessionEnd and prompt events remain observation events; only service-validated persisted receipts can create a checkpoint.

Migration bundles include `continuity_objects`, `continuity_pointers`, `continuity_events`, and `continuity_journal`. Shared immutable body hashes and pointers survive import. Private session projections are omitted with a manifest count, ID digest and recreation reason; ownership is never transferred. Non-completed journals block migration/transfer backup and target import. The ordinary local backup operation also blocks when unresolved continuity effects exist. A schema-4 backup restores through additive schema-5 migration; unknown future versions remain rejected.

## Latest executed C verification

Command: `.venv/Scripts/python.exe -m pytest tests/test_hooks.py tests/test_storage_config.py tests/test_phase4_host_continuity.py tests/test_phase4_hosted_cli.py tests/test_backup.py tests/test_phase3_migration.py --basetemp=.pmt-test/p4-c-final-round -q --tb=short`

Actual exit code: 0. Result: **64 passed** in 94.40 seconds, 2026-10-06. It used isolated local Git/SQLite/files and real loopback HTTPS with a temporary CA and independent Host process. Coverage includes Hook fixtures and native source authentication, Host generic CAS/replay/scope/private boundaries, Hosted overview, actual local Git/inventory capture and server-side basis construction, actual `decision_saved` checkpoint creation with `session_idle` rejection and unchanged pointer, backup journal quiescence and continuity retention, schema-4-to-5 restore, migration import/export and private projection invalidation.

After that run, the hosted selector test was extended to instantiate the `HostedChangesClient` router and assert there is no local SQLite fallback. The focused follow-up command `.venv/Scripts/python.exe -m pytest tests/test_storage_config.py::test_hosted_selector_routes_allowlist_and_blocks_local_database_fallback tests/test_phase4_hosted_cli.py --basetemp=.pmt-test/p4-c-hosted-route2 -q --tb=short` exited 0 with **6 passed** in 39.20 seconds. This confirms client adapter routing and Hosted CLI regressions. Separate actual loopback TLS R2 capture/detail command `.venv/Scripts/python.exe -m pytest tests/test_phase4_hosted_changes.py --basetemp=.pmt-test/p4-c-b-hostedchanges -q --tb=short` exited 0 with **1 passed** in 22.86 seconds: it captures before/after basis, reads the actual authorized client Git checkout, stores the bounded diff in local private spool, and returns the detail while Host metadata contains no checkout path. A later Hosted R3 actual loopback TLS pipeline passed with `.venv/Scripts/python.exe -m pytest tests/test_phase4_hosted_alignment_actual.py -q --basetemp=.pmt-test/p4-b-hosted-r3-final`: **1 passed**, exit 0, 64.70 seconds. It used an actual saved decision/event receipt, client Git change/index, typed graph impact, F1/F3 HostedFiles effects, C's typed apply-alignment receipt, physical client readback, exact same-request replay, and verified applied pointer revision 1.

The local Git/SQLite change-and-alignment regression `.venv/Scripts/python.exe -m pytest tests/test_phase4_r2_r3_actual.py --basetemp=.pmt-test/p4-c-r2r3 -q --tb=short` exited 0 with **7 passed** in 105.65 seconds. This is distinct from the Hosted R3 loopback positive above.

The Host decision receipt and alignment-quiescence checks were tested with `.venv/Scripts/python.exe -m pytest tests/test_phase4_host_decision_receipt.py --basetemp=.pmt-test/p4-c-quiescence3 -q --tb=short`; actual exit 0, **2 passed** in 5.88 seconds. The TLS case reads the actual saved decision event ID while returning only bounded refs/hash/revision/target/kind/state; stale revision, wrong kind, revoked/superseded decision, wrong active-run owner, and read-only device are denied, and a reason-text sentinel is absent from the response. The quiescence fixture confirms current completed F1/F3 receipts are allowed, while active affected Step, another same-workspace run, an incomplete Host file effect and an unresolved continuity journal block advancement.

The checkpoint selector fix keeps `environment_id=None` in the `current` checkpoint pointer lookup while the Host request still carries the authenticated environment. The actual Host checkpoint→SessionStart overview integration test plus native overview test passed **2 tests** in 21.88 seconds, `.pmt-test/p4-c-hook-checkpoint`; it verifies the newly created checkpoint ref and basis ref appear in the native SessionStart overview. Hook and storage regressions also passed **30 tests** in 10.32 seconds, `.pmt-test/p4-c-hook-selector`.

Current C source SHA-256 at the time of this draft:

| File | SHA-256 |
|---|---|
| `src/pmt/hooks.py` | `7d4449268564f5051bd60db7962fb116451b6c5cd410767f8589bf688644deda` |
| `src/pmt/storage_config.py` | `e9606684229879126fe59629a24be5f2df78a92bd4f3a2c0a754e2ce1340112c` |
| `src/pmt/hosted_continuity.py` | `56f13d5ac2bb20fe9259c52e7155561af8b48eafdb2e44e7d4b9eb5e189aa7f8` |
| `src/pmt/host/host_contract.py` | `7b8ef1de1f64234dd6474234e35e9c2e28122a04bbe1007522005a9a83a02980` |
| `src/pmt/host/data.py` | `f1a37147c0db1f2e0be0d0764a3954ebfe9a4825626655278dd78e9132757b60` |
| `src/pmt/migration.py` | `d1c06837a724d122d16e7094989ea2d05dd46f14fb66492d6725c668f4d44b5c` |
| `src/pmt/resources.py` | `2fd06f7eeeb0a658ce50cade7fb617e5b0dc98ac39d3c1efc57a4b12cdaa75666` |
| `tests/test_phase4_host_continuity.py` | `7d6b6ccc13db7aa28eacfff911d14f1b1561b7761b966ae11b11566a71effae6` |
| `tests/test_phase4_hosted_cli.py` | `1a3d8de9be8e6b3032f346d842b580b1999f9b8750c14b5a40e8b6e169d4f3b4` |

These hashes predate the final composite Host checkpoint/basis fixture edits later in this worktree and must be refreshed at the final source freeze.

## Tier status and remaining gates

| Acceptance area | Status at this tier |
|---|---|
| P4-R5-01 product fixtures and hosted overview auth/scope | `pass` for adapter/local/loopback tiers. Native product install/session tier is `not_run`; Codex and Claude executables were found but product versions were not run or verified. OpenCode is not installed. |
| P4-R5-02 hook replay and semantic checkpoint boundary | `pass` for Hook fixtures and actual Host service operation. Checkpoint creation requires an actual stored decision/run/review/publication boundary; session idle is rejected. Native product hook event is `not_run`. |
| P4-R5-03 secret and private metadata boundary | `pass` for synthetic prompt/transcript/path sentinels in adapter/Host tests. No actual product transcript or user profile was opened. |
| P4-R6-01 Host auth/CAS/local parity/migration | `pass` for storage service fixtures, schema-5 SQLite, real loopback HTTPS, current source/work revision checks, schema-4 restore, unresolved-journal protection, Hosted R2 metadata/detail and Hosted R3 F1/F3 typed apply/replay. Actual external Host/device and native product recovery are `not_run`. |
| P4-R6-02 new-session model quality | `not_evaluated`. Fixture and separate rubric are prepared in `p4-r6-model-fixture.json` and `p4-r6-model-rubric.json`. Root reported native independent-model execution was blocked by local capability/auto-review and no model output is treated as evidence. |
| P4-R6-03 comparison/efficiency measurement | `not_run`; there is no matching before/after product/model benchmark or provider token usage. |
| Final 0.4.0 packages and install/update/reinstall | `not_run` pending Root's final source freeze and package smoke. Current packaged snapshots remain historical 0.3.0 evidence. |

The standalone product capability observations and official vendor documentation links are in [r5-product-capabilities.md](r5-product-capabilities.md). No direct model API, Claude production call, external Host, Linux product install or user-primary data was used. No commit or push was made.
