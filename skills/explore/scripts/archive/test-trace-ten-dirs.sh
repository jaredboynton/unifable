#!/usr/bin/env bash
set -euo pipefail

TRACE_SH="${TRACE_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/trace-cursor.sh}"
TRACE_CMD=(env -u CURSOR_CONVERSATION_ID -u CURSOR_AGENT)
RUN_ROOT="${TMPDIR:-/tmp}/explore-trace-ten-dirs.$$"
FAKE_BIN="$RUN_ROOT/bin"
COUNT=10

cleanup() {
  if [ -S "$RUN_ROOT/acp.sock" ]; then
    PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" \
      EXPLORE_ACP_SOCKET="$RUN_ROOT/acp.sock" EXPLORE_ACP_PID="$RUN_ROOT/acp.pid" \
      EXPLORE_ACP_LOG="$RUN_ROOT/acp.log" EXPLORE_ACP_META="$RUN_ROOT/acp.meta.json" \
      node "$(cd "$(dirname "$TRACE_SH")" && pwd)/cursor-acp-trace.mjs" --stop-daemon >/dev/null 2>&1 || true
  fi
  rm -rf "$RUN_ROOT"
}
trap cleanup EXIT

mkdir -p "$FAKE_BIN" "$RUN_ROOT/home" "$RUN_ROOT/home/.cursor"
printf '{"token":"fake-test-auth"}\n' > "$RUN_ROOT/home/.cursor/auth.json"

cat > "$RUN_ROOT/fake-acp.mjs" <<'NODE'
import readline from "node:readline";
import path from "node:path";

let nextSession = 1;
let activePrompts = 0;
let maxActivePrompts = 0;
const sessions = new Map();
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
    const sessionId = `sess_${nextSession++}`;
    sessions.set(sessionId, { cwd: params.cwd });
    send({ jsonrpc: "2.0", id, result: { sessionId, configOptions: [] } });
    continue;
  }
  if (method === "session/prompt") {
    const text = promptText(params);
    const session = sessions.get(params.sessionId) || {};
    const workspace = path.basename(session.cwd || "missing-workspace");
    const match = text.match(/TEN_DIR_PROBE_(\d+)/);
    const label = match ? match[1] : "missing-label";
    activePrompts += 1;
    maxActivePrompts = Math.max(maxActivePrompts, activePrompts);
    setTimeout(() => {
      const chunks = [
        `## Flow\nBEGIN_FULL_RESULT_${label}\nworkspace=${workspace}\n`,
        `body-line-1=${label}:${workspace}\nbody-line-2=${label}:${workspace}\n`,
        `body-line-3=${label}:${workspace}\nEND_FULL_RESULT_${label}\n\n`,
        `## Code references\n\n## Key files\n- ${workspace}\nMAX_ACTIVE_PROMPTS=${maxActivePrompts}\n`,
      ];
      for (const chunk of chunks) {
        send({
          jsonrpc: "2.0",
          method: "session/update",
          params: {
            sessionId: params.sessionId,
            update: {
              sessionUpdate: "agent_message_chunk",
              content: { type: "text", text: chunk },
            },
          },
        });
      }
      send({ jsonrpc: "2.0", id, result: { stopReason: "end_turn" } });
      activePrompts -= 1;
    }, 300);
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
    echo "start:${PWD}" >> "$FAKE_ACP_START_LOG"
    exec node "$FAKE_ACP_SCRIPT"
    ;;
esac
echo "fake cursor-agent only supports acp" >&2
exit 70
SH
chmod +x "$FAKE_BIN/cursor-agent"

pids=()
for index in $(seq 1 "$COUNT"); do
  label="$(printf '%02d' "$index")"
  work_dir="$RUN_ROOT/work-$label"
  mkdir -p "$work_dir"
  (
    cd "$work_dir"
    PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" \
      FAKE_ACP_SCRIPT="$RUN_ROOT/fake-acp.mjs" FAKE_ACP_START_LOG="$RUN_ROOT/acp-starts.log" \
      EXPLORE_TRANSPORT=acp EXPLORE_ACP_STREAM=1 \
      EXPLORE_ACP_SOCKET="$RUN_ROOT/acp.sock" EXPLORE_ACP_PID="$RUN_ROOT/acp.pid" \
      EXPLORE_ACP_LOG="$RUN_ROOT/acp.log" EXPLORE_ACP_META="$RUN_ROOT/acp.meta.json" \
      "${TRACE_CMD[@]}" "$TRACE_SH" "TEN_DIR_PROBE_$label" \
      >"$RUN_ROOT/out-$label.stdout" 2>"$RUN_ROOT/out-$label.stderr"
  ) &
  pids+=("$!")
done

failed=0
for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    failed=1
  fi
done
if [ "$failed" -ne 0 ]; then
  echo "one or more ten-dir trace calls failed" >&2
  for label in $(seq -f '%02g' 1 "$COUNT"); do
    echo "--- $label stderr ---" >&2
    sed -n '1,160p' "$RUN_ROOT/out-$label.stderr" >&2 2>/dev/null || true
  done
  exit 1
fi

if [ "$(wc -l < "$RUN_ROOT/acp-starts.log" 2>/dev/null | tr -d ' ')" != "1" ]; then
  echo "expected 10 callers to share exactly one ACP daemon start" >&2
  cat "$RUN_ROOT/acp-starts.log" >&2 || true
  exit 1
fi

for index in $(seq 1 "$COUNT"); do
  label="$(printf '%02d' "$index")"
  stdout="$RUN_ROOT/out-$label.stdout"
  stderr="$RUN_ROOT/out-$label.stderr"
  grep -q "^## Flow" "$stdout"
  grep -q "BEGIN_FULL_RESULT_$label" "$stdout"
  grep -q "workspace=work-$label" "$stdout"
  grep -q "body-line-1=$label:work-$label" "$stdout"
  grep -q "body-line-2=$label:work-$label" "$stdout"
  grep -q "body-line-3=$label:work-$label" "$stdout"
  grep -q "END_FULL_RESULT_$label" "$stdout"
  grep -q "\[explore: full trace saved to $RUN_ROOT/home/.cache/explore/runs/" "$stdout"
  grep -q "^EXPLORE_RUN_ID=" "$stdout"
  grep -q "BEGIN_FULL_RESULT_$label" "$stderr"
  grep -q "END_FULL_RESULT_$label" "$stderr"
  for other in $(seq 1 "$COUNT"); do
    other_label="$(printf '%02d' "$other")"
    if [ "$other_label" != "$label" ] && grep -q "BEGIN_FULL_RESULT_$other_label\|workspace=work-$other_label\|END_FULL_RESULT_$other_label" "$stdout"; then
      echo "trace output for $label contained result for $other_label" >&2
      exit 1
    fi
  done
done

if ! grep -R "MAX_ACTIVE_PROMPTS=10" "$RUN_ROOT"/out-*.stdout >/dev/null 2>&1; then
  echo "expected proof that all 10 prompts were active concurrently" >&2
  grep -R "MAX_ACTIVE_PROMPTS=" "$RUN_ROOT"/out-*.stdout >&2 || true
  exit 1
fi

if find "$RUN_ROOT" -name .latest-run -print -quit | grep -q .; then
  echo "trace created a shared latest-run pointer" >&2
  find "$RUN_ROOT" -name .latest-run >&2
  exit 1
fi

echo "trace ten-dir shared-daemon proof passed"
