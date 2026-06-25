#!/usr/bin/env bash
# Compare trace-cursor.sh wall clock under hermetic HOME with sandbox disabled vs enabled.
#
# Usage (from the repo you want traced, usually the explore skill root):
#   ~/.agents/skills/explore/scripts/bench-trace-sandbox.sh
#
# Env:
#   EXPLORE_BENCH_QUERY     trace question (default: repo-local explore skill question)
#   EXPLORE_BENCH_RUNS      paired runs per mode (default: 4)
#   EXPLORE_BENCH_WORKSPACE workspace root (default: pwd)
#   EXPLORE_HERMETIC_HOME   passed through (default: 1 via trace-cursor.sh)
#   CURSOR_AUTH_TOKEN       optional; else reads ~/.cursor/auth.json accessToken
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hermetic-home.sh
. "$SCRIPT_DIR/hermetic-home.sh"
explore_apply_hermetic_default

EXPLORE_BENCH_RUNS="${EXPLORE_BENCH_RUNS:-4}"
EXPLORE_BENCH_WORKSPACE="${EXPLORE_BENCH_WORKSPACE:-$PWD}"
EXPLORE_BENCH_QUERY="${EXPLORE_BENCH_QUERY:-How does this skill split fast search from deep trace, and which scripts implement search.sh versus trace.sh end to end?}"

if ! [[ "$EXPLORE_BENCH_RUNS" =~ ^[0-9]+$ ]] || [ "$EXPLORE_BENCH_RUNS" -lt 1 ]; then
  printf 'bench-trace-sandbox: EXPLORE_BENCH_RUNS must be a positive integer\n' >&2
  exit 2
fi

command -v cursor-agent >/dev/null 2>&1 || {
  printf 'bench-trace-sandbox: cursor-agent not found on PATH\n' >&2
  exit 127
}

explore_real="$(explore_real_home)"
explore_base="$(cd "$explore_real/.cache/explore" 2>/dev/null && pwd || true)"
if [ -z "$explore_base" ]; then
  mkdir -p "$explore_real/.cache/explore"
  explore_base="$(cd "$explore_real/.cache/explore" && pwd)"
fi
explore_ensure_hermetic_home "$explore_real" "$(explore_hermetic_home_dir "$explore_base")" >/dev/null

if [ -z "${CURSOR_AUTH_TOKEN:-}" ] && [ -f "$explore_real/.cursor/auth.json" ]; then
  CURSOR_AUTH_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["accessToken"])' "$explore_real/.cursor/auth.json")"
  export CURSOR_AUTH_TOKEN
fi

BENCH_ROOT="${TMPDIR:-/tmp}/explore-trace-sandbox-bench-$$"
RESULTS="$BENCH_ROOT/results.tsv"
mkdir -p "$BENCH_ROOT"
printf 'mode\twall_s\tok\tout_bytes\n' > "$RESULTS"

run_one() {
  local mode="$1"
  local n="$2"
  local label="${mode}-${n}"
  local runs_dir="$BENCH_ROOT/$label"
  local run_dir
  local start end elapsed out_bytes ok=0

  mkdir -p "$runs_dir"
  start="$(python3 -c 'import time; print(time.time())')"
  if env -u CURSOR_CONVERSATION_ID -u CURSOR_AGENT \
    EXPLORE_HERMETIC_HOME="${EXPLORE_HERMETIC_HOME:-1}" \
    EXPLORE_SANDBOX="$mode" \
    EXPLORE_WORKSPACE="$EXPLORE_BENCH_WORKSPACE" \
    EXPLORE_RUNS_DIR="$runs_dir/runs" \
    EXPLORE_RUN_ID="$label" \
    EXPLORE_FORMAT=raw \
    "$SCRIPT_DIR/trace-cursor.sh" "$EXPLORE_BENCH_QUERY" >"$runs_dir/stdout" 2>"$runs_dir/stderr"; then
    ok=1
  fi
  end="$(python3 -c 'import time; print(time.time())')"
  elapsed="$(python3 -c "print(round($end - $start, 2))")"
  run_dir="$runs_dir/runs/$label"
  out_bytes=0
  if [ -f "$run_dir/out.md" ]; then
    out_bytes="$(wc -c < "$run_dir/out.md" | tr -d ' ')"
    [ -s "$run_dir/out.md" ] || ok=0
  else
    ok=0
  fi
  printf '%s\t%s\t%s\t%s\n' "$mode" "$elapsed" "$ok" "$out_bytes" >> "$RESULTS"
}

printf 'bench-trace-sandbox\n'
printf 'workspace: %s\n' "$EXPLORE_BENCH_WORKSPACE"
printf 'query: %s\n' "$EXPLORE_BENCH_QUERY"
printf 'paired runs per mode: %s (interleaved disabled/enabled)\n\n' "$EXPLORE_BENCH_RUNS"

i=1
while [ "$i" -le "$EXPLORE_BENCH_RUNS" ]; do
  run_one disabled "$i"
  run_one enabled "$i"
  i=$((i + 1))
done

python3 - <<PY
import statistics as st
from collections import defaultdict

rows = defaultdict(list)
with open("$RESULTS") as f:
    next(f, None)
    for line in f:
        mode, wall, ok, out_bytes = line.rstrip().split("\t")
        rows[mode].append((float(wall), int(ok), int(out_bytes)))

print("--- summary ---")
print(f"{'--sandbox':<12} {'n':>3} {'Median':>8} {'Mean':>8} {'Min-Max':>16}")
for mode in ("disabled", "enabled"):
    vals = [w for w, ok, _ in rows[mode] if ok]
    all_n = len(rows[mode])
    if not vals:
        print(f"{mode:<12} {all_n:>3} {'FAIL':>8} {'FAIL':>8} {'FAIL':>16}")
        continue
    med = st.median(vals)
    mean = st.mean(vals)
    print(f"{mode:<12} {len(vals):>3} {med:>7.2f}s {mean:>7.2f}s {min(vals):>6.2f}-{max(vals):>6.2f}")

disabled = [w for w, ok, _ in rows["disabled"] if ok]
enabled = [w for w, ok, _ in rows["enabled"] if ok]
if disabled and enabled and len(disabled) == len(enabled):
    pairs = [e - d for d, e in zip(disabled, enabled)]
    print(f"\npaired median delta (enabled - disabled): {st.median(pairs):+.2f}s")
print(f"\nartifacts: $BENCH_ROOT")
PY
