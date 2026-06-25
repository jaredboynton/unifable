#!/usr/bin/env bash
# explore/probe-rt-web-run.sh: Realtime web_run -> Codex /responses web_search bridge probe.
#
# Env: EXPLORE_RT_MODEL, EXPLORE_SEARCH_MODEL, EXPLORE_SEARCH_REASONING, EXPLORE_CODEX_AUTH_PATH
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "$SCRIPT_DIR/probe-rt-web-run.mjs" "$@"
