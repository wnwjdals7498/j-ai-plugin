# F15 isolated local acceptance

The repaired Core 0.3.0/schema 4 package snapshot is recorded in `repaired-source/f15-package-snapshot.json`. Three product bundles passed manifest/hash checks and unpacked portable CLI smoke tests in isolated working directories. This did not install into a user's environment.

The six-group acceptance run is preserved at `repaired-source/f15-local-acceptance.json`: all six groups exited 0, but the aggregate itself exited 1 and is marked inconclusive because it detected whitespace-only edits to `tests/test_phase3_batch.py` and `tests/test_phase3_host_network.py` during execution. The independent proof `repaired-source/test-whitespace-proof.json` confirms the changed lines, matching start/end hashes, and equal ASTs. After those edits, the affected F9/Host batch/network tests were rerun against current source: 21 passed, exit 0. The separate `repaired-source/f15-reconciled-local-acceptance.json` records that evidence without rewriting the earlier aggregate.

This is local-tier acceptance only. Native product installation, remote Host, public/Linux deployment, external model calls, and full model quality/cost measurement remain not_run or unknown. The single synthetic context projection is reported separately and is not a general model-quality result.
