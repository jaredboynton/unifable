#!/usr/bin/env bash
# explore/test-search.sh: unit tests for search-lib + optional live Cerebras integration.
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

echo "search unit tests..."
node --test "$SCRIPT_DIR/test-search-lib.mjs"
node --test "$SCRIPT_DIR/test/map-sigmap.test.mjs"
node --test "$SCRIPT_DIR/test/map-ast-extract.test.mjs"
node --test "$SCRIPT_DIR/test/map-pagerank.test.mjs"
node --test "$SCRIPT_DIR/test/ast-context.test.mjs"
node --test "$SCRIPT_DIR/test/search-seed.test.mjs"

if [ "${EXPLORE_SEARCH_LIVE:-}" != "1" ]; then
  echo "search live integration skipped (set EXPLORE_SEARCH_LIVE=1 to run)"
  echo "search tests passed"
  exit 0
fi

if [ -z "${CEREBRAS_API_KEY:-}" ]; then
  printf 'error: CEREBRAS_API_KEY required for live integration\n' >&2
  exit 1
fi

if ! command -v rg >/dev/null 2>&1; then
  printf 'error: rg required for live integration\n' >&2
  exit 127
fi

UNIFABLE="${EXPLORE_SEARCH_LIVE_REPO:-$HOME/__devlocal/unifable}"
if [ ! -d "$UNIFABLE" ]; then
  printf 'error: live repo not found: %s\n' "$UNIFABLE" >&2
  exit 1
fi

assert_hits() {
  local label="$1"
  local query="$2"
  local out
  out="$(cd "$UNIFABLE" && "$SCRIPT_DIR/search.sh" "$query")"
  if printf '%s' "$out" | grep -q 'No relevant code found'; then
    printf 'live search failed (empty): %s\n' "$label" >&2
    printf '%s\n' "$out" >&2
    exit 1
  fi
  if ! printf '%s' "$out" | grep -qE 'hooks/|scripts/gate/'; then
    printf 'live search failed (no gate paths): %s\n' "$label" >&2
    printf '%s\n' "$out" >&2
    exit 1
  fi
  printf 'live PASS: %s\n' "$label"
}

assert_hits "citation cross-check" \
  "How does citation cross-check work in pre_tool_use? Where are phantom citations credited from subagent transcripts?"

assert_hits "disputes fail-open" \
  "Where do disputes get adjudicated on Stop? fail-open behavior for gate bugs"

assert_hits "frontier adoption" \
  "How does frontier adoption resolution work in evidence spec and heavy workflow? Where is rejected_approach vs solution state?"

neg="$(cd "$UNIFABLE" && "$SCRIPT_DIR/search.sh" "where is the quantum_flux_capacitor module implemented")"
if ! printf '%s' "$neg" | grep -q 'No relevant code found'; then
  printf 'live negative control failed\n' >&2
  printf '%s\n' "$neg" >&2
  exit 1
fi
printf 'live PASS: negative control\n'

echo "search tests passed"
