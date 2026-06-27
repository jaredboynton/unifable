# Changelog

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
