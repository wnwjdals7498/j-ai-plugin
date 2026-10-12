# D2 client connect evidence

Date: 2026-10-08. Scope: D2 client setup/connect only. Base revision at final verification: `e4bb21ac061a8d384b06401a3a5583cd4ff57476`; D2 source/test paths are local dirty changes in the shared worktree. Python: `3.14.5rc1` from `C:\PMT\src\venv-dev`. Shared worktree also contained parent D1/E1/server and B3 work; this executor changed only `src/pmt/easy_cli.py`, `src/pmt/client_setup/connect.py`, `src/pmt/client_setup/client.py`, `src/pmt/client_setup/local_commands.py`, `src/pmt/client_setup/mode.py`, `tests/client_setup/test_d2_connect.py`, the one-line fixture cleanup in `tests/client_setup/test_b2_local_commands.py`, and this D2 evidence folder.

## Validation

Exact final PowerShell command (exit 0):

```powershell
$env:PYTHONPATH='src'; & C:\PMT\src\venv-dev\Scripts\python.exe -m pytest -q tests/client_setup/test_d2_connect.py tests/client_setup/test_b2_local_commands.py tests/client_setup/test_b2_cached_hook.py tests/client_setup/test_b1_client_setup.py --basetemp C:\PMT\work\phase5-test\D2\final-final --junitxml C:\PMT\work\phase5-test\D2\final-final-junit.xml 2>&1 | Tee-Object -FilePath C:\PMT\work\phase5-test\D2\final-final-pytest.txt; $code=$LASTEXITCODE; "pytest exit=$code"; exit $code
```

Result: **80 passed, 2 skipped**. The skips require POSIX filesystem permission/symlink behavior; this Windows run is not evidence for those cases. Exact pytest output and JUnit are `pytest-output.txt` and `junit.xml`.

`compileall` on all changed Python modules/tests: exit 0. `git diff --check` on D2-owned source/test paths: no whitespace errors. Ruff was unavailable (`No module named ruff`).

## Acceptance coverage

- T-C03-1: Actual D1 handoff → protected credential → `client.json` source `connect` → project import. Real D1 loopback integration ran `pmt connect`, `pmt storage status`, `pmt storage probe`, `pmt unlink`, `pmt link d2-sample`, and `pmt check`. Named link succeeded with actual D1 project/repository IDs; check authenticated, verified CA/compatibility, and replayed the same diagnostic request to the same record. `integration-output.txt` preserves command output and exits.
- T-C03-2: Invalid CA handoff rejected as `handoff_invalid`; hashes for existing storage/profile/projects/client and protected credential stayed equal.
- T-C03-3: Bad credential reached the real Host and returned `unauthenticated`; hashes for the existing storage profile, protected credential, and CA were unchanged. Unit coverage also seeds credential A and a hosted profile, attempts B with a rejected probe, and verifies the exact previous bytes remain.
- T-C03-4: CLI dry-run validates without Host contact or writes; ConfigRoot remains absent and source credential-file hash stays unchanged.
- C03 output: `pmt connect` prints endpoint, namespace, actor, scopes, permissions, probed compatibility versions, and public CA SHA-256; tests verify the credential is absent in both normal and dry-run output. Dry-run labels compatibility as handoff validation only.
- T-C02-3: Actual `easy_hook.run` processes Claude `SessionStart` with `CLAUDE_PLUGIN_OPTION_HANDOFF_FILE` and `CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL`; it reaches the shared `connect()` transaction, imports projects, persists `source=connect`, makes the protected credential available to the Core bridge process, keeps it out of returned `state.env` and `CLAUDE_ENV_FILE`, and creates no local database. Its transport is a test facade; the separate CLI acceptance used the real isolated D1 Host.
- Additional cases cover explicit `PMT_HOST_CREDENTIAL` conflict before writes, default HTTPS-only rejection of loopback HTTP in normal/dry-run connect, no-write disconnect/status, and injected storage/CA/project writers at rollback interleavings. Rollback groups project lock → ConfigRoot lock, verifies the returned profile hash before claiming ownership, and aborts dependent cleanup if a later profile writer wins.

The isolated Host ran only at `https://127.0.0.1:18765` with `C:\PMT\work\phase5-test\D1-handoff-sample\config`; final listener and matching-process checks both returned zero. The handoff credential file was consumed by path only. A second-root experiment reused the same D1 device and correctly got `session_device_conflict`; that temporary root was removed, matching one device identity per ConfigRoot.

`negative-checks.txt` records CA, bad-credential, dry-run, HTTPS-only, and injected concurrency results. No credential bytes, claim key, TLS private key, or full environment are included in Git evidence.

## Public helper surface

- `pmt.client_setup.connect.connect(config_root, handoff_path, *, credential=None, dry_run=False, environ=None, configure=configure_storage)`
- `pmt.client_setup.connect.disconnect(config_root)`
- `pmt.client_setup.local_commands.merge_handoff_projects(config_root, entries)` returns exact before/after bytes captured under the projects lock.
- `pmt.client_setup.client.write_client_metadata_snapshot(config_root, *, source, python_path, mode)` updates under the ConfigRoot lock and returns exact before/after bytes.

`pmt easy_cli` exposes `connect`, `disconnect`, `storage status`, and `storage probe`. `storage status` is readonly and does not load a credential; `storage probe` explicitly loads one for a live compatibility/authentication check. Connect validates handoff and sidecars before writes, stages protected credential and public CA, probes/publishes through official `configure_storage` CAS, merges project metadata under its process lock, marks `client.json` origin `connect`, and compare-restores only owned files. CLI connect and managed Claude setup share a separate setup lock; it does not hold the Core ConfigRoot lock across Host probes. Default client profiles remain HTTPS-only; loopback HTTP is available only through the separate handoff validation diagnostic API, not `pmt connect`. Connect does not create a local database.

## Limits

No production Host or user database was used. Cross-account DPAPI access was not tested in this D2 run; no POSIX host was available. `pmt storage switch` remains E2. No new Host scopes were created; connect used IDs/scopes from the actual D1 handoff.



## Broader client and Hook suite

After fixing a pytest-process environment leak in the hosted-check fixture, this fresh command passed with **129 passed, 2 skipped** (exit 0):

```powershell
$env:PYTHONPATH='src'; $env:PMT_CONFIG_ROOT='C:\PMT\work\phase5-test\D2-parent-rerun\config'; $env:PMT_DATA_ROOT='C:\PMT\work\phase5-test\D2-parent-rerun\data'; $env:PMT_HOST_CREDENTIAL=$null; $env:PMT_SCOPE_ID=$null; C:\PMT\src\venv-dev\Scripts\python.exe -m pytest -q tests/client_setup tests/test_easy_setup.py tests/test_hooks.py tests/test_hook_acceptance.py --basetemp C:\PMT\work\phase5-test\D2-parent-rerun\pytest --junitxml C:\PMT\work\phase5-test\D2-parent-rerun\junit.xml
```

The earlier 119-pass/10-failure output remains at `C:\PMT\work\phase5-test\D2-parent-final\stdout.txt` and `junit.xml`; failure cause and minimal fixture fix are documented in `negative-checks.txt`. No runtime or Core credential behavior changed.

Main verification: broad JUnit has131tests/0fail/0error/2skip (129passed),49.187s. IndependentD2closure20passed/compile0, APPROVE. Previousfailedbroad and precise presence-only B2→D2/control diagnostics retained; no product credential behavior or A0 test expectations changed.
