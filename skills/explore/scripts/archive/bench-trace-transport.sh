#!/usr/bin/env bash
# Compare trace-cursor.sh wall clock across transports: cursor-agent CLI vs the
# zero-dependency in-process harness. Both run the same query/workspace under a
# hermetic HOME, interleaved to reduce ordering bias.
#
# Usage (from the repo you want traced, usually the explore skill root):
#   ~/.agents/skills/explore/scripts/bench-trace-transport.sh
#
# Env:
#   EXPLORE_BENCH_QUERY      trace question (default: repo-local explore skill question)
#   EXPLORE_BENCH_RUNS       paired runs per transport (default: 5)
#   EXPLORE_BENCH_WORKSPACE  workspace root (default: pwd)
#   EXPLORE_BENCH_TRANSPORTS space-separated transports (default: "cli harness")
#   EXPLORE_HERMETIC_HOME    passed through (default: 1 via trace-cursor.sh)
#   CURSOR_AUTH_TOKEN        optional; else reads ~/.cursor/auth.json accessToken
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hermetic-home.sh
. "$SCRIPT_DIR/hermetic-home.sh"
explore_apply_hermetic_default

EXPLORE_BENCH_RUNS="${EXPLORE_BENCH_RUNS:-5}"
EXPLORE_BENCH_WORKSPACE="${EXPLORE_BENCH_WORKSPACE:-$PWD}"
EXPLORE_BENCH_TRANSPORTS="${EXPLORE_BENCH_TRANSPORTS:-cli harness}"
EXPLORE_BENCH_QUERY="${EXPLORE_BENCH_QUERY:-How does this skill split fast search from deep trace, and which scripts implement search.sh versus trace.sh end to end?}"

if ! [[ "$EXPLORE_BENCH_RUNS" =~ ^[0-9]+$ ]] || [ "$EXPLORE_BENCH_RUNS" -lt 1 ]; then
  printf 'bench-trace-transport: EXPLORE_BENCH_RUNS must be a positive integer\n' >&2
  exit 2
fi

# cursor-agent only required when the cli transport is in the set.
case " $EXPLORE_BENCH_TRANSPORTS " in
  *" cli "*|*" acp "*)
    command -v cursor-agent >/dev/null 2>&1 || {
      printf 'bench-trace-transport: cursor-agent not found on PATH (needed for cli/acp)\n' >&2
      exit 127
    }
    ;;
esac

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

BENCH_ROOT="${TMPDIR:-/tmp}/explore-trace-transport-bench-$$"
RESULTS="$BENCH_ROOT/results.tsv"
mkdir -p "$BENCH_ROOT"
printf 'transport\twall_s\tok\tout_bytes\tunique_citations\tquality_index\n' > "$RESULTS"

score_trace_out() {
  local out_md="$1"
  node "$SCRIPT_DIR/lib/bench-trace-scorer.mjs" --file "$out_md" --json
}

run_one() {
  local transport="$1"
  local n="$2"
  local label="${transport}-${n}"
  local runs_dir="$BENCH_ROOT/$label"
  local run_dir
  local start end elapsed out_bytes ok=0
  local unique_citations quality_index score_json

  mkdir -p "$runs_dir"
  start="$(python3 -c 'import time; print(time.time())')"
  if env -u CURSOR_CONVERSATION_ID -u CURSOR_AGENT \
    EXPLORE_HERMETIC_HOME="${EXPLORE_HERMETIC_HOME:-1}" \
    EXPLORE_TRANSPORT="$transport" \
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
  unique_citations=0
  quality_index=0
  if [ -f "$run_dir/out.md" ]; then
    out_bytes="$(wc -c < "$run_dir/out.md" | tr -d ' ')"
    score_json="$(score_trace_out "$run_dir/out.md")"
    read -r unique_citations quality_index <<EOF
$(python3 -c '
import json, sys
s = json.loads(sys.stdin.read())
print(s.get("uniqueCitations", 0), s.get("qualityIndex", 0))
' <<< "$score_json")
EOF
    [ -s "$run_dir/out.md" ] || ok=0
  else
    ok=0
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$transport" "$elapsed" "$ok" "$out_bytes" "$unique_citations" "$quality_index" >> "$RESULTS"
}

printf 'bench-trace-transport\n'
printf 'workspace: %s\n' "$EXPLORE_BENCH_WORKSPACE"
printf 'query: %s\n' "$EXPLORE_BENCH_QUERY"
printf 'transports: %s\n' "$EXPLORE_BENCH_TRANSPORTS"
printf 'paired runs per transport: %s (interleaved)\n\n' "$EXPLORE_BENCH_RUNS"

i=1
while [ "$i" -le "$EXPLORE_BENCH_RUNS" ]; do
  for t in $EXPLORE_BENCH_TRANSPORTS; do
    run_one "$t" "$i"
  done
  i=$((i + 1))
done

EXPLORE_BENCH_TRANSPORTS="$EXPLORE_BENCH_TRANSPORTS" python3 - <<PY
import os, statistics as st
from collections import defaultdict

transports = os.environ["EXPLORE_BENCH_TRANSPORTS"].split()
rows = defaultdict(list)
with open("$RESULTS") as f:
    next(f, None)
    for line in f:
        transport, wall, ok, out_bytes, unique_citations, quality_index = line.rstrip().split("\t")
        rows[transport].append((float(wall), int(ok), int(out_bytes), int(unique_citations), int(quality_index)))

print("--- summary ---")
print(f"{'transport':<12} {'n':>3} {'Median':>8} {'Mean':>8} {'Min-Max':>16} {'MedBytes':>9} {'MedCite':>8} {'MedQI':>6}")
for t in transports:
    vals = [w for w, ok, _, *_ in rows[t] if ok]
    byts = [b for _, ok, b, *_ in rows[t] if ok]
    cites = [c for _, ok, _, c, *_ in rows[t] if ok]
    qi = [q for _, ok, _, _, q in rows[t] if ok]
    all_n = len(rows[t])
    if not vals:
        print(f"{t:<12} {all_n:>3} {'FAIL':>8} {'FAIL':>8} {'FAIL':>16} {'FAIL':>9} {'FAIL':>8} {'FAIL':>6}")
        continue
    med = st.median(vals)
    mean = st.mean(vals)
    print(
        f"{t:<12} {len(vals):>3} {med:>7.2f}s {mean:>7.2f}s "
        f"{min(vals):>6.2f}-{max(vals):>6.2f} {int(st.median(byts)):>9} "
        f"{int(st.median(cites)):>8} {int(st.median(qi)):>6}"
    )

# paired delta vs the first transport (baseline) when counts align
if len(transports) >= 2:
    base = transports[0]
    base_vals = [w for w, ok, _ in rows[base] if ok]
    for t in transports[1:]:
        t_vals = [w for w, ok, _ in rows[t] if ok]
        if base_vals and t_vals and len(base_vals) == len(t_vals):
            pairs = [x - b for b, x in zip(base_vals, t_vals)]
            print(f"\npaired median delta ({t} - {base}): {st.median(pairs):+.2f}s")
print(f"\nartifacts: $BENCH_ROOT")
PY
