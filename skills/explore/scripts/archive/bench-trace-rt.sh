#!/usr/bin/env bash
# Compare trace-gemini.sh (gemini-3.1-flash-lite) vs trace-rt.sh (gpt-realtime-2) wall clock and output quality.
#
# Usage (from the repo you want traced, usually the explore skill root):
#   ~/.agents/skills/explore/scripts/bench-trace-rt.sh
#
# Env:
#   EXPLORE_BENCH_QUERY      trace question (default: How does trace.sh work end to end?)
#   EXPLORE_BENCH_RUNS       paired runs per transport (default: 3)
#   EXPLORE_BENCH_WORKSPACE  workspace root (default: pwd)
#   EXPLORE_MAP_MODE         map prefetch (default: tandem)
#   EXPLORE_BENCH_OUT        results directory (default: benchmarks/YYYY-MM-DD-trace-rt)
#   EXPLORE_HERMETIC_HOME    passed through for trace-gemini.sh (default: 1)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=hermetic-home.sh
. "$SCRIPT_DIR/hermetic-home.sh"
explore_apply_hermetic_default

EXPLORE_BENCH_RUNS="${EXPLORE_BENCH_RUNS:-3}"
EXPLORE_BENCH_WORKSPACE="${EXPLORE_BENCH_WORKSPACE:-$PWD}"
EXPLORE_MAP_MODE="${EXPLORE_MAP_MODE:-tandem}"
EXPLORE_BENCH_QUERY="${EXPLORE_BENCH_QUERY:-How does trace.sh work end to end?}"
EXPLORE_BENCH_OUT="${EXPLORE_BENCH_OUT:-$SKILL_DIR/benchmarks/$(date +%Y-%m-%d)-trace-rt}"
mkdir -p "$EXPLORE_BENCH_OUT"
EXPLORE_BENCH_OUT="$(cd "$EXPLORE_BENCH_OUT" && pwd)"

if ! [[ "$EXPLORE_BENCH_RUNS" =~ ^[0-9]+$ ]] || [ "$EXPLORE_BENCH_RUNS" -lt 1 ]; then
  printf 'bench-trace-rt: EXPLORE_BENCH_RUNS must be a positive integer\n' >&2
  exit 2
fi

command -v "${EXPLORE_GM_BIN:-gemini}" >/dev/null 2>&1 || {
  printf 'bench-trace-rt: gemini CLI not found on PATH (set EXPLORE_GM_BIN or install Gemini CLI)\n' >&2
  exit 127
}

if [ ! -f "${EXPLORE_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json}" ]; then
  printf 'bench-trace-rt: Codex auth not found (run: codex login)\n' >&2
  exit 1
fi

explore_real="$(explore_real_home)"
explore_base="$(cd "$explore_real/.cache/explore" 2>/dev/null && pwd || true)"
if [ -z "$explore_base" ]; then
  mkdir -p "$explore_real/.cache/explore"
  explore_base="$(cd "$explore_real/.cache/explore" && pwd)"
fi
explore_ensure_hermetic_home "$explore_real" "$(explore_hermetic_home_dir "$explore_base")" >/dev/null

mkdir -p "$EXPLORE_BENCH_OUT"
RESULTS="$EXPLORE_BENCH_OUT/results.tsv"
printf 'transport\twall_s\tok\tout_bytes\tunique_citations\tsection_score\tcompleteness\tquality_index\tcite_lineStart\tcite_inline\tcite_pathFirst\texplore_ms\tsubmit_ms\tmax_batch\texplore_turns\ttool_calls\n' > "$RESULTS"

score_trace_out() {
  local out_md="$1"
  local structured="${2:-}"
  local args=(--file "$out_md" --json --workspace "$EXPLORE_BENCH_WORKSPACE")
  if [ -n "$structured" ] && [ -f "$structured" ]; then
    args+=(--structured "$structured")
  fi
  node "$SCRIPT_DIR/lib/bench-trace-scorer.mjs" "${args[@]}"
}

parse_rt_phase_metrics() {
  local err_file="$1"
  explore_ms=""
  submit_ms=""
  max_batch=""
  explore_turns=""
  tool_calls=""
  if [ ! -s "$err_file" ]; then
    return 0
  fi
  local phase_line
  phase_line="$(grep '^phase explore_ms=' "$err_file" | tail -1 || true)"
  if [ -n "$phase_line" ]; then
    explore_ms="$(printf '%s' "$phase_line" | sed -n 's/.*explore_ms=\([0-9]*\).*/\1/p')"
    max_batch="$(printf '%s' "$phase_line" | sed -n 's/.*max_batch=\([0-9]*\).*/\1/p')"
    explore_turns="$(printf '%s' "$phase_line" | sed -n 's/.*explore_turns=\([0-9]*\).*/\1/p')"
    tool_calls="$(printf '%s' "$phase_line" | sed -n 's/.*tool_calls=\([0-9]*\).*/\1/p')"
  fi
  submit_ms="$(grep -o 'submit_ms=[0-9]*' "$err_file" | tail -1 | cut -d= -f2 || true)"
}

run_one() {
  local transport="$1"
  local n="$2"
  local label="${transport}-${n}"
  local runs_dir="$EXPLORE_BENCH_OUT/$label"
  local run_dir
  local start end elapsed out_bytes ok=0
  local script
  local score_json unique_citations section_score completeness quality_index
  local cite_lineStart cite_inline cite_pathFirst
  local explore_ms="" submit_ms="" max_batch="" explore_turns="" tool_calls=""

  mkdir -p "$runs_dir"
  if [ "$transport" = "gemini" ]; then
    script="$SCRIPT_DIR/trace-gemini.sh"
  else
    script="$SCRIPT_DIR/trace-rt.sh"
  fi

  start="$(python3 -c 'import time; print(time.time())')"
  if [ "$transport" = "gemini" ]; then
    if env -u CURSOR_CONVERSATION_ID -u CURSOR_AGENT \
      EXPLORE_HERMETIC_HOME="${EXPLORE_HERMETIC_HOME:-1}" \
      EXPLORE_MAP_MODE="$EXPLORE_MAP_MODE" \
      EXPLORE_WORKSPACE="$EXPLORE_BENCH_WORKSPACE" \
      EXPLORE_RUNS_DIR="$runs_dir/runs" \
      EXPLORE_RUN_ID="$label" \
      "$script" "$EXPLORE_BENCH_QUERY" >"$runs_dir/stdout" 2>"$runs_dir/stderr"; then
      ok=1
    fi
  else
    if env -u CURSOR_CONVERSATION_ID \
      EXPLORE_MAP_MODE="$EXPLORE_MAP_MODE" \
      EXPLORE_WORKSPACE="$EXPLORE_BENCH_WORKSPACE" \
      EXPLORE_RUNS_DIR="$runs_dir/runs" \
      EXPLORE_RUN_ID="$label" \
      EXPLORE_RT_PARALLEL_TOOL_CALLS=1 \
      "$script" "$EXPLORE_BENCH_QUERY" >"$runs_dir/stdout" 2>"$runs_dir/stderr"; then
      ok=1
    fi
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
  if [ "$transport" = "rt" ] && [ -f "$run_dir/err.log" ]; then
    parse_rt_phase_metrics "$run_dir/err.log"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$transport" "$elapsed" "$ok" "$out_bytes" \
    "$unique_citations" "$section_score" "$completeness" "$quality_index" \
    "$cite_lineStart" "$cite_inline" "$cite_pathFirst" \
    "${explore_ms:-}" "${submit_ms:-}" "${max_batch:-}" "${explore_turns:-}" "${tool_calls:-}" >> "$RESULTS"
}

export EXPLORE_BENCH_OUT EXPLORE_BENCH_QUERY EXPLORE_BENCH_WORKSPACE EXPLORE_MAP_MODE EXPLORE_BENCH_RUNS

printf 'bench-trace-rt\n'
printf 'workspace: %s\n' "$EXPLORE_BENCH_WORKSPACE"
printf 'query: %s\n' "$EXPLORE_BENCH_QUERY"
printf 'map mode: %s\n' "$EXPLORE_MAP_MODE"
printf 'paired runs per transport: %s (interleaved gemini, rt)\n\n' "$EXPLORE_BENCH_RUNS"

i=1
while [ "$i" -le "$EXPLORE_BENCH_RUNS" ]; do
  run_one gemini "$i"
  run_one rt "$i"
  i=$((i + 1))
done

python3 - <<'PY'
import os, statistics as st
from collections import defaultdict
from datetime import datetime, timezone

out_dir = os.environ["EXPLORE_BENCH_OUT"]
results_path = os.path.join(out_dir, "results.tsv")
transports = ["gemini", "rt"]
rows = defaultdict(list)
with open(results_path) as f:
    next(f, None)
    for line in f:
        parts = line.rstrip().split("\t")
        transport = parts[0]
        wall, ok, out_bytes = parts[1], int(parts[2]), int(parts[3])
        metrics = parts[4:11]
        rt_extra = parts[11:16] if len(parts) > 11 else ["", "", "", "", ""]
        rows[transport].append((float(wall), ok, out_bytes, *[int(x) for x in metrics], rt_extra))

summary_lines = []
summary_lines.append("# trace-rt vs gemini benchmark\n")
summary_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
summary_lines.append("## Query\n")
summary_lines.append(f"> {os.environ.get('EXPLORE_BENCH_QUERY', '')}\n")
summary_lines.append("\n## Config\n")
summary_lines.append(f"- workspace: `{os.environ.get('EXPLORE_BENCH_WORKSPACE', '')}`")
summary_lines.append(f"- map mode: `{os.environ.get('EXPLORE_MAP_MODE', 'tandem')}`")
summary_lines.append(f"- paired runs: `{os.environ.get('EXPLORE_BENCH_RUNS', '3')}`")
summary_lines.append(f"- gemini: trace-gemini.sh (gemini-3.1-flash-lite via Gemini CLI)")
summary_lines.append(f"- rt: trace-rt.sh (gpt-realtime-2: explore low / submit minimal reasoning, host passages, slim schema)")
summary_lines.append(f"- quality scorer: `scripts/lib/bench-trace-scorer.mjs`\n")
summary_lines.append("\n## Results\n")
summary_lines.append(
    "| transport | n (ok) | Median wall | Mean wall | Median bytes | "
    "Med cites | Med sections | Med quality | Med explore_ms | Med submit_ms | Med max_batch | Med explore_turns |"
)
summary_lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")

print("--- summary ---")
print(
    f"{'transport':<10} {'n':>3} {'Median':>8} {'Mean':>8} {'MedBytes':>9} "
    f"{'MedCite':>8} {'MedSec':>7} {'MedQI':>7} {'MedExpMs':>9} {'MedSubMs':>9} {'MaxBatch':>9} {'Turns':>6}"
)

def med_int(vals):
    return int(st.median(vals)) if vals else 0

for t in transports:
    ok_rows = [r for r in rows[t] if r[1]]
    all_n = len(rows[t])
    ok_n = len(ok_rows)
    if not ok_rows:
        print(f"{t:<10} {all_n:>3} {'FAIL':>8} {'FAIL':>8} {'FAIL':>9} {'FAIL':>8} {'FAIL':>7} {'FAIL':>7} {'FAIL':>9} {'FAIL':>9} {'FAIL':>9} {'FAIL':>6}")
        summary_lines.append(f"| {t} | {ok_n}/{all_n} | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |")
        continue
    walls = [r[0] for r in ok_rows]
    byts = [r[2] for r in ok_rows]
    cites = [r[3] for r in ok_rows]
    secs = [r[4] for r in ok_rows]
    qi = [r[6] for r in ok_rows]
    med = st.median(walls)
    mean = st.mean(walls)
    med_b = int(st.median(byts))
    med_c = int(st.median(cites))
    med_s = int(st.median(secs))
    med_q = int(st.median(qi))
    if t == "rt":
        explore_ms = [int(r[10][0]) for r in ok_rows if r[10][0].isdigit()]
        submit_ms = [int(r[10][1]) for r in ok_rows if r[10][1].isdigit()]
        max_batch = [int(r[10][2]) for r in ok_rows if r[10][2].isdigit()]
        turns = [int(r[10][3]) for r in ok_rows if r[10][3].isdigit()]
        med_e = med_int(explore_ms)
        med_su = med_int(submit_ms)
        med_mb = med_int(max_batch)
        med_t = med_int(turns)
    else:
        med_e = med_su = med_mb = med_t = 0
    print(
        f"{t:<10} {ok_n:>3} {med:>7.2f}s {mean:>7.2f}s {med_b:>9} "
        f"{med_c:>8} {med_s:>7} {med_q:>7} {med_e:>9} {med_su:>9} {med_mb:>9} {med_t:>6}"
    )
    summary_lines.append(
        f"| {t} | {ok_n}/{all_n} | {med:.2f}s | {mean:.2f}s | {med_b} | "
        f"{med_c} | {med_s} | {med_q} | {med_e or 'n/a'} | {med_su or 'n/a'} | {med_mb or 'n/a'} | {med_t or 'n/a'} |"
    )

base_vals = [r[0] for r in rows["gemini"] if r[1]]
rt_vals = [r[0] for r in rows["rt"] if r[1]]
if base_vals and rt_vals and len(base_vals) == len(rt_vals):
    pairs = [r - b for b, r in zip(base_vals, rt_vals)]
    delta = st.median(pairs)
    print(f"\npaired median delta (rt - gemini): {delta:+.2f}s")
    summary_lines.append(f"\nPaired median delta (rt - gemini): **{delta:+.2f}s**")

summary_lines.append("\n## Prior baseline (pre-optimizations, 2026-06-24 trace-rt-exec)\n")
summary_lines.append("| metric | trace-gemini.sh | trace-rt (explore_exec v1) |")
summary_lines.append("|---|---:|---:|")
summary_lines.append("| wall | 10.71s | 30.00s |")
summary_lines.append("| explore_ms | n/a | 15208 |")
summary_lines.append("| submit_ms | n/a | 14089 |")
summary_lines.append("| quality index | 29 | 59 |")
summary_lines.append("| explore_turns | n/a | 3 |")

summary_lines.append("\n## Prior baseline (v2 opt, 2026-06-24 trace-rt-opt)\n")
summary_lines.append("| metric | trace-gemini.sh | trace-rt v2 |")
summary_lines.append("|---|---:|---:|")
summary_lines.append("| wall | 10.18s | 22.83s |")
summary_lines.append("| explore_ms | n/a | 8551 |")
summary_lines.append("| submit_ms | n/a | 13797 |")
summary_lines.append("| quality index | 37 | 61 |")
summary_lines.append("| explore_turns | n/a | 1 |")

summary_lines.append("\n## Findings\n")
if rt_vals:
    summary_lines.append(
        "- Default reasoning: explore `low`, submit `minimal`; host-assembled code_passages, map line-range seeds, exec preflight, slim schema."
    )
    rt_ok = [r for r in rows["rt"] if r[1]]
    batches = [int(r[10][2]) for r in rt_ok if r[10][2].isdigit()]
    if batches:
        summary_lines.append(f"- RT median max_batch per explore turn: **{med_int(batches)}** (1 = no parallel batching).")
    turns = [int(r[10][3]) for r in rt_ok if r[10][3].isdigit()]
    if turns:
        summary_lines.append(f"- RT median explore model turns: **{med_int(turns)}**.")
    explore_ms = [int(r[10][0]) for r in rt_ok if r[10][0].isdigit()]
    submit_ms = [int(r[10][1]) for r in rt_ok if r[10][1].isdigit()]
    qi_vals = [r[6] for r in rt_ok]
    if rt_vals and explore_ms:
        summary_lines.append("\n### vs trace-rt v1 (2026-06-24 exec)\n")
        summary_lines.append("| metric | v1 | current | delta |")
        summary_lines.append("|---|---:|---:|---:|")
        summary_lines.append(f"| wall | 30.00s | {st.median(rt_vals):.2f}s | {st.median(rt_vals) - 30.0:+.2f}s |")
        summary_lines.append(f"| explore_ms | 15208 | {med_int(explore_ms)} | {med_int(explore_ms) - 15208:+d} |")
        if submit_ms:
            summary_lines.append(f"| submit_ms | 14089 | {med_int(submit_ms)} | {med_int(submit_ms) - 14089:+d} |")
        summary_lines.append(f"| quality index | 59 | {med_int(qi_vals)} | {med_int(qi_vals) - 59:+d} |")
        if turns:
            summary_lines.append(f"| explore_turns | 3 | {med_int(turns)} | {med_int(turns) - 3:+d} |")
        summary_lines.append("\n### vs trace-rt v2 (2026-06-24 opt)\n")
        summary_lines.append("| metric | v2 | current | delta |")
        summary_lines.append("|---|---:|---:|---:|")
        summary_lines.append(f"| wall | 22.83s | {st.median(rt_vals):.2f}s | {st.median(rt_vals) - 22.83:+.2f}s |")
        summary_lines.append(f"| explore_ms | 8551 | {med_int(explore_ms)} | {med_int(explore_ms) - 8551:+d} |")
        if submit_ms:
            summary_lines.append(f"| submit_ms | 13797 | {med_int(submit_ms)} | {med_int(submit_ms) - 13797:+d} |")
        summary_lines.append(f"| quality index | 61 | {med_int(qi_vals)} | {med_int(qi_vals) - 61:+d} |")
        if turns:
            summary_lines.append(f"| explore_turns | 1 | {med_int(turns)} | {med_int(turns) - 1:+d} |")

summary_lines.append(f"\n## Artifacts\n")
summary_lines.append(f"- `{results_path}`")
summary_lines.append(f"- per-run dirs under `{out_dir}/`\n")

with open(os.path.join(out_dir, "README.md"), "w") as f:
    f.write("\n".join(summary_lines) + "\n")

print(f"\nreadme: {os.path.join(out_dir, 'README.md')}")
print(f"artifacts: {out_dir}")
PY
