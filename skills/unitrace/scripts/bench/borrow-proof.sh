#!/usr/bin/env bash
# borrow-proof.sh -- the single promotion gate for the shared-daemon rtinfer
# borrow. Runs the borrow on/off proof for EVERY shared caller across both
# corpora and prints one PASS/FAIL report. Overall PASS is the precondition for
# flipping UNITRACE_DAEMON_RTINFER default on (then, after a soak release,
# removing the flag + locking the swept multiformat winner). See bench/AGENTS.md.
#
# Callers proven here:
#   - search   : search-multiformat-ab.mjs, uds vs rtinfer (+ fail-open arm),
#                on BOTH the synthetic multiformat corpus and the real repo.
#   - trace+nav: trace-ab.mjs, borrow-off vs borrow-on.
#   - enhance  : borrow-callers-ab.mjs, borrow-off vs borrow-on.
#   - websearch: borrow-callers-ab.mjs (opt-in via --with-websearch; live web).
#
# Each child exits nonzero on its own FAIL verdict; this script aggregates and
# exits nonzero if ANY caller/corpus failed. Per-child results land in
# scripts/bench/results/<ts>/.
#
# Usage:
#   borrow-proof.sh [--repo DIR] [--trace-repo DIR] [--repeats N]
#                   [--with-websearch] [--search-only]
#
# Requires: Codex auth (codex login) and a reachable cse-toold / rtinfer endpoint
# (otherwise every borrow arm reports served-rate 0% -> invalid, not PASS).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
command -v node >/dev/null 2>&1 || { echo "error: node not found on PATH" >&2; exit 127; }

REPO="$REPO_ROOT"
TRACE_REPO="${HOME}/__devlocal/kepler"
REPEATS=3
WITH_WEBSEARCH=0
SEARCH_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --trace-repo) TRACE_REPO="$2"; shift 2 ;;
    --repeats) REPEATS="$2"; shift 2 ;;
    --with-websearch) WITH_WEBSEARCH=1; shift ;;
    --search-only) SEARCH_ONLY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Clear stale search daemon sockets so a cold pool never skews the first arm.
rm -f "${HOME}/.unifable/searchd/"*.sock "${HOME}/.unifable/searchd/"*.lock 2>/dev/null || true

declare -a NAMES=()
declare -a CODES=()
overall=0

run_step() {
  local name="$1"; shift
  echo ""
  echo "=== borrow-proof: ${name} ==="
  "$@"
  local code=$?
  NAMES+=("$name")
  CODES+=("$code")
  if [ "$code" -ne 0 ]; then overall=1; fi
}

# 1) search -- synthetic multiformat corpus: uds vs rtinfer + fail-open arm.
run_step "search/multiformat" node "$SCRIPT_DIR/search-multiformat-ab.mjs" \
  --corpus multiformat --variants uds,rtinfer,rtinfer-absent --repeats "$REPEATS"

# 2) search -- real repo corpus: uds vs rtinfer.
run_step "search/unifable" node "$SCRIPT_DIR/search-multiformat-ab.mjs" \
  --corpus unifable --repo "$REPO" --variants uds,rtinfer --repeats "$REPEATS"

if [ "$SEARCH_ONLY" -eq 0 ]; then
  # 3) trace + nav -- borrow off/on.
  if [ -d "$TRACE_REPO" ]; then
    run_step "trace+nav" node "$SCRIPT_DIR/trace-ab.mjs" \
      --repo "$TRACE_REPO" --variants borrow-off,borrow-on --repeats "$REPEATS"
  else
    echo "skip trace+nav: trace repo not found at $TRACE_REPO" >&2
  fi

  # 4) enhance (+ optional websearch) -- borrow off/on.
  callers="enhance"
  [ "$WITH_WEBSEARCH" -eq 1 ] && callers="enhance,websearch"
  run_step "callers/${callers}" node "$SCRIPT_DIR/borrow-callers-ab.mjs" \
    --callers "$callers" --repo "$REPO" --repeats "$REPEATS"
fi

echo ""
echo "================ BORROW PROOF SUMMARY ================"
for i in "${!NAMES[@]}"; do
  if [ "${CODES[$i]}" -eq 0 ]; then verdict="PASS"; else verdict="FAIL"; fi
  printf '  %-22s %s (exit %s)\n' "${NAMES[$i]}" "$verdict" "${CODES[$i]}"
done
echo "====================================================="
if [ "$overall" -eq 0 ]; then
  echo "OVERALL: PASS -- borrow is proven on every caller/corpus run."
  echo "Next: flip UNITRACE_DAEMON_RTINFER default on, soak one release, then remove the flag (bench/AGENTS.md)."
else
  echo "OVERALL: FAIL -- do NOT flip the default. See per-step results above."
fi
exit "$overall"
