# B2 client command evidence

Date: 2026-10-08. Scope: B2 plus the parent-directed B1 Hook cache fix in `easy_hook.py`. Baseline commit: `06e18ec`. Python: `3.14.5rc1` from `C:\PMT\src\venv-dev`. Shared worktree also contained main/server/B3-owned edits; none were reverted or edited by B2.

## Final validation

Exact PowerShell command:

```powershell
C:\PMT\src\venv-dev\Scripts\python.exe -m pytest proj-mgmt-tool/tests/client_setup/test_b1_client_setup.py proj-mgmt-tool/tests/client_setup/test_b2_local_commands.py proj-mgmt-tool/tests/client_setup/test_b2_cached_hook.py --basetemp=C:\PMT\work\phase5-test\B2-last --junitxml=proj-mgmt-tool/docs/phase5/evidence/2026-10-08/B2/junit.xml -q *> proj-mgmt-tool/docs/phase5/evidence/2026-10-08/B2/pytest-output.txt
```

Exit: 0. Result: **60 passed, 2 skipped**. JUnit reports 62 tests, zero failures/errors, and two POSIX-only skips. The stdout file and JUnit XML are saved beside this README. Isolated Git repositories, ConfigRoots, DataRoots, SQLite files, and subprocess command executions are under `C:\PMT\work\phase5-test\B2-last`.

Syntax check (exit 0):

```powershell
C:\PMT\src\venv-dev\Scripts\python.exe -m compileall -q proj-mgmt-tool/src/pmt/easy_cli.py proj-mgmt-tool/src/pmt/easy_hook.py proj-mgmt-tool/src/pmt/client_setup proj-mgmt-tool/tests/client_setup/test_b1_client_setup.py proj-mgmt-tool/tests/client_setup/test_b2_local_commands.py proj-mgmt-tool/tests/client_setup/test_b2_cached_hook.py
```

## Coverage and limits

T-C06-1 creates local environment → repository → project scopes through Core operations, links the branch, and saves project metadata. T-C06-3 verifies a second branch reuses the same repository/project IDs. Tests also cover project-name linking, unlink, project listing, readonly mode reporting on empty/local/hosted profiles, and a corrupt/wrong-typed project registry that remains byte-identical and creates no scopes. A first mutating link command, not `pmt mode`, performs empty-root local setup.

T-C07 local flows add, start, pause, and complete an Item. Completion captures a real before-fingerprint, launches the actual quoted Windows Python command without a shell, stores actual stdout as a Core resource, records verification using the real exit code, and finishes with the claim token and verification ID. Exit 0 completes; exit 1 leaves the Item In Progress with its claim; an untracked dirty file blocks launch and completion. Command quoting was checked with `CommandLineToArgvW`, including a Python executable under a path with spaces. Corrupt claims remain byte-identical and prevent Core claim mutation. Two independent CLI processes starting different Items preserve both tokens in `easy-claims.json`. A hosted start regression verifies the existing `claim_ref` file and remote operation path remain active without creating a local database. A simulated ambiguous hosted request failure verifies result lookup and replay use the identical request ID and request body.

T-C08 local `mode` is read-only; local `check` performs setup/read, writes and reads one diagnostic fact, and replays the same request ID to the same record. Hosted check loads the protected credential, probes mocked compatibility, and replays the same fact. Offline hosted check returns failure and creates no local database.

The parent-directed Hook cache fix is also covered: only SessionStart performs managed setup; later Claude/Codex events use the cached local or hosted profile and protected credential, and fail with a product-native warning if no cache exists. Unsupported and malformed native events reach Core validation without setup writes. `--replay-pending` and `--read-context` continue through Core without requiring an event. An explicit, unmarked legacy ConfigRoot with a valid Core profile and initialized schema-5 DB uses a read-only synthetic local cache; default roots and managed markers do not receive this treatment.

C02 partial Host settings return `handoff_invalid` with only missing key names. The Hook shows those names while leaving fresh ConfigRoot/DataRoot and credential state untouched. An existing local profile remains local and keeps its profile bytes when partial Host settings appear; an existing hosted profile with removed settings still fails as `hosted_settings_missing`.

Two shared obstruction tests were also run and saved separately. They verify a known legacy DB reaches Core and retains the pending-write warning, while an uninitialized blocked root reports `not_configured` and preserves the obstructing path:

```powershell
C:\PMT\src\venv-dev\Scripts\python.exe -m pytest proj-mgmt-tool/tests/test_hook_acceptance.py::test_hook_03_pending_directory_write_failure_never_calls_core_or_claims_storage proj-mgmt-tool/tests/test_hook_acceptance.py::test_hook_03_database_and_pending_root_failure_is_visible_without_false_save --basetemp=C:\PMT\work\phase5-test\B2-shared-final --junitxml=proj-mgmt-tool/docs/phase5/evidence/2026-10-08/B2/shared-hook-junit.xml -q *> proj-mgmt-tool/docs/phase5/evidence/2026-10-08/B2/shared-hook-output.txt
```

Exit: 0. Result: **2 passed**; captured output and JUnit are `shared-hook-output.txt` and `shared-hook-junit.xml`.

The two skips are real POSIX filesystem permission/symlink tests on this Windows host; platform-independent POSIX metadata tests and native Windows DPAPI tests passed in the included B1 suite. Hosted service behavior was tested through a controlled store facade; no live Host, certificate endpoint, or user database was used. The separately owned `test_b3_entrypoints.py` was excluded from this B1+B2 run.
