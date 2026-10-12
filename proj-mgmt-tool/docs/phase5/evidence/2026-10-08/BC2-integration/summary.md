# B2/C2 parent integration
Date 2026-10-08; source baseline c88ab73 with final B2/C2 and main-owned Phase5 contract tests; Windows, Python 3.14.5rc1, development venv only.
Command from proj-mgmt-tool:
python -m pytest tests/client_setup tests/server_admin tests/test_easy_setup.py tests/test_hook_acceptance.py tests/test_hooks.py tests/test_phase5_host_common.py tests/test_phase5_handoff.py tests/test_phase5_registry_contract.py --basetemp C:/PMT/work/phase5-test/BC2-final/pytest-verified --junitxml C:/PMT/work/phase5-test/BC2-final/junit.xml -q
Explicit PMT_CONFIG_ROOT, PMT_DATA_ROOT, APPDATA and LOCALAPPDATA were all below C:/PMT/work/phase5-test/BC2-final.
Exit 0: 194 passed, 3 POSIX-only skipped, 89.97s. Actual raw stdout/JUnit retained.
Before this run, an initial harness named a nonexistent test_hook_runtime.py: pytest exit4, no tests ran. Corrected to the existing test_hooks.py.
Independent closure review: no P1/P2; focused39 passed and compile checks passed. B2 read-only mode and C2 no-TLS preflight regressions resolved.
Legacy setup assertions now follow authorized C01/C02/C04/C05 behavior while retaining no-mutation, shell quoting, and no-credential output checks. Actual Git Bash executable selected on Windows; PATH bash was an unrelated wrapper.
This is isolated integration, not live production/F1 or actual product sessions/F2/F3.
