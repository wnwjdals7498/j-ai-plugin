# B3 entrypoint and Codex Hook evidence

Base commit: `06e18ec437742405d5e383b356cee76404665a9f`. No commit created.

## Changes owned by B3

- Added the canonical `bin/pmt`, `bin/pmt.cmd`, and `scripts/pmt_easy.py` frontdoors. The helper reads `PMT_PYTHON` first, then bounded/validated `ConfigRoot/client.json`, before importing `pmt`; if neither provides an interpreter it exits 3 with guidance. It requires Python 3.13 and forwards exact arguments and exit status. Windows metadata handoff uses `subprocess.run` so saved interpreter and script paths with spaces work.
- Routed the Codex Hook through `pmt.easy_hook.run --product codex`. The stdlib-only entry checks Python before importing PMT; older Python gives a one-line SessionStart hint and exits 0. Codex commands use `PMT_PYTHON` when set, otherwise `python`/`py -3`.
- Added tests in `tests/client_setup/test_b3_entrypoints.py` for interpreter guard, metadata path, quoted arguments, Windows PowerShell, Windows Git Bash, and Codex Windows Hook command.

## Validation

| Check | Result |
|---|---|
| `python -m pytest -q tests/client_setup/test_b3_entrypoints.py` | 8 passed, exit 0. Source output and JUnit: `pytest-entrypoints-8.txt`, `pytest-entrypoints-8.xml`. |
| Hook contract: `tests/test_hooks.py` plus `test_hook_01_all_product_fixtures_use_real_cli_and_keep_only_minimal_event` | 23 passed, exit 0. Source output and JUnit: `pytest-hooks-current.txt`, `pytest-hooks-current.xml`. |
| Combined B3 + all `test_hook_acceptance.py` + `test_hooks.py` after B1 cache fix | 39 passed, 2 failed, exit 1. Remaining failures are the two pending-storage-obstruction cases, whose fixtures have no cached profile; C01 now intentionally returns a sanitized `not_configured` warning before Core on those later events. Parent/client executor notified. Output/JUnit: `pytest-hook-integration-final.txt`, `pytest-hook-integration-final.xml`. |
| Build plugin bundles with `scripts/build_plugins.py --output-dir C:\PMT\work\phase5-test\B3\package-output-final --version 0.5.2` | Passed; Core 0.4.1, schema 5. Output stayed outside the checkout. |
| Built Claude `bin/pmt.cmd` and Codex `bin/pmt` from real Windows PowerShell and Git Bash | Both exit 0 with explicit `PMT_PYTHON`, then both exit 0 with `PMT_PYTHON` unset and a `client.json` interpreter path containing spaces. With neither source, both exit 3 with setup guidance. Package and all ConfigRoot/DataRoot paths were isolated below `C:\PMT\work\phase5-test\B3`; `PYTHONPATH` unset. Record: `built-final-smoke.txt`. |
| Actual Codex `commandWindows` SessionStart fixture with Python and plugin paths containing spaces | Passed through Windows PowerShell and a temporary `.cmd` file; common Hook returned native JSON, exit 0. Included in the 8 B3 tests. |
| `git diff --check` on B3-owned files | Passed. |

All pytest basetemp, package output, ConfigRoot, and DataRoot were under `C:\PMT\work\phase5-test\B3`. Development interpreter: `C:\PMT\src\venv-dev\Scripts\python.exe` (Python 3.14.5rc1). Runtime shell: Windows PowerShell and `D:\Application\Git\bin\bash.exe` (Git for Windows).

## Test scope limits and integration note

- T-C10-3 fixture regression is represented by the 23-pass focused run. Actual Claude/Codex new sessions and Codex `/hooks` trust (T-C10-1/2) were not performed.
- Git for Windows Bash was exercised directly. Native Linux Bash (T-C11-1) and the Claude Code Bash tool/product session (T-C11-2) were not available in this Windows validation run.
- Python below 3.13 was simulated by patching the entrypoint's `sys.version_info`; no separate old interpreter was run.
- `built-launcher-smoke.txt` preserves the first package smoke failure: Windows `os.execvpe` mishandled the saved Python path with spaces from Git Bash. Replacing the metadata handoff with `subprocess.run` resolved it; the final 0.5.2 smoke is `built-final-smoke.txt`.
- Parallel dirty files belong to other lanes. B3 changes are limited to `integrations/codex/hook.py`, `integrations/codex/hooks/hooks.json`, `bin/pmt`, `bin/pmt.cmd`, `scripts/pmt_easy.py`, this test file, and this evidence folder. Graph refresh deferred while other writers are active, per main-agent direction.

## Parent final integration

After the client legacy-cache correction and one intended native-warning fixture update, the complete B3 + test_hook_acceptance.py + test_hooks.py run passed **42 tests**, exit0 (16.33s). Parent stdout/JUnit are parent-pytest.txt/xml. The prior39pass/2fail result above is preserved as superseded evidence, not the current result.
