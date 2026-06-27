# tests - agent notes

## Scope

These rules apply to pytest tests, shell tests, eval harnesses, fixtures, and
test rubrics.

## Rules

- Do not weaken or delete protected tests to make a suite pass.
- New gate behavior needs a failing-first or regression test near the affected
  module.
- Prefer focused tests while iterating, then run `just test-all` before release.
- Keep fixtures deterministic and avoid live network dependencies unless the test
  is explicitly marked for them.

## Verification

- Focused edits: run the touched test file or nearest behavioral group.
- Release edits: run `just test-all`.
