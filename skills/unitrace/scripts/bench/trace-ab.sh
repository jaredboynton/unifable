#!/usr/bin/env bash
# trace-ab.sh — thin wrapper for the trace A/B harness (scripts/bench/trace-ab.mjs).
#
# Usage:
#   trace-ab.sh [--repo DIR] [--prompts FILE] [--variants a,b,c] [--repeats N] [--prompt-id ID]
#
# Defaults: repo=~/__devlocal/kepler, prompts=scripts/bench/trace-kepler-prompts.json,
# all known variants, 2 repeats. Results land in scripts/bench/results/<timestamp>/.
#
# Tip: clear stale daemon sockets and allow warm time before a fresh matrix:
#   rm -f ~/.unifable/searchd/*.sock ~/.unifable/searchd/*.lock
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v node >/dev/null 2>&1 || { echo "error: node not found on PATH" >&2; exit 127; }
exec node "$SCRIPT_DIR/trace-ab.mjs" "$@"
