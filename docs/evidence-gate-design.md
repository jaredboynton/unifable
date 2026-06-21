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
- In-repo substrate (we extend, not greenfield): `hooks/pre_tool_use.py:143-198` is already a PreToolUse
  gate that blocks `WRITE_TOOLS` until a spec validates (opt-in `UNIFABLE_SPEC_GATE=1`, default off,
  fail-open). `scripts/gate/spec.py:116-168` validates `restated_goal` + `acceptance_criteria[{check,
  evidence}]` and rejects placeholder evidence via `FAKE_MARKERS` (`spec.py:78-99`); HEAVY adds
  `constraints` + `>=2 rejected_alternatives`. `scripts/gate/classify_task.py:75` maps
  quick->LIGHT / normal->STANDARD / deep->HEAVY. The lockdown is three deltas on this.

## The two phases

- RESEARCH phase (locked) — the default once a work-shaped task at grade >= STANDARD starts.
  - ALLOWED: `Read`, `Grep`, `Glob`, `Task`/`Agent` (subagents), `WebSearch`, `WebFetch`, read-only MCP
    (exa/tavily/ref/octocode search+fetch), and writing the evidence artifact itself
    (`.unifable/spec/<task>.json`, already permitted by `pre_tool_use.py:73-79`). `Bash` is allowed ONLY
    for read-only/research commands + an explicit script allowlist (see Bash gating).
  - BLOCKED: `Edit`/`Write`/`MultiEdit`/`NotebookEdit`/`apply_patch` and mutating `Bash`, each with a
    message naming the unlock step.
- ACTION phase (unlocked) — once the evidence artifact validates, all tools allowed (the existing spec
  gate's pass condition, now richer).

## Delta 1 — broaden the locked surface (pre_tool_use.py)

Today guard 2 only fires for `tool_name in WRITE_TOOLS`. Add an evidence-gate guard (opt-in
`UNIFABLE_EVIDENCE_GATE=1`) that also fires for `Bash` (classified, below) and treats the spec as the
unlock. Reuse `_block()` (exit 2) and the grade machinery. LIGHT (quick) waives entirely.

## Delta 2 — citation fields in the unlock predicate (spec.py SPEC_SCHEMA)

Add, so the spec literally *is* the three evidence types:
- `must_read`: list of `path:line` strings the model read (CODE evidence). Validate each matches
  `\S+:\d+`; optionally existence-check the file. Required >=1 at STANDARD+.
- `prior_art`: list of source URLs/repo refs (RESEARCH evidence). Required >=1 at HEAVY (architecture).
- `acceptance_criteria[{check, evidence}]` — TOOL-OUTPUT evidence (already present; FAKE_MARKERS enforced).
- `rejected_alternatives` (HEAVY, already present) — DECISION evidence.
Validation extends `validate_spec` (`spec.py:116`) with these; the contract strings
(`spec.py:216-237`) gain the new fields so the model is told exactly what to fill.

## Delta 3 — Bash gating (the novel, risky part)

Classify the `Bash` command string in the research phase:
- ALLOW if it is read-only/research: leading verb in {cat, ls, head, tail, grep, rg, find, wc, stat,
  file, tree, git log, git diff, git status, git show, git blame}, OR it invokes an allowlisted script
  (e.g. `*/trace.sh`, fusion's script, `*/explore/*.sh`) by absolute or repo-relative path.
- BLOCK otherwise (writes/installs/builds/network-mutating/destructive), with: "evidence gate: action
  tools are locked until `.unifable/spec/<task>.json` documents your evidence (must_read path:line,
  acceptance_criteria with live output, prior_art URL for HEAVY). Research with Read/Grep/web first."
- Safety: this is an ALLOWLIST (block-by-default in research phase) — higher false-positive risk than
  wfb's edit-only gate. Mitigations: (a) opt-in + default OFF + fail-open (matches
  `pre_tool_use.py:206-209`); (b) the model always retains a full research toolset, so it is never
  bricked — it can always satisfy the gate; (c) `WFB_BYPASS`-style one-off env escape; (d) ship behind
  the holdout harness (`scripts/shadow/` + `UNIFABLE_HOLDOUT=1`) and measure block-rate / false-positive-rate
  before any default-on.

## Rollout

Opt-in `UNIFABLE_EVIDENCE_GATE=1`, default OFF, fail-open — zero effect on existing sessions until
enabled. Graded: LIGHT waives; STANDARD = must_read + acceptance_criteria; HEAVY adds prior_art +
rejected_alternatives. Both hosts: Claude native hooks + Codex (same `pre_tool_use.py`, both list
`apply_patch`). Tests: extend `tests/test_spec_gate.py` + add a Bash-classifier test (allow/block
matrix, malformed, bypass, no-brick), mirroring wfb's 35/35 adversarial set.

## Open forks for the user

1. Bash gating scope: full lockdown now (writes + Bash allowlist, the stated intent, highest leverage,
   highest false-positive risk) vs. writes-first then add Bash gating after measuring.
2. Default: opt-in + measure (recommended, matches repo's fail-open history) vs. default-on.
3. Allowlist contents: confirm the read-only verb set + which scripts (trace.sh, fusion, others).
