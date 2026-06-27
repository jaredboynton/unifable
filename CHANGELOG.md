# Changelog

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
