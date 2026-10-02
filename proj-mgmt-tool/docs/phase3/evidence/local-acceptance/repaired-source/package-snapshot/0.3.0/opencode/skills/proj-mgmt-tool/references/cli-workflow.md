# PMT CLI workflow reference

The machine interface is one UTF-8 JSON request on stdin and one JSON response on stdout. Supply the same explicit `--data-root` and `--config-root` for every product sharing a PMT profile. Do not parse diagnostics from stdout.

## Lifecycle hook bridge

Native hook wrappers read one product JSON object from stdin and call:

```text
python -m pmt.hooks --product PRODUCT --event NATIVE_EVENT
```

Configure `PMT_DATA_ROOT`, `PMT_CONFIG_ROOT`, and optionally `PMT_PYTHON`, `PMT_INSTALLATION_ID`, and `PMT_PRODUCT_VERSION` in the product process environment. The bridge invokes the shared CLI with argv, never through shell interpolation. It writes a minimal pending envelope under `<data-root>/hook-pending/` before attempting storage and removes it only after a JSON `ok: true` response. A source occurrence ID produces deterministic event and request UUIDs; without one, replay is safe only from the same pending envelope.

At `SessionStart`/`session.created`, saved context is queried only when `PMT_SCOPE_ID` contains an explicit UUID; optional `PMT_RECORD_ID` narrows that query to one Item. The adapter sends `read_context` through the shared CLI and uses only its bounded `context_markdown` result. Missing scope means no context query. An invalid scope or failed query is surfaced as a native warning, not replaced by guessed project state. Codex and Claude use their SessionStart `additionalContext`; OpenCode adds the result through its documented system transform. Only explicit PMT records are read; native transcripts and prompt bodies are not inspected.

The event bridge sends only `record_event` with `protocol_version: 1`, UUID `request_id` and `event_id`, `actor: "hook"`, stable `session_id`, product source, and allowlisted event metadata. Context retrieval is a separate read-only `read_context` call. Prompt text, transcripts, tokens, tool output, and arbitrary native payload fields are excluded.

## Explicit work changes

Use the installed CLI's operation schema for `read_context`, `save_change`, `save_decision`, `claim_task`, `release_claim`, `finish_task`, and verification. Always carry returned IDs and expected revisions forward. A revision conflict requires a fresh read. Keep request IDs stable when retrying the same logical request and never reuse one for different input.

An unavailable, timed-out, or failed PMT call is not a successful save. Lifecycle observations never imply a decision, task completion, claim recovery, or Done state.
