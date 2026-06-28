#!/usr/bin/env bash
# explore/probe-alpha-search.sh: Codex alpha/search standalone web.run probe.
#
# Env: UNITRACE_SEARCH_MODEL, UNITRACE_CODEX_AUTH_PATH
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "$SCRIPT_DIR/probe-alpha-search.mjs" "$@"
