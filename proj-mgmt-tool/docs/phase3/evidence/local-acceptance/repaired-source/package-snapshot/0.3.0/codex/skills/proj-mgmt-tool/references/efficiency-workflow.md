# Structured changes and bounded context

Use the same configured protocol-v1 CLI and current execution ownership as the main workflow. An operation name in a plan is not a supported feature: check the installed runtime and an actual response. `operation_unavailable` is not a successful save. These Python operations implement declared decisions; the main session still resolves user goals and evidence applicability.

## Change a graph without resending it

1. Acquire the run's scopes before reading task source. Include the project scope, repository ID, local workspace, POSIX `relative_graph_path` and current `run_id` in source operations. A standalone project needs an explicit repository/workspace mapping or matching Git origin.
2. Call `capture_source_pin`. Carry the returned `SourcePin` unchanged. Git HEAD/ref, graph schema/version/hash and reviewed dirty state are distinct; a verified non-Git source has explicit `source_kind=non_git` and does not pretend to be clean.
3. Build a `change_set` with a new UUID `change_id`, reason/evidence refs and only intended create/update/relate/unrelate/deprecate operations. Omitted fields remain unchanged; explicit `clear` removes a value. Creation aliases use `temp:<1–64 ASCII token>` and never replace canonical UUIDs.
4. Use `preview_graph_change`, then `calculate_graph_impact` with the same original change set and preview under the before-source pin. Keep the ImpactSet. Unknown field/dependency/coverage reasons are not empty impact.
5. Call `apply_graph_change` with the same change identity and expected before pin. A new operation gets a new request UUID; retransmitting the same operation keeps its request UUID. Do not invent new node IDs between preview and apply.
6. Re-capture the actual source. A document consumer must match the ImpactSet's change ID/hash and expected candidate hash/version/schema with the apply receipt's before/new pins and the fresh source pin.

`rebuild_graph_index` creates a disposable source-pinned index. `query_graph` still requires the live claimed source and supports selected nodes/fields/relations, direction, bounded depth/page size and source/query-bound cursors. A stale/corrupt index requires explicit rebuilding; it is not an empty successful answer.

## Documents and context

The first document generation needs a complete baseline. Per-segment manifests record stable IDs, generated/manual ownership, template version and node/field/relation dependencies. `register_segment_manifest` records those dependencies; complete coverage requires the renderer's declared baseline set to match the actual registered set and source. A coverage handoff is not a signature or an authorization grant.

Preserve manual text and unrelated segments. A changed generated span, target hash, source or owner is a conflict. File and DB effects use a journal; a retained recovery copy can contain user edits. Never delete or overwrite conflict versions while trying to recover. Resume the original effect with current authorized scope access, rather than applying a new untracked replacement.

Build task context from current authorized source, Step criteria and private directive references. Use finite byte/line budgets and required goal, constraints, non-goals, evidence and unresolved conditions first. Missing required content must be returned as incomplete. A role selects a projection and does not grant access. Resolve short aliases only with the context's project/source/map version; rebuild them for a new context.

Detailed reads must refer to real retained content. Do not follow expired cursors or claim that discarded output is recoverable. On session resume, recheck the current source, owner/claim and evidence before using the previous summary. A summary is a navigation aid.

## Tool results and reusable evidence

Use `compact_tool_result` for the actual run/Step/task and output or authorized resource reference. It preserves permitted, redacted content as a hash-checked resource and returns bounded facts and refs. Reported status and exit without an applicable runner receipt are producer observations; they are not independent acceptance evidence. `read_tool_result_detail` returns only verified available byte/line ranges.

Reuse investigations/tests only when their definition, target, relevant input/environment/source/tool/dependency conditions and accessible evidence remain applicable. Model provenance is a condition only when the model is the subject being checked. Unknown required conditions, later failures, damaged evidence and relevant changes reject a hit. An active original run is a wait/share reference, not a completed result.

Keep independent user events even when their work reuses the same evidence. An atomic reuse claim prevents duplicate execution; it does not replace task scope ownership. Never reclaim a run or release a lock from timeout, idle or silence.

Byte counts, tool calls and elapsed time are separate from provider token usage. Keep unavailable usage unknown, and include detailed reads, context generation, retries and rework in comparisons. Fixture validation does not prove model quality or deployment support.
