#!/usr/bin/env bash
# explore/search.sh: fast semantic code search via Cerebras gpt-oss-120b + ripgrep.
#
# Locates relevant code in ~1-2s using an agentic ripgrep loop.
# Complement to trace-cursor.sh (deep behavioral understanding via cursor-agent).
#
# Usage:
#   search.sh "<natural-language query>"
#   search.sh --root /path/to/repo "<query>"
#   search.sh --json "<query>"         # machine-readable [{path,startLine,endLine,content}]
#   search.sh --map-mode pagerank "<query>"
#
# Env:
#   CEREBRAS_API_KEY       required; may be provided by ../.env
#   CEREBRAS_BASE_URL      default: https://api.cerebras.ai/v1
#   UNITRACE_SEARCH_MODEL   default: gpt-oss-120b
#   UNITRACE_WORKSPACE      workspace root (default: current dir)
#   UNITRACE_MAP_MODE       none | pagerank | sigmap | tandem (default: tandem)
#   UNITRACE_MAP_BUDGET     map token budget (default: 1024)
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

# Preflight: CEREBRAS_API_KEY
if [ -z "${CEREBRAS_API_KEY:-}" ]; then
  printf 'error: CEREBRAS_API_KEY is not set\n' >&2
  printf '  get a key at https://cloud.cerebras.ai\n' >&2
  exit 1
fi

# Preflight: node
if ! command -v node >/dev/null 2>&1; then
  printf 'error: node not found on PATH (required for search.mjs)\n' >&2
  exit 127
fi

if [ "$#" -eq 0 ]; then
  printf 'usage: search.sh [--root DIR] [--json] "<query>"\n' >&2
  exit 2
fi

exec node "$SCRIPT_DIR/search.mjs" "$@"
