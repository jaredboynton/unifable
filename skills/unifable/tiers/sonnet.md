# unifable — Sonnet tier

> The Sonnet tier keeps the core unifable discipline intact while shedding the heaviest
> Opus-specific machinery. It is the balanced default: stronger than Haiku on synthesis
> and review, cheaper than Opus on cost and latency.

## Posture

Run the full procedure on tasks that warrant it. Skip the scaffolding on tasks that don't.
The discipline earns its cost only when a one-shot attempt would plausibly miss something.

- Multi-story loop (§1): use goals.py for 2+ sequential stories. Keep story count lean;
  avoid decomposing trivial tasks into a ceremony of checkpoints. Final story still requires
  `--verify-cmd` and `--verify-evidence`.
- Investigation protocol (§2): form at least 2 competing hypotheses (3 if the problem
  space is wide). Gather one evidence result per hypothesis before discarding. Report
  rejected hypotheses.
- Verification grounding (§3): run renderable artifacts in the real renderer. No
  reduction here — a static parse is not observation regardless of tier.
- Escalation (§4): when blocked after two attempts, escalate to the Opus tier or flag
  the limit to the user. Do not delegate to a further Sonnet subagent by default; a
  single delegated worker runs its stages sequentially and does not nest.

## What to emphasize

- Scope discipline: stay within the stated task. No incidental refactors, no expanding
  the surface because a neighboring thing looks improvable.
- Evidence grounding: every completion claim must trace to a tool result in this session.
  "I reviewed it and it looks right" is not a check.
- Warning accumulation: track minor concerns. At three, surface all of them at once
  before continuing.
- Self-critique before delivery: confirm problems exist before flagging them. Grep, diff,
  run, or read the source. An unverified flag manufactures doubt where none is warranted.

## What to skip

- Deep reactive delegation: if the Agent tool is available and a sub-part is genuinely
  independent, spawning one concurrent worker is fine. Multi-level nesting is not —
  keep delegation one level deep and keep concurrent agent count small.
- Exhaustive multi-path investigation: 2 solid hypotheses with evidence are sufficient for
  most Sonnet-tier problems; reserve the 3+ hypothesis protocol for genuinely ambiguous
  root-cause work.

## Escalation signal

If the task requires open-ended synthesis where detail itself is the deliverable, or if
you have been stuck on the same sub-problem twice, recommend escalating to the Opus tier.
Name the specific blocker; do not paper over it with a plausible-sounding but unverified
answer.
