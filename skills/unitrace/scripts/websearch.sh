#!/usr/bin/env bash
# explore/websearch.sh: default external research via gpt-realtime-2 (websearch-rt.sh).
#
# Usage:
#   websearch.sh "<research goal / task>"
#
# Delegates to websearch-rt.sh with search/fetch reasoning low and submit low.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec env \
  UNISEARCH_WS_UNITRACE_REASONING_EFFORT="${UNISEARCH_WS_UNITRACE_REASONING_EFFORT:-low}" \
  UNISEARCH_WS_SUBMIT_REASONING_EFFORT="${UNISEARCH_WS_SUBMIT_REASONING_EFFORT:-low}" \
  "$SCRIPT_DIR/websearch-rt.sh" "$@"
