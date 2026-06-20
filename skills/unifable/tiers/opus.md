# unifable — Opus / Fable tier

> This is the default tier. The base SKILL.md targets Opus (and Fable). Read it first;
> this file is a posture addendum, not a replacement.

## Posture

Opus is the full-procedure tier. Every section of SKILL.md applies without reduction.

- Multi-story loop (§1): always run goals.py for 2+ sequential stories. The final story
  must carry `--verify-cmd` and `--verify-evidence`; the engine refuses completion without
  them.
- Investigation protocol (§2): form 3+ competing hypotheses. Carry each to an evidence
  result before discarding. Report which hypotheses were rejected and why.
- Verification grounding (§3): run every renderable artifact in the real renderer. A
  static parse is not observation.
- Escalation and delegation (§4): when a bounded hard slice is genuinely blocking after
  two attempts, package it as a background Workflow or Agent call with the full evidence
  set — repro, attempts, failure point, specific sub-question. Resume with the result as
  authoritative. Reactive delegation for a stuck slice only; never trigger on risk
  classification alone.

## What to emphasize

- Multi-stage planning: write the stage map before touching anything. Each stage must
  produce one verifiable artifact. Update the map when evidence invalidates a plan.
- Parallel delegation: when independent sub-parts exist and the runtime exposes the Agent
  tool, spawn them concurrently. Brief each with: task, expected output, save location,
  relevant prior context.
- Warning accumulation: track minor concerns across the run. At three, surface all of
  them at once before continuing.
- Self-critique: before delivery, read as a skeptical reviewer. Hunt for a real weakness.
  If none exists, say so plainly.

## What to skip

Nothing. This tier runs the full procedure.

## Escalation signal

If stuck on the same problem 2+ consecutive times, or if the task requires out-of-spec
discovery beyond the available tools, escalate: first recommend `/effort xhigh`; then
delegate the stuck slice via Agent/Workflow; then hand off to a human with the evidence
package. Report the limit honestly — do not paper over it.
