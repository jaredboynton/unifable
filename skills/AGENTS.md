# skills - agent notes

## Scope

These rules apply to bundled skills under this repo.

## Rules

- Skills are advisory. Do not make a skill the only mechanism for behavior that
  must be enforced.
- Keep each skill self-contained with a clear `SKILL.md`, local references, and
  local scoped agent notes when the implementation is nontrivial.
- Do not store secrets in skill files, memory files, fixtures, or examples.

## Verification

- For skill behavior changes, run the skill's local smoke or targeted tests.
- For enforcement behavior, add or update root hook/gate tests instead.
