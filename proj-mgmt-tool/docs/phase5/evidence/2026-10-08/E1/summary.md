# E1 plan/apply, service, and firewall evidence

E1 starting tree: `a2fd13d6e56f729da55a34dcbe0d594db1db2976`; final tested tree: `81d410ae4d286d2d3c9870a578f8c967364fcf58`. Main integration advanced the shared base during this lane. No E1 commit created.

## Owned implementation

- `src/pmt/server_admin/operations.py`: strict-config plan/apply, selected component filtering, directory/ACL drift plans, TLS and claim-key preflight, dry-run default, explicit `--apply`, admin gate, config hash recheck, sequential stop-on-first-failure, and post-apply verification.
- `src/pmt/server_admin/service.py`: root-TaskPath PMT Host Task Scheduler and systemd unit generation; SID-based Windows principal matching; service lifecycle helpers; optional named daily backup task/timer generation with deterministic artifact hashes; stable systemd credential aliases.
- `src/pmt/server_admin/firewall.py`: named Windows PMT rule and source-limited firewalld/UFW command plans; Public profile only with explicit `--allow-public`; unowned same-port firewalld rules block apply.
- `tests/server_admin/test_e1_operations.py`: 21 isolated command-generation and fake-adapter tests, including concurrent config-CAS lock cases for both apply entrypoints.

CLI integration contract for main-owned `cli.py`:

- `operations.add_operations_commands(command_factory)` and `operations.run_operations_command(args, config_root, adapter=None)`.
- `service.add_service_commands(command_factory)` and `service.run_service_command(args, config_root, adapter=...)`.
- The service parser exposes `service install --with-backup-timer`; without the flag an existing backup definition is not inspected, replaced, or removed. `service remove` cleans only the PMT-named companion artifact when it exists.
- Reuse one `operations.NativeOperationsAdapter()` for default read-only snapshots, admin checks, and explicit apply. Tests inject a recording fake.
- `service.credential_source_map(config, config_root) -> dict[str, str]` is pure. Alias mapping is `claim-<key_id>` for primary and retained file keys, and `tls-key` for the TLS private key. Absolute paths map to themselves; `${CREDENTIALS_DIRECTORY}/claim-<id>` falls back only to `HostConfigRoot/secrets/claim-<id>.key`; `${CREDENTIALS_DIRECTORY}/tls-key` falls back only to `HostConfigRoot/tls/tls-key`. The helper does not search files or mutate config. Systemd env/DPAPI references are rejected.

## Verification

Command:

```text
C:\PMT\src\venv-dev\Scripts\python.exe -m pytest -q tests/server_admin/test_e1_operations.py --junitxml C:\PMT\work\phase5-test\E1\pytest-e1-final.xml --basetemp C:\PMT\work\phase5-test\E1\pytest-e1-postintegration-temp
```

Result: **21 passed, exit 0**. Captured stdout and JUnit: `pytest-e1-final.txt`, `pytest-e1-final.xml`. All ConfigRoot and pytest data used temp paths. Recording adapters intercepted service and firewall commands; no actual Windows task, systemd unit, firewall rule, or Host state was changed. A separate read-only PowerShell SID lookup checked the two LocalService spellings and current-token identity.

`test_operations_plan_commands_equal_applied_commands_and_second_apply_is_noop` covers mocked T-S08-1 (second apply has zero changes) and T-S08-2 (plan command records are exactly what the fake adapter receives). `test_apply_lock_blocks_concurrent_config_cas_until_first_command_finishes` covers both operations and service apply: a separate process attempts a normal config CAS before the first planned command, waits while the shared lock is held, then commits after apply releases it. Other tests cover explicit apply/admin gates, first-failure stop, config-file immutability, exact ACL delta generation, Task Scheduler argv/settings/TaskPath/principal, backup artifact hashes and the no-flag preservation rule, credential alias mapping, firewall source rules, and explicit Public-profile gating. E1-owned whitespace scan is clean.

## Remaining validation and risks

- T-S09 actual Windows/Linux service installation/status/restart and T-S10 actual firewall reachability/idempotence were not run; they require the later F-stage environment. E1 verifies generated commands only.
- The optional timer generator emits the future `pmt-server backup --apply` action; E2 owns that CLI operation. No backup was created or scheduled.
- Custom Windows task accounts use passwordless S4U; built-in SYSTEM/LocalService/NetworkService accounts use `ServiceAccount`. Microsoft documents that S4U does not provide network or EFS-encrypted-file access, so the real service account and all configured paths need F1 validation. References: [New-ScheduledTaskPrincipal](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal?view=windowsserver2025-ps), [Principal.LogonType](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype).
- Firewalld rich rules do not carry a reliable per-rule owner label. If an existing, non-matching TCP accept rule uses the configured port, plan blocks and leaves it untouched for manual review; UFW uses the exact `PMT Host <port>` comment, and Windows manages only the exact display name.
- Main still owns CLI integration and the shared runtime resolver for systemd credential aliases. Persisted config is unchanged by E1; no C2 shared module was edited.
- Graph refresh was left to main integration while parallel writers remain active.

## Main integration closure

Main wired actual plan/apply/service CLI parsers and dispatch; failed results now return failure exit. Added in-memory systemd delivered-key resolver to serve/doctor/secret-check/TLS-check, initialized ConfigRoot read/traverse preflight, and bounded rotated logs --since filtering. Persisted config is unchanged by resolver.

Parent pre-review66pass/1skip stdout/JUnit preserved as parent-pre-review files; later review found3P2 and workerfixedTaskPath/SID/configlock. Parent fresh full tests/server_admin on final E1/main wiring:85pass/2skip, exit0,40.26s. Exact command python -m pytest tests/server_admin --basetemp C:/PMT/work/phase5-test/E1-parent-final/pytest --junitxml C:/PMT/work/phase5-test/E1-parent-final/junit.xml -q. SkipsareexplicitD1persistent-sample generator andPOSIXmode. Independent E1 closure APPROVE with21focusedpass. New main logs/CLI narrow13pass; runtime13cases includedparent.

Official systemd reference: https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.exec.xml Credentials LoadCredential lines3450-3460/3522-3532 and CREDENTIALS_DIRECTORY lines3808-3814. Global preinstalled Python/PyYAML was used for the server skill validator after development venv lacked yaml; this adds no dependency to PMT.

Automatic approval review rejected removal of doctor._health port8765 protection. That guard remains unchanged; its product behavior is a documented F1 approval dependency. No operating task/rule/restart or production port probe was performed.
