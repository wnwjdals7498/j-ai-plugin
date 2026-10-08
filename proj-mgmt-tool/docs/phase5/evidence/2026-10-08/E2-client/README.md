# E2 Client C09 evidence

Date: 2026-10-08. Source base: `91309454ae2980d23359fa4bef8cd110f44eb789` (parent integration commit); no client commit made. Python `3.14.5rc1` from `C:\PMT\src\venv-dev\Scripts\python.exe`. This lane owns `src/pmt/client_setup/switch.py`, narrow `src/pmt/client_setup/connect.py` support, `src/pmt/easy_cli.py`, `tests/client_setup/test_e2_switch.py`, and this evidence folder. No Core, storage_config, migration, Host/server, schema, manifest, or pyproject source edits.

## Fresh verification

Focused C09 command (exit 0; output/JUnit included):

```powershell
$env:PYTHONPATH='src'; $env:PMT_CONFIG_ROOT='C:\PMT\work\phase5-test\E2\unit-final'; $env:PMT_DATA_ROOT='C:\PMT\work\phase5-test\E2\unit-final-data'; $env:PMT_HOST_CREDENTIAL=''; $env:PMT_SCOPE_ID=''; C:\PMT\src\venv-dev\Scripts\python.exe -m pytest -q --tb=short tests/client_setup/test_e2_switch.py --basetemp C:\PMT\work\phase5-test\E2\unit-review5 --junitxml C:\PMT\work\phase5-test\E2\unit-review5-junit.xml
```

Result: **19 passed**. Covers local claim/sidecar/pending rejection, all-session Host outboxes, corrupt state, default-connect local refusal, mapping preservation/filtering, clone/export integrity, DB write-intent guard, probe/CAS rollback, live/offline hosted checks, all-granted-scope scanning (including hidden scope B), wildcard/stale-scope fail-closed handling, metadata rollback after a CAS conflict, foreign concurrent metadata writer preservation, and CLI surface.

Broader client + Hook regression (exit 0; output/JUnit included):

```powershell
$env:PYTHONPATH='src'; $env:PMT_CONFIG_ROOT='C:\PMT\work\phase5-test\E2\broad-review-final\config'; $env:PMT_DATA_ROOT='C:\PMT\work\phase5-test\E2\broad-review-final\data'; $env:PMT_HOST_CREDENTIAL=$null; $env:PMT_SCOPE_ID=$null; C:\PMT\src\venv-dev\Scripts\python.exe -m pytest -q --tb=short tests/client_setup tests/test_easy_setup.py tests/test_hooks.py tests/test_hook_acceptance.py --basetemp C:\PMT\work\phase5-test\E2\broad-review-final\pytest --junitxml C:\PMT\work\phase5-test\E2\broad-review-final-junit.xml
```

Result: **148 passed, 2 skipped** in 54.15s. Skips are POSIX permission and symlink cases requiring a POSIX filesystem. Fresh `compileall` and `git diff --check` both exited 0.

## Real TLS verification

A synthetic test-only device was issued on the isolated D1 Host at `C:\PMT\work\phase5-test\D1-handoff-sample\config`; its credential and handoff remain in the external E2 test root, never Git evidence. The test Host listened only on `127.0.0.1:18765` and is stopped; final listener count is zero.

- Local-to-hosted with clone export succeeded; the client printed only public connection information and CA SHA-256, and the local DB SHA-256 stayed unchanged.
- Hosted-to-local succeeded after online probe and read-only record scan; local DB and protected credential remained present.
- Rehosting and stopping the test Host made hosted-to-local fail closed (`switch_state_unverifiable`) with profile and DB unchanged.
- Final fresh scope-metadata round trip ran `pmt connect` on the existing isolated hosted profile, saved one authenticated scope in `client.json`, then switched to local with the live Host scope check. Both commands exited 0, mode ended local, and port 18765 was free after shutdown.

Safe command outputs are `real-tls-output.txt`, `scope-connect-output.txt`, and `scope-local-output.txt`. No secret bytes were read into evidence or printed. The export ZIP stays outside the repository.

## Behavior and limits

`pmt storage switch --to hosted --handoff <file> [--credential-file <file> | --credential-stdin] [--export <bundle.zip>]` and `pmt storage switch --to local` are supported. Normal `pmt connect` still rejects a local profile; only the explicit switch uses the internal local-to-hosted path. Before transitions, the client validates local claims, DB quiescence, Hook pending files, and every session's Host pending SQLite outbox read-only. It holds a SQLite `BEGIN IMMEDIATE` transaction with no business SQL writes during clone backup and Host probe/CAS. SQLite copies and migration preparation run against an isolated clone; original resource copies are hash-checked; ZIP publication is staged, manifest-verified, and no-overwrite.

Hosted-to-local requires a live authenticated Host check. It scans all canonical authenticated device scopes, including scopes not represented by a project mapping; it refuses active work, stale saved grants, wildcard/unknown state, or unavailable Host. It does not fall back to the local DB for hosted state. Same-root Claude/Codex retain one device identity; saved origin and scope metadata remain non-secret and device/namespace-bound. No Host import, production Host, or user database was used. Graphify refresh remains parent-owned during integration.

Parent final Python3.13.13 targeted C09+D2:39pass/0fail/0error,8.09s, exit0, externalbasetemp C:/PMT/work/phase5-test/E2-client-final-313/pytest. Independent E2 client closure APPROVE;19focusedpass/compile0, both bounded failure repros fixed. Original306pass/4skip dual-Python integration is retained as before final client review; final scoped148pass/2skip and39stable3.13 evidence covers the later fixes.
