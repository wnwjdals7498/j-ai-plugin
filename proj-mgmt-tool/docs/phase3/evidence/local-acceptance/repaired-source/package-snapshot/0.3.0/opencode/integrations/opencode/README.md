# OpenCode stable plugin entry

Keep this directory at `integrations/opencode/` beside the package `src/pmt/` tree. Register `integrations/opencode/pmt.js` as the local plugin entry; copying only `pmt.js` breaks its relative Python bridge. The plugin uses OpenCode's V1 `event` callback for session events and its dedicated `tool.execute.after(input, output)` hook for tool completion. It writes warnings through the documented `client.app.log()` API.

The plugin starts `bridge.py` by absolute package-relative path with an argv array (`shell: false`); the bridge adds the bundled `src` directory before importing PMT. It forwards only allowlisted event metadata and never copies tool output or prompt text. Set `PMT_PYTHON`, `PMT_DATA_ROOT`, and `PMT_CONFIG_ROOT` in the OpenCode process environment. Missing `PMT_INSTALLATION_ID` uses the persisted environment profile UUID under the config root.

For optional startup context, set `PMT_SCOPE_ID` to an explicit scope UUID and optionally `PMT_RECORD_ID` to an Item UUID within that scope. The plugin reads context through `read_context` and adds only the bounded `context_markdown` through OpenCode 1.18's `experimental.chat.system.transform` hook. With no explicit scope it does not query or infer one; lookup failures are reported through the OpenCode plugin log.

The installed OpenCode executable was unavailable in the verification environment, so plugin loading and callback execution remain unverified there.
