#!/usr/bin/env bash
# explore/trace-rt.sh: semantic codebase trace via gpt-realtime-2 (Codex OAuth).
#
# Usage:
#   trace-rt.sh "How does authentication flow through this service?"
#
# Each trace gets an isolated run directory under ${UNITRACE_RUNS_DIR}. When
# UNITRACE_RUNS_DIR is unset, runs live under $(dirname "$UNITRACE_OUT")/runs if
# UNITRACE_OUT is set, otherwise under ~/.cache/explore/runs.
#
# Env overrides:
#   UNITRACE_RT_MODEL           Realtime model slug (default: gpt-realtime-2)
#   UNITRACE_CODEX_AUTH_PATH    Codex OAuth file (default: ~/.codex/auth.json)
#   UNITRACE_RT_TIMEOUT         total trace deadline seconds (default: 300)
#   UNITRACE_RT_UNITRACE_MODE    nav | agentic | hybrid (default: nav; host-driven
#                              mini navigators, fail-open to agentic explore_exec)
#   UNITRACE_RT_NAV_MODEL       navigator model (default: gpt-realtime-mini)
#   UNITRACE_RT_NAV_COUNT       parallel navigators per round (default: 8)
#   UNITRACE_RT_NAV_ROUNDS      navigator rounds (default: 1)
#   UNITRACE_RT_DAEMON          daemon-pool submit synthesis (default: 1)
#   UNITRACE_RT_SYNTH_MODEL     submit synthesis model (default: gpt-realtime-2)
#   UNITRACE_RT_UNITRACE_MAX_TURNS explore tool cap (default: 3)
#   UNITRACE_RT_UNITRACE_MAX_READS hard cap on read_file paths (default: 14)
#   UNITRACE_RT_UNITRACE_MIN_READS early stop once explore stops (default: 4)
#   UNITRACE_RT_STOP_READS        stop explore after this many files read (default: 6)
#   UNITRACE_RT_STOP_TOOL_CALLS   stop explore after this many explore_exec calls (default: 2)
#   UNITRACE_RT_MAP_SEED          host-prefetch seed reads before turn 1 (default: 1)
#   UNITRACE_RT_SEED_MAX          max seed files (default: 4)
#   UNITRACE_RT_SEED_LINES        lines per seed read (default: 120)
#   UNITRACE_RT_EXEC_RESULT_MAX   max explore_exec JSON result bytes (default: 32000)
#   UNITRACE_RT_READ_EXCERPT_MAX  chars per file in submit packet (default: 900)
#   UNITRACE_RT_SUBMIT_EXCERPT_FILES max files in submit excerpts (default: 5)
#   UNITRACE_RT_SUBMIT_PACKET_MAX max submit packet chars (default: 45000)
#   UNITRACE_RT_SUBMIT_REASK    one reask on validation failure (default: 1)
#   UNITRACE_RT_PARALLEL_TOOL_CALLS enable parallel explore_exec calls in explore (default: 1)
#   UNITRACE_RT_EXEC_TIMEOUT_MS   per explore_exec wall clock ms (default: 25000)
#   UNITRACE_RT_SUBMIT_FRESH_CONTEXT  delete explore items before submit (default: 1; reconnect to force)
#   UNITRACE_RT_SUBMIT_TRANSPORT  rt | wire-rt (default: rt)
#   UNITRACE_RT_UNITRACE_REASONING_EFFORT  explore phase reasoning (default: none, omit + steer)
#   UNITRACE_RT_SUBMIT_REASONING_EFFORT  submit phase reasoning (default: low)
#   UNITRACE_RT_REASONING_EFFORT  optional override for both phases
#   UNITRACE_RT_SUBMIT_SLIM_SCHEMA  dynamic slim submit schema (default: 1)
#   UNITRACE_RT_HOST_PASSAGES       host-assembled code_passages (default: 1)
#   UNITRACE_RT_SUBMIT_POINTER_INDEX  pointer READ INDEX + citation_spans submit (default: 1)
#   UNITRACE_RT_SEED_FROM_MAP       map line-range seed reads (default: 1)
#   UNITRACE_RT_MAP_COMPACT         compact map in explore prompt (default: 1)
#   UNITRACE_RT_MAP_COMPACT_SUBMIT  compact map paths in submit packet (default: 1)
#   UNITRACE_RT_UNITRACE_TOOL_REQUIRED  require explore_exec on turn 1 when seeded (default: 1)
#   UNITRACE_WORKSPACE          workspace dir (default: current dir)
#   UNITRACE_OUT                optional explicit compatibility output path
#   UNITRACE_RUNS_DIR           directory for per-run state
#   UNITRACE_RUN_ID             explicit run id
#   UNITRACE_MAP_MODE           repo map prefetch: none | pagerank | sigmap | tandem (default: tandem)
#   UNITRACE_MAP_BUDGET         map token budget for trace prefetch (default: 1024)
#   UNITRACE_RUN_TTL_SECONDS    completed-run cleanup threshold (default: 86400)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=explore-hydrate.sh
. "$SCRIPT_DIR/explore-hydrate.sh"

if [ "${UNITRACE_INSIDE_TRACE_DAEMON:-}" = "1" ]; then
  printf 'explore: trace-rt.sh is blocked inside the trace daemon; use search.sh or read files directly.\n' >&2
  exit 2
fi

case "${1:-}" in
  --help|-h)
    awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  --*)
    printf 'explore: unknown flag %s (expected a quoted question)\n' "$1" >&2
    exit 2
    ;;
esac

if [ "$#" -eq 0 ]; then
  echo "usage: trace-rt.sh <question>" >&2
  exit 2
fi

for arg in "$@"; do
  case "$arg" in
    --*)
      printf 'explore: control flags are not accepted after the question; pass one quoted question\n' >&2
      exit 2
      ;;
  esac
done

abs_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}

valid_run_id() {
  [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] && [ "$1" != "." ] && [ "$1" != ".." ]
}

require_abs_env_path() {
  local name="$1"
  local value="${!name:-}"
  if [ -n "$value" ]; then
    case "$value" in
      /*) : ;;
      *)
        printf 'explore: %s must be an absolute path when set: %s\n' "$name" "$value" >&2
        exit 2
        ;;
    esac
  fi
}

require_abs_env_path UNITRACE_OUT
require_abs_env_path UNITRACE_RUNS_DIR

UNITRACE_HOME="${HOME:-$(cd ~ && pwd)}"
if [ -n "${UNITRACE_OUT:-}" ]; then
  COMPAT_OUT_FILE="$(abs_path "$UNITRACE_OUT")"
  BASE_DIR="$(dirname "$COMPAT_OUT_FILE")"
else
  COMPAT_OUT_FILE=""
  BASE_DIR="$(abs_path "${UNITRACE_HOME}/.cache/explore")"
fi
RUNS_DIR="$(abs_path "${UNITRACE_RUNS_DIR:-${BASE_DIR}/runs}")"
mkdir -p "$BASE_DIR" "$RUNS_DIR"

stat_mtime() {
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/$/\\n/' | tr -d '\n' | sed 's/\\n$//'
}

run_dir_for() {
  printf '%s/%s\n' "$RUNS_DIR" "$1"
}

run_id_from_dir() {
  basename "$1"
}

trace_state() {
  local run_dir="$1"
  local out_file="$run_dir/out.md"
  local err_file="$run_dir/err.log"
  local done_file="$run_dir/done"
  local running_file="$run_dir/running"
  if [ -f "$done_file" ] && [ -s "$out_file" ]; then
    echo "done"; return
  fi
  if [ -f "$running_file" ]; then
    local pid mtime age
    pid="$(cat "$running_file" 2>/dev/null || true)"
    mtime="$(stat_mtime "$running_file")"
    age=$(( $(date +%s) - mtime ))
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ "$age" -lt 600 ]; then
      echo "running"; return
    fi
  fi
  if [ -s "$err_file" ]; then echo "failed"; return; fi
  echo "none"
}

print_done() {
  local run_dir="$1"
  local run_id out_file
  run_id="$(run_id_from_dir "$run_dir")"
  out_file="$run_dir/out.md"
  cat "$out_file"
  printf '\n---\n[explore: full trace saved to %s]\n[explore: run id %s]\nUNITRACE_RUN_ID=%s\n' "$out_file" "$run_id" "$run_id"
}

print_failure() {
  local run_dir="$1"
  local run_id err_file raw_file status_file
  run_id="$(run_id_from_dir "$run_dir")"
  err_file="$run_dir/err.log"
  raw_file="$run_dir/raw"
  status_file="$run_dir/status.json"
  printf 'explore: no completed trace available for run %s.\n' "$run_id" >&2
  if [ -s "$status_file" ]; then
    printf -- '--- trace status (%s) ---\n' "$status_file" >&2
    cat "$status_file" >&2 2>/dev/null || true
  fi
  if [ -s "$err_file" ]; then
    printf -- '--- realtime-trace stderr (%s) ---\n' "$err_file" >&2
    cat "$err_file" >&2 2>/dev/null || true
  fi
  if [ -e "$raw_file" ]; then
    printf 'raw realtime-trace stdout (if any): %s\n' "$raw_file" >&2
  fi
}

command -v node >/dev/null 2>&1 || { echo "error: node not found on PATH" >&2; exit 127; }

CODEX_AUTH="${UNITRACE_CODEX_AUTH_PATH:-${HOME:-$(cd ~ && pwd)}/.codex/auth.json}"
if [ ! -f "$CODEX_AUTH" ]; then
  printf 'error: Codex auth not found at %s\n' "$CODEX_AUTH" >&2
  printf '  run: codex login\n' >&2
  exit 1
fi

QUESTION="$*"
MODEL="${UNITRACE_RT_MODEL:-gpt-realtime-2}"
WORKSPACE="${UNITRACE_WORKSPACE:-$PWD}"
WORKSPACE="$(abs_path "$WORKSPACE")"
export UNITRACE_WORKSPACE="$WORKSPACE"
export UNITRACE_INSIDE_TRACE_DAEMON=1
RUN_ID="${UNITRACE_RUN_ID:-$(date +%Y%m%dT%H%M%S)-$$-$RANDOM}"
if ! valid_run_id "$RUN_ID"; then
  printf 'explore: invalid run id %s (allowed: letters, numbers, dot, underscore, dash)\n' "$RUN_ID" >&2
  exit 2
fi
RUN_DIR="$(run_dir_for "$RUN_ID")"
OUT_FILE="$RUN_DIR/out.md"
RAW_FILE="$RUN_DIR/raw"
ERR_FILE="$RUN_DIR/err.log"
DONE_FILE="$RUN_DIR/done"
RUNNING_FILE="$RUN_DIR/running"
STATUS_FILE="$RUN_DIR/status.json"
WORK_DIR=""

write_status() {
  local state="$1"
  local exit_code_json="${2:-null}"
  local message="${3:-}"
  printf '{"run_id":"%s","state":"%s","pid":%s,"model":"%s","workspace":"%s","message":"%s","updated_at":%s,"exit_code":%s}\n' \
    "$(json_escape "$RUN_ID")" \
    "$(json_escape "$state")" \
    "$$" \
    "$(json_escape "$MODEL")" \
    "$(json_escape "$WORKSPACE")" \
    "$(json_escape "$message")" \
    "$(date +%s)" \
    "$exit_code_json" > "$STATUS_FILE"
}

publish_compat_success() {
  [ -n "$COMPAT_OUT_FILE" ] || return 0
  cp -f "$OUT_FILE" "$COMPAT_OUT_FILE" 2>/dev/null || true
  cp -f "$RAW_FILE" "${COMPAT_OUT_FILE}.raw" 2>/dev/null || true
  : > "${COMPAT_OUT_FILE}.err"
  : > "${COMPAT_OUT_FILE}.done"
  rm -f "${COMPAT_OUT_FILE}.running"
  printf '%s\n' "$RUN_ID" > "${COMPAT_OUT_FILE}.run" 2>/dev/null || true
}

publish_compat_failure() {
  [ -n "$COMPAT_OUT_FILE" ] || return 0
  cp -f "$ERR_FILE" "${COMPAT_OUT_FILE}.err" 2>/dev/null || true
  cp -f "$RAW_FILE" "${COMPAT_OUT_FILE}.raw" 2>/dev/null || true
  rm -f "${COMPAT_OUT_FILE}.done" "${COMPAT_OUT_FILE}.running"
  printf '%s\n' "$RUN_ID" > "${COMPAT_OUT_FILE}.run" 2>/dev/null || true
}

cleanup_current() {
  set +e
  [ -n "${WORK_DIR:-}" ] && rm -rf "$WORK_DIR"
  rm -f "$RUNNING_FILE"
  if [ ! -f "$DONE_FILE" ] && [ ! -s "$ERR_FILE" ]; then
    printf 'trace-rt exited before completion for run %s\n' "$RUN_ID" > "$ERR_FILE"
    write_status failed 1 "trace-rt exited before completion"
    publish_compat_failure
  fi
}
trap cleanup_current EXIT

cleanup_old_runs() {
  local ttl="${UNITRACE_RUN_TTL_SECONDS:-86400}"
  local now dir mtime age state
  now="$(date +%s)"
  for dir in "$RUNS_DIR"/*; do
    [ -d "$dir" ] || continue
    [ "$dir" = "$RUN_DIR" ] && continue
    [ -e "$dir/status.json" ] || [ -e "$dir/done" ] || [ -e "$dir/running" ] || [ -e "$dir/err.log" ] || [ -e "$dir/out.md" ] || continue
    state="$(trace_state "$dir")"
    [ "$state" = "running" ] && continue
    mtime="$(stat_mtime "$dir")"
    age=$((now - mtime))
    [ "$age" -gt "$ttl" ] && rm -rf "$dir"
  done
  return 0
}

mkdir -p "$RUN_DIR"
echo "$$" > "$RUNNING_FILE"
write_status running null "realtime-trace running"
cleanup_old_runs
WORK_DIR="$(mktemp -d "$RUN_DIR/work.XXXXXX")"
TMP_OUT="$WORK_DIR/out"
TMP_RAW="$WORK_DIR/raw"
PROMPT_FILE="$WORK_DIR/prompt.txt"
SUBMIT_PROMPT_FILE="$WORK_DIR/submit-prompt.txt"
STRUCTURED_JSON="$RUN_DIR/structured.json"

read -r -d '' UNITRACE_PROMPT <<EOF || true
Explore the codebase to gather ground truth for the question below. Do NOT write the final answer yet.

Use explore_exec only. Write JavaScript that orchestrates read-only tools in parallel via Promise.all.

Requirements:
1. Orient from REPO MAP, then explore_exec with Promise.all([tools.grep(...), tools.read({path, start_line, end_line}), ...]).
2. Read load-bearing entry points and direct callees under lib/ — targeted line ranges, not whole files when possible.
3. Prefer 4-8 read paths for a complete trace; stop after 2-3 explore_exec turns.
4. tools.read supports start_line/end_line; tools.batch_read accepts reads: [{path, start_line, end_line}].
5. Skip tests, benchmarks, and tangential helpers unless the question requires them.

Example explore_exec body:
const [hits, entry] = await Promise.all([
  tools.grep({ pattern: "runExplorePhase", glob: "scripts/**/*.mjs" }),
  tools.read({ path: "scripts/realtime-trace.mjs", start_line: 203, end_line: 303 }),
]);
return { hitCount: hits.hitCount, paths: hits.hits.map(h => h.path), entry };

Do not call unitrace.sh, trace-gemini.sh, trace-rt.sh, realtime-trace.mjs, or this explore wrapper recursively.
EOF

read -r -d '' SUBMIT_PROMPT <<EOF || true
Synthesize the structured trace from explore evidence. Call submit_trace once with a complete object.

Requirements:
1. opening_summary: direct answer, <= 120 words.
2. flow_steps: 4-8 short ordered pipeline strings.
3. comparison_tables: REQUIRED non-empty when the question contrasts tools, modes, or code paths.
4. sections: one per major script/module only (<= 45 words each).
5. key_files: load-bearing files only (not every file read).
6. code_passages: at most 5; file_path must be copied exactly from CODE_PASSAGES FILE_PATH ENUM; spans <= 40 lines each.
7. grounding_manifest: echo files_read and tool_turns from the submit packet.

Repo-map, grep-only, list_dir-only, and explore_exec-only paths are context, not valid code_passages citations.
Ground every claim in tool output, not memory.
EOF

if [ "${UNITRACE_WIRE_FORMAT:-0}" = "1" ] && command -v node >/dev/null 2>&1; then
  SUBMIT_PROMPT="$(node "$SCRIPT_DIR/lib/explore-output-prompt.mjs" --gk-submit)"
fi

MAP_FILE="$WORK_DIR/map.txt"
MAP_BLOCK=""
if [ "${UNITRACE_MAP_MODE:-tandem}" != "none" ] && command -v node >/dev/null 2>&1; then
  MAP_OUT="$(mktemp "${TMPDIR:-/tmp}/explore-trace-map.XXXXXX")"
  if node "$SCRIPT_DIR/map.mjs" --root "$WORKSPACE" --mode "${UNITRACE_MAP_MODE:-tandem}" "$QUESTION" > "$MAP_OUT" 2>/dev/null && [ -s "$MAP_OUT" ]; then
    MAP_BLOCK="$(cat "$MAP_OUT")"
  fi
  rm -f "$MAP_OUT"
fi

if [ -n "$MAP_BLOCK" ]; then
  printf '%s' "$MAP_BLOCK" > "$MAP_FILE"
  UNITRACE_MAP_BODY="$MAP_BLOCK"
  if [ "${UNITRACE_RT_MAP_COMPACT:-1}" = "1" ]; then
    UNITRACE_MAP_BODY="$(node --input-type=module -e "
import { compactMapBlock } from './lib/rt-trace-utils.mjs';
const fs = await import('node:fs');
console.log(compactMapBlock(fs.readFileSync(process.argv[1], 'utf8')));
" "$MAP_FILE" 2>/dev/null || printf '%s' "$MAP_BLOCK")"
  fi
  UNITRACE_PROMPT="${UNITRACE_PROMPT}

REPO MAP:
${UNITRACE_MAP_BODY}
"
else
  : > "$MAP_FILE"
fi

UNITRACE_PROMPT="${UNITRACE_PROMPT}
QUESTION: ${QUESTION}"

printf '%s' "$UNITRACE_PROMPT" > "$PROMPT_FILE"
printf '%s' "$SUBMIT_PROMPT" > "$SUBMIT_PROMPT_FILE"

RT_ARGS=(
  --prompt-file "$PROMPT_FILE"
  --map-file "$MAP_FILE"
  --question "$QUESTION"
  --workspace "$WORKSPACE"
  --out "$TMP_OUT"
  --raw "$TMP_RAW"
  --err "$ERR_FILE"
  --model "$MODEL"
  --auth-path "$CODEX_AUTH"
  --frames "$RUN_DIR/frames.ndjson"
)

RT_ARGS+=(--submit-prompt-file "$SUBMIT_PROMPT_FILE" --structured-out "$STRUCTURED_JSON")
if [ "${UNITRACE_WIRE_FORMAT:-0}" = "1" ]; then
  RT_ARGS+=(--wire 1)
fi

trace_status=0
node "$SCRIPT_DIR/realtime-trace.mjs" "${RT_ARGS[@]}" || trace_status=$?

cp -f "$TMP_RAW" "$RAW_FILE" 2>/dev/null || true

if [ "$trace_status" -ne 0 ]; then
  printf 'realtime-trace exited with status %s for run %s\n' "$trace_status" "$RUN_ID" >> "$ERR_FILE"
fi

if [ "$trace_status" -eq 0 ] && [ -s "$TMP_OUT" ]; then
  if [ "${UNITRACE_WIRE_FORMAT:-0}" = "1" ]; then
    cp -f "$TMP_OUT" "$RAW_FILE" 2>/dev/null || true
    if explore_hydrate_trace_output "$WORKSPACE" "$TMP_OUT" "$TMP_OUT.hydrated" "$SCRIPT_DIR" ""; then
      mv -f "$TMP_OUT.hydrated" "$TMP_OUT"
    else
      rm -f "$TMP_OUT.hydrated"
    fi
  fi
  mv -f "$TMP_OUT" "$OUT_FILE"
  : > "$DONE_FILE"
  rm -f "$RUNNING_FILE"
  write_status done 0 "trace complete"
  publish_compat_success
  print_done "$RUN_DIR"
else
  failure_code="$trace_status"
  [ "$failure_code" -eq 0 ] && failure_code=1
  if [ ! -s "$ERR_FILE" ]; then
    printf 'realtime-trace (model %s) exited with no output and no stderr; raw stdout (if any) at %s\n' \
      "$MODEL" "$RAW_FILE" > "$ERR_FILE"
  fi
  rm -f "$RUNNING_FILE"
  write_status failed "$failure_code" "trace failed"
  publish_compat_failure
  printf 'explore: no trace output captured for run %s.\n' "$RUN_ID" >&2
  printf '%s\n' "--- realtime-trace stderr ($ERR_FILE) ---" >&2
  cat "$ERR_FILE" >&2 2>/dev/null || true
  printf '%s\n' "--- raw realtime-trace stdout at $RAW_FILE ---" >&2
  exit 1
fi
