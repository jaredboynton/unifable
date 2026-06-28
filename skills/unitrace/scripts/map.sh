#!/usr/bin/env bash
# explore/map.sh: token-budgeted repo map prefetch (pagerank, sigmap, tandem).
#
# Usage:
#   map.sh [--root DIR] [--mode pagerank|sigmap|tandem|none] [--budget TOKENS] "<query>"
#
# Env:
#   UNITRACE_MAP_MODE     default: tandem
#   UNITRACE_MAP_BUDGET   default: 1024 (token estimate)
#   UNITRACE_WORKSPACE    workspace root (default: current dir)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v node >/dev/null 2>&1; then
  printf 'error: node not found on PATH (required for map.mjs)\n' >&2
  exit 127
fi

exec node "$SCRIPT_DIR/map.mjs" "$@"
