#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACP_SCRIPT="$SCRIPT_DIR/cursor-acp-trace.mjs"
RUN_ROOT="${TMPDIR:-/tmp}/explore-acp-concurrency.$$"
FAKE_BIN="$RUN_ROOT/bin"

cleanup() {
  if [ -n "${FAKE_DAEMON_PID:-}" ]; then
    kill "$FAKE_DAEMON_PID" 2>/dev/null || true
    wait "$FAKE_DAEMON_PID" 2>/dev/null || true
  fi
  if [ -S "$RUN_ROOT/acp.sock" ]; then
    PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" \
      UNITRACE_ACP_SOCKET="$RUN_ROOT/acp.sock" UNITRACE_ACP_PID="$RUN_ROOT/acp.pid" \
      UNITRACE_ACP_LOG="$RUN_ROOT/acp.log" UNITRACE_ACP_META="$RUN_ROOT/acp.meta.json" \
      node "$ACP_SCRIPT" --stop-daemon >/dev/null 2>&1 || true
  fi
  rm -rf "$RUN_ROOT"
}
trap cleanup EXIT

mkdir -p "$FAKE_BIN" "$RUN_ROOT/home" "$RUN_ROOT/work-a" "$RUN_ROOT/work-b"

cat > "$RUN_ROOT/fake-acp.mjs" <<'NODE'
import readline from "node:readline";

let nextSession = 1;
let nextTerminalRequest = 1000;
let pendingRecursive = null;
const rl = readline.createInterface({ input: process.stdin });

function send(value) {
  process.stdout.write(JSON.stringify(value) + "\n");
}

function promptText(params) {
  return params?.prompt?.find((item) => item?.type === "text")?.text || "";
}

for await (const line of rl) {
  if (!line.trim()) continue;
  const message = JSON.parse(line);
if (pendingRecursive && message.id === pendingRecursive.terminalRequestId) {
    const blocked = /recursive explore invocation blocked|trace client does not expose terminal commands|recursive trace(?:-cursor)?\.sh invocation blocked/.test(message.error?.message || "");
    send({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: pendingRecursive.sessionId,
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: "## Flow\nrecursive-block:" + blocked + "\n\n## Code references\n\n## Key files\n- fake\n" },
        },
      },
    });
    send({ jsonrpc: "2.0", id: pendingRecursive.promptId, result: { stopReason: "end_turn" } });
    pendingRecursive = null;
    continue;
  }
  const { id, method, params = {} } = message;
  if (method === "initialize") {
    send({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: 1,
        agentCapabilities: {
          loadSession: true,
          mcpCapabilities: {},
          promptCapabilities: { audio: false, embeddedContext: false, image: false },
          sessionCapabilities: {},
        },
      },
    });
    continue;
  }
  if (method === "session/new") {
    send({ jsonrpc: "2.0", id, result: { sessionId: `sess_${nextSession++}`, configOptions: [] } });
    continue;
  }
  if (method === "session/prompt") {
    const text = promptText(params);
    if (text.includes("recursive-block")) {
      const terminalRequestId = nextTerminalRequest++;
      pendingRecursive = { promptId: id, sessionId: params.sessionId, terminalRequestId };
      send({
        jsonrpc: "2.0",
        id: terminalRequestId,
        method: "terminal/create",
        params: {
          sessionId: params.sessionId,
          command: "bash",
          args: ["/Users/jaredboynton/.agents/skills/explore/scripts/trace-cursor.sh", "nested"],
        },
      });
      continue;
    }
    if (text.includes("late-final")) {
      send({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId: params.sessionId,
          update: {
            sessionUpdate: "agent_message_chunk",
            content: { type: "text", text: "run_id:" + params.sessionId + "\n" },
          },
        },
      });
      send({ jsonrpc: "2.0", id, result: { stopReason: "end_turn" } });
      setImmediate(() => {
        send({
          jsonrpc: "2.0",
          method: "session/update",
          params: {
            sessionId: params.sessionId,
            update: {
              sessionUpdate: "agent_message_chunk",
              content: { type: "text", text: "## Flow\ntrace:" + text + "\n\n## Code references\n\n## Key files\n- fake\n" },
            },
          },
        });
      });
      continue;
    }
    setImmediate(() => {
      send({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId: params.sessionId,
          update: {
            sessionUpdate: "agent_message_chunk",
            content: { type: "text", text: `## Flow\ntrace:${text}\n\n## Code references\n\n## Key files\n- fake\n` },
          },
        },
      });
      send({ jsonrpc: "2.0", id, result: { stopReason: "end_turn" } });
    });
    continue;
  }
  send({ jsonrpc: "2.0", id, result: null });
}
NODE

cat > "$FAKE_BIN/cursor-agent" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *" acp "*)
    echo start >> "$FAKE_ACP_START_LOG"
    exec node "$FAKE_ACP_SCRIPT"
    ;;
esac
echo "fake cursor-agent only supports acp" >&2
exit 70
SH
chmod +x "$FAKE_BIN/cursor-agent"

run_client() {
  local label="$1"
  local _prompt="$2"
  local workspace="$3"
  PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" FAKE_ACP_SCRIPT="$RUN_ROOT/fake-acp.mjs" \
    FAKE_ACP_START_LOG="$RUN_ROOT/acp-starts.log" \
    UNITRACE_ACP_SOCKET="$RUN_ROOT/acp.sock" UNITRACE_ACP_PID="$RUN_ROOT/acp.pid" \
    UNITRACE_ACP_LOG="$RUN_ROOT/acp.log" UNITRACE_ACP_META="$RUN_ROOT/acp.meta.json" \
    node "$ACP_SCRIPT" \
      --prompt-file "$RUN_ROOT/$label.prompt" \
      --out "$RUN_ROOT/$label.out" \
      --raw "$RUN_ROOT/$label.raw" \
      --err "$RUN_ROOT/$label.err" \
      --workspace "$workspace" \
      --model fake
}

printf 'slow-one' > "$RUN_ROOT/one.prompt"
printf 'slow-two' > "$RUN_ROOT/two.prompt"
run_client one slow-one "$RUN_ROOT/work-a" &
pid_one=$!
run_client two slow-two "$RUN_ROOT/work-b" &
pid_two=$!

status_one=0
status_two=0
wait "$pid_one" || status_one=$?
wait "$pid_two" || status_two=$?
if [ "$status_one" -ne 0 ] || [ "$status_two" -ne 0 ]; then
  echo "expected concurrent ACP traces to succeed, got one=$status_one two=$status_two" >&2
  echo "--- one.err ---" >&2
  sed -n '1,120p' "$RUN_ROOT/one.err" >&2 || true
  echo "--- two.err ---" >&2
  sed -n '1,120p' "$RUN_ROOT/two.err" >&2 || true
  exit 1
fi

if ! grep -q 'trace:slow-one' "$RUN_ROOT/one.out"; then
  echo "missing slow-one trace output" >&2
  cat "$RUN_ROOT/one.out" >&2 || true
  cat "$RUN_ROOT/one.err" >&2 || true
  exit 1
fi
if ! grep -q 'trace:slow-two' "$RUN_ROOT/two.out"; then
  echo "missing slow-two trace output" >&2
  cat "$RUN_ROOT/two.out" >&2 || true
  cat "$RUN_ROOT/two.err" >&2 || true
  exit 1
fi
if [ "$(wc -l < "$RUN_ROOT/acp-starts.log" | tr -d ' ')" != "1" ]; then
  echo "expected cold concurrent clients to share one ACP daemon start" >&2
  cat "$RUN_ROOT/acp-starts.log" >&2 || true
  exit 1
fi
if grep -q 'trace:slow-two' "$RUN_ROOT/one.out" || grep -q 'trace:slow-one' "$RUN_ROOT/two.out"; then
  echo "ACP trace outputs crossed session streams" >&2
  exit 1
fi

printf 'stream-probe' > "$RUN_ROOT/stream.prompt"
PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" FAKE_ACP_SCRIPT="$RUN_ROOT/fake-acp.mjs" \
  FAKE_ACP_START_LOG="$RUN_ROOT/acp-starts.log" \
  UNITRACE_ACP_SOCKET="$RUN_ROOT/acp.sock" UNITRACE_ACP_PID="$RUN_ROOT/acp.pid" \
  UNITRACE_ACP_LOG="$RUN_ROOT/acp.log" UNITRACE_ACP_META="$RUN_ROOT/acp.meta.json" \
  node "$ACP_SCRIPT" --stream \
    --prompt-file "$RUN_ROOT/stream.prompt" \
    --out "$RUN_ROOT/stream.out" \
    --raw "$RUN_ROOT/stream.raw" \
    --err "$RUN_ROOT/stream.err" \
    --workspace "$RUN_ROOT/work-a" \
    --model fake \
    >"$RUN_ROOT/stream.stdout" 2>"$RUN_ROOT/stream.stderr"
if ! grep -q 'trace:stream-probe' "$RUN_ROOT/stream.out"; then
  echo "missing stream trace output file" >&2
  cat "$RUN_ROOT/stream.out" >&2 || true
  cat "$RUN_ROOT/stream.err" >&2 || true
  exit 1
fi
if ! grep -q 'trace:stream-probe' "$RUN_ROOT/stream.stderr"; then
  echo "missing stream trace stderr" >&2
  cat "$RUN_ROOT/stream.stderr" >&2 || true
  cat "$RUN_ROOT/stream.err" >&2 || true
  exit 1
fi
if [ -s "$RUN_ROOT/stream.stdout" ]; then
  echo "expected ACP helper stream mode to keep stdout reserved for the wrapper's final trace" >&2
  cat "$RUN_ROOT/stream.stdout" >&2
  exit 1
fi

printf 'late-final' > "$RUN_ROOT/late-final.prompt"
PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" FAKE_ACP_SCRIPT="$RUN_ROOT/fake-acp.mjs" \
  FAKE_ACP_START_LOG="$RUN_ROOT/acp-starts.log" \
  UNITRACE_ACP_SOCKET="$RUN_ROOT/acp.sock" UNITRACE_ACP_PID="$RUN_ROOT/acp.pid" \
  UNITRACE_ACP_LOG="$RUN_ROOT/acp.log" UNITRACE_ACP_META="$RUN_ROOT/acp.meta.json" \
  node "$ACP_SCRIPT" \
    --prompt-file "$RUN_ROOT/late-final.prompt" \
    --out "$RUN_ROOT/late-final.out" \
    --raw "$RUN_ROOT/late-final.raw" \
    --err "$RUN_ROOT/late-final.err" \
    --workspace "$RUN_ROOT/work-a" \
    --model fake
if ! grep -q 'trace:late-final' "$RUN_ROOT/late-final.out"; then
  echo "missing late-final trace output" >&2
  cat "$RUN_ROOT/late-final.out" >&2 || true
  cat "$RUN_ROOT/late-final.err" >&2 || true
  exit 1
fi
if sed -n '1p' "$RUN_ROOT/late-final.out" | grep -q '^run_id:'; then
  echo "ACP helper returned a prompt/run id before the completed trace result" >&2
  cat "$RUN_ROOT/late-final.out" >&2
  exit 1
fi

printf 'recursive-block' > "$RUN_ROOT/recursive-block.prompt"
PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" FAKE_ACP_SCRIPT="$RUN_ROOT/fake-acp.mjs" \
  FAKE_ACP_START_LOG="$RUN_ROOT/acp-starts.log" \
  UNITRACE_ACP_SOCKET="$RUN_ROOT/acp.sock" UNITRACE_ACP_PID="$RUN_ROOT/acp.pid" \
  UNITRACE_ACP_LOG="$RUN_ROOT/acp.log" UNITRACE_ACP_META="$RUN_ROOT/acp.meta.json" \
  node "$ACP_SCRIPT" \
    --prompt-file "$RUN_ROOT/recursive-block.prompt" \
    --out "$RUN_ROOT/recursive-block.out" \
    --raw "$RUN_ROOT/recursive-block.raw" \
    --err "$RUN_ROOT/recursive-block.err" \
    --workspace "$RUN_ROOT/work-a" \
    --model fake
if ! grep -q 'recursive-block:true' "$RUN_ROOT/recursive-block.out"; then
  echo "recursive terminal block did not reach fake agent" >&2
  cat "$RUN_ROOT/recursive-block.out" >&2 || true
  cat "$RUN_ROOT/recursive-block.err" >&2 || true
  exit 1
fi

PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" FAKE_ACP_SCRIPT="$RUN_ROOT/fake-acp.mjs" \
  UNITRACE_ACP_SOCKET="$RUN_ROOT/acp.sock" UNITRACE_ACP_PID="$RUN_ROOT/acp.pid" \
  UNITRACE_ACP_LOG="$RUN_ROOT/acp.log" UNITRACE_ACP_META="$RUN_ROOT/acp.meta.json" \
  node "$ACP_SCRIPT" --stop-daemon

echo "ACP concurrency regression passed"
