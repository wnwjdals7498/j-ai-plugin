---
name: proj-mgmt-tool
description: Use the configured PMT CLI for durable project context, two requirement trees, versioned Step handoffs, model routing and claimed parallel execution with evidence-based completion.
---

# PMT project workflow

Use PMT as the shared source of project state when it is installed and configured. Read [the reference](references/cli-workflow.md) for the request envelope and operation examples.

For planning, model selection or parallel Step work, read [the phase-two workflow](references/model-workflow.md). This is the main session's tool-calling procedure; installing the skill does not create native tools or authorize a provider API connection.

For structured graph changes, partial documents, bounded task context or evidence reuse, read [the efficiency workflow](references/efficiency-workflow.md). Use returned references and current source pins instead of repeatedly transmitting the whole project.

For hosted storage, cross-device context, connection errors or preserved offline results, read [the Host workflow](references/host-workflow.md). Storage mode does not move model/CLI execution to the server.

For session recapture, checkpoints, change/alignment review or resume details, read [the continuity workflow](references/continuity-workflow.md). Recheck current source and work authority before acting on retained references.

- At the start of work, read the relevant project metadata overview and current revisions. A SessionStart hook may provide a bounded overview only when `PMT_SCOPE_ID` is explicitly configured; `PMT_RECORD_ID` may narrow it to one Item. Treat the overview as navigation metadata, not current source proof, private Step instructions, run ownership or permission. Without an explicit scope, do not guess one.
- Save explicit user decisions and meaningful changes with their reason and evidence references.
- Claim an Item before doing its work. Continue only after PMT returns the local claim token or authenticated Host claim reference; preserve its exact identity.
- For configured Steps, use the execution Queue and scope claim instead of an independent Item claim. Read/write task contents only after successful scope acquisition and Git reconciliation; dashboard metadata may be read beforehand.
- Finish only after its stated criteria and verification evidence are recorded. If work stops, release the claim as Paused or Blocked with a concrete next step.
- Revalidate current Host/source/work authority before reading task details or acting. A checkpoint can be created only from an actual persisted decision, current run/claim/result review, or applied change receipt.
- Treat Stop, idle, session end, and tool completion as lifecycle events. They never prove a task is Done, approve a decision or create a checkpoint.
- If the CLI is unavailable, say PMT is unavailable and continue only within the user's request; do not claim that state was saved.

Lifecycle hooks record only minimal identifiers and event metadata. Never read transcripts to infer intent, copy prompt bodies, or send tokens into PMT. Use an explicit PMT operation for decisions or status changes.
