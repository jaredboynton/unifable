#!/usr/bin/env bash
# Offline smoke test for trace-rt.sh wrapper + realtime-trace.mjs replay paths.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURE="$SCRIPT_DIR/fixtures/search-mini-repo"
STRUCTURED_REPLAY="$SCRIPT_DIR/fixtures/realtime-trace-structured-replay.ndjson"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/explore-trace-rt-test.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

mkdir -p "$SCRIPT_DIR/fixtures"

SUBMIT_ARGS='{"opening_summary":"Stop gate in gate_stop.py.","flow_steps":["hooks/gate_stop.py defines adjudicate_dispute"],"comparison_tables":[],"sections":[{"heading":"gate_stop","body":"Stop hook with adjudicate_dispute."}],"key_files":[{"path":"hooks/gate_stop.py","role":"stop gate"}],"code_passages":[{"file_path":"hooks/gate_stop.py","start_line":1,"end_line":4,"rationale":"Header and entry"}],"grounding_manifest":{"files_read":["hooks/gate_stop.py"],"tool_turns":1}}'

cat > "$STRUCTURED_REPLAY" <<EOF
{"dir":"recv","type":"response.function_call_arguments.done","event":{"type":"response.function_call_arguments.done","name":"submit_trace","arguments":$(printf '%s' "$SUBMIT_ARGS" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}}
{"dir":"recv","type":"response.done","event":{"type":"response.done","response":{"status":"completed","output":[{"type":"function_call","name":"submit_trace","arguments":$(printf '%s' "$SUBMIT_ARGS" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}]}}}
EOF

PROMPT="$WORK_DIR/prompt.txt"
SUBMIT="$WORK_DIR/submit.txt"
STRUCTURED="$WORK_DIR/structured.json"
printf 'QUESTION: where is stop handled?\n' > "$PROMPT"
printf 'submit instructions\n' > "$SUBMIT"

node "$SCRIPT_DIR/realtime-trace.mjs" \
  --prompt-file "$PROMPT" \
  --submit-prompt-file "$SUBMIT" \
  --workspace "$FIXTURE" \
  --out "$WORK_DIR/out-structured" \
  --raw "$WORK_DIR/raw-structured" \
  --err "$WORK_DIR/err-structured" \
  --structured-out "$STRUCTURED" \
  --replay "$STRUCTURED_REPLAY"

test -s "$WORK_DIR/out-structured"
grep -q "Stop gate" "$WORK_DIR/out-structured"
grep -q "## Flow" "$WORK_DIR/out-structured"
grep -q "gate_stop.py" "$WORK_DIR/out-structured"
test -s "$STRUCTURED"

node --test "$SCRIPT_DIR/test/test-realtime-trace.mjs"
node --test "$SCRIPT_DIR/test/rt-trace-utils.test.mjs"
node --test "$SCRIPT_DIR/test/rt-pick-passages.test.mjs"
node --test "$SCRIPT_DIR/test/rt-rehydrate-submit.test.mjs"
node --test "$SCRIPT_DIR/test/rt-explore-runtime.test.mjs"
node --test "$SCRIPT_DIR/test/rt-map-seed.test.mjs"
node --test "$SCRIPT_DIR/test/rt-explore-nav.test.mjs"
node --test "$SCRIPT_DIR/test/test-trace-schema.mjs"

(
  cd "$SCRIPT_DIR"
  node --input-type=module -e "
import fs from 'node:fs';
import { extractFunctionCalls } from './lib/rt-tools.mjs';
const lines = fs.readFileSync('./fixtures/realtime-trace-explore-replay.ndjson','utf8').trim().split('\n');
const turn2 = JSON.parse(lines[1]).event;
const calls = extractFunctionCalls(turn2.response);
if (calls.length !== 3) throw new Error('expected 3 parallel explore_exec calls, got ' + calls.length);
if (calls.some((c) => c.name !== 'explore_exec')) throw new Error('expected explore_exec only');
"
)

echo "test-trace-rt.sh: ok"
