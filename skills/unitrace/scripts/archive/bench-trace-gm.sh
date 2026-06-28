#!/usr/bin/env bash
# Compare trace-cursor.sh (cursor-agent) vs trace-gemini.sh (Gemini CLI) wall clock and output quality.
#
# Usage (from the repo you want traced, usually the explore skill root):
#   ~/.agents/skills/explore/scripts/bench-trace-gm.sh
#
# Env:
#   UNITRACE_BENCH_QUERY      trace question (default: How does trace.sh work end to end?)
#   UNITRACE_BENCH_RUNS       paired runs per transport (default: 3)
#   UNITRACE_BENCH_WORKSPACE  workspace root (default: pwd)
#   UNITRACE_MAP_MODE         map prefetch (default: tandem)
#   UNITRACE_BENCH_OUT        results directory (default: benchmarks/YYYY-MM-DD-trace-gm)
#   UNITRACE_HERMETIC_HOME    passed through for trace-cursor.sh (default: 1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=hermetic-home.sh
. "$SCRIPT_DIR/hermetic-home.sh"
explore_apply_hermetic_default

UNITRACE_BENCH_RUNS="${UNITRACE_BENCH_RUNS:-3}"
UNITRACE_BENCH_WORKSPACE="${UNITRACE_BENCH_WORKSPACE:-$PWD}"
UNITRACE_MAP_MODE="${UNITRACE_MAP_MODE:-tandem}"
UNITRACE_BENCH_QUERY="${UNITRACE_BENCH_QUERY:-How does trace.sh work end to end?}"
UNITRACE_BENCH_OUT="${UNITRACE_BENCH_OUT:-$SKILL_DIR/benchmarks/$(date +%Y-%m-%d)-trace-gm}"

if ! [[ "$UNITRACE_BENCH_RUNS" =~ ^[0-9]+$ ]] || [ "$UNITRACE_BENCH_RUNS" -lt 1 ]; then
  printf 'bench-trace-gm: UNITRACE_BENCH_RUNS must be a positive integer\n' >&2
  exit 2
fi

command -v cursor-agent >/dev/null 2>&1 || {
  printf 'bench-trace-gm: cursor-agent not found on PATH\n' >&2
  exit 127
}

command -v gemini >/dev/null 2>&1 || {
  printf 'bench-trace-gm: gemini CLI not found on PATH\n' >&2
  exit 127
}

explore_real="$(explore_real_home)"
explore_base="$(cd "$explore_real/.cache/explore" 2>/dev/null && pwd || true)"
if [ -z "$explore_base" ]; then
  mkdir -p "$explore_real/.cache/explore"
  explore_base="$(cd "$explore_real/.cache/explore" && pwd)"
fi
explore_ensure_hermetic_home "$explore_real" "$(explore_hermetic_home_dir "$explore_base")" >/dev/null

mkdir -p "$UNITRACE_BENCH_OUT"
RESULTS="$UNITRACE_BENCH_OUT/results.tsv"
printf 'transport\twall_s\tok\tout_bytes\tunique_citations\tsection_score\tcompleteness\tquality_index\tcite_lineStart\tcite_inline\tcite_pathFirst\n' > "$RESULTS"

score_trace_out() {
  local out_md="$1"
  local structured="${2:-}"
  local args=(--file "$out_md" --json)
  if [ -n "$structured" ] && [ -f "$structured" ]; then
    args+=(--structured "$structured")
  fi
  node "$SCRIPT_DIR/lib/bench-trace-scorer.mjs" "${args[@]}"
}

run_one() {
  local transport="$1"
  local n="$2"
  local label="${transport}-${n}"
  local runs_dir="$UNITRACE_BENCH_OUT/$label"
  local run_dir
  local start end elapsed out_bytes ok=0
  local script
  local score_json unique_citations section_score completeness quality_index
  local cite_lineStart cite_inline cite_pathFirst

  mkdir -p "$runs_dir"
  if [ "$transport" = "cursor" ]; then
    script="$SCRIPT_DIR/trace-cursor.sh"
  else
    script="$SCRIPT_DIR/trace-gemini.sh"
  fi

  start="$(python3 -c 'import time; print(time.time())')"
  if env -u CURSOR_CONVERSATION_ID -u CURSOR_AGENT \
    UNITRACE_HERMETIC_HOME="${UNITRACE_HERMETIC_HOME:-1}" \
    UNITRACE_MAP_MODE="$UNITRACE_MAP_MODE" \
    UNITRACE_WORKSPACE="$UNITRACE_BENCH_WORKSPACE" \
    UNITRACE_RUNS_DIR="$runs_dir/runs" \
    UNITRACE_RUN_ID="$label" \
    UNITRACE_FORMAT=raw \
    "$script" "$UNITRACE_BENCH_QUERY" >"$runs_dir/stdout" 2>"$runs_dir/stderr"; then
    ok=1
  fi
  end="$(python3 -c 'import time; print(time.time())')"
  elapsed="$(python3 -c "print(round($end - $start, 2))")"
  run_dir="$runs_dir/runs/$label"
  out_bytes=0
  unique_citations=0
  section_score=0
  completeness=0
  quality_index=0
  cite_lineStart=0
  cite_inline=0
  cite_pathFirst=0
  if [ -f "$run_dir/out.md" ]; then
    out_bytes="$(wc -c < "$run_dir/out.md" | tr -d ' ')"
    score_json="$(score_trace_out "$run_dir/out.md" "$run_dir/structured.json")"
    read -r unique_citations section_score completeness quality_index cite_lineStart cite_inline cite_pathFirst <<EOF
$(python3 -c '
import json, sys
s = json.loads(sys.stdin.read())
keys = ["uniqueCitations","sectionScore","completenessScore","qualityIndex","citeLineStart","citeInline","citePathFirst"]
print(" ".join(str(s.get(k, 0)) for k in keys))
' <<< "$score_json")
EOF
    [ -s "$run_dir/out.md" ] || ok=0
  else
    ok=0
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$transport" "$elapsed" "$ok" "$out_bytes" \
    "$unique_citations" "$section_score" "$completeness" "$quality_index" \
    "$cite_lineStart" "$cite_inline" "$cite_pathFirst" >> "$RESULTS"
}

export UNITRACE_BENCH_OUT UNITRACE_BENCH_QUERY UNITRACE_BENCH_WORKSPACE UNITRACE_MAP_MODE UNITRACE_BENCH_RUNS

printf 'bench-trace-gm\n'
printf 'workspace: %s\n' "$UNITRACE_BENCH_WORKSPACE"
printf 'query: %s\n' "$UNITRACE_BENCH_QUERY"
printf 'map mode: %s\n' "$UNITRACE_MAP_MODE"
printf 'paired runs per transport: %s (interleaved cursor, gemini)\n\n' "$UNITRACE_BENCH_RUNS"

i=1
while [ "$i" -le "$UNITRACE_BENCH_RUNS" ]; do
  run_one cursor "$i"
  run_one gemini "$i"
  i=$((i + 1))
done

python3 - <<'PY'
import os, statistics as st
from collections import defaultdict
from datetime import datetime, timezone

out_dir = os.environ["UNITRACE_BENCH_OUT"]
results_path = os.path.join(out_dir, "results.tsv")
transports = ["cursor", "gemini"]
rows = defaultdict(list)
cols = [
    "wall", "ok", "out_bytes", "unique_citations", "section_score",
    "completeness", "quality_index", "cite_lineStart", "cite_inline", "cite_pathFirst",
]
with open(results_path) as f:
    next(f, None)
    for line in f:
        parts = line.rstrip().split("\t")
        transport = parts[0]
        wall, ok, out_bytes = parts[1], int(parts[2]), int(parts[3])
        metrics = [int(x) for x in parts[4:]]
        rows[transport].append((float(wall), ok, out_bytes, *metrics))

summary_lines = []
summary_lines.append("# trace-gm vs cursor benchmark\n")
summary_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
summary_lines.append("## Query\n")
summary_lines.append(f"> {os.environ.get('UNITRACE_BENCH_QUERY', '')}\n")
summary_lines.append("\n## Config\n")
summary_lines.append(f"- workspace: `{os.environ.get('UNITRACE_BENCH_WORKSPACE', '')}`")
summary_lines.append(f"- map mode: `{os.environ.get('UNITRACE_MAP_MODE', 'tandem')}`")
summary_lines.append(f"- paired runs: `{os.environ.get('UNITRACE_BENCH_RUNS', '3')}`")
summary_lines.append(f"- cursor: trace-cursor.sh (composer-2.5-fast via cursor-agent)")
summary_lines.append(f"- gemini: trace-gemini.sh (gemini-3.1-flash-lite via Gemini CLI plan mode)")
summary_lines.append(f"- quality scorer: `scripts/lib/bench-trace-scorer.mjs` (multi-format citations + semantic sections)\n")
summary_lines.append("\n## Results\n")
summary_lines.append(
    "| transport | n (ok) | Median wall | Mean wall | Median bytes | "
    "Med cites | Med sections | Med quality | Med cite inline | Med cite path-first |"
)
summary_lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

print("--- summary ---")
print(
    f"{'transport':<10} {'n':>3} {'Median':>8} {'Mean':>8} {'MedBytes':>9} "
    f"{'MedCite':>8} {'MedSec':>7} {'MedQI':>7} {'MedInline':>10} {'MedPath':>8}"
)
for t in transports:
    vals = [w for w, ok, *_ in rows[t] if ok]
    byts = [b for _, ok, b, *_ in rows[t] if ok]
    cites = [c for _, ok, _, c, *_ in rows[t] if ok]
    secs = [s for _, ok, _, _, s, *_ in rows[t] if ok]
    qi = [q for _, ok, _, _, _, _, q, *_ in rows[t] if ok]
    inline = [i for _, ok, *rest in rows[t] if ok for i in [rest[6]]]
    pathfirst = [p for _, ok, *rest in rows[t] if ok for p in [rest[7]]]
    all_n = len(rows[t])
    ok_n = len(vals)
    if not vals:
        print(f"{t:<10} {all_n:>3} {'FAIL':>8} {'FAIL':>8} {'FAIL':>9} {'FAIL':>8} {'FAIL':>7} {'FAIL':>7} {'FAIL':>10} {'FAIL':>8}")
        summary_lines.append(f"| {t} | {ok_n}/{all_n} | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |")
        continue
    med = st.median(vals)
    mean = st.mean(vals)
    med_b = int(st.median(byts))
    med_c = int(st.median(cites))
    med_s = int(st.median(secs))
    med_q = int(st.median(qi))
    med_i = int(st.median(inline))
    med_p = int(st.median(pathfirst))
    print(
        f"{t:<10} {ok_n:>3} {med:>7.2f}s {mean:>7.2f}s {med_b:>9} "
        f"{med_c:>8} {med_s:>7} {med_q:>7} {med_i:>10} {med_p:>8}"
    )
    summary_lines.append(
        f"| {t} | {ok_n}/{all_n} | {med:.2f}s | {mean:.2f}s | {med_b} | "
        f"{med_c} | {med_s} | {med_q} | {med_i} | {med_p} |"
    )

base_vals = [w for w, ok, *_ in rows["cursor"] if ok]
gem_vals = [w for w, ok, *_ in rows["gemini"] if ok]
if base_vals and gem_vals and len(base_vals) == len(gem_vals):
    pairs = [g - b for b, g in zip(base_vals, gem_vals)]
    delta = st.median(pairs)
    print(f"\npaired median delta (gemini - cursor): {delta:+.2f}s")
    summary_lines.append(f"\nPaired median delta (gemini - cursor): **{delta:+.2f}s**")

summary_lines.append(f"\n## Artifacts\n")
summary_lines.append(f"- `{results_path}`")
summary_lines.append(f"- per-run dirs under `{out_dir}/`\n")

with open(os.path.join(out_dir, "README.md"), "w") as f:
    f.write("\n".join(summary_lines) + "\n")

print(f"\nreadme: {os.path.join(out_dir, 'README.md')}")
print(f"artifacts: {out_dir}")
PY
