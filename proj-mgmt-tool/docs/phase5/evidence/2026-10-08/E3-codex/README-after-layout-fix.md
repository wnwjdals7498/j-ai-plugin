# E3 Codex runtime discovery after Claude-only hook layout fix

Date: 2026-10-08  
Codex CLI: `codex-cli 0.160.1`  
Isolated CODEX_HOME: `C:\PMT\work\phase5-test\E3-codex\isolated-i4nq_3zq\codex-home`

The initial discovery result remains preserved in [README.md](README.md) and [skills-list-rpc.json](skills-list-rpc.json) as the before-layout-fix evidence. This file and [skills-list-rpc-after-layout-fix.json](skills-list-rpc-after-layout-fix.json) record the corrected result.

## Safe cache refresh

Before uninstalling anything, verified the resolved installed cache directory was inside the isolated CODEX_HOME, and that the stale cache contained generic `hooks/hooks.json`. Read `codex plugin` help for the documented `remove`, `add`, and `list` subcommands. The isolated plugin list identified `pmt-server@j-ai-plugins` as a local marketplace installation whose source was `C:\PMT\src\j-ai-plugin\pmt-server`.

With only the isolated CODEX_HOME selected and environment variables matching API keys, access tokens, or PMT Host credentials removed, ran:

```powershell
codex plugin remove pmt-server@j-ai-plugins --json
codex plugin add pmt-server@j-ai-plugins --json
```

Both returned exit 0. Verified the refreshed cache remained under the isolated CODEX_HOME, had `hooks/claude.json`, and did not contain `hooks/hooks.json`. The cached Codex manifest still has `"hooks": []`.

## Runtime RPC result

Generated the protocol schema using the local CLI’s documented command, then used the schema-exposed `initialize`, `skills/list`, and `hooks/list` APIs. Started `codex app-server --stdio`, sent `skills/list` with the repository CWD and `forceReload: true`, then `hooks/list`. No thread/turn/model request, provider authentication, external network call, or hook execution occurred. Terminated the owned app-server child after the RPC replies; it is stopped. Its exit code 1 reflects that explicit termination, not an RPC failure.

- `skills/list` returned enabled `pmt-server:pmt-server` with plugin ID `pmt-server@j-ai-plugins` from the refreshed cached skill path.
- `hooks/list` returned one CWD result with `hooks: []`, no warnings, and no errors. It found no `pmt-server` hook.

This confirms actual Codex app-server skill discovery while the Claude-only SessionStart hook is absent from Codex’s runtime hook inventory after the asset layout change. It does not establish behavior in an interactive Codex product session or trust prompt; no such session was launched. Only the isolated CODEX_HOME was refreshed; no global Codex config or cached source file was directly edited.

The owned regression test `test_server_plugin_declares_claude_only_hook_asset` checks that source Claude points to `./hooks/claude.json`, Codex declares no hooks, and the generic default hook filename is absent. It passed alone (`1 passed`); output and JUnit are in [pytest-asset-test.stdout.txt](pytest-asset-test.stdout.txt) and [junit-asset-test.xml](junit-asset-test.xml).

The complete E3 test file was also attempted after E2 changed shared server code: 13 passed, 2 failed. Both actual launcher tests failed because the current `pmt.server_admin.cli` imports `pmt.server_admin.upgrade`, which is absent in the in-progress source tree (`ModuleNotFoundError`). The failure output is preserved in [pytest-after-layout.stdout.txt](pytest-after-layout.stdout.txt) and [junit-after-layout.xml](junit-after-layout.xml). I did not patch shared code or build while E2 is active; rerun those launcher checks after the source tree is complete.
