# PMT CLI workflow reference

The machine interface is one UTF-8 JSON request on stdin and one JSON response on stdout. Supply the same explicit `--data-root` and `--config-root` for every product sharing a PMT profile. Do not parse diagnostics from stdout.

## Lifecycle hook bridge

Native hook wrappers read one product JSON object from stdin and call:

```text
python -m pmt.hooks --product PRODUCT --event NATIVE_EVENT
```

Configure `PMT_DATA_ROOT`, `PMT_CONFIG_ROOT`, and optionally `PMT_PYTHON`, `PMT_INSTALLATION_ID`, and `PMT_PRODUCT_VERSION` in the product process environment. The bridge invokes the shared CLI with argv, never through shell interpolation. It writes a minimal pending envelope under `<data-root>/hook-pending/` before attempting storage and removes it only after a JSON `ok: true` response. A source occurrence ID produces deterministic event and request UUIDs; without one, replay is safe only from the same pending envelope.

At `SessionStart`/`session.created`, the adapter sends `compose_resume_overview` only when `PMT_SCOPE_ID` contains an explicit UUID; optional `PMT_RECORD_ID` narrows the selected task. The overview is bounded shared metadata: current direction/status refs, active-run summaries, checkpoint/source freshness and unknowns. It is not private Step content, a source proof, a run claim, or permission to act. A unique configured mapping may provide repository/branch dimensions; missing or ambiguous mappings must remain a selection/unknown result. Codex and Claude use SessionStart `hookSpecificOutput.additionalContext`; OpenCode currently uses `experimental.chat.system.transform`, whose compatibility must be checked against the installed OpenCode API. Missing scope means no overview query. Invalid scope, revoked Host access or failed query is surfaced as a native warning, not replaced by guessed project state. Native transcripts, prompt bodies and private directives are not inspected or sent.

The event bridge sends only `record_event` with `protocol_version: 1`, UUID `request_id` and `event_id`, `actor: "hook"`, stable `session_id`, product source, and allowlisted event metadata. Overview retrieval is a separate read-only `compose_resume_overview` call. In Hosted mode the native SessionStart source must match the installed adapter/profile, the client uses the registered Host principal, and `PMT_SCOPE_ID` must match the request scope. Stop, idle, end-of-session and prompt events do not create semantic checkpoints. Prompt text, transcripts, tokens, tool output, absolute paths and arbitrary native payload fields are excluded.

## Explicit work changes

Use the installed CLI's operation schema for `compose_resume_overview`, `read_current_facts`, `capture_work_basis`, `validate_basis`, `create_checkpoint`, `read_checkpoint`, `link_session`, `save_change`, `save_decision`, `claim_task`, `release_claim`, `finish_task`, and verification. `capture_work_basis` reads Git and selected file hashes on the mapped client only after current run/claim authorization; Hosted storage receives refs/hashes and labels its provenance `client_attested` with `host_git_verified=false`. Always carry returned IDs and expected revisions forward. A revision conflict requires a fresh read. Keep request IDs stable when retrying the same logical request and never reuse one for different input.

An unavailable, timed-out, or failed PMT call is not a successful save. Lifecycle observations never imply a decision, task completion, claim recovery, or Done state.
