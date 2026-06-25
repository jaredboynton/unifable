#!/usr/bin/env bash
# bench-websearch.sh — quality + latency eval for websearch-gemini.sh (agy + Exa MCP).
#
# Usage:
#   bench-websearch.sh [--mock]
#
# Env:
#   EXPLORE_BENCH_WEBSEARCH_TASKS   tasks JSONL path
#   EXPLORE_BENCH_WEBSEARCH_RUNS    runs per task (default: 1)
#   EXPLORE_BENCH_WEBSEARCH_MOCK=1  mock output (no agy)
#   EXPLORE_AGY_TIMEOUT             per-query timeout (default 600s)
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
if [ "${EXPLORE_BENCH_WEBSEARCH_MOCK:-}" = "1" ]; then
  ARGS+=(--mock)
else
  if ! command -v agy >/dev/null 2>&1; then
    printf 'error: agy not found on PATH (or set EXPLORE_BENCH_WEBSEARCH_MOCK=1)\n' >&2
    exit 127
  fi
  if ! command -v script >/dev/null 2>&1; then
    printf 'error: script(1) not found on PATH\n' >&2
    exit 127
  fi
fi

exec node "$SCRIPT_DIR/bench-websearch.mjs" "${ARGS[@]}" "$@"
