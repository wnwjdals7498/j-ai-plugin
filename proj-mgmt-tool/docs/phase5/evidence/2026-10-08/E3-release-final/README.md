# E3 final committed release verification

Date: 2026-10-08
Commit: `aa39931835204ee4508e53df01a4cc53ef01434c` (`aa39931`)
Build output: `C:\PMT\work\phase5-test\E3-release-aa39931\0.5.0`

## Build and source proof

Main built from `git archive` of committed `aa39931` into `C:\PMT\work\phase5-test\E3-source-aa39931`; build metadata records no worktree inputs and exit 0. Current HEAD matches the build commit. Before this evidence folder was added, the only worktree status entry was `?? graphify-out/`. Full build command/output is in [build-metadata.json](build-metadata.json); archive-source file hashes and tracked Git modes are in [source-hashes.json](source-hashes.json).

The result has two root logical plugins, `pmt-lifecycle` and `pmt-server`, across four build targets: Claude, Codex, OpenCode, and Server. Each target reports Core `0.4.1`, plugin `0.5.0`, schema `5`. The 117 Core source files match all three client outputs. Claude and Codex `bin/pmt`, `bin/pmt.cmd`, and `scripts/pmt_easy.py` match their Git-archive sources by SHA-256. The 13 Server source assets match; the Server bundle adds only four generated metadata files. OpenCode intentionally packages its bridge rather than the root client frontdoors.

## Files, ZIPs, modes, and manifests

[bundle-inventory.json](bundle-inventory.json) records every output file’s size/hash/mode, every ZIP member’s size/hash/mode, and source-to-bundle comparisons. Each ZIP matches its target directory by member path and content hash, and each ZIP hash matches the successful builder metadata:

- Claude: `32c995d40e9bcaad292c21ad617ff3ffdd25e3c88f65c557149d49ee6b369e3e`
- Codex: `1de6071cace7be972cb97485001dc2e91a4314c824895e55b27e6e19993992f9`
- OpenCode: `869afe3a37eb94844e7b8f37500b8a1aba8306aea4c4a7d7e5d77ae160d881df`
- Server: `fb730f43ef04b5b04fafb86c350067c336c2faa1b39efbac0289c3e6ce944318`

All ZIP members use creator system `3` (Unix). `bin/pmt` and `bin/pmt-server` have mode `100755`; `bin/pmt.cmd`, `bin/pmt-server.cmd`, `scripts/pmt_easy.py`, and all other packaged files have mode `100644`.

Claude’s frozen client/server manifests and both bundled Claude marketplaces, plus the committed local Claude marketplace, all validated with exit 0, no errors, and no warnings. The Server manifest points to `./hooks/claude.json`, which declares exactly one `SessionStart` command. The Codex manifest declares `hooks: []`; no generic `hooks/hooks.json` exists. The Server bundle contains no `src/` tree or client `pmt` frontdoor. Validator output is in [claude-validator-results.json](claude-validator-results.json).

## Runtime checks

Using actual Windows PowerShell and installed Git Bash from an unrelated working directory, all six final-bundle launcher checks passed with exit 0. Claude/Codex client `mode` passed with explicit `PMT_PYTHON` and with saved `client.json` interpreter fallback; all returned `mode: unconfigured`, created no fresh ConfigRoot/DataRoot/profile/database, and the seeded metadata case left only its `client.json`. Both Server wrappers returned `version --json` 0.5.0/Core 0.4.1 with the dev Host venv, while a spaced and Unicode HostConfigRoot remained absent. All used isolated paths and no `PYTHONPATH`; exact arguments, stdout, and side-effect results are in [launcher-smoke-results.json](launcher-smoke-results.json).

The earlier actual Codex app-server RPC discovered the enabled `pmt-server:pmt-server` skill and returned zero hooks. Its cached skill, Claude hook, and Codex manifest match the final archive semantically; the cache contains `hooks/claude.json` and no generic hook file. See [codex-cache-semantic.json](codex-cache-semantic.json). No new RPC, provider request, or interactive product session was run for this final check.

Markdown scanning checked 11 local relative links in each client target and 5 in Server, with zero broken links; external URLs and anchors were excluded. Counts are in [markdown-links.json](markdown-links.json).

No Host network, service, task, firewall, database, credential, installer, or port operation was performed. The whole-project pytest run was left to main; this task made no source, test, documentation, or builder edits. Linux runtime and real Claude/Codex product-session trust remain unverified.
