#!/usr/bin/env bash
# Compare websearch-gemini.sh (agy + Exa MCP) vs websearch-rt.sh (gpt-realtime-2 + alpha/web_run).
#
# Usage:
#   ~/.agents/skills/explore/scripts/bench-websearch-rt.sh
#
# Env:
#   EXPLORE_BENCH_QUERY          research goal (default: What is MCP?)
#   EXPLORE_BENCH_RUNS           paired runs per transport (default: 2)
#   EXPLORE_BENCH_WORKSPACE      workspace root (default: pwd)
#   EXPLORE_BENCH_OUT            results directory (default: benchmarks/YYYY-MM-DD-websearch-rt)
#   EXPLORE_WS_BACKEND           RT arm backend (default: alpha)
#   EXA_API_KEY                  required for gemini arm (Exa MCP)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

EXPLORE_BENCH_RUNS="${EXPLORE_BENCH_RUNS:-2}"
EXPLORE_BENCH_WORKSPACE="${EXPLORE_BENCH_WORKSPACE:-$PWD}"
EXPLORE_BENCH_QUERY="${EXPLORE_BENCH_QUERY:-What is the Model Context Protocol (MCP)? Cite the official spec URL and one reference implementation.}"
EXPLORE_BENCH_OUT="${EXPLORE_BENCH_OUT:-$SKILL_DIR/benchmarks/$(date +%Y-%m-%d)-websearch-rt}"
mkdir -p "$EXPLORE_BENCH_OUT"
EXPLORE_BENCH_OUT="$(cd "$EXPLORE_BENCH_OUT" && pwd)"

if ! [[ "$EXPLORE_BENCH_RUNS" =~ ^[0-9]+$ ]] || [ "$EXPLORE_BENCH_RUNS" -lt 1 ]; then
  printf 'bench-websearch-rt: EXPLORE_BENCH_RUNS must be a positive integer\n' >&2
  exit 2
fi

command -v agy >/dev/null 2>&1 || {
  printf 'bench-websearch-rt: agy not found on PATH (required for gemini arm)\n' >&2
  exit 127
}

if [ ! -f "${EXPLORE_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json}" ]; then
  printf 'bench-websearch-rt: Codex auth not found (run: codex login)\n' >&2
  exit 1
fi

if [ -z "${EXA_API_KEY:-}" ]; then
  printf 'bench-websearch-rt: EXA_API_KEY not set (required for gemini arm)\n' >&2
  exit 1
fi

RESULTS="$EXPLORE_BENCH_OUT/results.tsv"
printf 'transport\twall_s\tok\tout_bytes\tquality_index\turl_count\tsection_score\tscope_ok\texplore_ms\tsubmit_ms\tsearches\turls_fetched\ttool_calls\n' > "$RESULTS"

score_websearch_out() {
  local out_md="$1"
  node --input-type=module -e "
import { readFileSync } from 'node:fs';
import { scoreWebsearchOutput } from './lib/bench-websearch-scorer.mjs';
const s = scoreWebsearchOutput(readFileSync(process.argv[1], 'utf8'));
const qi = (s.sections || 0) * 10 + (s.urlCount || 0) * 3 + (s.scopeOk ? 5 : 0);
console.log(JSON.stringify({ qualityIndex: qi, urlCount: s.urlCount, sections: s.sections, scopeOk: s.scopeOk }));
" "$out_md"
}

parse_ws_phase_metrics() {
  local err_file="$1"
  explore_ms=""
  submit_ms=""
  searches=""
  urls_fetched=""
  tool_calls=""
  [ -s "$err_file" ] || return 0
  local phase_line
  phase_line="$(grep '^phase search_ms=' "$err_file" | tail -1 || true)"
  if [ -n "$phase_line" ]; then
    explore_ms="$(printf '%s' "$phase_line" | sed -n 's/.*search_ms=\([0-9]*\).*/\1/p')"
    submit_ms="$(printf '%s' "$phase_line" | sed -n 's/.*submit_ms=\([0-9]*\).*/\1/p')"
    searches="$(printf '%s' "$phase_line" | sed -n 's/.*searches=\([0-9]*\).*/\1/p')"
    urls_fetched="$(printf '%s' "$phase_line" | sed -n 's/.*urls_fetched=\([0-9]*\).*/\1/p')"
    tool_calls="$(printf '%s' "$phase_line" | sed -n 's/.*fetches=\([0-9]*\).*/\1/p')"
  fi
}

run_one() {
  local transport="$1"
  local n="$2"
  local label="${transport}-${n}"
  local runs_dir="$EXPLORE_BENCH_OUT/$label"
  local run_dir
  local start end elapsed out_bytes ok=0
  local script
  local score_json quality_index url_count section_score scope_ok
  local explore_ms="" submit_ms="" searches="" urls_fetched="" tool_calls=""

  mkdir -p "$runs_dir"
  if [ "$transport" = "gemini" ]; then
    script="$SCRIPT_DIR/websearch-gemini.sh"
  else
    script="$SCRIPT_DIR/websearch-rt.sh"
  fi

  start="$(python3 -c 'import time; print(time.time())')"
  if env EXPLORE_WIRE_FORMAT=1 EXPLORE_WORKSPACE="$EXPLORE_BENCH_WORKSPACE" \
    EXPLORE_RUNS_DIR="$runs_dir/runs" EXPLORE_RUN_ID="$label" \
    "$script" "$EXPLORE_BENCH_QUERY" >"$runs_dir/stdout" 2>"$runs_dir/stderr"; then
    ok=1
  fi
  end="$(python3 -c 'import time; print(time.time())')"
  elapsed="$(python3 -c "print(round($end - $start, 2))")"
  run_dir="$runs_dir/runs/$label"
  out_bytes=0
  quality_index=0
  url_count=0
  section_score=0
  scope_ok=0
  local out_file=""
  if [ -f "$run_dir/out.md" ]; then
    out_file="$run_dir/out.md"
  elif [ -s "$runs_dir/stdout" ]; then
    out_file="$runs_dir/stdout"
  fi

  if [ -n "$out_file" ]; then
    out_bytes="$(wc -c < "$out_file" | tr -d ' ')"
    score_json="$(cd "$SCRIPT_DIR" && score_websearch_out "$out_file")"
    read -r quality_index url_count section_score scope_ok <<EOF
$(python3 -c '
import json, sys
s = json.loads(sys.stdin.read())
print(s.get("qualityIndex", 0), s.get("urlCount", 0), s.get("sections", 0), 1 if s.get("scopeOk") else 0)
' <<< "$score_json")
EOF
    [ -s "$out_file" ] || ok=0
  else
    ok=0
  fi
  if [ "$transport" = "rt" ] && [ -f "$run_dir/err.log" ]; then
    parse_ws_phase_metrics "$run_dir/err.log"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$transport" "$elapsed" "$ok" "$out_bytes" \
    "$quality_index" "$url_count" "$section_score" "$scope_ok" \
    "${explore_ms:-}" "${submit_ms:-}" "${searches:-}" "${urls_fetched:-}" "${tool_calls:-}" >> "$RESULTS"
}

export EXPLORE_BENCH_OUT EXPLORE_BENCH_QUERY EXPLORE_BENCH_WORKSPACE EXPLORE_BENCH_RUNS

printf 'bench-websearch-rt\n'
printf 'workspace: %s\n' "$EXPLORE_BENCH_WORKSPACE"
printf 'query: %s\n' "$EXPLORE_BENCH_QUERY"
printf 'paired runs per transport: %s\n\n' "$EXPLORE_BENCH_RUNS"

i=1
while [ "$i" -le "$EXPLORE_BENCH_RUNS" ]; do
  run_one gemini "$i"
  run_one rt "$i"
  i=$((i + 1))
done

python3 - <<'PY'
import os, statistics as st
from collections import defaultdict

out_dir = os.environ["EXPLORE_BENCH_OUT"]
results_path = os.path.join(out_dir, "results.tsv")
rows = defaultdict(list)
with open(results_path) as f:
    next(f, None)
    for line in f:
        parts = line.rstrip().split("\t")
        transport = parts[0]
        wall, ok = float(parts[1]), int(parts[2])
        out_bytes = int(parts[3])
        qi, urls, sections, scope = [int(x) for x in parts[4:8]]
        rows[transport].append((wall, ok, out_bytes, qi, urls, sections, scope))

def summarize(transport):
    data = rows.get(transport, [])
    if not data:
        return None
    walls = [d[0] for d in data]
    ok_rate = sum(d[1] for d in data) / len(data)
    qi_med = st.median([d[3] for d in data])
    return {
        "transport": transport,
        "ok_rate": ok_rate,
        "median_wall": st.median(walls),
        "median_qi": qi_med,
    }

summary = []
for transport in ("gemini", "rt"):
    s = summarize(transport)
    if s:
        summary.append(
            f"{transport}: median_wall={s['median_wall']:.2f}s ok_rate={s['ok_rate']:.0%} median_qi={s['median_qi']:.0f}"
        )

gem = rows.get("gemini", [])
rt = rows.get("rt", [])
if gem and rt:
    delta = st.median([d[0] for d in rt]) - st.median([d[0] for d in gem])
    summary.append(f"rt_vs_gemini_wall_delta_s={delta:+.2f}")

lines = ["| Transport | OK rate | Median wall | Median QI |", "|---|---|---|---|"]
for transport, label in (("gemini", "gemini"), ("rt", "RT (alpha/web_run)")):
    s = summarize(transport)
    if s:
        lines.append(
            f"| {label} | {s['ok_rate']:.0%} | {s['median_wall']:.1f}s | {s['median_qi']:.0f} |"
        )

report = "\n".join(summary)
table = "\n".join(lines)
print(report)
print("")
print(table)
with open(os.path.join(out_dir, "summary.txt"), "w") as f:
    f.write(report + "\n\n" + table + "\n")
PY

printf '\nresults: %s\nsummary: %s/summary.txt\n' "$RESULTS" "$EXPLORE_BENCH_OUT"
