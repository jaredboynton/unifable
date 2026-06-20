---
name: ground
description: Run before any hard-to-reverse or outward-facing change (destructive edits, deploys, schema migrations, API calls with side effects, config pushes). Builds an evidence ledger, drives claims from UNVERIFIED to VERIFIED via tool calls, resolves forks, then dispatches the cold grounding-verifier agent before proceeding.
---

# /ground — evidence ledger and cold verification

## When to use

Before any action that is hard to undo or affects an external system: destructive file edits, database migrations, deploys, outbound API calls, permission changes, config pushes. If you would normally say "I'm confident this is correct" without a tool call to back it, run /ground first.

## Evidence ledger

Maintain a two-column table throughout this skill. Every claim about the current state of the system starts in UNVERIFIED. A claim moves to VERIFIED only after a tool call produces output that directly confirms it. No claim is self-evident.

| VERIFIED | UNVERIFIED |
|---|---|
| _(confirmed claims with source)_ | _(claims not yet checked)_ |

Populate UNVERIFIED at the start with every assumption the planned change depends on. Examples: the file exists at the expected path; the schema column is nullable; the target service is reachable; the config key is not referenced elsewhere; the migration is idempotent.

## Gather-check loop

Repeat until UNVERIFIED is empty:

1. **Pick** the highest-risk UNVERIFIED row.
2. **Gather** — run the smallest tool call that can confirm or refute it (Read, Grep, Bash). Do not narrate what you are about to do; just run it.
3. **Adjudicate** — inspect the tool output. If it confirms the claim, move the row to VERIFIED with the source (file path + line, or command + output excerpt). If it refutes the claim, record the contradiction and stop: surface the conflict to the user before proceeding.
4. **Surface new dependencies** — if the tool output reveals additional assumptions the change depends on, add them to UNVERIFIED before continuing.

**Termination test**: UNVERIFIED is empty and every VERIFIED row has a cited source. Do not proceed to the next step until this condition holds.

## Fork-classification policy

During the loop, some open questions cannot be resolved by reading code or running commands. Classify each fork before acting:

- **Code-determinable** — the answer exists in the codebase, config, schema, or running system. Use your tools to determine it. Do not surface it to the user.
- **Genuine user preference** — the answer depends on a product decision, a risk tolerance, or an intent that is not encoded anywhere. Stop and surface it as a single, specific question with the two concrete options. Do not proceed past a preference fork without an explicit answer.

Never surface a code-determinable fork as a question. Never silently resolve a preference fork.

## Cold verification gate

When UNVERIFIED is empty and all forks are resolved, dispatch the grounding-verifier agent before making any change. Pass it exactly two things:

1. The evidence ledger (the completed two-column table with all VERIFIED rows and their sources).
2. The diff or a precise description of what will change.

Do not pass your reasoning, intent, or any explanation. The verifier is cold — it has no context from this session.

Wait for the verifier's verdict:

- **GO** — proceed with the change.
- **NO-GO** — do not proceed. Add each failed item back to UNVERIFIED, re-run the loop to address them, and dispatch the verifier again.
