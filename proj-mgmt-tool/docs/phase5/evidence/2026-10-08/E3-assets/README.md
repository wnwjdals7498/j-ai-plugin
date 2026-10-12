# E3 server plugin asset preflight

Date: 2026-10-08  
Source revision: `e4bb21ac061a8d384b06401a3a5583cd4ff57476`  
Working tree: concurrent D2/main edits were present; this preflight changed only the E3-owned SessionStart asset, server launchers, E3 test, and this evidence folder.

## Scope and result

Verified the prepared `pmt-server` SessionStart asset and both launchers. The metadata-only SessionStart script does not import PMT or inspect Host state. Given a valid Python 3.13+ interpreter and absolute config root, it exports only `PMT_SERVER_PYTHON` and `PMT_HOST_CONFIG_ROOT`; a fresh root remains absent and gets nonblocking native JSON guidance to run `pmt-server init`. Existing config content is left to `pmt-server config validate`. Missing arguments, relative roots, and unavailable `CLAUDE_ENV_FILE` return nonblocking native JSON. Invalid environment-file paths do not create their parent.

The Windows `.cmd` launcher and the real installed Git Bash launcher both ran `version --json` from an unrelated working directory with `PYTHONPATH` removed. Each used `C:\PMT\src\venv-dev\Scripts\python.exe` (Python `3.14.5rc1`) and accepted a config-root path containing spaces and Korean characters. Missing settings and nonexistent interpreters returned exit 3 with guidance. The safe config fixture used only `C:\PMT\work\phase5-test\E3`; no Host operation, service, firewall, database, credential, network port, or actual user config was touched.

The raw `pmt-server` skill was reviewed. It directs operators to the installed Host venv, tells them to inspect existing state and preserve backups, and says service/firewall changes require authorization. It does not grant authority to make Host changes outside the current user request; this preflight only ran the read-only version command.

## Validation

Command:

```powershell
& 'C:\PMT\src\venv-dev\Scripts\python.exe' -m pytest -q proj-mgmt-tool\tests\test_phase5_server_plugin.py --basetemp='C:\PMT\work\phase5-test\E3\pytest-temp-flowfix-final2' --junitxml='C:\PMT\work\phase5-test\E3\junit.xml'
```

Result: **14 passed**. Captured output is in [pytest.stdout.txt](pytest.stdout.txt); JUnit is in [junit.xml](junit.xml). The external basetemp and original reports are under `C:\PMT\work\phase5-test\E3`.

## Remaining checks

This is asset preflight only. Full source-freeze/bundle validation remains with main after concurrent client work settles. Native Linux execution, installed Claude product SessionStart trust, Codex end-user setup, and all Host operations remain untested and belong to later validation; no F2/F3 or operating-success claim is made. Graph refresh was deferred while concurrent writers were active.

Parent package preflight before E2: tests/test_packaging.py + test_phase5_server_plugin.py + test_phase5_host_common.py, external unique basetemp C:/PMT/work/phase5-test/E3-package-preflight/pytest, exit1:36pass/1fail in89.30s. Solefailure is the unchanged A0 Windows launcher.stat executable-bit assertion test_pkg_01_three_separate_bundles_manifest_and_zip_hashes. New four-target/two-logical-plugin, server no-Core, all sourcehash/ZIPmode checks pass. package-preflight stdout/JUnit retained. This is asset preparation, not final E3 whole-suite acceptance.
