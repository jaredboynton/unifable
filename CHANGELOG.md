# Changelog

## 1.12.2 - 2026-06-27

- Removed `unifable doctor` CLI subcommand and the session-env validation
  protocol (`docs/session-env-validation.md`, `scripts/measure_session_env.py`).

Verification:

- `python3 -m pytest tests/test_spec_canonical_root.py tests/test_spec_facade_api.py tests/test_session_resolve.py -q`
- `just test-all`

## 1.12.1 - 2026-06-27

- Made judge directive/steering file references lossless via pointer + host
  rehydration, fixing head-truncated filenames in hook feedback (e.g.
  `research_bash_guidance.py` surfaced as `ash_guidance.py`,
  `groundedness_facade_api.py` as `ness_facade_api.py`). The judge now receives a
  numbered FILE INDEX of the paths it already saw and references a file by its
  index in double brackets (`[[n]]`); `scripts/gate/file_refs.py` rehydrates the
  pointer to the exact path in `breaker_judges.arm_judge` before directive and
  steering are parsed. The model emits an integer, never a path string, so
  truncation is impossible by construction. Mirrors the explore skill's READ
  INDEX / `excerpt_index` pointer-submit pattern
  (`skills/explore/scripts/lib/rt-rehydrate-submit.mjs`).

Verification:

- `python3 -m pytest tests/test_file_refs.py tests/test_director.py -q` (26 passed)
- Live gpt-realtime-2 validation under recall pressure: pointer adoption 16/16,
  every directive rehydrated to full untruncated paths, 0 truncations.

## 1.12.0 - 2026-06-27

- Tightened the stepwise director's `directive` and arm-path `steering` schema
  descriptions (`scripts/gate/breaker_prompts.py`) so hook feedback is
  self-contained and immediately executable: name the actual path, read-only
  command, or check inline, and NEVER reference a spec task by ID (`T1`, "the
  spec board's listed checks") that the model would have to look up. Fixes
  pointer-style directives like "use the spec board's T1 check" that gave the
  model a label instead of a runnable next action.
- Added repo-grounded prompt enhancement at UserPromptSubmit. For
  under-specified code asks (no path/file token, >= 20 words, not obviously
  operational), a Standard-tier enhancer — `retrieveCandidates` seed + 4
  parallel `gpt-realtime-mini` navigators + one full `gpt-realtime-2` synth
  (reasoning omitted, the proven trace-submit config) — runs concurrently with
  the grade judge and its output is prepended as the first `additionalContext`
  line, ahead of the static mode block. Hook wall-clock is max(grade, enhance),
  not their sum.
- Rewrote the normal/deep mode lines in `classify_task.context_for_mode` to
  trigger-anchored directives that name the verification category and the
  Stop-gate consequence (was a leading "If files change" conditional). This
  block is the fallback used whenever the enhancer does not fire or fails open.
- Hard gates on the enhanced text: zero repo-specific commands (reject ->
  static fallback), zero hallucinated paths (cited ranges filtered by the
  windows actually retrieved), char cap 1200, 6000 ms subprocess timeout,
  fail-open to the static baseline on any error. `UNIFABLE_PROMPT_ENHANCE=0`
  disables (default on).
- New `scripts/gate/submit_enhance.py` (host-agnostic policy, stdlib only) +
  `skills/explore/scripts/enhance-prompt.mjs` (Node entrypoint reusing the
  explore skill's in-repo machinery) + `tests/test_submit_enhance.py` (policy
  unit tests) + `tests/test_grade_and_enhance.py` (concurrent wiring tests) +
  `docs/evals/prompt-enhance.md`.

Verification:

- Bench: four-arm (/tmp/enhance-bench, 2026-06-27) across a fixture repo and
  the unifable repo with the synth at reasoning omitted. Standard scored
  quality 9.0 across all four prompts, 4/4 ok, 0 hallucinated paths, 0
  repo-specific commands; the lite (no-nav) tier collapsed to quality 3 on the
  large repo at omitted reasoning. See `docs/evals/prompt-enhance.md`.
- `python3 -m pytest tests/test_submit_enhance.py tests/test_grade_and_enhance.py -q` (34 passed)
- `just test-all` (pytest 1226 passed + eval_gate_proof + test_gate_robustness 14/14 + audit_waits 46/46)
- `python3 -m py_compile hooks/gate_prompt.py scripts/gate/submit_enhance.py scripts/gate/classify_task.py`
- `python3 scripts/generate_docs.py --check` (generated docs current)
- Director fix: `python3 -m pytest tests/test_director.py -q` (15 passed).
  Live judge benchmark (gpt-realtime-2, 2 scenarios x 3 wording variants): the
  shipped wording inlines the concrete command in every directive/steering and
  drops the baseline's pointer phrasing, with no latency change (~2.3-2.6 s per
  call across all variants).

## 1.11.1 - 2026-06-27

- Fixed explore repo-map prefetch hangs on large trees by replacing the final
  PageRank definition ranking pass with an O(E + D) file-rank accumulation path
  and adding over-cap graph guards.
- Added a non-git huge-tree bail-out for `generateMapText` so home/cache-like
  directories return an empty prefetch map quickly unless explicitly overridden.
- Added PageRank equivalence, over-cap skip, dense-graph linearity, and
  huge-tree bail tests plus benchmark notes for the selected ranking approach.

Verification:

- `node --test skills/explore/scripts/test/*.test.mjs` (128 passed)
- `just test-all`

## 1.11.0 - 2026-06-27

- Parallelized PostToolUse judge calls (reconcile, discover, disarm, hint) under a
  single wall-clock budget via `posttool_judges.py` daemon-thread fan-out, with
  the host PostToolUse hook timeout raised to 120s.
- Delta-merge spec updates: reconcile and discover return pure deltas applied under
  `update_spec()`'s lock (hygiene first, then reconcile, then frontier) so parallel
  siblings cannot lose citation or task-board mutations.
- Atomic session-level coalesce for reconcile+discover on a dedicated
  `posttool_claims` table (SQLite schema v3), keyed by the same spec key as the
  evidence board; unreliable turn epochs fail open to running judges rather than
  suppressing work.
- Atomic HEAVY frontier counters (`posttool_frontier_counters`) replace
  last-writer-wins ledger bumps for `frontier_research_tools` /
  `frontier_discovery_count`.
- Bounded fail-open when the judge orchestrator fails: no sequential four-judge
  fallback that could exceed the host timeout.
- Frontier dedup in `apply_frontier_additions()` uses repo `_normalize_title()` /
  `_norm_title_conflicts()` instead of raw casefold.
- Realtime daemon pool dispatch counts queued outbox work plus in-flight holders
  when picking workers and checking overload, so a first-burst of concurrent judge
  requests spreads across the warm socket pool instead of piling onto worker 0.

Verification:

- `python3 -m pytest tests/test_posttool_parallel.py tests/test_posttool_timeout_budget.py tests/test_judge_daemon_routing.py tests/test_db.py -q`
- `just test-all` (1186+ passed, 9 subtests passed; 40/40 gate scenarios; 14/14 robustness checks)

## 1.10.2 - 2026-06-27

- Fixed the judge transcript renderer dropping Codex record bodies: `_record_text`
  in `transcript_tail.py` now falls back to top-level `payload` text
  (`response_item` / `event_msg` / `turn_context`, including `result.Ok.content`),
  mirroring unifusion's `codexPayloadText`. Codex proof is no longer invisible to
  the groundedness disarm judge, which was causing an unsatisfiable
  "claim still ungrounded" loop until fail-open.
- Hardened the breaker disarm prompt: `needed` must point only at artifacts
  already visible in the transcript segment and must not invent file paths or
  internal record types (turn_context, world_state, event_msg), which the judge
  was hallucinating and steering toward records the renderer cannot expose.
- Allowed `jq` (read-only JSON processor) in the pre-spec Bash research whitelist,
  both standalone and as a pipeline sink.
- Allowed read-only shell loops/conditionals (`for`/`while`/`until`/`if`/`case`)
  in the research whitelist: control keywords are stripped and the loop is allowed
  only when every command in its body is itself whitelisted. Command-substitution
  and dangerous-assignment guards are unchanged.
- Stopped truncating load-bearing Spec-update headlines: judge task titles,
  retraction reasons, and frontier rationales now reach the model whole, and a
  multi-task reconcile no longer silently drops headlines past the fourth
  (new `build_spec_update_context` helper).

Verification:

- `python3 -m pytest tests/test_transcript_tail.py tests/test_bash_classify.py tests/test_research_bash_guidance.py tests/test_spec_headlines_not_truncated.py tests/test_judge_transcript.py -q`
- `just test-all` (1153 passed, 9 subtests passed; 40/40 gate scenarios; 14/14 robustness checks)

## 1.10.1 - 2026-06-27

- Stopped emitting cite-only PostToolUse `additionalContext` after deterministic
  citation sync. Reads and fetches still update `repo_context` / `prior_art`, but
  the model no longer spends context on "synced N cite(s)" bookkeeping.
- Suppressed empty quick-task prompt context, so a waived task that needs no
  guidance emits `{}` instead of a generic "keep it concise" reminder.
- Kept PostToolUse state updates for real action signals: failures, breaker
  status, spec CLI feedback, and judge frontier/reconcile updates still surface
  when they require model action.
- Simplified reconcile revision headlines to `Tn revised: ...` and preserved the
  full judge reason instead of truncating it.

Verification:

- `python3 -m pytest tests/test_posttool_context_dedup.py tests/test_spec_state_notifications.py tests/test_citation_sync.py tests/test_hook_token_dedup.py tests/test_spec_reconcile.py -q` (36 passed)
- `just test-all` (1130 passed, 9 subtests passed)

## 1.10.0 - 2026-06-27

- Centralized the explore skill into the stable runtime: added `skills` to
  `runtime_sync._RUNTIME_TREE`, so `~/.unifable/current/skills/{explore,
  explore-websearch}` is seeded from the newest plugin version on every
  SessionStart and resolves the same way regardless of which CLI or plugin
  cache invoked the plugin.
- Split external web research into its own discoverable `explore-websearch`
  skill. Its `websearch.sh` is a thin delegate to the shared `explore`
  implementation (one engine, no duplicated lib): it resolves the explore copy
  from `~/.unifable/current/skills/explore`, then a sibling fallback.
- Pointed the research-Bash gate resolver at the central runtime first
  (`research_bash_guidance._DEFAULT_EXPLORE_ROOTS`), so `trace.sh` / `search.sh`
  / `websearch.sh` resolve from `~/.unifable/current` and survive removal of the
  legacy hand-maintained `~/.agents/skills/explore` copy.
- Rewrote `skills/explore/SKILL.md` to document the central path and the
  trace+search scope; external research now points at `explore-websearch`.

Verification:

- `python3 -m pytest tests/test_research_bash_guidance.py tests/test_bash_classify.py tests/test_runtime_sync.py -q` (157 passed)
- Force-seeded a throwaway runtime and confirmed `~/.unifable/current/skills/explore/scripts/trace.sh` and `explore-websearch/scripts/websearch.sh` resolve with `~/.agents` absent.
- Live e2e from the central path: `trace.sh`, `search.sh`, and the `explore-websearch` `websearch.sh` shim all returned grounded output (exit 0).

## 1.9.123 - 2026-06-27

- Synced judge transcript rendering with patchpress tool-use formatting: compact
  Edit/Write diffs, EditResult diffLines parsing, and age-based tool-output
  compression (default observation masking) via `transcript_tail.py`,
  `tool_use_format.py`, and `tool_output_compress.py`.
- Vendored patchpress 0.6.x compaction into the Unifusion skill
  (`tool-use-format.mjs`, supporting modules, updated `compact-full-transcript.mjs`)
  while preserving ATIF/Codex adapters and `UNIFUSION_TRANSCRIPT` resolution.
- Extended Skill-tool parsing in `breaker_filters.py` for `@@tool Skill` blocks;
  added optional Unifusion summarizer compression env knobs.

Verification:

- `python3 -m pytest tests/test_tool_use_format.py tests/test_judge_transcript.py tests/test_groundedness_breaker.py tests/test_transcript_retention.py -q`
- `bash skills/unifusion/scripts/selfcheck.sh`
- `python3 -m py_compile scripts/gate/tool_use_format.py scripts/gate/tool_output_compress.py scripts/gate/transcript_tail.py`

## 1.9.122 - 2026-06-27

- Moved groundedness breaker restriction copy out of judge-authored steering and
  into deterministic hook-owned output.
- Added canonical hook-visible tool restriction constants covering inspection,
  write, delegation, and shell/REPL surfaces.
- Tightened groundedness judge prompts so they ask for exact grounding actions
  while the hook appends the exact `Actions restricted to:` list.
- Extended regression coverage for stale restriction stripping, manifest matcher
  sync, generated judge prompts, and REPL/exec_command breaker blocking.

Verification:

- `python3 -m pytest -q`
- `python3 scripts/generate_docs.py --check`

## 1.9.121 - 2026-06-27

- Trimmed startup, PreToolUse, Stop, and completion-handoff hook wording so the
  model sees concrete next actions without stale breaker or blocked-tool claims.
- Updated judge and director prompts for impossibility evidence, provisional loop
  release, tool-scope guidance, heavy-workflow adoption, and generated reference
  examples.
- Narrowed pack router triggers and regenerated Claude/Codex hook output plus
  judge prompt references.
- Added scoped `AGENTS.md` and `CLAUDE.md` guidance across the repo, including
  release mechanics and changelog requirements.

Verification:

- `python3 scripts/generate_docs.py --check`
- `git diff --check`
- `python3 -m py_compile hooks/gate_prompt.py hooks/gate_prompt_effort.py hooks/gate_stop.py scripts/gate/context_block.py scripts/gate/pretool_block.py scripts/gate/heavy_workflow.py scripts/gate/spec_judge.py scripts/gate/breaker_prompts.py scripts/gate/loop_release.py scripts/gate/completion_handoff.py scripts/generate_docs.py`
- `just test-all`
