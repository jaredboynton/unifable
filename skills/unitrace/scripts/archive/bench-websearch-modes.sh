#!/usr/bin/env bash
# bench-websearch-modes.sh: benchmark RT websearch on QUALITY (structural QI +
# saved outputs for manual judging) as well as speed.
#
# Arms (alpha backend, swarm is the sole mode; fanout/deepen/search-open/
# search-only/combined modes and the exa RT backend + ensemble mode are retired):
#   alpha-swarm     parallel fanout source-class strategies + deepen aspect facets, one open pass
#
# Usage:
#   ~/.agents/skills/explore/scripts/bench-websearch-modes.sh
#
# Env:
#   UNITRACE_BENCH_QUERY      research goal (default: novel-improvement query for these scripts)
#   UNITRACE_BENCH_RUNS       runs per arm (default: 2)
#   UNITRACE_BENCH_WORKSPACE  workspace root (default: pwd)
#   UNITRACE_BENCH_OUT        results dir (default: benchmarks/YYYY-MM-DD-websearch-modes)
#   UNITRACE_BENCH_ARMS       space-separated arm list (default: alpha-swarm)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

UNITRACE_BENCH_RUNS="${UNITRACE_BENCH_RUNS:-2}"
UNITRACE_BENCH_WORKSPACE="${UNITRACE_BENCH_WORKSPACE:-$PWD}"
DEFAULT_QUERY="What are novel, proven techniques to improve a realtime LLM websearch pipeline built on gpt-realtime-2 driving OpenAI's web.run / alpha-search API (search_query plus open/fetch commands) over a Codex OAuth transport, with a search -> fetch -> submit architecture? Cover: getting full page content instead of search snippets, cutting end-to-end latency, improving source authority and citation/proof quality, and request coalescing or parallelism. For each technique cite a primary source URL (OpenAI docs, the openai/codex repo, papers, or production agent/RAG search systems) and name one reference implementation."
UNITRACE_BENCH_QUERY="${UNITRACE_BENCH_QUERY:-$DEFAULT_QUERY}"
UNITRACE_BENCH_OUT="${UNITRACE_BENCH_OUT:-$SKILL_DIR/benchmarks/$(date +%Y-%m-%d)-websearch-modes}"
UNITRACE_BENCH_ARMS="${UNITRACE_BENCH_ARMS:-alpha-swarm}"
mkdir -p "$UNITRACE_BENCH_OUT"
UNITRACE_BENCH_OUT="$(cd "$UNITRACE_BENCH_OUT" && pwd)"

if ! [[ "$UNITRACE_BENCH_RUNS" =~ ^[0-9]+$ ]] || [ "$UNITRACE_BENCH_RUNS" -lt 1 ]; then
  printf 'bench-websearch-modes: UNITRACE_BENCH_RUNS must be a positive integer\n' >&2
  exit 2
fi
if [ ! -f "${UNITRACE_CODEX_AUTH_PATH:-${HOME}/.codex/auth.json}" ]; then
  printf 'bench-websearch-modes: Codex auth not found (run: codex login)\n' >&2
  exit 1
fi

RESULTS="$UNITRACE_BENCH_OUT/results.tsv"
printf 'arm\trun\twall_s\tok\tout_bytes\tquality_index\turl_count\tsection_score\tscope_ok\tsearch_ms\tfetch_ms\tsubmit_ms\tsearches\tfetches\turls_fetched\n' > "$RESULTS"

score_websearch_out() {
  local out_md="$1"
  (cd "$SCRIPT_DIR" && node --input-type=module -e "
import { readFileSync } from 'node:fs';
import { scoreWebsearchOutput } from './lib/bench-websearch-scorer.mjs';
const s = scoreWebsearchOutput(readFileSync(process.argv[1], 'utf8'));
const qi = (s.sections || 0) * 10 + (s.urlCount || 0) * 3 + (s.scopeOk ? 5 : 0);
console.log(JSON.stringify({ qualityIndex: qi, urlCount: s.urlCount, sections: s.sections, scopeOk: s.scopeOk }));
" "$out_md")
}

parse_ws_phase_metrics() {
  local err_file="$1"
  search_ms=""; fetch_ms=""; submit_ms=""; searches=""; fetches=""; urls_fetched=""
  [ -s "$err_file" ] || return 0
  local line
  line="$(grep '^phase search_ms=' "$err_file" | tail -1 || true)"
  [ -n "$line" ] || return 0
  search_ms="$(printf '%s' "$line" | sed -n 's/.*search_ms=\([0-9]*\).*/\1/p')"
  fetch_ms="$(printf '%s' "$line" | sed -n 's/.*fetch_ms=\([0-9]*\).*/\1/p')"
  submit_ms="$(printf '%s' "$line" | sed -n 's/.*submit_ms=\([0-9]*\).*/\1/p')"
  searches="$(printf '%s' "$line" | sed -n 's/.*searches=\([0-9]*\).*/\1/p')"
  fetches="$(printf '%s' "$line" | sed -n 's/.*fetches=\([0-9]*\).*/\1/p')"
  urls_fetched="$(printf '%s' "$line" | sed -n 's/.*urls_fetched=\([0-9]*\).*/\1/p')"
}

arm_env() {
  # Echoes "KEY=VAL KEY=VAL ..." for the given arm.
  # Swarm is the sole alpha fetch mode. The fanout/deepen/search-open/search-only/
  # combined modes, the exa RT backend, and the ensemble mode are retired — see
  # docs/benchmarks/websearch-swarm.md and websearch-frontier.md.
  case "$1" in
    alpha-swarm)    echo "UNISEARCH_WS_BACKEND=alpha" ;;
    *) echo "" ;;
  esac
}

run_one() {
  local arm="$1" n="$2"
  local label="${arm}-${n}"
  local runs_dir="$UNITRACE_BENCH_OUT/$label"
  mkdir -p "$runs_dir"
  local start end elapsed out_bytes ok=0
  local quality_index=0 url_count=0 section_score=0 scope_ok=0
  local search_ms="" fetch_ms="" submit_ms="" searches="" fetches="" urls_fetched=""
  local extra_env
  extra_env="$(arm_env "$arm")"

  start="$(python3 -c 'import time; print(time.time())')"
  if env $extra_env UNITRACE_WIRE_FORMAT=1 UNITRACE_WORKSPACE="$UNITRACE_BENCH_WORKSPACE" \
    UNITRACE_RUNS_DIR="$runs_dir/runs" UNITRACE_RUN_ID="$label" \
    "$SCRIPT_DIR/websearch-rt.sh" "$UNITRACE_BENCH_QUERY" >"$runs_dir/stdout" 2>"$runs_dir/stderr"; then
    ok=1
  fi
  end="$(python3 -c 'import time; print(time.time())')"
  elapsed="$(python3 -c "print(round($end - $start, 2))")"

  local run_dir="$runs_dir/runs/$label"
  local out_file=""
  if [ -f "$run_dir/out.md" ]; then out_file="$run_dir/out.md"; elif [ -s "$runs_dir/stdout" ]; then out_file="$runs_dir/stdout"; fi

  if [ -n "$out_file" ]; then
    out_bytes="$(wc -c < "$out_file" | tr -d ' ')"
    cp -f "$out_file" "$runs_dir/out.md"
    local score_json
    score_json="$(score_websearch_out "$out_file")"
    read -r quality_index url_count section_score scope_ok <<EOF
$(python3 -c '
import json,sys
s=json.loads(sys.stdin.read())
print(s.get("qualityIndex",0), s.get("urlCount",0), s.get("sections",0), 1 if s.get("scopeOk") else 0)
' <<< "$score_json")
EOF
    [ -s "$out_file" ] || ok=0
  else
    ok=0; out_bytes=0
  fi
  # The phase metrics line is emitted on the process stderr (captured in
  # runs_dir/stderr); err.log only holds failure text.
  if grep -q '^phase search_ms=' "$runs_dir/stderr" 2>/dev/null; then
    parse_ws_phase_metrics "$runs_dir/stderr"
  elif [ -f "$run_dir/err.log" ]; then
    parse_ws_phase_metrics "$run_dir/err.log"
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$arm" "$n" "$elapsed" "$ok" "$out_bytes" \
    "$quality_index" "$url_count" "$section_score" "$scope_ok" \
    "${search_ms:-}" "${fetch_ms:-}" "${submit_ms:-}" "${searches:-}" "${fetches:-}" "${urls_fetched:-}" >> "$RESULTS"
  printf '  %-16s run %s: wall=%ss ok=%s qi=%s urls=%s fetched=%s\n' "$arm" "$n" "$elapsed" "$ok" "$quality_index" "$url_count" "${urls_fetched:-?}"
}

printf 'bench-websearch-modes\nworkspace: %s\narms: %s\nruns per arm: %s\nquery: %s\n\n' \
  "$UNITRACE_BENCH_WORKSPACE" "$UNITRACE_BENCH_ARMS" "$UNITRACE_BENCH_RUNS" "$UNITRACE_BENCH_QUERY"

for arm in $UNITRACE_BENCH_ARMS; do
  i=1
  while [ "$i" -le "$UNITRACE_BENCH_RUNS" ]; do
    run_one "$arm" "$i"
    i=$((i + 1))
  done
done

export UNITRACE_BENCH_OUT UNITRACE_BENCH_QUERY
python3 - <<'PY'
import os, json, statistics as st
from collections import defaultdict

out_dir = os.environ["UNITRACE_BENCH_OUT"]
rows = defaultdict(list)
with open(os.path.join(out_dir, "results.tsv")) as f:
    header = next(f).rstrip().split("\t")
    for line in f:
        p = line.rstrip("\n").split("\t")
        d = dict(zip(header, p))
        rows[d["arm"]].append(d)

def med(vals):
    vals = [v for v in vals if v != ""]
    return st.median([float(v) for v in vals]) if vals else None

summary = {
    "query": os.environ["UNITRACE_BENCH_QUERY"],
    "arms": {},
    "quality_dimensions": ["correctness", "source_authority", "breadth", "novelty", "proof_quality"],
    "quality_note": "Speed + structural metrics are machine-measured here. Per-dimension quality (0-10) is judged from the saved out.md files and recorded in the docs/benchmarks report (scores.json).",
}
for arm, rs in rows.items():
    summary["arms"][arm] = {
        "runs": len(rs),
        "ok_rate": sum(int(r["ok"]) for r in rs) / len(rs),
        "median_wall_s": med([r["wall_s"] for r in rs]),
        "median_quality_index": med([r["quality_index"] for r in rs]),
        "median_url_count": med([r["url_count"] for r in rs]),
        "median_sections": med([r["section_score"] for r in rs]),
        "median_search_ms": med([r["search_ms"] for r in rs]),
        "median_fetch_ms": med([r["fetch_ms"] for r in rs]),
        "median_submit_ms": med([r["submit_ms"] for r in rs]),
        "median_urls_fetched": med([r["urls_fetched"] for r in rs]),
        "out_md": [f"{arm}-{r['run']}/out.md" for r in rs],
    }

with open(os.path.join(out_dir, "results.json"), "w") as f:
    json.dump(summary, f, indent=2)

print("\n--- per-arm summary (speed + structural) ---")
print(f"{'arm':<16}{'ok':>5}{'wall_s':>9}{'QI':>5}{'urls':>6}{'fetched':>9}{'search_ms':>11}{'fetch_ms':>10}{'submit_ms':>11}")
for arm in summary["arms"]:
    a = summary["arms"].get(arm)
    if not a:
        continue
    def s(x): return "-" if x is None else (f"{x:.2f}" if isinstance(x, float) and x < 1000 else f"{x:.0f}")
    print(f"{arm:<16}{a['ok_rate']*100:>4.0f}%{s(a['median_wall_s']):>9}{s(a['median_quality_index']):>5}{s(a['median_url_count']):>6}{s(a['median_urls_fetched']):>9}{s(a['median_search_ms']):>11}{s(a['median_fetch_ms']):>10}{s(a['median_submit_ms']):>11}")
print(f"\nresults.tsv:  {out_dir}/results.tsv")
print(f"results.json: {out_dir}/results.json")
print("out.md per arm saved under each <arm>-<run>/ dir for quality judging.")
PY

printf '\ndone: %s\n' "$UNITRACE_BENCH_OUT"
