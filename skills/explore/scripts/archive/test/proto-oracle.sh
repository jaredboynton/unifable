#!/usr/bin/env bash
# Reproducible runner for the protobuf oracle test (frontier T3 gate).
# Runs from the cursor-api skill scripts dir so @bufbuild/protobuf + tsx + the
# generated proto resolve; the explore harness itself stays zero-dependency.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURSOR_API="${CURSOR_API_SCRIPTS:-$HOME/.claude/skills/cursor-api/scripts}"
if [ ! -d "$CURSOR_API/node_modules/@bufbuild" ]; then
  printf 'proto-oracle: missing @bufbuild/protobuf under %s (run: cd %s && npm install)\n' "$CURSOR_API" "$CURSOR_API" >&2
  exit 127
fi
cd "$CURSOR_API"
exec node --import tsx "$SCRIPT_DIR/proto-oracle.test.mjs"
