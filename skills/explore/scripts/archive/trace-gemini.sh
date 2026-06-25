#!/usr/bin/env bash
# explore/trace-gemini.sh: semantic codebase trace via Gemini CLI (gemini-3.1-flash-lite).
#
# Usage:
#   trace-gemini.sh "How does authentication flow through this service?"
#
# Each trace gets an isolated run directory under ${EXPLORE_RUNS_DIR}. When
# EXPLORE_RUNS_DIR is unset, runs live under $(dirname "$EXPLORE_OUT")/runs if
# EXPLORE_OUT is set, otherwise under ~/.cache/explore/runs.
#
# Env overrides:
#   EXPLORE_GM_MODEL           model slug (default: gemini-3.1-flash-lite)
#   EXPLORE_GM_BIN             gemini binary (default: gemini)
#   EXPLORE_GM_TIMEOUT         per-trace deadline seconds (default: 600)
#   EXPLORE_GM_OUTPUT_FORMAT   json | text (default: json)
#   EXPLORE_WORKSPACE          workspace dir (default: current dir)
#   EXPLORE_OUT                optional explicit compatibility output path
#   EXPLORE_RUNS_DIR           directory for per-run state
#   EXPLORE_RUN_ID             explicit run id
#   EXPLORE_MAP_MODE           repo map prefetch: none | pagerank | sigmap | tandem (default: tandem)
#   EXPLORE_MAP_BUDGET         map token budget for trace prefetch (default: 1024)
#   EXPLORE_RUN_TTL_SECONDS    completed-run cleanup threshold (default: 86400)
#   EXPLORE_WIRE_FORMAT        1 = agent emits wire plaintext; script rehydrates to markdown
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=explore-hydrate.sh
. "$SCRIPT_DIR/explore-hydrate.sh"

if [ "${EXPLORE_INSIDE_TRACE_DAEMON:-}" = "1" ]; then
  printf 'explore: trace-gemini.sh is blocked inside the trace daemon; use search.sh or read files directly.\n' >&2
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
  echo "usage: trace-gemini.sh <question>" >&2
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

require_abs_env_path EXPLORE_OUT
require_abs_env_path EXPLORE_RUNS_DIR

EXPLORE_HOME="${HOME:-$(cd ~ && pwd)}"
if [ -n "${EXPLORE_OUT:-}" ]; then
  COMPAT_OUT_FILE="$(abs_path "$EXPLORE_OUT")"
  BASE_DIR="$(dirname "$COMPAT_OUT_FILE")"
else
  COMPAT_OUT_FILE=""
  BASE_DIR="$(abs_path "${EXPLORE_HOME}/.cache/explore")"
fi
RUNS_DIR="$(abs_path "${EXPLORE_RUNS_DIR:-${BASE_DIR}/runs}")"
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
  printf '\n---\n[explore: full trace saved to %s]\n[explore: run id %s]\nEXPLORE_RUN_ID=%s\n' "$out_file" "$run_id" "$run_id"
}

command -v node >/dev/null 2>&1 || { echo "error: node not found on PATH" >&2; exit 127; }

GEMINI_BIN="${EXPLORE_GM_BIN:-gemini}"
if ! command -v "$GEMINI_BIN" >/dev/null 2>&1; then
  printf 'error: gemini CLI not found on PATH (set EXPLORE_GM_BIN or install Gemini CLI)\n' >&2
  exit 127
fi

QUESTION="$*"
MODEL="${EXPLORE_GM_MODEL:-gemini-3.1-flash-lite}"
WORKSPACE="${EXPLORE_WORKSPACE:-$PWD}"
WORKSPACE="$(abs_path "$WORKSPACE")"
export EXPLORE_WORKSPACE="$WORKSPACE"
export EXPLORE_INSIDE_TRACE_DAEMON=1
RUN_ID="${EXPLORE_RUN_ID:-$(date +%Y%m%dT%H%M%S)-$$-$RANDOM}"
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
    printf 'trace-gm exited before completion for run %s\n' "$RUN_ID" > "$ERR_FILE"
    write_status failed 1 "trace-gm exited before completion"
    publish_compat_failure
  fi
}
trap cleanup_current EXIT

cleanup_old_runs() {
  local ttl="${EXPLORE_RUN_TTL_SECONDS:-86400}"
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
write_status running null "gemini-trace running"
cleanup_old_runs
WORK_DIR="$(mktemp -d "$RUN_DIR/work.XXXXXX")"
TMP_OUT="$WORK_DIR/out"
TMP_RAW="$WORK_DIR/raw"
PROMPT_FILE="$WORK_DIR/prompt.txt"

read -r -d '' PROMPT <<EOF || true
Trace the codebase to answer the question below. Be thorough: follow the full end-to-end pipeline across scripts, lib/ helpers, and transport backends — not just the entry file. Cite load-bearing code with many precise file spans.

Do not call trace.sh, trace-gemini.sh, trace-cursor.sh, gemini, or this explore wrapper recursively.
EOF

if [ "${EXPLORE_WIRE_FORMAT:-0}" != "1" ]; then
  read -r -d '' _CITE_RULES <<'EOF' || true

Code references MUST use ```startLine:endLine:relative/path opening fences (no language tag on the opening fence).
Example: ```287:297:scripts/trace-cursor.sh
EOF
  PROMPT="${PROMPT}${_CITE_RULES}"
fi

MAP_BLOCK=""
if [ "${EXPLORE_MAP_MODE:-tandem}" != "none" ] && command -v node >/dev/null 2>&1; then
  MAP_OUT="$(mktemp "${TMPDIR:-/tmp}/explore-trace-map.XXXXXX")"
  if node "$SCRIPT_DIR/map.mjs" --root "$WORKSPACE" --mode "${EXPLORE_MAP_MODE:-tandem}" "$QUESTION" > "$MAP_OUT" 2>/dev/null && [ -s "$MAP_OUT" ]; then
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

if [ "${EXPLORE_WIRE_FORMAT:-0}" = "1" ] && command -v node >/dev/null 2>&1; then
  PROMPT="${PROMPT}

$(node "$SCRIPT_DIR/lib/explore-output-prompt.mjs" --trace-gm)"
fi

printf '%s' "$PROMPT" > "$PROMPT_FILE"

trace_status=0
node "$SCRIPT_DIR/gemini-trace.mjs" \
  --prompt-file "$PROMPT_FILE" \
  --workspace "$WORKSPACE" \
  --out "$TMP_OUT" \
  --raw "$TMP_RAW" \
  --err "$ERR_FILE" \
  --model "$MODEL" || trace_status=$?

cp -f "$TMP_RAW" "$RAW_FILE" 2>/dev/null || true

if [ "$trace_status" -ne 0 ]; then
  printf 'gemini-trace exited with status %s for run %s\n' "$trace_status" "$RUN_ID" >> "$ERR_FILE"
fi

if [ "$trace_status" -eq 0 ] && [ -s "$TMP_OUT" ]; then
  if [ "${EXPLORE_WIRE_FORMAT:-0}" = "1" ]; then
    cp -f "$TMP_OUT" "$RAW_FILE" 2>/dev/null || true
  fi
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
  failure_code="$trace_status"
  [ "$failure_code" -eq 0 ] && failure_code=1
  if [ ! -s "$ERR_FILE" ]; then
    printf 'gemini-trace (model %s) exited with no output and no stderr; raw stdout (if any) at %s\n' \
      "$MODEL" "$RAW_FILE" > "$ERR_FILE"
  fi
  rm -f "$RUNNING_FILE"
  write_status failed "$failure_code" "trace failed"
  publish_compat_failure
  printf 'explore: no trace output captured for run %s.\n' "$RUN_ID" >&2
  printf '%s\n' "--- gemini-trace stderr ($ERR_FILE) ---" >&2
  cat "$ERR_FILE" >&2 2>/dev/null || true
  printf '%s\n' "--- raw gemini-trace stdout at $RAW_FILE ---" >&2
  exit 1
fi
