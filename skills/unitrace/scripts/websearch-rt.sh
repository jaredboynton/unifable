#!/usr/bin/env bash
# explore/websearch-rt.sh: external research via gpt-realtime-2 (Codex OAuth + alpha/search web_run).
#
# Usage:
#   websearch-rt.sh "<research goal / task>"
#
# Each run gets an isolated directory under ${UNITRACE_RUNS_DIR} (default ~/.cache/explore/runs).
#
# Env overrides:
#   UNISEARCH_WS_BACKEND           alpha (only supported backend; exa is retired)
#   UNISEARCH_WS_MODEL           Realtime model slug (default: gpt-realtime-2)
#   UNITRACE_CODEX_AUTH_PATH    Codex OAuth file (default: ~/.codex/auth.json)
#   UNITRACE_SEARCH_MODEL       alpha/search model (default: gpt-5.4)
#   UNISEARCH_ALPHA_TRANSPORT    curl (default) or fetch
#   UNISEARCH_WS_TIMEOUT         total deadline seconds (default: 600)
#   UNISEARCH_WS_COALESCE_WEB_RUN     merge parallel web_run into one alpha call (default: 1)
#   UNISEARCH_WS_ALPHA_MAX_OUTPUT_TOKENS  alpha/search max_output_tokens (default: 128000, the model cap; small caps truncate multi-page fetches)
#   UNISEARCH_WS_SUBMIT_PACKET_MAX  max submit packet chars (default: 45000)
#   UNISEARCH_WS_SUBMIT_REASK       one reask on validation failure (default: 1)
#   UNISEARCH_WS_SWARM_OPEN_CAP    swarm: max URLs opened in the single open pass (default: 18)
#   UNISEARCH_WS_SUBMIT_FRESH_CONTEXT  between-round prune: delete (default), reconnect, or off
#   UNISEARCH_WS_SUBMIT_REASONING_EFFORT  submit round reasoning (default: low)
#   UNISEARCH_WS_REASONING_EFFORT  optional override for both phases
#   UNITRACE_WORKSPACE            caller repo for AGENTS.md/README context (default: cwd)
#   UNISEARCH_WEBSEARCH_SKILL_CONTEXT set to 1 to inject explore skill inventory
#   UNITRACE_OUT                  optional explicit compatibility output path
#   UNITRACE_RUNS_DIR             directory for per-run state
#   UNITRACE_RUN_ID               explicit run id
#   UNITRACE_RUN_TTL_SECONDS      completed-run cleanup threshold (default: 86400)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=explore-hydrate.sh
. "$SCRIPT_DIR/explore-hydrate.sh"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/env.sh"
explore_load_skill_env "$SKILL_DIR"

case "${1:-}" in
  --help|-h)
    awk 'NR > 1 && /^set -euo pipefail$/ { exit } NR > 1 { print }' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  --*)
    printf 'explore: unknown flag %s (expected a quoted research goal)\n' "$1" >&2
    exit 2
    ;;
esac

if [ "$#" -eq 0 ]; then
  echo "usage: websearch-rt.sh <research goal>" >&2
  exit 2
fi

for arg in "$@"; do
  case "$arg" in
    --*)
      printf 'explore: control flags are not accepted after the goal; pass one quoted research goal\n' >&2
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

websearch_state() {
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
  printf '\n---\n[explore: full websearch saved to %s]\n[explore: run id %s]\nUNITRACE_RUN_ID=%s\n' "$out_file" "$run_id" "$run_id"
}

print_failure() {
  local run_dir="$1"
  local run_id err_file raw_file status_file
  run_id="$(run_id_from_dir "$run_dir")"
  err_file="$run_dir/err.log"
  raw_file="$run_dir/raw"
  status_file="$run_dir/status.json"
  printf 'explore: no completed websearch available for run %s.\n' "$run_id" >&2
  if [ -s "$status_file" ]; then
    printf -- '--- websearch status (%s) ---\n' "$status_file" >&2
    cat "$status_file" >&2 2>/dev/null || true
  fi
  if [ -s "$err_file" ]; then
    printf -- '--- realtime-websearch stderr (%s) ---\n' "$err_file" >&2
    cat "$err_file" >&2 2>/dev/null || true
  fi
  if [ -e "$raw_file" ]; then
    printf 'raw realtime-websearch stdout (if any): %s\n' "$raw_file" >&2
  fi
}

command -v node >/dev/null 2>&1 || { echo "error: node not found on PATH" >&2; exit 127; }

CODEX_AUTH="${UNITRACE_CODEX_AUTH_PATH:-${HOME:-$(cd ~ && pwd)}/.codex/auth.json}"
if [ ! -f "$CODEX_AUTH" ]; then
  printf 'error: Codex auth not found at %s\n' "$CODEX_AUTH" >&2
  printf '  run: codex login\n' >&2
  exit 1
fi

WS_BACKEND="${UNISEARCH_WS_BACKEND:-alpha}"
case "$WS_BACKEND" in
  alpha)
    command -v curl >/dev/null 2>&1 || {
      printf 'error: curl not found on PATH (required for alpha/search transport)\n' >&2
      exit 127
    }
    ;;
  exa)
    # Retired: the exa RT backend is no longer supported (native alpha arms beat it;
    # see docs/benchmarks/websearch-frontier.md). websearch-gemini.sh still uses Exa MCP.
    printf 'error: UNISEARCH_WS_BACKEND=exa is retired; only alpha is supported\n' >&2
    exit 2
    ;;
  *)
    printf 'error: UNISEARCH_WS_BACKEND must be alpha (got: %s; exa is retired)\n' "$WS_BACKEND" >&2
    exit 2
    ;;
esac

GOAL="$*"
MODEL="${UNISEARCH_WS_MODEL:-gpt-realtime-2}"
WORKSPACE="${UNITRACE_WORKSPACE:-$PWD}"
WORKSPACE="$(abs_path "$WORKSPACE")"
export UNITRACE_WORKSPACE="$WORKSPACE"
export UNITRACE_WIRE_FORMAT="${UNITRACE_WIRE_FORMAT:-1}"
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
    printf 'websearch-rt exited before completion for run %s\n' "$RUN_ID" > "$ERR_FILE"
    write_status failed 1 "websearch-rt exited before completion"
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
    state="$(websearch_state "$dir")"
    [ "$state" = "running" ] && continue
    mtime="$(stat_mtime "$dir")"
    age=$((now - mtime))
    [ "$age" -gt "$ttl" ] && rm -rf "$dir"
  done
  return 0
}

mkdir -p "$RUN_DIR"
echo "$$" > "$RUNNING_FILE"
write_status running null "realtime-websearch running"
cleanup_old_runs
WORK_DIR="$(mktemp -d "$RUN_DIR/work.XXXXXX")"
TMP_OUT="$WORK_DIR/out"
TMP_RAW="$WORK_DIR/raw"
PROMPT_FILE="$WORK_DIR/prompt.txt"
SUBMIT_PROMPT_FILE="$WORK_DIR/submit-prompt.txt"

(
  cd "$SCRIPT_DIR"
  node --input-type=module -e "
import { writeFileSync } from 'node:fs';
import { buildWebsearchWebRunPrompt } from './websearch-lib.mjs';
const goal = process.argv[1];
const workspace = process.argv[2];
const skillContext = process.env.UNISEARCH_WEBSEARCH_SKILL_CONTEXT === '1';
const prompt = buildWebsearchWebRunPrompt(goal, { workspace, skillContext });
writeFileSync(process.argv[3], prompt);
" "$GOAL" "$WORKSPACE" "$PROMPT_FILE"
)

SUBMIT_PROMPT="$(node "$SCRIPT_DIR/lib/explore-output-prompt.mjs" --ws-pointer-submit)"
printf '%s' "$SUBMIT_PROMPT" > "$SUBMIT_PROMPT_FILE"

RT_ARGS=(
  --prompt-file "$PROMPT_FILE"
  --goal "$GOAL"
  --submit-prompt-file "$SUBMIT_PROMPT_FILE"
  --workspace "$WORKSPACE"
  --out "$TMP_OUT"
  --raw "$TMP_RAW"
  --err "$ERR_FILE"
  --model "$MODEL"
  --auth-path "$CODEX_AUTH"
  --frames "$RUN_DIR/frames.ndjson"
)

ws_status=0
node "$SCRIPT_DIR/realtime-websearch.mjs" "${RT_ARGS[@]}" || ws_status=$?

cp -f "$TMP_RAW" "$RAW_FILE" 2>/dev/null || true

if [ "$ws_status" -ne 0 ]; then
  printf 'realtime-websearch exited with status %s for run %s\n' "$ws_status" "$RUN_ID" >> "$ERR_FILE"
fi

if [ "$ws_status" -eq 0 ] && [ -s "$TMP_OUT" ]; then
  cp -f "$TMP_OUT" "$RAW_FILE" 2>/dev/null || true
  if explore_hydrate_websearch_output "$TMP_OUT" "$TMP_OUT.hydrated" "$SCRIPT_DIR" ""; then
    mv -f "$TMP_OUT.hydrated" "$TMP_OUT"
  else
    rm -f "$TMP_OUT.hydrated"
  fi
  mv -f "$TMP_OUT" "$OUT_FILE"
  : > "$DONE_FILE"
  rm -f "$RUNNING_FILE"
  write_status done 0 "websearch complete"
  publish_compat_success
  print_done "$RUN_DIR"
else
  failure_code="$ws_status"
  [ "$failure_code" -eq 0 ] && failure_code=1
  if [ ! -s "$ERR_FILE" ]; then
    printf 'realtime-websearch (model %s) exited with no output and no stderr; raw stdout (if any) at %s\n' \
      "$MODEL" "$RAW_FILE" > "$ERR_FILE"
  fi
  rm -f "$RUNNING_FILE"
  write_status failed "$failure_code" "websearch failed"
  publish_compat_failure
  printf 'explore: no websearch output captured for run %s.\n' "$RUN_ID" >&2
  printf '%s\n' "--- realtime-websearch stderr ($ERR_FILE) ---" >&2
  cat "$ERR_FILE" >&2 2>/dev/null || true
  printf '%s\n' "--- raw realtime-websearch stdout at $RAW_FILE ---" >&2
  exit 1
fi
