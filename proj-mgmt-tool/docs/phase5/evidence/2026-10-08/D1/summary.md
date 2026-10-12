# D1 project, device, and handoff evidence

- Work item: D1; source baseline: `a2fd13d6e56f729da55a34dcbe0d594db1db2976`.
- D1-owned source: `src/pmt/server_admin/{cli.py,registry.py,devices.py}` and `tests/server_admin/test_d1_registry.py`. The shared checkout also contains main-owned C2/E1/runtime-path changes; D1 did not edit their modules.
- Project creation uses the existing Host API over TLS with an ephemeral `*`/write bootstrap device and the official `create_scope` operation for environment → repository → project. The temporary device is revoked in `finally`; bootstrap revoke failure reports its nonsecret device ID for manual cleanup.
- Repository registry entries use the real project parent repository scope UUID read from the existing DB. A different repo mapping for one project is rejected with a separate-project instruction.
- Device mutations delegate to `AuthRegistry` with expected revisions. `admin` or `*` requires `--allow-admin`; wildcard devices cannot be handed off. Credentials are written only to no-overwrite protected `--credential-out` files in tests; test code reads them into process memory without printing or checking them into Git.
- Handoffs are built by `pmt.handoff.build_handoff`, carry `PMT_HOST_CREDENTIAL` as a separate-delivery reference, and optionally include only the configured public CA with the PEM-bytes SHA-256.

## Integrated verification

From `proj-mgmt-tool`:

```powershell
$testRoot='C:\PMT\work\phase5-test\D1'
$env:PMT_HOST_CONFIG_ROOT=Join-Path $testRoot 'env-config'
$env:PYTHONPATH=(Resolve-Path 'src').Path
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m pytest -q tests/server_admin/test_d1_registry.py tests/server_admin/test_c2_server.py tests/server_admin/test_server_admin.py tests/test_phase5_host_common.py::test_server_entry_point_version_is_importable_and_preserves_core_contract tests/test_phase5_registry_contract.py --basetemp (Join-Path $testRoot 'pytest-D1-final-66962303fbd341d9868adfe61926db85') --junitxml (Join-Path $testRoot 'pytest-D1-final-66962303fbd341d9868adfe61926db85.xml')
```

Exit 0: **41 passed, 2 skipped** in 40.48s. Skips: persistent D2 sample generator (run separately below) and C1 POSIX permission check on Windows. `python -m compileall -q src/pmt/server_admin tests/server_admin`: exit 0. Output/JUnit: `pytest-integrated.txt`, `pytest-integrated.xml`.

Acceptance coverage: T-S11-1 verifies the live isolated TLS scope chain and revoked bootstrap; T-S11-2 injects bootstrap revoke failure and verifies a manual-revoke device ID; T-S12-1 checks issued-device HTTPS compatibility; T-S12-2 verifies revoked credentials are rejected; T-S12-3 verifies rotation rejects old and accepts new credential; T-S12-4 verifies an out-of-scope request gets `scope_forbidden`; T-S13-1 validates a credential-free handoff with public CA digest. Standalone `handoff create`, safe `device list`, dry-run non-mutation, wildcard/admin gating, repo-parent mapping and second-repo rejection are also covered.

## Persistent D2 sample

A separate explicit test passed **1/1** and left a stopped, synthetic Host sample outside Git:

- HostConfigRoot: `C:\PMT\work\phase5-test\D1-handoff-sample\config`
- DataRoot: `C:\PMT\work\phase5-test\D1-handoff-sample\data`
- URL: `https://127.0.0.1:18765` (server stopped; port is free)
- Project: `d2-sample`; handoff: `C:\PMT\work\phase5-test\D1-handoff-sample\handoff.json`
- Protected credential file: `C:\PMT\work\phase5-test\D1\credentials\d2-sample.credential`; Windows ACL was checked without reading the file value.

Start the sample when needed with:

```powershell
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m pmt.server_admin --config-root 'C:\PMT\work\phase5-test\D1-handoff-sample\config' serve
```

The test stops the server after material creation. Sample test stdout/JUnit are `pytest-sample.txt` and `pytest-sample.xml`. No credential value appears in evidence.

## Handoff interfaces

- `project_add(config_root, name, *, title=None, apply=False) -> result dict`
- `project_repo_add(config_root, project_name, repository_name, remote, graph_path, *, apply=False) -> result dict`; resolves actual parent repository ID locally, without business-table writes.
- `list_projects(config_root) -> result dict`
- `device_issue(config_root, actor, project_names, permissions=None, *, allow_admin=False, credential_out=None, handoff_out=None, include_ca=False, apply=False) -> result dict`
- `device_list(config_root)`, `device_rotate(config_root, device_id, *, credential_out=None, apply=False)`, `device_grants(config_root, device_id, project_names, permissions, *, allow_admin=False, apply=False)`, `device_revoke(config_root, device_id, *, apply=False)`.
- `handoff_create(config_root, device_id, output, *, include_ca=False, apply=False)`.

## Scope and remaining gates

- No Host/API/schema/auth/claim contract changes, business SQL writes, dependencies, production Host/service/firewall operations, or commits.
- C2/F1 service-account execution and F2 native Linux permission checks remain later gates. D2 can use the stopped sample and its protected credential path above.

Parent fresh check: tests/server_admin/test_d1_registry.py + test_c2_server.py + tests/test_phase5_registry_contract.py, external unique basetemp C:/PMT/work/phase5-test/D1-parent/pytest: 16 passed, 1 explicit sample-generator skip, exit0, 35.62s. parent-stdout.txt and parent-junit.xml retained. Independent scoped D1/runtime/plugin review: APPROVE, no P1/P2; D1 4pass/1skip and runtime12pass.
