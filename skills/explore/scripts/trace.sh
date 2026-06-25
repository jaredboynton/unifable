#!/usr/bin/env bash
# explore/trace.sh: default deep codebase trace via gpt-realtime-2 (trace-rt.sh).
#
# Usage:
#   trace.sh "How does authentication flow through this service?"
#
# Delegates to trace-rt.sh with explore reasoning low and submit reasoning minimal.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec env \
  EXPLORE_RT_EXPLORE_REASONING_EFFORT="${EXPLORE_RT_EXPLORE_REASONING_EFFORT:-low}" \
  EXPLORE_RT_SUBMIT_REASONING_EFFORT="${EXPLORE_RT_SUBMIT_REASONING_EFFORT:-minimal}" \
  "$SCRIPT_DIR/trace-rt.sh" "$@"
