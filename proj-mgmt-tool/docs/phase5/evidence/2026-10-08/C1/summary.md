# C1 server admin evidence

- Work item: C1; baseline commit: `938cc4e33617bccf77ee94d9005436f32c3a7476`.
- C1-owned source: `src/pmt/server_admin/{cli.py,config.py,init.py,secrets.py}` and `tests/server_admin/test_server_admin.py`.
- Shared checkout has parallel main/client changes; C1 did not edit them. This C1 run started against the baseline above.
- All command state used `C:\PMT\work\phase5-test\C1`; `PMT_HOST_CONFIG_ROOT` pointed inside that isolated root. No ProgramData/default init apply, live Host, service, firewall, operating DB/key, private TLS key, or actual device was read or changed.

## Final verification

From `proj-mgmt-tool`, focused command:

```powershell
$testRoot='C:\PMT\work\phase5-test\C1'
$env:PMT_HOST_CONFIG_ROOT=Join-Path $testRoot 'env-config'
$env:PYTHONPATH=(Resolve-Path 'src').Path
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m pytest -q tests/server_admin/test_server_admin.py tests/test_phase5_host_common.py::test_server_entry_point_version_is_importable_and_preserves_core_contract --basetemp (Join-Path $testRoot 'pytest-C1-a319268e85b74f58afb3cd81bdb6b5ad') --junitxml (Join-Path $testRoot 'pytest-C1-a319268e85b74f58afb3cd81bdb6b5ad.xml')
```

Exit 0: **25 passed, 1 skipped** in 5.43s. Skip: actual POSIX permission test on Windows. Native Windows LocalMachine DPAPI round-trip and widened-DACL rejection passed. Output/JUnit: `pytest-final.txt`, `pytest-final.xml`.

Syntax check `python -m compileall -q src/pmt/server_admin tests/server_admin`: exit 0.

## Test mapping

- T-S01-1: existing phase5 version assertion checks release/Core/schema/protocol/Host schema values.
- T-S01-2: site-free subprocess receives `host_dependency_missing`, exit 5.
- T-S02-1/3: strict schema rejects unknown fields, bool-as-int, unhashable types, malformed IP/CIDR/proxy, path variable/tilde, secret-shaped values, duplicate JSON keys, and over-1MiB config.
- T-S02-2: independent spawned config writers use one initial digest; exactly one wins, loser conflicts, revision is 2.
- T-S03-1/2: init default dry-run leaves paths absent; apply creates config/key/profile/Host DB; repeat refuses.
- T-S03-3: corrupt preexisting partial DB preserved byte-for-byte; final config publish failure rolls back only created files/empty dirs; two concurrent init processes yield one winner.
- T-S03-4: synthetic isolated legacy Host DB/profile/device fixture; adopt publishes declarative config while preserving DB/profile bytes, namespace, and device listing. No operating state read.
- T-S04-4: native Windows DPAPI encrypted-file roundtrip and widened DACL rejection; POSIX widened-mode rejection is covered by stat fixture, not claimed as native Linux verification.
- Secret lifecycle: rotation creates a new key while retaining old reference; migration retains identical bytes and key ID and preserves source file; retirement refuses active lease and then drops only the reference; failed key creation after secrets-directory creation removes only the new empty directory and permits retry; failed config publication removes only the newly-created key.

## Server-admin helper interface for C2/E2

- `init_host(args, config_root) -> result dict`; `_adopt_metadata(data_root, db_config_root) -> metadata dict`; `_base_config(args, source) -> config dict`.
- `validate_config(config) -> config`; `load_config(path) -> config`; `load_config_snapshot(path) -> (config, sha256)`.
- `publish_config(root, config, expected_sha256=None, *, create=False) -> path`; `_process_lock(lock_path)` context manager; `_publish_config_locked(...)` for callers already holding that lock.
- `validate_service_account(account)` resolves the configured run identity; `store_key(value, path, kind, *, account=None) -> source ref`; `read_key(source, *, account=None) -> bytes`; `create_key_reference(root, key_id, *, kind=None, account=None) -> source ref`; `check_source(source, *, account=None) -> status`.
- `pmt-server` CLI `config show|validate|set`, `init [--apply|--adopt]`, and `secret check|init-claim-key|rotate-claim-key|retire-claim-key|migrate-claim-key`. Config and all secret mutations use the shared process lock; init owns it around preflight, artifact creation, and final publish.

## Remaining evidence gaps

- F1: actual service-account service-run test remains for the later authorized F1 phase; no service/account registration was attempted.
- F2: real Linux ownership/mode verification remains; Windows host only.
- C2 not started; waiting for main follow-up.

## External source references used for ACL handling

- Microsoft [CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata), `dwFlags` (L68–78), Remarks (L84–91): LocalMachine ciphertext can be decrypted by any same-machine user; restrictive ACL is essential.
- Microsoft [icacls](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls), parameters (L59–74), Remarks (L75–87): removing inherited ACEs does not remove unexpected explicit grants; DACL is inspected and widened ACL is rejected.
