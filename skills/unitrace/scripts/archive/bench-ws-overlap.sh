#!/usr/bin/env bash
# Before/after micro-benchmark for the WS handshake / seed-read overlap lever
# (UNITRACE_RT_OVERLAP_SETUP). Runs trace-rt.sh only, arm 0 (before) vs arm 1
# (after), N reps each on one query, and reports connect_ms / seed_ms /
# explore_ms / submit_ms / wall + groundedness so the transport delta is
# isolated from the gemini path.
#
# Env:
#   UNITRACE_BENCH_QUERY      default: How does trace.sh work end to end?
#   UNISEARCH_WS_REPS          reps per arm (default 3)
#   UNITRACE_BENCH_WORKSPACE  workspace (default: skill root)
#   UNITRACE_BENCH_OUT        out dir (default: benchmarks/<date>-ws-mode-adaptation)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

REPS="${UNISEARCH_WS_REPS:-3}"
WS="${UNITRACE_BENCH_WORKSPACE:-$SKILL_DIR}"
QUERY="${UNITRACE_BENCH_QUERY:-How does trace.sh work end to end?}"
MAP_MODE="${UNITRACE_MAP_MODE:-tandem}"
OUT="${UNITRACE_BENCH_OUT:-$SKILL_DIR/benchmarks/$(date +%Y-%m-%d)-ws-mode-adaptation}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
TSV="$OUT/overlap-results.tsv"
printf 'arm\trep\twall_s\tok\tconnect_ms\tseed_ms\texplore_ms\tsubmit_ms\tquality_index\tgrounded_ratio\n' > "$TSV"

num() { printf '%s' "$1" | grep -oE "$2=[0-9]+" | tail -1 | cut -d= -f2 || true; }

run_arm() {
  local arm="$1" rep="$2"
  local label="ovl${arm}-${rep}"
  local rdir="$OUT/$label"
  mkdir -p "$rdir"
  local start end wall ok=0
  start="$(python3 -c 'import time;print(time.time())')"
  if env -u CURSOR_CONVERSATION_ID \
      UNITRACE_MAP_MODE="$MAP_MODE" \
      UNITRACE_WORKSPACE="$WS" \
      UNITRACE_RUNS_DIR="$rdir/runs" \
      UNITRACE_RUN_ID="$label" \
      UNITRACE_RT_PARALLEL_TOOL_CALLS=1 \
      UNITRACE_RT_OVERLAP_SETUP="$arm" \
      "$SCRIPT_DIR/trace-rt.sh" "$QUERY" >"$rdir/stdout" 2>"$rdir/stderr"; then
    ok=1
  fi
  end="$(python3 -c 'import time;print(time.time())')"
  wall="$(python3 -c "print(round($end-$start,2))")"
  local run_dir="$rdir/runs/$label"
  local err="$run_dir/err.log"
  local connect_ms="" seed_ms="" explore_ms="" submit_ms="" qi=0 gr=0
  if [ -f "$err" ]; then
    local cline pline
    cline="$(grep '^phase connect_ms=' "$err" | tail -1 || true)"
    pline="$(grep '^phase explore_ms=' "$err" | tail -1 || true)"
    connect_ms="$(num "$cline" connect_ms)"
    seed_ms="$(num "$cline" seed_ms)"
    explore_ms="$(num "$pline" explore_ms)"
    submit_ms="$(grep -oE 'submit_ms=[0-9]+' "$err" | tail -1 | cut -d= -f2 || true)"
  fi
  if [ -f "$run_dir/out.md" ]; then
    [ -s "$run_dir/out.md" ] || ok=0
    local sjson
    sjson="$(node "$SCRIPT_DIR/lib/bench-trace-scorer.mjs" --file "$run_dir/out.md" --json --workspace "$WS" ${run_dir:+--structured "$run_dir/structured.json"} 2>/dev/null || echo '{}')"
    read -r qi gr <<EOF
$(python3 -c '
import json,sys
try: s=json.loads(sys.stdin.read())
except Exception: s={}
print(s.get("qualityIndex",0), s.get("groundedRatio", s.get("medGroundednessRatio", s.get("groundednessRatio",0))))
' <<< "$sjson")
EOF
  else
    ok=0
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$arm" "$rep" "$wall" "$ok" "${connect_ms:-}" "${seed_ms:-}" "${explore_ms:-}" "${submit_ms:-}" "${qi:-0}" "${gr:-0}" >> "$TSV"
  printf '  arm=%s rep=%s ok=%s wall=%ss connect_ms=%s seed_ms=%s explore_ms=%s submit_ms=%s qi=%s grounded=%s\n' \
    "$arm" "$rep" "$ok" "$wall" "${connect_ms:-?}" "${seed_ms:-?}" "${explore_ms:-?}" "${submit_ms:-?}" "${qi:-?}" "${gr:-?}"
}

printf 'bench-ws-overlap  query="%s"  reps=%s  ws=%s\n\n' "$QUERY" "$REPS" "$WS"
# Interleave arms to spread server-load noise evenly across before/after.
r=1
while [ "$r" -le "$REPS" ]; do
  run_arm 0 "$r"
  run_arm 1 "$r"
  r=$((r+1))
done

echo
UNISEARCH_WS_TSV="$TSV" UNISEARCH_WS_OUT="$OUT" UNISEARCH_WS_QUERY="$QUERY" python3 - <<'PY'
import os, statistics as st
from collections import defaultdict
tsv=os.environ["UNISEARCH_WS_TSV"]
rows=defaultdict(list)
with open(tsv) as f:
    next(f,None)
    for line in f:
        p=line.rstrip("\n").split("\t")
        if len(p)<10: continue
        arm=p[0]
        def i(x):
            try: return int(x)
            except: return None
        def fl(x):
            try: return float(x)
            except: return None
        rows[arm].append(dict(rep=p[1], wall=fl(p[2]), ok=i(p[3]), connect=i(p[4]), seed=i(p[5]),
                              explore=i(p[6]), submit=i(p[7]), qi=i(p[8]), grounded=fl(p[9])))
def med(vals):
    vals=[v for v in vals if v is not None]
    return st.median(vals) if vals else None
def fmt(x,suf=""):
    return "n/a" if x is None else (f"{x:.2f}{suf}" if isinstance(x,float) else f"{x}{suf}")
lines=[]
lines.append("### v10 before/after — WS handshake / seed overlap\n")
lines.append(f"Query: `{os.environ['UNISEARCH_WS_QUERY']}`\n")
lines.append("| arm | n(ok) | med wall | med connect_ms | med seed_ms | med explore_ms | med submit_ms | med quality | med grounded |")
lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
order=[("0","before (overlap off)"),("1","after (overlap on)")]
agg={}
for arm,label in order:
    rs=[r for r in rows.get(arm,[]) if r["ok"]]
    n=f"{len(rs)}/{len(rows.get(arm,[]))}"
    mw=med([r['wall'] for r in rs]); mc=med([r['connect'] for r in rs]); ms=med([r['seed'] for r in rs])
    me=med([r['explore'] for r in rs]); msu=med([r['submit'] for r in rs]); mq=med([r['qi'] for r in rs]); mg=med([r['grounded'] for r in rs])
    agg[arm]=dict(wall=mw,connect=mc,seed=ms,explore=me,submit=msu,qi=mq,grounded=mg)
    lines.append(f"| {label} | {n} | {fmt(mw,'s')} | {fmt(mc)} | {fmt(ms)} | {fmt(me)} | {fmt(msu)} | {fmt(mq)} | {fmt(mg)} |")
if "0" in agg and "1" in agg and agg["0"]["wall"] and agg["1"]["wall"]:
    dw=agg["1"]["wall"]-agg["0"]["wall"]
    lines.append(f"\nMedian wall delta (after - before): **{dw:+.2f}s**")
    if agg["0"]["connect"] is not None and agg["1"]["connect"] is not None:
        lines.append(f"  connect_ms delta: {agg['1']['connect']-agg['0']['connect']:+d} (after includes the now-hidden seed read)")
out=os.path.join(os.environ["UNISEARCH_WS_OUT"],"overlap-summary.md")
open(out,"w").write("\n".join(lines)+"\n")
print("\n".join(lines))
print(f"\nsummary: {out}")
PY
