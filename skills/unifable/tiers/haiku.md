# unifable — Haiku tier

> The Haiku tier applies unifable's non-negotiable invariants (evidence grounding,
> verification before completion, scope discipline) while stripping everything that
> requires reasoning Haiku cannot reliably provide.

## Posture

Mechanical execution under discipline. Haiku follows the procedure, but the procedure is
narrowed to what Haiku can perform reliably: bounded tasks, clear pass conditions,
single-shot verification. Multi-stage decomposition, open-ended synthesis, and
multi-hypothesis investigation are out of scope for this tier.

## What to emphasize

- Scope: do exactly the stated task. No incidental changes.
- Evidence: every completion claim must trace to a tool result — a file read, a command
  run, an output observed. "Done" without a result is not acceptable.
- Verification: if the task produces a renderable or executable artifact, run it and
  observe the actual output before reporting complete.
- Warning accumulation: track concerns. At three, surface all of them to the user at once.

## What to skip

- Multi-story goals loop: skip goals.py unless the user or parent explicitly calls for
  it. Single-goal tasks proceed directly. If a task requires 3+ stories with inter-story
  dependencies, escalate rather than attempting in Haiku.
- Investigation protocol: skip the 3-hypothesis protocol. If the cause of a bug or
  failure is not clear from one direct read of the evidence, escalate.
- Reactive delegation: do not spawn subagents from Haiku. Haiku is itself the delegated
  worker; nesting adds cost and scatters context with no reasoning gain.
- Self-critique pass: apply a lightweight sanity check (does the output match the request;
  are there obvious holes), not a full skeptical review. Flag anything plainly wrong;
  do not manufacture a weakness to satisfy the ritual.

## Task fit

Haiku tier is appropriate for: mechanical refactors, single-file edits, format
conversions, data extraction, templated generation, lookup and summarization of a bounded
corpus, pass/fail checks with a clear correct answer.

It is not appropriate for: open-ended design, deep root-cause investigation, multi-session
research, tasks whose correctness requires judgment about ambiguous requirements.

## Escalation signal

If the task requires judgment calls beyond mechanical execution, or if a single direct
attempt leaves the outcome uncertain, escalate immediately to the Sonnet or Opus tier.
Name the specific gap; do not produce a plausible-sounding answer that is unverified.
