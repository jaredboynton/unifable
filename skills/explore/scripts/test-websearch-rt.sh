#!/usr/bin/env bash
# Offline smoke test for websearch-rt three-round + pointer submit replay.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPLAY="$SCRIPT_DIR/fixtures/realtime-websearch-replay.ndjson"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/explore-websearch-rt-test.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT

POINTER='{"executive_summary":"MCP bridges LLM apps to external tools.","in_scope_findings":"The official spec defines MCP as an open protocol for context and tools.","adjacent_out_of_scope":"Patch sandboxes are adjacent.","prior_art":"Reference servers on GitHub.","gaps_risks":"Docs may lag registry.","recommended_next_steps":"Read the spec and reference servers.","citation_refs":[{"url_index":0,"excerpt_index":0,"rationale":"spec definition"},{"url_index":1,"excerpt_index":0,"rationale":"reference impl"}]}'

cat > "$REPLAY" <<EOF
{"dir":"recv","type":"response.function_call_arguments.done","event":{"type":"response.function_call_arguments.done","name":"submit_websearch_pointer","arguments":$(printf '%s' "$POINTER" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}}
{"dir":"recv","type":"response.done","event":{"type":"response.done","response":{"status":"completed","output":[{"type":"function_call","name":"submit_websearch_pointer","arguments":$(printf '%s' "$POINTER" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}]}}}
EOF

PROMPT="$WORK_DIR/prompt.txt"
SUBMIT="$WORK_DIR/submit.txt"
printf 'GOAL: What is MCP?\n' > "$PROMPT"
printf 'submit instructions\n' > "$SUBMIT"

node "$SCRIPT_DIR/realtime-websearch.mjs" \
  --prompt-file "$PROMPT" \
  --submit-prompt-file "$SUBMIT" \
  --goal "What is MCP?" \
  --workspace "$WORK_DIR" \
  --out "$WORK_DIR/out" \
  --raw "$WORK_DIR/raw" \
  --err "$WORK_DIR/err" \
  --replay "$REPLAY" \
  --hydrate 1

test -s "$WORK_DIR/out"
grep -q "Executive Summary" "$WORK_DIR/out"
grep -q "modelcontextprotocol.io" "$WORK_DIR/out"

node --test "$SCRIPT_DIR/test/rt-web-run-tools.test.mjs"
node --test "$SCRIPT_DIR/test/rt-rehydrate-websearch.test.mjs"
node --test "$SCRIPT_DIR/test/websearch-schema.test.mjs"

echo "test-websearch-rt.sh: ok"
