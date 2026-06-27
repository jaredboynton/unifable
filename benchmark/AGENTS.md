# benchmark - agent notes

## Scope

These rules apply to benchmark harnesses, tasks, fixtures, and summaries.

## Rules

- Keep benchmark fixtures deterministic and separate from production hook logic.
- Do not tune gates to benchmark fixtures without a matching product reason and
  test under `tests/`.
- Preserve task files as evaluation inputs; do not rewrite expected scenarios to
  make a measured result look better.

## Verification

- Harness changes: run the smallest benchmark smoke plus any touched unit tests.
- Result-summary changes: verify against an existing result file before release.
