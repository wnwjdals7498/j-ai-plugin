# B1 client setup evidence

Date: 2026-10-08. Scope: B1 only. Baseline commit: `938cc4e` (A1); working tree was already dirty with main-owned Phase 5 progress/Graphify changes and server-executor changes. This evidence covers only the B1-owned source and `tests/client_setup` paths. Python: `3.14.5rc1` from `C:\PMT\src\venv-dev`.

## Validation

Exact final command (PowerShell):

```powershell
C:\PMT\src\venv-dev\Scripts\python.exe -m pytest proj-mgmt-tool/tests/client_setup/test_b1_client_setup.py --basetemp=C:\PMT\work\phase5-test\B1-originfinal --junitxml=proj-mgmt-tool/docs/phase5/evidence/2026-10-08/B1/junit.xml -q *> proj-mgmt-tool/docs/phase5/evidence/2026-10-08/B1/pytest-output.txt
```

Exit: 0. Result: **31 passed, 2 skipped**. JUnit and captured pytest output are copied into this B1 evidence folder as `junit.xml` and `pytest-output.txt`; isolated test data is under `C:\PMT\work\phase5-test\B1-originfinal`.

The suite covers T-C01-1 (empty local profile, Core setup metadata, and SQLite creation), T-C01-2 (hosted settings missing never creates a local DB), T-C01-3 (local profile stays local when Host settings appear), and T-C01-4 (changed hosted settings use the current hash, preserve workspace mappings, and keep the old profile after a rejected probe). It also covers prior credential preservation after rejected connection and credential-only probes, unchanged-profile no-republish with changed-token authentication, compare-guarded rollback preserving a later writer, sanitized malformed handoff handling in SessionStart, credential restoration on legacy hosted hooks, Codex hosted-profile use without product options, actual Core parser dispatch for Claude and Codex product IDs, incomplete hook arguments, empty explicit ConfigRoot local initialization through the actual Hook, managed Claude hosted settings removal blocking the actual Hook, legacy hosted Hook credential loading through Core dispatch, same-root product sessions preserving origin metadata and harmless metadata extensions, T-C04-3 (credential excluded from hook environment file), native Windows DPAPI roundtrip/corrupt ciphertext rejection/atomic replacement, platform-independent POSIX mode and owner policy using stat doubles, Windows reparse-point detection, T-C05-1/2 (Windows defaults, explicit roots, legacy ConfigRoot and existing legacy DataRoot selection), handoff parsing via `pmt.handoff.load_handoff`, and hook event preservation. Local initialization messaging is tested through the implementation's returned mode.

Syntax check:

```powershell
C:\PMT\src\venv-dev\Scripts\python.exe -m compileall -q proj-mgmt-tool/src/pmt/client_setup proj-mgmt-tool/src/pmt/easy_setup.py proj-mgmt-tool/src/pmt/easy_hook.py proj-mgmt-tool/tests/client_setup/test_b1_client_setup.py
```

Exit: 0. The final run repeated this syntax check after pytest; exit 0.

## Gaps and separate compatibility run

The real POSIX permission and symlink filesystem tests were skipped because this execution host is Windows. Platform-independent mode/owner policy and reparse-point metadata tests passed. DPAPI was exercised natively on Windows, but cross-account rejection in T-C04-2 was not run; corrupt ciphertext rejection was tested instead. No actual Host network probe or handoff import transaction was represented as complete; D2/C-03 owns that transaction.

An additional run of existing `tests/test_easy_setup.py` was not green: 6 failed, 2 passed. Exact failures: `test_missing_options_change_nothing` expects no local initialization when Host options are absent; `test_first_run_publishes_profile_and_keeps_credential_out_of_process_env` expects the former `~/.config/pmt` path on Windows; `test_changed_option_republishes_with_current_hash_and_keeps_mappings` and `test_linked_checkout_exports_scope_and_unlinked_branch_does_not` hard-code that former path; `test_local_profile_is_never_replaced` expects newly supplied Host settings to reject an existing local profile instead of preserving local mode with a switch warning; `test_env_file_lines_are_shell_quoted` invokes Bash with a Windows drive path and failed to source the fixture. The first five assertions reflect superseded behavior/path assumptions. The Bash path failure remains a platform test limitation for B3/F3. Existing tests outside `tests/client_setup` were left untouched.
