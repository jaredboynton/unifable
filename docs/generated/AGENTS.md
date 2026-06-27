# docs/generated - agent notes

## Scope

These files are generated hook and judge references.

## Rules

- Do not hand-edit generated reference files.
- Change `scripts/generate_docs.py` or the source hook/judge data, then
  regenerate.
- Generated output must stay deterministic across two consecutive runs.

## Verification

- Run `python3 scripts/generate_docs.py`.
- Then run `python3 scripts/generate_docs.py --check`.
