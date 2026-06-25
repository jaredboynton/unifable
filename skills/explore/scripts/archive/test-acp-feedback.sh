#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACP_SCRIPT="$SCRIPT_DIR/cursor-acp-trace.mjs"
RUN_ROOT="${TMPDIR:-/tmp}/explore-acp-feedback.$$"
FAKE_BIN="$RUN_ROOT/bin"

cleanup() {
  if [ -n "${SLEEP_PID:-}" ]; then
    kill "$SLEEP_PID" 2>/dev/null || true
    wait "$SLEEP_PID" 2>/dev/null || true
  fi
  if [ -n "${FAKE_DAEMON_PID:-}" ]; then
    kill "$FAKE_DAEMON_PID" 2>/dev/null || true
    wait "$FAKE_DAEMON_PID" 2>/dev/null || true
  fi
  rm -rf "$RUN_ROOT"
}
trap cleanup EXIT

mkdir -p "$FAKE_BIN" "$RUN_ROOT/home" "$RUN_ROOT/work"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'case " $* " in' \
  '  *" acp "*)' \
  '    echo "A keychain cannot be found to store \"cursor-user.\"" >&2' \
  '    exit 1' \
  '    ;;' \
  'esac' \
  'echo "fake cursor-agent only supports acp failure" >&2' \
  'exit 70' \
  > "$FAKE_BIN/cursor-agent"
chmod +x "$FAKE_BIN/cursor-agent"

printf 'probe' > "$RUN_ROOT/prompt.txt"
if PATH="$FAKE_BIN:$PATH" HOME="$RUN_ROOT/home" \
  EXPLORE_ACP_SOCKET="$RUN_ROOT/acp.sock" EXPLORE_ACP_PID="$RUN_ROOT/acp.pid" \
  EXPLORE_ACP_LOG="$RUN_ROOT/acp.log" EXPLORE_ACP_META="$RUN_ROOT/acp.meta.json" \
  node "$ACP_SCRIPT" --prompt-file "$RUN_ROOT/prompt.txt" --out "$RUN_ROOT/out" --raw "$RUN_ROOT/raw" --err "$RUN_ROOT/err" --workspace "$RUN_ROOT/work" --model fake \
  >"$RUN_ROOT/stdout" 2>"$RUN_ROOT/stderr"; then
  echo "expected fake ACP auth failure" >&2
  exit 1
fi
grep -q 'keychain cannot be found' "$RUN_ROOT/err"
grep -q 'explore: cursor-agent reported an authentication failure' "$RUN_ROOT/err"
grep -q 'EXPLORE_ACP_SOCKET=' "$RUN_ROOT/err"

sleep 60 &
SLEEP_PID=$!
printf '%s\n' "$SLEEP_PID" > "$RUN_ROOT/stale.pid"
if HOME="$RUN_ROOT/home" EXPLORE_ACP_SOCKET="$RUN_ROOT/missing.sock" EXPLORE_ACP_PID="$RUN_ROOT/stale.pid" EXPLORE_ACP_LOG="$RUN_ROOT/stale.log" EXPLORE_ACP_META="$RUN_ROOT/stale.meta.json" \
  node "$ACP_SCRIPT" --stop-daemon >"$RUN_ROOT/stop.stdout" 2>"$RUN_ROOT/stop.stderr"; then
  echo "expected stop-daemon to refuse stale live pid" >&2
  exit 1
fi
kill -0 "$SLEEP_PID" 2>/dev/null
grep -q 'refusing to signal pid' "$RUN_ROOT/stop.stderr"

printf '%s\n' \
  'import fs from "node:fs";' \
  'import net from "node:net";' \
  'const socketPath = process.argv[2];' \
  'const shutdownPath = process.argv[3];' \
  'fs.rmSync(socketPath, { force: true });' \
  'const server = net.createServer((socket) => {' \
  '  socket.setEncoding("utf8");' \
  '  socket.on("data", (chunk) => {' \
  '    const request = JSON.parse(chunk.trim());' \
  '    if (request.control === "fingerprint") {' \
  '      socket.end(JSON.stringify({ ok: true, fingerprint: "different" }) + "\n");' \
  '      return;' \
  '    }' \
  '    if (request.control === "shutdown") {' \
  '      fs.writeFileSync(shutdownPath, "shutdown\\n");' \
  '      socket.end(JSON.stringify({ ok: true }) + "\n");' \
  '    }' \
  '  });' \
  '});' \
  'server.listen(socketPath);' \
  > "$RUN_ROOT/fake-daemon.mjs"
node "$RUN_ROOT/fake-daemon.mjs" "$RUN_ROOT/mismatch.sock" "$RUN_ROOT/mismatch.shutdown" &
FAKE_DAEMON_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -S "$RUN_ROOT/mismatch.sock" ] && break
  sleep 0.1
done
if HOME="$RUN_ROOT/home" EXPLORE_ACP_SOCKET="$RUN_ROOT/mismatch.sock" EXPLORE_ACP_PID="$RUN_ROOT/mismatch.pid" EXPLORE_ACP_LOG="$RUN_ROOT/mismatch.log" EXPLORE_ACP_META="$RUN_ROOT/mismatch.meta.json" \
  node "$ACP_SCRIPT" --stop-daemon >"$RUN_ROOT/mismatch.stdout" 2>"$RUN_ROOT/mismatch.stderr"; then
  echo "expected stop-daemon to refuse mismatched responsive daemon" >&2
  exit 1
fi
[ ! -e "$RUN_ROOT/mismatch.shutdown" ]
grep -q 'environment fingerprint does not match' "$RUN_ROOT/mismatch.stderr"
kill "$FAKE_DAEMON_PID" 2>/dev/null || true
wait "$FAKE_DAEMON_PID" 2>/dev/null || true
FAKE_DAEMON_PID=""

echo "ACP feedback regression passed"
