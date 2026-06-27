# docs - agent notes

## Scope

These rules apply to design docs, audits, generated references, and eval docs.

## Rules

- Keep docs current-state oriented; avoid historical narrative that belongs in
  git history or the changelog.
- Do not duplicate the root hook table or product overview; link to `README.md`
  when broad context is needed.
- Generated references under `docs/generated/` are script output; update the
  generator or fixture source, then regenerate.

## Verification

- For generated docs: run `python3 scripts/generate_docs.py --check`.
- For design docs: verify referenced paths, commands, and hook names still exist.
