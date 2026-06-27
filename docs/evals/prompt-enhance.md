# Eval: Repo-Grounded Prompt Enhancement

Measures whether the UserPromptSubmit gate injects a repo-grounded "enhanced
prompt" lead for under-specified code asks, and crucially whether it stays OFF
for prompts that are already grounded or operational.

Expected unifable route: `hooks/gate_prompt.py` runs the grade judge and the
enhancer concurrently. When the pre-grade heuristic fires (no path/file token,
>= 20 words, not obviously operational) AND the post-grade verdict confirms
`evidence_profile == "code"` and `mode in {normal, deep}`, the enhancer output
is prepended as the FIRST `additionalContext` line, ahead of the static mode
block from `classify_task.context_for_mode`. The static mode block is the
fallback used whenever the enhancer does not fire or fails open.

The enhancer (`scripts/gate/submit_enhance.py` + the Node entrypoint
`skills/explore/scripts/enhance-prompt.mjs`) reuses the explore skill's
in-repo machinery (retrieveCandidates + mini navigators + one full
gpt-realtime-2 synth). Hard gates: zero repo-specific commands, zero
hallucinated paths (cited ranges filtered by the windows actually retrieved),
char cap 1200, hard timeout 6000 ms, fail-open to the static baseline on any
error.

---

## Automated bench (rationale for the tier + config)

A four-arm bench was run in a temp harness (/tmp/enhance-bench, 2026-06-27)
across a small fixture repo and the unifable repo itself, with four prompts
(vague "stuck script", grounded off-by-one, vague stop-gate, grounded
explain-hook). Arms: lite-full (retrieve + 1 full synth), standard-full
(+4 mini nav), full-full (+8 mini nav), lite-mini (retrieve + 1 mini synth).
Quality was scored by an independent gpt-realtime-2 judge; cited ranges were
validated against retrieved windows (hallucinated-path gate); the enhanced
text was scanned for repo-specific commands.

Two reasoning settings were measured. The first runs used the synth at the
`codex_judge` default `reasoning_effort = "low"` (because no `reasoningEffort`
was passed and `UNIFABLE_JUDGE_REASONING_EFFORT` is unset). The final runs
used `reasoningEffort = "none"` (omitted) — the proven trace-submit config.
Omitted reasoning changed the result materially:

- Omitted fixed the `ok=false` failures (all arms 4/4) and lite-full's
  hallucination (0 across the board). Omitted is more robust, not just faster.
- Omitted exposed lite-full's large-repo collapse: q=3 on both unifable
  prompts (mean 5.8). The full synth at omitted reasoning cannot reason over a
  32-window dump. Nav's pruning to ~5-7 windows is what lets omitted-reasoning
  synthesis score q=9 on real repos.

Per-arm aggregate (synth reasoning OMITTED — production config):

| arm | ok | ms median (p90) | windows | quality mean | hallucinated | repo-cmd |
|---|---|---|---|---|---|---|
| lite-full | 4/4 | 3764 (4348) | 17.3 | 5.8 | 0 | 0 |
| standard-full | 4/4 | 4267 (5142) | 5.3 | 9.0 | 0 | 0 |
| full-full | 4/4 | 3888 (3992) | 5.8 | 8.8 | 0 | 0 |
| lite-mini | 4/4 | 1134 (1219) | 17.3 | 6.0 | 0 | 0 |

Decision: Standard (4 mini nav + full gpt-realtime-2 synth, reasoning omitted).
It is the only tier that holds q=9 across both small and large repos at the
production reasoning config, with 4/4 ok, 0 hallucinated paths, 0 repo-cmd.
Latency median ~4.3 s (p90 ~5.1 s, n=4 noisy); the hook timeout default is
6000 ms and the enhancer runs concurrently with the grade judge, so hook
wall-clock is max(grade, enhance), not their sum.

---

## Sub-scenario A: Vague code ask (enhanced lead expected)

### Test prompt

```
something is off with how the user prompt submit hook assembles the mode
context the model keeps getting weird weak verification guidance and the
mode lines read like conditionals diagnose and fix it
```

### Expected behavior

- Pre-grade heuristic fires (no path/file token, >= 20 words, not operational).
- Grade confirms `evidence_profile == "code"`, `mode in {normal, deep}`.
- The first `additionalContext` line is a grounded enhanced prompt: it names
  concrete `path:line` areas to investigate, drawn from the repo.
- The enhanced text contains NO repo-specific command (`pytest`, `npm test`,
  `just test`, `cargo build`, `go test`, ...). Verification is named by
  category only (a test / typecheck / lint / build that exercises the change).
- Cited `path:line` ranges reference files that exist on disk.
- The static mode block follows the enhanced lead.

### PASS example

```
Investigate how the UserPromptSubmit hook builds and formats the mode context
shown to the model, then tighten the assembly so guidance is consistently
strong. Investigation area 1: ... Start from scripts/gate/classify_task.py:66-78.
Investigation area 2: ... Check hooks/gate_prompt.py:259-295. Run a test that
exercises the change.

After any edit, run one verification command that exercises the change (a test,
typecheck, lint, or build); if none applies, name the reason. The Stop gate
blocks completion until a verification has run for changed files.
Cite evidence for load-bearing claims: path:line for code, cmd -> output ...
```

### FAIL example

```
After any edit, run one relevant verification command or state why none applies.
```

(Static-only fallback fired because the enhancer did not produce a grounded
lead — either it failed open, or the pre-grade heuristic wrongly skipped the
prompt. The vague ask got no repo grounding.)

### Failure signals

- Enhanced lead absent for a vague, >= 20-word code ask (enhancer failed open
  or heuristic misfired).
- Enhanced lead names a `path:line` that does not exist on disk (hallucination;
  the cited-range filter should have dropped it).
- Enhanced lead contains a repo-specific command (`pytest`, `npm test`, ...).
- Enhanced lead restates the user's own words instead of adding codebase
  grounding.
- Enhanced lead present but the grade verdict was operational/quick (post-grade
  gate failed to discard).

---

## Sub-scenario B: Path-grounded ask (enhancer must NOT fire)

### Test prompt

```
Fix the off-by-one in lib/pagination.ts slice helper around the cursor clamp,
and tighten the edge-case test.
```

### Expected behavior

- `has_path_token` is True (`lib/pagination.ts`), so `fire_enhance` is False.
- The enhancer subprocess is NOT launched (no ~4 s spend).
- `additionalContext` is the static mode block only; no enhanced lead.

### PASS example

```
After any edit, run one verification command that exercises the change (a test,
typecheck, lint, or build); if none applies, name the reason. The Stop gate
blocks completion until a verification has run for changed files.
Cite evidence for load-bearing claims ...
```

### Failure signals

- An enhanced lead is injected for a prompt that already names a path/file
  (wasted enhancer call + possible contradictory grounding).
- The enhancer subprocess is launched (observable via latency / daemon pool).

---

## Sub-scenario C: Operational ask (enhancer must NOT fire)

### Test prompt

```
Draft a renewal reply to the account owner about the executive sponsor
escalation, then post it to the customer's Slack channel.
```

### Expected behavior

- `looks_operational` is True (Slack / account owner / executive sponsor /
  renewal), so `fire_enhance` is False.
- The enhancer subprocess is NOT launched.
- `additionalContext` is the static mode block (operational profile) only.

### Failure signals

- An enhanced lead is injected for an operational ask (the enhancer would
  retrieve code windows irrelevant to a research/drafting task and produce a
  code-focused lead that misdirects the model).

---

## Env knobs

- `UNIFABLE_PROMPT_ENHANCE=0` disables the enhancer entirely (static baseline
  for every prompt). Default `1` (on).
- `UNIFABLE_PROMPT_ENHANCE_TIMEOUT_MS` caps the subprocess wall-clock (default
  6000); beyond it the enhancer is killed and the static baseline is used.
- `UNIFABLE_PROMPT_ENHANCE_NAV` sets the mini navigator count (default 4; the
  bench-decided Standard tier).
- `UNIFABLE_PROMPT_ENHANCE_MODEL` sets the synth model (default gpt-realtime-2).
- `EXPLORE_AST_SKIP_INSTALL=1` (set by the hook) makes retrieval use line-window
  hydration instead of installing ast-grep on the critical path; the bench used
  this and scored q=9.
