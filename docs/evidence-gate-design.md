# Evidence-Lockdown Gate — design

Goal: make citation a *precondition to acting*, not an after-the-fact check. Until the model has
documented verifiable evidence (code `path:line`, tool `command -> output`, research/prior-art URL),
only read/research tools and an explicit script allowlist are permitted; mutating/action tools are
blocked at the PreToolUse boundary. This closes the hole a Stop-side detector leaves open — where the
agent thrashes through edits first and only "cites" at the end.

## Why a PreToolUse lockdown (prior art + in-repo substrate)

- Prior art: `why-was-fable-banned` (https://github.com/SihyeonJeon/why-was-fable-banned) implements
  exactly this shape — a `PreToolUse` hook "intercepts every edit and exits 2 until the spec passes"
  (README "How it works"). Its spec demands restated goal, context chosen by authority, >=2 rejected
  alternatives with the boundary each breaks, risks, and *runnable acceptance*. Measured in-repo:
  "Adversarial / edge gate tests (downgrade, bypass, malformed, no-brick): 35/35 pass"; decision record
  per change goes from "none" to "enforced"; unspeced/forbidden-path edits go from "possible" to
  "blocked" (README "Benchmarks"). Rules were derived from 42 recorded real engineering sessions
  generalized to 8 decision axes (README "Where the rules came from"). This validates the pattern;
  note wfb gates *edits only*, not Bash — gating Bash (below) is beyond the proven prior art.
- In-repo substrate (we extend, not greenfield): `hooks/pre_tool_use.py:157-190` is a PreToolUse
  gate that blocks `WRITE_TOOLS` until a spec validates (unconditional — no env disable, fail-open
  on malformed input). `scripts/gate/spec.py:116-168` validates `restated_goal` +
  `acceptance_criteria[{check, evidence}]` and rejects placeholder evidence via `FAKE_MARKERS`
  (`spec.py:78-99`); HEAVY adds `constraints` + `>=2 rejected_alternatives`.
  `scripts/gate/classify_task.py:75` maps quick->LIGHT / normal->STANDARD / deep->HEAVY. The
  lockdown is three deltas on this.

## The two phases

- RESEARCH phase (locked) — the default once a work-shaped task at grade >= STANDARD starts.
  - ALLOWED: `Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, read-only MCP
    (exa/tavily/ref/octocode search+fetch). `Bash` is allowed ONLY for `ls`, `glob`, `rg`, or running
    any file named `trace.sh` (see Bash gating).
  - BLOCKED: `Edit`/`Write`/`MultiEdit`/`NotebookEdit`/`apply_patch`, `Task`/`Agent`, and non-whitelisted `Bash`, each with a
    message naming the unlock step.
- ACTION phase (unlocked) — once the evidence artifact validates, all tools allowed (the existing spec
  gate's pass condition, now richer). Citations sync from ledger activity automatically
  (`sync_citations_from_activity` in `scripts/gate/citations.py`); task checks run on Stop
  (`auto_validate_spec` in `scripts/gate/spec.py`). Agent-facing CLI: `unifable restate`,
  `unifable add-task`, and `unifable dispute`.

## Delta 1 — broaden the locked surface (pre_tool_use.py)

Today guard 2 only fires for `tool_name in WRITE_TOOLS`. Add an evidence-gate guard that also
fires for `Bash` (classified, below) and treats the spec as the unlock. Reuse `_block()` (exit 2)
and the grade machinery. LIGHT (quick) waives entirely. The gate is unconditional — there is no
env disable.

## Delta 2 — citation fields in the unlock predicate (spec.py SPEC_SCHEMA)

Add, so the spec literally *is* the three evidence types:
- `repo_context`: list of `path:line` strings the model read (CODE evidence). Validate each matches
  `\S+:\d+`; optionally existence-check the file. Required >=1 at STANDARD+.
- `prior_art`: list of source URLs/repo refs (RESEARCH evidence). Required >=1 at HEAVY (architecture).
- `acceptance_criteria[{check, evidence}]` — TOOL-OUTPUT evidence (already present; FAKE_MARKERS enforced).
- `rejected_alternatives` (HEAVY, already present) — DECISION evidence.
Validation extends `validate_spec` (`spec.py:116`) with these; the contract strings
(`spec.py:216-237`) gain the new fields so the model is told exactly what to fill.

## Delta 3 — Bash gating (the novel, risky part)

Classify the `Bash` command string in the research phase:
- ALLOW only if every command segment is `ls`, `glob`, `rg`, or invokes a file whose basename is
  `trace.sh` (directly or through `sh`/`bash`/`zsh`) by absolute or repo-relative path.
- BLOCK otherwise, with a message that names the allowed commands and says broader Bash unlocks only
  after a valid task spec exists with repo_context citations, acceptance evidence, and prior_art.
- Safety: this is an ALLOWLIST (block-by-default in research phase) — higher false-positive risk than
  wfb's edit-only gate. Mitigations: (a) unconditional + fail-open on malformed input (matches
  `pre_tool_use.py:206-209`); (b) the model always retains a full research toolset, so it is never
  bricked — it can still inspect with `ls`/`glob`/`rg` and use `trace.sh`; (c) ship behind the holdout harness
  (`scripts/shadow/` + `UNIFABLE_HOLDOUT=1`) and measure block-rate / false-positive-rate.

## Groundedness breaker — provisional lift

Separate from the evidence spec gate: the overconfidence breaker (`scripts/gate/groundedness.py`)
can arm on an unproven load-bearing claim and block mutations. Three release paths:

- **Full disarm** — release judge finds the claim grounded, retracted, or no longer load-bearing.
- **Provisional lift** — the model is pursuing the verification the breaker requested (reads cited,
  docs fetched, minimal experiment-setup edit) but is not yet fully grounded. Mutations are allowed
  within `lift_scope` (scoped to USER GOAL, including minimal scripts/checks when execution is
  required); the block cap is paused. The hook notifies the model why the lift was granted.
- **FAIL_OPEN** — after `BREAKER_MAX_BLOCKS` consecutive blocks on one arm (default 3).

While provisionally lifted, a monitor judge runs on mutation PreToolUse and the release judge also
runs (so a read/fetch that fully grounds the claim can disarm before the next mutation). **Minor
scope drift** yields an advisory hint only (`Hint (advisory, not a gate): …`) — the lift stays
open. **Egregious** unrelated work re-arms the breaker. Full disarm while lifted still applies when
the claim is grounded (including empirical validation in tool output), retracted, or no longer
load-bearing.

## Advisory judge hints — guidance, never a gate

The judge can also emit a **non-blocking hint**: one concrete next step for an agent that looks
stuck or is making poor judgement (e.g. validate-task looping on a check that references a
nonexistent file). The load-bearing invariant is that a hint is the *opposite* of a gate — it
**never** changes a verdict, changes a task status, or opens/lifts the completion breaker. It is
advisory context only, surfaced on a distinct `UNIFABLE_MODEL_HINT` channel
(`scripts/gate/model_notify.py`) labelled "advisory, not a gate" so the agent cannot mistake it
for an instruction. Three surfaces, all fail-open (a judge error yields no hint and leaves gate
behavior byte-identical):

- **Hint field on the verdict** — `judge_task` / `judge_dispute` carry an optional `hint` alongside
  `verdict`/`reason` (`scripts/gate/spec.py`). No extra judge call; it rides the call already made.
- **Stop completion-breaker loop** — once the agent has re-blocked Stop `COMPLETION_HINT_THRESHOLD`
  times (`hooks/gate_stop.py`, counter `completion_stop_blocks` in the ledger), one `judge_hint`
  call appends a nudge to the still-blocking reason. The block is unchanged; only guidance is added.
- **Repeated-failure loop** — when PostToolUse sees the same failure class repeat
  (`hooks/gate_post_tool.py`), the deterministic detection triggers a `judge_hint` call; the
  guidance itself is reasoned by the judge, never a canned string. Silent if the judge has nothing.

The proactive loops are threshold-bounded (mirroring `BREAKER_MAX_BLOCKS`) so they never spend a
judge call per tool. `judge_hint` (`scripts/gate/spec.py`) is verdict-free by construction: its
schema returns only `hint`, so it structurally cannot resolve a task. Locked by
`tests/test_judge_hint.py` (a hint with `verdict=0` keeps the task failed and the breaker closed).

## Rollout

Unconditional (always on, no env disable), fail-open on malformed input. Graded: LIGHT waives;
STANDARD = repo_context + acceptance_criteria; HEAVY adds prior_art + rejected_alternatives. Both
hosts: Claude native hooks + Codex (same `pre_tool_use.py`, both list `apply_patch`). Tests:
extend `tests/test_spec_gate.py` + add a Bash-classifier test (allow/block matrix, malformed,
no-brick), mirroring wfb's 35/35 adversarial set.

## Open forks for the user

1. Bash gating scope: full lockdown now (writes + Bash allowlist, the stated intent, highest leverage,
   highest false-positive risk) vs. writes-first then add Bash gating after measuring.
2. Allowlist contents: currently `ls`, `glob`, `rg`, and any file named `trace.sh`.
