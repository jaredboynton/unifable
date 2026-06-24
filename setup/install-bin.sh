#!/usr/bin/env bash
# Install unifable into ~/.local/bin (idempotent symlinks).
# Usage: install-bin.sh <plugin-root>
set -euo pipefail

ROOT="${1:-${CLAUDE_PLUGIN_ROOT:-}}"
[ -n "$ROOT" ] || { echo "unifable: install-bin.sh needs plugin root"; exit 1; }
SRC="$ROOT/bin/unifable"
HOOK="$ROOT/bin/unifable-hook"
LEGACY="$ROOT/bin/unifable-spec"
[ -f "$SRC" ] || { echo "unifable: missing $SRC"; exit 1; }
[ -f "$HOOK" ] || { echo "unifable: missing $HOOK"; exit 1; }

BINDIR="${UNIFABLE_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BINDIR"
ln -sf "$SRC" "$BINDIR/unifable"
ln -sf "$HOOK" "$BINDIR/unifable-hook"
ln -sf "$LEGACY" "$BINDIR/unifable-spec"
chmod +x "$SRC" "$HOOK" "$LEGACY"
echo "  ✓ unifable → $BINDIR/unifable"
echo "  ✓ unifable-hook → $BINDIR/unifable-hook"
echo "  ✓ unifable-spec → $BINDIR/unifable-spec (legacy alias)"
