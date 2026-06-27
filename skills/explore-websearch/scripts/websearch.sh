#!/usr/bin/env bash
# explore-websearch/websearch.sh: thin entrypoint that delegates to the shared
# explore implementation. The websearch engine (websearch-rt.sh + realtime-
# websearch.mjs + the shared lib/) lives in the sibling `explore` skill; this
# skill exists only to give external web research its own discoverable SKILL.md
# and gate-allowlisted entrypoint. There is exactly ONE implementation — no
# duplicated lib, no drift.
#
# Resolution order for the explore implementation:
#   1. $EXPLORE_IMPL_DIR                       (explicit override)
#   2. ~/.unifable/current/skills/explore      (stable central runtime; normal case)
#   3. <this-skill>/../explore                 (sibling in an un-synced tree: git checkout, ~/.agents)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIFABLE_HOME="${UNIFABLE_HOME:-$HOME/.unifable}"

candidates=(
  "${EXPLORE_IMPL_DIR:-}"
  "$UNIFABLE_HOME/current/skills/explore"
  "$(cd "$SCRIPT_DIR/../.." && pwd)/explore"
)

EXPLORE_DIR=""
for c in "${candidates[@]}"; do
  [ -n "$c" ] || continue
  if [ -x "$c/scripts/websearch-rt.sh" ]; then
    EXPLORE_DIR="$c"
    break
  fi
done

if [ -z "$EXPLORE_DIR" ]; then
  printf 'explore-websearch: could not locate the explore implementation (looked for scripts/websearch-rt.sh under %s, %s/current/skills/explore, and the sibling explore skill).\n' \
    "${EXPLORE_IMPL_DIR:-<unset EXPLORE_IMPL_DIR>}" "$UNIFABLE_HOME" >&2
  printf '  ensure the explore skill is installed (the SessionStart sync seeds ~/.unifable/current/skills).\n' >&2
  exit 1
fi

exec "$EXPLORE_DIR/scripts/websearch.sh" "$@"
