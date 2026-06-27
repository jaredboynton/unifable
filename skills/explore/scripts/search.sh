#!/usr/bin/env bash
# explore/search.sh: fast semantic code search via gpt-realtime-2 + ripgrep.
#
# Locates relevant code using an agentic ripgrep loop with gpt-realtime-2
# (Codex OAuth Realtime WebSocket) as the brain.
# Complement to trace.sh (deep behavioral understanding).
#
# Usage:
#   search.sh "<natural-language query>"
#   search.sh --root /path/to/repo "<query>"
#   search.sh --json "<query>"         # machine-readable [{path,startLine,endLine,content}]
#   search.sh --map-mode pagerank "<query>"
#
# Env:
#   EXPLORE_RT_MODEL                  default: gpt-realtime-2
#   EXPLORE_CODEX_AUTH_PATH           Codex OAuth file (default: ~/.codex/auth.json)
#   EXPLORE_SEARCH_REASONING_EFFORT   default: low
#   EXPLORE_WORKSPACE                 workspace root (default: current dir)
#   EXPLORE_MAP_MODE                  none | pagerank | sigmap | tandem (default: tandem)
#   EXPLORE_MAP_BUDGET                map token budget (default: 1024)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

# Preflight: rg
if ! command -v rg >/dev/null 2>&1; then
  printf 'error: ripgrep (rg) not found on PATH\n' >&2
  printf '  install: brew install ripgrep  (macOS)\n' >&2
  printf '           apt install ripgrep   (Debian/Ubuntu)\n' >&2
  printf '           https://github.com/BurntSushi/ripgrep#installation\n' >&2
  exit 127
fi

# Preflight: JavaScript runtime. Prefer Bun for startup, fall back to Node.
JS_RUNTIME=""
if command -v bun >/dev/null 2>&1; then
  JS_RUNTIME="bun"
elif command -v node >/dev/null 2>&1; then
  JS_RUNTIME="node"
else
  printf 'error: bun or node not found on PATH (required for search-rt.mjs)\n' >&2
  exit 127
fi

# Preflight: Codex auth
CODEX_AUTH="${EXPLORE_CODEX_AUTH_PATH:-${HOME:-$(cd ~ && pwd)}/.codex/auth.json}"
if [ ! -f "$CODEX_AUTH" ]; then
  printf 'error: Codex auth not found at %s\n' "$CODEX_AUTH" >&2
  printf '  run: codex login\n' >&2
  exit 1
fi

if [ "$#" -eq 0 ]; then
  printf 'usage: search.sh [--root DIR] [--json] "<query>"\n' >&2
  exit 2
fi

exec "$JS_RUNTIME" "$SCRIPT_DIR/search-rt.mjs" "$@"
