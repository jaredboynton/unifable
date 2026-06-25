#!/usr/bin/env bash
# Run the protobuf oracle test (T3 gate). The test cross-checks the zero-dep
# hand-rolled codec against @bufbuild/protobuf, which lives in the cursor-api
# skill (with tsx to load the generated .ts proto). The explore repo itself
# stays zero-dep; this dev-only test borrows bufbuild+tsx from cursor-api.
set -euo pipefail
CURSOR_API="${CURSOR_API_SCRIPTS:-/Users/jaredboynton/.claude/skills/cursor-api/scripts}"
ORACLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/proto-oracle.test.mjs"
if [ ! -d "$CURSOR_API/node_modules/@bufbuild/protobuf" ]; then
  echo "oracle: bufbuild not found in $CURSOR_API (run npm install there)" >&2
  exit 127
fi
cd "$CURSOR_API"
exec node --import tsx "$ORACLE"
