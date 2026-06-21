# unifable task runner. Run `just` to list recipes.

# List available recipes.
_default:
    @just --list

# Set the plugin version across all four plugin dirs (plugin.json + marketplace.json)
# and setup/setup.sh, then verify no straggler of the old version remains.
# Usage: just version 1.9.4   (or: just version patch|minor|major)
version VERSION:
    python3 scripts/bump_version.py {{VERSION}}
