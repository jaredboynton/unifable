#!/usr/bin/env bash
# explore/probe-alpha-search-fetch.sh: test alpha/search search_query then commands.open.
#
# Usage:
#   probe-alpha-search-fetch.sh [--mode search-then-open|search-only|open-only|combined]
#
# Env: UNITRACE_SEARCH_MODEL, UNITRACE_CODEX_AUTH_PATH, UNISEARCH_ALPHA_TRANSPORT, UNISEARCH_ALPHA_OPEN_CAP
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "$SCRIPT_DIR/probe-alpha-search-fetch.mjs" "$@"
