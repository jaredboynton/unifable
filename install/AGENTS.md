# install - agent notes

## Scope

These rules apply to host-specific install scripts.

## Rules

- Keep installer behavior idempotent and reversible.
- Do not persist stale generated context blocks; install/update paths should
  remove old managed blocks before writing new ones.
- Preserve separate Claude and Codex host assumptions instead of mixing host
  setup into a shared script unless the behavior is truly common.

## Verification

- Run shell syntax checks on touched scripts.
- For install-flow changes, use a temporary home or fixture path when possible.
