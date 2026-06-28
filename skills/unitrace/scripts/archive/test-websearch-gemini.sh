#!/usr/bin/env bash
# explore/test-websearch-gemini.sh: agy live integration for websearch-gemini.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

if ! command -v agy >/dev/null 2>&1; then
  printf 'warn: agy not on PATH; gemini live run skipped\n' >&2
  exit 0
fi

if ! command -v script >/dev/null 2>&1; then
  printf 'error: script(1) not on PATH\n' >&2
  exit 1
fi

if [ "${UNISEARCH_WEBSEARCH_LIVE:-}" != "1" ]; then
  echo "websearch-gemini live integration skipped (set UNISEARCH_WEBSEARCH_LIVE=1 to run)"
  exit 0
fi

out="$(UNITRACE_AGY_TIMEOUT=120 "$SCRIPT_DIR/websearch-gemini.sh" "What is the Model Context Protocol (MCP)? Cite the official spec URL and one reference implementation.")"
if ! printf '%s' "$out" | grep -qiE 'model context protocol|mcp'; then
  printf 'live websearch-gemini failed: answer missing expected topic\n' >&2
  printf '%s\n' "$out" >&2
  exit 1
fi
if ! printf '%s' "$out" | grep -qE 'https?://'; then
  printf 'live websearch-gemini failed: answer missing URL citations\n' >&2
  printf '%s\n' "$out" >&2
  exit 1
fi
printf 'PASS: live websearch-gemini returned cited findings\n'
