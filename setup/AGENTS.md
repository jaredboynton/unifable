# setup - agent notes

## Scope

These rules apply to setup, uninstall, and install-bin scripts.

## Rules

- Setup must keep managed blocks fresh and remove stale generated blocks.
- Keep global runtime refresh behavior aligned with `hooks/session_start.py` and
  `scripts/gate/runtime_sync.py`.
- Version strings in `setup/setup.sh` are managed by `just version`; do not
  hand-edit them.

## Verification

- Run shell syntax checks on touched scripts.
- Run version consistency checks after setup/version edits.
