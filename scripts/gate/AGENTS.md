# scripts/gate - agent notes

## Scope

These rules apply to host-agnostic gate policy, judge clients, ledger state, and
runtime helpers.

## Rules

- Keep this package host-agnostic. Claude/Codex-specific IO belongs in `hooks/`
  or install/setup code.
- Gate internals must fail open on their own bugs and bound enforcement loops
  with explicit caps.
- State writes go through the existing SQLite/WAL helpers or atomic file helpers;
  do not add ad hoc persistence.
- Judge prompts, hook copy, and router text are part of model interaction
  surface; keep them concise, concrete, and covered by tests or generated docs.

## Verification

- Run focused tests for touched modules and `python3 -m py_compile` on edited
  Python files.
- Run `just test-all` before release.
