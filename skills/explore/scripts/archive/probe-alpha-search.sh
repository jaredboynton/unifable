#!/usr/bin/env bash
# explore/probe-alpha-search.sh: Codex alpha/search standalone web.run probe.
#
# Env: EXPLORE_SEARCH_MODEL, EXPLORE_CODEX_AUTH_PATH
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "$SCRIPT_DIR/probe-alpha-search.mjs" "$@"
