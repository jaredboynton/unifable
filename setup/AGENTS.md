# setup - agent notes

## Scope

These rules apply to the uninstall script (`setup/uninstall.sh`). The bin
install and runtime seeding are owned by `scripts/gate/runtime_sync.py` (run by
the SessionStart hook and the `install/*.sh` tails); there is no `setup.sh`.

## Rules

- Keep uninstall idempotent and reversible; back up the host memory file before
  stripping legacy blocks.
- Keep the bin-removal set in sync with `runtime_sync._BOOTSTRAPS`
  (`unifable`, `unifable-hook`, `unifable-spec`, `unifusion`).
- Keep global runtime refresh behavior aligned with `hooks/session_start.py` and
  `scripts/gate/runtime_sync.py`.

## Verification

- Run shell syntax checks on touched scripts (`bash -n setup/uninstall.sh`).
