# scripts - agent notes

## Scope

These rules apply to top-level scripts that support versioning, docs, tests, and
repo maintenance.

## Rules

- Keep top-level scripts orchestration-focused; enforcement policy belongs under
  `scripts/gate/`.
- Version changes must use `scripts/bump_version.py` through `just version`.
- Generated-doc changes should be made in the generator or source data, not by
  editing generated Markdown.
- Scripts must avoid hidden network or credential dependencies unless the command
  name and docs make that dependency clear.

## Verification

- Run targeted unit tests for touched scripts.
- For version/doc scripts, run their check mode or a dry-run equivalent when
  available.
