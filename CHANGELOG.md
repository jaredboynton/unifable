# Changelog

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
