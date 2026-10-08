# E3 isolated Codex runtime skill discovery

Date: 2026-10-08  
Codex CLI: `codex-cli 0.160.1`  
CODEX_HOME: `C:\PMT\work\phase5-test\E3-codex\isolated-i4nq_3zq\codex-home`

## Procedure

Used the local `codex app-server --help` and generated its JSON schema with:

```powershell
codex app-server generate-json-schema --out C:\PMT\work\phase5-test\E3-codex\isolated-i4nq_3zq\schema
```

The local schema exposes `initialize`, `skills/list`, and `hooks/list`. Started `codex app-server --stdio` as a child with only the already isolated `CODEX_HOME`; removed environment entries matching API keys, access tokens, and PMT Host credentials. Sent `initialize`, `initialized`, then read-only `skills/list` (`cwds` set to the repository and `forceReload: true`) and `hooks/list`. Terminated the child after replies. It is stopped. The child exit code 1 is the expected result of explicit termination, not an RPC failure. No thread/turn/model request, external auth, network call, or product hook execution occurred.

## Findings

`skills/list` returned the enabled skill `pmt-server:pmt-server`, plugin ID `pmt-server@j-ai-plugins`, at the isolated cached path `plugins/cache/j-ai-plugins/pmt-server/0.5.0/skills/pmt-server/SKILL.md`. This confirms actual app-server runtime discovery, beyond cache presence.

`hooks/list` also returned a `sessionStart` command from that plugin's `hooks/hooks.json`, marked enabled with `trustStatus: untrusted`. The source and cached `.codex-plugin/plugin.json` both say `"hooks": []`, while the generic `hooks/hooks.json` contains the Claude SessionStart definition. Therefore `hooks: []` does **not** suppress Codex app-server discovery of this generic hook in CLI 0.160.1. This is inventory evidence only: the untrusted hook was not run, and its execution/trust prompt behavior remains unverified. It is a packaging/layout issue for main to resolve before claiming Codex has no server hook.

The sanitized RPC summary is in [skills-list-rpc.json](skills-list-rpc.json). No source, manifest, plugin cache asset, global Codex config, credential, or Host state was edited by this probe; app-server skill cache refresh was confined to the isolated CODEX_HOME.
