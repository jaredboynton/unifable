#!/usr/bin/env bash
# Install the unifable entrypoints into ~/.local/bin.
# Usage: install-bin.sh <plugin-root>
#
# Preferred: seed a stable runtime under ~/.unifable (decoupled from the versioned
# plugin cache) and link ~/.local/bin at it, so a later upgrade that deletes the old
# cache dir can never dangle these entrypoints (the exit-127 bug). SessionStart keeps
# ~/.unifable current thereafter. Falls back to direct cache symlinks when runtime_sync
# is unavailable (e.g. an older plugin build).
set -euo pipefail

ROOT="${1:-${CLAUDE_PLUGIN_ROOT:-}}"
[ -n "$ROOT" ] || { echo "unifable: install-bin.sh needs plugin root"; exit 1; }
SRC="$ROOT/bin/unifable"
HOOK="$ROOT/bin/unifable-hook"
LEGACY="$ROOT/bin/unifable-spec"
[ -f "$SRC" ] || { echo "unifable: missing $SRC"; exit 1; }
[ -f "$HOOK" ] || { echo "unifable: missing $HOOK"; exit 1; }

BINDIR="${UNIFABLE_BIN_DIR:-$HOME/.local/bin}"
SYNC="$ROOT/scripts/gate/runtime_sync.py"

# Preferred path: seed ~/.unifable from this version and link ~/.local/bin at the stable
# bootstraps. `-e` follows the new symlink to confirm a working entrypoint was produced.
if [ -f "$SYNC" ] \
  && UNIFABLE_BIN_DIR="$BINDIR" python3 "$SYNC" --source "$ROOT" >/dev/null 2>&1 \
  && [ -e "$BINDIR/unifable-hook" ]; then
  echo "  ✓ unifable runtime seeded under ${UNIFABLE_HOME:-$HOME/.unifable} and linked into $BINDIR"
  exit 0
fi

# Fallback: direct symlinks into the versioned cache (legacy; dangles on cache cleanup).
mkdir -p "$BINDIR"
ln -sf "$SRC" "$BINDIR/unifable"
ln -sf "$HOOK" "$BINDIR/unifable-hook"
ln -sf "$LEGACY" "$BINDIR/unifable-spec"
chmod +x "$SRC" "$HOOK" "$LEGACY"
echo "  ✓ unifable → $BINDIR/unifable"
echo "  ✓ unifable-hook → $BINDIR/unifable-hook"
echo "  ✓ unifable-spec → $BINDIR/unifable-spec (legacy alias)"
