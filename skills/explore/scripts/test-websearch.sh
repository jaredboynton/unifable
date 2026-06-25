#!/usr/bin/env bash
# explore/test-websearch.sh: unit tests + preflight for websearch stack.
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

echo "websearch unit tests..."
node --test "$SCRIPT_DIR/test/explore-skill-context.test.mjs"
node --test "$SCRIPT_DIR/test/test-websearch-lib.mjs"

echo "websearch RT offline tests..."
bash "$SCRIPT_DIR/test-websearch-rt.sh"

echo "websearch preflight..."

for script in websearch.sh websearch-rt.sh; do
  if [ ! -x "$SCRIPT_DIR/$script" ]; then
    printf 'error: %s missing or not executable\n' "$script" >&2
    exit 1
  fi
  if ! "$SCRIPT_DIR/$script" --help >/dev/null; then
    printf 'error: %s --help failed\n' "$script" >&2
    exit 1
  fi
  printf 'PASS: %s --help\n' "$script"
done

echo "websearch tests passed"
