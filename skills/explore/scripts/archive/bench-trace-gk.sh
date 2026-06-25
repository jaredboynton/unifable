#!/usr/bin/env bash
# Benchmark trace-gk.sh (grok-build-0.1 via xAI API) — wall time and output metrics.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
QUESTION="${1:-How does trace.sh work end to end?}"
WORKSPACE="${EXPLORE_WORKSPACE:-$ROOT}"
MAP_MODE="${EXPLORE_MAP_MODE:-tandem}"

if [ -z "${XAI_API_KEY:-}" ]; then
  printf 'bench-trace-gk: XAI_API_KEY not set\n' >&2
  exit 1
fi

bytes_of() {
  if [ -f "$1" ]; then wc -c < "$1" | tr -d ' '; else echo 0; fi
}

score_trace_out() {
  local out_md="$1"
  local structured="${2:-}"
  local args=(--file "$out_md" --json)
  if [ -n "$structured" ] && [ -f "$structured" ]; then
    args+=(--structured "$structured")
  fi
  node "$SCRIPT_DIR/lib/bench-trace-scorer.mjs" "${args[@]}"
}

mkdir -p "$ROOT/.cache/explore"
GK_OUT="$ROOT/.cache/explore/bench-trace-gk.out"

printf '=== trace-gk.sh (grok-build-0.1) ===\n'
START=$(date +%s)
BENCH_LOG="$ROOT/.cache/explore/bench-trace-gk.log"
env -u CURSOR_CONVERSATION_ID EXPLORE_MAP_MODE="$MAP_MODE" EXPLORE_WORKSPACE="$WORKSPACE" \
  "$SCRIPT_DIR/trace-gk.sh" "$QUESTION" > "$BENCH_LOG" 2> "$ROOT/.cache/explore/bench-trace-gk.err" || true
GK_ELAPSED=$(( $(date +%s) - START ))

RUN_ID="$(grep -o 'EXPLORE_RUN_ID=[^[:space:]]*' "$BENCH_LOG" | tail -1 | cut -d= -f2 || true)"
if [ -n "$RUN_ID" ]; then
  RUN_DIR="${EXPLORE_RUNS_DIR:-${HOME}/.cache/explore/runs}/$RUN_ID"
  cp -f "$RUN_DIR/out.md" "$GK_OUT" 2>/dev/null || : > "$GK_OUT"
else
  cp -f "$BENCH_LOG" "$GK_OUT" 2>/dev/null || : > "$GK_OUT"
fi

GK_JSON=""
GK_ERR=""
if [ -n "$RUN_ID" ] && [ -d "$RUN_DIR" ]; then
  GK_JSON="$RUN_DIR/structured.json"
  GK_ERR="$RUN_DIR/err.log"
fi

score_json="$(score_trace_out "$GK_OUT" "$GK_JSON")"
read -r unique_citations section_score completeness quality_index cite_lineStart cite_inline cite_pathFirst cite_structured cite_ref <<EOF
$(python3 -c '
import json, sys
s = json.loads(sys.stdin.read())
keys = [
    "uniqueCitations", "sectionScore", "completenessScore", "qualityIndex",
    "citeLineStart", "citeInline", "citePathFirst", "citeStructured", "citeRefLabels",
]
print(" ".join(str(s.get(k, 0)) for k in keys))
' <<< "$score_json")
EOF

printf '\n| metric | value |\n'
printf '| --- | ---: |\n'
printf '| wall seconds | %s |\n' "$GK_ELAPSED"
printf '| output bytes | %s |\n' "$(bytes_of "$GK_OUT")"
printf '| unique citations | %s |\n' "$unique_citations"
printf '| section score | %s |\n' "$section_score"
printf '| completeness (0-3) | %s |\n' "$completeness"
printf '| quality index (0-100) | %s |\n' "$quality_index"
printf '| cite line-start fences | %s |\n' "$cite_lineStart"
printf '| cite inline fences | %s |\n' "$cite_inline"
printf '| cite path-first | %s |\n' "$cite_pathFirst"
printf '| cite structured json | %s |\n' "$cite_structured"
printf '| ref labels (<refN>) | %s |\n' "$cite_ref"
if [ -s "$GK_ERR" ]; then
  printf '| explore_ms | %s |\n' "$(grep -o 'explore_ms=[0-9]*' "$GK_ERR" | tail -1 | cut -d= -f2 || echo n/a)"
  printf '| submit_ms | %s |\n' "$(grep -o 'submit_ms=[0-9]*' "$GK_ERR" | tail -1 | cut -d= -f2 || echo n/a)"
fi
printf '\noutput: %s\n' "$GK_OUT"
[ -n "$RUN_ID" ] && printf 'run id: %s\n' "$RUN_ID"
[ -s "$GK_JSON" ] && printf 'structured json: %s\n' "$GK_JSON"
[ -s "$GK_ERR" ] && printf 'err log: %s\n' "$GK_ERR"
