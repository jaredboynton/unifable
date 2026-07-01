#!/usr/bin/env bash
# unifusion.sh — run a frontier-research architecture panel on OpenCode.
#
# Usage:
#   unifusion.sh <question_file> [run_dir]
#
# Flow:
#   1. Build a factual shared context brief when available.
#   2. Assemble one canonical panel prompt with that brief plus the verbatim task.
#   3. Start ONE warm `opencode serve` daemon (skill-local config, merged over the
#      user's global providers/auth/MCP).
#   4. Fan out the architect agents as parallel `opencode run --attach` threads, one
#      session each; capture each thread's final message as its report.
#   5. Run one synthesis thread on the same daemon that reads the reports and returns
#      [FINAL]/[ANALYSIS].
#   6. Kill the daemon, persist analysis/final artifacts, and write a provenance record.
#
# Why OpenCode serve+attach (not `droid exec`, not 4 independent `opencode run`):
#   - Fan-out is deterministic at the shell level; no root orchestrator spends
#     reasoning tokens deciding to parallelize.
#   - One warm daemon means one MCP/provider init and no sqlite lock contention
#     between panelists.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/_unifusion_lib.sh"

OPENCODE_BIN="${UNIFUSION_OPENCODE_BIN:-opencode}"
OPENCODE_CFG="${UNIFUSION_OPENCODE_CONFIG:-$SCRIPT_DIR/../opencode/opencode.json}"
PARSE_EVENTS="$SCRIPT_DIR/../opencode/parse_events.py"

question_file="${1:?usage: unifusion.sh <question_file> [run_dir]}"
case "$question_file" in
  /*) ;;
  *) question_file="$(pwd -P)/$question_file" ;;
esac
if [ ! -s "$question_file" ]; then
  echo "[unifusion] question file is missing or empty: $question_file" >&2
  exit 2
fi
if ! have "$OPENCODE_BIN"; then
  echo "[unifusion] opencode CLI not installed — cannot run OpenCode-native Unifusion." >&2
  exit 127
fi
if ! have python3; then
  echo "[unifusion] python3 not installed — cannot parse opencode JSON output." >&2
  exit 127
fi
if ! have curl; then
  echo "[unifusion] curl not installed — cannot talk to the opencode server." >&2
  exit 127
fi
if [ ! -s "$OPENCODE_CFG" ]; then
  echo "[unifusion] missing opencode config: $OPENCODE_CFG" >&2
  exit 2
fi
case "$OPENCODE_CFG" in
  /*) ;;
  *) OPENCODE_CFG="$(cd "$(dirname "$OPENCODE_CFG")" && pwd -P)/$(basename "$OPENCODE_CFG")" ;;
esac

run_dir="${2:-}"
if [ -z "$run_dir" ]; then
  run_dir="$(mktemp -d "${TMPDIR:-/tmp}/unifusion-panel.XXXXXX")"
else
  mkdir -p "$run_dir"
fi
case "$run_dir" in
  /*) ;;
  *) run_dir="$(cd "$run_dir" && pwd -P)" ;;
esac

cwd="$(pwd -P)"
review_root="$run_dir/reports"
mkdir -p "$review_root"

arch_timeout="${UNIFUSION_ARCH_TIMEOUT:-900}"
synth_timeout="${UNIFUSION_SYNTH_TIMEOUT:-600}"
server_wait="${UNIFUSION_SERVER_WAIT:-30}"

# ---- panel definition ----------------------------------------------------------------------------
# Each entry: "<agent-name>:<label>:<variant>". An empty variant means "use the provider default"
# (only the openai-ws GPT-5.5 agents take an explicit reasoning variant).
default_panel=(
  "architect:gpt5.5:medium"
  "architect-opus:opus4.8:"
  "architect-glm:glm5.2:"
  "architect-kimi:kimi2.7:"
)
panel_specs=()
if [ -n "${UNIFUSION_AGENTS:-}" ]; then
  IFS=',' read -r -a panel_specs <<<"${UNIFUSION_AGENTS}"
else
  panel_specs=("${default_panel[@]}")
fi

synth_agent="${UNIFUSION_SYNTH_AGENT:-unifusion-synth}"
synth_variant="${UNIFUSION_SYNTH_VARIANT:-medium}"

agent_of()   { printf '%s' "${1%%:*}"; }
label_of()   { local r="${1#*:}"; printf '%s' "${r%%:*}"; }
variant_of() { printf '%s' "${1##*:}"; }
report_path_for() { printf '%s/%s.md\n' "$review_root" "$1"; }

# ---- best-effort shared session-context brief ----------------------------------------------------
context_file="$run_dir/context.md"
context_state="none"
if bash "$SCRIPT_DIR/summarize_session.sh" "$context_file" >"$run_dir/context.log" 2>&1 && [ -s "$context_file" ]; then
  context_state="$context_file"
else
  rm -f "$context_file"
fi

# ---- canonical panel prompt ----------------------------------------------------------------------
panel_prompt="$run_dir/panel_prompt.md"
{
  if [ "$context_state" != "none" ]; then
    echo "[SESSION CONTEXT — shared factual background, same for every architect; not a proposed approach]"
    cat "$context_file"
    echo
  fi
  cat <<'EOF'
[TASK]
Find the strongest current technical approach for the user's request below. Optimize for literal best-known
practice backed by current evidence, not habit or average convention. Use local repo evidence when relevant,
and external primary sources such as official docs, flagship GitHub repositories, papers, benchmarks, release
notes, and maintainer guidance.

[USER REQUEST — verbatim]
EOF
  cat "$question_file"
} >"$panel_prompt"

analysis_path="$run_dir/analysis.md"
final_path="$run_dir/final.md"
serve_log="$run_dir/serve.log"

# ---- start the warm daemon -----------------------------------------------------------------------
# opencode serve spawns `opencode acp` worker children; on normal client exit those get orphaned and
# survive killing just the serve PID. Snapshot the pre-existing opencode processes so cleanup can reap
# exactly the ones this run created without touching any ambient opencode daemon the user is running.
opencode_pids() { pgrep -f "$OPENCODE_BIN" 2>/dev/null | sort -u; }
baseline_pids=" $(opencode_pids | tr '\n' ' ') "

server_pid=""
cleanup() {
  if [ -n "$server_pid" ] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null
  fi
  local p
  for p in $(opencode_pids); do
    case "$baseline_pids" in *" $p "*) continue ;; esac
    kill "$p" 2>/dev/null || true
  done
  for _ in 1 2 3 4 5; do
    local remaining=""
    for p in $(opencode_pids); do
      case "$baseline_pids" in *" $p "*) continue ;; esac
      remaining="yes"
    done
    [ -z "$remaining" ] && break
    sleep 0.2
  done
  for p in $(opencode_pids); do
    case "$baseline_pids" in *" $p "*) continue ;; esac
    kill -9 "$p" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

OPENCODE_CONFIG="$OPENCODE_CFG" "$OPENCODE_BIN" serve --port 0 </dev/null >"$serve_log" 2>&1 &
server_pid=$!

server_url=""
deadline=$(( $(date +%s) + server_wait ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "[unifusion] opencode serve exited during startup; tail of log:" >&2
    tail -20 "$serve_log" >&2
    exit 1
  fi
  server_url="$(grep -oE 'http://127\.0\.0\.1:[0-9]+' "$serve_log" | head -1)"
  [ -n "$server_url" ] && break
  sleep 0.25
done
if [ -z "$server_url" ]; then
  echo "[unifusion] opencode serve did not report a listen URL within ${server_wait}s; tail of log:" >&2
  tail -20 "$serve_log" >&2
  exit 1
fi

new_session() {
  curl -s -X POST "${server_url}/session" -H 'content-type: application/json' -d '{}' \
    | python3 -c 'import sys,json;print((json.load(sys.stdin) or {}).get("id",""))' 2>/dev/null
}

# run_thread <agent> <variant> <session> <events_out> <log_out>
# Reads the panel prompt on stdin; writes the raw NDJSON event stream to <events_out>.
run_thread() {
  local agent="$1" variant="$2" session="$3" events="$4" log="$5"
  local args=(run --attach "$server_url" --session "$session" --agent "$agent" --format json)
  [ -n "$variant" ] && args+=(--variant "$variant")
  OPENCODE_CONFIG="$OPENCODE_CFG" _run_with_timeout "$arch_timeout" \
    "$OPENCODE_BIN" "${args[@]}" <"$panel_prompt" >"$events" 2>"$log"
}

# ---- fan out the architects in parallel ----------------------------------------------------------
pids=(); statuses=()
for spec in "${panel_specs[@]}"; do
  agent="$(agent_of "$spec")"
  variant="$(variant_of "$spec")"
  sid="$(new_session)"
  events="$run_dir/${agent}.events.json"
  log="$run_dir/${agent}.log"
  if [ -z "$sid" ]; then
    echo "[unifusion] could not create a session for $agent; it will be dropped." >"$log"
    ( exit 70 ) & pids+=("$!")
    continue
  fi
  printf '%s\n' "$sid" >"$run_dir/${agent}.session"
  run_thread "$agent" "$variant" "$sid" "$events" "$log" &
  pids+=("$!")
done

for i in "${!pids[@]}"; do
  wait "${pids[$i]}"
  statuses+=("$?")
done

# ---- collect reports -----------------------------------------------------------------------------
for i in "${!panel_specs[@]}"; do
  spec="${panel_specs[$i]}"
  agent="$(agent_of "$spec")"
  events="$run_dir/${agent}.events.json"
  report="$(report_path_for "$agent")"
  if [ "${statuses[$i]}" -eq 0 ] && [ -s "$events" ]; then
    python3 "$PARSE_EVENTS" "$events" >"$report" 2>/dev/null || true
  fi
done

# ---- synthesis thread ----------------------------------------------------------------------------
ok_labels=(); missing_labels=(); label_specs=(); ok_pairs=()
for spec in "${panel_specs[@]}"; do
  agent="$(agent_of "$spec")"
  label="$(label_of "$spec")"
  report="$(report_path_for "$agent")"
  if _has_content "$report"; then
    ok_labels+=("$label")
    label_specs+=("${label}=${report}")
    ok_pairs+=("${label}=${report}")
  else
    missing_labels+=("$label")
  fi
done

if [ "${#ok_pairs[@]}" -eq 0 ]; then
  echo "[unifusion] no architect produced a usable report; aborting." >&2
  exit 1
fi

# Inline the report bodies into the synthesis prompt. The synth agent cannot read files
# outside the repo cwd (opencode auto-rejects external_directory access in headless mode),
# and it does not need to: the shell already holds every report.
synth_prompt="$run_dir/synth_prompt.md"
{
  cat "$panel_prompt"
  echo
  echo "[ARCHITECT REPORTS — full text, inlined; synthesize these]"
  for pair in "${ok_pairs[@]}"; do
    label="${pair%%=*}"
    report="${pair#*=}"
    echo
    printf -- "===== PANELIST %s =====\n" "$label"
    cat "$report"
    echo
  done
} >"$synth_prompt"

synth_session="$(new_session)"
synth_events="$run_dir/synth.events.json"
synth_log="$run_dir/synth.log"
if [ -z "$synth_session" ]; then
  echo "[unifusion] could not create a synthesis session." >&2
  exit 1
fi
printf '%s\n' "$synth_session" >"$run_dir/synth.session"

synth_args=(run --attach "$server_url" --session "$synth_session" --agent "$synth_agent" --format json)
[ -n "$synth_variant" ] && synth_args+=(--variant "$synth_variant")
OPENCODE_CONFIG="$OPENCODE_CFG" _run_with_timeout "$synth_timeout" \
  "$OPENCODE_BIN" "${synth_args[@]}" <"$synth_prompt" >"$synth_events" 2>"$synth_log"
synth_status=$?

if [ "$synth_status" -ne 0 ] || [ ! -s "$synth_events" ]; then
  echo "[unifusion] synthesis thread failed (status $synth_status); tail of log:" >&2
  tail -20 "$synth_log" >&2
  exit 1
fi

python3 - "$synth_events" "$final_path" "$analysis_path" "$PARSE_EVENTS" <<'PY'
import importlib.util
import pathlib
import re
import sys

events_path, final_path, analysis_path, parser_path = sys.argv[1:5]

spec = importlib.util.spec_from_file_location("parse_events", parser_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
result = mod.extract_final_text(events_path)


def extract(name: str) -> str:
    m = re.search(rf"\[{name}\]\s*(.*?)\s*\[/{name}\]", result, re.S)
    return (m.group(1).strip() + "\n") if m else ""


final = extract("FINAL")
analysis = extract("ANALYSIS")
if not final:
    final = result.rstrip() + ("\n" if result else "")
if not analysis:
    analysis = result.rstrip() + ("\n" if result else "")

pathlib.Path(final_path).write_text(final)
pathlib.Path(analysis_path).write_text(analysis)
PY

if ! _has_content "$final_path"; then
  echo "[unifusion] empty final answer from synthesis thread." >&2
  exit 1
fi
if ! _has_content "$analysis_path"; then
  echo "[unifusion] empty analysis from synthesis thread." >&2
  exit 1
fi

# ---- provenance + manifest -----------------------------------------------------------------------
cleanup
server_pid=""
trap - EXIT INT TERM

slug="opencode"
if [ "${#ok_labels[@]}" -gt 0 ]; then
  slug="opencode-$(IFS=-; echo "${ok_labels[*]}")"
fi

panel_note=""
if [ "${#missing_labels[@]}" -gt 0 ]; then
  panel_note="dropped: $(IFS=', '; echo "${missing_labels[*]}")"
fi

words="$(wc -w <"$question_file" | tr -d ' ')"
in_tokens=$((words * 4 / 3))
estimate="~${words} words (~${in_tokens} input tokens) sent to ${#panel_specs[@]} architect threads running in parallel on one warm opencode daemon, then synthesized in one thread; per-architect timeout ${arch_timeout}s, synthesis timeout ${synth_timeout}s."

provenance_path=""
if [ "${UNIFUSION_SAVE_RUN:-1}" = "1" ]; then
  save_env=()
  [ -n "$panel_note" ] && save_env+=(UNIFUSION_PANEL_NOTE="$panel_note")
  [ "$context_state" != "none" ] && save_env+=(UNIFUSION_CONTEXT_FILE="$context_file")
  save_env+=(UNIFUSION_ESTIMATE="$estimate")
  provenance_path="$(env "${save_env[@]}" bash "$SCRIPT_DIR/save_run.sh" "$slug" "$question_file" "$analysis_path" "$final_path" "${label_specs[@]}")"
fi

echo "RUN_DIR=$run_dir"
echo "PANEL_PROMPT=$panel_prompt"
echo "SYNTH_PROMPT=$synth_prompt"
echo "CONTEXT=$context_state"
echo "OPENCODE_CONFIG=$OPENCODE_CFG"
echo "SERVER_URL=$server_url"
echo "SLUG=$slug"
echo "ANALYSIS=$analysis_path"
echo "FINAL=$final_path"
[ -n "$provenance_path" ] && echo "PROVENANCE=$provenance_path"
echo "ESTIMATE=$estimate"
echo "panel (${#ok_labels[@]}/${#panel_specs[@]} returned):"
for spec in "${panel_specs[@]}"; do
  agent="$(agent_of "$spec")"
  label="$(label_of "$spec")"
  report="$(report_path_for "$agent")"
  if _has_content "$report"; then
    printf 'PANELIST %s ok %s\n' "$label" "$report"
  else
    printf 'PANELIST %s dropped:missing %s\n' "$label" "$report"
  fi
done

exit 0
