#!/usr/bin/env bash
# explore/probe-rt-websearch.sh: probe gpt-realtime-2 Realtime WS for server-side web_search.
#
# Usage:
#   probe-rt-websearch.sh [--headers both] [--mode session-required] [--frames /tmp/probe.ndjson]
#
# Env overrides:
#   EXPLORE_RT_MODEL           Realtime model slug (default: gpt-realtime-2)
#   EXPLORE_CODEX_AUTH_PATH    Codex OAuth file (default: ~/.codex/auth.json)
#   EXPLORE_RT_TIMEOUT         per-run timeout seconds (default: 90)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "$SCRIPT_DIR/probe-rt-websearch.mjs" "$@"
