#!/usr/bin/env bash
# unisearch/unisearch.sh: thin entrypoint that delegates to the shared
# unitrace implementation. The websearch engine (websearch-rt.sh + realtime-
# websearch.mjs + the shared lib/) lives in the sibling `unitrace` skill; this
# skill exists only to give external web research its own discoverable SKILL.md
# and gate-allowlisted entrypoint. There is exactly ONE implementation — no
# duplicated lib, no drift.
#
# Resolution order for the unitrace implementation:
#   1. $UNITRACE_IMPL_DIR                       (explicit override)
#   2. ~/.unifable/current/skills/unitrace      (stable central runtime; normal case)
#   3. <this-skill>/../unitrace                 (sibling in an un-synced tree: git checkout, ~/.agents)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIFABLE_HOME="${UNIFABLE_HOME:-$HOME/.unifable}"

candidates=(
  "${UNITRACE_IMPL_DIR:-}"
  "$UNIFABLE_HOME/current/skills/unitrace"
  "$(cd "$SCRIPT_DIR/../.." && pwd)/unitrace"
)

UNITRACE_DIR=""
for c in "${candidates[@]}"; do
  [ -n "$c" ] || continue
  if [ -x "$c/scripts/websearch-rt.sh" ]; then
    UNITRACE_DIR="$c"
    break
  fi
done

if [ -z "$UNITRACE_DIR" ]; then
  printf 'unisearch: could not locate the unitrace implementation (looked for scripts/websearch-rt.sh under %s, %s/current/skills/unitrace, and the sibling unitrace skill).\n' \
    "${UNITRACE_IMPL_DIR:-<unset UNITRACE_IMPL_DIR>}" "$UNIFABLE_HOME" >&2
  printf '  ensure the unitrace skill is installed (the SessionStart sync seeds ~/.unifable/current/skills).\n' >&2
  exit 1
fi

exec "$UNITRACE_DIR/scripts/websearch.sh" "$@"
