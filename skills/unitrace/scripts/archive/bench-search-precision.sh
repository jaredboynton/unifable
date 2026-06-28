#!/usr/bin/env bash
# explore/bench-search-precision.mjs — compare search precision across map modes.
#
# Usage:
#   bench-search-precision.sh [--offline-only] [--mock-search]
#   UNITRACE_BENCH_LIVE=1 bench-search-precision.sh   # includes integration tasks + live Cerebras
#
# Env:
#   CEREBRAS_API_KEY          required for live search (not mock)
#   UNITRACE_BENCH_UNIFABLE    default: ~/__devlocal/unifable
#   UNITRACE_BENCH_TASKS       tasks JSONL path
#   UNITRACE_BENCH_RUNS        runs per task/mode (default: 1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

if ! command -v node >/dev/null 2>&1; then
  printf 'error: node not found on PATH\n' >&2
  exit 127
fi

ARGS=()
if [ "${UNITRACE_BENCH_LIVE:-}" != "1" ]; then
  ARGS+=(--offline-only)
fi

if [ "${UNITRACE_BENCH_MOCK:-}" = "1" ]; then
  ARGS+=(--mock-search)
elif [ -z "${CEREBRAS_API_KEY:-}" ] && [ "${UNITRACE_BENCH_LIVE:-}" = "1" ]; then
  printf 'error: CEREBRAS_API_KEY required for live bench (or set UNITRACE_BENCH_MOCK=1)\n' >&2
  exit 1
fi

exec node "$SCRIPT_DIR/bench-search-precision.mjs" "${ARGS[@]}" "$@"
