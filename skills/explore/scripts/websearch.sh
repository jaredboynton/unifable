#!/usr/bin/env bash
# explore/websearch.sh: default external research via gpt-realtime-2 (websearch-rt.sh).
#
# Usage:
#   websearch.sh "<research goal / task>"
#
# Delegates to websearch-rt.sh with explore reasoning low and submit reasoning minimal.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec env \
  EXPLORE_WS_EXPLORE_REASONING_EFFORT="${EXPLORE_WS_EXPLORE_REASONING_EFFORT:-low}" \
  EXPLORE_WS_SUBMIT_REASONING_EFFORT="${EXPLORE_WS_SUBMIT_REASONING_EFFORT:-minimal}" \
  "$SCRIPT_DIR/websearch-rt.sh" "$@"
