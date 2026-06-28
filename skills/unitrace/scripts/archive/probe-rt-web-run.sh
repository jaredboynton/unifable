#!/usr/bin/env bash
# explore/probe-rt-web-run.sh: Realtime web_run -> Codex /responses web_search bridge probe.
#
# Env: UNITRACE_RT_MODEL, UNITRACE_SEARCH_MODEL, UNITRACE_SEARCH_REASONING, UNITRACE_CODEX_AUTH_PATH
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "$SCRIPT_DIR/probe-rt-web-run.mjs" "$@"
