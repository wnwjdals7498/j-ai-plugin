# Legacy phase1/phase2 regression

The explicit legacy test selection completed against checkout `4f632da7065bd025d1b5053ef8d43cfe9774709f` with the F0 working tree changes present. The exact files, Python/pytest versions, start/end times, exit code, each test result, source SHA-256 values before/after, and dirty status are in `result.json`; raw output and JUnit XML are adjacent. The run used `.pmt-test/p3-legacy-regression` as its isolated pytest temp root.

Command: `.venv/Scripts/python.exe -m pytest -q -ra --tb=short --basetemp .pmt-test/p3-legacy-regression --junitxml docs/phase3/evidence/2026-10-02/legacy-regression/junit.xml` followed by the 25 explicit legacy test paths recorded in `result.json`. Exit code: 1. Result: 233 tests, 215 passed, 11 failed, 5 setup errors, 2 skipped.

The two skips are the existing Windows symlink privilege limits in `tests/test_resources.py`. The failure/error details are preserved per test in the manifest. They include old assertions that schema migration remains at version 3 while the current migration writes version 4; package tests rejected by the package builder's project version/schema metadata check; and a nested-project reconciliation test whose temporary Git object write was denied. These are recorded as observations only; no test or production source was edited for this run.

The product-boundary tests use their test fixtures. This run initiated no external model calls and does not establish a native GPT subagent execution or installed-product lifecycle result. The run excludes all phase3 parallel implementation and test files, including graph/results work.
