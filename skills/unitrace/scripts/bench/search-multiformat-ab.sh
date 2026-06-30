#!/usr/bin/env bash
# search-multiformat-ab.sh - thin wrapper for the multi-format search borrow
# PROOF GATE (scripts/bench/search-multiformat-ab.mjs).
#
# Usage:
#   search-multiformat-ab.sh [--corpus multiformat|unifable|<dir>] \
#       [--queries FILE] [--repo DIR] [--variants a,b] [--repeats N] \
#       [--concurrency N] [--debug]
#
# Defaults: corpus=multiformat, variants=uds,rtinfer, repeats=3, concurrency=1.
# Results land in scripts/bench/results/<ts>/{raw.json,summary.md}; the script
# exits nonzero on a FAIL verdict so it can gate a default flip.
#
# Needs Codex auth (codex login) and a reachable daemon or rtinfer endpoint.
# A `rtinfer` arm with served-rate < 90% means no cse-toold was reached; the
# verdict marks the run invalid rather than passing on a silent UDS fallthrough.
#
# Tip: clear stale search daemon sockets before a fresh matrix:
#   rm -f ~/.unifable/searchd/*.sock ~/.unifable/searchd/*.lock
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v node >/dev/null 2>&1 || { echo "error: node not found on PATH" >&2; exit 127; }
exec node "$SCRIPT_DIR/search-multiformat-ab.mjs" "$@"
