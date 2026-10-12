# C2 server admin evidence

- Work item: C2; source baseline: `c88ab7344970939f2d7ca4e168de225aee00d94a` (C1 is committed at `c293b7f`; main/client lanes continued on the shared branch).
- C2-owned source: `src/pmt/server_admin/{cli.py,doctor.py,logging.py,serve.py,status.py,tls.py}` and `tests/server_admin/test_c2_server.py`.
- No shared Core/Host, client, packaging, service, firewall, or production files were changed by C2.
- Test roots: `C:\PMT\work\phase5-test\C2`; runtime config/data/logs/TLS keys/certs are confined to pytest's unique basetemp. Tests used loopback ports 19443–19449 and one OS-selected ephemeral port, never 8765. All certificates/keys are synthetic; `cryptography` was used only as the existing `host-test` fixture dependency. No new runtime dependencies.

## Final verification

From `proj-mgmt-tool`:

```powershell
$testRoot='C:\PMT\work\phase5-test\C2'
$env:PMT_HOST_CONFIG_ROOT=Join-Path $testRoot 'env-config'
$env:PYTHONPATH=(Resolve-Path 'src').Path
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m pytest -q tests/server_admin/test_c2_server.py tests/server_admin/test_server_admin.py tests/test_phase5_host_common.py::test_server_entry_point_version_is_importable_and_preserves_core_contract --basetemp (Join-Path $testRoot 'pytest-C2-final-918ffebde212404ba11b188afe463e54') --junitxml (Join-Path $testRoot 'pytest-C2-final-918ffebde212404ba11b188afe463e54.xml')
```

Exit 0: **35 passed, 1 skipped** in 26.81s. Skip is C1's POSIX-only permission check on Windows. `python -m compileall -q src/pmt/server_admin tests/server_admin`: exit 0. Full output/JUnit are `pytest-final.txt` and `pytest-final.xml`.

## Acceptance mapping

- T-S05-1: synthetic IP SAN mismatch rejected using `SSLContext` with hostname validation.
- T-S05-2: mismatched certificate/private key rejected; unrelated trust CA/chain rejected; near-expiry certificate returns warning.
- T-S05-3 / T-S07-1: actual isolated TLS server serves `/health`; existing `HttpStore.check_compatibility()` authenticates through the registered CA file and returns the expected namespace/device.
- TLS registration: default is a deterministic dry-run; `--apply` copies cert/private key/public CA into restricted config-root storage, validates pair/chain/SAN/expiry, and config-CAS publishes. Original inputs remain unchanged. Injected publish conflict retains old config and removes only the operation's verified new files. Apply shares the config lock for snapshot through publish.
- T-S06: exactly 13 named diagnostics. Failure injection covers invalid config/schema, missing imports, incompatible DB schema, unsafe/missing paths, missing claim key, invalid TLS, occupied foreign port, and held data-root lock. Service/firewall integration remains a warning during this offline phase; missing diagnostic credentials yield `compat_unchecked` warning. The development `service.kind=none` venv case is reported as warning, not production-ready.
- T-S07-2: full-lifetime OS file lock rejects a concurrent second server before DB construction; live doctor distinguishes the responding configured Host from a foreign listener. Missing account/key preflight fails before opening the database. No-TLS without explicit loopback mode returns `host_tls_required` before DB construction; explicit loopback HTTP and trusted-loopback-proxy modes retain their supported behavior.
- Status: local metadata/counts are read through SQLite `mode=ro`; before/after DB and profile bytes match. While the isolated server runs it reports running state and PID/start time; offline compatibility is explicitly `not_checked`.
- S16: timed rotating JSON handler honors configured retention; bounded tail redacts bearer/header/token and quoted JSON credential/private-key values. Live isolated-server log does not contain its credential.
- C1 regression: the focused C1 config/init/secret suite and raw version contract ran in the same final command.

## Server-admin interfaces for D1/E1/E2

- `register_tls(config_root, cert_source, key_source, ca_source=None, *, apply=False) -> result dict`; `check_tls(config) -> result dict`. Registration uses stable content-addressed destination paths; source private keys are never removed.
- `run_doctor(config_root) -> {ok, checks}` with 13 named read-only checks; CLI exits 1 only when one or more checks fail.
- `serve_host(config_root, *, listen=None, allow_loopback_http=False)` performs config/path/run-account/key/TLS preflight, acquires `data_root/.pmt-server.lock`, opens the existing `Database`, then delegates to `host.cli.serve_host(db,args,claim_keys=<decoded keyring>,log_config=<timed JSON config>)`. The OS lock remains held until the legacy single-worker server returns.
- `read_status(config_root) -> metadata/counts/health result`, using read-only SQLite and the server lock marker.
- `server_log_config(log_dir, retain_days=30, level='info') -> uvicorn log config`; `read_logs(log_dir, tail=200) -> redacted line list`.

## Remaining gaps

- F1 actual configured service-account execution remains for the later authorized phase. `serve` now rejects a process whose resolved account differs from `service.account`; doctor warns if current-process access cannot prove configured-account access.
- F2 native Linux owner/mode verification remains; the active host is Windows. Firewall/service registration and SELinux AVC checks were not performed. C2 made no such changes or live queries.
- D1/E1/E2 are not started; wait for main follow-up.
