#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hermetic-home.sh
. "$SCRIPT_DIR/hermetic-home.sh"

RUN_ROOT="${TMPDIR:-/tmp}/explore-hermetic-home.$$"
REAL_HOME="$RUN_ROOT/real"
BASE_DIR="$RUN_ROOT/cache/explore"
HERMETIC_DIR="$(explore_hermetic_home_dir "$BASE_DIR")"

cleanup() {
  rm -rf "$RUN_ROOT"
}
trap cleanup EXIT

mkdir -p "$REAL_HOME/.cursor" "$BASE_DIR"
printf '{"token":"fake-test-auth"}\n' > "$REAL_HOME/.cursor/auth.json"
printf '{"state":"fake"}\n' > "$REAL_HOME/.cursor/agent-cli-state.json"

resolved="$(explore_ensure_hermetic_home "$REAL_HOME" "$HERMETIC_DIR")"
[ "$resolved" = "$HERMETIC_DIR" ]
[ -L "$HERMETIC_DIR/.cursor/auth.json" ]
[ "$(readlink "$HERMETIC_DIR/.cursor/auth.json")" = "$REAL_HOME/.cursor/auth.json" ]
[ -L "$HERMETIC_DIR/.cursor/agent-cli-state.json" ]
if [ "$(uname -s)" = "Darwin" ] && [ -d "$REAL_HOME/Library/Keychains" ]; then
  [ -L "$HERMETIC_DIR/Library/Keychains" ]
  [ "$(readlink "$HERMETIC_DIR/Library/Keychains")" = "$REAL_HOME/Library/Keychains" ]
fi
[ ! -e "$HERMETIC_DIR/.claude" ]
[ ! -e "$HERMETIC_DIR/.cursor/skills-cursor" ]
[ ! -e "$HERMETIC_DIR/.cursor/plugins" ]
[ ! -e "$HERMETIC_DIR/.cursor/cli-config.json" ]
grep -q '"hooks":' "$HERMETIC_DIR/.cursor/hooks.json"

EXPLORE_HERMETIC_HOME=1
resolved_home="$(explore_resolve_cursor_home "$BASE_DIR" "$REAL_HOME")"
[ "$resolved_home" = "$HERMETIC_DIR" ]

EXPLORE_HERMETIC_HOME=0
resolved_home="$(explore_resolve_cursor_home "$BASE_DIR" "$REAL_HOME")"
[ "$resolved_home" = "$REAL_HOME" ]

EXPLORE_CURSOR_HOME="$RUN_ROOT/explicit"
resolved_home="$(explore_resolve_cursor_home "$BASE_DIR" "$REAL_HOME")"
[ "$resolved_home" = "$RUN_ROOT/explicit" ]

echo "hermetic-home regression passed"
