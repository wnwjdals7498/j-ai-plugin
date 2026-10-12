# E2 server evidence

Date: 2026-10-08 (Asia/Seoul)
Baseline commit: `de5c989753fe333c4230a72e22682267bf00fff1`
Review update: added the missing `sys` import; extracted a pure candidate-install argv builder used by the native adapter; made the candidate Doctor scratch service kind explicitly `none` after in-memory path resolution. The original service configuration is unchanged. Tests never invoke the generated venv or pip commands.

## Environment and isolation

- Python: `C:\PMT\src\venv-dev\Scripts\python.exe`
- `PYTHONPATH`: resolved `src`
- Test roots: `C:\PMT\work\phase5-test\E2-server\reviewfix1` and `...\reviewfix-final`
- Synthetic Host databases, auth devices, archive fixtures, and fake service/install adapters only.
- No production pip install, task/service/firewall operation, operating Host probe, or real credential/key output.

## Commands and results

Focused E2:

```powershell
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m pytest -q tests/server_admin/test_e2_backup_upgrade.py --basetemp C:\PMT\work\phase5-test\E2-server\reviewfix1
```

Result: exit 0, 12 passed in 10.20s.

Compile and combined regressions:

```powershell
$env:PYTHONPATH=(Resolve-Path 'src').Path
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m compileall -q src/pmt/server_admin/backup.py src/pmt/server_admin/upgrade.py src/pmt/server_admin/cli.py
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m pytest -q tests/server_admin/test_server_admin.py tests/server_admin/test_c2_server.py tests/server_admin/test_d1_registry.py tests/server_admin/test_e1_operations.py tests/server_admin/test_e2_backup_upgrade.py tests/test_phase5_host_common.py tests/test_phase5_registry_contract.py --basetemp C:\PMT\work\phase5-test\E2-server\reviewfix-final --junitxml C:\PMT\work\phase5-test\E2-server\reviewfix-final.junit.xml
```

Result: compile exit 0; pytest exit 0, 85 passed, 2 skipped in 64.25s. Skips: POSIX mode check on Windows and D1 persistent-sample-only test.

`combined.stdout.txt` and `combined.junit.xml` are copied from the external test root after the run.

## Coverage mapping

- S15 backup/quiescence/artifact integrity/sanitized auth metadata/prune ownership: backup, active-claim, artifact, and prune cases in `tests/server_admin/test_e2_backup_upgrade.py`.
- S15 strict archive restore-check/import, empty-target guard, retained namespace/scope chain, resumable post-commit registry CAS receipt, ZIP path/count rejection: import/restore/archive cases in that file.
- S17 immutable source ref, prepared release contract, ordered separate-venv install/doctor/backup/stop/switch/start/compat, rollback after health/compat failure: upgrade cases in that file.
- Review regressions: candidate install argv uses `sys.executable`, pins the full SHA, and is tested without executing commands; systemd-shaped candidate Doctor uses a scratch `service.kind=none` config, while source config bytes remain unchanged.
- CLI E2 dry-run/apply and C1/C2/D1/E1/version/auth/registry contract regressions are included in combined coverage.

## Gaps

- Real service-account execution, real service/task/firewall changes, real package installation/upgrades, and production Host operations remain intentionally unrun (F1/production gate).
- POSIX file-mode behavior was skipped on Windows; real Linux verification remains pending.
- No new dependency added. `cryptography` remains synthetic-test-fixture-only; runtime paths use the standard library and existing Host operations.

Main verification: final backend fixes included in both Python3.14.5rc1 and Python3.13.13 integrated306pass/4skip runs. Prior parent109pass/2skip captured before native2P2review; parent-pre-review artifacts preserve that narrower successful evidence. Independent backend closure confirms sys import/pure command builder and service-agnostic scratch doctor fixed, focused12pass/compile0, no S15 blocker. Actual Native installer invocation was blocked by auto-review and replaced by pure command/static binding verification; no installation success is claimed. ClientC09final review fix remains separately tracked.
