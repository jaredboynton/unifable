# bin - agent notes

## Scope

These rules apply to executable entrypoints installed or invoked by users.

## Rules

- Keep wrappers thin; core behavior belongs in `scripts/gate/` or setup/install
  modules.
- Preserve executable intent and argument compatibility for existing commands.
- Do not embed secrets, absolute local paths, or machine-specific defaults.

## Verification

- Run the touched command with `--help` or the closest safe smoke path.
- For shell wrappers, run `bash -n` when applicable.
