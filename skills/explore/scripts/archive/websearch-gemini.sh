#!/usr/bin/env bash
# explore/websearch-gemini.sh: external research via Gemini 3.5 Flash (agy CLI + Exa MCP).
#
# Extensively searches external documentation, research papers, and GitHub prior art
# for a given task/goal. Complement to trace.sh (in-repo behavioral understanding)
# and search.sh (fast in-repo ripgrep).
#
# Usage:
#   websearch-gemini.sh "<research goal / task>"
#
# Env:
#   EXPLORE_AGY_MODEL          default: Gemini 3.5 Flash (Low)
#   EXPLORE_AGY_NO_MODEL       set to 1 to omit --model (use agy default)
#   EXPLORE_AGY_TIMEOUT        per-run budget in seconds (default: 600)
#   EXPLORE_AGY_BIN            agy binary override
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

case "${1:-}" in
  --help|-h)
    awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

if ! command -v node >/dev/null 2>&1; then
  printf 'error: node not found on PATH (required for websearch-gemini.mjs)\n' >&2
  exit 127
fi

if ! command -v agy >/dev/null 2>&1; then
  printf 'error: agy CLI not found on PATH (install Antigravity CLI)\n' >&2
  exit 127
fi

if ! command -v script >/dev/null 2>&1; then
  printf 'error: script(1) not found on PATH (required for agy stdout capture)\n' >&2
  exit 127
fi

exec node "$SCRIPT_DIR/websearch-gemini.mjs" "$@"
