---
name: proj-mgmt-tool
description: Use the configured PMT CLI for durable project context, two requirement trees, versioned Step handoffs, model routing and claimed parallel execution with evidence-based completion.
---

# PMT project workflow

Use PMT as the shared source of project state when it is installed and configured. Read [the reference](references/cli-workflow.md) for the request envelope and operation examples.

For planning, model selection or parallel Step work, read [the phase-two workflow](references/model-workflow.md). This is the main session's tool-calling procedure; installing the skill does not create native tools or authorize a provider API connection.

- At the start of work, read the relevant project context and current revisions. A SessionStart hook may provide saved context when `PMT_SCOPE_ID` is explicitly configured; `PMT_RECORD_ID` may narrow it to one Item. Without an explicit scope, do not guess one.
- Save explicit user decisions and meaningful changes with their reason and evidence references.
- Claim an Item before doing its work. Continue only after PMT returns a claim token.
- For configured Steps, use the execution Queue and scope claim instead of an independent Item claim. Read/write task contents only after successful scope acquisition and Git reconciliation; dashboard metadata may be read beforehand.
- Finish only after its stated criteria and verification evidence are recorded. If work stops, release the claim as Paused or Blocked with a concrete next step.
- Treat Stop, idle, session end, and tool completion as lifecycle events. They never prove a task is Done or approve a decision.
- If the CLI is unavailable, say PMT is unavailable and continue only within the user's request; do not claim that state was saved.

Lifecycle hooks record only minimal identifiers and event metadata. Never read transcripts to infer intent, copy prompt bodies, or send tokens into PMT. Use an explicit PMT operation for decisions or status changes.
