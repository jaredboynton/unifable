#!/usr/bin/env bash
# explore/trace-cursor.sh: semantic codebase trace via the Cursor Agent CLI.
#
# Usage:
#   trace-cursor.sh "How does authentication flow through this service?"
#
# Each trace gets an isolated run directory under ${UNITRACE_RUNS_DIR}. When
# UNITRACE_RUNS_DIR is unset, runs live under $(dirname "$UNITRACE_OUT")/runs if
# UNITRACE_OUT is set, otherwise under ~/.cache/explore/runs. No shared
# "latest run" pointer is maintained; the foreground process and per-run
# directory are the result contract.
#
# Env overrides:
#   UNITRACE_MODEL              cursor-agent model slug (default: composer-2.5-fast)
#   UNITRACE_CURSOR_MODE        cursor-agent mode: ask | plan (default: ask)
#   UNITRACE_WORKSPACE          workspace dir (default: current dir)
#   UNITRACE_TRANSPORT          cli | acp | harness (default: cli)
#   UNITRACE_INDEX              repo42 to route harness codebase_search to Cursor's
#                              server-side semantic index (else local search.sh)
#   UNITRACE_FORMAT             json | raw (default: auto, cli transport only)
#   UNITRACE_CURSOR_AGENT_BIN   cursor-agent binary for hermetic tests
#   UNITRACE_OUT                optional explicit compatibility output path
#   UNITRACE_RUNS_DIR           directory for per-run state
#   UNITRACE_RUN_ID             explicit run id
#   UNITRACE_HERMETIC_HOME      use isolated HOME for cursor-agent (default: 1)
#   UNITRACE_CURSOR_HOME        explicit HOME override (wins over hermetic home)
#   UNITRACE_MAP_MODE           repo map prefetch: none | pagerank | sigmap | tandem (default: tandem)
#   UNITRACE_MAP_BUDGET         map token budget for trace prefetch (default: 1024)
#   UNITRACE_RUN_TTL_SECONDS    completed-run cleanup threshold (default: 86400)
#   UNITRACE_WIRE_FORMAT        1 = agent emits wire plaintext; script rehydrates to markdown
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=explore-hydrate.sh
. "$SCRIPT_DIR/explore-hydrate.sh"
# shellcheck source=hermetic-home.sh
. "$SCRIPT_DIR/hermetic-home.sh"
explore_apply_hermetic_default

if [ "${UNITRACE_INSIDE_TRACE_DAEMON:-}" = "1" ]; then
  printf 'explore: trace-cursor.sh is blocked inside the trace daemon; use the bundled search.sh helper or read files directly.\n' >&2
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
  echo "usage: trace-cursor.sh <question>" >&2
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

if [ -n "${CURSOR_CONVERSATION_ID:-}" ]; then
  printf 'explore: trace-cursor.sh routed to search.sh (cursor-agent session)\n' >&2
  exec "$SCRIPT_DIR/search.sh" --root "$(pwd)" "$@"
fi

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

UNITRACE_HOME="$(explore_real_home)"
if [ -n "${UNITRACE_OUT:-}" ]; then
  COMPAT_OUT_FILE="$(abs_path "$UNITRACE_OUT")"
  BASE_DIR="$(dirname "$COMPAT_OUT_FILE")"
else
  COMPAT_OUT_FILE=""
  BASE_DIR="$(abs_path "${UNITRACE_HOME}/.cache/explore")"
fi
RUNS_DIR="$(abs_path "${UNITRACE_RUNS_DIR:-${BASE_DIR}/runs}")"
mkdir -p "$BASE_DIR" "$RUNS_DIR"
CURSOR_HOME="$(explore_resolve_cursor_home "$BASE_DIR" "$UNITRACE_HOME")"

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
    printf -- '--- cursor-agent stderr (%s) ---\n' "$err_file" >&2
    cat "$err_file" >&2 2>/dev/null || true
  fi
  if [ -e "$raw_file" ]; then
    printf 'raw cursor-agent stdout (if any): %s\n' "$raw_file" >&2
  fi
}

CURSOR_AGENT_BIN="${UNITRACE_CURSOR_AGENT_BIN:-cursor-agent}"
# The zero-dependency harness transport talks to the Cursor API directly and does
# not need the cursor-agent CLI; only require it for the cli/acp transports.
if [ "${UNITRACE_TRANSPORT:-cli}" != "harness" ]; then
  command -v "$CURSOR_AGENT_BIN" >/dev/null 2>&1 || { echo "error: cursor-agent not found on PATH" >&2; exit 127; }
fi

QUESTION="$*"
MODEL="${UNITRACE_MODEL:-composer-2.5-fast}"
CURSOR_MODE="${UNITRACE_CURSOR_MODE:-ask}"
case "$CURSOR_MODE" in
  ask|plan) : ;;
  *)
    printf 'explore: UNITRACE_CURSOR_MODE must be ask or plan, got %s\n' "$CURSOR_MODE" >&2
    exit 2
    ;;
esac
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
    printf 'trace exited before completion for run %s\n' "$RUN_ID" > "$ERR_FILE"
    write_status failed 1 "trace exited before completion"
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
write_status running null "cursor-agent running"
cleanup_old_runs
WORK_DIR="$(mktemp -d "$RUN_DIR/work.XXXXXX")"
TMP_OUT="$WORK_DIR/out"
TMP_RAW="$WORK_DIR/raw"
PROMPT_FILE="$WORK_DIR/prompt.txt"

CURSOR_ARGS=(
  --print
  --trust
  --force
  --disable-project-configs
  --sandbox "${UNITRACE_SANDBOX:-disabled}"
  --exclude-tools shellToolCall,writeShellStdinToolCall,editToolCall,applyAgentDiffToolCall,deleteToolCall
  --mode "$CURSOR_MODE"
  --model "$MODEL"
  --workspace "$WORKSPACE"
)

read -r -d '' PROMPT <<EOF || true
Trace the codebase to answer the question below. Explain how it works and cite the important files/functions you used.

Do not call trace-cursor.sh, trace.sh, trace-gemini.sh, cursor-acp-trace.mjs, or this explore wrapper recursively.
EOF

MAP_BLOCK=""
if [ "${UNITRACE_MAP_MODE:-tandem}" != "none" ] && command -v node >/dev/null 2>&1; then
  MAP_OUT="$(mktemp "${TMPDIR:-/tmp}/explore-trace-map.XXXXXX")"
  if node "$SCRIPT_DIR/map.mjs" --root "$WORKSPACE" --mode "${UNITRACE_MAP_MODE:-tandem}" "$QUESTION" > "$MAP_OUT" 2>/dev/null && [ -s "$MAP_OUT" ]; then
    MAP_BLOCK="$(cat "$MAP_OUT")"
  fi
  rm -f "$MAP_OUT"
fi

if [ -n "$MAP_BLOCK" ]; then
  PROMPT="${PROMPT}

${MAP_BLOCK}
"
fi

PROMPT="${PROMPT}
QUESTION: ${QUESTION}"

if [ "${UNITRACE_WIRE_FORMAT:-0}" = "1" ] && command -v node >/dev/null 2>&1; then
  PROMPT="${PROMPT}

$(node "$SCRIPT_DIR/lib/explore-output-prompt.mjs" --trace)"
fi

printf '%s' "$PROMPT" > "$PROMPT_FILE"

FORMAT="${UNITRACE_FORMAT:-auto}"
if [ "$FORMAT" = "auto" ]; then
  if command -v jq >/dev/null 2>&1; then FORMAT="json"; else FORMAT="raw"; fi
fi

cursor_status=0
parse_status=0
RAW_SRC="$TMP_RAW"
TRANSPORT="${UNITRACE_TRANSPORT:-cli}"

if [ "$TRANSPORT" = "harness" ]; then
  HOME="$CURSOR_HOME" node "$SCRIPT_DIR/cursor-harness.mjs" \
    --prompt-file "$PROMPT_FILE" \
    --out "$TMP_OUT" \
    --raw "$TMP_RAW" \
    --err "$ERR_FILE" \
    --workspace "$WORKSPACE" \
    --model "$MODEL" \
    --frames "$RUN_DIR/frames.ndjson" || cursor_status=$?
elif [ "$TRANSPORT" = "acp" ]; then
  ACP_ARGS=(
    --prompt-file "$PROMPT_FILE"
    --out "$TMP_OUT"
    --raw "$TMP_RAW"
    --err "$ERR_FILE"
    --workspace "$WORKSPACE"
    --model "$MODEL"
  )
  if [ "${UNITRACE_ACP_STREAM:-0}" = "1" ]; then
    ACP_ARGS=(--stream "${ACP_ARGS[@]}")
  fi
  HOME="$CURSOR_HOME" node "$SCRIPT_DIR/cursor-acp-trace.mjs" "${ACP_ARGS[@]}" || cursor_status=$?
elif [ "$FORMAT" = "json" ]; then
  HOME="$CURSOR_HOME" "$CURSOR_AGENT_BIN" "${CURSOR_ARGS[@]}" --output-format json "$PROMPT" \
    > "$TMP_RAW" 2>"$ERR_FILE" || cursor_status=$?
  if [ "$cursor_status" -eq 0 ]; then
    if ! jq -er 'select(.type=="result") | .result' "$TMP_RAW" > "$TMP_OUT" 2>>"$ERR_FILE"; then
      parse_status=$?
      printf 'failed to parse cursor-agent json result for run %s\n' "$RUN_ID" >> "$ERR_FILE"
    fi
  fi
else
  RAW_SRC="$TMP_OUT"
  HOME="$CURSOR_HOME" "$CURSOR_AGENT_BIN" "${CURSOR_ARGS[@]}" "$PROMPT" \
    > "$TMP_OUT" 2>"$ERR_FILE" || cursor_status=$?
fi

if [ "${UNITRACE_WIRE_FORMAT:-0}" = "1" ] && [ -s "$TMP_OUT" ]; then
  cp -f "$TMP_OUT" "$RAW_FILE" 2>/dev/null || true
else
  cp -f "$RAW_SRC" "$RAW_FILE" 2>/dev/null || true
fi

if [ "$cursor_status" -ne 0 ]; then
  printf 'cursor-agent exited with status %s for run %s\n' "$cursor_status" "$RUN_ID" >> "$ERR_FILE"
fi

if [ "$cursor_status" -eq 0 ] && [ "$parse_status" -eq 0 ] && [ -s "$TMP_OUT" ]; then
  if explore_hydrate_trace_output "$WORKSPACE" "$TMP_OUT" "$TMP_OUT.hydrated" "$SCRIPT_DIR" ""; then
    mv -f "$TMP_OUT.hydrated" "$TMP_OUT"
  else
    rm -f "$TMP_OUT.hydrated"
  fi
  mv -f "$TMP_OUT" "$OUT_FILE"
  : > "$DONE_FILE"
  rm -f "$RUNNING_FILE"
  write_status done 0 "trace complete"
  publish_compat_success
  print_done "$RUN_DIR"
else
  failure_code="$cursor_status"
  [ "$failure_code" -eq 0 ] && failure_code=1
  if [ ! -s "$ERR_FILE" ]; then
    printf 'cursor-agent (model %s) exited with no output and no stderr; raw stdout (if any) at %s\n' \
      "$MODEL" "$RAW_FILE" > "$ERR_FILE"
  fi
  rm -f "$RUNNING_FILE"
  write_status failed "$failure_code" "trace failed"
  publish_compat_failure
  printf 'explore: no trace output captured for run %s.\n' "$RUN_ID" >&2
  printf '%s\n' "--- cursor-agent stderr ($ERR_FILE) ---" >&2
  cat "$ERR_FILE" >&2 2>/dev/null || true
  printf '%s\n' "--- raw cursor-agent stdout at $RAW_FILE ---" >&2
  exit 1
fi
