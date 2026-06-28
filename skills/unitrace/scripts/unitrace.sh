#!/usr/bin/env bash
# unitrace/unitrace.sh: default deep codebase trace via gpt-realtime-2 (trace-rt.sh).
#
# Usage:
#   unitrace.sh "How does authentication flow through this service?"
#
# Delegates to trace-rt.sh with explore reasoning low and submit reasoning minimal.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec env \
  UNITRACE_RT_UNITRACE_REASONING_EFFORT="${UNITRACE_RT_UNITRACE_REASONING_EFFORT:-low}" \
  UNITRACE_RT_SUBMIT_REASONING_EFFORT="${UNITRACE_RT_SUBMIT_REASONING_EFFORT:-minimal}" \
  "$SCRIPT_DIR/trace-rt.sh" "$@"
