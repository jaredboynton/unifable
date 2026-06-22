#!/usr/bin/env bash
# Install unifable into ~/.local/bin (idempotent symlinks).
# Usage: install-bin.sh <plugin-root>
set -euo pipefail

ROOT="${1:-${CLAUDE_PLUGIN_ROOT:-}}"
[ -n "$ROOT" ] || { echo "unifable: install-bin.sh needs plugin root"; exit 1; }
SRC="$ROOT/bin/unifable"
LEGACY="$ROOT/bin/unifable-spec"
[ -f "$SRC" ] || { echo "unifable: missing $SRC"; exit 1; }

BINDIR="${UNIFABLE_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BINDIR"
ln -sf "$SRC" "$BINDIR/unifable"
ln -sf "$LEGACY" "$BINDIR/unifable-spec"
chmod +x "$SRC" "$LEGACY"
echo "  ✓ unifable → $BINDIR/unifable"
echo "  ✓ unifable-spec → $BINDIR/unifable-spec (legacy alias)"
