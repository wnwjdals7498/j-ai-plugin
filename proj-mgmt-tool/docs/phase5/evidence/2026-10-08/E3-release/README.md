# E3 frozen release preflight

Date: 2026-10-08  
Release directory: `C:\PMT\work\phase5-test\E3-release-final\0.5.0`  
Source HEAD at build: `ff683b612c09f14db2cb696d25d36eca7a43126b` (`ff683b6`, after `9130945`).

## Provenance and scope

This preserves the pre-fix release directory exactly as built. Its source checkout had concurrent Phase 5 modifications and `pmt-server/` was untracked, so the artifact is not reproducible from HEAD alone. Main has directed a later clean commit and fresh build; this evidence does not claim that future build. No files under the frozen `0.5.0` artifact directory were changed. Evidence and isolated smoke data are separate.

The output contains four target directories and matching ZIPs: Claude, Codex, OpenCode, and Server. The two root logical plugins are `pmt-lifecycle` (Claude/Codex/OpenCode targets) and `pmt-server` (Server target). All package metadata reports Core `0.4.1`, plugin `0.5.0`, schema `5`.

## Bundle and source integrity

The complete per-file size/SHA-256/filesystem-mode inventory, ZIP member hashes and mode metadata, source comparisons, manifests, and runtime-data filename scan are in [bundle-inventory.json](bundle-inventory.json). Every ZIP member matches its target directory file by path and SHA-256; there are no archive mismatches. The bundle file counts are Claude 133, Codex 134, OpenCode 130, Server 17. No `client.json`, `storage.json`, `profile.json`, `host-config.json`, `.env`, DPAPI, or key data files were packaged.

The 117 Python Core source files match the repo source byte-for-byte in all three client bundles. Canonical Claude and Codex frontdoors also match their source hashes: `bin/pmt` `38c865b3813a14e7b6a0aaec1ef0b322ccdfa8dd9b97186620d443289034f60b`, `bin/pmt.cmd` `8cd07914be891fefb86fe2ac2ff1ec5fb81c35391f83fccf3290a9ec23558196`, and `scripts/pmt_easy.py` `1362f92353ad7b8693f4e0bd5f9f4b77ef386468d0ec2ab02e52e523a26f3dc1`. OpenCode intentionally maps to its bridge and has no root CLI frontdoors in the builder mapping. The 13 source Server assets match the Server bundle; its four additional files are generated marketplace/plugin package metadata, with no source hash mismatches. Full source hashes and source tree state are in [source-hashes.json](source-hashes.json) and [source-state.txt](source-state.txt).

ZIP entries have `create_system=0` (DOS/Windows). The high-word mode fields mark `bin/pmt` and Server `bin/pmt-server` as `100755`; other `bin/*` entries, including `.cmd`, are also marked `100755`, while `scripts/pmt_easy.py` is `100644`. The Git index marks client `bin/pmt` as `100755`, and `bin/pmt.cmd`/`pmt_easy.py` as `100644`. Because these archives identify a DOS creator, Unix extraction’s treatment of the high-word mode values was not verified; Linux execution remains an F2 check. No Linux result is claimed.

The Server bundle has no `src/` tree, no client `pmt`/`pmt.cmd` root launcher, and no generic `hooks/hooks.json`. Its Claude manifest points to `./hooks/claude.json`, which defines exactly one `SessionStart` command. Its Codex manifest has `hooks: []`.

## Validators and safe launcher checks

Claude’s local validator returned exit 0 with no errors for the frozen client plugin manifest, Server plugin manifest, client marketplace, and source marketplace. The frozen Server marketplace also returned exit 0 with one non-blocking warning: it has no marketplace description. Full validator output is in [claude-validator-results.json](claude-validator-results.json).

Actual Windows PowerShell and Git Bash runs from an unrelated working directory used the frozen client frontdoors with `PYTHONPATH` removed. Each ran `mode` twice: once with `PMT_PYTHON` set to the dev venv, and once with the contract-valid `client.json` interpreter fallback. All four returned `mode: unconfigured`; fresh ConfigRoot/DataRoot paths remained absent, and metadata fallback left only its pre-seeded `client.json`. Both frozen Server wrappers ran `version --json` with the dev venv, spaces and Unicode in the uncreated HostConfigRoot, and returned version `0.5.0` / Core `0.4.1`. The HostConfigRoot remained absent. Exact argv, exits, stdout, and side-effect checks are in [launcher-smoke-results.json](launcher-smoke-results.json).

The prior isolated Codex app-server RPC discovered enabled `pmt-server:pmt-server` and returned an empty `hooks/list`. The isolated cached skill, Claude hook, and Codex manifest SHA-256 values match this frozen Server bundle; the cache contains `hooks/claude.json` and no `hooks/hooks.json`. See [codex-runtime-cache-crosscheck.json](codex-runtime-cache-crosscheck.json) and the earlier [runtime RPC record](../E3-codex/README-after-layout-fix.md). This did not run an interactive Codex session or trust prompt.

## Markdown links and operating boundaries

The packaged Markdown scan checked 11 relative local links in each client target and 5 in Server; it found zero broken links. External URLs and anchors were excluded. Counts are in [markdown-links.json](markdown-links.json).

All launcher smoke tests used only `C:\PMT\work\phase5-test\E3-release-final\smoke_final`; provider/API and Host credential environment variables were removed. No model/provider request, Host network call, service/task/firewall change, database operation, real user config, or port probe was made. Full pytest was left to main while it was running.
