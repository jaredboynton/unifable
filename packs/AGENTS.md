# packs - agent notes

## Scope

These rules apply to pack routing manifests and inline discipline packs.

## Rules

- Route triggers must be narrow enough to avoid surprising context injection.
- Pack copy should describe concrete behavior and avoid broad model-behavior
  catchalls.
- When routing semantics change, update generated docs and routing tests
  together.

## Verification

- Run `python3 -m pytest tests/test_pack_router.py -q`.
- Run `python3 scripts/generate_docs.py --check` when generated samples change.
