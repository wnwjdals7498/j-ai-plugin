# Main-session planning and model handoff

Use the existing protocol-v1 JSON envelope through `python -m pmt` with explicit configured data/config roots. stdout is the response envelope, stderr diagnostics. Use a new request ID for a new action and reuse it for retransmission of the same action.

## Planning and role decisions

The upper role is the current main session. Use the highest role for unresolved major principles or shared architecture choices; use the lower role for bounded approved implementation, research or tests. A user's named model and execution route take priority. Record actual agent/provider/model/permissions separately; don't claim a role implies product support.

1. Collect the original request, goals and constraints. Build the natural-language tree with a premise on each split, one-line summaries of at most 50 characters, gaps/unknowns and prototype/expansion/production scope and criteria.
2. Build the implementation tree from available code, documentation, valid knowledge/tests, frameworks, modules, permissions and resource limits. Connect each implementation method to the requirements it solves; specify semantic input/output, invariants, failure, test and logging methods.
3. Where user choice is needed, present at least three feasible methods with labels of at most 30 characters, plus custom input and delegated AI choice. If fewer methods are actually available, report the shortage. Reuse verified principles only after checking current applicability.
4. End a natural-language branch when further splitting selects implementation methods; end an implementation branch before direct file-edit recipes. User delegation also ends splitting at that node and records the delegated scope. Don't repeatedly ask within that scope.
5. Use `validate_plan_graph` and `save_plan_draft`. Only publish after both trees end and their principles/methods/evidence have been reviewed. `publish_project_docs` requires an owned run covering the generated documents and AGENTS reference; preserve existing text and provide observed file hashes for existing targets.

The main session performs interpretation and judgment. Python validators enforce structured constraints and persist references; they do not infer requirements from arbitrary user conversation. Unknown methods become research/experiment Steps with approved exploration boundaries before implementation is authorized.

## Routing and dispatch

1. Export the currently available native tools/model combinations into a capability snapshot with observation time and evidence source. Product version alone does not prove model availability, authentication or cancel/query support. Use `register_capabilities`; settings are optional through `save_routing_policy`.
2. Use `select_execution_route`. In auto mode, verified same-agent native subagents have priority; slot exhaustion means waiting. Confirmed unsupported native combinations may use permitted CLI/SDK, using the supported Claude/Codex command adapters. Direct API/SDK execution is outside the current scope. A started or unknown run has no alternate launch.
3. Use `save_step_directive` immediately before execution: parent Item, requirement/plan/directive versions, purpose, goal/non-goal, functional add/modify/delete/forbidden boundaries, semantic I/O, method, tests, logging, context refs, permissions, scope and criteria. Directives are internal resources, excluded from ordinary project-management views.
4. Use `enqueue_execution` and `prepare_execution`. Queue retries use the same Step/job and a new run; only confirmed unstarted/terminated transient failures can retry, at most twice. Only a successful claim response permits actual scope access.
5. Use `sync_project_baseline` after claim and before reading/writing the target. Review unknown commits and mapped/unmapped impacts, preserve dirty work, and advance the reviewed baseline only after reconciliation. Re-read/pin current directive and target versions; changes require replanning affected Steps.
6. Use `dispatch_execution`. For native mode, it returns a main action. Call the **actual native subagent tool** with the selected model, needed directive/context and boundaries. Then call `attach_execution_handle` with the returned native ID and current run revision. Never treat the returned main action as a launched subagent.
7. For CLI runners, persist and poll the original handle using `poll_execution`. On missing receipts, retain the original run and reconcile. Callback and polling are equivalent result-delivery paths; durable storage precedes main notification.

Independent scope claims permit parallel work; overlapping paths/logical resources wait. Keep at most the configured/actual capacity, with a default of three. Expand scopes atomically before accessing additional areas. Do not hold partial new scopes while waiting.

## Worker result and review

Return the pinned Step/run/directive version, actual route/model, changed areas/artifacts, each criterion's pass/fail/blocked/not_run, actual command/exit/environment/code-state and evidence references, unresolved issues and next action. A claim of success is not evidence. Use allowed product tools; direct provider APIs are disabled in this implementation.

Record evidence resources and `record_verification` with the pre-execution `lookup_verification` fingerprint. Use `submit_execution_result` for native results, or the runner's normalized receipt. Preserve stale/late results as historical facts; they don't complete the current plan. Use `review_step`/`review_execution` only with current passing verification, confirmed termination and explicit integration review. Item and Work completion require their own criteria; child success does not auto-finish parents.

On stop/cancel, call `request_execution_cancel`, ask the original native tool or `cancel_runner` to stop, and record confirmed termination via `reconcile_execution`. Time, silence, Stop or idle never release ownership. Update observation at state changes and within 60 seconds on long active work; keep actual-change time distinct from observation time.

For a premise change, stop affected dispatches, use `invalidate_plan_branch`, coordinate active owners and update only affected documentation/Steps/evidence. Escalate shared contracts/architecture to the highest role and changed user goals to the user. Restore an urgent service within authorized boundaries first, then preserve evidence and plan the root-cause fix.
